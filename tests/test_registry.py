"""The registry loads every built-in plugin and preset."""

from __future__ import annotations

import pytest

from celpix.core.errors import Stage
from celpix.plugins.registry import default_registry


def test_stage_plugins_present() -> None:
    reg = default_registry()
    assert reg.plugin(Stage.READ, "read.raw-file").info.name
    assert reg.plugin(Stage.WRITE, "write.raw-file").info.name
    assert reg.plugin(Stage.DECOMPRESS, "decompress.none")
    assert reg.plugin(Stage.COMPRESS, "compress.none")
    assert reg.plugin(Stage.INTERPRET_PIXEL, "codec.planar")
    assert reg.plugin(Stage.INTERPRET_PALETTE, "codec.color-mask")


def test_presets_loaded_from_resources() -> None:
    reg = default_registry()
    pixel_ids = {p.id for p in reg.presets(Stage.INTERPRET_PIXEL)}
    palette_ids = {p.id for p in reg.presets(Stage.INTERPRET_PALETTE)}
    assert {
        "preset.pixel.gb-2bpp",
        "preset.pixel.snes-4bpp",
        "preset.pixel.nes-2bpp",
    } <= pixel_ids
    assert {"preset.palette.bgr555", "preset.palette.rgb888"} <= palette_ids


def test_unknown_lookup_raises() -> None:
    reg = default_registry()
    with pytest.raises(KeyError):
        reg.plugin(Stage.READ, "nope")
    with pytest.raises(KeyError):
        reg.preset("nope")


def test_duplicate_registration_rejected() -> None:
    reg = default_registry()
    with pytest.raises(ValueError):
        reg.register(reg.plugin(Stage.READ, "read.raw-file"))
