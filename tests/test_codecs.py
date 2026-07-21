"""Codec engines: orientation known-vectors + encode/decode round trips."""

from __future__ import annotations

import pytest

from celpix.core.context import PipelineContext
from celpix.core.palette import Palette
from celpix.plugins.registry import default_registry

PIXEL_PRESETS = [
    "preset.pixel.gb-2bpp",
    "preset.pixel.snes-4bpp",
    "preset.pixel.nes-2bpp",
]
PALETTE_PRESETS = ["preset.palette.bgr555", "preset.palette.rgb888"]


def _pixel_engine(reg, preset_id):
    preset = reg.preset(preset_id)
    from celpix.core.errors import Stage

    return reg.plugin(Stage.INTERPRET_PIXEL, preset.engine_id), preset.params


def _color_engine(reg, preset_id):
    preset = reg.preset(preset_id)
    from celpix.core.errors import Stage

    return reg.plugin(Stage.INTERPRET_PALETTE, preset.engine_id), preset.params


@pytest.mark.parametrize("preset_id", PIXEL_PRESETS)
def test_planar_plane_maps_to_bit(preset_id: str) -> None:
    """Setting only plane k's MSB at row 0 must light bit k of pixel (0,0).

    This pins both the plane->bit assignment and the per-plane byte offsets, which
    an encode/decode round trip alone cannot (a consistently wrong orientation
    still round-trips).
    """
    reg = default_registry()
    engine, params = _pixel_engine(reg, preset_id)
    tile_bytes = 8 * 8 * params["bpp"] // 8
    for k, plane in enumerate(params["planes"]):
        data = bytearray(tile_bytes)
        data[plane["base"]] = 0x80  # row 0 (stride*0), leftmost pixel bit
        tiles = engine.decode(bytes(data), params, PipelineContext())
        assert tiles[0].get(0, 0) == (1 << k), f"plane {k} of {preset_id}"
        # every other pixel stays 0
        assert tiles[0].get(1, 0) == 0


@pytest.mark.parametrize("preset_id", PIXEL_PRESETS)
def test_planar_round_trip(preset_id: str) -> None:
    reg = default_registry()
    engine, params = _pixel_engine(reg, preset_id)
    tile_bytes = 8 * 8 * params["bpp"] // 8
    # A few tiles of deterministic, non-trivial bytes.
    data = bytes((i * 37 + 11) & 0xFF for i in range(tile_bytes * 5))
    tiles = engine.decode(data, params, PipelineContext())
    assert len(tiles) == 5
    assert engine.encode(tiles, params, PipelineContext()) == data


def test_planar_rejects_misaligned() -> None:
    reg = default_registry()
    engine, params = _pixel_engine(reg, "preset.pixel.snes-4bpp")
    with pytest.raises(ValueError):
        engine.decode(b"\x00" * 30, params, PipelineContext())  # not a multiple of 32


def test_bgr555_known_vector() -> None:
    reg = default_registry()
    engine, params = _color_engine(reg, "preset.palette.bgr555")
    # Pure blue: B field (0x7C00) all set, LE u16 -> bytes 00 7C.
    pal = engine.decode(b"\x00\x7c", params, PipelineContext())
    assert pal.color(0) == 0xFF0000FF


def test_rgb888_known_vector() -> None:
    reg = default_registry()
    engine, params = _color_engine(reg, "preset.palette.rgb888")
    pal = engine.decode(b"\x12\x34\x56", params, PipelineContext())
    assert pal.color(0) == 0xFF123456


@pytest.mark.parametrize("preset_id", PALETTE_PRESETS)
def test_color_round_trip(preset_id: str) -> None:
    reg = default_registry()
    engine, params = _color_engine(reg, preset_id)
    size = params["bytes_per_entry"]
    raw = bytearray((i * 53 + 7) & 0xFF for i in range(size * 16))
    if preset_id == "preset.palette.bgr555":
        # Clear the unused bit 15 of each LE u16 so the round trip is byte-exact.
        for off in range(0, len(raw), 2):
            raw[off + 1] &= 0x7F
    data = bytes(raw)
    pal = engine.decode(data, params, PipelineContext())
    assert len(pal) == 16
    assert engine.encode(pal, params, PipelineContext()) == data


def test_missing_color_sentinel() -> None:
    from celpix.core.palette import MISSING_COLOR

    pal = Palette([0xFF000000])
    assert pal.color(5) == MISSING_COLOR
