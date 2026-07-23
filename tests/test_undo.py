"""Undo/redo wiring: one unified session stack across every editing surface.

The commands themselves are thin (see ``ui/undo_commands.py``); the regression
risk lives in the wiring — that a gesture pushes exactly one command, that undo
truly restores every widget/document field the gesture touched (no cascade from
signal syncing), that consecutive moves coalesce only within one entry, that
undoing a change made elsewhere re-activates that entry, and that entry
lifecycle (open/add/remove) is itself undoable with object identity preserved.
"""

from __future__ import annotations

from celpix.project.workspace import Entry, EntryKind
from celpix.ui.main_window import MainWindow
from celpix.ui.undo_commands import (
    AddEntryCommand,
    RemoveEntriesCommand,
    SliceEditCommand,
    SliceParams,
)


def _open(qtbot, tmp_path, name="gfx.bin"):
    """A MainWindow with a 16 KiB pixel file open and current."""
    path = tmp_path / name
    path.write_bytes(bytes(range(256)) * 64)
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(path))
    return window, str(path)


def _add_slice(window, path, name="sliceA", offset=0, length=16384):
    """Add a slice through an AddEntryCommand (as the dialog path does), so it
    lands on the session stack. Length spans more than one page even at the
    widest tile size (4bpp) — a too-short slice makes a nav move clamp to a
    no-op and push nothing."""
    sl = Entry(
        name=name,
        kind=EntryKind.SLICE,
        path=path,
        slice_offset=offset,
        slice_length=length,
    )
    window._push_command(AddEntryCommand(window, sl, f'new slice "{name}"'))
    return sl


def test_offset_moves_coalesce_and_round_trip(qtbot, tmp_path) -> None:
    window, _ = _open(qtbot, tmp_path)
    stack = window._undo_stack
    base = stack.count()  # the open-file command sits below

    # Consecutive nav moves in one entry merge into a single step (id()==1).
    window._nav_tiles(1)
    window._nav_tiles(1)
    window._nav_rows(2)
    target = window._offset
    assert target > 0
    assert stack.count() == base + 1
    stack.undo()
    assert (window._offset, window._nudge) == (0, 0)
    stack.redo()
    assert window._offset == target

    # A run that walks back to its exact start collapses to nothing: the merged
    # command marks itself obsolete and drops off the stack.
    stack.undo()
    assert stack.count() == base + 1 and stack.index() == base
    window._nav_tiles(3)
    window._nav_tiles(-3)
    assert stack.count() == base


def test_offset_merge_chain_broken_by_other_command(qtbot, tmp_path) -> None:
    # A different command landing between two moves breaks the merge run, so the
    # moves on either side stay distinct steps rather than coalescing across it.
    window, _ = _open(qtbot, tmp_path)
    stack = window._undo_stack
    base = stack.count()

    window._nav_tiles(1)
    assert window._load_palette_at_offset(0)
    window._nav_tiles(1)
    assert stack.count() == base + 3


def test_pixel_preset_switch_round_trip(qtbot, tmp_path) -> None:
    window, _ = _open(qtbot, tmp_path)
    stack = window._undo_stack

    # Move off the origin first so the restored byte position is load-bearing.
    window._nav_rows(3)
    old_preset = window._doc.pixel_config.interpret_preset_id
    old_pos = window._byte_position()
    assert old_pos > 0

    idx = window._pixel_preset.findData("preset.pixel.snes-2bpp")
    assert idx >= 0
    window._pixel_preset.setCurrentIndex(idx)
    assert window._doc.pixel_config.interpret_preset_id == "preset.pixel.snes-2bpp"

    stack.undo()  # the switch is the top command (nav below it)
    assert window._doc.pixel_config.interpret_preset_id == old_preset
    assert window._pixel_preset.currentData() == old_preset  # combo re-synced
    assert window._byte_position() == old_pos  # view re-anchored in byte space
    stack.redo()
    assert window._doc.pixel_config.interpret_preset_id == "preset.pixel.snes-2bpp"


def test_header_toggle_round_trip(qtbot, tmp_path) -> None:
    window, _ = _open(qtbot, tmp_path)
    stack = window._undo_stack
    base = stack.count()

    window._headered.setChecked(True)  # default skip is 512 bytes
    assert window._doc.pixel_config.source.offset == 512
    assert stack.count() == base + 1  # syncing the checkbox back must not cascade
    stack.undo()
    assert window._doc.pixel_config.source.offset == 0
    assert not window._headered.isChecked()
    assert stack.count() == base + 1


def test_palette_at_offset_round_trip_and_failed_load(qtbot, tmp_path) -> None:
    window, _ = _open(qtbot, tmp_path)
    stack = window._undo_stack

    before_len = len(window._doc.palette)
    before_cfg = window._doc.palette_config
    assert window._load_palette_at_offset(0)
    assert window._palette_mode == "offset"
    stack.undo()
    assert window._palette_mode == "default"
    assert len(window._doc.palette) == before_len
    assert window._doc.palette_config is before_cfg
    stack.redo()
    assert window._palette_mode == "offset"

    # A load past EOF can't size even one entry: it fails silently, pushing
    # nothing and leaving the stack untouched.
    count = stack.count()
    assert not window._load_palette_at_offset(1 << 20)
    assert stack.count() == count


def test_palette_format_switch_round_trip(qtbot, tmp_path) -> None:
    window, _ = _open(qtbot, tmp_path)
    stack = window._undo_stack

    assert window._load_palette_at_offset(0)  # a reload only fires with a palette
    before_len = len(window._doc.palette)
    before_cfg = window._doc.palette_config

    idx = window._palette_preset.findData("preset.palette.rgb888")
    assert idx >= 0
    window._palette_preset.setCurrentIndex(idx)
    assert window._doc.palette_config.interpret_preset_id == "preset.palette.rgb888"

    stack.undo()
    assert window._doc.palette_config is before_cfg
    assert len(window._doc.palette) == before_len
    assert window._palette_preset.currentData() == "preset.palette.bgr555"
    stack.redo()
    assert window._doc.palette_config.interpret_preset_id == "preset.palette.rgb888"


def test_cross_entry_undo_reactivates_and_no_cross_entry_merge(qtbot, tmp_path) -> None:
    # The unified stack spans entries: undoing a move made in another entry must
    # switch the view back there before reverting, and moves in distinct entries
    # sit adjacent without merging.
    window, path = _open(qtbot, tmp_path)
    stack = window._undo_stack
    file_entry = window._workspace.current

    sl = _add_slice(window, path)
    assert window._workspace.current is sl  # add activates
    window._nav_tiles(1)  # a move inside the slice
    assert window._offset == 1
    window._activate_entry(file_entry)  # switch away (pushes nothing)
    switched_count = stack.count()

    stack.undo()  # undo the slice's move -> view must jump back to the slice
    assert window._workspace.current is sl
    assert window._offset == 0
    assert stack.count() == switched_count  # undo doesn't push

    # Moves in the slice then the file are two commands, not one merged step.
    window._nav_tiles(1)  # slice
    window._activate_entry(file_entry)
    window._nav_tiles(1)  # file
    assert stack.command(stack.count() - 1)._entry is file_entry
    assert stack.command(stack.count() - 2)._entry is sl


def test_switching_entries_pushes_nothing(qtbot, tmp_path) -> None:
    window, path = _open(qtbot, tmp_path)
    stack = window._undo_stack
    file_entry = window._workspace.current
    sl = _add_slice(window, path)

    # Restoring session state on a switch blocks widget signals, so bouncing
    # between entries pushes nothing.
    count = stack.count()
    window._activate_entry(file_entry)
    window._activate_entry(sl)
    window._activate_entry(file_entry)
    assert stack.count() == count


def test_rename_non_current_entry_applies_in_place(qtbot, tmp_path) -> None:
    # A rename lands on the shared stack and applies where it is — undoing it
    # must NOT drag the view to the renamed (non-current) entry.
    window, path = _open(qtbot, tmp_path)
    file_entry = window._workspace.current
    sl = window._workspace.add_slice(path, "sliceA", 0, 8192)  # not activated
    stack = window._undo_stack
    count = stack.count()

    window._rename_entry(sl, "renamed")
    assert sl.name == "renamed"
    assert stack.count() == count + 1
    assert window._workspace.current is file_entry

    stack.undo()
    assert sl.name == "sliceA"
    assert window._workspace.current is file_entry  # no activation on undo


def test_slice_edit_round_trip(qtbot, tmp_path) -> None:
    # Drive the command directly (the edit dialog is modal): re-pointing a slice
    # rewrites its coordinates and re-reads the region; undo restores both.
    window, path = _open(qtbot, tmp_path)
    sl = window._workspace.add_slice(path, "sliceA", 0, 256)

    before = SliceParams(sl.name, sl.slice_offset, sl.slice_length, sl.decompress_id)
    after = SliceParams("moved", 128, 512, "decompress.none")
    window._push_command(SliceEditCommand(window, sl, before=before, after=after))
    assert (sl.slice_offset, sl.slice_length, sl.name) == (128, 512, "moved")
    assert sl.doc is None  # cached document dropped so the new region re-reads

    window._undo_stack.undo()
    assert (sl.slice_offset, sl.slice_length, sl.name) == (0, 256, "sliceA")


def test_open_file_round_trip_and_reopen_dedupe(qtbot, tmp_path) -> None:
    # Opening a file is itself one undoable step; undo empties the workspace and
    # redo restores the very same Entry object with its document intact.
    path = tmp_path / "gfx.bin"
    path.write_bytes(bytes(range(256)) * 64)
    window = MainWindow()
    qtbot.addWidget(window)

    window._load_pixel(str(path))
    entry = window._workspace.current
    stack = window._undo_stack
    assert stack.count() == 1
    assert stack.undoText() == "open gfx.bin"

    stack.undo()
    assert window._workspace.entries == []
    assert window._doc is None
    stack.redo()
    assert window._workspace.current is entry  # same object, not a re-open
    assert window._doc is entry.doc

    # Re-opening an already-open path just activates it — no new command.
    window._load_pixel(str(path))
    assert stack.count() == 1


def test_add_slice_round_trip(qtbot, tmp_path) -> None:
    # Adding a slice activates it on redo; undo removes it and the view returns
    # to the previously-current entry via the workspace's close-repointing.
    window, path = _open(qtbot, tmp_path)
    file_entry = window._workspace.current
    stack = window._undo_stack

    sl = _add_slice(window, path)
    assert window._workspace.current is sl
    assert stack.undoText() == 'new slice "sliceA"'

    stack.undo()
    assert sl not in window._workspace.entries
    assert window._workspace.current is file_entry
    stack.redo()
    assert window._workspace.current is sl  # same object re-inserted


def test_remove_file_with_slice_round_trip(qtbot, tmp_path) -> None:
    # Removing a file also removes its slices; undo restores both at their
    # recorded positions (parent re-nested first), current back on the removed
    # entry, and the SAME Document objects — nothing is reloaded. This also
    # exercises the file-panel fix that drops nested slice items on file removal.
    window, path = _open(qtbot, tmp_path)
    file_entry = window._workspace.current
    sl = _add_slice(window, path)
    window._activate_entry(file_entry)  # make the file current before removal
    file_doc = file_entry.doc
    stack = window._undo_stack

    entries = window._workspace.entries
    victims = [file_entry, *window._workspace.slices_of(file_entry)]
    window._push_command(
        RemoveEntriesCommand(
            window,
            file_entry,
            victims=[(entries.index(e), e) for e in victims],
            was_current=window._workspace.current,
        )
    )
    assert window._workspace.entries == []
    assert window._doc is None

    stack.undo()
    assert window._workspace.entries[0] is file_entry
    assert window._workspace.entries[1] is sl
    assert window._workspace.current is file_entry
    assert window._doc is file_doc  # same Document — not reloaded from disk
    stack.redo()
    assert window._workspace.entries == []


def test_full_history_walk(qtbot, tmp_path) -> None:
    # Undoing everything empties the workspace; redoing everything replays it.
    # The top command here is an add, so full redo lands back on that entry.
    window, path = _open(qtbot, tmp_path)
    window._nav_tiles(2)
    _add_slice(window, path)
    window._nav_tiles(1)
    stack = window._undo_stack

    while stack.canUndo():
        stack.undo()
    assert window._workspace.entries == []
    assert window._doc is None

    while stack.canRedo():
        stack.redo()
    assert len(window._workspace.entries) == 2
    assert window._doc is not None


def test_ctrl_z_shortcut_reaches_undo(qtbot, tmp_path) -> None:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    window, _ = _open(qtbot, tmp_path)

    # The action's Ctrl+Z is a WindowShortcut, so the window must be shown and
    # active (activateWindow() doesn't stick under the offscreen platform).
    window.show()
    QApplication.setActiveWindow(window)
    window._canvas.setFocus()

    window._nav_tiles(1)
    assert window._offset == 1
    # Ctrl+Z routed through the window must reach the undo action, proving the
    # app-wide nav event filter doesn't swallow the shortcut.
    qtbot.keyClick(window, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    assert window._offset == 0
