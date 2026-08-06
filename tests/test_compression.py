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
    gba_lz77,
    konami_rle,
    lz16,
    lz_command,
    lzss_ring,
    packbits,
    prs,
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
