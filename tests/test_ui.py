"""UI wiring: the render bridge produces correct pixels and Open renders."""

from __future__ import annotations

from celpix.core.index_grid import IndexGrid
from celpix.core.palette import Palette
from celpix.ui import render_bridge
from celpix.ui.main_window import MainWindow


def test_render_bridge_maps_indices_to_palette(qtbot) -> None:
    grid = IndexGrid(2, 1, bytearray([1, 0]))
    palette = Palette([0xFF000000, 0xFFFF0000])  # black, red
    image = render_bridge.render(grid, palette)
    assert (image.width(), image.height()) == (2, 1)
    assert image.pixel(0, 0) & 0xFFFFFFFF == 0xFFFF0000  # red
    assert image.pixel(1, 0) & 0xFFFFFFFF == 0xFF000000  # black


def test_render_bridge_subpalette_offset(qtbot) -> None:
    grid = IndexGrid(1, 1, bytearray([0]))
    palette = Palette([0xFF111111, 0xFF222222])
    # base=1 shifts index 0 to palette entry 1.
    image = render_bridge.render(grid, palette, subpalette_base=1)
    assert image.pixel(0, 0) & 0xFFFFFFFF == 0xFF222222


def test_render_bridge_empty_grid_is_null(qtbot) -> None:
    assert render_bridge.render(IndexGrid(0, 0), Palette([])).isNull()


def _make_snes_file(tmp_path):
    px = tmp_path / "s.4bpp.sfc"
    px.write_bytes(bytes((i * 13 + 1) & 0xFF for i in range(32 * 8)))  # 8 tiles
    return px


def test_open_pixel_renders(qtbot, tmp_path, monkeypatch) -> None:
    from PySide6.QtWidgets import QFileDialog

    px = _make_snes_file(tmp_path)
    monkeypatch.setattr(
        QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(px), ""))
    )
    window = MainWindow()
    qtbot.addWidget(window)

    window._open_pixel()
    assert window._doc is not None
    assert len(window._doc.pixel_tiles) == 8
    assert not window._canvas._image.isNull()
    # Grayscale fallback until a palette file is opened.
    assert not window._has_palette_file


def test_open_palette_applies_colors(qtbot, tmp_path, monkeypatch) -> None:
    from PySide6.QtWidgets import QFileDialog

    px = _make_snes_file(tmp_path)
    pl = tmp_path / "s.4bpp.sfc.pal"
    pl.write_bytes(bytes((i * 7 + 2) & 0xFF for i in range(2 * 16)))

    monkeypatch.setattr(
        QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(px), ""))
    )
    window = MainWindow()
    qtbot.addWidget(window)
    window._open_pixel()

    monkeypatch.setattr(
        QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(pl), ""))
    )
    window._open_palette()
    assert window._has_palette_file
    assert len(window._doc.palette) == 16


def _write_planar_preset(dirpath, bpp: int) -> None:
    # One 8x8 planar preset at the given bpp (bytes/tile = 8*bpp). Geometry is the
    # engine's fixed unit, so a preset is only bpp + plane offsets.
    planes = {
        1: "[ { base = 0, stride = 1 } ]",
        2: "[ { base = 0, stride = 1 }, { base = 8, stride = 1 } ]",
    }[bpp]
    (dirpath / "custom.toml").write_text(
        "id = 'preset.pixel.custom'\n"
        "name = 'Custom'\n"
        "stage = 'interpret-pixel'\n"
        "engine_id = 'codec.planar'\n"
        "[params]\n"
        f"bpp = {bpp}\n"
        f"planes = {planes}\n"
    )


def test_refresh_reloads_edited_preset_and_reruns(qtbot, tmp_path, monkeypatch) -> None:
    from PySide6.QtWidgets import QFileDialog

    from celpix.plugins.discovery import load_user_plugins
    from celpix.plugins.registry import default_registry

    plugdir = tmp_path / "plugins"
    plugdir.mkdir()
    _write_planar_preset(plugdir, bpp=1)  # 8 bytes/tile
    data_file = tmp_path / "d.bin"
    data_file.write_bytes(bytes(64))  # 64 bytes

    def reload():
        reg = default_registry()
        return reg, load_user_plugins(reg, [str(plugdir)])

    registry, _ = reload()
    monkeypatch.setattr(
        QFileDialog,
        "getOpenFileName",
        staticmethod(lambda *a, **k: (str(data_file), "")),
    )
    window = MainWindow(registry=registry, reload_plugins=reload)
    qtbot.addWidget(window)

    # Select the dropped preset and open: 64 bytes / 8 bytes-per-tile = 8 tiles.
    window._pixel_preset.setCurrentIndex(
        window._pixel_preset.findData("preset.pixel.custom")
    )
    window._open_pixel()
    assert len(window._doc.pixel_tiles) == 8

    # Edit the preset on disk (bpp 1 -> 2, so 16 bytes/tile) and refresh: the open
    # file is re-decoded through the reloaded preset. 64 / 16 = 4 tiles.
    _write_planar_preset(plugdir, bpp=2)
    window._refresh_plugins()
    assert len(window._doc.pixel_tiles) == 4
