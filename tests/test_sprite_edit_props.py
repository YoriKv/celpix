"""Editing a sprite map's picked pieces through the controls that act on a
selection: the property row's record fields, the transform bar's two groups, the
Cell spin, and the Subsprites window's squares
(``docs/design/sprite-map.md`` §10.2-§10.4)."""

from __future__ import annotations

from PySide6.QtCore import Qt

from celpix.project.workspace import TileMode, TileSource
from celpix.ui.main_window import MainWindow
from uihelpers import _make_snes_file, _obj_file


def _sprite(qtbot, tmp_path, parts):
    """A window showing a sprite object bound to a bank, frame 0 holding ``parts``
    — records 0, 1, ... in that order, the first drawn in front."""
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_make_snes_file(tmp_path)))
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
