"""The files list and its entries: opening, slicing,
reordering, containers, missing files, and the project they live in."""

from __future__ import annotations

import json
from pathlib import Path

from celpix.plugins.base import RAW_CONTAINER
from celpix.project.workspace import PaletteMode
from celpix.ui.main_window import MainWindow
from uihelpers import (
    _drag_payload,
    _make_snes_file,
    _pattern_name,
    _scr_file,
    _section_names,
    _select_2d_pattern,
)

# QDropEvent stores only a *pointer* to its mime data — the real drag source owns
# it through the drop — so a temporary would dangle and the read segfaults. Held
# here for the process's lifetime, which is what a test's drops cost.
_DROP_MIME_KEEPALIVE: list = []


def _drop_event(*paths, ctrl: bool = False):
    """A drop carrying ``paths``, optionally with Ctrl held.

    The modifiers come from the *event* rather than the application's current
    state: Qt reports the latter as of the last input event, which after an
    unrelated modifier+key elsewhere in a suite is not what this drop meant.
    """
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QDropEvent

    mime = _drag_payload(*paths)
    _DROP_MIME_KEEPALIVE.append(mime)
    mods = (
        Qt.KeyboardModifier.ControlModifier if ctrl else Qt.KeyboardModifier.NoModifier
    )
    return QDropEvent(
        QPointF(10, 10),
        Qt.DropAction.CopyAction,
        mime,
        Qt.MouseButton.LeftButton,
        mods,
    )


def _entry_rows(panel):
    """Every entry row, in on-screen order, across the list's sections.

    Top-level rows are the Pixels/Tilemaps/Palettes headings, not entries
    (``file_list_panel.SECTIONS``), so a test after "the first file" has to look
    one level down. Flattened rather than per-section because that is the order
    the user reads.
    """
    tree = panel._tree
    rows = []
    for i in range(tree.topLevelItemCount()):
        section = tree.topLevelItem(i)
        rows.extend(section.child(j) for j in range(section.childCount()))
    return rows


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


def test_entry_history_walks_the_visit_trail(qtbot, tmp_path) -> None:
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent

    a = _make_snes_file(tmp_path)  # 8 tiles of 32 bytes
    b = tmp_path / "b.4bpp.sfc"
    b.write_bytes(bytes((i * 7 + 3) & 0xFF for i in range(32 * 8)))
    window = MainWindow()
    qtbot.addWidget(window)

    window._load_pixel(str(a))
    window._load_pixel(str(b))
    entry_a, entry_b = window._workspace.entries
    slice_a = window._workspace.add_slice(str(a), "gfx", 32, 32)
    window._activate_entry(slice_a)
    assert window._history == [entry_a, entry_b, slice_a]

    # Walking the trail moves the view without rewriting it.
    window._history_step(-1)
    window._history_step(-1)
    assert window._workspace.current is entry_a
    assert window._history == [entry_a, entry_b, slice_a]
    assert not window._back_action.isEnabled()  # at the oldest visit
    window._history_step(1)
    assert window._workspace.current is entry_b

    # The back/forward mouse buttons drive the same steps and are consumed whole,
    # press through release; a left click is none of this module's business.
    def click(button, kind=QEvent.Type.MouseButtonPress):
        return QMouseEvent(
            kind,
            QPointF(0, 0),
            QPointF(0, 0),
            button,
            button,
            Qt.KeyboardModifier.NoModifier,
        )

    assert window._handle_history_mouse(click(Qt.MouseButton.ForwardButton))
    assert window._workspace.current is slice_a
    assert window._handle_history_mouse(click(Qt.MouseButton.BackButton))
    assert window._workspace.current is entry_b
    assert window._handle_history_mouse(
        click(Qt.MouseButton.BackButton, QEvent.Type.MouseButtonRelease)
    )
    assert window._workspace.current is entry_b  # the release only gets swallowed
    assert not window._handle_history_mouse(click(Qt.MouseButton.LeftButton))

    # Visiting somewhere new from the middle drops what lay ahead.
    window._activate_entry(entry_a)
    assert window._history == [entry_a, entry_b, entry_a]
    assert not window._forward_action.isEnabled()

    # Closing an entry takes its visits with it, and the two visits to a that
    # this leaves either side of the gap collapse into one.
    window._workspace.close(entry_b)
    assert window._history == [entry_a]
    assert not window._back_action.isEnabled()
    assert not window._forward_action.isEnabled()


def test_slice_entry_views_bounded_region_with_view_relative_addresses(
    qtbot, tmp_path
) -> None:
    px = _make_snes_file(tmp_path)  # 8 tiles of 32 bytes
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(px))

    entry = window._workspace.add_slice(str(px), "gfx", 64, 64)  # tiles 2..3
    window._activate_entry(entry)
    assert window._doc.tile_count == 2
    # A slice's addresses count from its own first byte, so the top of the view
    # reads 0 however far into the parent the region sits — while the offset it
    # is anchored at (what a palette offset or a jump-to-source speaks in) stays
    # the parent's.
    assert window._offset_text() == "0x000000"
    assert window._anchor_base() == 64
    assert not window._change_container_action.isEnabled()
    assert window._write_action.isEnabled()
    # Slices never nest: a slice on screen offers no slice-creation actions.
    window._on_slots_selected(0, 0)  # a selection can't unlock from-selection
    assert not window._new_slice_action.isEnabled()
    assert not window._new_slice_from_view_action.isEnabled()
    assert not window._new_slice_from_selection_action.isEnabled()

    # Switching back to the parent shows the whole file from its own state, and
    # the file *does* spawn slices.
    window._activate_entry(window._workspace.entries[0])
    assert window._doc.tile_count == 8
    assert window._change_container_action.isEnabled()
    assert window._new_slice_action.isEnabled()
    assert window._new_slice_from_view_action.isEnabled()


def test_slice_addresses_read_view_relative_while_offsets_stay_anchored(
    qtbot, tmp_path
) -> None:
    """A typed address moves within the slice; the numbers written down don't.

    The two coordinate spaces meet on a slice, and the regression to fear is one
    leaking into the other: a jump interpreted as a parent offset would land two
    regions away, and a palette offset taken from the box would read the top of
    the file instead of the bytes beside the graphics.
    """
    px = _make_snes_file(tmp_path)  # 8 tiles of 32 bytes
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(px))
    entry = window._workspace.add_slice(str(px), "gfx", 64, 64)  # tiles 2..3
    window._activate_entry(entry)
    window._columns.setValue(1)  # one tile a page, so the second is scrolled to
    window._rows.setValue(1)

    window._address_edit.setText("0x20")  # the *slice's* second tile
    window._address_edit.commit()
    assert window._offset == 1
    assert window._offset_text() == "0x000020"
    # That same tile is anchored at parent byte 0x60, which is what an Offset
    # palette stores - it reaches outside the slice by design.
    assert window._initial_palette_offset() == 0x60
    # Jump to Source hands over the child's stored offset, in those coordinates:
    # landing on it from the parent must reach the region, not byte 0x40 of it.
    window._activate_entry(window._workspace.entries[0])
    window._columns.setValue(1)
    window._rows.setValue(1)
    window._land_on_byte(entry.slice_offset)
    assert window._offset == 2
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
    assert window.windowTitle() == "celPix - yoshi gfx"

    # Cancel (Escape): nothing changes and the label is restored. The first
    # editor may still await deleteLater — take the newest one.
    panel._begin_rename(entry)
    editor = panel._tree.findChildren(QLineEdit)[-1]
    editor.setText("discarded")
    qtbot.keyClick(editor, Qt.Key.Key_Escape)
    qtbot.waitUntil(lambda: panel._editing is None)
    assert entry.name == "yoshi gfx"
    assert panel._items[entry].text(0) == "yoshi gfx"

    # A file renames the same way — its basename is only the name it opens
    # under — and the label keeps the container hint the name is shown with.
    file_entry = window._workspace.entries[0]
    panel._begin_rename(file_entry)
    editor = panel._tree.findChildren(QLineEdit)[-1]
    assert editor.text() == file_entry.name  # the bare name, no hints to edit
    editor.setText("tileset ROM")
    qtbot.keyClick(editor, Qt.Key.Key_Return)
    qtbot.waitUntil(lambda: file_entry.name == "tileset ROM")
    assert panel._items[file_entry].text(0) == "tileset ROM"

    # A palette renames too — a numbered .pal says nothing about which scene it
    # colours — and the file it points at stays in the tooltip, which is what
    # keeps a renamed palette traceable to its bytes.
    palette = window._workspace.add_palette(str(tmp_path / "colors.pal"), None)
    panel._begin_rename(palette)
    editor = panel._tree.findChildren(QLineEdit)[-1]
    editor.setText("cave BG")
    qtbot.keyClick(editor, Qt.Key.Key_Return)
    qtbot.waitUntil(lambda: palette.name == "cave BG")
    assert panel._items[palette].text(0) == "cave BG"
    assert str(tmp_path / "colors.pal") in panel._items[palette].toolTip(0)


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
        staticmethod(lambda *_a, **_k: SliceParams("b", 200, 32, "compression.none")),
    )
    window._edit_slice(b)
    assert offsets() == sorted(offsets())
    assert panel._offset_of(file_item.child(file_item.childCount() - 1)) == 200


def test_shift_arrows_reorder_the_files_and_undo_puts_them_back(
    qtbot, tmp_path
) -> None:
    # Shift+Up/Down resize the view window everywhere else; with the list focused
    # they reorder the files instead (the navigation filter defers to the tree).
    # Both the model list and the tree rows have to move, and the file's slices
    # have to stay nested under it rather than being left behind.
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    first = _make_snes_file(tmp_path)
    second = tmp_path / "other.4bpp.sfc"
    second.write_bytes(bytes((i * 7 + 3) & 0xFF for i in range(32 * 8)))
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(first))
    window._load_pixel(str(second))
    a = window._workspace.find_file(str(first))
    b = window._workspace.find_file(str(second))
    cut = window._workspace.add_slice(str(first), "cut", 64, 64)
    panel = window._files_panel

    def file_rows() -> list[str]:
        return [row.text(0) for row in _entry_rows(panel)]

    window.show()
    QApplication.setActiveWindow(window)
    tree = panel._tree
    # Highlight first, focus second: activating an entry hands focus to the
    # canvas, and the list only claims the keys while it holds focus itself.
    tree.setCurrentItem(panel._items[a])
    tree.setFocus()

    qtbot.keyClick(tree, Qt.Key.Key_Down, Qt.KeyboardModifier.ShiftModifier)
    assert window._workspace.entries == [b, a, cut]  # the slice travelled along
    assert file_rows() == [b.name, a.name]
    assert panel._items[cut].parent() is panel._items[a]
    assert tree.currentItem() is panel._items[a]  # the moved row keeps the highlight

    # Already last: the key does nothing rather than pushing an empty undo step.
    depth = window._undo_stack.count()
    qtbot.keyClick(tree, Qt.Key.Key_Down, Qt.KeyboardModifier.ShiftModifier)
    assert window._undo_stack.count() == depth
    assert file_rows() == [b.name, a.name]

    window._undo_stack.undo()
    assert window._workspace.entries == [a, cut, b]
    assert file_rows() == [a.name, b.name]

    # A slice has no hand order to change - its offset places it.
    tree.setCurrentItem(panel._items[cut])
    tree.setFocus()
    qtbot.keyClick(tree, Qt.Key.Key_Up, Qt.KeyboardModifier.ShiftModifier)
    assert window._workspace.entries == [a, cut, b]

    # The context-menu path, with that slice still the shown row: it leaves the
    # tree along with its parent, so it has to come back nested and highlighted.
    panel.move_requested.emit(a, 1)
    assert window._workspace.entries == [b, a, cut]
    assert panel._items[cut].parent() is panel._items[a]
    assert tree.currentItem() is panel._items[cut]


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
    # A drop is an open like any other, so it heads the Open Recent list. Qt
    # hands back drop URLs with POSIX separators, so what is stored is the
    # normalized path, not the spelling the drop arrived in.
    from celpix.ui.widgets import _recent_path, load_recent_projects

    assert load_recent_projects()[0] == _recent_path(str(project))


def test_slice_carved_from_a_reshaped_view_reads_what_was_on_screen(
    qtbot, tmp_path, monkeypatch
) -> None:
    """Under an active reshape the view shows the reordered region on 0-based
    addresses, and a slice carved from it comes back holding exactly those
    bytes.

    The two halves are one rule: a slice offset is a position in its parent's
    own coordinates, which under a reshape are the reordered buffer's - the same
    ones the view displays. So the carve must round-trip, and the regression to
    fear is it silently reading the *file* at that number instead, which decodes
    to plausible-looking garbage (docs/design/reshape-stage.md §3).
    """
    from celpix.core.context import PipelineContext
    from celpix.plugins.builtins.split_planes import SplitPartsReshape
    from celpix.ui.slice_dialog import SliceDialog, SliceParams

    data = bytes((i * 13 + 1) & 0xFF for i in range(1024))
    px = tmp_path / "pair.4bpp.sfc"
    px.write_bytes(data)

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(px))
    entry = window._workspace.current

    # What _apply_container_edit does once the dialog returns a reshape.
    entry.reshape_id = "reshape.split-planes-2"
    window._capture_session()
    window._workspace.drop_document(entry)
    window._on_current_entry_changed(entry)

    joined = SplitPartsReshape(2).reshape(data, PipelineContext())
    assert bytes(window._doc.pixel_data) == joined
    assert window._anchor_base() == 0  # positions are the buffer's, not the file's

    window._columns.setValue(4)
    window._rows.setValue(2)  # a page of 8 SNES tiles = 256 bytes
    window._nav_rows(1)  # ...starting one row in, at buffer byte 128
    monkeypatch.setattr(
        SliceDialog,
        "get_slice",
        staticmethod(
            lambda *_a, **kw: SliceParams(
                "cut", kw["offset"], kw["length"], kw["compression_id"]
            )
        ),
    )
    window._new_slice_from_view()

    cut = window._workspace.entries[-1]
    assert (cut.slice_offset, cut.slice_length) == (128, 256)
    window._workspace.set_current(cut)
    assert bytes(window._doc.pixel_data) == joined[128:384]
    assert joined[128:384] != data[128:384]  # the file at that offset is other bytes


def test_slice_of_a_reshaped_two_chip_region_writes_back_through_its_parent(
    qtbot, tmp_path
) -> None:
    """Editing a slice inside a reordered region saves, and its bytes land
    scattered across *both* chips the way the reshape says they must.

    The slice's own bounds name a position in the parent's joined buffer, not in
    any file, so it cannot be deposited at them: the edit is spliced into the
    parent's buffer and the parent's write carries the whole region back through
    ``unshape``. The regression this guards is the direct deposit — writing the
    slice's bytes contiguously at that offset in the first chip, which both
    corrupts that chip and leaves the second one untouched.
    """
    from celpix.core.context import PipelineContext
    from celpix.plugins.base import RAW_CONTAINER
    from celpix.plugins.builtins.split_planes import SplitPartsReshape
    from celpix.ui.container_dialog import ContainerEdit

    first, second = tmp_path / "a.4bpp.sfc", tmp_path / "b.bin"
    chip_a = bytes((i * 7 + 3) & 0xFF for i in range(512))
    chip_b = bytes((i * 11 + 5) & 0xFF for i in range(512))
    first.write_bytes(chip_a)
    second.write_bytes(chip_b)

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(first))
    parent = window._workspace.current
    paths = (str(first), str(second))
    window._apply_container_edit(
        parent, ContainerEdit(RAW_CONTAINER, paths, "reshape.split-planes-2")
    )
    reshape = SplitPartsReshape(2)
    joined = reshape.reshape(chip_a + chip_b, PipelineContext())
    assert bytes(window._doc.pixel_data) == joined

    cut = window._workspace.add_slice(parent.path, "cut", 128, 64)
    window._workspace.set_current(cut)
    assert window._doc.pixel_config.write_enabled  # writable — through the parent
    edit = b"\xa5" * 16
    window._doc.replace_bytes(0, edit)
    window._workspace.set_pixel_revision(cut, window._workspace.next_revision())

    assert window._write_entry(cut)

    # The joined region reads back with the edit in place...
    expected = joined[:128] + edit + joined[144:]
    assert (
        reshape.reshape(first.read_bytes() + second.read_bytes(), PipelineContext())
        == expected
    )
    # ...which took *both* chips to express: split-planes-2 lays alternate bytes
    # on each, so a 16-byte edit is 8 bytes in each file, not 16 in the first.
    assert first.read_bytes() != chip_a
    assert second.read_bytes() != chip_b
    assert not cut.pixel_dirty and not parent.pixel_dirty


def test_a_sibling_slice_reads_an_edit_the_fold_has_not_run_for_yet(
    qtbot, tmp_path
) -> None:
    """The invariant that lets the fold be lazy. An edit through a slice records
    what the parent owes rather than re-encoding on the spot — re-encoding is the
    whole cost, and on a compressed slice it is a search no stroke can afford — so
    the parent's buffer is briefly *behind* its slices.

    It may never be observed that way. Everything that reads those bytes settles
    the region first, and a **sibling over the same window** is the sharpest
    reader: a dirty parent hands its buffer to a slice's read
    (``workspace._parent_view_bytes``), so a sibling loading here sees either the
    edit or the bytes it replaced, with nothing in between.
    """
    rom = tmp_path / "rom.bin"
    rom.write_bytes(bytes((i * 7) & 0xFF for i in range(0x800)))
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(rom))
    parent = window._workspace.current

    edited = window._workspace.add_slice(parent.path, "first", 0x100, 0x100)
    window._workspace.set_current(edited)
    window._apply_pixel_bytes(
        [(0, b"\x5a" * 32)],
        window._workspace.next_revision(),
        window._workspace.next_revision(),  # the parent's, so it reads dirty too
        entry=edited,
    )
    assert parent.pixel_dirty  # ...which is what sends a sibling to its buffer
    # The debt stands: nothing has needed the parent's bytes yet.
    assert edited in parent.pending_folds
    assert bytes(parent.doc.pixel_data[0x100:0x120]) != b"\x5a" * 32

    # A sibling over the same region loads now, and settles it on the way.
    sibling = window._workspace.add_slice(parent.path, "second", 0x100, 0x100)
    window._activate_entry(sibling)

    assert not parent.pending_folds
    assert bytes(sibling.doc.pixel_data[:32]) == b"\x5a" * 32
    assert bytes(parent.doc.pixel_data[0x100:0x120]) == b"\x5a" * 32


def test_a_slice_edit_reaches_its_parent_and_the_parents_container(
    qtbot, tmp_path
) -> None:
    """A slice is a region *of* a file, so an edit made through it is an edit to
    that file: the file's own view shows it, and saving goes out through the
    file's container rather than around it.

    The container half is the sharp regression. A slice used to deposit raw bytes
    at its own bounds, which skipped the parent container's write - so editing a
    slice of a Game Boy ROM left the header checksums describing the *old* bytes
    and the ROM failed its own boot check. `container.gb-rom` exists only to
    repair those on write, which makes it the honest witness for "did the
    parent's container run?".
    """
    from celpix.plugins.builtins.gb_rom import repair_checksums
    from celpix.ui.container_dialog import ContainerEdit

    rom = tmp_path / "game.gb"
    rom.write_bytes(repair_checksums(bytes((i * 7) & 0xFF for i in range(0x8000))))
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(rom))
    parent = window._workspace.current
    window._apply_container_edit(parent, ContainerEdit("container.gb-rom", (str(rom),)))

    cut = window._workspace.add_slice(parent.path, "gfx", 0x4000, 0x100)
    window._workspace.set_current(cut)
    window._apply_pixel_bytes(
        [(0, b"\x5a" * 32)], window._workspace.next_revision(), entry=cut
    )

    # The file's own view holds the edit made through the slice - same bytes.
    window._workspace.set_current(parent)
    assert bytes(window._doc.pixel_data[0x4000:0x4020]) == b"\x5a" * 32

    window._workspace.set_current(cut)
    assert window._write_entry(cut)
    written = rom.read_bytes()
    assert written[0x4000:0x4020] == b"\x5a" * 32
    # The parent's container ran: a raw deposit would leave these stale.
    assert written == repair_checksums(written)
    assert not cut.pixel_dirty and not parent.pixel_dirty


def test_container_notices_land_in_the_row_tooltip(qtbot, tmp_path) -> None:
    """A non-fatal container notice washes the entry's row and spells itself out
    in that row's tooltip — the one place a user already looks to ask what is
    wrong with a row, alongside the missing-file lines.

    The row is the part that needs a test: the panel refreshes an entry when it is
    *added*, which is before its document exists and so before it has any notices
    to show — so this only works if something refreshes it after the load.
    """
    from PySide6.QtCore import Qt

    # A CHR-RAM cart declares zero CHR banks: the read succeeds, but what it
    # hands back is program code rather than tiles.
    chr_ram = tmp_path / "chrram.nes"
    chr_ram.write_bytes(
        bytes([*b"NES\x1a", 2, 0, 0, 0])
        + bytes(8)
        + bytes((i * 7) & 0xFF for i in range(0x8000))
    )
    clean = tmp_path / "clean.bin"
    clean.write_bytes(bytes((i * 13) & 0xFF for i in range(4096)))

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(chr_ram))

    row = _entry_rows(window._files_panel)[0]
    assert row.background(0).style() != Qt.BrushStyle.NoBrush  # amber
    tip = row.toolTip(0)
    assert "CHR-RAM cart: no tile data in this file" in tip  # the summary...
    assert "0 CHR banks" in tip  # ...and the explanation under it
    assert row.toolTip(1) == tip  # the glyph answers too, not just the name

    # An entry with nothing to report carries no wash and no extra tooltip lines.
    window._load_pixel(str(clean))
    plain = _entry_rows(window._files_panel)[1]
    # Not the amber: what this row carries is the open-entry tint, since loading
    # it made it the one on screen.
    assert plain.background(0).color() != row.background(0).color()
    assert plain.toolTip(0) == str(clean)
    # Switching back restores it, since it is derived rather than one-shot.
    window._activate_entry(window._workspace.entries[0])
    assert "CHR-RAM" in _entry_rows(window._files_panel)[0].toolTip(0)


def test_switching_entries_does_not_dirty_the_project(qtbot, tmp_path) -> None:
    """Which entry is shown is saved but is not a *change*: browsing to another
    file - to read it, or to take a palette off it - would otherwise put a save
    prompt in front of someone who only looked around. Anything that changes how
    an entry is set up still counts."""
    a = _make_snes_file(tmp_path)
    b = tmp_path / "b.4bpp.sfc"
    b.write_bytes(bytes((i * 7) & 0xFF for i in range(32 * 8)))
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(a))
    window._load_pixel(str(b))
    window._save_project_to(str(tmp_path / "p.celpix"))
    assert not window._project_is_dirty()

    window._activate_entry(window._workspace.entries[0])
    assert not window._project_is_dirty()
    # ...and the switch is still written, so reopening lands where it was left.
    assert window._project_snapshot()["current"] == 0

    window._columns.setValue(window._columns.value() + 1)
    assert window._project_is_dirty()


def test_the_shown_entry_stays_marked_after_the_selection_moves_off_it(
    qtbot, tmp_path
) -> None:
    """The list's own highlight follows the *selection*, which a click on a
    palette or a bookmark takes away - so which entry the canvas is showing is
    marked separately, and has to move with it rather than being painted once."""
    from PySide6.QtCore import Qt

    first = _make_snes_file(tmp_path)
    second = tmp_path / "second.bin"
    second.write_bytes(bytes((i * 7) & 0xFF for i in range(32 * 8)))
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(first))
    window._load_pixel(str(second))

    rows = _entry_rows(window._files_panel)[:2]
    assert rows[1].background(0).style() != Qt.BrushStyle.NoBrush
    assert rows[0].background(0).style() == Qt.BrushStyle.NoBrush

    # Back to the first: the mark moves rather than accumulating.
    window._activate_entry(window._workspace.entries[0])
    assert rows[0].background(0).style() != Qt.BrushStyle.NoBrush
    assert rows[1].background(0).style() == Qt.BrushStyle.NoBrush


def test_small_rom_sized_file_is_not_headered(qtbot, tmp_path) -> None:
    """A 512-byte `.sfc` is a tile sheet that happens to fit the copier
    arithmetic, not a header with nothing behind it — claiming it would hand the
    view an empty document."""
    px = tmp_path / "tiles.4bpp.sfc"
    px.write_bytes(bytes((i * 13 + 1) & 0xFF for i in range(512)))

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(px))

    assert window._workspace.current.container_id == RAW_CONTAINER
    assert len(window._doc.pixel_data) == 512


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

    window._compression.setCurrentIndex(window._compression.findData("compression.lz2"))
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


def test_scan_ui_thaw_does_not_arm_structure_actions(qtbot, tmp_path) -> None:
    from celpix.plugins.builtins import lz_command

    # Same layout as the jump/scan test: a structure, a junk region no scheme
    # accepts, another structure, padding. Jumping past the first structure
    # lands in the junk, where the overlay is hidden and both Jump-to-Next and
    # promote-to-slice are off.
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
    window._columns.setValue(4)
    window._rows.setValue(4)
    window._compression.setCurrentIndex(window._compression.findData("compression.lz2"))
    window._set_byte_position(len(packed_a))  # into the junk: no structure here
    assert not window._overlay.isVisible()
    assert not window._jump_next.isEnabled()
    assert not window._promote_button.isEnabled()

    # A scan freezes the whole UI then thaws it. The blanket re-enable on thaw
    # must restore Jump / promote from the overlay's structure state, not switch
    # them on just because a scan ended (regression: a scan landing back on this
    # same offset never re-refreshed, so they were left wrongly enabled).
    window._set_scan_ui(True)
    window._set_scan_ui(False)

    assert not window._jump_next.isEnabled()
    assert not window._promote_button.isEnabled()


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


def test_slice_dialog_bounds_offsets_by_the_whole_joined_region(
    qtbot, tmp_path
) -> None:
    """A region spread over several ROM chips is addressed as the concatenation,
    so the dialog's end-of-region check is their combined size.

    Bounding by the first chip alone is the regression: it refuses every offset
    past the first file as running off the end, putting most of a multi-chip
    region out of reach with a message that looks like the user's arithmetic
    was wrong.
    """
    from celpix.plugins.registry import default_registry
    from celpix.ui.slice_dialog import SliceDialog

    first, second = tmp_path / "a.bin", tmp_path / "b.bin"
    first.write_bytes(bytes(0x100))
    second.write_bytes(bytes(0x100))
    registry = default_registry()
    paths = (str(first), str(second))

    def validated(offset: int, length: int):
        dialog = SliceDialog(registry, paths=paths, offset=offset, length=length)
        qtbot.addWidget(dialog)
        dialog._validate_and_accept()
        return dialog._params

    assert validated(0x180, 0x40).offset == 0x180  # in the second chip
    assert validated(0x1F0, 0x40) is None  # past the joined end, still refused


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

    window._on_slots_selected(1, 5)  # a drag spanning slots 1..5
    assert (window._selected_tile, window._selected_last) == (1, 5)
    assert window._canvas._selected_slots == {1, 2, 3, 4, 5}
    assert window._new_slice_from_selection_action.isEnabled()
    # A drag reaching into blank padding clamps to the tiles that exist; the
    # anchor order doesn't matter.
    window._on_slots_selected(12, 6)
    assert (window._selected_tile, window._selected_last) == (6, 7)

    window._on_slots_selected(1, 5)
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


def test_delete_key_removes_entry_even_with_a_tile_selection(
    qtbot, tmp_path, monkeypatch
) -> None:
    # The Delete key in the files list must remove the highlighted entry. It
    # regressed when a tile selection was active: the canvas's window-wide
    # Clear (also Delete) overloaded ambiguously with the list's remove, and Qt
    # fired neither - so Delete silently did nothing. The list now claims the
    # key while focused, so it works regardless of the canvas selection.
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QMessageBox

    from celpix.plugins.builtins import lz_command
    from celpix.project.workspace import EntryKind

    tiles = bytes((i * 29 + 5) & 0xFF for i in range(32 * 4))
    px = tmp_path / "packed.bin"
    px.write_bytes(lz_command.compress(tiles, big_endian_offsets=True) + bytes(64))

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(px))
    # A decompressed slice - the case the report was filed against.
    window._workspace.add_slice(str(px), "packed-slice", 0, None, "compression.lz2")
    slice_entry = next(
        e for e in window._workspace.entries if e.kind is EntryKind.SLICE
    )
    window._activate_entry(slice_entry)

    # A live tile selection arms the canvas's Clear (Delete) action - the
    # ingredient that used to make the list's Delete ambiguous.
    window._on_slots_selected(0, 0)
    window._sync_edit_actions()
    assert window._clear_action.isEnabled()

    monkeypatch.setattr(
        QMessageBox, "question", lambda *_a, **_k: QMessageBox.StandardButton.Yes
    )
    # A WindowShortcut only resolves in a shown, active window (see the undo
    # Ctrl+Z test); the list must hold focus for its own key handling.
    window.show()
    QApplication.setActiveWindow(window)
    tree = window._files_panel._tree
    tree.setFocus()
    tree.setCurrentItem(window._files_panel._items[slice_entry])

    qtbot.keyClick(tree, Qt.Key.Key_Delete)
    assert slice_entry not in window._workspace.entries


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
            lambda *_a, **_k: SliceParams("bigger", 32, 96, "compression.none")
        ),
    )
    window._edit_slice(entry)
    assert (entry.name, entry.slice_offset, entry.slice_length) == ("bigger", 32, 96)
    # The on-screen slice re-read the new region immediately - the view still
    # opens on its own byte 0, but it is anchored at the new offset now.
    assert window._doc is entry.doc
    assert window._doc.tile_count == 3
    assert window._anchor_base() == 32
    assert window._offset_text() == "0x000000"


def test_edit_slice_keeps_the_view_across_the_re_read(
    qtbot, tmp_path, monkeypatch
) -> None:
    # Re-pointing a slice re-reads it, but the arrangement is the entry's, not
    # the region's: a wide-bitmap view must come back on its own geometry rather
    # than the codec's default - and undo must put it back the same way.
    from celpix.ui.slice_dialog import SliceDialog, SliceParams

    px = tmp_path / "bitmap.bin"
    px.write_bytes(bytes((i * 7 + 3) & 0xFF for i in range(306 * 24 * 3)))
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(px))
    entry = window._workspace.add_slice(str(px), "gfx", 0, 306 * 16 * 3)
    window._activate_entry(entry)
    window._pixel_preset.setCurrentIndex(
        window._pixel_preset.findData("preset.pixel.dc-rgb888-be")
    )
    _select_2d_pattern(window)
    window._bitmap_width.setValue(306)
    assert (window._doc.tile_width, window._columns.value()) == (6, 51)

    monkeypatch.setattr(
        SliceDialog,
        "get_slice",
        staticmethod(
            lambda *_a, **_k: SliceParams("gfx", 0, 306 * 12 * 3, "compression.none")
        ),
    )
    window._edit_slice(entry)
    assert entry.slice_length == 306 * 12 * 3  # the region really did change
    assert window._bitmap_width.value() == 306
    assert window._two_d.isChecked()
    # The format the width was chosen for survives with it (the session snapshot
    # is only refreshed at capture points, and this is one).
    assert window._pixel_preset.currentData() == "preset.pixel.dc-rgb888-be"
    # The width reached the *load*, not just the widget: the tiles are re-cut.
    assert (window._doc.tile_width, window._columns.value()) == (6, 51)

    window._undo_stack.undo()
    assert entry.slice_length == 306 * 16 * 3
    assert window._bitmap_width.value() == 306
    assert (window._doc.tile_width, window._columns.value()) == (6, 51)


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
    # A non-default subpalette row: it picks which colors the tiles index, so a
    # slice that opened back on row 0 would render in the wrong ones.
    window._subpalette.setValue(3)
    # A non-default arrangement, likewise: it decides which bytes make up each
    # tile, so a slice back on Linear would show the same region scrambled.
    window._pattern.setCurrentIndex(
        window._pattern.findText(_pattern_name("genesis-sprite"))
    )

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
        staticmethod(lambda *_a, **_k: SliceParams("mine", 64, 64, "compression.none")),
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
    assert slice_entry.session.preview_compression_id == "compression.none"
    # The subpalette row and the arrangement ride on the view options rather than
    # the session, so they take a hand-off of their own to come across.
    assert slice_entry.doc.view.subpalette_row == 3
    assert window._subpalette.value() == 3
    view = slice_entry.doc.view
    assert (view.block_columns, view.block_rows, view.block_order) == (2, 2, "column")
    assert window._pattern.currentData().id == "genesis-sprite"

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


def test_jump_to_source_arms_the_slices_compression_preview(qtbot, tmp_path) -> None:
    # The parent reads raw, so a compressed slice's bytes are packed at its
    # address: the jump has to arm the preview combo with the codec that unpacks
    # them, or the user lands on the right offset with no way to see the tiles.
    px = _make_snes_file(tmp_path)
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(px))
    parent = window._workspace.find_file(str(px))

    slice_entry = window._workspace.add_slice(
        str(px), "packed", 64, 64, "compression.lz1"
    )
    window._jump_to_slice_source(slice_entry)

    assert window._workspace.current is parent
    assert window._compression_id() == "compression.lz1"
    # Preview only - the parent's own bytes are still read unpacked.
    assert parent.compression_id == "compression.none"


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
    window._sync_locate_action()
    assert window._locate_missing_action.isEnabled()

    # Restoring the file at the same path clears the missing state and disarms it.
    rom.write_bytes(bytes((i * 13 + 1) & 0xFF for i in range(32 * 8)))
    window._sync_locate_action()
    assert not window._locate_missing_action.isEnabled()


def test_project_swap_drops_the_whole_list_in_one_go(
    qtbot, tmp_path, monkeypatch
) -> None:
    """Replacing the workspace is one reset, not one removal per entry.

    The per-entry cascade made every listener pay its removal cost n times for
    a list about to be empty - most damagingly the Locate action's scan, which
    stats every referenced path and so probed the disk quadratically, freezing
    the UI for seconds on real paths.
    """
    from celpix.project import workspace as workspace_module

    window = MainWindow()
    qtbot.addWidget(window)
    for i in range(12):
        rom = tmp_path / f"r{i}.4bpp.sfc"
        rom.write_bytes(bytes(32 * 8))
        window._workspace.open_file(str(rom))
    window._activate_entry(window._workspace.entries[0])  # something in the trail
    assert window._history

    probes: list[str] = []
    real_exists = workspace_module.exists
    monkeypatch.setattr(
        workspace_module,
        "exists",
        lambda path: (probes.append(path), real_exists(path))[1],
    )
    window._workspace.replace([], None)

    # Nothing sweeps the list: the only probe left is the one row repainted as
    # it loses the highlight. The per-entry cascade cost 66 for these 12.
    assert len(probes) <= 1
    assert _entry_rows(window._files_panel) == []
    assert window._files_panel._sections == {}  # headers go with their rows
    assert window._history == [] and window._history_pos == -1


def test_locate_action_follows_a_close_and_its_undo(qtbot, tmp_path) -> None:
    """Closing the last missing entry disarms the action, and undoing that
    close - which puts the still-missing file back - re-arms it."""
    rom = _make_snes_file(tmp_path)
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(rom))
    entry = window._workspace.entries[0]
    rom.unlink()
    window._sync_locate_action()
    assert window._locate_missing_action.isEnabled()

    window._apply_close_entry(entry)
    assert not window._locate_missing_action.isEnabled()

    window._apply_restore_entries([(0, entry)], None)
    assert window._locate_missing_action.isEnabled()


def test_project_swap_clears_the_undo_history(qtbot, tmp_path, monkeypatch) -> None:
    """Loading a project, and starting a new one, both empty the undo stack.

    A command holds the entries it acted on, and a swap discards every one of
    them - undoing across it would reinstate an entry from the project the user
    just left into the one they just opened.
    """
    from PySide6.QtWidgets import QMessageBox

    _click_message_box(monkeypatch, QMessageBox.ButtonRole.DestructiveRole)
    px = _make_snes_file(tmp_path)
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(px))
    assert window._undo_stack.count()  # opening a file is undoable

    project = tmp_path / "p.celpix"
    window._save_project_to(str(project))
    window._load_project(str(project))
    assert window._undo_stack.count() == 0

    other = tmp_path / "t.4bpp.sfc"
    other.write_bytes(bytes(32 * 8))
    window._load_pixel(str(other))
    assert window._undo_stack.count()
    window._new_project()
    assert window._undo_stack.count() == 0


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


def test_unsaved_files_gate_offers_write_discard_cancel(
    qtbot, tmp_path, monkeypatch
) -> None:
    # The quit-time files prompt (via the shared unsaved-changes gate): three
    # buttons - Write Changes / Discard / Cancel - with Write Changes the
    # default. Cancel refuses, Discard proceeds without writing, Write writes.
    from PySide6.QtWidgets import QMessageBox

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_make_snes_file(tmp_path)))
    entry = window._workspace.current
    # Force the entry dirty without a real edit - the gate keys off dirtiness.
    window._workspace.set_pixel_revision(entry, window._workspace.next_revision())
    assert entry.pixel_dirty

    seen: dict = {}
    monkeypatch.setattr(
        QMessageBox,
        "exec",
        lambda self: (
            seen.update(
                labels={b.text().replace("&", "") for b in self.buttons()},
                default=self.defaultButton().text().replace("&", ""),
            )
            or 0
        ),
    )
    clicked = {"role": None}
    monkeypatch.setattr(
        QMessageBox,
        "clickedButton",
        lambda self: next(
            (b for b in self.buttons() if self.buttonRole(b) == clicked["role"]), None
        ),
    )

    def quit_gate() -> bool:
        return window._resolve_dirty_entries(
            "Quitting discards unsaved changes to",
            write_label="Write Changes",
            skip_label="Discard",
            default_write=True,
        )

    # Cancel: refuse, and the button set / default are exactly as specified.
    clicked["role"] = QMessageBox.ButtonRole.RejectRole
    assert quit_gate() is False
    assert seen["labels"] == {"Write Changes", "Discard", "Cancel"}
    assert seen["default"] == "Write Changes"
    assert entry.pixel_dirty  # nothing written

    # Discard: proceed, still without writing.
    clicked["role"] = QMessageBox.ButtonRole.DestructiveRole
    assert quit_gate() is True
    assert entry.pixel_dirty

    # Write Changes: writes to disk first, so the entry goes clean and we proceed.
    clicked["role"] = QMessageBox.ButtonRole.AcceptRole
    assert quit_gate() is True
    assert not entry.pixel_dirty


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
    assert any(title == "celPix - locate" for title, _msg in captured_alerts)


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

    # The row names *which* reference is gone. Its ROM is on disk, so wording that
    # only said "referenced file is missing" would send the user hunting for a
    # graphic that never moved.
    tip = _entry_rows(window._files_panel)[0].toolTip(0)
    assert "Palette file is missing" in tip
    assert str(pal) in tip  # the path, which is nowhere else in the tooltip
    assert "File is missing" not in tip  # the entry's own file is fine


def test_write_saves_the_graphic_and_the_file_palette_it_shows(qtbot, tmp_path) -> None:
    """Ctrl+W saves what is on screen - which includes the .pal being rendered.

    A file palette is owned by its own entry, so a color edit dirties that and
    not the graphic; without this, Write would silently leave the colors unsaved
    and only the Files list could reach them.
    """
    from celpix.project.workspace import EntryKind

    pal = tmp_path / "shared.pal"
    pal.write_bytes(bytes(32))
    px = _make_snes_file(tmp_path)
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(px))
    window._add_palette_file(str(pal))
    entry = next(e for e in window._workspace.entries if e.kind is EntryKind.PALETTE)
    window._use_palette_entry(entry)
    graphic = window._workspace.current

    window._palette_panel._select(0)
    window._on_color_changed(0xFFFFFFFF)
    assert entry.palette_dirty and not graphic.pixel_dirty

    window._write_current()
    assert not entry.palette_dirty  # the .pal went with the graphic...
    assert pal.read_bytes() != bytes(32)
    assert entry.name in window.statusBar().currentMessage()  # ...and is named

    # A clean palette is left alone rather than having its mtime bumped: it is a
    # shared file, and there is nothing of it to save.
    stamp = pal.stat().st_mtime_ns
    window._write_current()
    assert pal.stat().st_mtime_ns == stamp
    assert entry.name not in window.statusBar().currentMessage()


def test_new_project_returns_the_window_to_the_launch_state(qtbot, tmp_path) -> None:
    """New Project is the project-load path onto an empty workspace, so
    everything tied to the old session goes with it - not just the list. A kept
    undo stack would point at discarded entries, and a kept project path would
    aim the next Save at the project that was just closed.
    """
    rom = _make_snes_file(tmp_path)
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(rom))
    window._workspace.add_slice(str(rom), "slice", 64, 64)
    window._workspace.hidden_pixel_presets = {"snes-2bpp"}
    # Saved first, so neither the unsaved-project nor the unsaved-bytes gate has
    # anything to ask about (both are covered by the load path's own tests).
    window._save_project_to(str(tmp_path / "hack.celpix"))
    assert window._undo_stack.count()  # opening the file was a command

    window._new_project()

    assert window._workspace.entries == []
    assert window._workspace.current is None
    assert window._doc is None
    assert window._undo_stack.count() == 0
    assert window._project_path is None and window._saved_project is None
    assert window._workspace.hidden_pixel_presets == set()
    # A clean slate rather than a half-closed one: the next open behaves as it
    # would on a fresh launch.
    window._load_pixel(str(rom))
    assert window._doc is not None


# -- export (docs/design/export.md) ----------------------------------------
def test_export_png_is_indexed_and_round_trips_to_disk(qtbot, tmp_path, monkeypatch):
    """The exported image is a true indexed PNG, and what reaches disk is it.

    Checked as one export because the in-memory image and the written file are two
    halves of one operation - separating them would build a second window to
    re-export the same eight tiles.
    """
    from PySide6.QtGui import QImage
    from PySide6.QtWidgets import QFileDialog

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

    out = tmp_path / "sheet.png"
    monkeypatch.setattr(
        QFileDialog, "getSaveFileName", lambda *a, **k: (str(out), "PNG image (*.png)")
    )
    window._export_png(window._workspace.current)

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


def test_export_acts_on_the_entry_it_is_handed(qtbot, tmp_path, monkeypatch):
    """Export targets the entry whose menu was opened, not the current view - it
    loads a never-activated one on demand and leaves the view where it was - and
    the Raw pathway writes decoded bytes rather than an image."""
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

    # Raw export of the parent: an uncompressed file's decoded bytes are its
    # bytes verbatim.
    dump = tmp_path / "dump.bin"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(dump), ""))
    window._export_raw(parent)
    assert dump.read_bytes() == sheet.read_bytes()


def test_window_title_names_project_and_marks_it_unsaved(qtbot, tmp_path):
    window = MainWindow()
    qtbot.addWidget(window)
    px = _make_snes_file(tmp_path)
    window._load_pixel(str(px))
    # No project yet: the title names the current graphic, and carries no
    # unsaved marker - there is no project file to be unsaved against.
    assert window.windowTitle() == f"celPix - {px.name}"
    assert not window.isWindowModified()

    # Saving gives the session a project file: the title names it (with Qt's
    # [*] marker placeholder) and reads clean.
    project = tmp_path / "session.celpix"
    window._save_project_to(str(project))
    assert window.windowTitle() == "celPix - session.celpix[*]"
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


def test_open_recent_lists_projects_newest_first_and_prunes_a_missing_one(
    qtbot, tmp_path, captured_alerts
) -> None:
    """The recent list is settings-backed, so it has to survive the window: a
    fresh one shows what an earlier session opened, newest first and with no
    duplicate for the project it reopened. A row whose file has gone is dropped
    on use - nothing else ever notices."""
    from celpix.ui.widgets import clear_recent_projects, load_recent_projects

    clear_recent_projects()
    px = _make_snes_file(tmp_path)
    first = MainWindow()
    qtbot.addWidget(first)
    first._load_pixel(str(px))
    one = tmp_path / "one.celpix"
    two = tmp_path / "two.celpix"
    first._save_project_to(str(one))
    first._save_project_to(str(two))
    first._load_project(str(one))  # reopening moves it up rather than repeating

    window = MainWindow()
    qtbot.addWidget(window)
    window._sync_recent_menu()
    assert [a.toolTip() for a in window._recent_menu.actions()[:2]] == [
        str(one),
        str(two),
    ]
    assert [a.text() for a in window._recent_menu.actions()[:2]] == [
        "&1 one.celpix",
        "&2 two.celpix",
    ]

    two.unlink()
    window._recent_menu.actions()[1].trigger()
    assert captured_alerts  # told, not silently ignored
    assert load_recent_projects() == [str(one)]

    # ...and with the list empty the submenu has nothing to offer, so it greys
    # out rather than opening onto an empty box.
    clear_recent_projects()
    window._sync_recent_menu()
    assert not window._recent_menu.menuAction().isEnabled()


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


def test_opening_a_file_detects_its_container(qtbot, tmp_path) -> None:
    """A file's container is picked from its signature when it is opened."""
    ines = tmp_path / "cart.nes"
    chr_rom = bytes((i * 7) & 0xFF for i in range(8192))
    ines.write_bytes(
        bytes([*b"NES\x1a", 1, 1, 0, 0]) + bytes(8) + bytes(16384) + chr_rom
    )
    plain = tmp_path / "plain.bin"
    plain.write_bytes(bytes(1024))

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(ines))
    window._load_pixel(str(plain))

    assert window._workspace.find_file(str(ines)).container_id == "container.ines"
    assert window._workspace.find_file(str(plain)).container_id == RAW_CONTAINER
    # Detection is not cosmetic: the reader skipped to the CHR ROM, so the
    # document holds the graphics rather than the header and PRG banks.
    assert window._workspace.find_file(str(ines)).doc.pixel_data == chr_rom


def test_container_info_reports_the_read_as_one_table(qtbot, tmp_path) -> None:
    """The popup lays a report out as name/value rows, each with its own tooltip.

    Three things it has to get right, all of which a refactor can quietly break:
    the section rows span both columns (so the values stay in one column), every
    value row carries the container's explanation as a tooltip, and the host's own
    summary is shown even for a container that describes nothing.
    """
    from celpix.pipeline.pathway import PathwayConfig
    from celpix.pipeline.pipeline import inspect_container
    from celpix.plugins.base import FileRef
    from celpix.plugins.registry import default_registry
    from celpix.ui.container_info_dialog import ContainerInfoDialog

    cart = tmp_path / "cart.nes"
    cart.write_bytes(
        bytes([*b"NES\x1a", 1, 1, 0, 0]) + bytes(8) + bytes(16384) + bytes(8192)
    )

    def rows_of(container_id: str):
        report = inspect_container(
            PathwayConfig(
                source=FileRef((str(cart),)),
                interpret_preset_id="",
                container_id=container_id,
            ),
            default_registry(),
        )
        dialog = ContainerInfoDialog(report)
        qtbot.addWidget(dialog)
        table = dialog._table
        spanned, values = [], {}
        for row in range(table.rowCount()):
            name = table.item(row, 0)
            if table.columnSpan(row, 0) == 2:
                spanned.append(name.text())
                continue
            values[name.text()] = (table.item(row, 1).text(), name.toolTip())
        return spanned, values

    spanned, values = rows_of("container.ines")
    assert spanned == ["Read by the container", "Passed to later stages"]
    assert values["Container"][0] == "iNES file (auto-skip header)"
    assert values["Payload"][0].startswith("8 KiB")
    assert values["CHR banks"][0].startswith("1 ")
    assert "8 KiB each" in values["CHR banks"][1]  # what the value was used for
    assert values["Payload offset"][0] == "0x004010"  # published for the view

    # Read as plain bytes instead: a container that describes nothing still
    # reports, because the summary and the hints are the host's to say.
    spanned, values = rows_of(RAW_CONTAINER)
    assert spanned == ["Read by the container", "Passed to later stages"]
    assert values["Payload"][0] == values["Source"][0]  # nothing was stripped
    assert values["Payload offset"][0] == "0x000000"


def test_container_info_is_offered_wherever_a_container_is(
    qtbot, tmp_path, monkeypatch, opened_menus
) -> None:
    """File menu and Files dock both reach it, on the kinds that have a container.

    The pairing is the point: the File menu's action follows the entry on screen
    and a slice has no container of its own, while the dock's has to be on the
    palette rows too - a palette's framing decides how many of its colors are
    real, so it is exactly a row worth inspecting.
    """
    from PySide6.QtCore import Qt

    from celpix.project.workspace import EntryKind
    from celpix.ui.container_info_dialog import ContainerInfoDialog

    px = _make_snes_file(tmp_path)
    pal = tmp_path / "colors.pal"
    pal.write_bytes(bytes(range(32)))
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(px))
    entry = window._workspace.find_file(str(px))
    window._add_palette_file(str(pal))

    assert window._container_info_action.isEnabled()
    # A slice reads through its parent's container, so it has none to report on.
    sliced = window._workspace.add_slice(str(px), "gfx", 64, 64)
    window._activate_entry(sliced)
    assert not window._container_info_action.isEnabled()

    shown = []
    monkeypatch.setattr(
        ContainerInfoDialog,
        "show_report",
        staticmethod(lambda parent, report: shown.append(report)),
    )
    window._show_container_info(sliced)  # not a kind that has a container
    assert shown == []
    window._show_container_info(entry)
    assert [report.container_id for report in shown] == [entry.container_id]

    # And from the dock: which kinds offer the item, and that it acts on the row
    # it was opened over rather than on whatever is on screen.
    tree = window._files_panel._tree
    offered = {}

    def visit(item) -> None:
        row = item.data(0, Qt.ItemDataRole.UserRole)
        if row is not None:
            window._files_panel._show_menu(tree.visualItemRect(item).center())
            for action in opened_menus[-1].actions():
                if "Container Info" in action.text():
                    shown.clear()
                    action.trigger()
                    offered[row.kind] = shown[-1].paths[0]
        for i in range(item.childCount()):
            visit(item.child(i))

    for i in range(tree.topLevelItemCount()):
        visit(tree.topLevelItem(i))
    assert offered == {EntryKind.FILE: str(px), EntryKind.PALETTE: str(pal)}


def _answer_container_dialog(monkeypatch, answer) -> None:
    """Make Edit File Container… return ``answer`` instead of opening."""
    from celpix.ui.container_dialog import ContainerDialog

    monkeypatch.setattr(
        ContainerDialog, "edit_container", staticmethod(lambda *_a, **_k: answer)
    )


def test_change_container_re_reads_the_file(qtbot, tmp_path, monkeypatch) -> None:
    """Edit File Container… re-reads through the newly chosen one, in place."""
    from celpix.ui.container_dialog import ContainerEdit

    cart = tmp_path / "cart.bin"  # named so nothing claims it
    chr_rom = bytes((i * 3) & 0xFF for i in range(8192))
    whole = bytes([*b"NES\x1a", 1, 1, 0, 0]) + bytes(8) + bytes(16384) + chr_rom
    cart.write_bytes(whole)

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(cart))
    entry = window._workspace.find_file(str(cart))
    # Magic claims it whatever the suffix says, so detection already found iNES.
    assert entry.container_id == "container.ines"

    _answer_container_dialog(monkeypatch, ContainerEdit(RAW_CONTAINER, (str(cart),)))
    window._change_container_for(entry)
    assert entry.container_id == RAW_CONTAINER
    assert entry.doc.pixel_data == whole  # plain bytes: the header is back

    # Cancelling changes nothing — not the container, not the loaded bytes.
    _answer_container_dialog(monkeypatch, None)
    window._change_container_for(entry)
    assert entry.container_id == RAW_CONTAINER
    assert entry.doc.pixel_data == whole


def test_change_container_is_file_only(qtbot, tmp_path, monkeypatch) -> None:
    """A slice has no container of its own, so neither menu offers it one."""
    from celpix.ui.container_dialog import ContainerDialog

    px = _make_snes_file(tmp_path)
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(px))
    sliced = window._workspace.add_slice(str(px), "gfx", 64, 64)

    called = []
    monkeypatch.setattr(
        ContainerDialog,
        "edit_container",
        staticmethod(lambda *_a, **_k: called.append(1) or None),
    )
    window._change_container_for(sliced)
    assert called == []  # never even asked

    window._activate_entry(sliced)
    assert not window._change_container_action.isEnabled()
    window._activate_entry(window._workspace.find_file(str(px)))
    assert window._change_container_action.isEnabled()


def test_container_dialog_edits_the_file_list(qtbot, tmp_path, monkeypatch) -> None:
    """The row buttons reorder, replace and drop files, and never drop the last."""
    from celpix.plugins.registry import default_registry
    from celpix.ui import container_dialog
    from celpix.ui.container_dialog import ContainerDialog

    chips = [str(tmp_path / f"chip{i}.bin") for i in range(3)]
    for chip in chips:
        Path(chip).write_bytes(b"\0" * 32)
    spare = str(tmp_path / "spare.bin")
    Path(spare).write_bytes(b"\0" * 32)

    dialog = ContainerDialog(default_registry(), paths=tuple(chips))
    qtbot.addWidget(dialog)
    assert dialog.paths() == tuple(chips)

    # Moving is a swap with the neighbour, and the ends can't move past them.
    dialog._rows[2].up.click()
    assert dialog.paths() == (chips[0], chips[2], chips[1])
    assert not dialog._rows[0].up.isEnabled()
    assert not dialog._rows[-1].down.isEnabled()

    # Browse replaces one row in place; Append lands at the end, since the order
    # is what the join uses and only the user can state it.
    monkeypatch.setattr(
        container_dialog.QFileDialog,
        "getOpenFileName",
        staticmethod(lambda *_a, **_k: (spare, "")),
    )
    dialog._rows[0].browse.click()
    assert dialog.paths() == (spare, chips[2], chips[1])
    dialog._append.click()
    assert dialog.paths() == (spare, chips[2], chips[1], spare)

    dialog._rows[3].remove.click()
    assert dialog.paths() == (spare, chips[2], chips[1])
    # A region is at least one file: emptying the list stops one short, with the
    # last row's Remove disabled — and a no-op even if it is reached anyway.
    for _ in range(2):
        dialog._rows[-1].remove.click()
    assert not dialog._rows[0].remove.isEnabled()
    dialog._remove(0)
    assert dialog.paths() == (spare,)


def test_container_dialog_marks_detection_for_the_first_file(qtbot, tmp_path) -> None:
    """The (detected) marker follows the file it describes when row 1 changes."""
    from celpix.plugins.registry import default_registry
    from celpix.ui.container_dialog import ContainerDialog

    ines = tmp_path / "cart.nes"
    ines.write_bytes(bytes([*b"NES\x1a", 1, 1, 0, 0]) + bytes(8) + bytes(16384 + 8192))
    plain = _make_snes_file(tmp_path)

    dialog = ContainerDialog(default_registry(), paths=(str(ines), str(plain)))
    qtbot.addWidget(dialog)
    marked = dialog._container.itemData(
        next(
            i
            for i in range(dialog._container.count())
            if "(detected)" in dialog._container.itemText(i)
        )
    )
    assert marked == "container.ines"

    # Swap the plain file to the front: detection now says plain bytes, and the
    # marker has to move with it or it recommends the old file's answer.
    dialog._rows[1].up.click()
    marked = dialog._container.itemData(
        next(
            i
            for i in range(dialog._container.count())
            if "(detected)" in dialog._container.itemText(i)
        )
    )
    assert marked == RAW_CONTAINER


def test_editing_the_file_list_repoints_the_file_and_its_slices(
    qtbot, tmp_path, monkeypatch
) -> None:
    """Applying a new list re-reads the joined region, slices included."""
    from celpix.ui.container_dialog import ContainerEdit

    first = tmp_path / "chip1.bin"
    second = tmp_path / "chip2.bin"
    first.write_bytes(bytes(range(128)) * 2)
    second.write_bytes(bytes(range(255, 127, -1)) * 2)

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(first))
    entry = window._workspace.find_file(str(first))
    sliced = window._workspace.add_slice(str(first), "gfx", 0x100, 0x40)
    assert entry.doc.pixel_data == first.read_bytes()

    _answer_container_dialog(
        monkeypatch, ContainerEdit(RAW_CONTAINER, (str(first), str(second)))
    )
    window._change_container_for(entry)
    assert entry.paths == (str(first), str(second))
    # The slice carries the same list — its offset is into the joined buffer.
    assert sliced.paths == (str(first), str(second))
    # And the region really is the two chips joined, so an offset past the first
    # one now lands in the second rather than off the end.
    assert entry.doc.pixel_data == first.read_bytes() + second.read_bytes()


def test_file_list_refuses_an_already_open_first_file(
    qtbot, tmp_path, monkeypatch
) -> None:
    """An entry is its first file, so two entries can't come to name one."""
    from celpix.ui.container_dialog import ContainerEdit

    one, two = tmp_path / "one.bin", tmp_path / "two.bin"
    for path in (one, two):
        path.write_bytes(bytes((i * 7 + 1) & 0xFF for i in range(32 * 8)))

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(one))
    window._load_pixel(str(two))
    entry = window._workspace.find_file(str(one))

    _answer_container_dialog(monkeypatch, ContainerEdit(RAW_CONTAINER, (str(two),)))
    window._change_container_for(entry)
    assert entry.paths == (str(one),)  # refused, and said so
    assert window._workspace.find_file(str(two)) is not entry


def test_file_labels_carry_a_container_hint(qtbot, tmp_path) -> None:
    """A file read through a container says so; a plain one stays unadorned."""
    ines = tmp_path / "cart.nes"
    ines.write_bytes(bytes([*b"NES\x1a", 1, 1, 0, 0]) + bytes(8) + bytes(16384 + 8192))
    plain = _make_snes_file(tmp_path)

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(ines))
    window._load_pixel(str(plain))
    panel = window._files_panel

    cart = window._workspace.find_file(str(ines))
    assert panel._items[cart].text(0) == "cart.nes (iNES)"
    # The tag is short by design; the tooltip carries the full name.
    assert "Container iNES file (auto-skip header)" in panel._items[cart].toolTip(0)

    # Plain bytes are the overwhelming majority — no hint, no wasted width.
    bare = window._workspace.find_file(str(plain))
    assert panel._items[bare].text(0) == bare.name
    assert "Container" not in panel._items[bare].toolTip(0)

    # A slice has no container of its own, so it never carries a hint.
    sliced = window._workspace.add_slice(str(ines), "gfx", 64, 64)
    assert panel._items[sliced].text(0) == "gfx"

    # The unsaved marker leads, so the hint has to sit inside it, not after it.
    cart.pixel_revision = cart.pixel_saved_revision + 1
    panel.refresh_entry(cart)
    assert panel._items[cart].text(0) == "● cart.nes (iNES)"

    # And the hint tracks the container it names.
    cart.container_id = RAW_CONTAINER
    panel.refresh_entry(cart)
    assert panel._items[cart].text(0) == "● cart.nes"


def test_file_labels_count_the_files_a_region_joins(qtbot, tmp_path) -> None:
    """A row named after one chip says how many files it really covers."""
    chips = [tmp_path / f"chip{i}.bin" for i in range(3)]
    for chip in chips:
        chip.write_bytes(bytes((i * 5 + 1) & 0xFF for i in range(32 * 8)))

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(chips[0]))
    panel = window._files_panel
    entry = window._workspace.find_file(str(chips[0]))
    assert panel._items[entry].text(0) == "chip0.bin"  # one file, nothing to say

    entry.extra_paths = (str(chips[1]), str(chips[2]))
    panel.refresh_entry(entry)
    assert panel._items[entry].text(0) == "chip0.bin (3)"
    # The order is what the join uses, so the tooltip numbers the files.
    tip = panel._items[entry].toolTip(0)
    assert "3 files joined end to end:" in tip
    assert f"2. {chips[1]}" in tip and f"3. {chips[2]}" in tip

    # A slice is one region cut out of the join, not a list of its own.
    sliced = window._workspace.add_slice(str(chips[0]), "gfx", 64, 64)
    assert sliced.extra_paths == entry.extra_paths
    assert panel._items[sliced].text(0) == "gfx"


def test_the_files_list_groups_entries_into_sections(qtbot, tmp_path) -> None:
    """Sections say what an entry holds; the tree's nesting keeps saying "a
    window into that file's bytes" (docs/design/tilemap-entry.md §2)."""
    from celpix.core.capabilities import ContentKind

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_make_snes_file(tmp_path)))
    # Only the kinds actually open get a heading, so a plain session shows one.
    assert _section_names(window._files_panel) == ["Pixels"]

    entry = window._workspace.current
    entry.content_kind = ContentKind.TILEMAP
    window._files_panel.remove_entry(entry)
    window._files_panel.add_entry(entry)
    assert _section_names(window._files_panel) == ["Tilemaps"]


def test_a_sections_heading_goes_away_with_its_last_entry(qtbot, tmp_path) -> None:
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_make_snes_file(tmp_path)))
    panel = window._files_panel
    assert _section_names(panel) == ["Pixels"]
    panel.remove_entry(window._workspace.entries[0])
    assert _section_names(panel) == []


def test_sections_keep_their_order_whatever_opens_first(qtbot, tmp_path) -> None:
    """The order is the list's, not the session's: a tilemap opened before any
    pixel file must not push Pixels below it."""
    from celpix.core.capabilities import ContentKind
    from celpix.project.workspace import Entry, EntryKind

    window = MainWindow()
    qtbot.addWidget(window)
    panel = window._files_panel
    panel.add_entry(
        Entry(
            name="m.scr",
            kind=EntryKind.FILE,
            path=str(tmp_path / "m.scr"),
            content_kind=ContentKind.TILEMAP,
        )
    )
    panel.add_entry(
        Entry(name="p.pal", kind=EntryKind.PALETTE, path=str(tmp_path / "p.pal"))
    )
    panel.add_entry(
        Entry(name="t.bin", kind=EntryKind.FILE, path=str(tmp_path / "t.bin"))
    )
    assert _section_names(panel) == ["Pixels", "Tilemaps", "Palettes"]


def test_open_tilemap_data_forces_the_tilemap_reading(
    qtbot, tmp_path, monkeypatch
) -> None:
    """Detection can only recognise a format it knows, so a raw region of a ROM
    has no way to announce itself as a map. Asking is how that is said."""
    from PySide6.QtWidgets import QFileDialog

    from celpix.core.capabilities import ContentKind

    plain = tmp_path / "region.bin"
    plain.write_bytes(bytes(range(256)) * 8)
    window = MainWindow()
    qtbot.addWidget(window)
    monkeypatch.setattr(
        QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(plain), ""))
    )
    window._open_tilemap()

    entry = window._workspace.current
    assert entry.content_kind is ContentKind.TILEMAP
    assert window._doc.is_tilemap
    assert _section_names(window._files_panel) == ["Tilemaps"]


def test_open_pixel_data_forces_the_pixel_reading(qtbot, tmp_path, monkeypatch) -> None:
    """The mirror: a screen file opened through the pixel entry reads as tiles."""
    from PySide6.QtWidgets import QFileDialog

    from celpix.core.capabilities import ContentKind
    from celpix.core.tilemap import Cell

    scr = _scr_file(tmp_path, [Cell(index=1)])
    window = MainWindow()
    qtbot.addWidget(window)
    monkeypatch.setattr(
        QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(scr), ""))
    )
    window._open_pixel()

    entry = window._workspace.current
    assert entry.content_kind is ContentKind.PIXELS
    assert not window._doc.is_tilemap
    # Still read through its own container, so the payload alone.
    assert entry.container_id == "container.scgcad-scr"


def test_a_dropped_col_lands_in_the_palettes_section(qtbot, tmp_path) -> None:
    """The palette that ships beside the screen and panel files."""
    col = tmp_path / "colors.col"
    col.write_bytes(bytes(0x400))
    window = MainWindow()
    qtbot.addWidget(window)
    window.dropEvent(_drop_event(col))

    assert window._workspace.find_palette(str(col)) is not None
    assert _section_names(window._files_panel) == ["Palettes"]


def test_ctrl_dropping_asks_how_to_read_the_file(
    qtbot, tmp_path, open_as_answer
) -> None:
    """Detection is a guess and silent about being one; Ctrl is how the user
    overrules it without going to find the matching menu entry."""
    from celpix.core.capabilities import ContentKind

    plain = tmp_path / "region.bin"
    plain.write_bytes(bytes(range(256)) * 8)
    window = MainWindow()
    qtbot.addWidget(window)

    open_as_answer.kind = ContentKind.TILEMAP
    window.dropEvent(_drop_event(plain, ctrl=True))
    assert window._workspace.current.content_kind is ContentKind.TILEMAP

    # Without the modifier the same file takes the ordinary route.
    other = tmp_path / "other.bin"
    other.write_bytes(bytes(range(256)) * 8)
    window.dropEvent(_drop_event(other))
    assert window._workspace.current.content_kind is ContentKind.PIXELS


def test_cancelling_the_open_as_prompt_opens_nothing(
    qtbot, tmp_path, open_as_answer
) -> None:
    plain = tmp_path / "region.bin"
    plain.write_bytes(bytes(range(256)) * 8)
    window = MainWindow()
    qtbot.addWidget(window)

    open_as_answer.kind = None  # Cancel
    window.dropEvent(_drop_event(plain, ctrl=True))
    assert window._workspace.entries == []


def test_a_new_slice_can_be_carved_out_as_a_tilemap(qtbot, tmp_path) -> None:
    """A slice inherits its parent's reading, which is right for the common case
    and wrong for the one this row exists for: a ROM is opened as pixels, and the
    map that draws them is a region of that same ROM. Only creation offers it —
    editing a live slice would re-read it as another kind of thing.
    """
    from celpix.core.capabilities import ContentKind
    from celpix.plugins.registry import default_registry
    from celpix.ui.slice_dialog import SliceDialog, SliceParams

    rom = _make_snes_file(tmp_path)
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(rom))
    parent = window._workspace.current
    assert parent.content_kind is ContentKind.PIXELS

    dialog = SliceDialog(
        default_registry(),
        paths=parent.paths,
        offset=0,
        length=0x40,
        content_kind=parent.content_kind,
        choose_content=True,
    )
    qtbot.addWidget(dialog)
    dialog._content.setCurrentIndex(dialog._content.findData(ContentKind.TILEMAP))
    dialog._validate_and_accept()
    params = dialog._params
    assert params.content_kind is ContentKind.TILEMAP

    # An edit is not offered the row, and carries the entry's own answer through
    # untouched - so its before/after pair stays comparable.
    edit = SliceDialog(
        default_registry(),
        paths=parent.paths,
        offset=0,
        length=0x40,
        content_kind=ContentKind.TILEMAP,
    )
    qtbot.addWidget(edit)
    edit._validate_and_accept()
    assert edit._params.content_kind is ContentKind.TILEMAP
    assert edit._params == SliceParams(
        params.name,
        0,
        0x40,
        params.compression_id,
        params.reshape_id,
        ContentKind.TILEMAP,
    )


def test_a_project_plugins_cell_format_reaches_the_files_list(qtbot, tmp_path) -> None:
    """A row's layout icon comes off a preset the *project* supplies.

    Opening a project **replaces** the window's registry rather than adding to
    one, and the files panel is the only widget holding a reference of its own —
    so it has to be handed the new object. Left on the one it was built with, it
    looks every row's cell format up in a registry that has never heard of the
    project's own ``plugins/`` folder, reports no layout, and draws the plain
    grid icon on a fontmap and a sprite map alike. Only the *shipped* formats
    looked right, which is what made it read as one entry being misconfigured.
    """
    from celpix.plugins.discovery import load_user_plugins, project_plugin_dir
    from celpix.plugins.registry import default_registry

    px = _make_snes_file(tmp_path)
    plugins = tmp_path / "plugins" / "tilemap"
    plugins.mkdir(parents=True)
    (plugins / "words.toml").write_text(
        'id = "preset.tilemap.project-text"\n'
        'name = "Project text run"\n'
        'engine_id = "codec.tilemap.packed"\n'
        "[params]\n"
        'layout = "text"\n'
        "bytes = 1\n"
        'fields = "iiii iiii"\n',
        encoding="utf-8",
    )
    project = tmp_path / "p.celpix"
    project.write_text(
        json.dumps(
            {
                "version": 1,
                "current": 0,
                "entries": [
                    {"kind": "file", "name": "art", "path": str(px)},
                    {
                        "kind": "slice",
                        "name": "words",
                        "path": str(px),
                        "slice_offset": 0,
                        "slice_length": 8,
                        "compression_id": "compression.none",
                        "content_kind": "tilemap",
                        "tilemap_preset_id": "preset.tilemap.project-text",
                        "tile_source": {"mode": "entry", "entry_index": 0},
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    def reload_plugins(project_path=None):
        # A *fresh* registry every time, exactly as the app builds one — which is
        # the whole of what the panel could be left behind by.
        registry = default_registry()
        folder = project_plugin_dir(project_path)
        issues = load_user_plugins(
            registry, [folder] if folder else [], project_dir=folder
        )
        return registry, issues

    registry, issues = reload_plugins()
    window = MainWindow(registry=registry, reload_plugins=reload_plugins)
    qtbot.addWidget(window)
    window._load_project(str(project))

    entry = next(e for e in window._workspace.entries if e.name == "words")
    assert "Fontmap" in window._files_panel._items[entry].toolTip(0)

    # And it lets go again: closing the project drops those plugins, so the row
    # falls back to the plain grid rather than naming a format nothing provides.
    window._new_project()
    assert issues == []
