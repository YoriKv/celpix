"""UI wiring: the render bridge produces correct pixels and Open renders."""

from __future__ import annotations

import json
from pathlib import Path

from celpix.core.arrangement import ARRANGEMENT_PRESETS
from celpix.core.index_grid import IndexGrid
from celpix.core.palette import Palette
from celpix.project.workspace import PaletteMode
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
    # Direct-color grids render straight to ARGB32, ignoring the palette.
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
    # Slices never nest: a slice on screen offers no slice-creation actions.
    window._on_tiles_selected(0, 0)  # a selection can't unlock from-selection
    assert not window._new_slice_action.isEnabled()
    assert not window._new_slice_from_view_action.isEnabled()
    assert not window._new_slice_from_selection_action.isEnabled()

    # Switching back to the parent shows the whole file from its own state, and
    # the file *does* spawn slices.
    window._activate_entry(window._workspace.entries[0])
    assert window._doc.tile_count == 8
    assert window._headered.isEnabled()
    assert window._new_slice_action.isEnabled()
    assert window._new_slice_from_view_action.isEnabled()


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
    assert window.windowTitle() == "Celpix - yoshi gfx"

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


def test_file_list_children_stay_sorted_by_offset(qtbot, tmp_path, monkeypatch) -> None:
    from celpix.ui.slice_dialog import SliceDialog, SliceParams

    px = _make_snes_file(tmp_path)
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(px))
    file_entry = window._workspace.find_file(str(px))

    # Add slices/bookmarks out of offset order; the list must present them sorted.
    window._workspace.add_slice(str(px), "c", 128, 32)
    window._workspace.add_slice(str(px), "a", 32, 32)
    b = window._workspace.add_slice(str(px), "b", 64, 32)
    window._new_bookmark_for(file_entry)  # bookmark at the parked view (offset 0)

    panel = window._files_panel
    file_item = panel._items[file_entry]

    def offsets() -> list[int]:
        n = file_item.childCount()
        return [panel._offset_of(file_item.child(i)) for i in range(n)]

    assert offsets() == sorted(offsets())  # slices and bookmarks intermixed, by offset

    # Editing a slice's offset re-sorts it in place: move "b" (was 64) past the
    # 128 slice, and it should land last among the children.
    monkeypatch.setattr(
        SliceDialog,
        "get_slice",
        staticmethod(lambda *_a, **_k: SliceParams("b", 200, 32, "decompress.none")),
    )
    window._edit_slice(b)
    assert offsets() == sorted(offsets())
    assert panel._offset_of(file_item.child(file_item.childCount() - 1)) == 200


def test_arrow_key_browsing_keeps_focus_on_file_list(qtbot, tmp_path) -> None:
    px = _make_snes_file(tmp_path)
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(px))
    file_entry = window._workspace.find_file(str(px))
    slice_entry = window._workspace.add_slice(str(px), "gfx", 64, 64)

    # Spy on the canvas focus grab: it should fire for a plain activation but be
    # suppressed while the list reports it is being browsed with the arrow keys.
    focus_calls: list[int] = []
    window._canvas.setFocus = lambda *_a: focus_calls.append(1)  # type: ignore[method-assign]

    window._activate_entry(slice_entry)  # mouse/programmatic: hands focus over
    assert focus_calls == [1]

    window._files_panel._tree.key_navigating = True  # as keyPressEvent sets it
    try:
        window._activate_entry(file_entry)
    finally:
        window._files_panel._tree.key_navigating = False
    assert window._workspace.current is file_entry  # the entry still loaded
    assert focus_calls == [1]  # ...but the canvas did not steal focus


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
    # Offset palettes are edited in place: Write is armed, bounded to the
    # palette's own bytes in the parent file.
    assert doc.palette_config.write_enabled is True


def test_palette_export_writes_a_pal_and_registers_it(qtbot, tmp_path, monkeypatch):
    """An Offset palette lives buried in the pixel file; exporting is the only
    way it becomes a file of its own, and the export joins Palettes so it is
    re-applicable (and travels with the project) without a second gesture."""
    from PySide6.QtWidgets import QFileDialog

    from celpix.pipeline import pipeline
    from celpix.pipeline.pathway import PathwayConfig
    from celpix.plugins.base import FileRef
    from celpix.project.workspace import EntryKind

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_make_snes_file(tmp_path)))
    # Default is generated and File already *is* a .pal - neither can export.
    assert window._palette_mode == "default"
    assert not window._export_palette_action.isEnabled()

    assert window._load_palette_at_offset(32)
    assert window._export_palette_action.isEnabled()

    out = tmp_path / "gfx.pal"
    monkeypatch.setattr(
        QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(out), ""))
    )
    # The palette was read as BGR555; the export must ignore that (and the
    # hidden dropdown) and write plain RGB triplets.
    assert window._palette_preset_id() == "preset.palette.bgr555"
    window._export_palette_file()
    assert out.stat().st_size == 3 * len(window._doc.palette)

    # The file holds the whole palette on screen - every entry, not just the
    # ones an edit touched, as a save-back into an existing file would.
    reloaded = pipeline.load_palette(
        PathwayConfig(
            source=FileRef(str(out)),
            interpret_preset_id="preset.palette.rgb888",
        ),
        window._registry,
    )
    assert reloaded.palette.colors == window._doc.palette.colors
    palettes = [e for e in window._workspace.entries if e.kind is EntryKind.PALETTE]
    assert [e.name for e in palettes] == ["gfx.pal"]
    # Registered as what was written, so the double-click round-trips: applying
    # it decodes RGB888 and lands the very colors that were exported.
    assert palettes[0].palette_preset_id == "preset.palette.rgb888"

    # Exporting over an already-registered path re-stamps it: the entry has to
    # describe the bytes now on disk, not the file they replaced.
    palettes[0].palette_preset_id = "preset.palette.bgr555"
    window._export_palette_file()
    assert palettes[0].palette_preset_id == "preset.palette.rgb888"

    window._use_palette_entry(palettes[0])
    assert window._palette_preset_id() == "preset.palette.rgb888"
    assert window._doc.palette.colors == reloaded.palette.colors


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
    assert not window._palette_mode.is_real


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
    assert window._palette_mode.is_real
    assert len(window._doc.palette) == 16


def test_dropped_celpix_opens_as_a_project_and_claims_the_drop(qtbot, tmp_path):
    """A .celpix is a session, not an entry: it replaces the workspace, and the
    other files in the same drop are ignored rather than loaded around it."""
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QDropEvent

    px = _make_snes_file(tmp_path)
    saver = MainWindow()
    qtbot.addWidget(saver)
    saver._load_pixel(str(px))
    project = tmp_path / "session.celpix"
    saver._save_project_to(str(project))

    other = tmp_path / "other.4bpp.sfc"
    other.write_bytes(bytes(32 * 4))
    window = MainWindow()
    qtbot.addWidget(window)

    mime = _drag_payload(other, project)
    event = QDropEvent(
        QPointF(10, 10),
        Qt.DropAction.CopyAction,
        mime,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    window.dropEvent(event)

    # The project loaded, and nothing from the dropped .sfc came with it.
    # Qt hands back drop URLs with POSIX separators even on Windows, so compare
    # the file the path names rather than the spelling.
    assert window._project_path is not None
    assert Path(window._project_path).samefile(project)
    assert [e.name for e in window._workspace.entries] == [px.name]


def test_dropped_pal_becomes_a_palette_entry_applied_on_use(
    qtbot, tmp_path, monkeypatch
) -> None:
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QDropEvent

    from celpix.project.workspace import EntryKind

    px = _make_snes_file(tmp_path)
    pal = tmp_path / "colors.pal"
    pal.write_bytes(bytes((i * 7 + 2) & 0xFF for i in range(2 * 16)))  # 16 BGR555

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(px))

    # A dropped .pal registers as a PALETTE entry, not pixel data — the view
    # stays on the pixel file.
    mime = _drag_payload(pal)
    event = QDropEvent(
        QPointF(10, 10),
        Qt.DropAction.CopyAction,
        mime,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    window.dropEvent(event)
    palettes = [e for e in window._workspace.entries if e.kind is EntryKind.PALETTE]
    assert len(palettes) == 1
    palette_entry = palettes[0]
    assert palette_entry.palette_preset_id == window._palette_preset_id()
    assert not window._palette_mode.is_real  # registered, not yet applied

    # Re-dropping the same file doesn't duplicate it (path is identity).
    window.dropEvent(event)
    assert sum(e.kind is EntryKind.PALETTE for e in window._workspace.entries) == 1

    # Using it applies the colors to the current view in File mode.
    window._use_palette_entry(palette_entry)
    assert window._palette_mode.is_real
    assert window._palette_mode == "file"
    assert len(window._doc.palette) == 16


def test_palette_format_change_restamps_the_palette_entry(qtbot, tmp_path) -> None:
    """Re-picking the format while a registered .pal is on screen re-stamps the
    entry, so the next double-click decodes the way the user just chose - and
    undo takes the entry back with the palette."""
    from celpix.project.workspace import EntryKind

    px = _make_snes_file(tmp_path)
    pal = tmp_path / "colors.pal"
    pal.write_bytes(bytes((i * 7 + 2) & 0xFF for i in range(2 * 16)))  # 16 BGR555

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(px))
    window._add_palette_file(str(pal))
    entry = next(e for e in window._workspace.entries if e.kind is EntryKind.PALETTE)
    registered = entry.palette_preset_id
    window._use_palette_entry(entry)

    # Move the dropdown as a user would - same entry size, so the re-decode
    # succeeds and the commit sticks.
    other = "preset.palette.rgb565"
    assert other != registered
    window._palette_preset.setCurrentIndex(window._palette_preset.findData(other))
    assert entry.palette_preset_id == other

    # The format is a property of the entry, not of the dropdown's last
    # position: moving away and re-applying comes back to it.
    window._use_default_palette()
    window._use_palette_entry(entry)
    assert window._palette_preset_id() == other

    window._undo_stack.undo()  # back off the re-apply
    window._undo_stack.undo()  # back off the default
    window._undo_stack.undo()  # back off the format change
    assert entry.palette_preset_id == registered


def test_use_bookmark_as_palette_reads_offset_from_parent(qtbot, tmp_path) -> None:
    from celpix.project.workspace import EntryKind

    # BGR555 white at absolute offset 32 — the offset a bookmark there points at.
    data = bytearray(bytes((i * 13 + 1) & 0xFF for i in range(32 * 8)))
    data[32:34] = b"\xff\x7f"
    px = tmp_path / "p.4bpp.sfc"
    px.write_bytes(bytes(data))

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(px))
    entry = window._workspace.find_file(str(px))

    # A bookmark at byte 32 (where the white palette entry sits). Shrink the
    # window first so the view can actually scroll a tile down to it.
    window._columns.setValue(2)
    window._rows.setValue(1)
    window._set_byte_position(32)
    assert window._byte_position() == 32
    window._new_bookmark_for(entry)
    bookmark = next(
        e for e in window._workspace.entries if e.kind is EntryKind.BOOKMARK
    )
    assert bookmark.slice_offset == 32
    # Move the view away — Use as Palette must not move it back, only set colors.
    window._set_byte_position(0)
    assert window._byte_position() == 0

    window._use_bookmark_as_palette(bookmark)
    assert window._palette_mode == "offset"
    assert window._doc.palette_config.source.offset == 32
    assert window._doc.palette.colors[0] == 0xFFFFFFFF
    assert window._byte_position() == 0  # the view position is untouched


def test_offset_palette_step_buttons_move_by_one_tile(qtbot, tmp_path) -> None:
    # snes-4bpp: 32 bytes per tile. Start an offset palette at byte 32, then step.
    data = bytearray(bytes((i * 13 + 1) & 0xFF for i in range(32 * 8)))
    px = tmp_path / "p.4bpp.sfc"
    px.write_bytes(bytes(data))

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(px))
    assert window._load_palette_at_offset(32)
    assert window._doc.bytes_per_tile == 32

    # Forward one tile: +32 bytes.
    window._step_palette_offset(1)
    assert window._doc.palette_config.source.offset == 64
    # Back one tile: −32 bytes, returning to 32.
    window._step_palette_offset(-1)
    assert window._doc.palette_config.source.offset == 32
    # Stepping back past byte 0 clamps and stops there (no further movement, no
    # alert): from 32 one tile back is 0, and another is still 0.
    window._step_palette_offset(-1)
    assert window._doc.palette_config.source.offset == 0
    window._step_palette_offset(-1)
    assert window._doc.palette_config.source.offset == 0


def test_palette_dock_header_tracks_mode(qtbot, tmp_path, monkeypatch) -> None:
    window = _open_with_palette_at_tile1(qtbot, tmp_path, monkeypatch)
    # Default: neither the offset field nor the format row is shown.
    assert not window._palette_offset_edit.isVisibleTo(window)
    assert not window._palette_preset.isVisibleTo(window)
    assert not window._palette_file_label.isVisibleTo(window)

    assert not window._palette_offset_prev.isVisibleTo(window)

    # Offset mode decodes raw bytes: the offset field, its step arrows, and the
    # format row all appear.
    window._on_tiles_selected(1, 1)
    window._load_palette_from_selection()
    assert window._palette_mode == "offset"
    assert window._palette_offset_edit.isVisibleTo(window)
    assert window._palette_offset_prev.isVisibleTo(window)
    assert window._palette_offset_next.isVisibleTo(window)
    assert window._palette_preset.isVisibleTo(window)


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


def test_cycling_pixel_formats_keeps_target_offset_across_whole_bank(
    qtbot, tmp_path, monkeypatch
) -> None:
    # Cycling the pixel dropdown to eyeball formats must keep re-anchoring on the
    # position where the run started, even when an intermediate format has tiles
    # so large the view clamps back to page 0. The whole-bank 8bpp format is one
    # 16384-byte tile; a 2-bank file holds only ~2 of those, so any multi-tile
    # window collapses to offset 0 — and the sub-tile remainder it would keep is
    # NOT the position. Before the target latch, switching back landed there.
    window = _open_big(qtbot, tmp_path, monkeypatch, tiles=1024)  # 32768 B = 2 banks
    window._columns.setValue(16)
    window._rows.setValue(2)  # 32-tile page, small enough to leave room to scroll
    window._set_offset(900)  # tile 900 -> byte 28800 (900 * 32), well past bank 0
    assert window._doc.bytes_per_tile == 32
    assert window._offset == 900

    # Switch to whole-bank 8bpp (16384 B/tile). byte 28800 -> tile 1 + nudge 12416,
    # but a 16x2 page needs 32 whole-bank tiles and only 2 exist, so the offset
    # clamps to 0 — the exact case that used to lose the position. The scratch
    # target latches the true byte position so the next switch can recover it.
    window._pixel_preset.setCurrentIndex(
        window._pixel_preset.findData("preset.pixel.snes-8bpp-bank")
    )
    assert window._doc.bytes_per_tile == 16384
    assert window._offset == 0  # clamped away from the real position
    assert window._pixel_switch_target == 28800  # ...but the target remembers it

    # Switch back to 4bpp mid-run: re-anchoring on the latched 28800 (not the
    # clamped view) restores tile 900. Before the fix this read the clamped view's
    # byte 12416 and landed at tile 388 (12416 // 32).
    window._pixel_preset.setCurrentIndex(
        window._pixel_preset.findData("preset.pixel.snes-4bpp")
    )
    assert window._doc.bytes_per_tile == 32
    assert window._offset == 900  # position survived the round-trip

    # Leaving the dropdown ends the run, so a fresh switch re-anchors on the live
    # view rather than resurrecting this stale target.
    window._end_pixel_switch_run()
    assert window._pixel_switch_target is None


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
    assert window._canvas._selected_slots == {2}
    assert window._palette_from_selection_action.isEnabled()

    # Scrolling away hides the highlight but keeps the selection; scrolling back
    # restores it.
    window._nav_rows(1)
    assert window._selected_tile == 2
    assert not window._canvas._selected_slots
    window._nav_rows(-1)
    assert window._canvas._selected_slots == {2}

    # Switching to another file leaves the selection behind (a tile index from
    # one file means nothing in another); the fresh entry starts unselected.
    window._load_pixel(str(_make_snes_file(tmp_path)))
    assert window._selected_tile is None
    assert not window._palette_from_selection_action.isEnabled()
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
    assert doc.palette_config.write_enabled is True  # edited in place
    assert window._palette_mode.is_real
    # The dock reflects the switch to Offset mode, with the offset field armed.
    assert window._palette_mode_combo.currentData() == "offset"
    assert window._palette_offset_edit.isEnabled()
    assert window._palette_offset_edit.text() == "0x000020"

    # Reloading pixels must not clobber the from-selection palette...
    window._apply_pixel_config(
        window._pixel_preset_id(), window._header_offset(), window._byte_position()
    )
    assert len(window._doc.palette) == 112
    # ...and Write covers the palette too, since an Offset palette is edited in
    # place and saved back into the bytes it was read from.
    window._write_current()
    assert "pixel + palette" in window.statusBar().currentMessage()


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
    assert doc.palette_config.write_enabled is True  # still edited in place


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
    window._load_palette_from_selection()  # 128 colors = rows 0..7
    window._palette_panel.subpalette_clicked.emit(5)
    assert window._subpalette.value() == 5


def test_palette_mode_starts_default_and_default_restores_fallback(
    qtbot, tmp_path, monkeypatch
) -> None:
    window = _open_with_palette_at_tile1(qtbot, tmp_path, monkeypatch)
    assert window._palette_mode_combo.currentData() == "default"
    # The offset field is hidden outside Offset mode (isVisibleTo reports the
    # intended visibility even though this test never shows the window).
    assert not window._palette_offset_edit.isVisibleTo(window)

    window._on_tiles_selected(1, 1)
    window._load_palette_from_selection()
    assert window._palette_mode.is_real
    # Offset mode reveals the field.
    assert window._palette_offset_edit.isVisibleTo(window)

    window._palette_mode_combo.setCurrentIndex(
        window._palette_mode_combo.findData(PaletteMode.DEFAULT)
    )
    colors = window._doc.palette.colors
    assert colors[0] == 0xFF000000 and colors[1] == 0xFFFFFFFF  # fallback again
    assert not window._palette_mode.is_real
    assert window._doc.palette_config.source.path == ""
    assert not window._palette_offset_edit.isVisibleTo(window)
    assert window._palette_offset_edit.text() == ""


def _fceux_nes_state(tmp_path, first_index: int) -> object:
    """A minimal FCEUX (.fc0) NES state whose PPU palette RAM starts with
    ``first_index`` — an index into the NES 64-color master palette. Just the
    "FCSX" header and a PPU section (type 3) carrying the "PRAM" field."""
    import struct

    def u32(v):
        return struct.pack("<I", v)

    pram = bytes([first_index]) + b"\x00" * 31
    ppu = b"PRAM" + u32(len(pram)) + pram
    payload = bytes([3]) + u32(len(ppu)) + ppu
    header = b"FCSX" + u32(len(payload)) + u32(0) + u32(0xFFFFFFFF)
    state = tmp_path / "game.fc0"
    state.write_bytes(header + payload)
    return state


def _mesen_state(tmp_path) -> object:
    """A minimal Mesen (.mss) SNES state on disk: the MSS header, a dummy zlib
    screenshot and ROM name, then the zlib-compressed record stream carrying a
    ``ppu.cgram`` entry with three distinct colors. Exercises the extract path
    (parse header → zlib → read CGRAM by label) and the inline-bytes source."""
    import struct
    import zlib

    def u32(v):
        return struct.pack("<I", v)

    colors = [(0xF8, 0, 0), (0, 0xF8, 0), (0, 0, 0xF8)]  # R, G, B
    words = [((b >> 3) << 10) | ((g >> 3) << 5) | (r >> 3) for (r, g, b) in colors]
    cgram = struct.pack("<256H", *(words + [0] * (256 - len(words))))
    records = b"ppu.cgram\x00" + u32(len(cgram)) + cgram
    comp = zlib.compress(records)

    screenshot = zlib.compress(b"\x00" * 16)
    header = b"MSS" + u32(0) + u32(4) + u32(0)  # versions + console 0 (SNES)
    video = u32(16) + u32(2) + u32(2) + u32(100) + u32(len(screenshot)) + screenshot
    blob = b"\x01" + u32(len(records)) + u32(len(comp)) + comp
    state = tmp_path / "game.mss"
    state.write_bytes(header + video + u32(4) + b"game" + blob)
    return state


def test_mesen_state_extracts_snes_palette(qtbot, tmp_path, monkeypatch) -> None:
    # Mesen's palette lives inside a zlib-compressed record stream, not at a file
    # offset — this drives the inline-bytes source path through the pipeline.
    from PySide6.QtWidgets import QFileDialog

    state = _mesen_state(tmp_path)
    window = _open_with_palette_at_tile1(qtbot, tmp_path, monkeypatch)
    monkeypatch.setattr(
        QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(state), ""))
    )
    window._palette_mode_combo.setCurrentIndex(
        window._palette_mode_combo.findData(PaletteMode.EMULATOR)
    )

    assert window._palette_mode == "emulator"
    assert window._palette_preset_id() == "preset.palette.bgr555"  # SNES CGRAM
    assert window._doc.palette_config.write_enabled is False  # view-only state
    colors = window._doc.palette.colors
    assert len(colors) == 256
    # The three CGRAM colors decoded to three distinct non-black entries.
    assert len({colors[0], colors[1], colors[2]}) == 3
    window._undo_stack.undo()
    assert window._palette_mode == "default"


def test_emulator_state_loads_and_switches_codec(qtbot, tmp_path, monkeypatch) -> None:
    from PySide6.QtWidgets import QFileDialog

    # index 0x30 in the NES master table is white — an easy color to assert on.
    state = _fceux_nes_state(tmp_path, 0x30)
    window = _open_with_palette_at_tile1(qtbot, tmp_path, monkeypatch)
    assert window._palette_preset_id() == "preset.palette.bgr555"  # SNES default

    monkeypatch.setattr(
        QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(state), ""))
    )
    window._palette_mode_combo.setCurrentIndex(
        window._palette_mode_combo.findData(PaletteMode.EMULATOR)
    )

    assert window._palette_mode == "emulator"
    # The console was auto-detected, so the palette codec is now the NES table...
    assert window._palette_preset_id() == "preset.palette.nes-indexed"
    assert (
        window._doc.palette_config.interpret_preset_id == "preset.palette.nes-indexed"
    )
    # ...and the 32 index bytes decoded through it, first one white.
    assert len(window._doc.palette) == 32
    assert window._doc.palette.colors[0] == 0xFFFFFFFF
    assert window._doc.palette_config.write_enabled is False  # view-only
    assert window._palette_mode.is_real
    # Undo returns to the previous (default) palette and its codec.
    window._undo_stack.undo()
    assert window._palette_mode == "default"
    assert window._palette_preset_id() == "preset.palette.bgr555"


def test_emulator_state_unrecognised_reverts_with_message(
    qtbot, tmp_path, monkeypatch, captured_alerts
) -> None:
    from PySide6.QtWidgets import QFileDialog

    junk = tmp_path / "not.state"
    junk.write_bytes(b"random bytes, no known signature")
    window = _open_with_palette_at_tile1(qtbot, tmp_path, monkeypatch)
    monkeypatch.setattr(
        QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(junk), ""))
    )
    window._palette_mode_combo.setCurrentIndex(
        window._palette_mode_combo.findData(PaletteMode.EMULATOR)
    )
    assert window._palette_mode == "default"
    # The failure is surfaced as a modal alert, not a status line.
    assert any("Unrecognised" in message for _title, message in captured_alerts)


def test_palette_offset_failure_alerts_not_status(
    qtbot, tmp_path, monkeypatch, captured_alerts
) -> None:
    # A palette load that can't size even one entry (offset past EOF) used to
    # fail with only a status line; it now blocks with a modal so the user
    # can't miss that nothing loaded.
    window = _open_with_palette_at_tile1(qtbot, tmp_path, monkeypatch)
    assert not window._load_palette_at_offset(1 << 20)
    assert any("Not enough data" in message for _title, message in captured_alerts)


def test_emulator_state_redetects_on_restore(qtbot, tmp_path, monkeypatch) -> None:
    from PySide6.QtWidgets import QFileDialog

    from celpix.project.workspace import EntryKind

    state = _fceux_nes_state(tmp_path, 0x30)  # NES white
    window = _open_with_palette_at_tile1(qtbot, tmp_path, monkeypatch)
    entry = window._workspace.current

    monkeypatch.setattr(
        QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(state), ""))
    )
    window._palette_mode_combo.setCurrentIndex(
        window._palette_mode_combo.findData(PaletteMode.EMULATOR)
    )
    assert window._palette_mode == "emulator"

    # Bookmark the emulator-state view: the snapshot stores only the state path.
    window._new_bookmark_for(entry)
    bookmark = next(
        e for e in window._workspace.entries if e.kind is EntryKind.BOOKMARK
    )
    assert bookmark.session.palette_mode == "emulator"
    assert bookmark.pending_palette is not None and bookmark.pending_palette.path

    # Drive the parent back to the default palette, then jump: restoring must
    # re-detect the state (offset + NES codec are not stored) and re-decode it.
    window._palette_mode_combo.setCurrentIndex(
        window._palette_mode_combo.findData(PaletteMode.DEFAULT)
    )
    window._jump_to_bookmark(bookmark)
    assert window._palette_mode == "emulator"
    assert window._palette_preset_id() == "preset.palette.nes-indexed"
    assert window._doc.palette.colors[0] == 0xFFFFFFFF


def test_palette_offset_box_commit_loads_at_offset(
    qtbot, tmp_path, monkeypatch
) -> None:
    window = _open_with_palette_at_tile1(qtbot, tmp_path, monkeypatch)
    # Switching to Offset mode with no selection loads at the window's top-left
    # (byte 0 here).
    window._palette_mode_combo.setCurrentIndex(
        window._palette_mode_combo.findData(PaletteMode.OFFSET)
    )
    assert window._palette_mode == "offset"
    assert window._doc.palette_config.source.offset == 0

    # Typing an offset re-loads there (tile 1 starts with BGR555 white).
    window._palette_offset_edit.setText("0x20")
    window._palette_offset_edit.commit()
    assert window._doc.palette.colors[0] == 0xFFFFFFFF
    assert window._doc.palette_config.source.offset == 32
    assert window._doc.palette_config.write_enabled is True
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
        window._palette_mode_combo.findData(PaletteMode.FILE)
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
    assert not window._palette_mode.is_real

    # Otherwise P triggers Palette > Palette from Selection.
    monkeypatch.setattr(
        QApplication, "focusWidget", staticmethod(lambda: window._canvas)
    )
    assert window._handle_nav_key(press_p) is True
    assert window._palette_mode == "offset"
    assert window._doc.palette.colors[0] == 0xFFFFFFFF


def test_g_key_toggles_grid(qtbot, tmp_path, monkeypatch) -> None:
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtWidgets import QApplication

    window = _open_big(qtbot, tmp_path, monkeypatch, tiles=8)
    press_g = QKeyEvent(
        QEvent.Type.KeyPress, Qt.Key.Key_G, Qt.KeyboardModifier.NoModifier
    )

    # Focused text input keeps the letter (it may be typing).
    monkeypatch.setattr(
        QApplication, "focusWidget", staticmethod(lambda: window._offset_edit)
    )
    assert window._handle_nav_key(press_g) is False
    assert not window._grid.isChecked()

    # Otherwise G flips View > Grid, flowing through to the stored view state.
    monkeypatch.setattr(
        QApplication, "focusWidget", staticmethod(lambda: window._canvas)
    )
    assert window._handle_nav_key(press_g) is True
    assert window._grid.isChecked()
    assert window._doc.view.show_grid


def _isolate_settings(tmp_path) -> None:
    """Point QSettings at a throwaway INI so grid-style writes don't touch the
    user's real config, and reads are deterministic across a fresh window."""
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    QApplication.instance().setApplicationName("CelpixTest")
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(
        QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path)
    )
    QSettings().clear()


def test_grid_style_menu_applies_and_persists(qtbot, tmp_path) -> None:
    from celpix.ui.canvas import GridStyle

    _isolate_settings(tmp_path)
    window = MainWindow()
    qtbot.addWidget(window)

    dot = next(
        a for a in window._grid_style_group.actions() if a.data() is GridStyle.DOT
    )
    dot.trigger()
    assert window._canvas._grid_style is GridStyle.DOT

    # A fresh window reads the persisted style back (app-global, not per-project)
    # and reflects it in the radio group.
    reopened = MainWindow()
    qtbot.addWidget(reopened)
    assert reopened._canvas._grid_style is GridStyle.DOT
    checked = [a.data() for a in reopened._grid_style_group.actions() if a.isChecked()]
    assert checked == [GridStyle.DOT]


def test_grid_style_defaults_when_setting_is_bad(qtbot, tmp_path) -> None:
    from PySide6.QtCore import QSettings

    from celpix.ui.canvas import GridStyle

    _isolate_settings(tmp_path)
    QSettings().setValue("view/grid_style", "bogus")  # stale / foreign value
    window = MainWindow()
    qtbot.addWidget(window)
    assert window._canvas._grid_style is GridStyle.LINE


def test_palette_panel_color_selection_click_and_arrows(qtbot) -> None:
    from PySide6.QtCore import QPoint, Qt

    from celpix.ui.palette_panel import SWATCH, PalettePanel

    panel = PalettePanel()
    qtbot.addWidget(panel)
    panel.set_palette(list(range(32)))  # two rows of 16
    panel.set_active_range(16, 16)
    picked: list[int] = []
    panel.color_selected.connect(picked.append)

    # Click selects the color (and still selects its subpalette — separate signal).
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
    window._load_palette_from_selection()  # 128 colors = subpalette rows 0..7
    window._subpalette.setValue(6)
    window._palette_panel._select(100)
    assert window._doc.view.subpalette_row == 6

    # Back to Default: 16 fallback colors = one row. Row and color selection
    # both land back inside the palette.
    window._palette_mode_combo.setCurrentIndex(
        window._palette_mode_combo.findData(PaletteMode.DEFAULT)
    )
    assert window._subpalette.value() == 0
    assert window._doc.view.subpalette_row == 0
    assert window._palette_panel.selected_index() == 15
    assert "Subpal 0 · Color 15" in window._color_details.text()


def test_pixel_mode_switch_reanchors_subpalette_on_selection(
    qtbot, tmp_path, monkeypatch
) -> None:
    # The subpalette row index is relative to the format's color count, so a
    # preset switch recomputes it from the selected color: entry 20 is row 1
    # under 4bpp (16-entry rows) but row 5 under 2bpp (4-entry rows).
    window = _open_with_palette_at_tile1(qtbot, tmp_path, monkeypatch)
    window._on_tiles_selected(0, 0)
    window._load_palette_from_selection()  # 128 colors
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

    # Without a color selection the old base anchors instead, so the view
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
    assert window._color_details.text() == "No color selected"

    # Fallback palette entry 1 is white; selecting it fills the readout. The
    # position reads as subpalette + color-within-it (4bpp: 16-entry rows).
    window._palette_panel._select(1)
    assert "#FFFFFFFF" in window._color_details.text()
    assert "Subpal 0 · Color 1 ($1)" in window._color_details.text()
    assert "R 255  G 255  B 255  A 255" in window._color_details.text()

    # A palette reload recolors the same index; the readout follows on refresh.
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


def test_compression_overlay_honors_the_arrangement(qtbot, tmp_path) -> None:
    from celpix.plugins.builtins import lz_command

    # An LZ2 structure of 4 distinct SNES 4bpp tiles. Viewed 2 tiles wide, both a
    # 2D wide-bitmap read and a 1×2 block grouping must re-lay the preview — proving
    # the overlay runs the same arrangement path as the live view (not a 1D fork).
    tiles = bytes((i * 29 + 5) & 0xFF for i in range(32 * 4))
    px = tmp_path / "packed.bin"
    px.write_bytes(lz_command.compress(tiles, big_endian_offsets=True) + bytes(64))

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(px))
    window._columns.setValue(2)  # a 2-wide bitmap, so 2D/blocks actually reorder
    window._compression.setCurrentIndex(window._compression.findData("decompress.lz2"))
    assert window._overlay.isVisible()
    flat = window._overlay._canvas._image.copy()

    window._two_d.setChecked(True)
    assert window._overlay._canvas._image != flat
    window._two_d.setChecked(False)
    assert window._overlay._canvas._image == flat  # back to the 1D preview

    window._block_rows.setValue(2)
    assert window._overlay._canvas._image != flat


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


def test_rows_past_end_of_file_are_not_clamped_or_black_filled(qtbot, tmp_path) -> None:
    small = tmp_path / "small.4bpp.sfc"
    small.write_bytes(bytes(32 * 6))  # 6 tiles of 32 B (SNES 4bpp)
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(small))
    window._columns.setValue(4)  # tile_count 6 => only ceil(6/4)=2 rows of data
    window._rows.setValue(8)  # far more rows than the file fills

    # The rows spin is a free display-window height: its max is a fixed 256, not
    # bound to the data, so a value larger than the file survives instead of being
    # dragged down to the 2 rows that exist.
    assert window._rows.value() == 8
    assert window._rows.maximum() == 256

    # The composed image is narrowed to the rows that actually hold tiles, so the
    # extra 6 empty rows show the neutral viewport background rather than black
    # filler tiles: height is 2 data rows * 8 px, not 8 rows * 8 px, and the width
    # is the 4 columns * 8 px. (SNES 4bpp tiles are 8x8.)
    assert (window._doc.tile_width, window._doc.tile_height) == (8, 8)
    assert window._canvas._image.height() == 2 * 8
    assert window._canvas._image.width() == 4 * 8


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
    assert window._canvas._selected_slots == {1, 2, 3, 4, 5}
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


def test_new_slice_inherits_parent_pixel_and_palette_not_toolbar(
    qtbot, tmp_path, monkeypatch
) -> None:
    from celpix.project.workspace import EntryKind
    from celpix.ui.slice_dialog import SliceDialog, SliceParams

    # File A: BGR555 white at absolute offset 32, viewed as a *non-default*
    # pixel preset with an offset-mode palette read from that offset.
    data = bytearray(bytes((i * 13 + 1) & 0xFF for i in range(32 * 8)))
    data[32:34] = b"\xff\x7f"  # BGR555 white
    file_a = tmp_path / "a.4bpp.sfc"
    file_a.write_bytes(bytes(data))
    file_b = tmp_path / "b.4bpp.sfc"
    file_b.write_bytes(bytes((i * 7 + 3) & 0xFF for i in range(32 * 8)))

    window = MainWindow()
    qtbot.addWidget(window)

    # A: non-default preset + offset palette, all while A is current.
    window._load_pixel(str(file_a))
    entry_a = window._workspace.find_file(str(file_a))
    idx = window._pixel_preset.findData("preset.pixel.snes-2bpp")
    assert idx != -1  # a genuinely non-default preset
    window._pixel_preset.setCurrentIndex(idx)
    assert window._load_palette_at_offset(32)
    assert window._palette_mode == "offset"

    # B: loaded, made current, and viewed as the *default* preset with a
    # default palette — so the live toolbar no longer reflects A's state.
    window._load_pixel(str(file_b))
    assert window._workspace.current is window._workspace.find_file(str(file_b))
    window._pixel_preset.setCurrentIndex(
        window._pixel_preset.findData("preset.pixel.snes-4bpp")  # the default
    )
    assert window._pixel_preset_id() == "preset.pixel.snes-4bpp"
    assert window._palette_mode == "default"

    # Create a slice from A while B is current. A real SliceParams (not None)
    # makes the slice actually get created and pushed; its own offset (64) is
    # deliberately distinct from the palette offset (32).
    monkeypatch.setattr(
        SliceDialog,
        "get_slice",
        staticmethod(lambda *_a, **_k: SliceParams("mine", 64, 64, "decompress.none")),
    )
    window._new_slice_for(entry_a)

    slices = [e for e in window._workspace.entries if e.kind is EntryKind.SLICE]
    assert len(slices) == 1
    slice_entry = slices[0]

    # The slice copied A's live state, not B's / the toolbar default. Adding a
    # slice auto-activates it, so its pending palette is already consumed into
    # the loaded document — the session (never consumed) still proves the copy.
    assert slice_entry.session is not None
    assert slice_entry.session.pixel_preset_id == "preset.pixel.snes-2bpp"
    assert slice_entry.session.palette_mode == "offset"
    assert slice_entry.session.compression_id == "decompress.none"

    # End-to-end: the on-screen slice loaded A's palette from offset 32 (A's
    # offset, distinct from the slice's own offset of 64), reading A's file.
    assert window._workspace.current is slice_entry
    assert window._doc.palette_config.source.path == str(file_a)
    assert window._doc.palette_config.source.offset == 32
    assert window._doc.palette.colors[0] == 0xFFFFFFFF


def test_jump_to_source_shows_slice_in_parent_at_absolute_offset(
    qtbot, tmp_path
) -> None:
    from celpix.project.workspace import EntrySession

    px = _make_snes_file(tmp_path)  # 8 tiles of 32 bytes = 256 bytes, snes-4bpp
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(px))  # parent is current, default snes-4bpp
    # Shrink the viewport so a mid-file origin is actually reachable: at the
    # default 16x16 page the whole file fits on one screen and any scroll clamps
    # back to 0, which would hide whether the jump landed on the right byte.
    window._columns.setValue(2)
    window._rows.setValue(2)

    # A slice of tiles 2..3 (offset 64, length 64). Give it a *different* pixel
    # preset than the parent's, so the jump proves the parent adopts the slice's
    # interpretation rather than keeping its own. snes-2bpp is 16 bytes/tile, so
    # the whole 256-byte file reads as 16 tiles — distinct from both the slice's
    # bounded 4-tile view and the parent's own 8-tile snes-4bpp view.
    slice_entry = window._workspace.add_slice(str(px), "gfx", 64, 64)
    slice_entry.session = EntrySession(
        pixel_preset_id="preset.pixel.snes-2bpp",
        palette_preset_id="preset.palette.bgr555",
    )
    parent = window._workspace.find_file(str(px))
    assert window._pixel_preset_id() == "preset.pixel.snes-4bpp"  # parent's own

    # Jump is navigation, not an edit: nothing should land on the undo stack.
    undo_before = window._undo_stack.count()
    window._jump_to_slice_source(slice_entry)
    assert window._undo_stack.count() == undo_before

    # The parent is on screen showing the *whole* file (16 snes-2bpp tiles), not
    # the slice's bounded region (which would be 4 tiles).
    assert window._workspace.current is parent
    assert window._doc.tile_count == 16
    # It adopted the slice's pixel preset, keeping the parent's header settings.
    assert window._pixel_preset_id() == "preset.pixel.snes-2bpp"
    assert parent.doc.pixel_config.interpret_preset_id == "preset.pixel.snes-2bpp"
    # The view origin lands byte-exactly on the slice's absolute file offset (64).
    assert window._offset_text() == "0x000040"
    assert window._byte_position() == 64


def test_jump_to_source_carries_the_slices_live_palette(qtbot, tmp_path) -> None:
    """The parent must arrive under the slice's palette, not its own.

    Two things conspire against that and both are silent: the on-screen entry's
    session snapshot lags the live toolbar (so the palette mode read off it is
    stale), and dropping the cached document recomputes the pending palette from
    the *parent*, overwriting the one the jump installed.
    """
    px = _make_snes_file(tmp_path)
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(px))
    window._load_palette_at_offset(0x10)  # the parent's own palette
    parent = window._workspace.find_file(str(px))

    slice_entry = window._workspace.add_slice(str(px), "gfx", 64, 64)
    window._activate_entry(slice_entry)
    window._load_palette_at_offset(0x40)  # the slice's, never captured to session
    assert window._doc.palette_config.source.offset == 0x40

    window._jump_to_slice_source(slice_entry)

    assert window._workspace.current is parent
    assert window._palette_mode is PaletteMode.OFFSET
    assert window._doc.palette_config.source.offset == 0x40  # the slice's, not 0x10


def test_jump_to_source_opens_parent_when_closed(qtbot, tmp_path) -> None:
    from celpix.project.workspace import EntrySession

    px = _make_snes_file(tmp_path)
    window = MainWindow()
    qtbot.addWidget(window)

    # A slice whose parent file was never opened — the handler must open it. We
    # can't close() an open parent to reach this state (that takes its slices
    # with it), so we register the slice directly against an unopened path.
    slice_entry = window._workspace.add_slice(str(px), "gfx", 64, 64)
    slice_entry.session = EntrySession(
        pixel_preset_id="preset.pixel.snes-2bpp",
        palette_preset_id="preset.palette.bgr555",
    )
    assert window._workspace.find_file(str(px)) is None  # parent not open yet

    window._jump_to_slice_source(slice_entry)

    # The parent, freshly opened, is on screen showing the whole file through
    # the slice's preset (16 snes-2bpp tiles). Its default-sized viewport swallows
    # the whole file, so the byte-exact landing is left to the test above — here
    # the point is that a *closed* parent is opened, shown, and reconfigured.
    parent = window._workspace.find_file(str(px))
    assert parent is not None  # the handler opened it
    assert window._workspace.current is parent
    assert window._doc.tile_count == 16  # the whole file, via the slice's preset
    assert window._pixel_preset_id() == "preset.pixel.snes-2bpp"
    assert parent.doc.pixel_config.interpret_preset_id == "preset.pixel.snes-2bpp"


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


def test_bookmark_snapshots_live_view_and_jump_restores_it(qtbot, tmp_path) -> None:
    from celpix.project.workspace import EntryKind, PaletteSource

    # BGR555 white at absolute offset 32, distinct from where the view is parked.
    data = bytearray(bytes((i * 13 + 1) & 0xFF for i in range(32 * 8)))  # 256 bytes
    data[32:34] = b"\xff\x7f"
    px = tmp_path / "p.4bpp.sfc"
    px.write_bytes(bytes(data))

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(px))
    entry = window._workspace.find_file(str(px))

    # Configure a genuinely non-default live view: snes-2bpp reads the 256 bytes
    # as 16 tiles, an offset-mode palette out of offset 32, a shrunk 2x2 viewport,
    # and a scroll to byte 64 (two 2-tile rows down) — none of it the defaults.
    idx = window._pixel_preset.findData("preset.pixel.snes-2bpp")
    window._pixel_preset.setCurrentIndex(idx)
    assert window._load_palette_at_offset(32)
    assert window._palette_mode == "offset"
    window._columns.setValue(2)
    window._rows.setValue(2)
    window._nav_rows(2)
    assert window._byte_position() == 64

    window._new_bookmark_for(entry)
    bookmarks = [e for e in window._workspace.entries if e.kind is EntryKind.BOOKMARK]
    assert len(bookmarks) == 1
    bookmark = bookmarks[0]

    # The snapshot captured the live state; the origin lives in slice_offset, so
    # pending_view keeps the geometry with offset/nudge zeroed.
    assert bookmark.slice_offset == 64
    assert bookmark.session.pixel_preset_id == "preset.pixel.snes-2bpp"
    assert bookmark.session.palette_mode == "offset"
    assert (bookmark.pending_view.columns, bookmark.pending_view.rows) == (2, 2)
    assert (bookmark.pending_view.tile_offset, bookmark.pending_view.byte_nudge) == (
        0,
        0,
    )
    assert bookmark.pending_palette == PaletteSource(offset=32)
    # Creating a bookmark must not activate it — the parent stays on screen.
    assert window._workspace.current is entry
    assert window._doc is entry.doc

    # Drive the parent's live state well away from the snapshot in every axis.
    window._pixel_preset.setCurrentIndex(
        window._pixel_preset.findData("preset.pixel.snes-4bpp")
    )
    window._palette_mode_combo.setCurrentIndex(
        window._palette_mode_combo.findData(PaletteMode.DEFAULT)
    )
    window._columns.setValue(16)
    window._rows.setValue(16)
    window._set_byte_position(0)
    assert window._palette_mode == "default"

    # The jump is navigation, not an edit: only the earlier creation is on the
    # undo stack; landing the view must add nothing.
    undo_before = window._undo_stack.count()
    window._jump_to_bookmark(bookmark)
    assert window._undo_stack.count() == undo_before

    # Every captured axis is restored on the parent: preset, palette mode+offset
    # (and the color it reads), viewport geometry, and the byte-exact origin.
    assert window._workspace.current is entry
    assert window._pixel_preset_id() == "preset.pixel.snes-2bpp"
    assert entry.doc.pixel_config.interpret_preset_id == "preset.pixel.snes-2bpp"
    assert window._palette_mode == "offset"
    assert window._doc.palette_config.source.offset == 32
    assert window._doc.palette.colors[0] == 0xFFFFFFFF
    assert (window._columns.value(), window._rows.value()) == (2, 2)
    assert window._byte_position() == 64
    # The jump copies the snapshot, never consuming it — the bookmark survives to
    # be jumped to again.
    assert bookmark.pending_view is not None
    assert bookmark.pending_palette is not None


def test_bookmark_double_click_jumps_instead_of_renaming(qtbot, tmp_path) -> None:
    from celpix.project.workspace import EntryKind

    px = _make_snes_file(tmp_path)
    window = MainWindow()
    qtbot.addWidget(window)
    window.show()  # a hidden view won't enter item-editing state
    window._load_pixel(str(px))
    entry = window._workspace.find_file(str(px))
    window._new_bookmark_for(entry)
    bookmark = next(
        e for e in window._workspace.entries if e.kind is EntryKind.BOOKMARK
    )

    panel = window._files_panel
    jumped: list[object] = []
    panel.jump_to_bookmark_requested.connect(jumped.append)

    # Double-clicking a bookmark is its jump action, not the inline renamer a
    # slice's double-click opens.
    panel._tree.itemDoubleClicked.emit(panel._items[bookmark], 0)
    assert jumped == [bookmark]
    assert panel._editing is None  # no rename editor opened


def _canvas_with_3x2_red(qtbot):
    """A Canvas holding a 3-col x 2-row image of opaque red 8x8 tiles at zoom 1.

    Red (not the gray backing) so a real tile's pixels are unmistakably distinct
    from the past-end fill.
    """
    from PySide6.QtGui import QImage

    from celpix.ui.canvas import Canvas

    c = Canvas()
    qtbot.addWidget(c)
    c.set_tile_size(8, 8)
    c.set_zoom(1)
    img = QImage(3 * 8, 2 * 8, QImage.Format.Format_RGB32)
    img.fill(0xFFFF0000)  # opaque red
    c.set_image(img)
    return c


def test_past_end_region_maps_linear_padding_to_last_row_block(qtbot) -> None:
    from PySide6.QtCore import QRect
    from PySide6.QtGui import QRegion

    c = _canvas_with_3x2_red(qtbot)

    # 5 of 6 slots filled: the stream ends one slot into the bottom row, so slot 5
    # (row 1, col 2) is padding. The trailing block is that single last-row cell:
    # x = col 2 * 8px, y = row 1 * 8px, one column wide, one tile tall.
    c.set_filled_tiles(5)
    assert c._background_region() == QRegion(QRect(2 * 8, 1 * 8, 1 * 8, 8))

    # The region is in device coords, so it scales with zoom.
    c.set_zoom(3)
    assert c._background_region() == QRegion(
        QRect(2 * 8 * 3, 1 * 8 * 3, 1 * 8 * 3, 8 * 3)
    )
    c.set_zoom(1)

    # A full window has no padding; neither does an unset count.
    c.set_filled_tiles(6)
    assert c._background_region() is None
    c.set_filled_tiles(None)
    assert c._background_region() is None

    # An exactly-full last row (remainder 0) is not padding either: 3 fills the
    # top row completely, leaving the bottom row entirely absent, not partial.
    c.set_filled_tiles(3)
    assert c._background_region() is None


def test_arrangement_controls_reach_the_view_and_canvas(qtbot, tmp_path) -> None:
    # The toolbar's block/order/2D controls must flow through _refresh_view into
    # the stored ViewOptions and the canvas's placement — otherwise the feature
    # renders nothing.
    px = _make_snes_file(tmp_path)  # 8 tiles
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(px))

    window._block_cols.setValue(2)
    window._block_rows.setValue(2)
    window._block_order.setCurrentIndex(window._block_order.findData("column"))
    window._two_d.setChecked(True)

    view = window._doc.view
    assert (view.block_columns, view.block_rows) == (2, 2)
    assert view.block_order == "column"
    assert view.two_dimensional is True
    # The canvas got the same placement, so clicks/selection map correctly.
    assert window._canvas._block_rows == 2
    assert window._canvas._block_order == "column"

    # Settings survive a round-trip through another entry and back (session state).
    window._workspace.add_slice(str(px), "gfx", 64, 64)
    window._activate_entry(window._workspace.entries[1])
    window._activate_entry(window._workspace.entries[0])
    back = window._doc.view
    assert (back.block_rows, back.block_order, back.two_dimensional) == (
        2,
        "column",
        True,
    )


def test_pattern_preset_fills_and_locks_arrangement_controls(qtbot, tmp_path) -> None:
    # The Pattern picker is the arrangement analogue of the Offset format picker:
    # a preset fills the block/order/2D controls and locks them; Custom unlocks
    # them. The lock keeps a preset's values from being edited out from under it.
    px = _make_snes_file(tmp_path)
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(px))

    # Default view is Linear (first preset) — controls locked.
    assert window._pattern.currentData().id == "linear"
    assert not window._block_cols.isEnabled()

    idx = window._pattern.findData(
        next(p for p in ARRANGEMENT_PRESETS if p.id == "genesis-sprite")
    )
    window._pattern.setCurrentIndex(idx)
    view = window._doc.view
    assert (view.block_columns, view.block_rows, view.block_order) == (2, 2, "column")
    assert view.two_dimensional is False
    # A preset owns the controls, so they stay read-only.
    assert not window._block_cols.isEnabled()
    assert not window._two_d.isEnabled()

    # Custom unlocks them without changing the values it inherits.
    window._pattern.setCurrentIndex(window._pattern.findData("custom"))
    assert window._block_cols.isEnabled() and window._two_d.isEnabled()
    assert window._doc.view.block_order == "column"


def test_pattern_selection_is_rederived_on_session_restore(qtbot, tmp_path) -> None:
    # Restoring an entry sets the block/order/2D widgets from its saved view; the
    # Pattern picker must reselect the matching preset (or Custom) to match — the
    # selection isn't persisted, it's derived from those four values.
    px = _make_snes_file(tmp_path)
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(px))

    window._pattern.setCurrentIndex(
        window._pattern.findData(
            next(p for p in ARRANGEMENT_PRESETS if p.id == "nes-8x16")
        )
    )
    window._workspace.add_slice(str(px), "gfx", 64, 64)
    window._activate_entry(window._workspace.entries[1])
    # The fresh slice inherits the default arrangement → Linear, controls locked.
    assert window._pattern.currentData().id == "linear"
    assert not window._block_rows.isEnabled()

    window._activate_entry(window._workspace.entries[0])
    # Back on the file, the 8×16 preset is reselected (and stays locked).
    assert window._pattern.currentData().id == "nes-8x16"
    assert not window._block_rows.isEnabled()


def test_canvas_block_layout_maps_clicks_and_backgrounds_gaps(qtbot) -> None:
    from PySide6.QtCore import QPointF, QRect
    from PySide6.QtGui import QRegion

    c = _canvas_with_3x2_red(qtbot)
    # 1×2 blocks: consecutive tiles stack vertically, so the cell at (col 0, row 1)
    # is slot 1 (the bottom of the first sprite), not slot 3 as in row-major.
    c.set_arrangement(1, 2, "row")
    assert c._slot_at(QPointF(0 * 8 + 4, 1 * 8 + 4)) == 1
    assert c._slot_at(QPointF(1 * 8 + 4, 0 * 8 + 4)) == 2  # second sprite's top

    # With only 4 of 6 slots filled, slots 4 and 5 (the third column's block) are
    # padding — the canvas backgrounds that whole column.
    c.set_filled_tiles(4)
    region = c._background_region()
    assert region == QRegion(QRect(2 * 8, 0, 8, 2 * 8))


def test_canvas_column_order_maps_clicks_genesis_style(qtbot) -> None:
    from PySide6.QtCore import QPointF

    # A 3×2-tile canvas as one column-major block: tiles run down each column, so
    # the top of the second column (cell 1,0) is slot 2, and the bottom of the
    # first column (cell 0,1) is slot 1 — the Mega Drive sprite order.
    c = _canvas_with_3x2_red(qtbot)
    c.set_arrangement(3, 2, "column")
    assert c._slot_at(QPointF(0 * 8 + 4, 1 * 8 + 4)) == 1  # bottom of column 0
    assert c._slot_at(QPointF(1 * 8 + 4, 0 * 8 + 4)) == 2  # top of column 1


def test_canvas_paints_past_end_slots_as_background(qtbot) -> None:
    from PySide6.QtGui import QColor

    from celpix.ui.canvas import CANVAS_BACKGROUND

    c = _canvas_with_3x2_red(qtbot)
    c.set_filled_tiles(5)  # slot 5 (bottom-right cell) is padding

    img_out = c.grab().toImage()

    # Sample the centre of the padding cell (col 2, row 1): x = 2*8 + 4 = 20,
    # y = 1*8 + 4 = 12. It must show the neutral backing, not a black index-0 tile.
    # Compare RGB only — grab() may carry an alpha the fill color doesn't.
    assert img_out.pixelColor(20, 12).rgb() == CANVAS_BACKGROUND.rgb()

    # A real (filled) tile still paints its data: slot 0's centre (4, 4) is red,
    # and definitely not the gray backing.
    assert img_out.pixelColor(4, 4).rgb() == QColor(0xFF, 0x00, 0x00).rgb()
    assert img_out.pixelColor(4, 4).rgb() != CANVAS_BACKGROUND.rgb()


def test_activating_missing_data_entry_shows_unavailable(qtbot, tmp_path) -> None:
    # A referenced file that has moved makes its entry the current selection but
    # inert — the old behaviour refused to switch; now it degrades gracefully.
    a = _make_snes_file(tmp_path)  # s.4bpp.sfc
    b = tmp_path / "b.4bpp.sfc"
    b.write_bytes(bytes((i * 7 + 3) & 0xFF for i in range(32 * 8)))
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(a))
    window._load_pixel(str(b))
    entries = window._workspace.entries

    a.unlink()  # a's file vanishes after it was opened; b is on screen
    assert window._workspace.current is entries[1]
    window._activate_entry(entries[0])

    # It becomes current (not bounced back to b), with the document actions greyed.
    assert window._workspace.current is entries[0]
    assert not window._write_action.isEnabled()
    assert not window._new_slice_action.isEnabled()
    assert not window._new_slice_from_view_action.isEnabled()
    assert not window._new_bookmark_action.isEnabled()


def test_locate_action_tracks_missing_files(qtbot, tmp_path) -> None:
    rom = _make_snes_file(tmp_path)
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(rom))
    assert not window._locate_missing_action.isEnabled()  # file present → disarmed

    rom.unlink()  # the referenced file goes missing
    window._update_locate_action()
    assert window._locate_missing_action.isEnabled()

    # Restoring the file at the same path clears the missing state and disarms it.
    rom.write_bytes(bytes((i * 13 + 1) & 0xFF for i in range(32 * 8)))
    window._update_locate_action()
    assert not window._locate_missing_action.isEnabled()


def _click_message_box(monkeypatch, role) -> None:
    """Make every QMessageBox auto-click its button carrying ``role``."""
    from PySide6.QtWidgets import QMessageBox

    monkeypatch.setattr(QMessageBox, "exec", lambda self: 0)
    monkeypatch.setattr(
        QMessageBox,
        "clickedButton",
        lambda self: next(
            (button for button in self.buttons() if self.buttonRole(button) == role),
            None,
        ),
    )


def _accept_message_box(monkeypatch) -> None:
    """Make the next QMessageBox auto-click its AcceptRole button."""
    from PySide6.QtWidgets import QMessageBox

    _click_message_box(monkeypatch, QMessageBox.ButtonRole.AcceptRole)


def test_relocate_missing_corrects_path_loads_and_clears(
    qtbot, tmp_path, monkeypatch
) -> None:
    from PySide6.QtWidgets import QFileDialog

    from celpix.project.workspace import data_missing, missing_paths

    src = tmp_path / "src"
    src.mkdir()
    rom = src / "rom.4bpp.sfc"
    rom.write_bytes(bytes((i * 13 + 1) & 0xFF for i in range(32 * 8)))
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(rom))
    entry = window._workspace.current

    # The ROM moves elsewhere on disk; the open entry now points at nothing.
    dest = tmp_path / "dest"
    dest.mkdir()
    moved = dest / "rom.4bpp.sfc"
    rom.rename(moved)
    entry.doc = None  # force a reload once the path is corrected
    assert data_missing(entry)

    # Accept the summary prompt, then point the file picker at the moved file.
    _accept_message_box(monkeypatch)
    monkeypatch.setattr(
        QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(moved), ""))
    )
    window._relocate_missing(prompt_summary=True)

    assert entry.path == str(moved)
    assert not data_missing(entry)
    assert entry.doc is not None and window._doc.tile_count == 8
    assert missing_paths(window._workspace) == []
    assert not window._locate_missing_action.isEnabled()


def test_relocate_missing_rejects_duplicate_open_file(
    qtbot, tmp_path, monkeypatch, captured_alerts
) -> None:
    from PySide6.QtWidgets import QFileDialog

    from celpix.project.workspace import data_missing, missing_paths

    # File B is genuinely open; a second FILE entry A references a file that isn't
    # on disk. Locating A onto B's path would leave two entries editing one file.
    file_b = _make_snes_file(tmp_path)
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(file_b))
    entry_a = window._workspace.open_file(str(tmp_path / "gone.4bpp.sfc"))
    assert data_missing(entry_a)
    entry_count = len(window._workspace.entries)

    # The picker points A at B — already open — so the relocation must be refused.
    _accept_message_box(monkeypatch)
    monkeypatch.setattr(
        QFileDialog,
        "getOpenFileName",
        staticmethod(lambda *a, **k: (str(file_b), "")),
    )
    window._relocate_missing(prompt_summary=True)

    # A stays missing at its original path, no duplicate entry for B was created,
    # and the user was told why.
    assert entry_a.path == str(tmp_path / "gone.4bpp.sfc")
    assert data_missing(entry_a)
    assert missing_paths(window._workspace) == [str(tmp_path / "gone.4bpp.sfc")]
    assert len(window._workspace.entries) == entry_count
    assert any(title == "Celpix - locate" for title, _msg in captured_alerts)


def test_missing_palette_file_degrades_quietly_and_keeps_reference(
    qtbot, tmp_path, monkeypatch, captured_alerts
) -> None:
    import json
    from os.path import normcase

    from PySide6.QtWidgets import QMessageBox

    from celpix.project import projectfile
    from celpix.project.workspace import (
        EntrySession,
        PaletteSource,
        Workspace,
        palette_source_for,
    )

    rom = _make_snes_file(tmp_path)  # a real pixel file
    pal = tmp_path / "s.pal"  # an external palette that is missing on load

    # A project whose entry reads its palette from an external file.
    ws = Workspace()
    entry = ws.open_file(str(rom))
    entry.session = EntrySession(
        pixel_preset_id="preset.pixel.snes-4bpp",
        palette_preset_id="preset.palette.bgr555",
        palette_mode="file",
    )
    entry.pending_palette = PaletteSource(path=str(pal), offset=0)
    ws.set_current(entry)
    project = tmp_path / "hack.celpix"
    projectfile.save_project(ws, str(project))

    # The palette file can't be found when the project opens. Decline the relocate
    # prompt so the quiet-degrade path runs on the entry's first activation.
    monkeypatch.setattr(QMessageBox, "exec", lambda self: 0)
    monkeypatch.setattr(QMessageBox, "clickedButton", lambda self: None)
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_project(str(project))

    loaded = window._workspace.current
    assert loaded.session.palette_mode == "file"  # mode kept, not reset to default
    assert loaded.missing_palette is not None  # the reference is remembered
    assert window._doc.palette == window._fallback_palette()  # default palette shown
    assert captured_alerts == []  # a missing palette degrades silently

    # The original reference survives: palette_source_for and a re-save both carry
    # the intended path forward, so it can be relocated later.
    assert normcase(palette_source_for(loaded).path) == normcase(str(pal))
    reproject = tmp_path / "resaved.celpix"
    projectfile.save_project(window._workspace, str(reproject))
    raw = json.loads(reproject.read_text(encoding="utf-8"))
    assert raw["entries"][0]["palette"]["path"] == "s.pal"


# -- color editing (docs/design/palette-editing.md) ------------------------
def _open_for_color_edit(qtbot, tmp_path, monkeypatch):
    """A window on the default palette with entry 3 selected for editing."""
    window = _open_with_palette_at_tile1(qtbot, tmp_path, monkeypatch)
    window._palette_panel._select(3)
    return window


def test_editing_the_default_palette_forks_a_custom_one(
    qtbot, tmp_path, monkeypatch
) -> None:
    window = _open_for_color_edit(qtbot, tmp_path, monkeypatch)
    assert window._palette_mode == "default"
    before_len = len(window._doc.palette)
    assert before_len == 16  # 4bpp index space

    window._on_color_changed(0xFF123456)

    # The edit forked to a project-stored Custom palette expanded to 16 rows,
    # and landed on the selected entry.
    assert window._palette_mode == "custom"
    assert window._palette_mode_combo.currentData() == "custom"
    assert len(window._doc.palette) == 256
    assert window._doc.palette.color(3) == 0xFF123456
    # A custom palette has no file behind it, so Write must never target one.
    assert window._doc.palette_config.write_enabled is False
    # ...and the entry isn't "dirty": it is saved with the project, not by Write.
    assert not window._workspace.current.pixel_dirty


def test_custom_fork_undo_peels_the_edit_then_the_fork(
    qtbot, tmp_path, monkeypatch
) -> None:
    window = _open_for_color_edit(qtbot, tmp_path, monkeypatch)
    stack = window._undo_stack
    original = window._doc.palette.color(3)

    window._on_color_changed(0xFF123456)
    assert stack.count() >= 2  # the fork and the edit are separate steps

    stack.undo()  # the color edit
    assert window._palette_mode == "custom"
    assert window._doc.palette.color(3) == original

    stack.undo()  # the fork itself
    assert window._palette_mode == "default"
    assert len(window._doc.palette) == 16

    stack.redo()
    stack.redo()
    assert window._palette_mode == "custom"
    assert window._doc.palette.color(3) == 0xFF123456


def test_consecutive_edits_to_one_entry_merge_into_a_step(
    qtbot, tmp_path, monkeypatch
) -> None:
    window = _open_for_color_edit(qtbot, tmp_path, monkeypatch)
    before_3 = window._doc.palette.color(3)
    window._on_color_changed(0xFF111111)  # forks to custom, then edits
    stack = window._undo_stack
    count = stack.count()

    # A slider drag emits on every step; the whole run collapses into the one
    # step already on the stack, exactly as consecutive view moves do.
    for value in (0xFF222222, 0xFF333333, 0xFF444444):
        window._on_color_changed(value)
    assert stack.count() == count
    assert window._doc.palette.color(3) == 0xFF444444

    # A different entry breaks the run rather than merging into it.
    window._palette_panel._select(4)
    before_4 = window._doc.palette.color(4)
    window._on_color_changed(0xFF555555)
    assert stack.count() == count + 1

    # Undo peels them in reverse: entry 4, then entry 3's entire run at once.
    stack.undo()
    assert window._doc.palette.color(4) == before_4
    stack.undo()
    assert window._doc.palette.color(3) == before_3


def test_edit_run_returning_to_its_start_leaves_no_step(
    qtbot, tmp_path, monkeypatch
) -> None:
    window = _open_for_color_edit(qtbot, tmp_path, monkeypatch)
    window._on_color_changed(0xFF111111)  # fork + first edit
    stack = window._undo_stack
    count = stack.count()
    start = window._doc.palette.color(3)

    window._on_color_changed(0xFF222222)
    window._on_color_changed(start)  # dragged back to where it began

    assert stack.count() == count  # the empty step dropped itself
    assert window._doc.palette.color(3) == start


def test_editing_a_file_palette_writes_in_place(qtbot, tmp_path, monkeypatch) -> None:
    from PySide6.QtWidgets import QFileDialog

    window = _open_with_palette_at_tile1(qtbot, tmp_path, monkeypatch)
    pal = tmp_path / "colors.pal"
    pal.write_bytes(bytes(32))  # 16 BGR555 entries, all black
    monkeypatch.setattr(
        QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(pal), ""))
    )
    assert window._open_palette()
    assert window._palette_mode == "file"
    window._palette_panel._select(1)

    window._on_color_changed(0xFFFFFFFF)

    # A file palette is edited in place — no fork, and Write is armed.
    assert window._palette_mode == "file"
    assert window._doc.palette.color(1) == 0xFFFFFFFF
    assert window._doc.palette_config.write_enabled is True
    # Pending on the *palette* pathway — the graphic itself is unchanged.
    assert window._workspace.current.palette_dirty
    assert not window._workspace.current.pixel_dirty

    window._write_current()
    # BGR555 white at entry 1 = 0x7FFF little-endian, in the file's second slot.
    assert pal.read_bytes()[2:4] == b"\xff\x7f"
    assert not window._workspace.current.palette_dirty


def test_palette_only_edit_does_not_rewrite_the_graphic(
    qtbot, tmp_path, monkeypatch
) -> None:
    # The dirt is tracked per pathway, so saving a color edit must leave the
    # graphic file untouched — it has no pending changes of its own.
    from PySide6.QtWidgets import QFileDialog

    window = _open_with_palette_at_tile1(qtbot, tmp_path, monkeypatch)
    entry = window._workspace.current
    graphic = Path(entry.path)
    pal = tmp_path / "colors.pal"
    pal.write_bytes(bytes(32))
    monkeypatch.setattr(
        QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(pal), ""))
    )
    assert window._open_palette()
    window._palette_panel._select(1)

    before = graphic.read_bytes()
    mtime = graphic.stat().st_mtime_ns
    window._on_color_changed(0xFFFFFFFF)

    # The palette is pending; the graphic itself is not.
    assert entry.palette_dirty
    assert not entry.pixel_dirty

    window._write_current()
    assert "palette" in window.statusBar().currentMessage()
    assert "pixel" not in window.statusBar().currentMessage()
    # Byte-identical *and* untouched — no needless rewrite.
    assert graphic.read_bytes() == before
    assert graphic.stat().st_mtime_ns == mtime
    assert not entry.palette_dirty


def test_eyedropper_samples_the_canvas_without_moving_the_selection(
    qtbot, tmp_path, monkeypatch
) -> None:
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QMouseEvent

    window = _open_for_color_edit(qtbot, tmp_path, monkeypatch)
    window._open_color_editor(3)
    picked: list[int] = []
    window._canvas.color_picked.connect(picked.append)

    window._set_pick_mode(True)
    before_selection = window._selected_tile
    event = QMouseEvent(
        QMouseEvent.Type.MouseButtonPress,
        QPointF(2, 2),
        QPointF(2, 2),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    window._canvas.mousePressEvent(event)

    assert len(picked) == 1
    assert picked[0] >> 24 == 0xFF  # a real, opaque rendered pixel
    # Sampling must not select a tile: in Offset mode that would reload the
    # palette out from under the color being edited.
    assert window._selected_tile == before_selection
    # The pick disarms itself and lands in the editor.
    assert window._color_editor.editor.color() == picked[0]


def test_eyedropper_from_the_grid_keeps_the_edited_entry_selected(
    qtbot, tmp_path, monkeypatch
) -> None:
    window = _open_for_color_edit(qtbot, tmp_path, monkeypatch)
    window._open_color_editor(3)
    panel = window._palette_panel
    source = window._doc.palette.color(9)

    window._set_pick_mode(True)
    panel.color_picked.emit(source)

    # Entry 3 is still the one being edited — it took entry 9's color.
    assert panel.selected_index() == 3
    assert window._doc.palette.color(3) == source


def test_custom_palette_round_trips_through_a_project(
    qtbot, tmp_path, monkeypatch
) -> None:
    from celpix.project import projectfile

    window = _open_for_color_edit(qtbot, tmp_path, monkeypatch)
    window._on_color_changed(0xFF8899AA)
    assert window._palette_mode == "custom"
    saved = list(window._doc.palette.colors)

    project = tmp_path / "p.celpix"
    window._save_project_to(str(project))  # the real path — it refreshes the session

    # The colors themselves are the stored form — there is no file behind them.
    loaded = projectfile.load_project(str(project))
    assert loaded.version == projectfile.PROJECT_VERSION
    entry = loaded.entries[0]
    assert entry.session.palette_mode == "custom"
    assert entry.pending_palette.colors == saved
    assert entry.pending_palette.path is None

    # Re-opening restores the edited palette rather than regenerating a default.
    window2 = MainWindow()
    qtbot.addWidget(window2)
    window2._load_project(str(project))
    assert window2._palette_mode == "custom"
    assert window2._doc.palette.colors == saved


# -- export (docs/design/export.md) ----------------------------------------
def test_export_document_image_is_indexed_with_opaque_palette(qtbot, tmp_path):
    from PySide6.QtGui import QImage

    from celpix.ui import export

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_make_snes_file(tmp_path)))  # 8 SNES 4bpp tiles

    image = export.document_image(window._doc, window._registry)
    assert image.format() == QImage.Format.Format_Indexed8
    # 8 tiles at the default 16 columns fit one row, narrowed to 8 tiles wide.
    assert (image.width(), image.height()) == (64, 8)
    table = image.colorTable()
    assert len(table) == 16  # exactly the 4bpp subpalette, not a padded 256
    # Every entry keeps its own (opaque) alpha — index 0 is not forced transparent.
    assert table[0] >> 24 == 0xFF
    assert table[1] >> 24 == 0xFF


def test_export_current_png_round_trips(qtbot, tmp_path, monkeypatch):
    from PySide6.QtGui import QImage
    from PySide6.QtWidgets import QFileDialog

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_make_snes_file(tmp_path)))

    out = tmp_path / "sheet.png"
    monkeypatch.setattr(
        QFileDialog, "getSaveFileName", lambda *a, **k: (str(out), "PNG image (*.png)")
    )
    window._export_png(window._workspace.current)

    assert out.exists()
    reloaded = QImage(str(out))
    assert not reloaded.isNull()
    assert (reloaded.width(), reloaded.height()) == (64, 8)


def test_export_project_writes_slices_and_skips_sliced_file(
    qtbot, tmp_path, monkeypatch
):
    from PySide6.QtWidgets import QFileDialog

    window = MainWindow()
    qtbot.addWidget(window)
    # A plain file (exported whole) and a sliced file (only its slices export).
    plain = _make_snes_file(tmp_path)
    plain.rename(tmp_path / "plain.4bpp.sfc")
    plain = tmp_path / "plain.4bpp.sfc"
    sheet = tmp_path / "sheet.4bpp.sfc"
    sheet.write_bytes(bytes((i * 3) & 0xFF for i in range(32 * 8)))
    window._load_pixel(str(plain))
    window._load_pixel(str(sheet))
    window._workspace.add_slice(str(sheet), "hero", 0, 64)  # tiles 0..1
    window._workspace.add_slice(str(sheet), "foe", 64, 64)  # tiles 2..3

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    monkeypatch.setattr(
        QFileDialog, "getExistingDirectory", lambda *a, **k: str(out_dir)
    )
    window._export_project()

    written = sorted(p.name for p in out_dir.glob("*.png"))
    # The unsliced file plus both slices; never the sliced parent file itself.
    # (Only the final extension is stripped, so the stem keeps its ".4bpp" tag.)
    assert written == [
        "plain.4bpp.png",
        "sheet.4bpp_foe.png",
        "sheet.4bpp_hero.png",
    ]


def test_export_png_loads_the_named_entry_not_the_current_one(
    qtbot, tmp_path, monkeypatch
):
    """The files-list Export acts on the entry whose menu was opened, loading it
    on demand — the parent file stays the current view throughout."""
    from PySide6.QtGui import QImage
    from PySide6.QtWidgets import QFileDialog

    window = MainWindow()
    qtbot.addWidget(window)
    sheet = _make_snes_file(tmp_path)
    window._load_pixel(str(sheet))
    parent = window._workspace.current
    hero = window._workspace.add_slice(str(sheet), "hero", 0, 64)  # tiles 0..1
    assert hero.doc is None  # never activated

    out = tmp_path / "hero.png"
    monkeypatch.setattr(
        QFileDialog, "getSaveFileName", lambda *a, **k: (str(out), "PNG image (*.png)")
    )
    window._files_panel.export_png_requested.emit(hero)

    reloaded = QImage(str(out))
    assert not reloaded.isNull()
    # Two tiles' worth of pixels, not the whole eight-tile file.
    assert (reloaded.width(), reloaded.height()) == (16, 8)
    assert window._workspace.current is parent  # the view never moved


def test_export_raw_writes_decoded_bytes(qtbot, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog

    window = MainWindow()
    qtbot.addWidget(window)
    px = _make_snes_file(tmp_path)
    window._load_pixel(str(px))

    out = tmp_path / "dump.bin"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(out), ""))
    window._export_raw(window._workspace.current)

    # An uncompressed file's decoded bytes are its bytes verbatim.
    assert out.read_bytes() == px.read_bytes()


def test_window_title_names_project_and_marks_it_unsaved(qtbot, tmp_path):
    window = MainWindow()
    qtbot.addWidget(window)
    px = _make_snes_file(tmp_path)
    window._load_pixel(str(px))
    # No project yet: the title names the current graphic, and carries no
    # unsaved marker - there is no project file to be unsaved against.
    assert window.windowTitle() == f"Celpix - {px.name}"
    assert not window.isWindowModified()

    # Saving gives the session a project file: the title names it (with Qt's
    # [*] marker placeholder) and reads clean.
    project = tmp_path / "session.celpix"
    window._save_project_to(str(project))
    assert window.windowTitle() == "Celpix - session.celpix[*]"
    assert not window.isWindowModified()

    # A view change is part of what a project stores, so it goes unsaved...
    window._zoom.setValue(window._zoom.value() + 1)
    assert window.isWindowModified()
    # ...and putting it back clears the marker: "modified" is the live session
    # compared against the file, not a flag that only ever goes one way.
    window._zoom.setValue(window._zoom.value() - 1)
    assert not window.isWindowModified()

    window._zoom.setValue(window._zoom.value() + 1)
    window._save_project_to(str(project))
    assert not window.isWindowModified()

    # A tile selection is not part of what a project stores, so clicking around
    # in one must never leave it looking unsaved.
    window._select_tiles(1, 3)
    assert not window._project_is_dirty()


def test_loading_over_an_unsaved_project_offers_to_save_it(
    qtbot, tmp_path, monkeypatch
) -> None:
    from PySide6.QtWidgets import QMessageBox

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_make_snes_file(tmp_path)))
    project = tmp_path / "session.celpix"
    window._save_project_to(str(project))
    other = tmp_path / "other.celpix"
    other.write_bytes(project.read_bytes())

    # Unsaved session changes; cancelling the prompt leaves the load undone.
    window._zoom.setValue(window._zoom.value() + 3)
    _click_message_box(monkeypatch, QMessageBox.ButtonRole.RejectRole)
    window._load_project(str(other))
    assert window._project_path == str(project)
    assert window.isWindowModified()

    # Answering "Save Project" writes the changes out before loading on, so the
    # project the user is leaving keeps them.
    _click_message_box(monkeypatch, QMessageBox.ButtonRole.AcceptRole)
    window._load_project(str(other))
    assert window._project_path == str(other)
    assert not window.isWindowModified()
    assert json.loads(project.read_text())["entries"][0]["view"]["zoom"] == (
        window._zoom.value() + 3
    )


def test_export_dialog_defaults_to_project_dir(qtbot, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog

    window = MainWindow()
    qtbot.addWidget(window)
    graphics = tmp_path / "gfx"
    graphics.mkdir()
    px = graphics / "s.4bpp.sfc"
    px.write_bytes(bytes(32 * 4))
    window._load_pixel(str(px))
    proj_dir = tmp_path / "proj"
    proj_dir.mkdir()
    window._project_path = str(proj_dir / "s.celpix")

    captured = {}
    monkeypatch.setattr(
        QFileDialog,
        "getSaveFileName",
        lambda parent, caption, directory, filt: (
            captured.update(directory=directory) or ("", "")
        ),
    )
    window._export_png(window._workspace.current)

    # The suggested path sits in the project's folder, not the graphic's.
    assert str(proj_dir) in captured["directory"]
    assert str(graphics) not in captured["directory"]


# -- clipboard: copy / cut / paste -----------------------------------------
def _open_pixels(qtbot, tmp_path, data: bytes | None = None):
    px = tmp_path / "clip.4bpp.sfc"
    px.write_bytes(data or bytes((i * 13 + 1) & 0xFF for i in range(32 * 8)))
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(px))
    return window


def test_switching_pixel_format_keeps_unsaved_edits(qtbot, tmp_path) -> None:
    """A format switch reinterprets the bytes in memory, it doesn't re-read them.

    The edits only exist in that buffer, so re-running the pathway would put the
    file's own bytes back over them - a silent loss with nothing to undo.
    """
    window = _open_pixels(qtbot, tmp_path)
    window._select_tiles(0, 0)
    window._clear_pixels()
    edited = bytes(window._doc.pixel_data)
    assert edited != (tmp_path / "clip.4bpp.sfc").read_bytes()  # unsaved: memory only

    combo = window._pixel_preset
    presets = [combo.itemData(i) for i in range(combo.count())]
    other = next(p for p in presets if p != window._pixel_preset_id())
    combo.setCurrentIndex(presets.index(other))

    assert bytes(window._doc.pixel_data) == edited
    window._undo_stack.undo()  # back to the original format, still edited
    assert bytes(window._doc.pixel_data) == edited


def test_copy_then_paste_duplicates_a_tile_and_undo_restores(qtbot, tmp_path) -> None:
    window = _open_pixels(qtbot, tmp_path)
    original = window._doc.pixel_data

    window._select_tiles(0, 0)
    assert window._copy_selection()
    window._select_tiles(3, 3)
    window._paste()

    # Tile 3 now holds tile 0's bytes; indices move verbatim within one format.
    assert window._doc.pixel_data[96:128] == original[:32]
    assert window._doc.pixel_data[:96] == original[:96]
    assert window._workspace.current.pixel_dirty
    # The paste selects what it landed on, so the next paste stamps forward.
    assert (window._selected_tile, window._selected_last) == (3, 3)

    window._undo_stack.undo()
    assert window._doc.pixel_data == original


def test_paste_is_clipped_at_the_end_of_the_data(qtbot, tmp_path) -> None:
    window = _open_pixels(qtbot, tmp_path)  # 8 tiles
    size = len(window._doc.pixel_data)
    window._select_tiles(0, 3)  # copy four tiles …
    assert window._copy_selection()
    window._select_tiles(6, 6)  # … onto the last two: two must be dropped
    window._paste()
    assert len(window._doc.pixel_data) == size
    assert window._selected_last == 7


def test_clear_blanks_the_selected_tiles(qtbot, tmp_path) -> None:
    window = _open_pixels(qtbot, tmp_path)
    window._select_tiles(2, 3)
    window._clear_pixels()
    assert window._doc.pixel_data[64:128] == bytes(64)
    assert window._doc.pixel_data[128:160] != bytes(32)  # tile 4 untouched


def test_cut_copies_before_it_blanks(qtbot, tmp_path) -> None:
    window = _open_pixels(qtbot, tmp_path)
    original = window._doc.pixel_data
    window._select_tiles(1, 1)
    window._cut_selection()
    assert window._doc.pixel_data[32:64] == bytes(32)
    # What was cut is on the clipboard: pasting it elsewhere restores it.
    window._select_tiles(5, 5)
    window._paste()
    assert window._doc.pixel_data[160:192] == original[32:64]


def test_paste_of_an_external_image_matches_the_active_palette(qtbot, tmp_path) -> None:
    from PySide6.QtGui import QGuiApplication, QImage

    from celpix.pipeline import pipeline

    window = _open_pixels(qtbot, tmp_path)
    # An image straight from another program: nothing but pixels on the
    # clipboard, painted in a color the palette holds exactly.
    color = window._doc.palette.color(5)
    image = QImage(8, 8, QImage.Format.Format_ARGB32)
    image.fill(color)
    QGuiApplication.clipboard().setImage(image)

    window._select_tiles(1, 1)
    window._paste()
    tile = pipeline.decode_tiles(window._doc, window._registry, 1, 1)[0]
    assert set(tile.data) == {5}


def test_paste_into_a_narrower_format_refits_by_color(qtbot, tmp_path) -> None:
    from celpix.pipeline import pipeline

    window = _open_pixels(qtbot, tmp_path)
    window._select_tiles(0, 0)
    source = pipeline.decode_tiles(window._doc, window._registry, 0, 1)[0]
    assert max(source.data) > 3  # the 4bpp tile really does use high indices
    assert window._copy_selection()

    # 2bpp can only reference four colors; the copied indices no longer fit, so
    # the paste re-matches them through the palette instead of writing garbage.
    window._pixel_preset.setCurrentIndex(
        window._pixel_preset.findData("preset.pixel.snes-2bpp")
    )
    window._select_tiles(1, 1)
    window._paste()
    pasted = pipeline.decode_tiles(window._doc, window._registry, 1, 1)[0]
    assert max(pasted.data) <= 3


def test_clipboard_payload_round_trips_and_rejects_junk() -> None:
    from celpix.ui.clipboard import TilePayload

    tiles = [IndexGrid(2, 2, bytes([0, 1, 2, 3]))]
    payload = TilePayload.from_tiles(tiles, (0xFF000000, 0xFFFFFFFF))
    raw = payload.to_bytes()
    assert TilePayload.from_bytes(raw) == payload
    assert TilePayload.from_bytes(raw).tiles() == tiles
    # The clipboard is shared with the whole machine: anything malformed has to
    # read as "no payload", never as a torn grid.
    assert TilePayload.from_bytes(raw[:-1]) is None
    assert TilePayload.from_bytes(b"") is None
    assert TilePayload.from_bytes(b"not a celpix payload") is None


def test_clipboard_actions_track_the_selection(qtbot, tmp_path) -> None:
    window = _open_pixels(qtbot, tmp_path)
    assert not window._copy_action.isEnabled()
    window._select_tiles(0, 1)
    assert window._copy_action.isEnabled()
    assert window._cut_action.isEnabled()
    window._copy_selection()
    assert window._paste_action.isEnabled()


def _rect_shape(window, tmp_path) -> None:
    """Switch the Shape picker to Rectangle.

    QSettings is redirected to a throwaway INI first: the switch persists the
    choice app-wide, so neither the developer's real config nor a later test in
    this process may inherit it.
    """
    from celpix.ui.main_window.selection import SelectionShape

    _isolate_settings(tmp_path)
    combo = window._selection_shape
    combo.setCurrentIndex(combo.findData(SelectionShape.RECT))


def test_rectangle_drag_selects_a_block_and_shape_switch_collapses(
    qtbot, tmp_path, monkeypatch
) -> None:
    window = _open_big(qtbot, tmp_path, monkeypatch, tiles=64)
    window._columns.setValue(8)
    window._rows.setValue(8)

    # A linear drag first: switching shape must not reinterpret it as a block.
    window._on_tiles_selected(0, 9)
    assert window._selection_tiles() == list(range(10))
    assert not window._canvas._selection_as_block
    # A run filling whole rows fills a rectangle, but was picked as a run and
    # must keep outlining row by row.
    window._on_tiles_selected(0, 15)
    assert not window._canvas._selection_as_block
    _rect_shape(window, tmp_path)
    assert (window._selected_tile, window._selected_last) == (0, 0)

    # Slots 0..9 now read as the corners of a 2x2 cell block, so the selection
    # is two runs of two tiles a row apart — not the ten tiles between them.
    window._on_tiles_selected(0, 9)
    assert window._rect_cells == (2, 2)
    assert window._selection_tiles() == [0, 1, 8, 9]
    assert window._canvas._selected_slots == {0, 1, 8, 9}
    assert window._canvas._selection_as_block


def test_rectangle_collapses_when_the_view_reshuffles_its_tiles(
    qtbot, tmp_path, monkeypatch
) -> None:
    window = _open_big(qtbot, tmp_path, monkeypatch, tiles=64)
    window._columns.setValue(8)
    window._rows.setValue(8)
    _rect_shape(window, tmp_path)
    window._on_tiles_selected(0, 9)  # tiles 0, 1, 8, 9

    # Half the columns: those same four cells now sit over tiles 0, 1, 4, 5, so
    # the rectangle no longer covers what was selected and drops to its corner.
    window._columns.setValue(4)
    assert window._rect_cells is None
    assert (window._selected_tile, window._selected_last) == (0, 0)
    assert window._canvas._selected_slots == {0}


def test_new_slice_from_selection_refuses_a_disjoint_rectangle(
    qtbot, tmp_path, monkeypatch, captured_alerts
) -> None:
    from celpix.ui.slice_dialog import SliceDialog

    window = _open_big(qtbot, tmp_path, monkeypatch, tiles=64)
    window._columns.setValue(8)
    window._rows.setValue(8)
    _rect_shape(window, tmp_path)
    captured: dict = {}
    monkeypatch.setattr(
        SliceDialog,
        "get_slice",
        staticmethod(lambda *_args, **kwargs: captured.update(kwargs)),
    )

    window._on_tiles_selected(0, 9)  # 2x2 — its rows sit apart in the file
    window._new_slice_from_selection()
    assert not captured
    assert "continuous run" in captured_alerts[-1][1]

    # Full width: the rows are back-to-back, so it is one run and is offered.
    window._on_tiles_selected(0, 15)
    window._new_slice_from_selection()
    assert (captured["offset"], captured["length"]) == (0, 16 * 32)


def test_rectangle_copy_paste_and_clear_touch_only_their_cells(
    qtbot, tmp_path, monkeypatch
) -> None:
    from celpix.ui import clipboard

    window = _open_big(qtbot, tmp_path, monkeypatch, tiles=64)
    window._columns.setValue(8)
    window._rows.setValue(8)
    _rect_shape(window, tmp_path)
    original = window._doc.pixel_data
    tb = window._doc.bytes_per_tile

    def tile_bytes(data, index):
        return data[index * tb : (index + 1) * tb]

    window._on_tiles_selected(0, 9)  # the 2x2 block of tiles 0, 1, 8, 9
    assert window._copy_selection()
    payload = clipboard.take_payload()
    assert (payload.count, payload.columns) == (4, 2)

    # Anchored on tile 4 the copy lands as a 2x2 block, not a run of four.
    window._on_tiles_selected(4, 4)
    window._paste()
    for src, dst in ((0, 4), (1, 5), (8, 12), (9, 13)):
        assert tile_bytes(window._doc.pixel_data, dst) == tile_bytes(original, src)
    assert tile_bytes(window._doc.pixel_data, 6) == tile_bytes(original, 6)

    # Clear blanks the rectangle's own cells and leaves the gap between rows.
    window._on_tiles_selected(0, 9)
    window._clear_pixels()
    for blanked in (0, 1, 8, 9):
        assert tile_bytes(window._doc.pixel_data, blanked) == bytes(tb)
    assert tile_bytes(window._doc.pixel_data, 2) == tile_bytes(original, 2)


# -- image import ----------------------------------------------------------
def _fill_png(path, window, index: int, width: int, height: int) -> str:
    """Write a solid PNG in the color the view's palette holds at ``index``."""
    from PySide6.QtGui import QImage

    image = QImage(width, height, QImage.Format.Format_ARGB32)
    image.fill(window._doc.palette.color(index))
    assert image.save(str(path), "PNG")
    return str(path)


def test_paste_of_an_odd_sized_image_keeps_the_pixels_it_never_covered(
    qtbot, tmp_path
) -> None:
    from PySide6.QtGui import QGuiApplication, QImage

    from celpix.pipeline import pipeline

    window = _open_pixels(qtbot, tmp_path)
    before = pipeline.decode_tiles(window._doc, window._registry, 0, 2)
    # A tile and a half wide: the second tile's right half is not the image's to
    # write, and padding it black would erase art the source never spoke for.
    image = QImage(12, 8, QImage.Format.Format_ARGB32)
    image.fill(window._doc.palette.color(5))
    QGuiApplication.clipboard().setImage(image)

    window._select_tiles(0, 0)
    window._paste()

    after = pipeline.decode_tiles(window._doc, window._registry, 0, 2)
    assert set(after[0].data) == {5}
    for y in range(8):
        for x in range(4):
            assert after[1].get(x, y) == 5
        for x in range(4, 8):
            assert after[1].get(x, y) == before[1].get(x, y)


def test_import_png_from_the_files_list_lands_at_the_start_of_the_file(
    qtbot, tmp_path, monkeypatch
) -> None:
    from PySide6.QtWidgets import QFileDialog

    from celpix.pipeline import pipeline

    window = _open_big(qtbot, tmp_path, monkeypatch, tiles=64)
    window._columns.setValue(8)
    window._rows.setValue(4)
    original = window._doc.pixel_data
    png = _fill_png(tmp_path / "sprite.png", window, 3, 16, 16)
    monkeypatch.setattr(
        QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (png, ""))
    )

    window._nav_rows(2)  # the view is elsewhere in the file …
    assert window._offset > 0
    window._import_png_into(window._workspace.current)

    # … but the import goes to tile 0 and brings the view back to see it. The
    # 2×2-tile image lands as a block, not as a run of four tiles.
    assert window._offset == 0
    tiles = pipeline.decode_tiles(window._doc, window._registry, 0, 10)
    for index in (0, 1, 8, 9):
        assert set(tiles[index].data) == {3}
    tb = window._doc.bytes_per_tile
    assert window._doc.pixel_data[2 * tb : 3 * tb] == original[2 * tb : 3 * tb]

    window._undo_stack.undo()
    assert window._doc.pixel_data == original


def test_import_png_from_the_canvas_lands_on_the_selection(
    qtbot, tmp_path, monkeypatch
) -> None:
    from PySide6.QtWidgets import QFileDialog

    from celpix.pipeline import pipeline

    window = _open_pixels(qtbot, tmp_path)
    before = pipeline.decode_tiles(window._doc, window._registry, 5, 2)
    png = _fill_png(tmp_path / "half.png", window, 5, 12, 8)
    monkeypatch.setattr(
        QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (png, ""))
    )

    window._select_tiles(5, 5)
    window._import_png_here()

    after = pipeline.decode_tiles(window._doc, window._registry, 5, 2)
    assert set(after[0].data) == {5}
    # Same partial-tile rule as paste: the half the image didn't reach is the
    # file's own pixels, not padding.
    for y in range(8):
        assert after[1].get(0, y) == 5
        assert after[1].get(7, y) == before[1].get(7, y)


def test_dropped_png_imports_onto_the_selection_instead_of_opening(qtbot, tmp_path):
    """A PNG is picture data, not a binary to read graphics out of: dropping one
    imports it where the canvas menu would, and never joins the files list."""
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QDropEvent

    from celpix.pipeline import pipeline

    window = _open_pixels(qtbot, tmp_path)
    png = _fill_png(tmp_path / "sprite.png", window, 5, 8, 8)
    window._select_tiles(5, 5)

    mime = _drag_payload(png)  # must outlive the event, or Qt reads freed memory
    event = QDropEvent(
        QPointF(10, 10),
        Qt.DropAction.CopyAction,
        mime,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    window.dropEvent(event)

    assert len(window._workspace.entries) == 1  # the PNG did not become an entry
    imported = pipeline.decode_tiles(window._doc, window._registry, 5, 1)
    assert set(imported[0].data) == {5}


def test_a_solid_block_of_cells_gets_one_outline() -> None:
    from celpix.ui.canvas import Canvas

    # A rectangle selection is one shape on screen and must read as one box.
    assert Canvas._solid_block({0: [2, 3], 1: [2, 3]}) == (2, 0, 2, 2)
    assert Canvas._solid_block({4: [0, 1, 2]}) == (0, 4, 3, 1)
    # Anything ragged has no single box: rows that don't line up, a hole in a
    # row, or a skipped row all fall back to per-row outlines.
    assert Canvas._solid_block({0: [1, 2], 1: [0, 1, 2]}) is None
    assert Canvas._solid_block({0: [0, 2]}) is None
    assert Canvas._solid_block({0: [0], 2: [0]}) is None
    assert Canvas._solid_block({}) is None
