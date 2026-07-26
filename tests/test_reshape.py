"""The Reshape stage: plugin round trips, pipeline position, and write rules."""

from __future__ import annotations

import pytest

from celpix.core.context import (
    KEY_COMPRESSED_SIZE,
    KEY_DECOMPRESS_COMPLETE,
    PipelineContext,
)
from celpix.core.errors import PipelineError, Stage
from celpix.pipeline import pipeline
from celpix.pipeline.pathway import PathwayConfig
from celpix.plugins.base import NO_RESHAPE, FileRef, PluginInfo
from celpix.plugins.bitswap import BitswapReshape, bitswap_from_toml
from celpix.plugins.builtins.konami_rle import KonamiNesRle
from celpix.plugins.builtins.m7_interleave import M7VramReshape
from celpix.plugins.builtins.split_planes import PART_COUNTS, SplitPartsReshape
from celpix.plugins.registry import default_registry
from celpix.project.workspace import backfill_slice_length, new_slice

# -- SNES Mode 7 VRAM split -------------------------------------------------


def test_m7_vram_split_known_vector() -> None:
    # Interleaved words: map bytes at even offsets, pixel bytes at odd offsets.
    # The split puts the pixels first, then the map.
    interleaved = bytes([0xA0, 0x01, 0xA1, 0x02, 0xA2, 0x03])
    split = M7VramReshape().reshape(interleaved, PipelineContext())
    assert split == bytes([0x01, 0x02, 0x03, 0xA0, 0xA1, 0xA2])
    assert M7VramReshape().unshape(split, PipelineContext()) == interleaved


def test_m7_vram_split_round_trips_odd_length() -> None:
    data = bytes((i * 37 + 5) & 0xFF for i in range(129))
    split = M7VramReshape().reshape(data, PipelineContext())
    assert M7VramReshape().unshape(split, PipelineContext()) == data


# -- Split bitplanes (N equal parts, one per plane) --------------------------


def test_split_planes_join_interleaves_parts_in_order() -> None:
    """Part *k* must land on tile byte ``k + N * y``, which is what makes the
    shipped ``{ base = k, stride = N }`` planar presets read the joined buffer."""
    parts = bytes((0x10, 0x11, 0x12)) + bytes((0x20, 0x21, 0x22))
    joined = SplitPartsReshape(2).reshape(parts, PipelineContext())
    assert joined == bytes((0x10, 0x20, 0x11, 0x21, 0x12, 0x22))
    assert SplitPartsReshape(2).unshape(joined, PipelineContext()) == parts


@pytest.mark.parametrize("parts", PART_COUNTS)
def test_split_planes_round_trips_including_ragged_tail(parts: int) -> None:
    """Both directions are exact for every part count, and a length that isn't a
    whole number of parts keeps its tail rather than shearing the image."""
    plugin = SplitPartsReshape(parts)
    ctx = PipelineContext()
    for length in (0, 1, parts * 8 - 1, parts * 8, parts * 8 + 3):
        data = bytes((i * 37 + 5) & 0xFF for i in range(length))
        assert plugin.unshape(plugin.reshape(data, ctx), ctx) == data
        assert plugin.reshape(plugin.unshape(data, ctx), ctx) == data


# -- ROM-pair word interleave (ROM_LOAD32_WORD) ------------------------------


def test_word_interleave_weaves_16bit_words_alternately() -> None:
    """Two chips alternating at 16-bit granularity — the byte pairs must stay
    intact, which is exactly what distinguishes this from split-planes-2."""
    chips = bytes((0x10, 0x11, 0x12, 0x13)) + bytes((0x20, 0x21, 0x22, 0x23))
    joined = SplitPartsReshape(2, unit=2).reshape(chips, PipelineContext())
    assert joined == bytes((0x10, 0x11, 0x20, 0x21, 0x12, 0x13, 0x22, 0x23))
    assert SplitPartsReshape(2, unit=2).unshape(joined, PipelineContext()) == chips


def test_word_interleave_round_trips_including_ragged_tail() -> None:
    plugin = SplitPartsReshape(2, unit=2)
    ctx = PipelineContext()
    for length in (0, 1, 3, 4, 7, 8, 10, 34):
        data = bytes((i * 37 + 5) & 0xFF for i in range(length))
        assert plugin.unshape(plugin.reshape(data, ctx), ctx) == data
        assert plugin.reshape(plugin.unshape(data, ctx), ctx) == data
        # Whatever doesn't fill a whole word-aligned pair of parts is passed
        # through at the end, not folded into a part.
        aligned = (length // 4) * 4
        assert plugin.reshape(data, ctx)[aligned:] == data[aligned:]


# -- position in the pipeline ------------------------------------------------


def test_reshape_runs_between_container_read_and_decompress(tmp_path) -> None:
    """A compressed structure inside a split region is only contiguous after
    the join, so the file holds ``unshape(packed)`` and only the order
    read -> reshape -> decompress recovers the payload."""
    ctx = PipelineContext()
    payload = b"\x00" * 40 + bytes(range(24)) + b"\xff" * 16
    packed = KonamiNesRle().compress(payload, ctx)
    scattered = SplitPartsReshape(2).unshape(packed, ctx)
    px = tmp_path / "pair.bin"
    px.write_bytes(scattered)
    cfg = PathwayConfig(
        source=FileRef(str(px)),
        interpret_preset_id="preset.pixel.snes-4bpp",
        reshape_id="reshape.split-planes-2",
        compression_id="compression.konami-nes-rle",
    )
    data = pipeline.load_pixel_data(cfg, default_registry())
    assert data.data == payload


def _make_files(tmp_path):
    # 4 SNES 4bpp tiles (32B each) + 16 BGR555 colors, as test_pipeline builds.
    pixel_bytes = bytes((i * 29 + 5) & 0xFF for i in range(32 * 4))
    pal = bytearray((i * 17 + 3) & 0xFF for i in range(2 * 16))
    for off in range(1, len(pal), 2):
        pal[off] &= 0x7F
    px = tmp_path / "gfx.4bpp.sfc"
    pl = tmp_path / "gfx.4bpp.sfc.pal"
    px.write_bytes(pixel_bytes)
    pl.write_bytes(bytes(pal))
    return px, pl, pixel_bytes


def test_reshaped_file_save_round_trips_byte_identical(tmp_path) -> None:
    reg = default_registry()
    px, pl, pixel_bytes = _make_files(tmp_path)
    pixel_cfg = PathwayConfig(
        source=FileRef(str(px)),
        interpret_preset_id="preset.pixel.snes-4bpp",
        reshape_id="reshape.split-planes-2",
    )
    palette_cfg = PathwayConfig(
        source=FileRef(str(pl)), interpret_preset_id="preset.palette.bgr555"
    )
    doc = pipeline.load(pixel_cfg, palette_cfg, reg)
    # The view sees the joined bytes, not the file's order...
    assert doc.pixel_data == SplitPartsReshape(2).reshape(
        pixel_bytes, PipelineContext()
    )
    # ...and an untouched save puts every byte back where it came from.
    pipeline.save(doc, reg)
    assert px.read_bytes() == pixel_bytes


class _StubCompression:
    """Compression scheme whose *packed* size is dictated by the test."""

    def __init__(self, packed: bytes) -> None:
        self.info = PluginInfo(
            id="compression.stub", name="Stub", stage=Stage.COMPRESSION
        )
        self._packed = packed

    def decompress(self, data: bytes, ctx: PipelineContext) -> bytes:
        return data

    def compress(self, data: bytes, ctx: PipelineContext) -> bytes:
        return self._packed


def _save_reshaped_slice_with_stub(tmp_path, packed: bytes):
    """A 64-byte reshaped slice at offset 32 whose compressor emits ``packed``."""
    reg = default_registry()
    px, pl, pixel_bytes = _make_files(tmp_path)
    pixel_cfg = PathwayConfig(
        source=FileRef(str(px), offset=32, length=64),
        dest=FileRef(str(px), offset=32, length=64),
        interpret_preset_id="preset.pixel.snes-4bpp",
        reshape_id="reshape.split-planes-2",
        compression_id="compression.stub",
    )
    palette_cfg = PathwayConfig(
        source=FileRef(str(pl)),
        interpret_preset_id="preset.palette.bgr555",
        write_enabled=False,
    )
    reg.register(_StubCompression(packed))
    doc = pipeline.load(pixel_cfg, palette_cfg, reg)
    return px, pixel_bytes, lambda: pipeline.save(doc, reg)


def test_reshaped_bounded_write_requires_exact_fill(tmp_path) -> None:
    # Short-of-the-slot is fine for a self-delimiting compressed stream, but a
    # reshape's part boundaries are len/N of the region: writing the front of a
    # slot is writing a *different region*, so it must refuse.
    px, pixel_bytes, save = _save_reshaped_slice_with_stub(tmp_path, bytes(10))
    with pytest.raises(PipelineError) as excinfo:
        save()
    assert (excinfo.value.stage, excinfo.value.action) == (Stage.RESHAPE, "unshape")
    assert px.read_bytes() == pixel_bytes  # nothing partial written


def test_reshaped_bounded_write_accepts_exact_fill(tmp_path) -> None:
    px, pixel_bytes, save = _save_reshaped_slice_with_stub(tmp_path, b"\xab" * 64)
    save()
    out = px.read_bytes()
    assert out[32:96] == b"\xab" * 64  # a fill is its own unshape
    assert out[:32] == pixel_bytes[:32] and out[96:] == pixel_bytes[96:]


# -- the bitswap engine (reshapes as data) -----------------------------------


def _ref_bitswap(i: int, bits_msb: list[int]) -> int:
    """MAME's bitswap<N>(i, ...), written independently of the engine."""
    n = len(bits_msb)
    return sum(((i >> src) & 1) << (n - 1 - k) for k, src in enumerate(bits_msb))


def _bitswap(bits: list[int], gather: bool = False) -> BitswapReshape:
    return BitswapReshape("reshape.test", "test", bits, gather)


def test_bitswap_scatters_through_the_table() -> None:
    # 3-bit table: out bit2 <- in bit2, bit1 <- bit0, bit0 <- bit1. Checked
    # byte-for-byte against an independent bitswap so a chunk-plan bug in the
    # engine can't hide behind a matching round trip.
    bits = [2, 0, 1]
    data = bytes((i * 37 + 5) & 0xFF for i in range(8))
    out = _bitswap(bits).reshape(data, PipelineContext())
    assert all(out[_ref_bitswap(i, bits)] == data[i] for i in range(8))


def test_bitswap_round_trips_across_blocks_and_keeps_the_tail() -> None:
    # A 16-byte block over a 3.5-block region: each block permutes on its own
    # (chosen so the chunk plan is exercised: bits 1..0 identity -> 4-byte
    # chunks), and the half-block tail passes through untouched.
    bits = [2, 3, 1, 0]
    plugin = _bitswap(bits)
    ctx = PipelineContext()
    data = bytes((i * 37 + 5) & 0xFF for i in range(16 * 3 + 8))
    out = plugin.reshape(data, ctx)
    assert plugin.unshape(out, ctx) == data
    assert out[48:] == data[48:]
    for base in (0, 16, 32):
        block = data[base : base + 16]
        assert all(out[base + _ref_bitswap(i, bits)] == block[i] for i in range(16))


def test_bitswap_gather_is_the_inverse_direction() -> None:
    # gather = true reads the table as dst[i] = src[bitswap(i)] — the other
    # MAME idiom — so its reshape must equal the scatter plugin's unshape.
    bits = [0, 2, 1, 3]
    ctx = PipelineContext()
    data = bytes(range(16))
    scatter, gather = _bitswap(bits), _bitswap(bits, gather=True)
    assert gather.reshape(data, ctx) == scatter.unshape(data, ctx)
    assert gather.unshape(gather.reshape(data, ctx), ctx) == data


def test_bitswap_rejects_bad_tables_and_undersized_regions() -> None:
    with pytest.raises(ValueError):
        _bitswap([2, 1, 1])  # not a permutation
    with pytest.raises(ValueError):
        _bitswap([])  # no address lines at all
    with pytest.raises(ValueError):
        bitswap_from_toml(
            'id = "reshape.x"\nname = "x"\nengine_id = "reshape.wrong"\n'
            "[params]\nbits = [0]\n"
        )
    with pytest.raises(ValueError):
        # A region smaller than one block would reshape nothing at all — a
        # misconfiguration, not a tail.
        _bitswap([3, 2, 1, 0]).reshape(bytes(8), PipelineContext())


def test_shipped_gaelco_preset_applies_the_driver_table() -> None:
    """The shipped preset registers as an ordinary reshape plugin and moves
    bytes exactly where the MAME driver's bitswap<20> table says."""
    plugin = default_registry().plugin(Stage.RESHAPE, "reshape.gaelco-16x16")
    bits = [19, 18, 17, 16, 15, 12, 11, 10, 9, 8, 7, 6, 5, 14, 13, 4, 3, 2, 1, 0]
    data = bytes(range(256)) * 4096  # exactly one 1 MiB block
    out = plugin.reshape(data, PipelineContext())
    for i in (0, 1 << 5, 1 << 13, 1 << 14, (1 << 19) | 0x1F, 0xABCDE):
        assert out[_ref_bitswap(i, bits)] == data[i]
    assert plugin.unshape(out, PipelineContext()) == data


# -- slice-length discovery is off under a reshape ---------------------------


def test_backfill_slice_length_refuses_under_a_reshape() -> None:
    # The discovered extent is measured in *reshaped* space; recording it would
    # re-bound the window and change the permutation itself.
    ctx = PipelineContext()
    ctx.set(KEY_COMPRESSED_SIZE, 40)
    ctx.set(KEY_DECOMPRESS_COMPLETE, True)
    plain = new_slice("rom.bin", "s", 0, None, "compression.konami-nes-rle")
    assert backfill_slice_length(plain, ctx)
    reshaped = new_slice(
        "rom.bin",
        "s",
        0,
        None,
        "compression.konami-nes-rle",
        reshape_id="reshape.split-planes-2",
    )
    assert not backfill_slice_length(reshaped, ctx)
    assert reshaped.slice_length is None
    assert reshaped.reshape_id != NO_RESHAPE
