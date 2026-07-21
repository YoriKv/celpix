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


def _drag_payload(*paths):
    from PySide6.QtCore import QMimeData, QUrl

    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(p)) for p in paths])
    return mime


def test_drop_opens_pixel_file(qtbot, tmp_path) -> None:
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QDropEvent

    px = _make_snes_file(tmp_path)
    window = MainWindow()
    qtbot.addWidget(window)

    # Keep `mime` referenced: QDropEvent stores only a pointer to it (the real drag
    # source owns the mime data through the drop), so a temporary would dangle.
    mime = _drag_payload(px)
    event = QDropEvent(
        QPointF(10, 10),
        Qt.DropAction.CopyAction,
        mime,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    window.dropEvent(event)
    assert event.isAccepted()
    assert window._doc is not None
    assert window._doc.tile_count == 8
    assert not window._canvas._image.isNull()


def test_drag_enter_accepts_files_and_ignores_other(qtbot, tmp_path) -> None:
    from PySide6.QtCore import QMimeData, QPoint, Qt
    from PySide6.QtGui import QDragEnterEvent

    window = MainWindow()
    qtbot.addWidget(window)

    def enter(mime):
        ev = QDragEnterEvent(
            QPoint(1, 1),
            Qt.DropAction.CopyAction,
            mime,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        window.dragEnterEvent(ev)
        return ev.isAccepted()

    text = QMimeData()
    text.setText("not a file")
    assert enter(_drag_payload(_make_snes_file(tmp_path))) is True
    assert enter(text) is False


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
    assert window._doc.tile_count == 8
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


def _make_big_snes_file(tmp_path, tiles: int):
    px = tmp_path / "big.4bpp.sfc"
    px.write_bytes(bytes((i * 13 + 1) & 0xFF for i in range(32 * tiles)))
    return px


def _open_big(qtbot, tmp_path, monkeypatch, tiles: int) -> MainWindow:
    from PySide6.QtWidgets import QFileDialog

    px = _make_big_snes_file(tmp_path, tiles)
    monkeypatch.setattr(
        QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(px), ""))
    )
    window = MainWindow()
    qtbot.addWidget(window)
    window._open_pixel()
    return window


def test_navigation_steps_by_row_and_tile(qtbot, tmp_path, monkeypatch) -> None:
    window = _open_big(qtbot, tmp_path, monkeypatch, tiles=64)
    window._columns.setValue(16)
    window._rows.setValue(2)
    assert window._offset == 0

    window._nav_rows(1)  # down one row = +columns tiles
    assert window._offset == 16
    window._nav_tiles(1)  # right one tile
    assert window._offset == 17
    window._nav_tiles(-1)
    window._nav_rows(-1)
    assert window._offset == 0


def test_navigation_clamps_to_file_bounds(qtbot, tmp_path, monkeypatch) -> None:
    window = _open_big(qtbot, tmp_path, monkeypatch, tiles=64)
    window._columns.setValue(16)
    window._rows.setValue(2)  # 32-tile page; last page top-left = 64 - 32 = 32.

    window._nav_rows(-1)  # already at top: stays put
    assert window._offset == 0
    window._nav_end()
    assert window._offset == 32
    window._nav_rows(5)  # can't scroll past the last page
    assert window._offset == 32
    window._nav_home()
    assert window._offset == 0


def test_hex_offset_box_tracks_offset(qtbot, tmp_path, monkeypatch) -> None:
    window = _open_big(qtbot, tmp_path, monkeypatch, tiles=64)
    window._columns.setValue(16)
    window._rows.setValue(2)
    window._nav_rows(1)  # +16 tiles * 32 bytes/tile = 0x200
    assert window._offset_edit.text() == "0x000200"
    assert "tile 16 / 64" in window._nav_info.text()


def test_parse_hex_offset_accepts_prefixed_and_bare() -> None:
    p = MainWindow._parse_hex_offset
    assert p("0x400") == 0x400
    assert p("400") == 0x400
    assert p("0X1fe000") == 0x1FE000
    assert p("$400") == 0x400  # ROM-hacking $ prefix
    assert p("  0x400  ") == 0x400  # surrounding whitespace tolerated
    assert p("") is None
    assert p("nonsense") is None


def test_typing_hex_offset_jumps_and_snaps(qtbot, tmp_path, monkeypatch) -> None:
    # Integration of the offset box with the window: commit -> jump -> tile-snap ->
    # normalised display. The hex-form variants (0x/bare/$) are covered by the
    # _parse_hex_offset unit test; the jump path is form-independent.
    window = _open_big(qtbot, tmp_path, monkeypatch, tiles=64)
    window._columns.setValue(16)
    window._rows.setValue(2)
    window._offset_edit.setText("0x410")  # 1040 bytes -> tile 32 (1024)
    window._offset_edit.commit()
    assert window._offset == 32
    assert window._offset_edit.text() == "0x000400"  # normalised, snapped


def test_bad_hex_offset_reverts(qtbot, tmp_path, monkeypatch) -> None:
    # Invalid input reverts the box to the current offset. The commit path is
    # focus-independent (CommittingLineEdit always re-renders), so this one case
    # covers both the focused and unfocused scenarios.
    window = _open_big(qtbot, tmp_path, monkeypatch, tiles=64)
    window._nav_rows(1)
    before = window._offset
    window._offset_edit.setText("nonsense")
    window._offset_edit.commit()
    assert window._offset == before
    assert window._offset_edit.text() == f"0x{before * 32:06X}"


def test_offset_scrollbar_jumps_and_stays_in_sync(qtbot, tmp_path, monkeypatch) -> None:
    window = _open_big(qtbot, tmp_path, monkeypatch, tiles=64)
    window._columns.setValue(16)
    window._rows.setValue(2)  # page = 32 tiles; scrollbar max = 64 - 32 = 32.
    assert window._offset_bar.maximum() == 32
    assert window._offset_bar.pageStep() == 32
    assert window._offset_bar.singleStep() == 16  # one row

    # Dragging the scrollbar moves the offset.
    window._offset_bar.setValue(20)
    assert window._offset == 20

    # Moving via keys/buttons keeps the scrollbar in step (no feedback loop).
    window._nav_home()
    assert window._offset == 0
    assert window._offset_bar.value() == 0


def test_switching_codec_preserves_byte_offset(qtbot, tmp_path, monkeypatch) -> None:
    # Opened as SNES 4bpp (32 bytes/tile); a small window leaves room to scroll.
    window = _open_big(qtbot, tmp_path, monkeypatch, tiles=64)
    window._columns.setValue(4)
    window._rows.setValue(4)
    window._set_offset(10)  # tile 10 -> byte 320 (10 * 32)
    assert window._doc.bytes_per_tile == 32
    assert window._offset == 10

    # Switch to GB 2bpp (16 bytes/tile): the file byte position (320) is preserved,
    # snapped to the new tile boundary -> tile 20.
    window._pixel_preset.setCurrentIndex(
        window._pixel_preset.findData("preset.pixel.gb-2bpp")
    )
    assert window._doc.bytes_per_tile == 16
    assert window._offset == 20  # 320 // 16


def test_nav_keys_act_unless_an_arrow_input_is_focused(
    qtbot, tmp_path, monkeypatch
) -> None:
    # Navigation keys work wherever focus is, EXCEPT when an arrow-consuming input
    # (dropdown, spin box, text field) is focused — that keeps the keys for itself.
    # focusWidget is monkeypatched because real focus delivery is environment-dependent.
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtWidgets import QApplication

    window = _open_big(qtbot, tmp_path, monkeypatch, tiles=64)
    window._columns.setValue(16)
    window._rows.setValue(2)  # page 32 -> room to scroll down one row (16 tiles)
    down = QKeyEvent(
        QEvent.Type.KeyPress, Qt.Key.Key_Down, Qt.KeyboardModifier.NoModifier
    )

    def focus_is(widget):
        monkeypatch.setattr(QApplication, "focusWidget", staticmethod(lambda: widget))

    # Arrow-consuming inputs keep the key: not handled, no navigation.
    for control in (window._pixel_preset, window._rows, window._offset_edit):
        focus_is(control)
        assert window._handle_nav_key(down) is False
        assert window._offset == 0

    # A non-input widget (the canvas) lets the key navigate.
    focus_is(window._canvas)
    assert window._handle_nav_key(down) is True
    assert window._offset == 16


def test_shift_arrow_resizes_and_reclamps(qtbot, tmp_path, monkeypatch) -> None:
    window = _open_big(qtbot, tmp_path, monkeypatch, tiles=64)
    window._columns.setValue(16)
    window._rows.setValue(2)
    window._nav_end()
    assert window._offset == 32  # last page with a 32-tile window

    # Grow the window (more rows): the page gets bigger, so the offset re-clamps.
    window._adjust_spin(window._rows, 2)  # rows 2 -> 4 = 64-tile page
    assert window._rows.value() == 4
    assert window._offset == 0  # whole file now fits in one page


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
    assert window._doc.tile_count == 8

    # Edit the preset on disk (bpp 1 -> 2, so 16 bytes/tile) and refresh: the open
    # file is re-decoded through the reloaded preset. 64 / 16 = 4 tiles.
    _write_planar_preset(plugdir, bpp=2)
    window._refresh_plugins()
    assert window._doc.tile_count == 4
