"""The PVR texture plugin: twiddle math, payload geometry, and save-back.

The twiddle reference below is written straight from the format specification
(``docs/graphics-formats-reference/implementation-guide.md`` §7), so the address
math is guarded independently of the table the plugin actually builds.
"""

from __future__ import annotations

import random
import struct

import pytest

from celpix.core.context import (
    KEY_COMPRESSED_SIZE,
    KEY_DECOMPRESS_COMPLETE,
    PipelineContext,
)
from celpix.core.notices import notices
from celpix.plugins.builtins import lzss_ring, pvr
from celpix.plugins.builtins.pvr import PvrCompression

# -- helpers -----------------------------------------------------------------


def _morton(x: int, y: int) -> int:
    """Interleave the low bits of x and y — y takes the odd bit positions."""
    out = 0
    for bit in range(max(x, y).bit_length()):
        out |= ((x >> bit) & 1) << (2 * bit)
        out |= ((y >> bit) & 1) << (2 * bit + 1)
    return out


def _spec_index(x: int, y: int, width: int, height: int) -> int:
    """Morton order over square blocks laid along the longer dimension."""
    side = min(width, height)
    if width >= height:
        return (x // side) * side * side + _morton(x % side, y)
    return (y // side) * side * side + _morton(x, y % side)


def _chunk(
    pixel_format: int,
    data_format: int,
    width: int,
    height: int,
    payload: bytes,
    gbix: int | None = None,
) -> bytes:
    head = struct.pack(
        "<4sIBBHHH",
        b"PVRT",
        8 + len(payload),
        pixel_format,
        data_format,
        0,
        width,
        height,
    )
    lead = b"" if gbix is None else struct.pack("<4sII", b"GBIX", 8, gbix) + bytes(4)
    return lead + head + payload


def _texels(count: int, seed: int = 1) -> list[int]:
    rng = random.Random(seed)
    return [rng.randrange(0x10000) for _ in range(count)]


def _twiddled_chunk(width: int, height: int, seed: int = 1) -> tuple[bytes, list[int]]:
    """A twiddled ARGB1555 chunk plus the linear texels it should decode to."""
    linear = _texels(width * height, seed)
    stored = [0] * (width * height)
    for y in range(height):
        for x in range(width):
            stored[_spec_index(x, y, width, height)] = linear[y * width + x]
    return _chunk(
        0x00, 0x01, width, height, struct.pack(f"<{len(stored)}H", *stored)
    ), linear


def _decode(blob: bytes) -> tuple[list[int], PipelineContext]:
    ctx = PipelineContext()
    out = PvrCompression().decompress(blob, ctx)
    return list(struct.unpack(f"<{len(out) // 2}H", out)), ctx


# -- twiddling ---------------------------------------------------------------


@pytest.mark.parametrize(
    "width,height",
    [(8, 8), (64, 64), (64, 32), (32, 64), (128, 16), (1, 1)],
)
def test_twiddle_map_matches_the_spec(width: int, height: int) -> None:
    # Both rectangle orientations: the aspect decides which axis is cut into
    # square blocks, so a map built for 64x32 is wrong for 32x64.
    expected = tuple(
        _spec_index(x, y, width, height) for y in range(height) for x in range(width)
    )
    assert pvr._twiddle_map(width, height) == expected


def test_twiddle_round_trips_and_rejects_non_power_of_two() -> None:
    values = list(range(64 * 32))
    assert pvr.untwiddle(pvr.twiddle(values, 64, 32), 64, 32) == values
    with pytest.raises(ValueError):
        pvr._twiddle_map(48, 32)


# -- decode ------------------------------------------------------------------


def test_twiddled_texture_decodes_to_linear_order() -> None:
    blob, linear = _twiddled_chunk(64, 32)
    values, ctx = _decode(blob)
    assert values == linear
    assert ctx.get(KEY_COMPRESSED_SIZE) == len(blob)
    assert ctx.get(KEY_DECOMPRESS_COMPLETE) is True


def test_linear_data_formats_are_not_untwiddled() -> None:
    # Data format 0x09 stores texels in row order already; untwiddling it would
    # scramble a texture that was never twiddled in the first place.
    linear = _texels(32 * 16)
    blob = _chunk(0x00, 0x09, 32, 16, struct.pack(f"<{len(linear)}H", *linear))
    assert _decode(blob)[0] == linear


def test_gbix_block_is_stepped_over() -> None:
    blob, linear = _twiddled_chunk(16, 16)
    with_gbix = struct.pack("<4sII", b"GBIX", 8, 0xCAFE) + bytes(4) + blob
    assert _decode(with_gbix)[0] == linear


def test_mip_base_level_is_the_payload_tail() -> None:
    # Mip chains are stored smallest-first, so the full-size level sits at the
    # END of the payload. Reading forward from zero yields the 1x1 level.
    base, linear = _twiddled_chunk(32, 32)
    smaller = bytes(sum(max(1, 32 >> i) ** 2 for i in range(1, 6)) * 2)
    blob = _chunk(0x00, 0x02, 32, 32, smaller + base[16:])
    assert _decode(blob)[0] == linear


def test_pal4_indices_take_the_low_nibble_first() -> None:
    # Pixel 2k is the low nibble of byte k. Swapping the halves still decodes to
    # a plausible image, so only a byte-level check catches it.
    payload = bytes([0x21, 0x43])
    blob = _chunk(0x00, 0x05, 2, 2, payload)
    out = PvrCompression().decompress(blob, PipelineContext())
    assert pvr._unpack_indices(out, 4, True) == [1, 2, 3, 4]


def test_vq_expands_2x2_blocks_from_the_codebook() -> None:
    # Codebook at the FRONT, index map at the BACK; each entry is one 2x2 block
    # stored TL, TR, BL, BR.
    codebook = struct.pack(
        "<8H", 0x1111, 0x2222, 0x3333, 0x4444, 0xAAAA, 0xBBBB, 0xCCCC, 0xDDDD
    )
    payload = codebook + bytes([0, 1, 1, 0])  # 2x2 blocks -> a 4x4 texture
    values, _ = _decode(_chunk(0x00, 0x10, 4, 4, payload))
    assert values == [
        0x1111,
        0x2222,
        0xAAAA,
        0xBBBB,
        0x3333,
        0x4444,
        0xCCCC,
        0xDDDD,
        0xAAAA,
        0xBBBB,
        0x1111,
        0x2222,
        0xCCCC,
        0xDDDD,
        0x3333,
        0x4444,
    ]


# -- the CPR (LZSS) wrapper --------------------------------------------------


def test_incompressible_texture_is_still_recognised_as_wrapped() -> None:
    # The wrapper is confirmed by expanding it and finding the magic, not by
    # "declared size exceeds the buffer" — random texels expand under LZSS, and
    # a size test would refuse to open the result at all.
    blob, linear = _twiddled_chunk(64, 64, seed=5)
    wrapped = lzss_ring.compress(blob)
    assert len(wrapped) > len(blob)  # the premise: this one did not compress
    values, ctx = _decode(wrapped)
    assert values == linear
    # The structure's extent in the file is the compressed member, not the chunk.
    assert ctx.get(KEY_COMPRESSED_SIZE) == len(wrapped)


def test_unwrapped_chunk_reports_its_own_length() -> None:
    blob, _ = _twiddled_chunk(16, 16)
    _, ctx = _decode(blob + b"\xff" * 32)  # trailing bytes are the next structure
    assert ctx.get(KEY_COMPRESSED_SIZE) == len(blob)


def test_data_that_is_neither_wrapped_nor_a_chunk_raises() -> None:
    with pytest.raises(ValueError):
        PvrCompression().decompress(b"\x00" * 64, PipelineContext())


# -- save-back ---------------------------------------------------------------


@pytest.mark.parametrize("gbix", [None, 0xCAFE])
def test_save_back_rebuilds_the_chunk_byte_for_byte(gbix: int | None) -> None:
    blob, _ = _twiddled_chunk(32, 64)
    if gbix is not None:
        blob = struct.pack("<4sII", b"GBIX", 8, gbix) + bytes(4) + blob
    ctx = PipelineContext()
    out = PvrCompression().decompress(blob, ctx)
    assert PvrCompression().compress(out, ctx) == blob


def test_save_back_preserves_the_smaller_mip_levels() -> None:
    # Only the base level is editable; the rest must come back untouched rather
    # than zeroed or dropped.
    base, _ = _twiddled_chunk(32, 32)
    # The real chain below the base level: 16x16 down to 1x1, two bytes a texel,
    # filled with a recognisable pattern so a dropped or zeroed level shows up.
    chain_bytes = sum(max(1, 32 >> i) ** 2 for i in range(1, 6)) * 2
    smaller = bytes((i * 7) & 0xFF for i in range(chain_bytes))
    blob = _chunk(0x00, 0x02, 32, 32, smaller + base[16:])
    ctx = PipelineContext()
    out = PvrCompression().decompress(blob, ctx)
    assert PvrCompression().compress(out, ctx) == blob


def test_save_back_rewraps_when_the_source_was_wrapped() -> None:
    blob, _ = _twiddled_chunk(32, 32)
    ctx = PipelineContext()
    out = PvrCompression().decompress(lzss_ring.compress(blob), ctx)
    assert lzss_ring.decompress(PvrCompression().compress(out, ctx))[0] == blob


def test_vq_is_view_only() -> None:
    payload = struct.pack("<4H", 0, 1, 2, 3) + bytes([0] * 4)
    ctx = PipelineContext()
    out = PvrCompression().decompress(_chunk(0x00, 0x10, 4, 4, payload), ctx)
    with pytest.raises(ValueError):
        PvrCompression().compress(out, ctx)


def test_save_back_without_a_loaded_header_raises() -> None:
    # The header is what a chunk is rebuilt around, so writing through a context
    # that never decoded one has to fail rather than invent one.
    with pytest.raises(ValueError):
        PvrCompression().compress(b"\x00" * 64, PipelineContext())


# -- notices -----------------------------------------------------------------


def test_decode_names_the_preset_that_reads_its_output() -> None:
    # A codec is handed a fresh context, so the only way the user learns which
    # pixel format the texture is in is the notice.
    blob, _ = _twiddled_chunk(16, 16)
    _, ctx = _decode(blob)
    said = " ".join(n.summary + n.detail for n in notices(ctx))
    assert "ARGB1555" in said and "16x16" in said


def test_mipmapped_and_vq_textures_warn() -> None:
    base, _ = _twiddled_chunk(32, 32)
    smaller = bytes(sum(max(1, 32 >> i) ** 2 for i in range(1, 6)) * 2)
    _, ctx = _decode(_chunk(0x00, 0x02, 32, 32, smaller + base[16:]))
    assert any(n.is_warning and "mip" in n.summary.lower() for n in notices(ctx))

    payload = struct.pack("<4H", 0, 1, 2, 3) + bytes(4)
    _, ctx = _decode(_chunk(0x00, 0x10, 4, 4, payload))
    assert any(n.is_warning and "view-only" in n.summary for n in notices(ctx))
