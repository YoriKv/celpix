"""The LZ compression codecs: known-vector decodes, round trips, edge cases.

The decode vectors are hand-assembled from the format specification
(``docs/graphics-formats-reference/implementation-guide.md``), so they guard
the bit/byte math independently of our own compressor.
"""

from __future__ import annotations

import random

import pytest

from celpix.core.context import (
    KEY_COMPRESSED_SIZE,
    KEY_DECOMPRESS_COMPLETE,
    KEY_DECOMPRESS_PARTIAL,
    PipelineContext,
)
from celpix.plugins.builtins import (
    enigma,
    gba_lz77,
    konami_rle,
    kosinski,
    lz16,
    lz_command,
    lzss_ring,
    nemesis,
    packbits,
    prs,
    slz,
)
from celpix.plugins.builtins._lz import MatchFinder
from celpix.plugins.builtins.gba_lz77 import GbaLz77Compression
from celpix.plugins.builtins.konami_rle import KonamiNesRle
from celpix.plugins.builtins.lz16 import (
    KEY_LZ16_ROWS,
    Lz16Compression,
    Lz16ImprovedCompression,
)
from celpix.plugins.builtins.lz_command import Lz1, Lz1Improved, Lz2, Lz2Improved
from celpix.plugins.builtins.lzss_ring import LzssRingCompression
from celpix.plugins.builtins.packbits import PackBitsCompression
from celpix.plugins.builtins.prs import PrsCompression
from celpix.plugins.builtins.slz import Slz16Compression, Slz24Compression

# -- LZ1/LZ2 command stream -------------------------------------------------

# One command of each kind. Output: "ABC" + "DDDD" + "XYX" + [5,6,7] + first 4
# output bytes again (backref to offset 0), then the terminator.
_VECTOR_OUT = b"ABC" + b"DDDD" + b"XYX" + bytes((5, 6, 7)) + b"ABCD"
_VECTOR_BODY = [
    0x02,
    0x41,
    0x42,
    0x43,  # literal x3: "ABC"
    0x23,
    0x44,  # byte fill x4: "D"
    0x42,
    0x58,
    0x59,  # word fill x3: "XYX"
    0x62,
    0x05,  # increasing fill x3: 5,6,7
]
_VECTOR_TAIL = [0x83, 0x00, 0x00, 0xFF]  # backref x4 @0 (BE=LE here), terminator


def test_lz2_decode_known_vector() -> None:
    stream = bytes(_VECTOR_BODY + _VECTOR_TAIL)
    out, consumed = lz_command.decompress(stream, big_endian_offsets=True)
    assert out == _VECTOR_OUT
    assert consumed == len(stream)


def test_lz1_offset_is_little_endian() -> None:
    # A backref at offset 0x0001 distinguishes the byte orders: LE reads
    # (0x01, 0x00), BE would read offset 0x0100 and fail (unwritten output).
    stream = bytes([0x01, 0x41, 0x42, 0x81, 0x01, 0x00, 0xFF])
    out, _ = lz_command.decompress(stream, big_endian_offsets=False)
    assert out == b"AB" + b"B" * 2
    with pytest.raises(ValueError):
        lz_command.decompress(stream, big_endian_offsets=True)


def test_lz2_long_form_length() -> None:
    # Long-form byte fill of 300 zeros: header 111 001 LL, L=299.
    length = 300
    encoded = length - 1
    stream = bytes([0xE0 | (0x20 >> 3) | (encoded >> 8), encoded & 0xFF, 0x00, 0xFF])
    out, _ = lz_command.decompress(stream, big_endian_offsets=True)
    assert out == bytes(length)


def test_lz2_overlapping_backref_extends_runs() -> None:
    # Backref reaching past the current output end re-reads its own output —
    # the format's run-extension idiom.
    stream = bytes([0x01, 0x11, 0x22, 0x85, 0x00, 0x00, 0xFF])
    out, _ = lz_command.decompress(stream, big_endian_offsets=True)
    assert out == bytes([0x11, 0x22, 0x11, 0x22, 0x11, 0x22, 0x11, 0x22])


@pytest.mark.parametrize("big_endian", [False, True])
@pytest.mark.parametrize("pack", [lz_command.compress, lz_command.compress_improved])
def test_lz_round_trip(pack, big_endian: bool) -> None:
    rng = random.Random(1)
    payloads = [
        b"",
        b"\x00" * 2000,
        bytes(range(256)) * 5,
        bytes(rng.randrange(256) for _ in range(3000)),
        bytes(rng.choice(b"\x00\x0f\xf0") for _ in range(1000)),
    ]
    for data in payloads:
        packed = pack(data, big_endian_offsets=big_endian)
        out, consumed = lz_command.decompress(
            packed + b"\x5a" * 9, big_endian_offsets=big_endian
        )
        assert out == data
        # Trailing garbage is never consumed — the terminator bounds the read.
        assert consumed == len(packed)


def _commands(stream: bytes) -> list[tuple[int, int, int]]:
    """A stream as ``(op, length, offset)`` per command, offset 0 but on a
    backreference.

    What separates the two parses is which commands they choose, and a round trip
    is blind to that: both decode to the same bytes whatever they emit. So the
    byte-exact parse's rules have to be asserted on the command list itself.
    """
    out: list[tuple[int, int, int]] = []
    i = 0
    while stream[i] != 0xFF:
        header = stream[i]
        i += 1
        if (header & 0xE0) == 0xE0:
            op = (header << 3) & 0xE0
            length = (((header & 0x03) << 8) | stream[i]) + 1
            i += 1
        else:
            op = header & 0xE0
            length = (header & 0x1F) + 1
        offset = 0
        if op == 0x00:
            i += length
        elif op == 0x40:
            i += 2
        elif op == 0x80:
            offset = (stream[i] << 8) | stream[i + 1]
            i += 2
        else:
            i += 1
        out.append((op, length, offset))
    return out


def test_lz_original_parse_yields_the_boundary_byte_to_a_fill() -> None:
    # The pre-emption rule, in the smallest data that shows it: a run is taken
    # wherever it starts and every other command stops short of it, so this codes
    # as word=4 + fill=5 rather than the word run eating the fifth 0x00.
    data = bytes((0x00, 0xFF, 0x00, 0xFF, 0x00, 0x00, 0x00, 0x00, 0x00))
    packed = lz_command.compress(data, big_endian_offsets=True)
    assert _commands(packed) == [(0x40, 4, 0), (0x20, 5, 0)]
    assert lz_command.decompress(packed, big_endian_offsets=True)[0] == data


def test_lz_original_increasing_run_never_wraps() -> None:
    # The decoder wraps mod 256; the original encoder never emitted a run that
    # needed it, so 0xFE 0xFF 0x00 is a run of 2 and falls through to literals.
    assert _commands(lz_command.compress(b"\x10\x11\x12", big_endian_offsets=True)) == [
        (0x60, 3, 0)
    ]
    assert _commands(lz_command.compress(b"\xfe\xff\x00", big_endian_offsets=True)) == [
        (0x00, 3, 0)
    ]


def test_lz_original_backref_stops_where_a_run_starts() -> None:
    # The headline consequence: a 17-byte match is split into backref/fill/backref
    # because a fill run begins inside it. The match is not truncated for cost —
    # the shortest path would take it whole — so nothing but the rule explains it.
    block = b"\x10\x77\x31\x99\x2a\x5c\x08\xe3" + b"\x00" * 5 + b"\x41\x93\x7d\x22"
    packed = lz_command.compress(block * 2, big_endian_offsets=True)
    assert _commands(packed) == [
        (0x00, 8, 0),  # literal — no run fires on these bytes
        (0x20, 5, 0),  # fill of five 0x00
        (0x00, 4, 0),  # literal
        (0x80, 8, 0),  # backref — stops at the fill starting inside the copy
        (0x20, 5, 0),
        (0x80, 4, 13),
    ]
    assert lz_command.decompress(packed, big_endian_offsets=True)[0] == block * 2


def test_lz_original_backref_ties_take_the_earliest_offset() -> None:
    # Two candidates match "ABCD" equally at the third occurrence; the original
    # encoder took the first occurrence, not the nearest — the opposite of what a
    # newest-first chain walk hands you.
    block = b"\x10\x77\x31\x99"
    data = block + b"\x5a" + block + b"\xa5" + block
    packed = lz_command.compress(data, big_endian_offsets=True)
    # Both copies point at offset 0; a newest-first walk would give the second
    # one offset 5.
    assert [c for c in _commands(packed) if c[0] == 0x80] == [(0x80, 4, 0)] * 2


def test_lz_improved_parse_is_smaller_and_the_plugins_pick_their_own() -> None:
    # The two parses are the only difference between the plugin pairs, and both
    # write the same format — so either plugin reads either stream.
    rng = random.Random(7)
    data = bytes(rng.choice(b"\x00\x0f\xf0") for _ in range(1000))
    exact = lz_command.compress(data, big_endian_offsets=True)
    improved = lz_command.compress_improved(data, big_endian_offsets=True)
    assert len(improved) < len(exact)

    ctx = PipelineContext()
    assert Lz2().compress(data, ctx) == exact
    assert Lz2Improved().compress(data, ctx) == improved
    assert Lz2().decompress(improved, PipelineContext()) == data
    # LZ1 is the same parse over little-endian offsets, so only its backreference
    # payloads differ from LZ2's.
    assert Lz1().compress(data, ctx) != exact
    assert Lz1().decompress(Lz1().compress(data, ctx), PipelineContext()) == data
    assert Lz1Improved().compress(data, ctx) == lz_command.compress_improved(
        data, big_endian_offsets=False
    )


def test_match_finder_seeded_scan_never_loses_to_the_plain_search() -> None:
    # `all_longest` seeds each position from the previous position's match rather
    # than walking the chain from scratch. If the shortcut ever missed a longer
    # match nothing would fail — the stream still round-trips, it is just bigger —
    # so what it guarantees has to be asserted directly. The seed only raises the
    # bar the walk then has to beat, and it can name a candidate older than the
    # chain cap reaches, so the result is never *worse* than the plain search and
    # is sometimes better; every reported match must still be a real one.
    rng = random.Random(11)
    payloads = [
        bytes(64),
        b"abcabcabc" * 40,
        bytes(rng.choice(b"\x00\x0f\xf0\xff") for _ in range(1200)),
        bytes(rng.randrange(256) for _ in range(600)),
    ]
    for data in payloads:
        n = len(data)
        kwargs = {"min_match": 3, "window": None, "max_candidates": 64}
        lengths, offsets = MatchFinder(data, **kwargs).all_longest(1024)
        reference = MatchFinder(data, **kwargs)
        for pos in range(n):
            want, _ = reference.longest(pos, min(n - pos, 1024))
            reference.add(pos)
            assert lengths[pos] >= (want if want >= 3 else 0)
            if lengths[pos]:
                end = offsets[pos] + lengths[pos]
                assert offsets[pos] < pos
                assert data[pos : pos + lengths[pos]] == data[offsets[pos] : end]


def test_lz_partial_decode_returns_valid_prefix() -> None:
    # A bounded window can cut a structure short: partial mode returns the
    # prefix decoded so far, strict mode keeps raising.
    rng = random.Random(3)
    data = bytes(rng.randrange(256) for _ in range(400))
    packed = lz_command.compress(data, big_endian_offsets=True)
    cut = packed[: len(packed) // 2]
    with pytest.raises(ValueError):
        lz_command.decompress(cut, big_endian_offsets=True)
    out, consumed = lz_command.decompress(
        cut, big_endian_offsets=True, allow_partial=True
    )
    assert 0 < len(out) < len(data)
    assert data[: len(out)] == out
    assert consumed == len(cut)


def test_lz_partial_still_rejects_corrupt_streams() -> None:
    # Structural corruption (backref into unwritten output) is not truncation;
    # partial mode must still refuse — that's the overlay's validity signal.
    stream = b"\x83\xff\xff" + bytes(40)
    with pytest.raises(ValueError):
        lz_command.decompress(stream, big_endian_offsets=True, allow_partial=True)


def test_lz_plugin_honours_partial_context_flag() -> None:
    data = bytes(range(64)) * 3
    packed = Lz2().compress(data, PipelineContext())
    ctx = PipelineContext()
    ctx.set(KEY_DECOMPRESS_PARTIAL, True)
    out = Lz2().decompress(packed[:-1], ctx)  # terminator cut off
    assert data[: len(out)] == out
    assert ctx.get(KEY_DECOMPRESS_COMPLETE) is False  # truncated: end unknown


def test_lz_malformed_raises() -> None:
    with pytest.raises(ValueError):  # no terminator
        lz_command.decompress(b"\x03\x41", big_endian_offsets=True)
    with pytest.raises(ValueError):  # backref into unwritten output
        lz_command.decompress(b"\x82\x12\x34\xff", big_endian_offsets=True)


def test_lz_plugins_record_compressed_size() -> None:
    data = b"\x07" * 100
    packed = Lz2().compress(data, PipelineContext())
    ctx = PipelineContext()
    # LZ1 and LZ2 agree on everything but backrefs; an all-fill stream decodes
    # identically, which keeps this plugin-level check codec-agnostic.
    assert Lz1().decompress(packed + b"\x00" * 3, ctx) == data
    assert ctx.get(KEY_COMPRESSED_SIZE) == len(packed)
    assert ctx.get(KEY_DECOMPRESS_COMPLETE) is True  # terminator = known end


# -- LZ16 -------------------------------------------------------------------


def _tile_payloads() -> list[tuple[bytes, int]]:
    rng = random.Random(2)
    return [
        (bytes(512), 1),
        (bytes((i * 7) & 0xFF for i in range(1024)), 2),
        (bytes(rng.randrange(256) for _ in range(1536)), 3),
    ]


def test_lz16_round_trip_and_probe() -> None:
    for tiles, rows in _tile_payloads():
        packed = lz16.compress(tiles)
        out, consumed = lz16.decompress(packed, rows)
        assert out == tiles
        assert consumed == len(packed)
        # With an exactly-sized buffer the row count is recoverable.
        assert lz16.probe_rows(packed) == rows


def test_lz16_probe_rejects_overread_data() -> None:
    packed = lz16.compress(bytes(512))
    with pytest.raises(ValueError):
        lz16.probe_rows(packed + b"\x00" * 4)


def test_lz16_plugin_probes_and_records_context() -> None:
    tiles, rows = _tile_payloads()[1]
    packed = lz16.compress(tiles)
    ctx = PipelineContext()
    assert Lz16Compression().decompress(packed, ctx) == tiles
    assert ctx.get(KEY_LZ16_ROWS) == rows
    assert ctx.get(KEY_COMPRESSED_SIZE) == len(packed)


def test_lz16_plugin_honours_explicit_rows() -> None:
    # An over-read buffer defeats the probe, but an explicit row count from the
    # context still decodes it.
    tiles, rows = _tile_payloads()[1]
    packed = lz16.compress(tiles)
    ctx = PipelineContext()
    ctx.set(KEY_LZ16_ROWS, rows)
    assert Lz16Compression().decompress(packed + b"\xa5" * 5, ctx) == tiles


def test_lz16_partial_decode_recovers_leading_rows() -> None:
    # A window extending past the structure decodes into trailing garbage; the
    # completed leading tile rows survive, and the real rows come back intact.
    tiles, rows = _tile_payloads()[1]
    packed = lz16.compress(tiles)
    out, got_rows, consumed = lz16.decompress_partial(packed + b"\x00" * 40)
    assert got_rows >= rows
    assert out[: len(tiles)] == tiles
    assert consumed >= len(packed)

    ctx = PipelineContext()
    ctx.set(KEY_DECOMPRESS_PARTIAL, True)
    assert (
        Lz16Compression().decompress(packed + b"\x00" * 40, ctx)[: len(tiles)] == tiles
    )


def test_lz16_partial_rejects_non_lz16_data() -> None:
    # The first tile row is the validity test — data that can't even produce
    # one row is "not LZ16", not a truncated structure.
    with pytest.raises(ValueError):
        lz16.decompress_partial(b"\x12\x34")


def test_lz16_improved_beats_the_byte_exact_parse_and_still_round_trips() -> None:
    # The two compressors differ only in predictor ranking, so they only diverge
    # on data where the two rankings disagree: a background color that is nearly
    # always *unchanged* row-to-row is cheap by run frequency but invisible to
    # the color-change count, which spends its seven slots on the figures. Eight
    # colors changing per row push it out.
    pixels = bytearray(128 * 8)
    for y in range(8):
        for i in range(8):
            color = (y * 8 + i) % 15 + 1
            pixels[y * 128 + i * 4 : y * 128 + i * 4 + 2] = bytes([color, color])
    tiles = lz16._pixels_to_tiles(pixels, 1)

    exact = lz16.compress(tiles)
    improved = lz16.compress_improved(tiles)
    assert len(improved) < len(exact)
    assert lz16.decompress(exact, 1)[0] == tiles
    assert lz16.decompress(improved, 1)[0] == tiles

    ctx = PipelineContext()
    assert Lz16ImprovedCompression().compress(tiles, ctx) == improved
    # Decoding is shared: either plugin reads either stream.
    assert Lz16Compression().decompress(improved, PipelineContext()) == tiles


def test_lz16_compress_rejects_partial_tile_rows() -> None:
    with pytest.raises(ValueError):
        lz16.compress(bytes(511))
    with pytest.raises(ValueError):
        lz16.compress(b"")


# -- Konami NES RLE ---------------------------------------------------------


def test_konami_round_trip() -> None:
    rng = random.Random(4)
    payloads = [
        b"",
        b"\x42",
        b"\x55" * 300,  # long run: forces multi-chunk fills past the 126 cap
        bytes(range(256)),  # all distinct: every byte a literal
        b"\xab" * 125,  # runs bracketing the 126-byte fill boundary
        b"\xab" * 126,
        b"\xab" * 127,
        b"\xcd" * 252,
        bytes(range(126)),  # literal blocks bracketing the boundary
        bytes(range(127)),
        bytes([0x7F, 0x80, 0xFF]) * 40,  # values colliding with control bytes
        b"\x7f" * 130 + b"\x80" * 3 + b"\xff" * 200,
        b"AB" * 5 + b"C" * 10 + b"D" + b"EFG" + b"H" * 200,  # mixed short/long
        bytes(rng.randrange(256) for _ in range(2000)),
        bytes(rng.choice(b"\x00\x7f\x80\xff") for _ in range(1500)),
    ]
    for data in payloads:
        packed = konami_rle.compress(data)
        # 0x11 trailing garbage is itself a valid control byte, so this also
        # checks the terminator — not buffer exhaustion — bounds the read.
        out, consumed, complete = konami_rle.decompress(packed + b"\x11" * 7)
        assert out == data
        assert complete is True
        assert consumed == len(packed)


def test_konami_decode_known_vector() -> None:
    # One fill, one literal, a 0x7F PPU-address-change (the next 2 bytes are the
    # little-endian destination 0x1234 — consumed, not emitted), a second fill,
    # then the 0xFF terminator. Guards the decoder independently of our own
    # compressor, and pins the address-change skip: the address low byte 0x34
    # must NOT be mistaken for a fill-52 control (the Contra-family desync bug).
    stream = bytes([0x03, 0xAA, 0x82, 0x11, 0x22, 0x7F, 0x34, 0x12, 0x02, 0xBB, 0xFF])
    out, consumed, complete = konami_rle.decompress(stream)
    assert out == bytes([0xAA, 0xAA, 0xAA, 0x11, 0x22, 0xBB, 0xBB])
    assert consumed == len(stream)
    assert complete is True


def test_konami_long_run_caps_fill_chunks() -> None:
    # 300 identical bytes exceed the 126-byte fill ceiling, so the run must
    # split into three fills (126 + 126 + 48); a single oversized count would
    # collide with the 0x7F/0xFF control values and decode wrong.
    packed = konami_rle.compress(b"\x55" * 300)
    assert len(packed) == 3 * 2 + 1  # three (count, value) fills + terminator
    out, consumed, complete = konami_rle.decompress(packed)
    assert out == b"\x55" * 300
    assert complete is True


def test_konami_truncated_stream_decodes_prefix() -> None:
    # A buffer cut mid-literal (before the terminator) yields the prefix
    # decoded so far, flagged incomplete — the bounded-window / truncated-dump
    # case the decoder must survive.
    data = bytes(range(200))  # all distinct: literal-heavy stream
    packed = konami_rle.compress(data)
    cut = packed[: len(packed) - 30]
    out, consumed, complete = konami_rle.decompress(cut)
    assert complete is False
    assert consumed <= len(cut)
    assert 0 < len(out) < len(data)
    assert data[: len(out)] == out


def test_konami_plugins_record_size_and_round_trip() -> None:
    data = b"\x00" * 50 + bytes(range(30)) + b"\xff" * 40
    packed = KonamiNesRle().compress(data, PipelineContext())
    ctx = PipelineContext()
    out = KonamiNesRle().decompress(packed + b"\x5a" * 6, ctx)
    assert out == data
    # The terminator position is the structure's byte length; trailing garbage
    # past it is not counted.
    assert ctx.get(KEY_COMPRESSED_SIZE) == len(packed)
    assert ctx.get(KEY_DECOMPRESS_COMPLETE) is True


def test_konami_fds_decode_known_vector() -> None:
    # The FDS reading of the two reserved control bytes: 0x7F is a 127-byte fill
    # (repeat the next byte 127 times) and 0x80 is a 256-byte literal (copy the
    # next 256 bytes verbatim, control-valued payload included). Interleaved with
    # a normal fill and literal, then the shared 0xFF terminator. Guards that
    # fds=True reads 0x7F/0x80 the GraveyardDuck way rather than as an
    # address-change / no-op, independently of our own compressor.
    literal256 = bytes(range(256))  # payload spans 0x7F/0x80/0xFF verbatim
    stream = (
        bytes([0x03, 0xAA])  # fill x3
        + bytes([0x82, 0x11, 0x22])  # literal x2
        + bytes([0x7F, 0xCC])  # FDS 127-fill of 0xCC
        + bytes([0x80])
        + literal256  # FDS 256-byte literal
        + bytes([0xFF])  # terminator
    )
    out, consumed, complete = konami_rle.decompress(stream, fds=True)
    assert (
        out
        == bytes([0xAA]) * 3 + bytes([0x11, 0x22]) + bytes([0xCC]) * 127 + literal256
    )
    assert consumed == len(stream)
    assert complete is True


def test_konami_variant_flag_switches_control_semantics() -> None:
    # One stream, two readings. After a fill both agree on, a 0x7F diverges: the
    # Contra reading treats it as a PPU address change (skip the next 2 bytes,
    # emit nothing more, and the trailing 0xFF is a clean terminator), while the
    # FDS reading treats it as a 127-fill of 0x41 and re-frames the rest — so the
    # two paths cannot collapse into one.
    stream = bytes([0x02, 0x30, 0x7F, 0x41, 0x42, 0xFF])

    contra_out, contra_consumed, contra_complete = konami_rle.decompress(
        stream, fds=False
    )
    assert contra_out == bytes([0x30, 0x30])
    assert contra_consumed == len(stream)
    assert contra_complete is True

    fds_out, fds_consumed, _ = konami_rle.decompress(stream, fds=True)
    assert fds_out == bytes([0x30, 0x30]) + bytes([0x41]) * 127 + bytes([0xFF]) * 66
    assert fds_consumed == len(stream)
    assert fds_out != contra_out


def test_konami_fds_round_trip() -> None:
    # The shared compressor stays in the unambiguous subset (no 0x7F/0x80), so
    # the very same packed bytes must also round-trip under the FDS decoder, not
    # just the Contra one already covered above.
    rng = random.Random(5)
    payloads = [
        b"\x00" * 400,  # long run: multi-chunk fills past the 126 cap
        bytes(range(256)),  # all distinct: every byte a literal
        bytes([0x7F, 0x80, 0xFF]) * 60,  # values colliding with control bytes
        bytes(rng.randrange(256) for _ in range(2000)),
    ]
    for data in payloads:
        packed = konami_rle.compress(data)
        # 0x11 trailing garbage is a valid control byte, so this also checks the
        # terminator — not buffer exhaustion — bounds the FDS read.
        out, consumed, complete = konami_rle.decompress(packed + b"\x11" * 7, fds=True)
        assert out == data
        assert complete is True
        assert consumed == len(packed)


# -- PackBits ---------------------------------------------------------------


def test_packbits_decode_known_vector() -> None:
    # The worked example from the format's own documentation: a 3-run, a 3-byte
    # literal, a 4-run, a 4-byte literal and a 10-run. Guards the signed control
    # arithmetic (0xFE = -2 is a *3*-byte run, not a 2-byte one) independently of
    # our compressor, and pins that literal payloads may hold 0x80 verbatim.
    stream = bytes.fromhex("fe aa 02 80 00 2a fd aa 03 80 00 2a 22 f7 aa")
    out, consumed = packbits.decompress(stream)
    assert out == bytes.fromhex(
        "aa aa aa 80 00 2a aa aa aa aa 80 00 2a 22 aa aa aa aa aa aa aa aa aa aa"
    )
    assert consumed == len(stream)
    # Our encoder reproduces this stream byte for byte — the runs are all ≥3, so
    # the run/literal split has no leeway.
    assert packbits.compress(out) == stream


def test_packbits_round_trip() -> None:
    rng = random.Random(7)
    payloads = [
        b"",
        b"\x42",
        b"\x42\x42",  # a bare 2-run: the packet-vs-literal boundary case
        b"\x55" * 300,  # long run: multi-packet, 128 + 128 + a 44 tail
        b"\x55" * 130,  # 128-run plus a 2-byte tail (its own run packet)
        b"\x55" * 129,  # 128-run plus a lone byte (a literal)
        b"\xab" * 128,  # exactly one full run packet
        bytes(range(128)),  # exactly one full literal packet
        bytes(range(129)),
        bytes(range(256)),  # all distinct: literal-only
        bytes([0x80]) * 200,  # the no-op control value as data
        b"AB" * 5 + b"C" * 10 + b"D" + b"EFG" + b"H" * 200,
        bytes(rng.randrange(256) for _ in range(2000)),
        bytes(rng.choice(b"\x00\x80\xff") for _ in range(1500)),
    ]
    for data in payloads:
        packed = packbits.compress(data)
        out, consumed = packbits.decompress(packed)
        assert out == data
        assert consumed == len(packed)
        # One control byte per 128 output bytes is the format's worst case.
        assert len(packed) <= len(data) + (len(data) + 127) // 128


def test_packbits_nop_control_is_skipped() -> None:
    # 0x80 as a *control* byte carries no data of its own: the byte after it is
    # the next control. Reading it as a run (or as an end marker) would desync
    # everything following.
    stream = bytes([0x80, 0x01, 0x41, 0x42, 0x80, 0xFF, 0x43])
    out, consumed = packbits.decompress(stream)
    assert out == b"AB" + b"CC"
    assert consumed == len(stream)


def test_packbits_truncated_stream_decodes_prefix() -> None:
    # A window can cut a packet in half. The literal bytes that *are* present
    # still decode — the overlay preview wants them — but `consumed` stops at the
    # last whole packet, since only that offset is a real packet boundary.
    stream = bytes([0x01, 0x41, 0x42, 0x03, 0x43, 0x44])  # 4-byte literal, 2 given
    out, consumed = packbits.decompress(stream)
    assert out == b"ABCD"
    assert consumed == 3
    # A run header with no value byte contributes nothing at all.
    out, consumed = packbits.decompress(bytes([0x01, 0x41, 0x42, 0xFE]))
    assert out == b"AB"
    assert consumed == 3


def test_packbits_plugin_records_size_but_never_complete() -> None:
    # The format has no terminator, so a decode that reaches the end of the
    # buffer is "the buffer ran out", not "the structure ended". Reporting
    # complete would let a slice with no length backfill its extent as the whole
    # rest of the file — hence the flag is always False, unlike every other
    # scheme here.
    data = b"\x00" * 50 + bytes(range(30)) + b"\xff" * 40
    packed = PackBitsCompression().compress(data, PipelineContext())
    ctx = PipelineContext()
    assert PackBitsCompression().decompress(packed, ctx) == data
    assert ctx.get(KEY_COMPRESSED_SIZE) == len(packed)
    assert ctx.get(KEY_DECOMPRESS_COMPLETE) is False


def test_packbits_output_cap_stops_an_unbounded_read() -> None:
    # Any bytes are valid PackBits, so a read that runs to end-of-file expands
    # arbitrary data by up to 128x. The cap stops the decode at a packet
    # boundary instead of raising: there is no stream end to be inconsistent
    # with, so over-long output isn't evidence of corruption.
    bomb = bytes([0x81, 0x00]) * 20000  # 128 output bytes per 2 input bytes
    out, consumed = packbits.decompress(bomb)
    assert len(out) == 0x100000
    assert consumed == 2 * (0x100000 // 128) < len(bomb)


# The FDS plugin wrapper (KonamiFdsRle*) is a copy of the NES wrapper differing
# only in the fds flag, and the shared compressor stays in the unambiguous
# subset — so a plugin round-trip there decodes identically to the NES one and
# adds nothing over test_konami_plugins_record_size_and_round_trip. The FDS-only
# decode behaviour is guarded by test_konami_variant_flag_switches_control_
# semantics and test_konami_fds_round_trip.


# -- LZSS, 4 KiB ring, size-prefixed ----------------------------------------


def test_lzss_decode_known_vector() -> None:
    # Two literals then a distance-2 back-reference of 5, spelled from the spec:
    # ring position (0xFEE + 0) & 0xFFF for output position 0, length 5 - 3 = 2.
    stream = bytes([0x07, 0x00, 0x00, 0x00, 0x03, 0x41, 0x42, 0xEE, 0xF2])
    out, consumed, complete = lzss_ring.decompress(stream)
    assert out == b"ABABABA"  # "AB" + 5 bytes copied from position 0, overlapping
    assert (consumed, complete) == (len(stream), True)


def test_lzss_reference_before_the_output_reads_the_rings_zero_fill() -> None:
    # The ring is zero-filled and its cursor starts at 0xFEE, so a reference to a
    # position the output has not reached yet is legal and yields zeros. Getting
    # the cursor origin wrong still decodes — it just silently reads the wrong
    # slots — so this is the test that pins 0xFEE.
    stream = bytes([0x03, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])
    out, _, complete = lzss_ring.decompress(stream)
    assert out == b"\x00\x00\x00"
    assert complete


def test_lzss_size_prefix_bounds_the_decode() -> None:
    # The body carries no terminator: the declared size is the only thing that
    # ends it, and anything past the structure must be left for the next reader.
    stream = bytes([0x02, 0x00, 0x00, 0x00, 0x03, 0x41, 0x42]) + b"junk"
    out, consumed, complete = lzss_ring.decompress(stream)
    assert out == b"AB"
    assert (consumed, complete) == (7, True)


def test_lzss_truncated_stream_needs_the_partial_flag() -> None:
    full = lzss_ring.compress(b"the quick brown fox " * 40)
    cut = full[: len(full) // 2]
    with pytest.raises(ValueError):
        lzss_ring.decompress(cut)
    out, _, complete = lzss_ring.decompress(cut, partial=True)
    assert complete is False
    assert out and (b"the quick brown fox " * 40).startswith(out)


def test_lzss_round_trip_across_shapes() -> None:
    random.seed(1105)
    for raw in (
        b"A",
        b"\x00" * 5000,  # long runs: the self-overlap path
        bytes(range(256)) * 40,  # past the 4 KiB ring, so matches age out
        bytes(random.randrange(256) for _ in range(3000)),  # incompressible
        bytes(random.choice(b"\x00\x01\xff") for _ in range(9000)),
    ):
        packed = lzss_ring.compress(raw)
        out, consumed, complete = lzss_ring.decompress(packed)
        assert out == raw
        assert (consumed, complete) == (len(packed), True)


def test_lzss_plugin_records_size_and_completeness() -> None:
    raw = bytes(range(64)) * 30
    plugin = LzssRingCompression()
    packed = plugin.compress(raw, PipelineContext())
    ctx = PipelineContext()
    assert plugin.decompress(packed + b"\xff" * 16, ctx) == raw
    assert ctx.get(KEY_COMPRESSED_SIZE) == len(packed)
    assert ctx.get(KEY_DECOMPRESS_COMPLETE) is True


# -- PRS ---------------------------------------------------------------------

# Literal 'A', literal 'B', short copy (distance 2, length 4), long copy
# (distance 6, length 3), end marker. Assembled from the spec, so it guards the
# reader independently of our own writer — including the interleave: the second
# control byte (0x02) sits at index 6, *after* the long copy's operand bytes
# D1 FF, because a control byte is fetched only when a bit from it is needed.
# Our compressor spells these nine bytes more cheaply (one length-7 long copy),
# which is why this is a decode vector rather than a round-trip one.
_PRS_VECTOR = bytes([0x93, 0x41, 0x42, 0xFE, 0xD1, 0xFF, 0x02, 0x00, 0x00])


def test_prs_decode_known_vector() -> None:
    out, consumed, complete = prs.decompress(_PRS_VECTOR)
    assert out == b"ABABABABA"
    assert (consumed, complete) == (len(_PRS_VECTOR), True)


def test_prs_control_bytes_interleave_lazily() -> None:
    # The writer must place control bytes exactly where the reader fetches them:
    # only when a bit is actually needed. Nine distinct bytes are nine literals,
    # so the first control byte's eight bits run out mid-run and the second lands
    # *between* the eighth and ninth literal rather than up front.
    packed = prs.compress(bytes(range(9)))
    assert packed == bytes([0xFF]) + bytes(range(8)) + bytes([0x05, 0x08, 0x00, 0x00])
    assert prs.decompress(packed)[0] == bytes(range(9))


def test_prs_end_marker_bounds_the_decode() -> None:
    out, consumed, _ = prs.decompress(_PRS_VECTOR + b"trailing junk")
    assert out == b"ABABABABA"
    assert consumed == len(_PRS_VECTOR)


def test_prs_truncated_stream_needs_the_partial_flag() -> None:
    full = prs.compress(b"the quick brown fox " * 40)
    cut = full[: len(full) // 2]
    with pytest.raises(ValueError):
        prs.decompress(cut)
    out, consumed, complete = prs.decompress(cut, partial=True)
    assert complete is False
    assert consumed <= len(cut)
    assert out and (b"the quick brown fox " * 40).startswith(out)


def test_prs_copy_reaching_before_the_output_raises_even_when_partial() -> None:
    # A structurally impossible stream is corruption, not truncation, so the
    # partial flag must not paper over it.
    bad = bytes([0x02, 0xF9, 0xFF, 0x02, 0x00, 0x00])  # long copy at distance 1
    for partial in (False, True):
        with pytest.raises(ValueError):
            prs.decompress(bad, partial=partial)


def test_prs_round_trip_across_shapes() -> None:
    random.seed(1106)
    for raw in (
        b"",
        b"A",
        b"\x00" * 5000,  # 256-byte extended copies, overlapping
        bytes(range(256)) * 40,  # past the 8 KiB reach
        bytes(random.randrange(256) for _ in range(3000)),  # incompressible
        bytes(random.choice(b"\x00\x01\xff") for _ in range(9000)),
    ):
        packed = prs.compress(raw)
        out, consumed, complete = prs.decompress(packed)
        assert out == raw
        assert (consumed, complete) == (len(packed), True)


def test_prs_plugin_records_size_and_completeness() -> None:
    raw = bytes(range(64)) * 30
    plugin = PrsCompression()
    packed = plugin.compress(raw, PipelineContext())
    ctx = PipelineContext()
    assert plugin.decompress(packed + b"\xff" * 16, ctx) == raw
    assert ctx.get(KEY_COMPRESSED_SIZE) == len(packed)
    assert ctx.get(KEY_DECOMPRESS_COMPLETE) is True


# -- GBA/NDS BIOS LZ77 ------------------------------------------------------


@pytest.mark.parametrize(
    ("stream", "expected"),
    # Hand-assembled from the format spec, so they guard the three fields that
    # fail *silently* when read wrong: the flag byte's MSB-first order and its
    # set-means-back-reference sense, the length bias of 3, and the displacement
    # being stored one less than the distance. Read any of them the other way and
    # a stream still decodes to something of the right length.
    [
        # Flag 0x00: eight literals, of which the declared size wants four.
        ("10040000" + "00" + "41424344", b"ABCD"),
        # Flag 0x40: literal 'A', then bit 6 set -> a back-reference of b0=b1=0,
        # i.e. length 3 at displacement field 0, which is distance *1*.
        ("10040000" + "40" + "41" + "0000", b"AAAA"),
        # Flag 0x20: 'A', 'B', then b0=0x10 -> length 4, displacement field 1 ->
        # distance 2. The copy overlaps what it is still writing.
        ("10060000" + "20" + "4142" + "1001", b"ABABAB"),
        # b0=0xF0 -> the maximum length of 18, at distance 1.
        ("10140000" + "20" + "5a5a" + "f000", b"Z" * 20),
    ],
)
def test_gba_lz77_decode_known_vector(stream: str, expected: bytes) -> None:
    raw = bytes.fromhex(stream)
    out, consumed, complete = gba_lz77.decompress(raw)
    assert out == expected
    assert complete
    assert consumed == len(raw)


def test_gba_lz77_declared_size_cuts_a_match_short() -> None:
    """The size is the only terminator, and it does not fall on match boundaries.

    A decoder that finishes each back-reference before re-testing the limit
    overruns here by one byte - the last match is 18 long and only 17 are wanted.
    """
    out, _, complete = gba_lz77.decompress(
        bytes.fromhex("10130000" + "20" + "5a5a" + "f000")
    )
    assert out == b"Z" * 19
    assert complete


def test_gba_lz77_reference_before_the_start_is_corrupt() -> None:
    # Distance 2 with only one byte produced. The reference decoder reads
    # whatever precedes its buffer; there is no output to defend, so this raises.
    with pytest.raises(ValueError, match="before the start"):
        gba_lz77.decompress(bytes.fromhex("10040000" + "40" + "41" + "0001"))


def test_gba_lz77_rejects_a_non_lz77_header() -> None:
    # The high nibble is the BIOS's dispatch; 0x30 is RLE, which this is not.
    with pytest.raises(ValueError, match="not an LZ77 header"):
        gba_lz77.decompress(bytes.fromhex("30040000" + "00" + "41424344"))


@pytest.mark.parametrize(
    "data",
    [
        b"A",
        b"A" * 40,
        b"ABABAB" * 50,
        bytes(1024),
        bytes(range(256)) * 3,
        b"HELLO " * 300,
    ],
)
def test_gba_lz77_round_trips_and_stays_vram_safe(data: bytes) -> None:
    """Every emitted displacement must be at least 1, not 0.

    The BIOS has two entry points and the VRAM-safe one (SWI 0x12) cannot handle
    a stored displacement of 0. A ROM whose game calls it gets corrupt graphics
    from a stream that decodes perfectly under SWI 0x11, so the constraint is
    invisible to a round-trip test - it has to be asserted on the bytes.
    """
    stream = gba_lz77.compress(data)
    out, consumed, complete = gba_lz77.decompress(stream)
    assert out == data
    assert complete and consumed == len(stream)

    at = gba_lz77.HEADER_SIZE
    while at < len(stream):
        flags = stream[at]
        at += 1
        for bit in range(8):
            if at >= len(stream):
                break
            if flags & (0x80 >> bit):
                disp = ((stream[at] & 0x0F) << 8) | stream[at + 1]
                assert disp + 1 >= gba_lz77.MIN_DISTANCE
                at += 2
            else:
                at += 1


def test_gba_lz77_plugin_records_the_structures_extent() -> None:
    payload = b"the quick brown fox " * 40
    stream = gba_lz77.compress(payload)
    plugin = GbaLz77Compression()

    ctx = PipelineContext()
    # Trailing bytes stand in for whatever follows the structure in a ROM: the
    # recorded size must be the stream's own, not the buffer it arrived in.
    assert plugin.decompress(stream + b"\xff" * 64, ctx) == payload
    assert ctx.get(KEY_COMPRESSED_SIZE) == len(stream)
    assert ctx.get(KEY_DECOMPRESS_COMPLETE) is True

    # A window that cuts the stream short decodes to a prefix of the real output
    # rather than raising, which is what a bounded view preview needs.
    cut = PipelineContext()
    cut.set(KEY_DECOMPRESS_PARTIAL, True)
    prefix = plugin.decompress(stream[: len(stream) // 2], cut)
    assert 0 < len(prefix) < len(payload)
    assert payload.startswith(prefix)
    assert cut.get(KEY_DECOMPRESS_COMPLETE) is False


# -- SLZ ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stream", "expected"),
    # Hand-assembled from the format spec. The first two guard the field that
    # fails silently: this format puts the *distance* in the high twelve bits of
    # the reference word and biases it by 3, where the BIOS LZ77 above puts the
    # *length* in the high nibble and biases the distance by 1. Read either way a
    # stream decodes to something of about the right length.
    [
        # Token 0x00: eight literals, of which the declared size wants four.
        ("0004" + "00" + "41424344", b"ABCD"),
        # Token 0x10: three literals, then bit 3 set -> a reference of 0x0000,
        # i.e. distance 3 and length 3. Read as BIOS LZ77 this would be distance
        # 1, giving b"ABCCCC".
        ("0006" + "10" + "414243" + "0000", b"ABCABC"),
        # Token 0x08: four literals, then 0x0011 -> distance 4, length 4. Pins
        # the shift as well as the bias: a distance field read from the low
        # twelve bits would be 0x011, not 1.
        ("0008" + "08" + "41424344" + "0011", b"ABCDABCD"),
        # Length 18 at distance 3: the copy overlaps what it is still writing,
        # which is how the format run-length-encodes a repeating cell.
        ("0015" + "10" + "414243" + "000f", b"ABC" * 7),
    ],
)
def test_slz_decode_known_vector(stream: str, expected: bytes) -> None:
    raw = bytes.fromhex(stream)
    out, consumed, complete = slz.decompress(raw, size_bytes=slz.SIZE_BYTES_16)
    assert out == expected
    assert complete
    assert consumed == len(raw)


def test_slz24_reads_the_same_body_behind_a_wider_prefix() -> None:
    # The variants differ in the size prefix and nothing else, so the same body
    # must decode identically - and a 16-bit reader would take the third prefix
    # byte for a token and produce nonsense rather than failing.
    raw = bytes.fromhex("000004" + "00" + "41424344")
    out, consumed, complete = slz.decompress(raw, size_bytes=slz.SIZE_BYTES_24)
    assert out == b"ABCD"
    assert (consumed, complete) == (len(raw), True)


def test_slz_size_prefix_bounds_the_decode() -> None:
    # The body carries no terminator: the declared size is the only thing that
    # ends it, and anything past the structure must be left for the next reader.
    raw = bytes.fromhex("0004" + "00" + "41424344") + b"junk"
    out, consumed, complete = slz.decompress(raw, size_bytes=slz.SIZE_BYTES_16)
    assert out == b"ABCD"
    assert (consumed, complete) == (7, True)


def test_slz_overshooting_the_declared_size_is_corrupt() -> None:
    """Unlike the BIOS LZ77, the last match is never clipped.

    A well-formed stream lands on the declared size exactly, so a match that
    would carry the output past it means the tokens are not this stream's.
    Clipping instead would quietly accept a misread reference word.
    """
    raw = bytes.fromhex("0005" + "10" + "414243" + "0000")
    with pytest.raises(ValueError, match="against a declared"):
        slz.decompress(raw, size_bytes=slz.SIZE_BYTES_16)


def test_slz_reference_before_the_start_is_corrupt() -> None:
    # The 68000 decoder reads whatever precedes its buffer; there is no output
    # to defend, so this raises rather than inventing one.
    with pytest.raises(ValueError, match="before the start"):
        slz.decompress(bytes.fromhex("0004" + "80" + "0000"), size_bytes=2)


def test_slz_trailing_token_byte_is_not_part_of_the_structure() -> None:
    """The reference encoder flushes a full token group twice.

    A payload whose op count is an exact multiple of 8 gets a trailing empty
    token byte the decoder never reaches, so such a stream occupies one byte more
    than it consumes. Counting it would overstate the slot a save-back has to fit.
    """
    raw = bytes.fromhex("0008" + "00" + "4142434445464748") + b"\x00"
    out, consumed, complete = slz.decompress(raw, size_bytes=slz.SIZE_BYTES_16)
    assert out == b"ABCDEFGH"
    assert complete and consumed == len(raw) - 1


def test_slz_empty_payload_is_a_zero_prefix_and_round_trips() -> None:
    # The format's own encoding of empty, so it is written and accepted rather
    # than rejected the way the ring LZSS rejects a zero size.
    for size_bytes in (slz.SIZE_BYTES_16, slz.SIZE_BYTES_24):
        packed = slz.compress(b"", size_bytes=size_bytes)
        assert packed == bytes(size_bytes)
        assert slz.decompress(packed, size_bytes=size_bytes) == (b"", size_bytes, True)


@pytest.mark.parametrize("size_bytes", [slz.SIZE_BYTES_16, slz.SIZE_BYTES_24])
@pytest.mark.parametrize(
    "data",
    [
        b"A",
        b"A" * 40,
        b"ABABAB" * 50,  # matches nearer than the biased distance can name
        bytes(1024),
        bytes(range(256)) * 3,
        bytes(range(256)) * 24,  # past the 4098-byte window, so matches age out
        b"HELLO " * 300,
    ],
)
def test_slz_round_trips(data: bytes, size_bytes: int) -> None:
    stream = slz.compress(data, size_bytes=size_bytes)
    out, consumed, complete = slz.decompress(stream, size_bytes=size_bytes)
    assert out == data
    assert complete and consumed == len(stream)


def test_slz16_rejects_a_payload_its_size_field_cannot_hold() -> None:
    # Checked before the parse, so the 24-bit variant is the only way to carry it.
    with pytest.raises(ValueError, match="16-bit SLZ size field"):
        slz.compress(bytes(0x10000), size_bytes=slz.SIZE_BYTES_16)
    assert slz.compress(bytes(0x10000), size_bytes=slz.SIZE_BYTES_24)


@pytest.mark.parametrize(
    ("plugin", "size_bytes"),
    [
        (Slz16Compression(), slz.SIZE_BYTES_16),
        (Slz24Compression(), slz.SIZE_BYTES_24),
    ],
)
def test_slz_plugins_record_the_structures_extent(
    plugin: Slz16Compression | Slz24Compression, size_bytes: int
) -> None:
    payload = b"the quick brown fox " * 40
    stream = slz.compress(payload, size_bytes=size_bytes)

    ctx = PipelineContext()
    # Trailing bytes stand in for whatever follows the structure in a ROM: the
    # recorded size must be the stream's own, not the buffer it arrived in.
    assert plugin.decompress(stream + b"\xff" * 64, ctx) == payload
    assert ctx.get(KEY_COMPRESSED_SIZE) == len(stream)
    assert ctx.get(KEY_DECOMPRESS_COMPLETE) is True

    # A window that cuts the stream short decodes to a prefix of the real output
    # rather than raising, which is what a bounded view preview needs.
    cut = PipelineContext()
    cut.set(KEY_DECOMPRESS_PARTIAL, True)
    prefix = plugin.decompress(stream[: len(stream) // 2], cut)
    assert 0 < len(prefix) < len(payload)
    assert payload.startswith(prefix)
    assert cut.get(KEY_DECOMPRESS_COMPLETE) is False


# -- Nemesis -----------------------------------------------------------------


def _nemesis_bits(bits: str) -> bytes:
    """Pack an MSB-first bit string, zero-padding the final byte."""
    padded = bits.ljust((len(bits) + 7) // 8 * 8, "0")
    return bytes(int(padded[i : i + 8], 2) for i in range(0, len(padded), 8))


# One tile, two 1-bit codes: `0` emits a single index 0, `1` emits a run of
# eight index 1 - i.e. a whole row from one code, which is the point of the
# format. Entry byte is (run - 1) << 4 | code width.
_TABLE = bytes.fromhex("800100817101ff")
# Row 0 from the run code, then seven rows of eight single-nibble codes.
_ONE_ROW_THEN_ZEROS = _nemesis_bits("1" + "0" * 56)


def test_nemesis_decodes_runs_not_pixels() -> None:
    """A code carries a run length, so one code can fill a row.

    Emitting one nibble per code instead produces a stream of the right shape
    and the wrong length, which reads as art sliding out of alignment.
    """
    stream = bytes.fromhex("0001") + _TABLE + _ONE_ROW_THEN_ZEROS
    out, consumed, complete = nemesis.decompress(stream)
    assert out == b"\x11\x11\x11\x11" + bytes(28)
    assert complete
    assert consumed == len(stream)


def test_nemesis_xor_mode_is_against_the_previous_output_row() -> None:
    """Header bit 15 makes every row a delta on the row already written.

    Same bit stream as above: the seven rows that decode to zero must therefore
    come out identical to the first one, not blank.
    """
    stream = bytes.fromhex("8001") + _TABLE + _ONE_ROW_THEN_ZEROS
    out, _, complete = nemesis.decompress(stream)
    assert out == b"\x11" * 32
    assert complete


def test_nemesis_inline_escape_beats_a_colliding_code() -> None:
    """Six set bits are the escape even when the table has codes that prefix it.

    The table here answers a single `1` bit, so a decoder matching shortest-code
    first without testing the escape decodes this to something entirely else.
    """
    inline = "111111" + "111" + "1100"  # run of 8, palette index 0xC
    stream = bytes.fromhex("0001") + _TABLE + _nemesis_bits(inline * 8)
    out, _, complete = nemesis.decompress(stream)
    assert out == b"\xcc" * 32
    assert complete


def test_nemesis_reports_the_streams_own_length() -> None:
    """The tile count ends the decode mid-byte, and that byte is the last one.

    Counting the look-ahead fetch instead would push the next structure's start
    late, which is how a ROM's art chain is walked.
    """
    plugin = nemesis.NemesisCompression()
    stream = bytes.fromhex("0001") + _TABLE + _ONE_ROW_THEN_ZEROS
    ctx = PipelineContext()
    assert plugin.decompress(stream + b"\xff" * 64, ctx) == b"\x11\x11\x11\x11" + bytes(
        28
    )
    assert ctx.get(KEY_COMPRESSED_SIZE) == len(stream)
    assert ctx.get(KEY_DECOMPRESS_COMPLETE) is True


def test_nemesis_truncation_is_an_error_unless_partial() -> None:
    stream = bytes.fromhex("0002") + _TABLE + _ONE_ROW_THEN_ZEROS
    with pytest.raises(ValueError, match="ended after 1 of 2 tiles"):
        nemesis.decompress(stream)

    ctx = PipelineContext()
    ctx.set(KEY_DECOMPRESS_PARTIAL, True)
    prefix = nemesis.NemesisCompression().decompress(stream, ctx)
    assert prefix == b"\x11\x11\x11\x11" + bytes(28)
    assert ctx.get(KEY_DECOMPRESS_COMPLETE) is False


@pytest.mark.parametrize(
    ("stream", "match"),
    # Each rejection is what keeps a scan for these streams from claiming
    # arbitrary bytes: two zeros are the commonest word in any ROM, and a code
    # wider than its declared length or repeated is not a prefix code.
    [
        ("0000800100817101ff", "zero tiles"),
        ("0001800103817101ff", "does not fit in 1 bits"),
        ("0001800100817100ff", "appears twice"),
        ("0001800900ff", "outside 1..8"),
    ],
)
def test_nemesis_rejects_bytes_that_are_not_a_stream(stream: str, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        nemesis.decompress(bytes.fromhex(stream))


@pytest.mark.parametrize(
    "art",
    # A flat fill, a vertical gradient (XOR mode's best case), a horizontal one
    # (its worst), pixel noise, and runs of exactly eight crossing every row
    # boundary in the sheet.
    [
        bytes(32),
        bytes(b for row in range(8) for b in bytes([row * 0x11]) * 4) * 3,
        bytes([0x01, 0x23, 0x45, 0x67]) * 8 * 3,
        bytes((i * 37 + i // 7) & 0xFF for i in range(32 * 12)),
        bytes([0x00, 0x00, 0x11, 0x11] * 8 * 5),
        bytes([0xFF] * 4 + [0x00] * 4) * 4 * 6,
    ],
)
def test_nemesis_round_trips(art: bytes) -> None:
    stream = nemesis.compress(art)
    out, consumed, complete = nemesis.decompress(stream)
    assert out == art
    assert complete
    assert consumed == len(stream)


def test_nemesis_refines_only_the_seeds_worth_refining() -> None:
    """The encoder's cost is refinement and refinement is seed-sensitive, so the
    seeds are ranked by their **unrefined** size and only the best few are taken
    further. What has to hold is that the ranking is a good proxy: the budget
    gives up a fraction of a percent against refining every seed, not the several
    percent that separates a good seed from a bad one.

    Measured over a real ROM's 28 streams the difference is 22 bytes in 22,779,
    and every stream stays smaller than the one the ROM shipped — which is the
    bar, since a stream that grows will not fit the slot it came from.
    """
    art = bytes((i * 37 + (i >> 5) * 7) & 0xFF for i in range(32 * 24))

    budgeted = nemesis.compress(art)
    assert nemesis.decompress(budgeted)[0] == art

    every = len(nemesis.SEED_CAPS) * 2  # both modes compete as seeds too
    assert nemesis.REFINE_SEEDS < every
    original = nemesis.REFINE_SEEDS
    nemesis.REFINE_SEEDS = every
    try:
        exhaustive = nemesis.compress(art)
    finally:
        nemesis.REFINE_SEEDS = original
    assert len(budgeted) <= len(exhaustive) * 1.01


def test_nemesis_runs_cross_row_boundaries() -> None:
    """A run is a run of *pixels*, not of pixels within a row.

    The decoder counts nibbles into a row and flushes on the eighth, with no
    regard for how much of the symbol it is unpacking is left - so a parse that
    stops every run at the row boundary is decode-identical and needlessly
    larger. Rows here alternate four 0s then four 1s with its mirror, so the
    pixel sequence is runs of eight straddling every boundary: one symbol each
    if they may cross, two if they may not.
    """
    art = bytes([0x00, 0x00, 0x11, 0x11] + [0x11, 0x11, 0x00, 0x00]) * 4 * 4
    pixels = nemesis._nibbles(
        [int.from_bytes(art[at : at + 4], "big") for at in range(0, len(art), 4)]
    )
    assert max(run for _, run in nemesis._greedy_parse(pixels)) == nemesis.MAX_RUN
    assert nemesis.decompress(nemesis.compress(art))[0] == art


def test_nemesis_code_space_leaves_the_escape_room() -> None:
    """No code may collide with the six set bits that mean "inline".

    A code starting 111111 would be unreachable (the escape is tested first) and
    a shorter all-ones code would swallow the escape. Both live at the top of
    their length's range, so the guard is that the code space stays under
    1 - 2**-6 - checked here over a symbol set wide enough to need all 8 bits.
    """
    counts = {
        (index, run): 1 + (index * run) % 7
        for index in range(16)
        for run in range(1, 9)
    }
    lengths = nemesis._code_lengths(counts)
    assert max(lengths.values()) <= nemesis.MAX_CODE_BITS
    assert sum(2.0**-length for length in lengths.values()) <= nemesis.CODE_SPACE
    for bits, code in nemesis._assign_codes(lengths).values():
        assert code >> max(bits - nemesis.INLINE_BITS, 0) != nemesis.INLINE_PREFIX


def test_nemesis_compresses_whole_tiles_only() -> None:
    with pytest.raises(ValueError, match="whole 8x8 tiles"):
        nemesis.compress(bytes(40))


def test_nemesis_plugin_round_trips_through_the_stage() -> None:
    plugin = nemesis.NemesisCompression()
    art = bytes((i * 11) & 0xFF for i in range(32 * 6))
    ctx = PipelineContext()
    stream = plugin.compress(art, ctx)
    assert plugin.decompress(stream + b"\xff" * 32, ctx) == art
    assert ctx.get(KEY_COMPRESSED_SIZE) == len(stream)


# -- Enigma ------------------------------------------------------------------


def _cells(words: list[int]) -> bytes:
    return b"".join(word.to_bytes(2, "big") for word in words)


def test_enigma_decodes_the_published_worked_example() -> None:
    """The format's own documented example, byte for byte.

    Worth having as one assertion because of how much of the format it pins at
    once: both six-bit ops, a decrementing inline run, the terminator, and - the
    part no other test here reaches - a flag field that names the *palette* bits
    rather than the flips, combined into the cell by OR.
    """
    stream = bytes.fromhex("070C00000010053D118FE0")
    out, _, complete = enigma.decompress(stream)
    assert out == _cells(
        [0x0000, 0x0001]
        + [0x0010] * 4
        + [0x4018, 0x4017, 0x4016, 0x4015, 0x4014, 0x4013, 0x4012, 0x4011, 0x4010]
    )
    assert complete


def test_enigma_flag_field_spells_its_bits_most_significant_first() -> None:
    """A mask naming two non-adjacent flags stores the higher one first.

    The one ordering that cannot be guessed from a round trip: read the other way
    round, this stream decodes to a horizontally flipped tile of the wrong
    priority instead of to anything that looks like a failure. Mask 0x11 is
    priority plus H flip, and the field here is 1 then 0.
    """
    stream = bytes.fromhex("011100000000E17F8000")
    out, _, complete = enigma.decompress(stream)
    assert out == _cells([0x8001])  # priority set, H flip clear
    assert complete


def test_enigma_incrementing_word_is_state_across_tokens() -> None:
    """It carries between its own tokens, and an intervening op does not disturb it.

    A decoder holding it per-token instead restarts at the header value, which
    decodes a screen whose tile bank walk silently resets partway down.
    """
    stream = bytes.fromhex("08000100FFFF05007F80")
    out, _, complete = enigma.decompress(stream)
    assert out == _cells([0x0100, 0x0101, 0xFFFF, 0x0102, 0x0103])
    assert complete


def test_enigma_literal_batches_stop_short_of_the_terminator() -> None:
    """Fifteen cells to a literal token, not sixteen.

    A full count nibble on the literal op *is* the end marker, so a compressor
    that batches sixteen writes a stream that stops at the first such batch. The
    cells here are all distinct, so nothing but literal tokens can encode them.
    """
    cells = _cells([(i * 37 + 1) & 0x7FF for i in range(200)])
    stream = enigma.compress(cells)
    out, _, complete = enigma.decompress(stream)
    assert out == cells
    assert complete


@pytest.mark.parametrize(
    "words",
    # A flat screen, a bank walked in order, both flips, the palette lines and
    # priority the flag mask has to widen for, a descending run, and a run that
    # walks off the top of the index field so the increment carries into a flag.
    [
        [0x0010] * 200,
        list(range(0x100, 0x100 + 300)),
        [0x0800 | i for i in range(40)] + [0x1000 | i for i in range(40)],
        [(i % 4) << 13 | (i * 7 & 0x7FF) for i in range(400)],
        [0x8000 | (i & 0x3F) for i in range(120)],
        [(0x400 - i) & 0xFFFF for i in range(300)],
        [(0x7FD + i) & 0xFFFF for i in range(20)],
    ],
)
def test_enigma_round_trips(words: list[int]) -> None:
    cells = _cells(words)
    stream = enigma.compress(cells)
    out, _, complete = enigma.decompress(stream)
    assert out == cells
    assert complete


def test_enigma_walks_a_bank_of_flagged_cells_with_the_free_token() -> None:
    """An ascending run of flipped tiles is still an ascending run.

    The incrementing word is matched against the whole cell, so a screen drawn
    from a mirrored or high-priority bank can use the operand-free token only if
    a candidate start carries those flags too. Offering only the index would cost
    this screen every one of them - a stored cell per run instead of six bits, so
    the stream comes out several times larger while still round-tripping, which
    is why size is the assertion.
    """
    cells = _cells([0x0800 | i for i in range(128)])
    stream = enigma.compress(cells)
    assert len(stream) <= 16  # header plus a handful of tokens
    assert enigma.decompress(stream)[0] == cells


@pytest.mark.parametrize(
    ("stream", "match"),
    # The header is the only thing standing between a scan for these streams and
    # any six bytes in a ROM, so both of its fields are bounded.
    [
        ("000000000000FE", "outside 1..11"),
        ("0C0000000000FE", "outside 1..11"),
        ("072000000000FE", "above the cell's top five"),
        ("0700", "shorter than the 6-byte header"),
    ],
)
def test_enigma_rejects_bytes_that_are_not_a_stream(stream: str, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        enigma.decompress(bytes.fromhex(stream))


def test_enigma_truncation_is_an_error_unless_partial() -> None:
    cells = _cells(list(range(0x100, 0x100 + 300)))
    stream = enigma.compress(cells)
    cut = stream[: len(stream) // 2]
    with pytest.raises(ValueError, match="source ended"):
        enigma.decompress(cut)

    ctx = PipelineContext()
    ctx.set(KEY_DECOMPRESS_PARTIAL, True)
    prefix = enigma.EnigmaCompression().decompress(cut, ctx)
    assert 0 < len(prefix) < len(cells)
    assert cells.startswith(prefix)
    assert ctx.get(KEY_DECOMPRESS_COMPLETE) is False


def test_enigma_plugin_reports_the_streams_own_length() -> None:
    """Rounded up to a word, which is where the next structure starts.

    A ROM chains a screen's map and its art end to end, so the map's extent is
    what says where the art begins - and the 68000 unpacker reads the token
    stream a word at a time, so a stream ending mid-word still owns the rest.
    """
    plugin = enigma.EnigmaCompression()
    cells = _cells([0x0010] * 64 + list(range(50)))
    ctx = PipelineContext()
    stream = plugin.compress(cells, ctx)
    assert plugin.decompress(stream + b"\xff" * 32, ctx) == cells
    assert ctx.get(KEY_COMPRESSED_SIZE) == len(stream)
    assert len(stream) % 2 == 0


def test_enigma_compresses_whole_cells_only() -> None:
    with pytest.raises(ValueError, match="whole 2-byte cells"):
        enigma.compress(bytes(9))


# -- Kosinski ----------------------------------------------------------------


def test_kosinski_descriptor_is_little_endian_lsb_first() -> None:
    """Three literals and the end marker, in one partly-used descriptor word.

    Bits go into the word from its low end in the order they are written, and the
    word itself is little-endian. Read big-endian or MSB-first and the very first
    bit selects a match instead of a literal, so the stream decodes to garbage
    rather than failing.
    """
    out, consumed, complete = kosinski.decompress(bytes.fromhex("170041424300F000"))
    assert out == b"ABC"
    assert complete
    assert consumed == 8


def test_kosinski_fetches_its_next_descriptor_before_the_pending_payload() -> None:
    """The eager refill, which is the format's one genuinely silent trap.

    Sixteen literals spend a whole descriptor word, and the word for what follows
    sits *before* the sixteenth literal's byte - because the decoder fetches it
    the instant the last bit is spent, which is one op before that byte is read.
    A decoder refilling lazily reads the same bits in the same order and takes
    the ``70`` here as its descriptor.
    """
    stream = kosinski.compress(b"abcdefghijklmnop")
    assert stream == bytes.fromhex("ffff6162636465666768696a6b6c6d6e6f02007000f000")
    assert kosinski.decompress(stream)[0] == b"abcdefghijklmnop"


def test_kosinski_match_may_read_output_it_has_not_written_yet() -> None:
    """One literal plus a five-byte match one back is six bytes of fill.

    The copy is a byte at a time, so the source repeats with period ``distance``.
    A decoder that snapshots the window first produces five zero bytes here.
    """
    out, _, complete = kosinski.decompress(bytes.fromhex("5900AAFF00F000"))
    assert out == b"\xaa" * 6
    assert complete


def test_kosinski_skips_a_module_boundary_marker() -> None:
    """Count 1 on the three-byte form emits nothing and is not the end.

    It separates the chunks of the moduled variant. Treating it as a terminator
    truncates such a stream at its first boundary; treating it as a match copies
    garbage.
    """
    out, _, complete = kosinski.decompress(bytes.fromhex("16000000015A00F000"))
    assert out == b"Z"
    assert complete


@pytest.mark.parametrize(
    "plain",
    # Nothing, one byte, incompressible bytes, a long fill, a short period, and a
    # match at each form's boundary: inline reaches 5 bytes within 256, the short
    # form 9 within 8 KiB, the long form beyond that.
    [
        b"",
        b"A",
        bytes(range(64)),
        b"\xab" * 5000,
        b"celPix!" * 700,
        b"abcde" + b"x" * 250 + b"abcde",
        b"abcdefghi" + b"x" * 8000 + b"abcdefghi",
        bytes((i * 97 + i // 5) & 0xFF for i in range(3000)),
    ],
)
def test_kosinski_round_trips(plain: bytes) -> None:
    stream = kosinski.compress(plain)
    out, consumed, complete = kosinski.decompress(stream)
    assert out == plain
    assert complete
    assert consumed == len(stream)


def test_kosinski_round_trips_across_every_descriptor_boundary() -> None:
    """Lengths 0..40 of incompressible data, which is one descriptor bit each.

    So the terminator lands at every offset within a descriptor word, including
    the one that fills it exactly - the case that needs a dummy word emitted
    after it, and the only case where omitting one still produces a stream that
    looks well formed.
    """
    rng = random.Random(4)
    for length in range(41):
        plain = bytes(rng.randrange(256) for _ in range(length))
        stream = kosinski.compress(plain)
        out, consumed, complete = kosinski.decompress(stream)
        assert out == plain, length
        assert complete and consumed == len(stream), length


def test_kosinski_truncation_is_an_error_unless_partial() -> None:
    plain = bytes((i * 97) & 0xFF for i in range(2000))
    stream = kosinski.compress(plain)
    cut = stream[: len(stream) // 2]
    with pytest.raises(ValueError, match="source ended"):
        kosinski.decompress(cut)

    ctx = PipelineContext()
    ctx.set(KEY_DECOMPRESS_PARTIAL, True)
    prefix = kosinski.KosinskiCompression().decompress(cut, ctx)
    assert 0 < len(prefix) < len(plain)
    assert plain.startswith(prefix)
    assert ctx.get(KEY_DECOMPRESS_COMPLETE) is False


def test_kosinski_rejects_a_match_reaching_before_the_output() -> None:
    """An inline match at the very start has nothing behind it to copy."""
    with pytest.raises(ValueError, match="reaches"):
        kosinski.decompress(bytes.fromhex("0C00FF00F000"))


def test_kosinski_plugin_round_trips_through_the_stage() -> None:
    plugin = kosinski.KosinskiCompression()
    plain = bytes((i * 13) & 0xFF for i in range(600)) + b"\x00" * 400
    ctx = PipelineContext()
    stream = plugin.compress(plain, ctx)
    assert plugin.decompress(stream + b"\xff" * 32, ctx) == plain
    assert ctx.get(KEY_COMPRESSED_SIZE) == len(stream)
