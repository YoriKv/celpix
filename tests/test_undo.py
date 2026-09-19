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
    PixelEditCommand,
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


def test_a_pixel_switch_carries_the_palette_row_it_re_anchored(qtbot, tmp_path) -> None:
    # The row is re-anchored once, at push time, and both halves carry it: worked
    # out again on undo it would measure from the live row (and a narrower count
    # floors), so the row the switch started from would not come back.
    window, _ = _open(qtbot, tmp_path)
    stack = window._undo_stack
    window._pixel_preset.setCurrentIndex(
        window._pixel_preset.findData("preset.pixel.snes-2bpp")
    )
    window._palette_row.setValue(13)  # 2bpp: colors 52-55, inside 4bpp row 3
    window._pixel_preset.setCurrentIndex(
        window._pixel_preset.findData("preset.pixel.snes-4bpp")
    )
    assert window._palette_row.value() == 3

    stack.undo()  # re-derived, 3 x 16 colors would floor back to row 12
    assert window._palette_row.value() == window._doc.view.palette_row == 13
    stack.redo()
    assert window._palette_row.value() == 3


def test_a_rearrangement_survives_a_switch_through_fewer_tiles(qtbot, tmp_path) -> None:
    # Stored whole and bounded only on the read: an entry switch while a codec
    # with bigger tiles has fewer of them must not drop the pairs past its end.
    window, path = _open(qtbot, tmp_path)
    file_entry = window._workspace.current
    window._pixel_preset.setCurrentIndex(
        window._pixel_preset.findData("preset.pixel.snes-2bpp")
    )
    many = window._doc.tile_count
    stored = window._tile_rearrangement.swap_many([(0, many - 1)])
    window._set_tile_rearrangement(stored)
    window._pixel_preset.setCurrentIndex(
        window._pixel_preset.findData("preset.pixel.snes-4bpp")
    )
    assert window._doc.tile_count < many
    _add_slice(window, path)  # activates the slice
    window._activate_entry(file_entry)
    assert window._tile_rearrangement == stored
    assert window._doc.view.tile_rearrangement == stored


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

    before = SliceParams(sl.name, sl.slice_offset, sl.slice_length, sl.compression_id)
    after = SliceParams("moved", 128, 512, "compression.none")
    window._push_command(SliceEditCommand(window, sl, before=before, after=after))
    assert (sl.slice_offset, sl.slice_length, sl.name) == (128, 512, "moved")
    assert sl.doc is None  # cached document dropped so the new region re-reads

    window._undo_stack.undo()
    assert (sl.slice_offset, sl.slice_length, sl.name) == (0, 256, "sliceA")


def test_slice_edits_and_parent_edits_compose_and_undo_together(
    qtbot, tmp_path
) -> None:
    """A slice and its file are one set of bytes, so an edit through either
    reaches the other as it lands — and undo takes it back out of both.

    The regression this guards is the two buffers racing. Each edit is folded
    into the file's buffer immediately, so an edit made *on* the file inside a
    dirty slice's window composes on top instead of being reverted when that
    slice's stale window is folded in behind it. And because the file carries
    the unsaved state too, its revision has to travel through undo alongside the
    slice's — otherwise undoing a slice edit leaves the file reading dirty for a
    change that no longer exists anywhere.
    """
    window, path = _open(qtbot, tmp_path)
    parent = window._workspace.current
    original = bytes(parent.doc.pixel_data[0x100:0x180])
    sl = window._workspace.add_slice(path, "gfx", 0x100, 0x80)

    # An edit through the slice reaches the file, and dirties both.
    window._workspace.set_current(sl)
    window._push_command(
        PixelEditCommand(
            window, sl, "paint", regions=[(0x00, original[:0x10], b"\x5a" * 16)]
        )
    )
    assert sl.pixel_dirty and parent.pixel_dirty
    window._workspace.set_current(parent)
    assert bytes(parent.doc.pixel_data[0x100:0x110]) == b"\x5a" * 16

    # An edit on the file *inside* that slice's window composes with it rather
    # than either one winning.
    file_edit = [(0x120, original[0x20:0x30], b"\x3c" * 16)]
    window._push_command(PixelEditCommand(window, parent, "paint", regions=file_edit))
    window._workspace.set_current(sl)
    assert bytes(sl.doc.pixel_data[0x00:0x10]) == b"\x5a" * 16  # the slice's own
    assert bytes(sl.doc.pixel_data[0x20:0x30]) == b"\x3c" * 16  # the file's

    # Undoing back past both takes the slice's edit out of the file's buffer
    # too, and leaves neither reading dirty for it.
    window._undo_stack.undo()
    window._undo_stack.undo()
    window._workspace.set_current(parent)
    assert bytes(parent.doc.pixel_data[0x100:0x180]) == original
    assert not sl.pixel_dirty and not parent.pixel_dirty


def test_container_edit_round_trip(qtbot, tmp_path, monkeypatch) -> None:
    """Edit File Container… is one undoable step covering all three of what the
    dialog settles — the file list, the container and the reshape — and a
    re-pointed file carries its slices in *both* directions: a child left on the
    old path would no longer find its parent, and its offsets address the joined
    buffer either way.
    """
    from celpix.plugins.base import NO_RESHAPE, RAW_CONTAINER
    from celpix.ui.container_dialog import ContainerDialog, ContainerEdit

    window, path = _open(qtbot, tmp_path)
    second = tmp_path / "gfx2.bin"
    second.write_bytes(bytes(range(256)) * 64)
    entry = window._workspace.current
    sl = window._workspace.add_slice(path, "sliceA", 0, 256)
    steps = window._undo_stack.count()

    joined = (path, str(second))
    monkeypatch.setattr(
        ContainerDialog,
        "edit_container",
        staticmethod(
            lambda *_a, **_k: ContainerEdit(
                RAW_CONTAINER, joined, "reshape.split-planes-2"
            )
        ),
    )
    window._change_container_for(entry)
    assert window._undo_stack.count() == steps + 1
    assert (entry.paths, entry.reshape_id) == (joined, "reshape.split-planes-2")
    assert sl.paths == joined

    window._undo_stack.undo()
    assert (entry.paths, entry.reshape_id) == ((path,), NO_RESHAPE)
    assert sl.paths == (path,)

    window._undo_stack.redo()
    assert (entry.paths, entry.reshape_id) == (joined, "reshape.split-planes-2")
    assert sl.paths == joined


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


def test_pinning_a_selection_is_one_undoable_step_that_leaves_bytes_clean(
    qtbot, tmp_path
) -> None:
    """Pinning is on the stack, but it is not an edit.

    Both halves matter. It has to be undoable, because a mis-pinned selection is
    tedious to reverse by hand. And it must not dirty the entry: no byte changed,
    so a Write prompt afterwards would be asking about nothing.
    """
    window, _ = _open(qtbot, tmp_path)
    entry = window._workspace.current
    stack = window._undo_stack

    window._set_linear_selection(4, 7)
    window._palette_row.setValue(3)  # a step of its own - see the axis test below
    before = stack.count()
    window._pin_selection()

    assert stack.count() == before + 1
    assert not window._palette_regions.is_empty()
    assert not entry.pixel_dirty  # display state, not an edit
    pinned = window._palette_regions

    stack.undo()
    assert window._palette_regions.is_empty()
    assert not entry.pixel_dirty
    stack.redo()
    assert window._palette_regions == pinned

    # Pinning the same tiles to the same row again is not a second history step.
    window._pin_selection()
    assert stack.count() == before + 1


def test_view_axes_are_undoable_per_axis_and_carry_the_position(
    qtbot, tmp_path
) -> None:
    """Cols, Rows and Palette Row are on the stack, one step per axis run.

    All three are written to the project file, so nudging one is a change to
    the saved document — but none of them moves a byte, so undoing one must
    leave the entry exactly as clean as it was. The position rides along
    because a wider window re-clamps it: restoring the axis without it would
    land the view somewhere the user never was.
    """
    window, _ = _open(qtbot, tmp_path)
    entry = window._workspace.current
    stack = window._undo_stack

    # Park the view on the last page, so widening Cols has to pull it back.
    window._nav_end()
    parked = window._offset
    assert parked > 0
    base = stack.count()

    # A run in one axis coalesces; the next axis starts a new step.
    window._columns.setValue(24)
    window._columns.setValue(32)
    window._rows.setValue(8)
    assert stack.count() == base + 2
    assert window._doc.view.columns == 32 and window._doc.view.rows == 8
    assert not entry.pixel_dirty  # a layout, not an edit

    stack.undo()  # rows
    assert window._rows.value() == 16 and window._doc.view.rows == 16
    stack.undo()  # the whole columns run, and the position it was made from
    assert window._columns.value() == 16 and window._doc.view.columns == 16
    assert window._offset == parked
    stack.redo()
    assert window._columns.value() == 32
    stack.redo()  # back to the top, so the pushes below truncate nothing

    # Palette Row is the third axis, and merges only with itself.
    before_row = stack.count()
    window._palette_row.setValue(2)
    window._palette_row.setValue(3)
    assert stack.count() == before_row + 1
    stack.undo()
    assert window._palette_row.value() == 0 and window._doc.view.palette_row == 0
    stack.redo()

    # A run that walks back to where it started is no step at all.
    settled = stack.count()
    window._columns.setValue(20)
    window._columns.setValue(32)
    assert stack.count() == settled


def test_a_pattern_pick_is_one_step_over_every_control_it_moved(
    qtbot, tmp_path
) -> None:
    """The bar's most destructive gesture: a pick fills four controls and
    withdraws the bitmap width, and afterwards nothing on screen says what any of
    them held. One step has to take the whole of it back — including the Cols the
    width had displaced, and the byte position the re-cut landed on.
    """
    from celpix.core.arrangement import ARRANGEMENT_PRESETS

    window, _ = _open(qtbot, tmp_path)
    stack = window._undo_stack
    window._columns.setValue(24)
    window._two_d.setChecked(True)
    window._bitmap_width.setValue(128)  # 8-px tiles: 16 columns span it
    assert window._doc.view.bitmap_width == 128
    assert window._columns.value() == 16 and not window._columns.isEnabled()
    window._nav_rows(2)
    parked = window._byte_position()
    assert parked > 0
    base = stack.count()

    preset = next(p for p in ARRANGEMENT_PRESETS if p.id == "genesis-sprite")
    window._pattern.setCurrentIndex(window._pattern.findData(preset))
    view = window._doc.view
    assert (view.block_columns, view.block_rows, view.block_order) == (2, 2, "column")
    assert view.bitmap_width == 0  # the width does not follow a new arrangement
    assert window._columns.value() == 24  # handed back, not left at the derived 16
    assert stack.count() == base + 1

    stack.undo()
    view = window._doc.view
    assert (view.block_columns, view.block_rows) == (1, 1)
    assert view.bitmap_width == 128 and view.two_dimensional
    assert window._columns.value() == 16  # the width owns it again
    assert window._byte_position() == parked  # the re-cut re-anchored in bytes
    stack.redo()
    assert window._doc.view.bitmap_width == 0
    assert window._doc.view.block_columns == 2


def test_arrangement_merges_per_control_and_custom_costs_no_step(
    qtbot, tmp_path
) -> None:
    """Two rules the bar needs. A run in one control is one step, while two
    different controls stay two — and picking **Custom** moves nothing the
    project file keeps, so it unlocks the controls without costing a step and
    without a later edit re-locking them.
    """
    from celpix.core.arrangement import ARRANGEMENT_PRESETS

    window, _ = _open(qtbot, tmp_path)
    stack = window._undo_stack
    base = stack.count()

    # Custom off the default preset unlocks the controls and is not a step: no
    # axis moved, and there is no width for the pick to withdraw.
    window._pattern.setCurrentIndex(window._pattern.findData("custom"))
    assert window._block_cols.isEnabled() and stack.count() == base

    window._two_d.setChecked(True)
    window._bitmap_width.setValue(64)
    window._bitmap_width.setValue(128)  # merges with the step above
    assert stack.count() == base + 2  # 2D, then the width run

    # A run that walks back to its start dissolves, like a nav run.
    settled = stack.count()
    window._bitmap_width.setValue(64)
    window._bitmap_width.setValue(128)
    assert stack.count() == settled

    # A hand edit that happens to match a preset must not re-lock the controls.
    nes = next(p for p in ARRANGEMENT_PRESETS if p.id == "nes-8x16")
    window._two_d.setChecked(nes.two_dimensional)
    window._block_rows.setValue(nes.block_rows)
    window._block_cols.setValue(nes.block_columns)
    assert window._pattern.currentData() == "custom"
    assert window._block_rows.isEnabled()


def test_the_preview_picker_is_one_step_per_scheme(qtbot, tmp_path) -> None:
    """The main view stays raw whatever the picker says, so nothing here moves a
    byte — but which scheme a region is *believed* to be in is written to the
    entry's session, and finding it means trying schemes until one decodes.

    The before state has to come off the session rather than a stale capture:
    a session is otherwise only written on the way out of an entry, which is far
    too late to be what a second pick measures itself against.
    """
    window, _ = _open(qtbot, tmp_path)
    stack = window._undo_stack
    combo = window._compression
    first = window._compression_id()
    base = stack.count()

    combo.setCurrentIndex(combo.findData("compression.lz2"))
    assert window._compression_id() == "compression.lz2"
    combo.setCurrentIndex(combo.findData("compression.lz1"))
    assert stack.count() == base + 2  # two tries, two steps

    stack.undo()
    assert window._compression_id() == "compression.lz2"
    stack.undo()
    assert window._compression_id() == first
    stack.redo()
    assert window._compression_id() == "compression.lz2"


def test_the_projects_own_settings_are_undoable(qtbot, tmp_path, monkeypatch) -> None:
    """The two keys a ``.celpix`` holds above its entries.

    The filter needs a **macro**: unchecking the format in force switches the
    view too, and a Ctrl+Z that restored thirty hidden formats while leaving the
    view on a codec nobody chose would be worse than no step. The aspect needs
    ``None`` to survive — the project still *asking*, which is the state that
    leaves a container free to answer on the next load and which the dialog
    cannot express.
    """
    from celpix.ui.pixel_aspect_dialog import PixelAspectDialog

    window, _ = _open(qtbot, tmp_path)
    stack = window._undo_stack
    combo = window._pixel_preset
    current = combo.currentData()
    # Only the real rows: the dropdown is grouped, and a heading carries a
    # sentinel rather than a preset id.
    ids = [
        combo.itemData(i)
        for i in range(combo.count())
        if isinstance(combo.itemData(i), str)
    ]
    other = next(i for i in ids if i != current)
    base = stack.count()

    window._apply_pixel_filter({other})  # hides the format in force, so it switches
    assert combo.currentData() == other
    assert current in window._workspace.hidden_pixel_presets
    assert stack.count() == base + 1  # one macro, not two steps
    stack.undo()
    assert not window._workspace.hidden_pixel_presets
    assert window._doc.pixel_config.interpret_preset_id == current

    assert window._workspace.pixel_aspect is None  # nobody has answered yet
    monkeypatch.setattr(PixelAspectDialog, "ask", staticmethod(lambda *_a: (2, 1)))
    window._on_pixel_aspect()
    assert window._workspace.pixel_aspect == (2, 1)
    stack.undo()
    assert window._workspace.pixel_aspect is None  # the question, not a 1:1


def test_the_base_palette_row_is_one_step_and_never_a_re_read(qtbot, tmp_path) -> None:
    """The base is a reading, not an edit: on the stack, no revision stamped, and
    landed on the document already loaded.

    That last part is what makes it safe on a *pixel* entry, whose document holds
    unsaved edits the file has never seen — a re-read to recover the number the
    file states would take those with it. So undo carries the resolved base back
    itself, and the entry's own answer returns to "whatever the file said".
    """
    window, _ = _open(qtbot, tmp_path)
    entry = window._workspace.current
    stack = window._undo_stack
    before = stack.count()
    assert entry.palette_row_base is None  # a raw file states none

    window._row_base.setValue(5)
    assert stack.count() == before + 1
    assert (entry.palette_row_base, window._doc.palette_row_base) == (5, 5)
    assert not entry.pixel_dirty  # a reading of the bytes, not a change to them

    stack.undo()
    assert entry.palette_row_base is None
    assert window._doc.palette_row_base == 0  # the file's answer, back in force
    stack.redo()
    assert (entry.palette_row_base, window._doc.palette_row_base) == (5, 5)

    # Re-entering the value it already holds is not a second step.
    window._row_base.setValue(5)
    assert stack.count() == before + 1


def test_unpin_all_drops_every_region_and_arms_with_the_pinning(
    qtbot, tmp_path
) -> None:
    """Both unpins hang off whether anything is *pinned*, which only the pin
    gesture changes - so landing a set of regions is what has to re-arm them, not
    the next selection change. Unpin All needs no selection at all, and is one
    undo step back to the whole set."""
    window, _ = _open(qtbot, tmp_path)
    stack = window._undo_stack
    assert not window._unpin_all_action.isEnabled()

    window._set_linear_selection(0, 3)
    window._palette_row.setValue(2)
    window._pin_selection()
    pinned = window._palette_regions
    # Nothing about the selection changed, so a stale sync would leave these grey.
    assert window._unpin_palette_action.isEnabled()
    assert window._unpin_all_action.isEnabled()

    before = stack.count()
    window._clear_selection()
    window._unpin_all()
    assert window._palette_regions.is_empty()
    assert stack.count() == before + 1
    assert not window._unpin_all_action.isEnabled()

    stack.undo()
    assert window._palette_regions == pinned
    assert window._unpin_all_action.isEnabled()


def test_unpinning_only_clears_the_selected_tiles(qtbot, tmp_path) -> None:
    """The interval split has to survive the round trip through the gesture."""
    window, _ = _open(qtbot, tmp_path)
    window._set_linear_selection(0, 7)
    window._palette_row.setValue(2)
    window._pin_selection()

    window._set_linear_selection(2, 3)
    window._unpin_selection()

    area = window._doc.tile_width * window._doc.tile_height
    rows = [window._palette_regions.row_at(t * area, 0) for t in range(8)]
    assert rows == [2, 2, 0, 0, 2, 2, 2, 2]
