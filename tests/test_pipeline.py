"""End-to-end pipeline: byte-identical round trip + hard-stop failures."""

from __future__ import annotations

import pytest

from celpix.core.context import PipelineContext
from celpix.core.errors import PipelineError, Stage
from celpix.pipeline import pipeline
from celpix.pipeline.pathway import PathwayConfig
from celpix.plugins.base import FileRef
from celpix.plugins.registry import default_registry


def _make_files(tmp_path):
    # 4 SNES 4bpp tiles (32B each) of deterministic bytes.
    pixel_bytes = bytes((i * 29 + 5) & 0xFF for i in range(32 * 4))
    # 16 BGR555 colours (32B), unused bit 15 cleared for an exact round trip.
    pal = bytearray((i * 17 + 3) & 0xFF for i in range(2 * 16))
    for off in range(1, len(pal), 2):
        pal[off] &= 0x7F
    px = tmp_path / "gfx.4bpp.sfc"
    pl = tmp_path / "gfx.4bpp.sfc.pal"
    px.write_bytes(pixel_bytes)
    pl.write_bytes(bytes(pal))
    return px, pl, pixel_bytes, bytes(pal)


def _configs(px, pl):
    pixel = PathwayConfig(
        source=FileRef(str(px)), interpret_preset_id="preset.pixel.snes-4bpp"
    )
    palette = PathwayConfig(
        source=FileRef(str(pl)), interpret_preset_id="preset.palette.bgr555"
    )
    return pixel, palette


def test_load_then_save_is_byte_identical(tmp_path) -> None:
    reg = default_registry()
    px, pl, pixel_bytes, pal_bytes = _make_files(tmp_path)
    pixel_cfg, palette_cfg = _configs(px, pl)

    doc = pipeline.load(pixel_cfg, palette_cfg, reg)
    assert doc.tile_count == 4  # 128 bytes / 32 bytes-per-tile, decoded on demand
    assert len(doc.palette) == 16

    pipeline.save(doc, reg)
    assert px.read_bytes() == pixel_bytes
    assert pl.read_bytes() == pal_bytes


def test_decode_window_matches_full_decode(tmp_path) -> None:
    reg = default_registry()
    px, pl, pixel_bytes, _ = _make_files(tmp_path)  # 4 SNES 4bpp tiles
    pixel_cfg, palette_cfg = _configs(px, pl)
    doc = pipeline.load(pixel_cfg, palette_cfg, reg)

    preset = reg.preset(pixel_cfg.interpret_preset_id)
    engine = reg.plugin(Stage.INTERPRET_PIXEL, preset.engine_id)
    all_tiles = engine.decode(pixel_bytes, preset.params, PipelineContext())

    # A windowed decode returns exactly the same tiles as slicing a full decode.
    assert pipeline.decode_window(doc, reg, 1, 2) == all_tiles[1:3]
    # A window running past the end yields only the tiles that exist.
    assert pipeline.decode_window(doc, reg, 3, 5) == all_tiles[3:4]


def test_provenance_recorded(tmp_path) -> None:
    from celpix.core.context import KEY_SOURCE_PATH

    reg = default_registry()
    px, pl, *_ = _make_files(tmp_path)
    pixel_cfg, palette_cfg = _configs(px, pl)
    doc = pipeline.load(pixel_cfg, palette_cfg, reg)
    assert doc.pixel_ctx.get(KEY_SOURCE_PATH) == str(px)


def test_palette_write_optional(tmp_path) -> None:
    reg = default_registry()
    px, pl, _, pal_bytes = _make_files(tmp_path)
    pixel_cfg, palette_cfg = _configs(px, pl)
    palette_cfg.write_enabled = False
    doc = pipeline.load(pixel_cfg, palette_cfg, reg)
    # Corrupt the palette in memory, then save: the file must be untouched.
    doc.palette.colors[0] = 0xFFFFFFFF
    pipeline.save(doc, reg)
    assert pl.read_bytes() == pal_bytes


def test_misaligned_pixel_buffer_pads_the_last_tile(tmp_path) -> None:
    # 1.5 tiles' worth of data: the partial tile counts and decodes zero-padded.
    reg = default_registry()
    px = tmp_path / "odd.4bpp.sfc"
    pixel_bytes = bytes((i * 29 + 5) & 0xFF for i in range(48))
    px.write_bytes(pixel_bytes)
    pl = tmp_path / "p.pal"
    pl.write_bytes(b"\x00" * 32)
    pixel_cfg, palette_cfg = _configs(px, pl)

    doc = pipeline.load(pixel_cfg, palette_cfg, reg)
    assert doc.tile_count == 2
    assert doc.pixel_data == pixel_bytes  # padding is decode-only, never stored

    preset = reg.preset(pixel_cfg.interpret_preset_id)
    engine = reg.plugin(Stage.INTERPRET_PIXEL, preset.engine_id)
    padded = engine.decode(pixel_bytes + bytes(16), preset.params, PipelineContext())
    assert pipeline.decode_window(doc, reg, 0, 2) == padded


def _slice_configs(px, pl, offset, length):
    pixel = PathwayConfig(
        source=FileRef(str(px), offset=offset, length=length),
        interpret_preset_id="preset.pixel.snes-4bpp",
    )
    palette = PathwayConfig(
        source=FileRef(str(pl)), interpret_preset_id="preset.palette.bgr555"
    )
    return pixel, palette


def test_slice_round_trip_touches_only_the_slice(tmp_path) -> None:
    # A bounded source (a slice of the parent) loads just that window and saves
    # back in place: bytes outside [offset, offset+length) stay byte-identical.
    reg = default_registry()
    px, pl, pixel_bytes, _ = _make_files(tmp_path)
    pixel_cfg, palette_cfg = _slice_configs(px, pl, offset=32, length=64)

    doc = pipeline.load(pixel_cfg, palette_cfg, reg)
    assert doc.tile_count == 2
    assert doc.pixel_data == pixel_bytes[32:96]

    pipeline.save(doc, reg)
    assert px.read_bytes() == pixel_bytes


class _StubCompress:
    """Compressor whose output size is dictated by the test."""

    def __init__(self, packed: bytes) -> None:
        from celpix.plugins.base import PluginInfo

        self.info = PluginInfo(id="compress.stub", name="Stub", stage=Stage.COMPRESS)
        self._packed = packed

    def compress(self, data: bytes, ctx: PipelineContext) -> bytes:
        return self._packed


def _save_slice_with_stub(tmp_path, packed: bytes):
    """Save a 64-byte slice at offset 32 through a stub compressor emitting
    ``packed``; returns (path, original bytes, save thunk)."""
    reg = default_registry()
    px, pl, pixel_bytes, _ = _make_files(tmp_path)
    pixel_cfg, palette_cfg = _slice_configs(px, pl, offset=32, length=64)
    pixel_cfg.compress_id = "compress.stub"
    palette_cfg.write_enabled = False
    reg.register(_StubCompress(packed))
    doc = pipeline.load(pixel_cfg, palette_cfg, reg)
    return px, pixel_bytes, lambda: pipeline.save(doc, reg)


def test_bounded_write_refuses_oversized_result(tmp_path) -> None:
    px, pixel_bytes, save = _save_slice_with_stub(tmp_path, packed=bytes(65))
    with pytest.raises(PipelineError) as excinfo:
        save()
    assert excinfo.value.stage == Stage.WRITE
    assert px.read_bytes() == pixel_bytes  # nothing partial written


def test_bounded_write_accepts_exact_fit(tmp_path) -> None:
    px, pixel_bytes, save = _save_slice_with_stub(tmp_path, packed=b"\xab" * 64)
    save()
    out = px.read_bytes()
    assert out[32:96] == b"\xab" * 64
    assert out[:32] == pixel_bytes[:32] and out[96:] == pixel_bytes[96:]


def test_bounded_write_leaves_slot_tail_on_short_result(tmp_path) -> None:
    px, pixel_bytes, save = _save_slice_with_stub(tmp_path, packed=b"\xab" * 10)
    save()
    out = px.read_bytes()
    assert out[32:42] == b"\xab" * 10
    assert out[42:96] == pixel_bytes[42:96]  # stale tail deliberately untouched


def test_pixel_write_optional(tmp_path) -> None:
    reg = default_registry()
    px, pl, pixel_bytes, _ = _make_files(tmp_path)
    pixel_cfg, palette_cfg = _configs(px, pl)
    pixel_cfg.write_enabled = False
    doc = pipeline.load(pixel_cfg, palette_cfg, reg)
    doc.pixel_data = bytes(len(doc.pixel_data))  # zero it; save must not land
    pipeline.save(doc, reg)
    assert px.read_bytes() == pixel_bytes


def test_missing_source_file_hard_stops(tmp_path) -> None:
    reg = default_registry()
    pixel_cfg = PathwayConfig(
        source=FileRef(str(tmp_path / "nope.sfc")),
        interpret_preset_id="preset.pixel.snes-4bpp",
    )
    palette_cfg = PathwayConfig(
        source=FileRef(str(tmp_path / "nope.pal")),
        interpret_preset_id="preset.palette.bgr555",
    )
    with pytest.raises(PipelineError) as excinfo:
        pipeline.load(pixel_cfg, palette_cfg, reg)
    assert excinfo.value.stage == Stage.READ
