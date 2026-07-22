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
from celpix.plugins.builtins import konami_rle, lz16, lz_command
from celpix.plugins.builtins.konami_rle import (
    KonamiFdsRleCompress,
    KonamiFdsRleDecompress,
    KonamiNesRleCompress,
    KonamiNesRleDecompress,
)
from celpix.plugins.builtins.lz16 import KEY_LZ16_ROWS, Lz16Decompress
from celpix.plugins.builtins.lz_command import (
    Lz1Decompress,
    Lz2Compress,
    Lz2Decompress,
)
from celpix.plugins.builtins.m7_interleave import M7VramCompress, M7VramDecompress

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
def test_lz_round_trip(big_endian: bool) -> None:
    rng = random.Random(1)
    payloads = [
        b"",
        b"\x00" * 2000,
        bytes(range(256)) * 5,
        bytes(rng.randrange(256) for _ in range(3000)),
        bytes(rng.choice(b"\x00\x0f\xf0") for _ in range(1000)),
    ]
    for data in payloads:
        packed = lz_command.compress(data, big_endian_offsets=big_endian)
        out, consumed = lz_command.decompress(
            packed + b"\x5a" * 9, big_endian_offsets=big_endian
        )
        assert out == data
        # Trailing garbage is never consumed — the terminator bounds the read.
        assert consumed == len(packed)


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
    packed = Lz2Compress().compress(data, PipelineContext())
    ctx = PipelineContext()
    ctx.set(KEY_DECOMPRESS_PARTIAL, True)
    out = Lz2Decompress().decompress(packed[:-1], ctx)  # terminator cut off
    assert data[: len(out)] == out
    assert ctx.get(KEY_DECOMPRESS_COMPLETE) is False  # truncated: end unknown


def test_lz_malformed_raises() -> None:
    with pytest.raises(ValueError):  # no terminator
        lz_command.decompress(b"\x03\x41", big_endian_offsets=True)
    with pytest.raises(ValueError):  # backref into unwritten output
        lz_command.decompress(b"\x82\x12\x34\xff", big_endian_offsets=True)


def test_lz_plugins_record_compressed_size() -> None:
    data = b"\x07" * 100
    packed = Lz2Compress().compress(data, PipelineContext())
    ctx = PipelineContext()
    # LZ1 and LZ2 agree on everything but backrefs; an all-fill stream decodes
    # identically, which keeps this plugin-level check codec-agnostic.
    assert Lz1Decompress().decompress(packed + b"\x00" * 3, ctx) == data
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
    assert Lz16Decompress().decompress(packed, ctx) == tiles
    assert ctx.get(KEY_LZ16_ROWS) == rows
    assert ctx.get(KEY_COMPRESSED_SIZE) == len(packed)


def test_lz16_plugin_honours_explicit_rows() -> None:
    # An over-read buffer defeats the probe, but an explicit row count from the
    # context still decodes it.
    tiles, rows = _tile_payloads()[1]
    packed = lz16.compress(tiles)
    ctx = PipelineContext()
    ctx.set(KEY_LZ16_ROWS, rows)
    assert Lz16Decompress().decompress(packed + b"\xa5" * 5, ctx) == tiles


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
        Lz16Decompress().decompress(packed + b"\x00" * 40, ctx)[: len(tiles)] == tiles
    )


def test_lz16_partial_rejects_non_lz16_data() -> None:
    # The first tile row is the validity test — data that can't even produce
    # one row is "not LZ16", not a truncated structure.
    with pytest.raises(ValueError):
        lz16.decompress_partial(b"\x12\x34")


def test_lz16_compress_rejects_partial_tile_rows() -> None:
    with pytest.raises(ValueError):
        lz16.compress(bytes(511))
    with pytest.raises(ValueError):
        lz16.compress(b"")


# -- SNES Mode 7 VRAM split -------------------------------------------------


def test_m7_vram_split_known_vector() -> None:
    # Interleaved words: map bytes at even offsets, pixel bytes at odd offsets.
    # The split puts the pixels first, then the map.
    interleaved = bytes([0xA0, 0x01, 0xA1, 0x02, 0xA2, 0x03])
    split = M7VramDecompress().decompress(interleaved, PipelineContext())
    assert split == bytes([0x01, 0x02, 0x03, 0xA0, 0xA1, 0xA2])
    assert M7VramCompress().compress(split, PipelineContext()) == interleaved


def test_m7_vram_split_round_trips_odd_length() -> None:
    data = bytes((i * 37 + 5) & 0xFF for i in range(129))
    split = M7VramDecompress().decompress(data, PipelineContext())
    assert M7VramCompress().compress(split, PipelineContext()) == data


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
    packed = KonamiNesRleCompress().compress(data, PipelineContext())
    ctx = PipelineContext()
    out = KonamiNesRleDecompress().decompress(packed + b"\x5a" * 6, ctx)
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


def test_konami_fds_plugins_record_size_and_round_trip() -> None:
    data = b"\x00" * 50 + bytes(range(30)) + b"\xff" * 40
    packed = KonamiFdsRleCompress().compress(data, PipelineContext())
    ctx = PipelineContext()
    out = KonamiFdsRleDecompress().decompress(packed + b"\x5a" * 6, ctx)
    assert out == data
    # The terminator position is the structure's byte length; trailing garbage
    # past it is not counted.
    assert ctx.get(KEY_COMPRESSED_SIZE) == len(packed)
    assert ctx.get(KEY_DECOMPRESS_COMPLETE) is True
