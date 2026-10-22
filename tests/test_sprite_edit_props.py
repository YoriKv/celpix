"""Editing a sprite map's picked pieces through the controls that act on a
selection: the property row's record fields, the transform bar's two groups, the
Cell spin, and the Subsprites window's squares
(``docs/design/sprite-map.md`` §10.2-§10.4)."""

from __future__ import annotations

from PySide6.QtCore import Qt

from celpix.project.workspace import TileMode, TileSource
from celpix.ui.main_window import MainWindow
from uihelpers import _make_snes_file, _obj_file


def _sprite(qtbot, tmp_path, parts, *, tiles: int = 8):
    """A window showing a sprite object bound to a bank of ``tiles`` tiles,
    frame 0 holding ``parts`` — records 0, 1, ... in that order, the first
    drawn in front."""
    window = MainWindow()
    qtbot.addWidget(window)
    bank = _make_snes_file(tmp_path)
    if tiles != 8:
        bank.write_bytes(bytes((i * 13 + 1) & 0xFF for i in range(32 * tiles)))
    window._load_pixel(str(bank))
    window._load_pixel(str(_obj_file(tmp_path, parts)))
    entry = window._workspace.current
    entry.tile_source = TileSource(
        mode=TileMode.ENTRY, entry=window._workspace.entries[0]
    )
    entry.doc = None  # re-read through the new binding
    window._activate_entry(window._workspace.entries[0])
    window._activate_entry(entry)
    return window


def _frame(window, at: int = 0):
    return window._doc.shown_frames[at]


def test_the_row_writes_offsets_and_size_into_the_picked_records(
    qtbot, tmp_path
) -> None:
    """X and Y are signed spins over the field's range, ``—`` over a pick that
    disagrees; Size is the bit, captioned with what it spells at the pair. Each
    edit is one step that rewrites the records the pieces are drawn from."""
    window = _sprite(qtbot, tmp_path, [(0, 0, 1), (8, -4, 2)])
    widgets = window._record_props_widgets
    assert set(widgets) == {"x", "y", "size", "drawn"}
    x, size = widgets["x"], widgets["size"]
    assert x.maximum() == 127 and not x.isEnabled()  # nothing picked yet
    assert [size.itemText(i) for i in range(size.count())] == [
        "Small (1x1)",
        "Large (2x2)",
    ]

    window._set_picked_records([0, 1])
    assert x.text() == "—"  # 0 and 8 disagree
    x.setValue(-5)
    assert [sub.x for sub in _frame(window)] == [-5, -5]
    assert window.statusBar().currentMessage() == "Moved 2 subsprites to x = -5."
    assert x.value() == -5 and x.minimum() == -128  # uniform now: no sentinel
    window._undo_stack.undo()
    assert [sub.x for sub in _frame(window)] == [0, 8]
    assert window._picked_records == [0, 1]
    x.setValue(-128)  # a value at the floor is a value, not the dash
    assert _frame(window)[0].x == -128 and x.text() == "-128"
    window._undo_stack.undo()

    window._set_picked_records([1])
    assert size.currentText() == "Small (1x1)"
    size.activated.emit(1)
    assert _frame(window)[1].size() == (2, 2) and _frame(window)[1].y == -4
    assert window.statusBar().currentMessage() == (
        "Set size to Large (2x2) for 1 subsprite."
    )
    assert size.currentText() == "Large (2x2)"


def test_drawn_off_hides_a_piece_and_on_brings_the_same_one_back(
    qtbot, tmp_path
) -> None:
    """On a slot format Drawn is the slot's bit: off clears it, the pick keeps
    the undrawn record, and on puts that very slot back as it was."""
    window = _sprite(qtbot, tmp_path, [(0, 0, 1), (8, 0, 2)])
    drawn = window._record_props_widgets["drawn"]
    window._set_picked_records([1])
    assert drawn.isChecked()

    drawn.click()
    assert [sub.record for sub in _frame(window)] == [0]
    assert window._picked_records == [1] and not drawn.isChecked()
    assert window.statusBar().currentMessage() == "Turned drawn off for 1 subsprite."

    drawn.click()
    back = _frame(window)[1]
    assert (back.record, back.x, back.index) == (1, 8, 2)


def test_the_tile_group_flips_in_place_and_the_block_group_mirrors_the_pick(
    qtbot, tmp_path
) -> None:
    """H toggles each picked piece's bit where it sits. Shift+H mirrors the pick
    as one picture, each piece reflected by its own width: a 2x2 beside a 1x1
    swap sides and the pair's box stays where it was - reflecting left edges
    alone would leave a tile's gap."""
    window = _sprite(qtbot, tmp_path, [(0, 0, 1, 0, True), (16, 0, 5)])
    window._set_picked_records([1])
    assert window._tile_group.flip_h.isEnabled()
    window._tile_group.flip_h.trigger()
    assert _frame(window)[1].flip_h and _frame(window)[1].x == 16
    assert not _frame(window)[0].flip_h
    assert window.statusBar().currentMessage() == "Flipped 1 subsprite."
    window._undo_stack.undo()

    window._set_picked_records([0, 1])
    assert window._block_group.flip_h.isEnabled()
    assert not window._block_group.rotate_cw.isEnabled()
    window._block_group.flip_h.trigger()
    big, small = _frame(window)
    assert (big.x, big.size(), big.flip_h) == (8, (2, 2), True)
    assert (small.x, small.size(), small.flip_h) == (0, (1, 1), True)
    assert window.statusBar().currentMessage() == (
        "Flipped 2 subsprites as one picture."
    )


def test_the_cell_spin_retiles_the_picked_piece(qtbot, tmp_path) -> None:
    """The Cell spin writes the tile into the picked record, and says so in
    subsprites."""
    window = _sprite(qtbot, tmp_path, [(0, 0, 1), (8, 0, 2)])
    window._set_picked_records([1])
    assert window._cell_index.isEnabled() and window._cell_index.value() == 2
    window._cell_index.setValue(9)
    assert [sub.index for sub in _frame(window)] == [1, 9]
    assert window.statusBar().currentMessage() == "Pointed 1 subsprite at tile $9."


def test_the_subsprites_window_picks_by_square_and_reorders_by_drag(
    qtbot, tmp_path
) -> None:
    """A press on a square picks its record, as a canvas press would; a drag
    within the frame reorders, leaving the moved piece picked."""
    window = _sprite(qtbot, tmp_path, [(0, 0, 1), (8, 0, 2), (16, 0, 3)])
    window._show_subsprites()
    panel = window._subsprites._panel
    try:
        square = panel._cell_rect(2).center()
        qtbot.mousePress(panel, Qt.MouseButton.LeftButton, pos=square)
        qtbot.mouseRelease(panel, Qt.MouseButton.LeftButton, pos=square)
        assert window._picked_records == [2]

        qtbot.mousePress(panel, Qt.MouseButton.LeftButton, pos=square)
        front = panel._cell_rect(0).center()
        qtbot.mouseRelease(panel, Qt.MouseButton.LeftButton, pos=front)
        assert [sub.index for sub in _frame(window)] == [3, 1, 2]
        assert window._picked_records == [0]  # the moved piece, at its new record
        assert window.statusBar().currentMessage() == (
            "Moved subsprite 2 to place 0 in frame 0."
        )

        # A view-only map still picks by square; only the drag, a write, refuses.
        window._doc.subsprite_limits = None
        cells = list(window._doc.cells)
        qtbot.mousePress(panel, Qt.MouseButton.LeftButton, pos=square)
        qtbot.mouseRelease(panel, Qt.MouseButton.LeftButton, pos=front)
        assert window._picked_records == [2] and window._doc.cells == cells
        assert "view-only" in window.statusBar().currentMessage()
    finally:
        window._subsprites.hide()


def test_the_record_verbs_wait_for_the_tool_and_a_pick(qtbot, tmp_path) -> None:
    """Outside Edit Subsprites only Copy is offered (the sheet's pixels); inside
    it Cut and Clear need a pick, Paste a buffer, and the row follows the pick
    rather than the held stamp while there is one."""
    window = _sprite(qtbot, tmp_path, [(0, 0, 1), (8, 0, 2)])
    window._set_picked_records([1])
    assert not window._cut_action.isEnabled()
    assert not window._paste_action.isEnabled()

    window._set_stamping(True)
    window._set_picked_records([1])
    assert window._cut_action.isEnabled() and window._clear_action.isEnabled()
    assert window._copy_action.isEnabled()
    assert window._cell_props_target() == "selection"
    assert window._record_props_widgets["x"].value() == 8
    window._set_picked_records([])
    assert not window._cut_action.isEnabled()
    assert not window._copy_action.isEnabled()
    assert not window._paste_action.isEnabled()  # nothing copied yet


def test_a_tile_taken_for_placing_retargets_the_row_to_the_held_piece(
    qtbot, tmp_path
) -> None:
    """The tool both picks and places, so the row follows whichever was pointed
    at last: a sheet pick drops the pick, and Flip H, the Tile group's H and
    Size then say how the next piece lands. The add lays it so and picks it,
    handing the row back to the piece; another sheet pick takes it again."""
    # 64 tiles: a 2x2 at tile 3 spends tiles 3, 4, 19 and 20.
    window = _sprite(qtbot, tmp_path, [(0, 0, 1), (8, 0, 2)], tiles=64)
    window._set_stamping(True)
    window._set_picked_records([1])
    flip_h = window._cell_props_widgets["flip_h"]
    widgets = window._record_props_widgets
    x, size, drawn = widgets["x"], widgets["size"], widgets["drawn"]
    assert x.isEnabled() and not drawn.isHidden()

    window._tile_source_panel.tile_selected.emit(3)  # a sheet pick, for placing
    assert window._picked_records == [] and window._cell_props_target() == "attrs"
    assert size.isEnabled() and size.currentText() == "Small (1x1)"
    assert not x.isEnabled() and drawn.isHidden()
    flip_h.click()
    assert window._stamp_attrs["flip_h"] is True
    size.activated.emit(1)
    assert (
        window._stamp_attrs["size"] == "large" and size.currentText() == "Large (2x2)"
    )
    assert window.statusBar().currentMessage() == "Next subsprite lands Large (2x2)."
    # The Tile group targets the held piece too, so H and the box agree.
    assert window._tile_group.flip_h.isEnabled()
    window._tile_group.flip_h.trigger()
    assert window._stamp_attrs["flip_h"] is False and not flip_h.isChecked()
    window._tile_group.flip_h.trigger()
    assert flip_h.isChecked()

    ox, oy = window._slot_origin(window._sprite_sheet(), 0)
    window._add_held_subsprites(ox, oy, False)
    added = next(sub for sub in _frame(window) if sub.record == 2)
    assert (added.index, added.size(), added.flip_h) == (3, (2, 2), True)
    assert window._picked_records == [2]
    assert window._cell_props_target() == "selection"
    assert x.isEnabled() and x.value() == 0 and size.currentText() == "Large (2x2)"

    window._tile_source_panel.tile_selected.emit(3)
    assert window._picked_records == [] and window._cell_props_target() == "attrs"
    assert size.currentText() == "Large (2x2)"  # the setting outlives the add


def test_an_eyedropped_piece_takes_the_row_and_its_size_is_rewritten_in_place(
    qtbot, tmp_path
) -> None:
    """A right click drops the pick and holds the record: the row shows the
    size its bits spell, and a Size pick rewrites the held record - session
    state, nothing on the stack - so the add lays it at that size. A sheet pick
    displacing it carries the size over with the flips."""
    window = _sprite(qtbot, tmp_path, [(0, 0, 1), (8, 0, 2)], tiles=64)
    window._set_stamping(True)
    window._set_picked_records([0])
    size = window._record_props_widgets["size"]
    cells = list(window._doc.cells)
    ox, oy = window._slot_origin(window._sprite_sheet(), 0)
    window._canvas.stamp_pixel_pressed.emit(ox + 10, oy + 2, Qt.MouseButton.RightButton)
    steps = window._undo_stack.count()  # after the eyedrop's own Palette Row step
    assert window._source_cell == cells[1]
    assert window._picked_records == [] and window._cell_props_target() == "record"
    assert size.isEnabled() and size.currentText() == "Small (1x1)"

    size.activated.emit(1)
    assert window._doc.cells == cells and window._undo_stack.count() == steps
    assert size.currentText() == "Large (2x2)"
    assert window.statusBar().currentMessage() == (
        "Held subsprite's size set to Large (2x2)."
    )
    window._add_held_subsprites(ox, oy, False)
    added = next(sub for sub in _frame(window) if sub.record == 2)
    assert (added.index, added.size()) == (2, (2, 2))

    window._tile_source_panel.tile_selected.emit(3)
    assert window._cell_props_target() == "attrs"
    assert (
        window._stamp_attrs["size"] == "large" and size.currentText() == "Large (2x2)"
    )


def _with_sheet(window):
    """The tile source tab up, so its ring converges (hidden, it is skipped)."""
    window.show()
    window._tile_source_dock.setVisible(True)
    window._tile_source_dock.raise_()
    window._refresh_tile_source()
    return window._tile_source_panel


def test_the_tile_source_rings_every_tile_a_piece_spends(qtbot, tmp_path) -> None:
    """The ring is the footprint, not the corner: a picked 2x2 rings four
    squares; a tile held for placing rings what the next click would spend,
    following the Size setting; a sweep rings the pieces it splits into, and
    the add lays exactly those - one large and two small - and picks them."""
    window = _sprite(qtbot, tmp_path, [(0, 0, 1), (16, 0, 5, 0, True)], tiles=64)
    panel = _with_sheet(window)
    window._set_picked_records([1])
    assert panel._marked == {5, 6, 21, 22}
    window._set_picked_records([0])
    assert panel._marked == {1}

    window._set_stamping(True)
    panel.tile_selected.emit(3)
    assert panel._marked == {3}
    window._record_props_widgets["size"].activated.emit(1)
    assert panel._marked == {3, 4, 19, 20}

    panel.area_selected.emit([[8, 9, 10], [24, 25, 26]])
    assert panel._marked == {8, 9, 24, 25, 10, 26}
    assert not window._record_props_widgets["size"].isEnabled()
    ox, oy = window._slot_origin(window._sprite_sheet(), 0)
    window._add_held_subsprites(ox, oy, False)
    added = sorted((s for s in _frame(window) if s.record >= 2), key=lambda s: s.record)
    assert [(s.index, s.size(), s.x, s.y) for s in added] == [
        (8, (2, 2), 0, 0),
        (10, (1, 1), 16, 0),
        (26, (1, 1), 16, 8),
    ]
    assert window._picked_records == [2, 3, 4]
    window._set_stamping(False)
    assert panel._marked == {26}  # the primary's own footprint, tool or no tool


def test_a_piece_whose_footprint_leaves_the_bank_is_refused(qtbot, tmp_path) -> None:
    """A 2x2 at the last tile of a row spends a tile past a 32-tile bank: the
    click lays nothing and the status line names the tile and the bank's last."""
    window = _sprite(qtbot, tmp_path, [(0, 0, 1)], tiles=32)
    window._set_stamping(True)
    window._tile_source_panel.tile_selected.emit(15)
    window._record_props_widgets["size"].activated.emit(1)
    cells = list(window._doc.cells)
    ox, oy = window._slot_origin(window._sprite_sheet(), 0)
    window._add_held_subsprites(ox, oy, False)
    assert window._doc.cells == cells
    message = window.statusBar().currentMessage()
    assert (
        "2x2 piece at tile $F spends tile $20, past the tile source's last ($1F)"
        in message
    )
