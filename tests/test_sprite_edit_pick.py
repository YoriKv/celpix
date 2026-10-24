"""The sprite map's pick as records: several of them, migrated across edits and
carried by the undo step, plus the canvas's pixel-reporting stamp gestures and
origin marks (``docs/design/sprite-map.md`` §8.1, §10.1, §11)."""

from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import QEvent, QPointF, QRect, Qt
from PySide6.QtGui import QImage, QMouseEvent
from PySide6.QtWidgets import QApplication

from celpix.core.sprite import FrameLayout
from celpix.project.workspace import TileMode, TileSource
from celpix.ui.canvas import Canvas
from celpix.ui.main_window import MainWindow
from uihelpers import _make_snes_file, _obj_file

_PLAIN = Qt.KeyboardModifier.NoModifier
_CTRL = Qt.KeyboardModifier.ControlModifier
_SHIFT = Qt.KeyboardModifier.ShiftModifier


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
    return window, entry


def test_presses_pick_records_and_the_pick_is_the_selection(qtbot, tmp_path) -> None:
    """Plain picks one, Ctrl toggles the front piece, Shift spans the stack from
    the anchor — and whatever is picked is what the cell controls act on, since
    a sheet square names no record."""
    # Two pieces stacked at the origin, a third apart at (24, 5).
    window, _ = _sprite(qtbot, tmp_path, [(0, 0, 1), (0, 0, 2), (24, 5, 3)])
    canvas = window._canvas

    window._on_pixel_picked(26, 7, _PLAIN)
    assert window._picked_records == [2] and window._selected_cells() == [2]
    assert canvas._pick_outline == [QRect(24, 5, 8, 8)]
    status = window.statusBar().currentMessage()
    assert "subsprite 2 of 3 at (+24, +5)" in status and "frame box" in status

    window._on_pixel_picked(2, 2, _CTRL)  # the front of the stack joins
    assert window._selected_cells() == [2, 0] and len(canvas._pick_outline) == 2
    assert window.statusBar().currentMessage() == "2 subsprites picked in frame 0"
    window._on_pixel_picked(14, 2, _CTRL)  # a toggle that misses changes nothing
    assert window._picked_records == [2, 0]
    window._on_pixel_picked(2, 2, _CTRL)  # ...and a second toggle takes it out
    assert window._picked_records == [2]

    # Shift spans from the anchor to the clicked piece, which ends up primary.
    window._on_pixel_picked(2, 2, _PLAIN)
    window._on_pixel_picked(26, 7, _SHIFT)
    assert window._picked_records == [0, 1, 2] and window._pick_anchor == 0
    assert len(canvas._pick_outline) == 3
    window._on_pixel_picked(2, 2, _SHIFT)  # the anchor is kept, so this shrinks it
    assert window._picked_records == [0]

    window._on_pixel_picked(14, 2, _PLAIN)  # empty sheet clears
    assert window._picked_records == [] and canvas._pick_outline is None


def test_the_pick_follows_its_records_through_edits_and_undo(qtbot, tmp_path) -> None:
    """A record names the piece, so an edit to it, its undo and a re-render keep
    the pick on it; a pick the step carries is what each side puts back; a
    record that is not drawn stays picked with no outline, and one past the
    cells is dropped."""
    window, _ = _sprite(qtbot, tmp_path, [(0, 0, 1), (8, 0, 2), (24, 5, 3)])
    doc, canvas = window._doc, window._canvas

    window._set_picked_records([1])
    cells = list(doc.cells)
    cells[1] = replace(cells[1], index=5)
    assert window._apply_cells(cells, "retile")
    assert window._picked_records == [1] and len(canvas._pick_outline) == 1
    assert window._picked_subsprite_record().index == 5
    window._undo_stack.undo()
    assert window._picked_records == [1]
    assert window._picked_subsprite_record().index == 2

    window._set_picked_records([0])
    cells = list(window._doc.cells)
    cells[0] = replace(cells[0], index=6)
    assert window._apply_cells(cells, "retile", pick=([0], [0, 2]))
    assert window._picked_records == [0, 2]
    window._undo_stack.undo()
    assert window._picked_records == [0]
    window._undo_stack.redo()
    assert window._picked_records == [0, 2]

    # Record 5 is a slot of frame 0 with its drawn bit clear; 10_000 no cell.
    window._set_picked_records([5, 1, 10_000])
    window._revalidate_subsprite()
    assert window._picked_records == [5, 1]
    assert [record for record, _ in window._picked_subsprites()] == [1]
    assert canvas._pick_outline == [QRect(8, 0, 8, 8)]


def test_origin_marks_follow_the_origin_box_and_leave_with_the_sprite(
    qtbot, tmp_path
) -> None:
    """With Origin ticked, one mark per frame slot at object (0, 0) — pixel
    (-left, -top) of the slot — with the frame box as the span its axes are
    drawn over; none with the box off, and none on a document with no frames."""
    window, _ = _sprite(qtbot, tmp_path, [(-8, -4, 1), (8, 0, 2)])
    canvas = window._canvas
    assert canvas._origin_marks == ()
    steps = window._undo_stack.count()
    window._show_origin_box.setChecked(True)
    assert window._undo_stack.count() == steps + 1 and window._doc.view.show_origin
    # The box is the pieces' bounds rounded out to whole tiles - (-8, -4) comes
    # out (-8, -8) - so the origin is 8 pixels into the slot each way.
    assert window._sprite_sheet().box == (-8, -8, 24, 16)
    assert canvas._origin_marks == ((8, 8),)
    assert canvas._origin_extent == (-8, -8, 24, 16)

    window._activate_entry(window._workspace.entries[0])  # the plain bank
    assert canvas._origin_marks == ()
    window._undo_stack.undo()  # reaches back to the sprite and unticks the box
    assert not window._show_origin_box.isChecked() and canvas._origin_marks == ()


def test_the_frame_bar_stands_in_for_the_arrangement_row_and_boxes_the_frames(
    qtbot, tmp_path
) -> None:
    """On a sprite map the Frame bar shows where the Arrangement row is hidden,
    and back on a bank the two swap again. A size is a minimum from the aligned
    side and an alignment pins the origin, each one merging step per control,
    the sheet re-cut on each; the pieces always fit; an entry switch restores
    the entry's own layout."""
    window, entry = _sprite(qtbot, tmp_path, [(-8, -4, 1), (8, 0, 2)])
    assert not window._frame_toolbar.isHidden() and window._arrange_toolbar.isHidden()
    steps = window._undo_stack.count()

    window._frame_width.setValue(64)
    assert window._doc.view.frame.width == 64
    assert window._sprite_sheet().box == (-8, -8, 64, 16)
    assert window._undo_stack.count() == steps + 1
    window._frame_width.setValue(72)  # a run on one spin is one step
    assert window._undo_stack.count() == steps + 1
    assert window._sprite_sheet().box == (-8, -8, 72, 16)

    window._frame_align_x.setCurrentIndex(window._frame_align_x.findData("center"))
    assert window._undo_stack.count() == steps + 2
    # Centred: half the width asked, or the longer side - 36 rounded to 40.
    assert window._sprite_sheet().box == (-40, -8, 80, 16)
    window._undo_stack.undo()
    assert window._sprite_sheet().box == (-8, -8, 72, 16)
    assert window._frame_align_x.currentData() == "fit"
    window._undo_stack.undo()
    assert window._sprite_sheet().box == (-8, -8, 24, 16)
    assert window._frame_width.value() == 0

    window._frame_align_y.setCurrentIndex(window._frame_align_y.findData("bottom"))
    assert window._sprite_sheet().box == (-8, -8, 24, 16)  # a piece reaches y = 8
    window._activate_entry(window._workspace.entries[0])
    assert window._frame_toolbar.isHidden() and not window._arrange_toolbar.isHidden()
    window._activate_entry(entry)
    assert not window._frame_toolbar.isHidden()
    assert window._frame_align_y.currentData() == "bottom"
    assert window._doc.view.frame == FrameLayout(align_y="bottom")


def test_the_stamp_reports_pixels_when_told_to(qtbot) -> None:
    """In pixel-reporting mode the stamp gestures speak image pixels through
    their own signals: a left drag as press, moves and release; a right drag that
    leaves its tile as a normalised band, a right click as a press; a right press
    mid-drag cancels. The slot signals stay silent throughout."""
    canvas = Canvas()
    qtbot.addWidget(canvas)
    image = QImage(32, 16, QImage.Format.Format_RGB32)
    image.fill(0)
    canvas.set_image(image)
    canvas.set_tile_size(8, 8)
    canvas.set_zoom(1.0)
    canvas.set_stamping(True)
    canvas.set_stamp_in_pixels(True)
    canvas.resize(32, 16)

    seen: list[tuple] = []
    canvas.stamp_pixel_pressed.connect(lambda x, y, b: seen.append(("press", x, y, b)))
    canvas.stamp_pixel_moved.connect(lambda x, y: seen.append(("move", x, y)))
    canvas.stamp_pixel_released.connect(lambda x, y: seen.append(("release", x, y)))
    canvas.stamp_pixel_area_picked.connect(lambda *band: seen.append(("band", *band)))
    canvas.stamp_pixel_cancelled.connect(lambda: seen.append(("cancel",)))
    canvas.stamp_pressed.connect(lambda *_: seen.append(("slot",)))
    canvas.stamp_area_picked.connect(lambda *_: seen.append(("slot",)))

    left, right = Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton
    none = Qt.MouseButton.NoButton

    def send(kind, x, y, button, buttons):
        pos = QPointF(x + 0.5, y + 0.5)
        QApplication.sendEvent(
            canvas,
            QMouseEvent(kind, pos, canvas.mapToGlobal(pos), button, buttons, _PLAIN),
        )

    press, move = QEvent.Type.MouseButtonPress, QEvent.Type.MouseMove
    release = QEvent.Type.MouseButtonRelease

    send(press, 3, 4, left, left)
    send(move, 5, 4, none, left)
    send(move, 5, 4, none, left)  # the same pixel again reports nothing
    send(release, 6, 4, left, none)
    assert seen == [("press", 3, 4, left), ("move", 5, 4), ("release", 6, 4)]

    seen.clear()
    send(press, 12, 9, right, right)
    send(move, 1, 1, none, right)
    send(release, 1, 1, right, none)
    send(press, 1, 1, right, right)  # stays in its tile: a click
    send(release, 3, 2, right, none)
    assert seen == [("band", 1, 1, 12, 9), ("press", 1, 1, right)]

    seen.clear()
    send(press, 3, 4, left, left)
    send(press, 3, 4, right, left | right)
    send(release, 3, 4, left, right)  # the drag is over; nothing is released
    assert seen == [("press", 3, 4, left), ("cancel",)]
