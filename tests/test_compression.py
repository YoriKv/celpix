"""The LZ compression codecs: known-vector decodes, round trips, edge cases.

The decode vectors are hand-assembled from the format specification
(``docs/graphics-formats-reference/implementation-guide.md``), so they guard
the bit/byte math independently of our own compressor.
"""

from __future__ import annotations

import random

import pytest

from celpix.core.capabilities import ContentKind
from celpix.core.context import (
    KEY_COMPRESSED_SIZE,
    KEY_DECOMPRESS_COMPLETE,
    KEY_DECOMPRESS_PARTIAL,
    KEY_INPUTS,
    KEY_SURROUND,
    KEY_SURROUND_START,
    PipelineContext,
)
from celpix.core.errors import Stage
from celpix.pipeline import pipeline
from celpix.plugins.base import STAGE_DEFAULT_PRESET
from celpix.plugins.builtins import (
    aplib,
    bluesky_lz,
    enigma,
    gba_lz77,
    koei_lz,
    konami_rle,
    kosinski,
    lz4w,
    lz16,
    lz_command,
    lzss_ring,
    namco_lz,
    nemesis,
    packbits,
    phantasy_star_rle,
    prs,
    rnc,
    slz,
    snes_rle,
    sonic2_tiles,
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
from celpix.plugins.builtins.phantasy_star_rle import PhantasyStarRleCompression
from celpix.plugins.builtins.prs import PrsCompression
from celpix.plugins.builtins.slz import Slz16Compression, Slz24Compression
from celpix.plugins.builtins.snes_rle import Rle1Compression, Rle2Compression
from celpix.plugins.registry import default_registry
from celpix.project.workspace import Workspace, pixel_config_for, tilemap_config_for

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


def test_konami_plugin_is_strict_unless_told_the_buffer_is_bounded() -> None:
    # The scan and a slice read decode strictly: a buffer that ends before the
    # terminator is a failure there, and only the preview (allow-partial) gets
    # the prefix back. Without this the scan hit on almost any byte.
    packed = konami_rle.compress(bytes(range(200)))
    cut = packed[:-30]
    with pytest.raises(ValueError, match="terminator"):
        KonamiNesRle().decompress(cut, PipelineContext())
    ctx = PipelineContext()
    ctx.set(KEY_DECOMPRESS_PARTIAL, True)
    prefix = KonamiNesRle().decompress(cut, ctx)
    assert 0 < len(prefix) < 200 and bytes(range(len(prefix))) == prefix
    assert ctx.get(KEY_DECOMPRESS_COMPLETE) is False


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


# -- RLE1 / RLE2 (SNES tilemap RLE) ------------------------------------------


def test_rle1_decode_known_vector() -> None:
    # Hand-assembled from the format description: a 3-byte literal (0x02 = L+1),
    # a 5-run (0x84 = C set, L+1 = 5), a 1-byte literal, then the $FF $FF end.
    # Guards the L+1 arithmetic and pins that $FF is only an end marker at a
    # *header* position — the 5-run's value byte here is $FF and must not end it.
    stream = bytes.fromhex("02 41 42 43 84 ff 00 7a ff ff")
    out, consumed, complete = snes_rle.decompress(stream, terminated=True)
    assert out == b"ABC" + b"\xff" * 5 + b"\x7a"
    assert consumed == len(stream)
    assert complete is True
    # The same bytes without the terminator are RLE2, and one byte shorter.
    out2, consumed2, complete2 = snes_rle.decompress(stream[:-2], terminated=False)
    assert out2 == out
    assert consumed2 == len(stream) - 2
    assert complete2 is False


def test_rle_round_trip() -> None:
    rng = random.Random(11)
    payloads = [
        b"",
        b"\x42",
        b"\x42\x42",  # a bare pair: a run packet from 2 up, unlike PackBits
        b"\x55" * 300,  # long run: 128 + 128 + a 44 tail
        b"\xab" * 128,  # exactly one full run packet
        b"\xab" * 129,  # a full packet plus a lone byte (a literal)
        bytes(range(128)),  # exactly one full literal packet
        bytes(range(129)),
        bytes(range(256)),  # all distinct: literal-only
        b"\xff" * 127,  # the $FF run bracketing the terminator collision
        b"\xff" * 128,
        b"\xff" * 129,
        b"\x01\x02" + b"\xff" * 128 + b"\x03",  # the same collision mid-stream
        b"AB" * 5 + b"C" * 10 + b"D" + b"EFG" + b"H" * 200,
        bytes(rng.randrange(256) for _ in range(2000)),
        bytes(rng.choice(b"\x00\xff") for _ in range(1500)),  # tilemap-shaped
    ]
    for terminated in (True, False):
        for data in payloads:
            packed = snes_rle.compress(data, terminated=terminated)
            out, consumed, complete = snes_rle.decompress(packed, terminated=terminated)
            assert out == data
            assert consumed == len(packed)
            assert complete is terminated
            # Worst case is a 1-byte literal alternating with a 2-run — 4 bytes
            # of packet per 3 of output — since taking a run at 2 can split a
            # literal for no gain. That is the price of the original packer's
            # threshold; incompressible data in practice sits near 1.01x.
            assert len(packed) <= (4 * len(data) + 2) // 3 + 2


def test_rle1_never_writes_its_own_terminator() -> None:
    # 128 copies of $FF would encode as the run header $FF followed by the value
    # $FF — which *is* the end marker, truncating the stream where it stands. The
    # encoder caps a run of that one byte at 127 under the terminated framing and
    # lets the rest ride along, so no header/value pair spells $FF $FF.
    packed = snes_rle.compress(b"\xff" * 128, terminated=True)
    assert packed[:2] == b"\xfe\xff"  # a 127-run, not a 128-run
    # $FF $FF ends the stream only where a *header* is read, so walk the headers
    # and check none but the last starts one. (The pair does occur inside this
    # stream, at the literal payload preceding the terminator, and is inert.)
    headers, i = [], 0
    while i < len(packed) - 2:
        headers.append(i)
        i += 2 if packed[i] & 0x80 else packed[i] + 2
    assert i == len(packed) - 2  # the walk lands exactly on the terminator
    assert all(packed[h : h + 2] != b"\xff\xff" for h in headers)
    # RLE2 has no terminator to collide with, so its runs fill the packet.
    assert snes_rle.compress(b"\xff" * 128, terminated=False) == b"\xff\xff"


def test_rle1_truncated_stream_needs_the_partial_flag() -> None:
    # No terminator inside the buffer is how RLE1 says "this is not an RLE1
    # stream", which is what makes it worth scanning for; a bounded view window
    # cuts real streams short the same way, so `partial` downgrades it to the
    # prefix decoded so far.
    data = bytes(range(200))
    packed = snes_rle.compress(data, terminated=True)
    cut = packed[: len(packed) - 30]
    with pytest.raises(ValueError):
        snes_rle.decompress(cut, terminated=True)
    out, consumed, complete = snes_rle.decompress(cut, terminated=True, partial=True)
    assert complete is False
    assert consumed <= len(cut)
    assert 0 < len(out) < len(data)
    assert data[: len(out)] == out


def test_rle1_plugin_reports_the_structure_extent() -> None:
    # The terminator bounds the read, so trailing bytes past it — themselves
    # valid headers — are neither decoded nor counted, and the size reported is
    # the slot a save-back must fit.
    data = b"\x00" * 50 + bytes(range(30)) + b"\xff" * 40
    packed = Rle1Compression().compress(data, PipelineContext())
    ctx = PipelineContext()
    assert Rle1Compression().decompress(packed + b"\x5a" * 6, ctx) == data
    assert ctx.get(KEY_COMPRESSED_SIZE) == len(packed)
    assert ctx.get(KEY_DECOMPRESS_COMPLETE) is True


def test_rle2_plugin_records_size_but_never_complete() -> None:
    # RLE2 has no end marker: reaching the end of the buffer is "the buffer ran
    # out", not "the structure ended". Reporting complete would let a slice with
    # no length backfill its extent as the whole rest of the file — the same rule
    # PackBits follows, and why both declare self_delimiting False.
    data = b"\x00" * 50 + bytes(range(30)) + b"\xff" * 40
    packed = Rle2Compression().compress(data, PipelineContext())
    ctx = PipelineContext()
    assert Rle2Compression().decompress(packed, ctx) == data
    assert ctx.get(KEY_COMPRESSED_SIZE) == len(packed)
    assert ctx.get(KEY_DECOMPRESS_COMPLETE) is False
    assert Rle2Compression.info.self_delimiting is False


def test_rle2_stops_at_the_output_cap() -> None:
    # With no terminator, an unbounded read over a whole file would expand at up
    # to 64x. The decode stops at one SNES bank of output, on a packet boundary.
    bomb = b"\xff\x00" * 0x10000  # 128-runs, 2 bytes each
    out, consumed, complete = snes_rle.decompress(bomb, terminated=False)
    assert len(out) == 0x10000
    assert consumed == 2 * (0x10000 // 128) < len(bomb)
    assert complete is False


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
    # The low nibble is reserved as zero. Masking it off let the scan accept
    # thousands of "structures" per cartridge that no encoder ever wrote.
    with pytest.raises(ValueError, match="reserved"):
        gba_lz77.decompress(bytes.fromhex("12040000" + "00" + "41424344"))


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


# -- RNC ---------------------------------------------------------------------


def _rnc_stream(method: int, body: bytes, plain: bytes, chunks: int = 1) -> bytes:
    """A header around ``body``, with the sizes and CRCs the decoder verifies."""
    return (
        b"RNC"
        + bytes((method,))
        + len(plain).to_bytes(4, "big")
        + len(body).to_bytes(4, "big")
        + rnc.crc16(plain).to_bytes(2, "big")
        + rnc.crc16(body).to_bytes(2, "big")
        + bytes((0, chunks))
        + body
    )


def _lsb_words(fields: list[tuple[int, int]]) -> bytes:
    """``(value, bits)`` fields packed low bit first into 16-bit LE words."""
    acc = shift = 0
    for value, bits in fields:
        acc |= value << shift
        shift += bits
    return acc.to_bytes(-(-shift // 16) * 2, "little")


def test_rnc_crc16_is_the_reflected_a001_polynomial() -> None:
    assert rnc.crc16(b"123456789") == 0xBB3D


def test_rnc1_decodes_length_classes_and_ends_a_chunk_on_a_literal_run() -> None:
    """Two literals, a 6-byte match 2 back, and an empty final run.

    Guards three silent traps at once: a Huffman symbol is a *bit-length class*
    followed by extra bits (the run of 2 is symbol 2 plus one bit, the length 6
    is symbol 3 plus two); a subchunk count of 2 is two runs but one match; and
    the literal bytes sit after the whole 16-bit word their run code is in, not
    inside the bit stream.
    """
    bits = _lsb_words(
        [
            (0, 1), (0, 1),                      # not locked, not keyed
            (3, 5), (1, 4), (0, 4), (1, 4),      # runs: symbols 0 and 2, 1 bit
            (2, 5), (0, 4), (1, 4),              # distances: symbol 1 only
            (4, 5), (0, 4), (0, 4), (0, 4), (1, 4),  # lengths: symbol 3 only
            (2, 16),                             # two subchunks
            (1, 1), (0, 1),                      # run: symbol 2, extra 0 -> 2
            (0, 1),                              # distance: 1 + 1
            (0, 1), (0, 2),                      # length: symbol 3, extra 0 -> 4 + 2
            (0, 1),                              # final run: 0
        ]
    )  # fmt: skip
    plain = b"ABABABAB"
    stream = _rnc_stream(1, bits + b"AB", plain)
    assert rnc.decompress(stream, method=1) == (plain, len(stream), True)


def test_rnc2_reads_raw_bytes_between_bit_bytes() -> None:
    """Literal A, literal B, a 4-byte match 2 back, then the end of the chunk.

    The bit bytes are 0x08 and 0x78; the two literals follow the first because
    it was fetched before they were read, and the match's distance byte and the
    marker's zero byte follow the second for the same reason.
    """
    body = bytes.fromhex("084142780100")
    stream = _rnc_stream(2, body, b"ABABAB")
    assert rnc.decompress(stream, method=2) == (b"ABABAB", len(stream), True)


@pytest.mark.parametrize("method", [rnc.METHOD_1, rnc.METHOD_2])
@pytest.mark.parametrize(
    "plain",
    # Nothing, one byte, incompressible bytes, a long fill, a repeat 2000 back,
    # and low-entropy data past the 0x3000-byte chunk the packer covers.
    [
        b"",
        b"A",
        bytes(range(256)),
        b"\xab" * 5000,
        bytes((i * 7919) % 251 for i in range(2000)) * 2,
        bytes((i * 97 + i // 7) & 0xFF for i in range(0x3100)),
    ],
)
def test_rnc_round_trips(method: int, plain: bytes) -> None:
    stream = rnc.compress(plain, method=method)
    ctx = PipelineContext()
    plugin = rnc.Rnc1Compression() if method == 1 else rnc.Rnc2Compression()
    assert plugin.decompress(stream + b"RNC junk", ctx) == plain
    assert ctx.get(KEY_COMPRESSED_SIZE) == len(stream)
    assert ctx.get(KEY_DECOMPRESS_COMPLETE) is True


def test_rnc_rejects_corrupt_streams_and_forgives_only_a_short_buffer() -> None:
    rng = random.Random(5)
    plain = bytes(rng.randrange(256) for _ in range(1500)) * 2
    stream = rnc.compress(plain, method=1)

    flipped = bytearray(stream)
    flipped[40] ^= 0x10
    with pytest.raises(ValueError, match="packed data fails its CRC"):
        rnc.decompress(bytes(flipped), method=1)
    wrong_sum = bytearray(stream)
    wrong_sum[12] ^= 1
    with pytest.raises(ValueError, match="unpacked data fails its CRC"):
        rnc.decompress(bytes(wrong_sum), method=1)
    with pytest.raises(ValueError, match="method byte"):
        rnc.decompress(stream, method=2)

    cut = stream[: len(stream) // 2]
    with pytest.raises(ValueError, match="source ends"):
        rnc.decompress(cut, method=1)
    prefix, _, complete = rnc.decompress(cut, method=1, partial=True)
    assert not complete
    assert 0 < len(prefix) < len(plain)
    assert plain.startswith(prefix)


# -- Namco LZSS and the Strike variant ---------------------------------------


def test_namco_lz_prefix_is_big_endian_and_strike_masks_its_ring() -> None:
    """The ring LZSS vector behind a 16-bit big-endian size.

    The reference names ring position 0xFEE, the Namco cursor origin. The Strike
    ring is 2048 bytes, so the same field reads as 0x7EE -- its own origin --
    which is what masking the top bit has to give.
    """
    stream = bytes([0x00, 0x07, 0x03, 0x41, 0x42, 0xEE, 0xF2])
    assert namco_lz.decompress(stream) == (b"ABABABA", 7, True)
    assert namco_lz.decompress(stream, ring_size=namco_lz.STRIKE_RING)[0] == b"ABABABA"


def test_namco_lz_streams_read_the_same_under_a_1024_byte_ring() -> None:
    """The encoder's hedge on the doubted window: nothing reaches past 1024 back.

    A block repeated 2000 bytes on would be a cheap match under the 4096-byte
    ring. Written that way, a 1024-byte-ring reading copies the wrong bytes.
    """
    block = bytes((i * 7919) % 251 for i in range(2000))
    stream = namco_lz.compress(block * 2)
    assert namco_lz.decompress(stream)[0] == block * 2
    narrow = lzss_ring.decompress(
        stream, size_bytes=2, byteorder="big", ring_size=0x400
    )
    assert narrow[0] == block * 2


@pytest.mark.parametrize("ring", [namco_lz.NAMCO_RING, namco_lz.STRIKE_RING])
def test_namco_lz_round_trips_and_needs_a_whole_stream(ring: int) -> None:
    plain = bytes((i * 97 + i // 5) & 0xFF for i in range(3000)) + bytes(3000)
    stream = namco_lz.compress(plain, ring_size=ring, window=min(ring, 0x400))
    assert namco_lz.decompress(stream + b"junk", ring_size=ring) == (
        plain,
        len(stream),
        True,
    )
    with pytest.raises(ValueError, match="source ended"):
        namco_lz.decompress(stream[:-3], ring_size=ring)
    with pytest.raises(ValueError, match="empty payload"):
        namco_lz.compress(b"", ring_size=ring)


# -- Koei's SNES LZ ----------------------------------------------------------


def test_koei_lz_reads_its_code_words_from_among_the_literals() -> None:
    """The interleaving, on a vector assembled from the format description.

    ``80 60`` is the priming word, then the flag byte ``11000000``: two
    literals, a match, and the flag bit that introduces the end marker. The
    match's nine bits of code exhaust the priming word, so the *second* word is
    fetched mid-match and lands at offset 5 -- after the two literal bytes it
    was written before. A reader that took the words as a block up front, or
    left them to the end, would see ``00 FE`` as literal data.
    """
    stream = bytes([0x80, 0x60, 0xC0, 0x41, 0x42, 0x00, 0xFE])
    # 011 length (4 bytes) + 000001 distance (one back of two) + the marker.
    assert koei_lz.decompress(stream + b"junk") == (b"ABABAB", len(stream), True)
    assert koei_lz.compress(b"ABABAB") == stream


def test_koei_lz_round_trips_every_code_bucket_and_stops_on_its_marker() -> None:
    """Both code tables end to end, plus the extents the marker has to bound.

    The payload reaches the far distance codes (a 3 KiB block repeated), the
    saturated length code (runs past 255 bytes) and the near ones in between.
    """
    block = bytes((i * 7919) % 251 for i in range(3000))
    plain = block + b"\x00" * 600 + block + b"the quick brown fox " * 8
    stream = koei_lz.compress(plain)
    assert koei_lz.decompress(stream + b"\xaa" * 8) == (plain, len(stream), True)
    assert koei_lz.compress(b"") != b""  # an empty payload is still a stream
    assert koei_lz.decompress(koei_lz.compress(b""))[0] == b""


def test_koei_lz_needs_the_whole_stream_unless_asked_for_a_prefix() -> None:
    plain = bytes((i * 97 + i // 5) & 0xFF for i in range(2000)) + bytes(1000)
    stream = koei_lz.compress(plain)
    with pytest.raises(ValueError, match="source ended"):
        koei_lz.decompress(stream[: len(stream) // 2])
    prefix, _, complete = koei_lz.decompress(stream[: len(stream) // 2], partial=True)
    assert not complete
    assert 0 < len(prefix) < len(plain)
    assert plain.startswith(prefix)
    with pytest.raises(ValueError, match="16-bit code word"):
        koei_lz.decompress(b"\x00")


# -- BlueSky LZ + RLE --------------------------------------------------------


def test_bluesky_lz_decodes_both_two_byte_ops_and_the_whole_ring_distance() -> None:
    """Literals A B, a 4-byte match 2 back, a 3-byte fill of Z, and a 3-byte
    match at distance field 0 — which is the full 2048 bytes back, into the
    ring's zero fill. The size field says 12 by storing 11.
    """
    stream = bytes.fromhex("000B384142A100005A8000")
    assert bluesky_lz.decompress(stream) == (b"ABABABZZZ\0\0\0", len(stream), True)
    # One less in the size field cuts the final match short rather than failing.
    shorter = bytes.fromhex("000A") + stream[2:]
    assert bluesky_lz.decompress(shorter)[0] == b"ABABABZZZ\0\0"


@pytest.mark.parametrize(
    "plain",
    [
        b"A",
        bytes(range(256)),
        b"\x00" * 5000,  # fills chained end to end
        bytes((i * 7919) % 251 for i in range(1500)) * 2,  # within the 2 KiB ring
        b"ab" + bytes(300) + b"ab" * 50,
    ],
)
def test_bluesky_lz_round_trips(plain: bytes) -> None:
    stream = bluesky_lz.compress(plain)
    assert bluesky_lz.decompress(stream + b"junk") == (plain, len(stream), True)


def test_bluesky_lz_truncation_is_an_error_unless_partial() -> None:
    rng = random.Random(6)
    plain = bytes(rng.randrange(256) for _ in range(2000))
    cut = bluesky_lz.compress(plain)[:1000]
    with pytest.raises(ValueError, match="source ended"):
        bluesky_lz.decompress(cut)
    prefix, _, complete = bluesky_lz.decompress(cut, partial=True)
    assert not complete
    assert plain.startswith(prefix)
    with pytest.raises(ValueError, match="empty payload"):
        bluesky_lz.compress(b"")


# -- Master System: "Phantasy Star" RLE and the "Sonic 2" tile codec -----------

# Four parts of four bytes: a run and a literal, a literal, a run of zeros, a
# literal and a run. Verified against the game's Z80 tile loader, which writes
# part k's byte i at k + 4i.
_PS_STREAM = bytes.fromhex("03AA810100 841011121300 040000 82F0F102FF00")
_PS_TILES = bytes.fromhex("AA1000F0 AA1100F1 AA1200FF 011300FF")


def test_phantasy_star_rle_weaves_its_parts_and_reads_80_as_256_literals() -> None:
    assert phantasy_star_rle.decompress(_PS_STREAM + b"\x7f") == (
        _PS_TILES,
        len(_PS_STREAM),
        True,
    )
    # The tile loader counts a literal down with djnz, so a zero count is 256.
    literal = bytes(range(256))
    assert phantasy_star_rle.decompress(b"\x80" + literal + b"\x00", parts=1) == (
        literal,
        258,
        True,
    )


@pytest.mark.parametrize("parts", [2, 4])
def test_phantasy_star_rle_round_trips_and_never_writes_80(parts: int) -> None:
    rng = random.Random(parts)
    data = (
        bytes(64)
        + b"\x5a" * 1000  # runs past the 127-byte packet
        + bytes(rng.randrange(256) for _ in range(1016))  # literals past it too
        + bytes(rng.choice((0, 0, 1, 0x80)) for _ in range(512))
    )
    packed = phantasy_star_rle.compress(data, parts=parts)
    assert phantasy_star_rle.decompress(packed, parts=parts) == (
        data,
        len(packed),
        True,
    )
    i = 0
    for _ in range(parts):  # walk the packets: $80 means 256 or 65,536 by loader
        while packed[i]:
            assert packed[i] != 0x80
            i += 1 + (packed[i] & 0x7F if packed[i] & 0x80 else 1)
        i += 1
    assert len(packed) < len(data) * 3 // 4


def test_phantasy_star_rle_rejects_a_cut_stream_and_a_wrong_part_count() -> None:
    with pytest.raises(ValueError, match="inside part 3 of 4"):
        phantasy_star_rle.decompress(_PS_STREAM[:-2])
    prefix, _, complete = phantasy_star_rle.decompress(_PS_STREAM[:-2], partial=True)
    assert not complete and prefix[:3] == _PS_TILES[:3]
    # A two-part tilemap read as four parts takes two more out of what follows.
    tilemap = phantasy_star_rle.compress(bytes(range(40)), parts=2)
    with pytest.raises(ValueError, match="part count is likely wrong"):
        phantasy_star_rle.decompress(tilemap + b"\x81\x00\x00\x82\x00\x00\x00", parts=4)
    with pytest.raises(ValueError, match="whole number"):
        phantasy_star_rle.compress(bytes(5), parts=2)


def test_phantasy_star_rle_interleave_is_an_input_defaulting_to_tiles(tmp_path) -> None:
    """Unbound, the host hands the codec its default of 4; bound to 2, the same
    stream reads as a map's two parts, woven two apart — on the tilemap pathway
    too, which resolves a compressed map's scheme inputs as the pixel side does."""
    rom = tmp_path / "rom.sms"
    rom.write_bytes(bytes(0x40) + _PS_STREAM + bytes(0x40))
    reg = default_registry()
    ws = Workspace()
    parent = ws.open_file(str(rom))
    codec = PhantasyStarRleCompression.info.id
    sl = ws.add_slice(parent.path, "art", 0x40, len(_PS_STREAM), codec)
    preset = STAGE_DEFAULT_PRESET[Stage.INTERPRET_PIXEL]

    cfg = pixel_config_for(sl, preset, reg, ws)
    assert cfg.compression_id == codec and not cfg.input_problems
    assert pipeline.load_pixel_data(cfg, reg).data == _PS_TILES

    woven = bytes.fromhex("AA10AA11AA120113")
    sl.inputs = {codec: {phantasy_star_rle.INPUT_PARTS: 2}}
    assert (
        pipeline.load_pixel_data(pixel_config_for(sl, preset, reg, ws), reg).data
        == woven
    )
    sl.content_kind = ContentKind.TILEMAP
    cfg = tilemap_config_for(sl, "preset.tilemap.sms-bg", reg, ws)
    assert cfg.compression_id == codec
    assert pipeline.load_tilemap_data(cfg, reg).data == woven


def _sonic2_vector() -> tuple[bytes, bytes]:
    """One tile of each type, in one bitstream byte read low pair first.

    The XOR tile's mask sets bits 0, 2, 4 and 17 (byte 2, bit 1). Its pass is a
    running XOR, so byte 4 becomes 0F ^ FF — the byte 2 already changed — not
    0F ^ F0; and byte 17's 01 carries down every odd byte after it. Verified
    against the game's Z80 loader.
    """
    stream = (
        bytes.fromhex("0100 0400 3400")  # header, 4 tiles, bitstream at 52
        + bytes(range(32))  # %01 raw
        + bytes.fromhex("01000080 1122")  # %10: bytes 0 and 31
        + bytes.fromhex("15000200 0FF00F01")  # %11
        + bytes([0b11_10_01_00])
    )
    masked = bytearray(32)
    masked[0], masked[31] = 0x11, 0x22
    xored = bytearray(32)
    xored[0], xored[2] = 0x0F, 0xFF
    xored[4:16:2] = b"\xf0" * 6
    xored[17:32:2] = b"\x01" * 8
    return stream, bytes(32) + bytes(range(32)) + masked + xored


def test_sonic2_tiles_decode_all_four_types() -> None:
    stream, tiles = _sonic2_vector()
    assert sonic2_tiles.decompress(stream + b"\xee" * 8) == (tiles, len(stream), True)


@pytest.mark.parametrize(
    "tiles",
    [
        bytes(64),
        bytes((i * 7919) % 251 for i in range(32 * 20)),  # raw
        bytes(8) + b"\x33" + bytes(23),  # masked
        b"\x0f\xf0" * 8 + bytes(16),  # a chain the XOR pass zeroes
        bytes(random.Random(9).choice((0, 0, 0, 0x81)) for _ in range(32 * 64)),
    ],
)
def test_sonic2_tiles_round_trip(tiles: bytes) -> None:
    packed = sonic2_tiles.compress(tiles)
    assert sonic2_tiles.decompress(packed) == (tiles, len(packed), True)


def test_sonic2_tiles_reject_truncated_and_corrupt_streams() -> None:
    stream, tiles = _sonic2_vector()
    with pytest.raises(ValueError, match="bitstream ends"):
        sonic2_tiles.decompress(stream[:-1])
    cut = stream[:45] + stream[52:]  # the XOR tile cut one byte into its mask
    cut = cut[:4] + (45).to_bytes(2, "little") + cut[6:]
    with pytest.raises(ValueError, match="after 3 of 4 tiles"):
        sonic2_tiles.decompress(cut)
    prefix, _, complete = sonic2_tiles.decompress(cut, partial=True)
    assert not complete and prefix == tiles[:96]
    with pytest.raises(ValueError, match="not 01 00"):
        sonic2_tiles.decompress(b"\x00" + stream[1:])
    with pytest.raises(ValueError, match="tile count is 0"):
        sonic2_tiles.decompress(stream[:2] + b"\x00\x00" + stream[4:])
    with pytest.raises(ValueError, match="whole 32-byte tiles"):
        sonic2_tiles.compress(bytes(33))


# -- aPLib and LZ4W (SGDK's two packers) ---------------------------------------

# One 34-byte payload as SGDK's own packers wrote it: apj.jar and lz4w.jar over
# the same bytes, so both vectors are the reference tools' streams and not ours.
_SGDK_PLAIN = b"ABCD" * 4 + b"hello hello hello\x00"
_SGDK_APLIB = bytes.fromhex("4114424344 04e068656c e26f522006 dc3000".replace(" ", ""))
_SGDK_LZ4W = bytes.fromhex(
    "2501 41424344 3402 68656c6c6f20 1000 6f00 0000 0000".replace(" ", "")
)


def test_aplib_decodes_the_reference_packers_stream() -> None:
    """Literals, a block after a literal (bias 3), short matches and the end.

    The block is the trap: its gamma-coded distance high byte is biased by 3
    here because a literal preceded it, and by 2 after another block.
    """
    assert aplib.decompress(_SGDK_APLIB + b"\xff" * 8) == (
        _SGDK_PLAIN,
        len(_SGDK_APLIB),
        True,
    )


def _aplib_stream(first: int, *ops: str | int) -> bytes:
    """Assemble a stream from the raw first byte and a sequence of tag bits
    (a string) and operand bytes (an int), laid out as the reader fetches them:
    a tag byte is allocated at the first bit written after the previous one
    filled, and operand bytes land after it."""
    out = bytearray((first,))
    tag_at, left = -1, 0
    for op in ops:
        if isinstance(op, int):
            out.append(op)
            continue
        for bit in op:
            if not left:
                tag_at, left = len(out), 8
                out.append(0)
            left -= 1
            if bit == "1":
                out[tag_at] |= 1 << left
    return bytes(out)


def _gamma(value: int) -> str:
    """The tag bits of aPLib's gamma code: the leading 1 implied, then each
    remaining bit followed by whether another follows."""
    rest = bin(value)[3:]
    return "".join(b + ("1" if i < len(rest) - 1 else "0") for i, b in enumerate(rest))


def test_aplib_block_bias_and_repeat_form_follow_the_previous_op() -> None:
    """After a block the distance gamma is biased by 2; after a literal a gamma
    of 2 means "the previous block's distance" and the bias is 3.

    Stream: 'A', literal 'B', block(gamma 3 -> high 0, low 2, len gamma 2 + 2
    for distance < 128 = 4 -> "ABAB"), block(gamma 2 after a block -> high 0,
    low 2, len 2 + 2 -> "ABAB"), literal 'C', repeat block(gamma 2, len gamma 3
    -> 3 bytes from 2 back -> "BCB"), end.
    """
    stream = _aplib_stream(
        0x41,
        "0", 0x42,
        "10", _gamma(3), 0x02, _gamma(2),
        "10", _gamma(2), 0x02, _gamma(2),
        "0", 0x43,
        "10", _gamma(2), _gamma(3),
        "110", 0x00,
    )  # fmt: skip
    out, consumed, complete = aplib.decompress(stream)
    assert out == b"AB" + b"ABAB" + b"ABAB" + b"C" + b"BCB"
    assert consumed == len(stream) and complete


def test_aplib_length_adjustments_accumulate_past_32000() -> None:
    """A distance of 32000 earns both the 1280 and the 32000 increments.

    'A', literal 'B', a block at distance 2 for 32000 bytes (bias 3, length
    gamma + 2 below 128), then a block at distance 32000 (bias 2, high 125,
    low 0) whose gamma of 2 has to come out as 4 - and lands on "ABAB" only if
    the distance is right too.
    """
    stream = _aplib_stream(
        0x41,
        "0", 0x42,
        "10", _gamma(3), 0x02, _gamma(31998),
        "10", _gamma(125 + 2), 0x00, _gamma(2),
        "110", 0x00,
    )  # fmt: skip
    out, consumed, complete = aplib.decompress(stream)
    assert out == b"AB" * 16003
    assert consumed == len(stream) and complete


def test_aplib_tiny_form_writes_a_zero_or_copies_one_byte() -> None:
    stream = _aplib_stream(0x41, "111", "0000", "111", "0010", "110", 0x00)
    assert aplib.decompress(stream)[0] == b"A\x00A"


def test_aplib_rejects_a_reach_before_the_output() -> None:
    # 'A' then a short match 3 back into one byte of output.
    with pytest.raises(ValueError, match="reaches 3 bytes back"):
        aplib.decompress(_aplib_stream(0x41, "110", 0x06))


def test_aplib_truncated_stream_needs_the_partial_flag() -> None:
    cut = _SGDK_APLIB[:-2]
    with pytest.raises(ValueError, match="source ended"):
        aplib.decompress(cut)
    prefix, consumed, complete = aplib.decompress(cut, partial=True)
    assert _SGDK_PLAIN.startswith(prefix) and len(prefix) >= 16
    assert consumed == len(cut) and not complete


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(b"A", id="one"),
        pytest.param(bytes(1), id="zero"),
        pytest.param(bytes(3000), id="fill"),
        pytest.param(bytes(range(256)) * 3, id="ramp"),
        pytest.param(
            bytes(random.Random(3).randrange(256) for _ in range(2000)), id="noise"
        ),
        pytest.param(
            bytes(random.Random(4).choice(b"\x00\x11\x22\x33") for _ in range(4001)),
            id="tiles",
        ),
        # A match past the 1280 band; the 32000 one is a decode vector, since
        # encoding that much is a search the suite cannot afford per test.
        pytest.param(
            (tail := bytes(random.Random(5).randrange(256) for _ in range(300)))
            + bytes(1300)
            + tail,
            id="far",
        ),
    ],
)
def test_aplib_round_trips(data: bytes) -> None:
    packed = aplib.compress(data)
    assert aplib.decompress(packed) == (data, len(packed), True)


def test_aplib_plugin_records_size_and_refuses_an_empty_payload() -> None:
    plugin = aplib.AplibCompression()
    ctx = PipelineContext()
    assert plugin.decompress(_SGDK_APLIB + bytes(4), ctx) == _SGDK_PLAIN
    assert ctx.get(KEY_COMPRESSED_SIZE) == len(_SGDK_APLIB)
    assert ctx.get(KEY_DECOMPRESS_COMPLETE) is True
    with pytest.raises(ValueError, match="empty payload"):
        plugin.compress(b"", ctx)


def test_lz4w_decodes_the_reference_packers_stream() -> None:
    """Literals and short matches, all counted in words, and the odd tail byte
    riding in the end marker."""
    assert lz4w.decompress(_SGDK_LZ4W + b"\xff" * 8) == (
        _SGDK_PLAIN,
        len(_SGDK_LZ4W),
        True,
    )


def test_lz4w_long_match_distance_word_follows_the_literals() -> None:
    """A long match: literals first, then the negated 15-bit distance, and the
    match overlapping its own output. Then the end block carries an odd byte."""
    # Distance 1 word is stored as 0: -(1 - 1) & 0x7FFF.
    stream = bytes.fromhex("2000 41424344" + "1001 4546 0000" + "0000 805a")
    assert lz4w.decompress(stream) == (
        b"ABCD" + b"EF" + b"EFEFEF" + b"Z",
        len(stream),
        True,
    )


def test_lz4w_source_relative_match_reads_the_preceding_data() -> None:
    """X = 1 addresses the ROM from the stream's own read pointer: here 5 words
    back from just past the distance word lands on the preceding data's start."""
    previous = bytes.fromhex("112233445566")
    stream = bytes.fromhex("0001 fffc 0000 0000")
    with pytest.raises(ValueError, match="precedes the stream"):
        lz4w.decompress(stream)
    with pytest.raises(ValueError, match="outside the preceding data"):
        lz4w.decompress(stream, previous=previous[2:])
    assert lz4w.decompress(stream, previous=previous) == (previous, len(stream), True)

    plugin = lz4w.Lz4wCompression()
    ctx = PipelineContext()
    ctx.set(KEY_INPUTS, {lz4w.INPUT_PREVIOUS: previous})
    assert plugin.decompress(stream, ctx) == previous
    assert ctx.get(KEY_COMPRESSED_SIZE) == len(stream)


def test_lz4w_rejects_a_reach_before_the_output_and_needs_partial_when_cut() -> None:
    with pytest.raises(ValueError, match="reaches 2 words back"):
        lz4w.decompress(bytes.fromhex("1101 4142"))
    cut = _SGDK_LZ4W[:-5]
    with pytest.raises(ValueError, match="source ended"):
        lz4w.decompress(cut)
    prefix, consumed, complete = lz4w.decompress(cut, partial=True)
    # The cut leaves half a header, which is not consumed.
    assert prefix == _SGDK_PLAIN[:32] and consumed == len(cut) - 1 and not complete


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(b"", id="empty"),
        pytest.param(b"A", id="odd-one"),
        pytest.param(b"AB", id="word"),
        pytest.param(bytes(3000), id="fill"),
        pytest.param(bytes(3001), id="odd-fill"),
        # Forty incompressible words: literal runs past a header's fifteen.
        pytest.param(
            bytes(random.Random(6).randrange(256) for _ in range(80)), id="noise"
        ),
        pytest.param(
            bytes(random.Random(7).choice(b"\x00\x11\x22\x33") for _ in range(4001)),
            id="tiles",
        ),
        # A match further back than the short form's 256 words.
        pytest.param(
            bytes(random.Random(8).randrange(256) for _ in range(1000)) * 2, id="far"
        ),
    ],
)
def test_lz4w_round_trips(data: bytes) -> None:
    packed = lz4w.compress(data)
    assert lz4w.decompress(packed) == (data, len(packed), True)
    assert len(packed) % 2 == 0


def test_lz4w_reads_the_preceding_data_from_the_hosts_surround() -> None:
    """With the buffer the stream was cut from on the context, a source-relative
    match resolves without any binding; a bound input overrides it."""
    previous = bytes.fromhex("112233445566")
    stream = bytes.fromhex("0001 fffc 0000 0000")
    plugin = lz4w.Lz4wCompression()
    ctx = PipelineContext()
    ctx.set(KEY_SURROUND, b"\xaa\xbb" + previous + stream + b"\xcc")
    ctx.set(KEY_SURROUND_START, 2 + len(previous))
    assert plugin.decompress(stream, ctx) == previous
    ctx.set(KEY_INPUTS, {lz4w.INPUT_PREVIOUS: bytes.fromhex("778899aabbcc")})
    assert plugin.decompress(stream, ctx) == bytes.fromhex("778899aabbcc")


def test_lz4w_packs_against_the_preceding_data_only_when_asked() -> None:
    """The pack_previous input turns matches into the preceding bytes into
    source-relative long matches; off, the stream is valid on its own."""
    rng = random.Random(12)
    previous = bytes(rng.choice(b"\x00\x11\x22\x33") for _ in range(1024))
    data = previous[300:900] + bytes(rng.randrange(256) for _ in range(40))
    plugin = lz4w.Lz4wCompression()
    ctx = PipelineContext()
    ctx.set(KEY_SURROUND, previous + b"\xff" * 8)
    ctx.set(KEY_SURROUND_START, len(previous))
    plain = plugin.compress(data, ctx)
    assert lz4w.decompress(plain) == (data, len(plain), True)

    ctx.set(KEY_INPUTS, {lz4w.INPUT_PACK_PREVIOUS: 1})
    packed = plugin.compress(data, ctx)
    assert len(packed) < len(plain)
    with pytest.raises(ValueError, match="copies from"):
        lz4w.decompress(packed)
    assert plugin.decompress(packed, ctx) == data
    # Cut where a save would lay it: the bytes before the slot are the
    # preceding data, and the stream is read from right after them.
    assert lz4w.decompress(packed, previous=previous) == (
        data,
        len(packed),
        True,
    )


def test_lz4w_source_match_past_the_field_is_written_as_literals() -> None:
    """A source-relative distance is measured from the stream's read pointer,
    which incompressible literals push past the output's, so a match that fits
    the output-relative window can still overflow the field; it goes as
    literals and the stream needs no preceding data."""
    rng = random.Random(13)
    tail = bytes(rng.randrange(256) for _ in range(64))
    previous = tail + bytes(
        rng.randrange(256) for _ in range(lz4w.LONG_MAX_DISTANCE * 2 - 64 - 2)
    )
    # 3000 literal words cost 200 headers: enough to push the match past 0x4000.
    data = bytes(rng.randrange(256) for _ in range(6000)) + tail
    packed = lz4w.compress(data, previous=previous)
    assert lz4w.decompress(packed) == (data, len(packed), True)


def test_lz4w_pack_previous_without_preceding_data_warns_and_packs_plain() -> None:
    from celpix.core.notices import notices

    plugin = lz4w.Lz4wCompression()
    ctx = PipelineContext()
    ctx.set(KEY_INPUTS, {lz4w.INPUT_PACK_PREVIOUS: 1})
    packed = plugin.compress(b"ABABABAB", ctx)
    assert lz4w.decompress(packed)[0] == b"ABABABAB"
    assert [n.summary for n in notices(ctx)] == ["Packed without the preceding data"]
