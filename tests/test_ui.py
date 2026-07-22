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


def test_render_bridge_argb_grid(qtbot) -> None:
    # Direct-colour grids render straight to ARGB32, ignoring the palette.
    from celpix.core.argb_grid import ArgbGrid

    grid = ArgbGrid(2, 1)
    grid.set(0, 0, 0xFF112233)
    grid.set(1, 0, 0xFF445566)
    image = render_bridge.render(grid, Palette([]))
    assert (image.width(), image.height()) == (2, 1)
    assert image.pixel(0, 0) & 0xFFFFFFFF == 0xFF112233
    assert image.pixel(1, 0) & 0xFFFFFFFF == 0xFF445566


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


def test_multi_drop_adds_entries_and_switching_restores_state(qtbot, tmp_path) -> None:
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QDropEvent

    a = tmp_path / "a.4bpp.sfc"
    a.write_bytes(bytes((i * 13 + 1) & 0xFF for i in range(32 * 64)))  # 64 tiles
    b = tmp_path / "b.4bpp.sfc"
    b.write_bytes(bytes((i * 7 + 3) & 0xFF for i in range(32 * 8)))  # 8 tiles
    window = MainWindow()
    qtbot.addWidget(window)

    mime = _drag_payload(a, b)
    event = QDropEvent(
        QPointF(10, 10),
        Qt.DropAction.CopyAction,
        mime,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    window.dropEvent(event)
    # Both files became entries; the last dropped one is on screen.
    entries = window._workspace.entries
    assert [e.name for e in entries] == ["a.4bpp.sfc", "b.4bpp.sfc"]
    assert window._workspace.current is entries[1]
    assert window._doc.tile_count == 8

    # Give each entry distinct state: shrink b's window so its 8 tiles can
    # scroll, move its view, then switch to a and change its pixel preset.
    window._columns.setValue(4)
    window._rows.setValue(1)
    window._nav_rows(1)
    offset_b = window._offset
    assert offset_b > 0
    window._activate_entry(entries[0])
    assert window._doc.tile_count == 64
    assert window._offset == 0  # a starts at the top, not at b's position
    window._pixel_preset.setCurrentIndex(
        window._pixel_preset.findData("preset.pixel.nes-2bpp")
    )

    # Switching back and forth restores each entry's own offset and preset.
    window._activate_entry(entries[1])
    assert window._offset == offset_b
    assert window._pixel_preset.currentData() == "preset.pixel.snes-4bpp"
    window._activate_entry(entries[0])
    assert window._pixel_preset.currentData() == "preset.pixel.nes-2bpp"


def test_slice_entry_views_bounded_region_with_absolute_addresses(
    qtbot, tmp_path
) -> None:
    px = _make_snes_file(tmp_path)  # 8 tiles of 32 bytes
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(px))

    entry = window._workspace.add_slice(str(px), "gfx", 64, 64)  # tiles 2..3
    window._activate_entry(entry)
    assert window._doc.tile_count == 2
    # A raw slice displays parent-file-absolute addresses, so its first tile
    # reads as the slice offset — and the header skip is a whole-file setting.
    assert window._offset_text() == "0x000040"
    assert not window._headered.isEnabled()
    assert window._write_action.isEnabled()

    # Switching back to the parent shows the whole file from its own state.
    window._activate_entry(window._workspace.entries[0])
    assert window._doc.tile_count == 8
    assert window._headered.isEnabled()


def test_slice_rename_inline_editor_commits_and_cancels(qtbot, tmp_path) -> None:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QLineEdit

    px = _make_snes_file(tmp_path)
    window = MainWindow()
    qtbot.addWidget(window)
    window.show()  # a hidden view won't enter item-editing state
    window._load_pixel(str(px))
    entry = window._workspace.add_slice(str(px), "0x000040 (0x40)", 64, 64)
    window._activate_entry(entry)
    panel = window._files_panel

    # Commit: the entry, its label, and the window title all take the new name.
    # The delegate commits Return via a queued invocation — wait, don't assert
    # synchronously.
    panel._begin_rename(entry)
    editor = panel._tree.findChild(QLineEdit)
    assert editor is not None
    editor.setText("yoshi gfx")
    qtbot.keyClick(editor, Qt.Key.Key_Return)
    qtbot.waitUntil(lambda: entry.name == "yoshi gfx")
    assert panel._items[entry].text(0) == "yoshi gfx"
    assert window.windowTitle() == "Celpix — yoshi gfx"

    # Cancel (Escape): nothing changes and the label is restored. The first
    # editor may still await deleteLater — take the newest one.
    panel._begin_rename(entry)
    editor = panel._tree.findChildren(QLineEdit)[-1]
    editor.setText("discarded")
    qtbot.keyClick(editor, Qt.Key.Key_Escape)
    qtbot.waitUntil(lambda: panel._editing is None)
    assert entry.name == "yoshi gfx"
    assert panel._items[entry].text(0) == "yoshi gfx"

    # Files are not renameable — their name is the on-disk basename.
    panel._begin_rename(window._workspace.entries[0])
    assert panel._editing is None


def test_slice_offset_palette_reads_parent_file_absolute(qtbot, tmp_path) -> None:
    # BGR555 white at absolute offset 32 — *before* the slice, so a successful
    # read proves the offset is parent-file-absolute, not slice-relative.
    data = bytearray(bytes((i * 13 + 1) & 0xFF for i in range(32 * 8)))
    data[32:34] = b"\xff\x7f"
    px = tmp_path / "p.4bpp.sfc"
    px.write_bytes(bytes(data))
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(px))
    slice_entry = window._workspace.add_slice(str(px), "gfx", 64, 64)
    window._activate_entry(slice_entry)

    assert window._load_palette_at_offset(32)
    doc = window._doc
    assert doc.palette.colors[0] == 0xFFFFFFFF
    assert doc.palette_config.source.path == str(px)  # the parent file
    assert doc.palette_config.source.offset == 32
    assert doc.palette_config.write_enabled is False


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


def test_typing_hex_offset_jumps_byte_exact(qtbot, tmp_path, monkeypatch) -> None:
    # Integration of the offset box with the window: commit -> jump -> normalised
    # display. Byte-exact: a sub-tile address becomes the grid's byte nudge. The
    # hex-form variants (0x/bare/$) are covered by the test_address unit tests;
    # the jump path is form-independent.
    window = _open_big(qtbot, tmp_path, monkeypatch, tiles=64)
    window._columns.setValue(16)
    window._rows.setValue(2)
    window._offset_edit.setText("0x210")  # tile 16 plus a 16-byte nudge
    window._offset_edit.commit()
    assert (window._offset, window._nudge) == (16, 16)
    assert window._offset_edit.text() == "0x000210"  # normalised, byte-exact
    # Past the end clamps to the last full page, which sits on the tile grid.
    window._offset_edit.setText("0xFFFF")
    window._offset_edit.commit()
    assert (window._offset, window._nudge) == (32, 0)
    assert window._offset_edit.text() == "0x000400"


def test_byte_nudge_steps_wrap_and_clamp(qtbot, tmp_path, monkeypatch) -> None:
    window = _open_big(qtbot, tmp_path, monkeypatch, tiles=64)
    window._columns.setValue(16)
    window._rows.setValue(2)

    window._nav_bytes(1)
    assert (window._offset, window._nudge) == (0, 1)
    assert window._nudge_info.text() == "+1 B"
    assert window._offset_edit.text() == "0x000001"
    # Tile-based moves keep the nudge — it is alignment, not position.
    window._nav_rows(1)
    assert (window._offset, window._nudge) == (16, 1)
    # A byte step back wraps across the tile boundary.
    window._nav_bytes(-2)
    assert (window._offset, window._nudge) == (15, 31)
    # Home keeps the alignment; stepping below byte 0 clamps to the file start.
    window._nav_home()
    assert (window._offset, window._nudge) == (0, 31)
    window._nav_bytes(-40)
    assert (window._offset, window._nudge) == (0, 0)
    # And the origin can't nudge past the last full page.
    window._nav_end()
    window._nav_bytes(1)
    assert (window._offset, window._nudge) == (32, 0)
    # The 0B button clears the nudge without moving the tile origin.
    from PySide6.QtWidgets import QPushButton

    window._set_byte_position(16 * 32 + 5)
    assert (window._offset, window._nudge) == (16, 5)
    next(b for b in window.findChildren(QPushButton) if b.text() == "0B").click()
    assert (window._offset, window._nudge) == (16, 0)

    # Ctrl+Left/Right and 0 route to the byte actions (Ctrl passes the filter).
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtWidgets import QApplication

    monkeypatch.setattr(QApplication, "focusWidget", staticmethod(lambda: None))

    def press(key, mods=Qt.KeyboardModifier.NoModifier):
        return window._handle_nav_key(QKeyEvent(QEvent.Type.KeyPress, key, mods))

    ctrl = Qt.KeyboardModifier.ControlModifier
    assert press(Qt.Key.Key_Right, ctrl) is True
    assert (window._offset, window._nudge) == (16, 1)
    assert press(Qt.Key.Key_Left, ctrl) is True
    assert (window._offset, window._nudge) == (16, 0)
    window._nav_bytes(3)
    assert press(Qt.Key.Key_0) is True
    assert (window._offset, window._nudge) == (16, 0)
    # An unregistered Ctrl combo is not consumed (normal shortcuts still work).
    assert press(Qt.Key.Key_S, ctrl) is False


def _select_address_format(window: MainWindow, entry_id: str) -> None:
    """Pick a dropdown entry by id ('hex', 'custom', or a bank preset id)."""
    combo = window._addr_format
    combo.setCurrentIndex(
        next(
            i
            for i in range(combo.count())
            if getattr(combo.itemData(i), "id", combo.itemData(i)) == entry_id
        )
    )


def test_address_format_dropdown_switches_display_and_parse(
    qtbot, tmp_path, monkeypatch
) -> None:
    # The dropdown swaps the offset box's format/parse pair: the displayed text
    # re-renders in the new format, and typed addresses parse under it. The
    # mapping math itself is covered in test_address.
    window = _open_big(qtbot, tmp_path, monkeypatch, tiles=64)
    window._columns.setValue(16)
    window._rows.setValue(2)
    window._nav_rows(1)  # byte 0x200

    _select_address_format(window, "snes-lorom")
    assert window._offset_edit.text() == "$00:8200"

    window._offset_edit.setText("$00:8400")  # byte 0x400 -> tile 32
    window._offset_edit.commit()
    assert window._offset == 32
    assert window._offset_edit.text() == "$00:8400"


def test_bank_setting_edit_diverges_to_custom(qtbot, tmp_path, monkeypatch) -> None:
    # A preset fills the bank-setting spins; hand-editing one flips the dropdown
    # to Custom (the preset no longer describes the settings), re-rendering the
    # box under the edited layout. Re-selecting the preset restores its values.
    window = _open_big(qtbot, tmp_path, monkeypatch, tiles=64)
    window._columns.setValue(16)
    window._rows.setValue(2)
    window._nav_rows(1)  # byte 0x200

    assert not window._bank_size.isEnabled()  # flat hex needs no bank settings
    _select_address_format(window, "snes-lorom")
    assert window._bank_size.isEnabled()
    assert (
        window._bank_size.value(),
        window._bank_addr.value(),
        window._bank_first.value(),
    ) == (0x8000, 0x8000, 0x00)

    window._bank_first.setValue(0x40)  # e.g. SuperFX-style bank numbering
    assert window._addr_format.currentData() == "custom"
    assert window._offset_edit.text() == "$40:8200"

    _select_address_format(window, "snes-lorom")
    assert window._bank_first.value() == 0x00
    assert window._offset_edit.text() == "$00:8200"


def test_split_preset_hides_bank_settings(qtbot, tmp_path, monkeypatch) -> None:
    # ExHiROM/ExLoROM are piecewise mappings the three-spin model can't
    # express: selecting one hides the bank settings entirely and renders
    # through the split layout; a banked preset brings the settings back.
    window = _open_big(qtbot, tmp_path, monkeypatch, tiles=64)
    _select_address_format(window, "snes-exhirom")
    assert window._bank_settings.isHidden()
    assert window._offset_edit.text() == "$C0:0000"
    _select_address_format(window, "snes-lorom")
    assert not window._bank_settings.isHidden()


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
    # engine's fixed unit, so a preset is only bpp + plane offsets. Pixel presets
    # live in the pixel/ subfolder of the plugin root (the folder gives the stage).
    planes = {
        1: "[ { base = 0, stride = 1 } ]",
        2: "[ { base = 0, stride = 1 }, { base = 8, stride = 1 } ]",
    }[bpp]
    pixel_dir = dirpath / "pixel"
    pixel_dir.mkdir(exist_ok=True)
    (pixel_dir / "custom.toml").write_text(
        "id = 'preset.pixel.custom'\n"
        "name = 'Custom'\n"
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


def test_click_selects_tile_and_selection_survives_scrolling(
    qtbot, tmp_path, monkeypatch
) -> None:
    from PySide6.QtCore import QPoint, Qt

    window = _open_big(qtbot, tmp_path, monkeypatch, tiles=64)
    window._columns.setValue(16)
    window._rows.setValue(2)

    # Tile 2 at zoom 4 spans x 64..95 in widget coords.
    assert window._zoom.value() == 4
    qtbot.mouseClick(window._canvas, Qt.MouseButton.LeftButton, pos=QPoint(65, 1))
    assert window._selected_tile == 2
    assert window._canvas._selected_span == (2, 2)
    assert window._load_selection_action.isEnabled()

    # Scrolling away hides the highlight but keeps the selection; scrolling back
    # restores it.
    window._nav_rows(1)
    assert window._selected_tile == 2
    assert window._canvas._selected_span is None
    window._nav_rows(-1)
    assert window._canvas._selected_span == (2, 2)

    # Switching to another file leaves the selection behind (a tile index from
    # one file means nothing in another); the fresh entry starts unselected.
    window._load_pixel(str(_make_snes_file(tmp_path)))
    assert window._selected_tile is None
    assert not window._load_selection_action.isEnabled()
    # Re-opening the first file is a no-op-in-place activation of its entry —
    # and switching back restores its remembered selection.
    window._open_pixel()
    assert window._selected_tile == 2


def test_click_on_blank_padding_is_ignored(qtbot, tmp_path, monkeypatch) -> None:
    # 8-tile file in a 32-slot window: slot 10 is padding past the file's end.
    window = _open_big(qtbot, tmp_path, monkeypatch, tiles=8)
    window._columns.setValue(16)
    window._rows.setValue(2)
    window._on_tiles_selected(10, 10)
    assert window._selected_tile is None


def _open_with_palette_at_tile1(qtbot, tmp_path, monkeypatch) -> MainWindow:
    """An 8-tile SNES-4bpp file whose tile 1 starts with BGR555 white."""
    from PySide6.QtWidgets import QFileDialog

    data = bytearray(bytes((i * 13 + 1) & 0xFF for i in range(32 * 8)))
    data[32:34] = b"\xff\x7f"  # BGR555 0x7FFF = white, little-endian
    px = tmp_path / "p.4bpp.sfc"
    px.write_bytes(bytes(data))
    monkeypatch.setattr(
        QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(px), ""))
    )
    window = MainWindow()
    qtbot.addWidget(window)
    window._open_pixel()
    return window


def test_load_palette_from_selection(qtbot, tmp_path, monkeypatch) -> None:
    window = _open_with_palette_at_tile1(qtbot, tmp_path, monkeypatch)
    window._on_tiles_selected(1, 1)  # byte offset 32
    window._load_palette_from_selection()

    doc = window._doc
    # 256 bytes - 32 offset = 224 bytes = 112 BGR555 entries (256-entry cap unhit).
    assert len(doc.palette) == 112
    assert doc.palette.colors[0] == 0xFFFFFFFF
    assert doc.palette_config.source.offset == 32
    assert doc.palette_config.source.length == 224
    assert doc.palette_config.write_enabled is False
    assert window._has_palette_file
    # The dock reflects the switch to Offset mode, with the offset field armed.
    assert window._palette_mode_combo.currentData() == "offset"
    assert window._palette_offset_edit.isEnabled()
    assert window._palette_offset_edit.text() == "0x000020"

    # Reloading pixels must not clobber the from-selection palette...
    window._reload_pixel()
    assert len(window._doc.palette) == 112
    # ...and Write must not claim the palette was written.
    window._write_current()
    assert "pixel + palette" not in window.statusBar().currentMessage()


def test_palette_preset_switch_refloors_from_selection_window(
    qtbot, tmp_path, monkeypatch
) -> None:
    window = _open_with_palette_at_tile1(qtbot, tmp_path, monkeypatch)
    window._on_tiles_selected(1, 1)
    window._load_palette_from_selection()

    window._palette_preset.setCurrentIndex(
        window._palette_preset.findData("preset.palette.rgb888")
    )
    doc = window._doc
    # 224 bytes floored to whole 3-byte entries = 74 entries / 222 bytes.
    assert len(doc.palette) == 74
    assert doc.palette_config.source.length == 222
    assert doc.palette_config.write_enabled is False  # still view-only


def test_palette_panel_click_maps_to_subpalette(qtbot, tmp_path, monkeypatch) -> None:
    from PySide6.QtCore import QPoint, Qt

    from celpix.ui.palette_panel import SWATCH, PalettePanel

    panel = PalettePanel()
    qtbot.addWidget(panel)
    panel.set_palette(list(range(256)))
    panel.set_active_range(8, 4)  # 2bpp: 4-entry subpalettes, a quarter-row range
    got: list[int] = []
    panel.subpalette_clicked.connect(got.append)
    # Swatch 40 = display row 2, col 8; with 4-entry subpalettes that's
    # subpalette 10 — the index space sizes the mapping, not the 16-wide display.
    qtbot.mouseClick(
        panel,
        Qt.MouseButton.LeftButton,
        pos=QPoint(8 * SWATCH + 1, 2 * SWATCH + 1),
    )
    assert got == [10]

    # Window-level wiring: the panel's signal drives the subpalette spin. Needs
    # a palette that actually has row 5 (the view clamps rows to the palette).
    window = _open_with_palette_at_tile1(qtbot, tmp_path, monkeypatch)
    window._on_tiles_selected(0, 0)
    window._load_palette_from_selection()  # 128 colours = rows 0..7
    window._palette_panel.subpalette_clicked.emit(5)
    assert window._subpalette.value() == 5


def test_palette_mode_starts_default_and_default_restores_fallback(
    qtbot, tmp_path, monkeypatch
) -> None:
    window = _open_with_palette_at_tile1(qtbot, tmp_path, monkeypatch)
    assert window._palette_mode_combo.currentData() == "default"
    assert not window._palette_offset_edit.isEnabled()

    window._on_tiles_selected(1, 1)
    window._load_palette_from_selection()
    assert window._has_palette_file

    window._palette_mode_combo.setCurrentIndex(
        window._palette_mode_combo.findData("default")
    )
    colors = window._doc.palette.colors
    assert colors[0] == 0xFF000000 and colors[1] == 0xFFFFFFFF  # fallback again
    assert not window._has_palette_file
    assert window._doc.palette_config.source.path == ""
    assert not window._palette_offset_edit.isEnabled()
    assert window._palette_offset_edit.text() == ""


def test_palette_offset_box_commit_loads_at_offset(
    qtbot, tmp_path, monkeypatch
) -> None:
    window = _open_with_palette_at_tile1(qtbot, tmp_path, monkeypatch)
    # Switching to Offset mode with no selection loads at the window's top-left
    # (byte 0 here).
    window._palette_mode_combo.setCurrentIndex(
        window._palette_mode_combo.findData("offset")
    )
    assert window._palette_mode == "offset"
    assert window._doc.palette_config.source.offset == 0

    # Typing an offset re-loads there (tile 1 starts with BGR555 white).
    window._palette_offset_edit.setText("0x20")
    window._palette_offset_edit.commit()
    assert window._doc.palette.colors[0] == 0xFFFFFFFF
    assert window._doc.palette_config.source.offset == 32
    assert window._doc.palette_config.write_enabled is False
    assert window._palette_offset_edit.text() == "0x000020"  # normalised


def test_palette_mode_file_cancel_reverts_dropdown(
    qtbot, tmp_path, monkeypatch
) -> None:
    from PySide6.QtWidgets import QFileDialog

    window = _open_with_palette_at_tile1(qtbot, tmp_path, monkeypatch)
    window._on_tiles_selected(1, 1)
    window._load_palette_from_selection()
    before = list(window._doc.palette.colors)

    monkeypatch.setattr(
        QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: ("", ""))
    )
    window._palette_mode_combo.setCurrentIndex(
        window._palette_mode_combo.findData("file")
    )
    assert window._palette_mode == "offset"
    assert window._palette_mode_combo.currentData() == "offset"
    assert window._doc.palette.colors == before


def test_palette_offset_box_follows_address_format(
    qtbot, tmp_path, monkeypatch
) -> None:
    window = _open_with_palette_at_tile1(qtbot, tmp_path, monkeypatch)
    window._on_tiles_selected(1, 1)
    window._load_palette_from_selection()
    _select_address_format(window, "snes-lorom")
    assert window._palette_offset_edit.text() == "$00:8020"


def test_palette_panel_arrows_move_selection_and_subpalette_follows(
    qtbot, tmp_path, monkeypatch
) -> None:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    from celpix.ui.palette_panel import PalettePanel

    panel = PalettePanel()
    qtbot.addWidget(panel)
    panel.set_palette(list(range(64)))  # 4 subpalettes of 16
    panel.set_active_range(16, 16)  # row 1 active
    got: list[int] = []
    panel.subpalette_clicked.connect(got.append)

    # Up/Down move the *selection* one display row; the subpalette follows it.
    # With no selection yet, movement starts from the active range's first entry.
    qtbot.keyClick(panel, Qt.Key.Key_Down)  # selects 32 -> subpalette 2
    qtbot.keyClick(panel, Qt.Key.Key_Up)  # selects 16 -> subpalette 1
    assert (panel.selected_index(), got) == (16, [2, 1])

    # No display row above/below: the selection (and its column) stays put.
    panel._select(3)
    qtbot.keyClick(panel, Qt.Key.Key_Up)
    assert panel.selected_index() == 3
    panel._select(51)
    qtbot.keyClick(panel, Qt.Key.Key_Down)
    assert panel.selected_index() == 51

    # While the panel is focused, the window's global nav filter defers to it
    # (same contract as the other arrow-consuming inputs).
    window = _open_big(qtbot, tmp_path, monkeypatch, tiles=64)
    from PySide6.QtCore import QEvent
    from PySide6.QtGui import QKeyEvent

    monkeypatch.setattr(
        QApplication, "focusWidget", staticmethod(lambda: window._palette_panel)
    )
    down = QKeyEvent(
        QEvent.Type.KeyPress, Qt.Key.Key_Down, Qt.KeyboardModifier.NoModifier
    )
    assert window._handle_nav_key(down) is False
    assert window._offset == 0


def test_p_key_loads_palette_from_selection(qtbot, tmp_path, monkeypatch) -> None:
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtWidgets import QApplication

    window = _open_with_palette_at_tile1(qtbot, tmp_path, monkeypatch)
    window._on_tiles_selected(1, 1)
    press_p = QKeyEvent(
        QEvent.Type.KeyPress, Qt.Key.Key_P, Qt.KeyboardModifier.NoModifier
    )

    # Focused text input keeps the letter (it may be typing).
    monkeypatch.setattr(
        QApplication, "focusWidget", staticmethod(lambda: window._offset_edit)
    )
    assert window._handle_nav_key(press_p) is False
    assert not window._has_palette_file

    # Otherwise P triggers Palette > Load from Selection.
    monkeypatch.setattr(
        QApplication, "focusWidget", staticmethod(lambda: window._canvas)
    )
    assert window._handle_nav_key(press_p) is True
    assert window._palette_mode == "offset"
    assert window._doc.palette.colors[0] == 0xFFFFFFFF


def test_palette_panel_color_selection_click_and_arrows(qtbot) -> None:
    from PySide6.QtCore import QPoint, Qt

    from celpix.ui.palette_panel import SWATCH, PalettePanel

    panel = PalettePanel()
    qtbot.addWidget(panel)
    panel.set_palette(list(range(32)))  # two rows of 16
    panel.set_active_range(16, 16)
    picked: list[int] = []
    panel.color_selected.connect(picked.append)

    # Click selects the colour (and still selects its subpalette — separate signal).
    qtbot.mouseClick(
        panel, Qt.MouseButton.LeftButton, pos=QPoint(3 * SWATCH + 1, SWATCH + 1)
    )
    assert panel.selected_index() == 19
    assert picked == [19]

    # Left/Right move the selection freely across rows, capped only at the
    # palette's ends.
    qtbot.keyClick(panel, Qt.Key.Key_Right)
    qtbot.keyClick(panel, Qt.Key.Key_Left)
    qtbot.keyClick(panel, Qt.Key.Key_Left)
    assert picked == [19, 20, 19, 18]
    panel._select(16)
    qtbot.keyClick(panel, Qt.Key.Key_Left)  # crosses into the previous row
    assert panel.selected_index() == 15
    panel._select(0)
    qtbot.keyClick(panel, Qt.Key.Key_Left)  # palette start: no change
    assert panel.selected_index() == 0
    panel._select(31)
    qtbot.keyClick(panel, Qt.Key.Key_Right)  # palette end: no change
    assert panel.selected_index() == 31

    # (Up/Down movement + the subpalette following the selection are covered by
    # test_palette_panel_arrows_move_selection_and_subpalette_follows.)

    # With no selection, Right starts from the active subpalette's first entry.
    fresh = PalettePanel()
    qtbot.addWidget(fresh)
    fresh.set_palette(list(range(32)))
    fresh.set_active_range(16, 16)
    qtbot.keyClick(fresh, Qt.Key.Key_Right)
    assert fresh.selected_index() == 17

    # A shrunken palette clamps a stranded selection back inside (or clears it
    # when nothing is left).
    panel.set_palette(list(range(8)))
    assert panel.selected_index() == 7
    panel.set_palette([])
    assert panel.selected_index() is None


def test_mode_switch_resets_row_and_selection_into_palette(
    qtbot, tmp_path, monkeypatch
) -> None:
    window = _open_with_palette_at_tile1(qtbot, tmp_path, monkeypatch)
    window._on_tiles_selected(0, 0)
    window._load_palette_from_selection()  # 128 colours = subpalette rows 0..7
    window._subpalette.setValue(6)
    window._palette_panel._select(100)
    assert window._doc.view.subpalette_row == 6

    # Back to Default: 16 fallback colours = one row. Row and colour selection
    # both land back inside the palette.
    window._palette_mode_combo.setCurrentIndex(
        window._palette_mode_combo.findData("default")
    )
    assert window._subpalette.value() == 0
    assert window._doc.view.subpalette_row == 0
    assert window._palette_panel.selected_index() == 15
    assert "Subpal 0 · Colour 15" in window._color_details.text()


def test_pixel_mode_switch_reanchors_subpalette_on_selection(
    qtbot, tmp_path, monkeypatch
) -> None:
    # The subpalette row index is relative to the format's colour count, so a
    # preset switch recomputes it from the selected colour: entry 20 is row 1
    # under 4bpp (16-entry rows) but row 5 under 2bpp (4-entry rows).
    window = _open_with_palette_at_tile1(qtbot, tmp_path, monkeypatch)
    window._on_tiles_selected(0, 0)
    window._load_palette_from_selection()  # 128 colours
    window._palette_panel._select(20)
    window._subpalette.setValue(1)
    window._pixel_preset.setCurrentIndex(
        window._pixel_preset.findData("preset.pixel.gb-2bpp")
    )
    assert window._subpalette.value() == 5
    window._pixel_preset.setCurrentIndex(
        window._pixel_preset.findData("preset.pixel.snes-4bpp")
    )
    assert window._subpalette.value() == 1

    # Without a colour selection the old base anchors instead, so the view
    # keeps showing the same palette region.
    window._palette_panel.set_palette([])  # drops the selection
    window._palette_panel.set_palette(window._doc.palette.colors)
    window._subpalette.setValue(2)  # base 32 under 4bpp
    window._pixel_preset.setCurrentIndex(
        window._pixel_preset.findData("preset.pixel.gb-2bpp")
    )
    assert window._subpalette.value() == 8  # base 32 under 2bpp


def test_color_details_show_selected_color(qtbot, tmp_path, monkeypatch) -> None:
    window = _open_with_palette_at_tile1(qtbot, tmp_path, monkeypatch)
    assert window._color_details.text() == "No colour selected"

    # Fallback palette entry 1 is white; selecting it fills the readout. The
    # position reads as subpalette + colour-within-it (4bpp: 16-entry rows).
    window._palette_panel._select(1)
    assert "#FFFFFFFF" in window._color_details.text()
    assert "Subpal 0 · Colour 1 ($1)" in window._color_details.text()
    assert "R 255  G 255  B 255  A 255" in window._color_details.text()

    # A palette reload recolours the same index; the readout follows on refresh.
    window._on_tiles_selected(1, 1)
    window._load_palette_from_selection()
    assert "#FFFFFFFF" not in window._color_details.text()  # index 1 changed


def test_compression_overlay_shows_and_hides(qtbot, tmp_path) -> None:
    from celpix.plugins.builtins import lz_command

    # The file is an LZ2 structure (4 SNES 4bpp tiles) followed by trailing
    # bytes: the main view keeps showing the raw file; the overlay shows the
    # decompressed tiles for the current window.
    tiles = bytes((i * 29 + 5) & 0xFF for i in range(32 * 4))
    packed = lz_command.compress(tiles, big_endian_offsets=True)
    px = tmp_path / "packed.bin"
    px.write_bytes(packed + bytes(64))

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(px))
    assert not window._overlay.isVisible()
    raw_image = window._canvas._image.copy()

    window._compression.setCurrentIndex(window._compression.findData("decompress.lz2"))
    assert window._overlay.isVisible()
    assert not window._overlay._canvas._image.isNull()
    # The parallel run leaves the main (raw) view untouched.
    assert window._canvas._image == raw_image
    assert window._doc.pixel_config.decompress_id == "decompress.none"

    window._compression.setCurrentIndex(window._compression.findData("decompress.none"))
    assert not window._overlay.isVisible()


def test_compression_overlay_hides_on_invalid_data(qtbot, tmp_path) -> None:
    # A leading backreference into unwritten output can never start a valid
    # structure, so no compression scheme should claim this window.
    px = tmp_path / "junk.bin"
    px.write_bytes(b"\x83\xff\xff" * 22)

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(px))
    window._compression.setCurrentIndex(window._compression.findData("decompress.lz2"))
    assert not window._overlay.isVisible()


def test_header_skip_shifts_view_and_offsets(qtbot, tmp_path) -> None:
    header = bytes(range(16))
    body = bytes((i * 13 + 1) & 0xFF for i in range(32 * 8))
    px = tmp_path / "rom.sfc"
    px.write_bytes(header + body)

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(px))
    assert bytes(window._doc.pixel_data[:16]) == header  # unchecked: raw file

    window._header_len.setValue(16)  # no re-render while unchecked
    window._headered.setChecked(True)
    assert window._doc.pixel_config.source.offset == 16
    assert bytes(window._doc.pixel_data) == body
    assert window._doc.tile_count == 8

    window._header_len.setValue(32)  # a length edit re-applies while checked
    assert bytes(window._doc.pixel_data) == body[16:]

    window._headered.setChecked(False)  # unchecking restores the full file
    assert bytes(window._doc.pixel_data) == header + body


def test_jump_and_scan_navigate_structures(qtbot, tmp_path) -> None:
    from celpix.plugins.builtins import lz_command

    # Structure A, then a junk region no scheme accepts (backrefs into nothing
    # interleaved with empty structures), then structure B, then padding.
    tiles_a = bytes((i * 29 + 5) & 0xFF for i in range(32 * 4))
    tiles_b = bytes((i * 31 + 7) & 0xFF for i in range(32 * 4))
    packed_a = lz_command.compress(tiles_a, big_endian_offsets=True)
    packed_b = lz_command.compress(tiles_b, big_endian_offsets=True)
    junk = (b"\x83\xff\xff" * 40)[:120]
    px = tmp_path / "packed2.bin"
    px.write_bytes(packed_a + junk + packed_b + bytes(512))

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(px))
    # A 4x4 page: big enough (512 B) to hold a whole structure — Jump needs the
    # end in view — yet small enough that late byte positions aren't clamped.
    window._columns.setValue(4)
    window._rows.setValue(4)
    assert not window._scan_button.isEnabled()  # compression off
    assert not window._jump_next.isEnabled()

    window._compression.setCurrentIndex(window._compression.findData("decompress.lz2"))
    assert window._scan_button.isEnabled()
    assert window._jump_next.isEnabled()  # whole structure A (known end) in view
    window._on_jump_next()
    assert window._byte_position() == len(packed_a)
    # The junk region doesn't decompress: overlay hides, Jump disarms.
    assert not window._overlay.isVisible()
    assert not window._jump_next.isEnabled()

    window._on_scan()  # synchronous; walks the junk and lands on structure B
    assert window._byte_position() == len(packed_a) + len(junk)
    assert window._overlay.isVisible()
    assert window._jump_next.isEnabled()
    assert window._scan_button.text() == "Scan"  # restored after the run


def test_new_slice_from_view_prefills_viewport_extent(
    qtbot, tmp_path, monkeypatch
) -> None:
    from celpix.ui.slice_dialog import SliceDialog

    px = _make_snes_file(tmp_path)  # 8 tiles of 32 B
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(px))
    window._columns.setValue(4)
    window._rows.setValue(1)
    window._nav_rows(1)  # view starts at tile 4 = byte 128
    captured: dict = {}
    monkeypatch.setattr(
        SliceDialog,
        "get_slice",
        staticmethod(lambda *_args, **kwargs: captured.update(kwargs)),  # "cancel"
    )
    window._new_slice_from_view()
    assert (captured["offset"], captured["length"]) == (128, 128)

    # A window bigger than the data clamps the prefill to the bytes that exist.
    small = tmp_path / "small.4bpp.sfc"
    small.write_bytes(bytes(32 * 6))
    window._load_pixel(str(small))
    window._columns.setValue(4)
    window._rows.setValue(2)  # page = 8 tiles > the 6-tile file
    captured.clear()
    window._new_slice_from_view()
    assert (captured["offset"], captured["length"]) == (0, 192)


def test_drag_selects_range_and_new_slice_from_selection(
    qtbot, tmp_path, monkeypatch
) -> None:
    from celpix.ui.slice_dialog import SliceDialog

    px = _make_snes_file(tmp_path)  # 8 tiles of 32 B
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(px))
    window._columns.setValue(4)
    window._rows.setValue(2)  # all 8 tiles in view

    window._on_tiles_selected(1, 5)  # a drag spanning slots 1..5
    assert (window._selected_tile, window._selected_last) == (1, 5)
    assert window._canvas._selected_span == (1, 5)
    assert window._new_slice_from_selection_action.isEnabled()
    # A drag reaching into blank padding clamps to the tiles that exist; the
    # anchor order doesn't matter.
    window._on_tiles_selected(12, 6)
    assert (window._selected_tile, window._selected_last) == (6, 7)

    window._on_tiles_selected(1, 5)
    captured: dict = {}
    monkeypatch.setattr(
        SliceDialog,
        "get_slice",
        staticmethod(lambda *_args, **kwargs: captured.update(kwargs)),  # "cancel"
    )
    window._new_slice_from_selection()
    assert (captured["offset"], captured["length"]) == (32, 160)  # tiles 1..5


def test_remove_entry_always_confirms(qtbot, tmp_path, monkeypatch) -> None:
    from PySide6.QtWidgets import QMessageBox

    px = _make_snes_file(tmp_path)
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(px))
    entry = window._workspace.entries[0]

    monkeypatch.setattr(
        QMessageBox, "question", lambda *_a, **_k: QMessageBox.StandardButton.No
    )
    window._remove_entry(entry)
    assert window._workspace.entries == [entry]  # declining keeps it

    monkeypatch.setattr(
        QMessageBox, "question", lambda *_a, **_k: QMessageBox.StandardButton.Yes
    )
    # Through the panel's Delete-shortcut slot, so the wiring is covered too.
    window._files_panel._remove_current()
    assert window._workspace.entries == []


def test_edit_slice_updates_coordinates_and_reloads(
    qtbot, tmp_path, monkeypatch
) -> None:
    from celpix.ui.slice_dialog import SliceDialog, SliceParams

    px = _make_snes_file(tmp_path)
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(px))
    entry = window._workspace.add_slice(str(px), "gfx", 64, 64)  # tiles 2..3
    window._activate_entry(entry)
    assert window._doc.tile_count == 2

    monkeypatch.setattr(
        SliceDialog,
        "get_slice",
        staticmethod(
            lambda *_a, **_k: SliceParams("bigger", 32, 96, "decompress.none")
        ),
    )
    window._edit_slice(entry)
    assert (entry.name, entry.slice_offset, entry.slice_length) == ("bigger", 32, 96)
    # The on-screen slice re-read the new region immediately.
    assert window._doc is entry.doc
    assert window._doc.tile_count == 3
    assert window._offset_text() == "0x000020"


def test_project_save_and_load_restores_session(qtbot, tmp_path) -> None:
    px = _make_snes_file(tmp_path)  # 8 tiles
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(px))
    window._columns.setValue(4)
    window._rows.setValue(1)
    window._zoom.setValue(2)
    window._nav_rows(1)
    assert window._offset == 4
    window._load_palette_at_offset(0x20)  # palette out of the pixel file
    saved_palette = window._doc.palette
    sliced = window._workspace.add_slice(str(px), "tail", 0xC0, 0x40)
    window._activate_entry(sliced)

    project = tmp_path / "session.celpix"
    window._save_project_to(str(project))
    assert project.exists()

    other = MainWindow()
    qtbot.addWidget(other)
    other._load_project(str(project))
    entries = other._workspace.entries
    assert [e.name for e in entries] == ["s.4bpp.sfc", "tail"]
    # The saved current entry (the slice) is active; the other stays lazy.
    assert other._workspace.current is entries[1]
    assert other._doc is not None and other._doc.tile_count == 2
    assert entries[0].doc is None

    other._activate_entry(entries[0])
    assert other._doc.tile_count == 8
    assert (other._columns.value(), other._rows.value()) == (4, 1)
    assert other._zoom.value() == 2
    assert other._offset == 4
    assert other._palette_mode == "offset"
    assert other._doc.palette == saved_palette
