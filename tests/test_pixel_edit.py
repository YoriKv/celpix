"""Pixel editing mode: the tools, the pen, the float clipboard, and the toggle.

Exercises the controller through the same entry points the canvas drives — the
``_on_pixel_*`` gesture handlers and the clipboard/transform dispatch — plus one
real mouse event to guard the canvas's pixel mapping and mode branching. The
Qt-free rasterizer math lives in ``test_draw.py``; here we check that a gesture
becomes the right *undoable byte edit* on the live document, including under a
block arrangement (the compose/split round-trip).
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QPointF, QRect, Qt
from PySide6.QtGui import QMouseEvent

from celpix.ui.canvas import Canvas
from celpix.ui.main_window import MainWindow
from celpix.ui.main_window.transform import OP_FLIP_H
from celpix.ui.tools import EditMode, Tool


def _window(qtbot, tmp_path, tiles: int = 8):
    """A window with a 4bpp file open, in pixel mode with a non-zero pen."""
    px = tmp_path / "s.4bpp.sfc"
    px.write_bytes(bytes((i * 13 + 1) & 0xFF for i in range(32 * tiles)))
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(px))
    window._set_edit_mode(EditMode.PIXEL)
    window._subpalette.setValue(0)
    window._palette_panel.select_index(5)  # pen = index 5 (within the subpalette)
    return window


def _pixel(window, x: int, y: int) -> int:
    return window._window_grid().get(x, y)


# -- the mode toggle -------------------------------------------------------
def test_the_toggle_arms_pixel_mode_and_tile_mode_takes_it_back(
    qtbot, tmp_path
) -> None:
    window = _window(qtbot, tmp_path)  # the helper leaves it in pixel mode
    assert window._edit_mode is EditMode.PIXEL
    assert window._canvas._edit_mode is EditMode.PIXEL
    # Pixel mode is rectangle-only: the shape picker is forced and locked.
    assert window._selection_shape.currentData().name == "RECT"
    assert not window._selection_shape.isEnabled()
    assert window._tools_panel.isEnabled()
    assert window._edit_mode_action.isChecked()

    window._set_edit_mode(EditMode.TILE)
    assert window._canvas._edit_mode is EditMode.TILE
    assert window._selection_shape.isEnabled()
    assert not window._tools_panel.isEnabled()


def test_switching_mode_clears_the_selection_and_what_the_status_says(
    qtbot, tmp_path
) -> None:
    """A tile selection can't survive a trip through pixel mode, so neither may
    the status line still announcing it."""
    window = _window(qtbot, tmp_path)
    window._set_edit_mode(EditMode.TILE)
    window._on_slots_selected(1, 1)
    assert window._selected_tile == 1
    assert "Selected" in window.statusBar().currentMessage()
    window._set_edit_mode(EditMode.PIXEL)
    window._set_edit_mode(EditMode.TILE)
    assert window._selected_tile is None
    assert window.statusBar().currentMessage() == ""


def test_tile_mode_swaps_to_the_select_tool_still_checked(qtbot, tmp_path) -> None:
    """Tile mode selects rather than paints, so it swaps to Select — and the rail
    keeps that button checked even though the whole rail is disabled."""
    window = _window(qtbot, tmp_path)
    window._on_tool_selected(Tool.PENCIL)
    assert window._tool is Tool.PENCIL
    window._set_edit_mode(EditMode.TILE)
    assert window._tool is Tool.SELECT
    assert window._tools_panel._buttons[Tool.SELECT].isChecked()
    assert not window._tools_panel._buttons[Tool.PENCIL].isChecked()
    assert not window._tools_panel.isEnabled()  # highlighted while disabled


# -- canvas gesture mapping ------------------------------------------------
def test_canvas_emits_pixel_gesture_only_in_pixel_mode(qtbot) -> None:
    from celpix.core.index_grid import IndexGrid
    from celpix.core.palette import Palette
    from celpix.ui import render_bridge

    canvas = Canvas()
    qtbot.addWidget(canvas)
    canvas.set_tile_size(8, 8)
    canvas.set_zoom(4)
    canvas.set_image(render_bridge.render(IndexGrid(8, 8), Palette([0xFF000000])))

    def press(button=Qt.MouseButton.LeftButton):
        # Pixel (2,3) at zoom 4 sits at device (8..11, 12..15); pick the middle.
        return QMouseEvent(
            QEvent.Type.MouseButtonPress,
            QPointF(9, 13),
            button,
            button,
            Qt.KeyboardModifier.NoModifier,
        )

    canvas.set_edit_mode(EditMode.PIXEL)
    with qtbot.waitSignal(canvas.pixel_pressed, timeout=500) as blocker:
        canvas.mousePressEvent(press())
    assert blocker.args[:2] == [2, 3]

    # Tile mode instead reports a tile slot and never a pixel gesture.
    canvas.set_edit_mode(EditMode.TILE)
    with qtbot.waitSignal(canvas.slots_selected, timeout=500):
        canvas.mousePressEvent(press())


# -- drawing tools ---------------------------------------------------------
def test_pencil_paints_one_pixel_as_single_undo(qtbot, tmp_path) -> None:
    window = _window(qtbot, tmp_path)
    window._tool = Tool.PENCIL
    before = window._undo_stack.count()
    window._on_pixel_pressed(2, 3, Qt.MouseButton.LeftButton)
    window._on_pixel_released(2, 3)
    assert _pixel(window, 2, 3) == 5
    assert window._undo_stack.count() == before + 1
    window._undo_stack.undo()
    assert _pixel(window, 2, 3) != 5


def test_pencil_drag_connects_samples(qtbot, tmp_path) -> None:
    window = _window(qtbot, tmp_path)
    window._tool = Tool.PENCIL
    window._on_pixel_pressed(0, 0, Qt.MouseButton.LeftButton)
    window._on_pixel_moved(3, 0)
    window._on_pixel_released(3, 0)
    # The whole run between the samples is painted (no gaps).
    assert all(_pixel(window, x, 0) == 5 for x in range(4))


def test_line_tool_paints_endpoints(qtbot, tmp_path) -> None:
    window = _window(qtbot, tmp_path)
    window._tool = Tool.LINE
    window._on_pixel_pressed(0, 0, Qt.MouseButton.LeftButton)
    window._on_pixel_moved(3, 3)
    window._on_pixel_released(3, 3)
    assert _pixel(window, 0, 0) == 5 and _pixel(window, 3, 3) == 5


def test_rect_tool_draws_outline_not_centre(qtbot, tmp_path) -> None:
    window = _window(qtbot, tmp_path)
    window._tool = Tool.RECT
    window._on_pixel_pressed(0, 0, Qt.MouseButton.LeftButton)
    window._on_pixel_released(2, 2)
    assert _pixel(window, 0, 0) == 5 and _pixel(window, 2, 0) == 5
    assert _pixel(window, 1, 1) != 5  # the centre is hollow


def test_fill_floods_a_region(qtbot, tmp_path) -> None:
    window = _window(qtbot, tmp_path)
    # Clear a rectangle to 0 first, then fill from inside it.
    window._tool = Tool.SELECT
    window._marquee = QRect(0, 0, 4, 4)
    window._pixel_clear()
    window._tool = Tool.FILL
    window._on_pixel_pressed(1, 1, Qt.MouseButton.LeftButton)
    assert _pixel(window, 0, 0) == 5 and _pixel(window, 3, 3) == 5


def test_a_selection_masks_the_drawing_tools(qtbot, tmp_path) -> None:
    """A marquee is a mask: a stroke run across its edge changes only what is
    inside it, so the art beyond the selection is safe from an overrun."""
    window = _window(qtbot, tmp_path)
    window._marquee = QRect(0, 0, 4, 4)
    window._canvas.set_marquee(window._marquee)
    window._tool = Tool.PENCIL
    beyond = [_pixel(window, x, 0) for x in range(4, 8)]
    window._on_pixel_pressed(0, 0, Qt.MouseButton.LeftButton)
    window._on_pixel_moved(7, 0)  # drag straight out of the selection
    window._on_pixel_released(7, 0)
    assert all(_pixel(window, x, 0) == 5 for x in range(4))
    assert [_pixel(window, x, 0) for x in range(4, 8)] == beyond


def test_a_selection_confines_a_fill(qtbot, tmp_path) -> None:
    window = _window(qtbot, tmp_path)
    window._tool = Tool.SELECT
    window._marquee = QRect(0, 0, 8, 8)
    window._pixel_clear()  # one flat region to flood
    window._marquee = QRect(0, 0, 4, 4)
    window._tool = Tool.FILL
    window._on_pixel_pressed(1, 1, Qt.MouseButton.LeftButton)
    assert _pixel(window, 3, 3) == 5  # inside the selection
    assert _pixel(window, 5, 5) == 0  # the same flat region, but outside it
    window._on_pixel_pressed(5, 5, Qt.MouseButton.LeftButton)  # seed outside
    assert _pixel(window, 5, 5) == 0  # ...fills nothing


def test_right_click_eyedropper_sets_pen(qtbot, tmp_path) -> None:
    window = _window(qtbot, tmp_path)
    # Paint index 5 somewhere, move the pen off it, then sample it back.
    window._tool = Tool.PENCIL
    window._on_pixel_pressed(2, 2, Qt.MouseButton.LeftButton)
    window._on_pixel_released(2, 2)
    window._palette_panel.select_index(1)
    assert window._pen_value() == 1
    window._on_pixel_pressed(2, 2, Qt.MouseButton.RightButton)
    assert window._pen_value() == 5


def test_right_click_eyedropper_sweeps_while_held(qtbot) -> None:
    """A held right button samples each new pixel it passes over (a re-emitted
    right press), so the picker can be swept instead of clicked pixel by pixel;
    the same pixel isn't re-sampled, and releasing ends the sweep."""
    from celpix.core.index_grid import IndexGrid
    from celpix.core.palette import Palette
    from celpix.ui import render_bridge

    canvas = Canvas()
    qtbot.addWidget(canvas)
    canvas.set_tile_size(8, 8)
    canvas.set_zoom(4)
    canvas.set_image(render_bridge.render(IndexGrid(8, 8), Palette([0xFF000000])))
    canvas.set_edit_mode(EditMode.PIXEL)

    samples: list[tuple[int, int, object]] = []
    canvas.pixel_pressed.connect(lambda x, y, b: samples.append((x, y, b)))

    def _event(kind, x, y, button, buttons):
        return QMouseEvent(
            kind, QPointF(x, y), button, buttons, Qt.KeyboardModifier.NoModifier
        )

    right = Qt.MouseButton.RightButton
    none = Qt.MouseButton.NoButton
    # Pixel (px, 0) at zoom 4 spans device x = px*4 .. px*4+3; pick the middle.
    canvas.mousePressEvent(_event(QEvent.Type.MouseButtonPress, 1, 1, right, right))
    canvas.mouseMoveEvent(_event(QEvent.Type.MouseMove, 9, 1, none, right))  # (2,0)
    canvas.mouseMoveEvent(_event(QEvent.Type.MouseMove, 10, 1, none, right))  # same
    canvas.mouseMoveEvent(_event(QEvent.Type.MouseMove, 21, 1, none, right))  # (5,0)
    canvas.mouseReleaseEvent(_event(QEvent.Type.MouseButtonRelease, 21, 1, right, none))
    # After release the sweep is over: a further right-button move samples nothing.
    canvas.mouseMoveEvent(_event(QEvent.Type.MouseMove, 29, 1, none, right))  # (7,0)

    assert [(x, y) for x, y, _ in samples] == [(0, 0), (2, 0), (5, 0)]
    assert all(button == right for _, _, button in samples)


def test_space_pan_takes_over_the_mouse(qtbot) -> None:
    """While pan is armed a left drag pans the view and neither selects nor paints."""
    from celpix.core.index_grid import IndexGrid
    from celpix.core.palette import Palette
    from celpix.ui import render_bridge

    canvas = Canvas()
    qtbot.addWidget(canvas)
    canvas.set_zoom(4)
    canvas.set_image(render_bridge.render(IndexGrid(8, 8), Palette([0xFF000000])))
    canvas.set_edit_mode(EditMode.PIXEL)

    presses: list = []
    moves: list = []
    pans: list = []
    canvas.pixel_pressed.connect(lambda *a: presses.append(a))
    canvas.pixel_moved.connect(lambda *a: moves.append(a))
    canvas.pan_requested.connect(lambda dx, dy: pans.append((dx, dy)))

    left = Qt.MouseButton.LeftButton
    none = Qt.MouseButton.NoButton

    def _ev(kind, x, y, button, buttons):
        return QMouseEvent(
            kind, QPointF(x, y), button, buttons, Qt.KeyboardModifier.NoModifier
        )

    canvas.set_pan_mode(True)
    canvas.mousePressEvent(_ev(QEvent.Type.MouseButtonPress, 4, 4, left, left))
    canvas.mouseMoveEvent(_ev(QEvent.Type.MouseMove, 40, 40, none, left))
    canvas.mouseReleaseEvent(_ev(QEvent.Type.MouseButtonRelease, 40, 40, left, none))

    assert presses == []  # the gesture never fired — panning swallowed the press
    assert moves == []
    assert pans  # the drag emitted at least one pan step


def test_ctrl_wheel_zooms_and_a_plain_wheel_is_left_to_scroll(qtbot) -> None:
    """Ctrl+wheel is the zoom; an unmodified wheel must fall through to the
    scroll area (it is *ignored*, not swallowed) so it still scrolls the view."""
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QImage, QWheelEvent

    from celpix.core.index_grid import IndexGrid
    from celpix.core.palette import Palette
    from celpix.ui import render_bridge

    canvas = Canvas()
    qtbot.addWidget(canvas)
    canvas.set_zoom(4)
    canvas.set_image(render_bridge.render(IndexGrid(8, 8), Palette([0xFF000000])))

    steps: list[int] = []
    canvas.zoom_requested.connect(lambda s, _pos: steps.append(s))

    def _wheel(dy, modifier=Qt.KeyboardModifier.ControlModifier):
        return QWheelEvent(
            QPointF(4, 4),
            QPointF(4, 4),
            QPoint(0, 0),
            QPoint(0, dy),
            Qt.MouseButton.NoButton,
            modifier,
            Qt.ScrollPhase.NoScrollPhase,
            False,
        )

    canvas.wheelEvent(_wheel(120))  # one notch up -> zoom in
    canvas.wheelEvent(_wheel(-120))  # one notch down -> zoom out
    assert steps == [1, -1]

    # No Ctrl: no zoom, and the event is passed up rather than accepted, which is
    # what lets the enclosing scroll area scroll on it.
    for modifier in (
        Qt.KeyboardModifier.NoModifier,
        Qt.KeyboardModifier.ShiftModifier,  # Shift is no longer the zoom
    ):
        plain = _wheel(120, modifier=modifier)
        canvas.wheelEvent(plain)
        assert steps == [1, -1]
        assert not plain.isAccepted()

    # With no image there is nothing to zoom; even Ctrl+wheel does nothing.
    canvas.set_image(QImage())
    canvas.wheelEvent(_wheel(120))
    assert steps == [1, -1]


def test_zoom_steps_from_the_menu_anchor_on_the_viewport_centre(
    qtbot, tmp_path
) -> None:
    """The View ▸ Zoom actions drive the same zoom the wheel does, without a
    cursor to anchor on — so they must still land inside the spin's range."""
    window = _window(qtbot, tmp_path)
    window._zoom.setValue(4)
    window._zoom_steps(1)
    assert window._zoom.value() == 5
    window._zoom_steps(-1)
    assert window._zoom.value() == 4
    for _ in range(50):  # clamps at the ends rather than running away
        window._zoom_steps(-1)
    assert window._zoom.value() == window._zoom.minimum()


def test_zoom_request_steps_and_clamps(qtbot, tmp_path) -> None:
    window = _window(qtbot, tmp_path)
    window._zoom.setValue(4)
    window._on_zoom_requested(2, QPointF(0, 0))
    assert window._zoom.value() == 6
    window._on_zoom_requested(-100, QPointF(0, 0))
    assert window._zoom.value() == window._zoom.minimum()
    window._on_zoom_requested(100, QPointF(0, 0))
    assert window._zoom.value() == window._zoom.maximum()


def test_right_click_menu_is_suppressed_in_pixel_mode(qtbot, tmp_path, monkeypatch):
    """The canvas context menu would swallow the right-click eyedropper, so the
    handler returns in pixel mode before it builds (and modally execs) a menu.

    Asserted at the Python level — reaching ``menu.exec()`` under the offscreen
    platform would block and hang the run, which is exactly what we're avoiding.
    """
    from PySide6.QtCore import QPoint

    window = _window(qtbot, tmp_path)

    def _boom():
        raise AssertionError("built the context menu in pixel mode")

    # Menu construction pulls the clipboard actions first; the guard must return
    # before that, so patching it to explode proves nothing downstream ran.
    monkeypatch.setattr(window, "_clipboard_actions", _boom)
    window._show_canvas_menu(QPoint(0, 0))  # returns early: no menu, no raise


# -- floating selection & pixel clipboard ----------------------------------
def test_copy_clear_paste_round_trips(qtbot, tmp_path) -> None:
    window = _window(qtbot, tmp_path)
    window._tool = Tool.SELECT
    original = [[_pixel(window, x, y) for x in range(3)] for y in range(3)]
    window._marquee = QRect(0, 0, 3, 3)
    window._pixel_copy()
    window._pixel_clear()
    assert _pixel(window, 1, 1) == 0
    window._pixel_paste()  # arrives floating, centred on the view
    assert window._float_grid is not None
    window._float_pos = (0, 0)  # drag it back over the hole...
    window._commit_float()  # ...and set it down
    assert [[_pixel(window, x, y) for x in range(3)] for y in range(3)] == original


def test_move_lands_as_a_single_undo_step(qtbot, tmp_path) -> None:
    """Landing a moved float writes its hole and its destination as one edit."""
    window = _window(qtbot, tmp_path)
    window._tool = Tool.SELECT
    src = _pixel(window, 0, 0)
    window._marquee = QRect(0, 0, 2, 2)
    before = window._undo_stack.count()
    window._marquee_press(0, 0)  # press inside -> lift a moving float
    assert window._float_grid is not None and window._float_source_rect is not None
    window._marquee_drag(6, 6)  # move it away
    window._commit_float()
    assert window._undo_stack.count() == before + 1  # one step for the whole move
    assert _pixel(window, 0, 0) == 0  # the hole was blanked
    assert _pixel(window, 6, 6) == src  # the pixels landed at the destination


def test_bare_click_clears_the_selection(qtbot, tmp_path) -> None:
    """A click with no drag deselects rather than selecting a single pixel."""
    window = _window(qtbot, tmp_path)
    window._tool = Tool.SELECT
    window._marquee = QRect(0, 0, 4, 4)
    window._canvas.set_marquee(window._marquee)
    # Press and release on one pixel, well outside the existing selection.
    window._on_pixel_pressed(10, 10, Qt.MouseButton.LeftButton)
    window._on_pixel_released(10, 10)
    assert window._marquee is None


def _move_selection(window, from_xy, to_xy) -> None:
    """Drag the selection at ``from_xy`` to ``to_xy``, as the canvas drives it."""
    window._on_pixel_pressed(*from_xy, Qt.MouseButton.LeftButton)
    window._on_pixel_moved(*to_xy)
    window._on_pixel_released(*to_xy)


def test_a_moved_selection_floats_until_it_is_dropped(qtbot, tmp_path) -> None:
    """A move writes nothing while the pixels are up: the destination is untouched
    however far they travel, and only the position they were lifted from clears."""
    window = _window(qtbot, tmp_path)
    window._tool = Tool.SELECT
    src, dest = _pixel(window, 0, 0), _pixel(window, 6, 6)
    window._marquee = QRect(0, 0, 2, 2)
    _move_selection(window, (0, 0), (6, 6))
    assert window._float_grid is not None
    assert window._marquee == QRect(6, 6, 2, 2)  # the selection followed the pixels
    # Still in the air, so the document has both ends exactly as they were.
    assert _pixel(window, 0, 0) == src and _pixel(window, 6, 6) == dest

    # Dragging on doesn't leave a second hole - the one owed stays at the origin.
    _move_selection(window, (6, 6), (10, 4))
    assert window._float_source_rect == QRect(0, 0, 2, 2)
    assert _pixel(window, 6, 6) == dest

    # Dropping the selection lands them: the source clears, the destination is
    # overwritten, and the whole move is the one step it always was.
    before = window._undo_stack.count()
    window._pixel_key(Qt.Key.Key_Escape, False, False)
    assert window._undo_stack.count() == before + 1
    assert window._float_grid is None and window._marquee is None
    assert _pixel(window, 0, 0) == 0
    assert _pixel(window, 10, 4) == src


def test_undoing_a_move_takes_the_pixels_back_out_of_the_air(qtbot, tmp_path) -> None:
    """A drag is one step, and nothing was written - so undo just puts the float
    back where it came from, and redo puts it back in the air."""
    window = _window(qtbot, tmp_path)
    window._tool = Tool.SELECT
    src = _pixel(window, 0, 0)
    window._marquee = QRect(0, 0, 2, 2)
    before = window._undo_stack.count()
    _move_selection(window, (0, 0), (6, 6))
    assert window._undo_stack.count() == before + 1

    window._undo_stack.undo()
    assert window._float_grid is None
    assert window._marquee == QRect(0, 0, 2, 2)
    assert _pixel(window, 0, 0) == src  # never written, so nothing to restore

    window._undo_stack.redo()
    assert window._float_grid is not None
    assert window._marquee == QRect(6, 6, 2, 2)
    assert window._float_source_rect == QRect(0, 0, 2, 2)  # still owes its hole


def test_undoing_a_landing_puts_the_pixels_back_in_the_air(qtbot, tmp_path) -> None:
    """Undoing a float's landing lifts the same float again - hole and all.

    The screen looks the same either side of a landing, so an undo that reverted
    only the bytes would leave pixels that read as floating but are really down:
    the next drag would then blank the destination they were sitting on rather
    than the place they came from.
    """
    window = _window(qtbot, tmp_path)
    window._tool = Tool.SELECT
    src, dest = _pixel(window, 0, 0), _pixel(window, 6, 6)
    window._marquee = QRect(0, 0, 2, 2)
    _move_selection(window, (0, 0), (6, 6))
    steps = window._undo_stack.count()
    _move_selection(window, (14, 14), (14, 14))  # a click away lands them...
    assert window._undo_stack.count() == steps + 1  # ...as one step, not two
    assert _pixel(window, 0, 0) == 0 and _pixel(window, 6, 6) == src

    window._undo_stack.undo()
    assert window._float_grid is not None  # back in the air...
    assert window._marquee == QRect(6, 6, 2, 2)
    assert window._float_source_rect == QRect(0, 0, 2, 2)  # ...still owing its hole
    assert _pixel(window, 0, 0) == src and _pixel(window, 6, 6) == dest

    # So dragging on blanks where the pixels came from, not where they hovered.
    _move_selection(window, (6, 6), (10, 4))
    window._pixel_key(Qt.Key.Key_Escape, False, False)
    assert _pixel(window, 0, 0) == 0
    assert _pixel(window, 6, 6) == dest
    assert _pixel(window, 10, 4) == src


def test_paste_floats_centred_on_the_view_until_it_is_dropped(qtbot, tmp_path) -> None:
    window = _window(qtbot, tmp_path)
    window._tool = Tool.SELECT
    window._marquee = QRect(0, 0, 3, 3)
    copied = [[_pixel(window, x, y) for x in range(3)] for y in range(3)]
    window._pixel_copy()
    at = window._centred_position(3, 3)
    under = _pixel(window, *at)
    window._on_tool_selected(Tool.PENCIL)  # a tool that couldn't drag the paste
    window._pixel_paste()
    assert window._tool is Tool.SELECT  # ...so the paste arms the one that can
    # It arrives in the air, centred on the viewport - and hides what is under it
    # without touching it.
    assert window._marquee == QRect(at[0], at[1], 3, 3)
    assert window._float_source_rect is None  # a paste lifted nothing
    assert _pixel(window, *at) == under

    window._pixel_key(Qt.Key.Key_Escape, False, False)  # drop it
    landed = [
        [_pixel(window, at[0] + x, at[1] + y) for x in range(3)] for y in range(3)
    ]
    assert landed == copied


def test_cutting_a_float_deletes_it_without_setting_it_down(qtbot, tmp_path) -> None:
    """Cut on pixels that are already off the page writes the hole they owed and
    throws them away - it must not land them on the way out."""
    window = _window(qtbot, tmp_path)
    window._tool = Tool.SELECT
    src, dest = _pixel(window, 0, 0), _pixel(window, 6, 6)
    window._marquee = QRect(0, 0, 2, 2)
    _move_selection(window, (0, 0), (6, 6))
    window._pixel_cut()
    assert window._float_grid is None and window._marquee is None
    assert _pixel(window, 0, 0) == 0  # the hole the move owed
    assert _pixel(window, 6, 6) == dest and dest != src  # nothing landed

    # Throwing a float away is one step with the hole it wrote, so undo lifts
    # those pixels back into the air rather than leaving them deleted.
    window._undo_stack.undo()
    assert window._float_grid is not None
    assert window._float_source_rect == QRect(0, 0, 2, 2)
    assert _pixel(window, 0, 0) == src


def test_moving_the_view_sets_a_floating_selection_down(qtbot, tmp_path) -> None:
    """A float is positioned against the visible window, so scrolling lands it
    rather than letting it drift onto other bytes."""
    window = _window(qtbot, tmp_path, tiles=64)
    window._tool = Tool.SELECT
    window._columns.setValue(4)
    window._rows.setValue(2)  # a page well short of the file, so a move is real
    src = _pixel(window, 0, 0)
    window._marquee = QRect(0, 0, 2, 2)
    _move_selection(window, (0, 0), (6, 6))
    assert window._float_grid is not None
    window._set_offset(window._offset + window._columns.value())  # scroll a row
    assert window._float_grid is None
    window._set_offset(0)
    assert _pixel(window, 0, 0) == 0 and _pixel(window, 6, 6) == src


def test_what_a_pixel_gesture_costs_on_the_undo_stack(qtbot, tmp_path) -> None:
    """Which gestures are a step and which are free.

    The rule differs by tool, which is the whole reason it is worth pinning: a
    *painting* interaction is always one step, whether or not it moved a pixel
    (the user drew, and Ctrl+Z should answer), while a Select gesture is a step
    only when the selection actually ends up somewhere else.

    Measured on the stack's **index**, not its count: this walks back through an
    undo partway, and the next push truncates the redo tail it left behind — so
    the count can stay put across a push that really did add a step.
    """
    window = _window(qtbot, tmp_path)
    stack = window._undo_stack
    window._tool = Tool.SELECT

    # Nothing selected, click empty space: no selection change, no step.
    before = stack.index()
    window._on_pixel_pressed(10, 10, Qt.MouseButton.LeftButton)
    window._on_pixel_released(10, 10)
    assert stack.index() == before

    # Dragging one out *is* a step, and undo restores the previous (empty) one.
    window._on_pixel_pressed(0, 0, Qt.MouseButton.LeftButton)
    window._on_pixel_moved(3, 3)
    window._on_pixel_released(3, 3)
    assert window._marquee == QRect(0, 0, 4, 4)
    assert stack.index() == before + 1
    stack.undo()
    assert window._marquee is None

    # Pressing inside a selection lifts a float; releasing without moving puts it
    # straight back, so the round trip is free.
    window._marquee = QRect(0, 0, 2, 2)
    window._canvas.set_marquee(window._marquee)
    before = stack.index()
    window._on_pixel_pressed(0, 0, Qt.MouseButton.LeftButton)
    window._on_pixel_released(0, 0)
    assert stack.index() == before
    assert window._marquee == QRect(0, 0, 2, 2)

    # Clicking away drops the selection — a change, so a step, and undoable.
    window._on_pixel_pressed(20, 20, Qt.MouseButton.LeftButton)
    window._on_pixel_released(20, 20)
    assert window._marquee is None
    assert stack.index() == before + 1
    stack.undo()
    assert window._marquee == QRect(0, 0, 2, 2)

    # A paint that changes no bytes still costs a step.
    window._tool = Tool.PENCIL
    window._on_pixel_pressed(2, 3, Qt.MouseButton.LeftButton)
    window._on_pixel_released(2, 3)  # paints the pen value
    before = stack.index()
    window._on_pixel_pressed(2, 3, Qt.MouseButton.LeftButton)
    window._on_pixel_released(2, 3)  # same pen, same pixel
    assert stack.index() == before + 1


def test_pen_preview_follows_the_drawing_tools_and_the_pen(qtbot, tmp_path) -> None:
    """The one-pixel preview arms for tools that paint, and tracks the pen colour."""
    from PySide6.QtWidgets import QApplication

    window = _window(qtbot, tmp_path)
    canvas = window._canvas

    def hover(x, y):
        QApplication.sendEvent(
            canvas,
            QMouseEvent(
                QEvent.Type.MouseMove,
                QPointF(x, y),
                Qt.MouseButton.NoButton,
                Qt.MouseButton.NoButton,
                Qt.KeyboardModifier.NoModifier,
            ),
        )

    for tool in (Tool.PENCIL, Tool.LINE, Tool.RECT, Tool.ELLIPSE_FILLED, Tool.FILL):
        window._on_tool_selected(tool)
        hover(9, 13)  # pixel (2,3) at zoom 4
        assert canvas._preview_color is not None, tool
        assert canvas._hover_pixel == (2, 3), tool
    # The samplers/selectors don't lay down paint, so they preview nothing.
    for tool in (Tool.EYEDROPPER, Tool.SELECT):
        window._on_tool_selected(tool)
        hover(9, 13)
        assert canvas._preview_color is None, tool

    # It shows the pen's own colour, and follows a new swatch.
    window._on_tool_selected(Tool.PENCIL)
    hover(9, 13)
    was = canvas._preview_color.name()
    window._palette_panel.select_index(3)
    assert canvas._preview_color.name() != was

    # Leaving the canvas untargets the pixel; tile mode disarms the preview.
    QApplication.sendEvent(canvas, QEvent(QEvent.Type.Leave))
    assert canvas._hover_pixel is None
    window._set_edit_mode(EditMode.TILE)
    assert canvas._preview_color is None


def test_canvas_drag_selects_through_real_event_delivery(qtbot, tmp_path) -> None:
    """Driven by real events, not the ``_on_pixel_*`` shortcut the other tests use.

    The canvas leaves its presses unaccepted so ClickFocus still works, and Qt then
    propagates them up to the scroll viewport — which must not read that as a click
    on the background and clear the marquee the press just anchored, killing the
    drag before it starts.
    """
    from PySide6.QtWidgets import QApplication

    window = _window(qtbot, tmp_path)
    window._tool = Tool.SELECT
    window.resize(900, 700)

    def _ev(kind, x, y, button, buttons):
        return QMouseEvent(
            kind, QPointF(x, y), button, buttons, Qt.KeyboardModifier.NoModifier
        )

    left, none = Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton
    canvas = window._canvas
    QApplication.sendEvent(canvas, _ev(QEvent.Type.MouseButtonPress, 5, 5, left, left))
    assert window._marquee is not None  # the press anchored it...
    QApplication.sendEvent(canvas, _ev(QEvent.Type.MouseMove, 40, 40, none, left))
    QApplication.sendEvent(
        canvas, _ev(QEvent.Type.MouseButtonRelease, 40, 40, left, none)
    )
    assert window._marquee is not None  # ...and the drag grew it, not dropped it
    assert window._marquee.width() > 1
    assert window._marquee.height() > 1


def test_clicking_the_canvas_background_clears_the_selection(qtbot, tmp_path) -> None:
    """A click on the surround around the art deselects, as its own step."""
    window = _window(qtbot, tmp_path)
    window._tool = Tool.SELECT
    window._marquee = QRect(0, 0, 4, 4)
    window._canvas.set_marquee(window._marquee)
    before = window._undo_stack.count()
    window._clear_selection_on_background()
    assert window._marquee is None
    assert window._undo_stack.count() == before + 1
    window._undo_stack.undo()
    assert window._marquee == QRect(0, 0, 4, 4)
    # With nothing selected it records nothing.
    window._apply_marquee(None)
    count = window._undo_stack.count()
    window._clear_selection_on_background()
    assert window._undo_stack.count() == count


def test_transform_toolbar_is_dead_until_a_file_is_open(qtbot, tmp_path) -> None:
    """The transform bar acts on a document, so it follows _doc — unlike the
    interpretation bars, which stay live to configure the next open."""
    window = MainWindow()
    qtbot.addWidget(window)
    assert window._doc is None
    assert not window._transform_toolbar.isEnabled()
    assert window._codecs_toolbar.isEnabled()  # still live with nothing open
    px = tmp_path / "s.4bpp.sfc"
    px.write_bytes(bytes(32 * 4))
    window._load_pixel(str(px))
    assert window._transform_toolbar.isEnabled()


def test_mode_toggles_swap_only_when_available(qtbot, tmp_path) -> None:
    window = _window(qtbot, tmp_path)  # pixel mode, document open
    # Pixel mode is rectangle-only, so the selection-mode swap is unavailable.
    assert not window._can_toggle_selection_mode()
    shape = window._selection_shape.currentData()
    window._toggle_selection_mode()
    assert window._selection_shape.currentData() is shape  # inert

    # E swaps the edit mode; back in tile mode the selection swap is available.
    window._toggle_edit_mode()
    assert window._edit_mode is EditMode.TILE
    assert window._can_toggle_selection_mode()
    first = window._selection_shape.currentData()
    window._toggle_selection_mode()
    swapped = window._selection_shape.currentData()
    assert swapped is not first
    # The toggle is the user picking a shape, so it must persist exactly as the
    # combo does - a round trip through a mode that forces Rectangle has to hand
    # *this* shape back, not the one that was stored before the toggle.
    window._toggle_edit_mode()  # pixel mode forces Rectangle
    window._toggle_edit_mode()
    assert window._selection_shape.currentData() is swapped
    window._toggle_selection_mode()
    assert window._selection_shape.currentData() is first  # swaps back
    window._toggle_edit_mode()
    assert window._edit_mode is EditMode.PIXEL


def test_float_survives_a_tool_switch(qtbot, tmp_path) -> None:
    """A tool switch is neither clearing nor a new selection, so the float stays."""
    window = _window(qtbot, tmp_path)
    window._tool = Tool.SELECT
    window._marquee = QRect(0, 0, 2, 2)
    window._pixel_copy()
    window._pixel_paste()
    assert window._float_grid is not None
    window._on_tool_selected(Tool.PENCIL)
    assert window._float_grid is not None


def test_escape_stamps_the_float(qtbot, tmp_path) -> None:
    window = _window(qtbot, tmp_path)
    window._tool = Tool.SELECT
    window._marquee = QRect(0, 0, 2, 2)
    window._pixel_copy()
    window._pixel_paste()
    assert window._float_grid is not None
    handled = window._pixel_key(Qt.Key.Key_Escape, False, False)
    assert handled and window._float_grid is None  # Esc dropped it


def test_number_key_selects_tool(qtbot, tmp_path) -> None:
    window = _window(qtbot, tmp_path)
    # The digits follow the tools rail's order, so "4" is the fourth button.
    assert window._pixel_key(Qt.Key.Key_4, False, False)  # "4" -> Fill
    assert window._tool is Tool.FILL
    assert window._pixel_key(Qt.Key.Key_1, False, False)  # "1" -> Select
    assert window._tool is Tool.SELECT


# -- pixel transforms ------------------------------------------------------
def test_flip_transforms_the_marquee_region(qtbot, tmp_path) -> None:
    window = _window(qtbot, tmp_path)
    window._tool = Tool.SELECT
    row = [_pixel(window, x, 0) for x in range(4)]
    window._marquee = QRect(0, 0, 4, 4)
    window._transform_pixel_region(OP_FLIP_H)
    # A horizontal flip reverses each row of the region.
    assert [_pixel(window, x, 0) for x in range(4)] == row[::-1]


def test_rotate_needs_a_square_region(qtbot, tmp_path) -> None:
    window = _window(qtbot, tmp_path)
    window._tool = Tool.SELECT
    window._marquee = QRect(0, 0, 4, 2)  # non-square
    window._sync_transform_actions()
    assert not window._pixel_group.rotate_cw.isEnabled()
    window._marquee = QRect(0, 0, 4, 4)  # square
    window._sync_transform_actions()
    assert window._pixel_group.rotate_cw.isEnabled()


def test_the_transform_keys_press_the_group_pixel_mode_is_showing(
    qtbot, tmp_path
) -> None:
    """H reaches the Pixel group, and Shift+H is passed on rather than reaching
    for a Block half pixel mode does not have."""
    window = _window(qtbot, tmp_path)  # starts in pixel mode
    window._tool = Tool.SELECT
    row = [_pixel(window, x, 0) for x in range(4)]
    window._marquee = QRect(0, 0, 4, 4)
    window._sync_transform_actions()
    assert window._transform_key(Qt.Key.Key_H, False, False)
    assert [_pixel(window, x, 0) for x in range(4)] == row[::-1]
    assert not window._transform_key(Qt.Key.Key_H, True, False)
    assert [_pixel(window, x, 0) for x in range(4)] == row[::-1]  # unchanged


def test_shift_square_marquee_corner_snaps_and_clamps(qtbot, tmp_path) -> None:
    # A 64x64 window so the clamp cases have room to be exceeded.
    window = _window(qtbot, tmp_path, tiles=64)
    window._columns.setValue(8)
    window._rows.setValue(8)  # 8 tiles * 8px = 64px each side, indices 0..63
    # Side takes the larger drag extent, in the drag's direction from the anchor.
    assert window._square_corner(2, 2, 6, 4) == (6, 6)  # dx=4 > dy=2 -> side 4
    assert window._square_corner(10, 10, 4, 2) == (2, 2)  # both negative -> side 8
    # Clamped so the square can't run off the window: near the right edge the
    # side shrinks to the room left on the tight axis.
    assert window._square_corner(60, 10, 63, 30) == (63, 13)  # room_x=3 caps side


def test_transform_bar_swaps_groups_with_edit_mode(qtbot, tmp_path) -> None:
    window = _window(qtbot, tmp_path)  # starts in pixel mode
    # Pixel mode shows only the Pixel group; the tile-mode groups are hidden.
    assert window._pixel_group.flip_h.isVisible()
    assert not window._tile_group.flip_h.isVisible()
    assert not window._block_group.flip_h.isVisible()
    window._set_edit_mode(EditMode.TILE)
    assert not window._pixel_group.flip_h.isVisible()
    assert window._tile_group.flip_h.isVisible()
    assert window._block_group.flip_h.isVisible()


# -- arrangement round-trip ------------------------------------------------
def test_pixel_edit_round_trips_under_block_layout(qtbot, tmp_path) -> None:
    window = _window(qtbot, tmp_path, tiles=16)
    # A 2×2 tile-block arrangement: compose and split must agree so a pixel drawn
    # on screen re-decodes to the same on-screen spot (through the byte edit).
    window._block_cols.setValue(2)
    window._block_rows.setValue(2)
    window._tool = Tool.PENCIL
    # Draw in the second block column (past the block boundary).
    window._on_pixel_pressed(10, 2, Qt.MouseButton.LeftButton)
    window._on_pixel_released(10, 2)
    # A fresh decode+compose (not the working grid) still shows the pixel there.
    assert _pixel(window, 10, 2) == 5


def test_double_click_selects_the_whole_tile(qtbot, tmp_path) -> None:
    """Double-clicking with Select takes the tile the pixel sits in, as one undo
    step — including when it lands inside a selection, where the opening press
    has already lifted a float to be moved."""
    window = _window(qtbot, tmp_path)
    window._tool = Tool.SELECT
    tile_w, tile_h = window._doc.tile_width, window._doc.tile_height

    # A press begins the gesture, exactly as the canvas drives it.
    window._on_pixel_pressed(tile_w + 3, tile_h + 2, Qt.MouseButton.LeftButton)
    before = window._undo_stack.count()
    window._on_pixel_double_clicked(tile_w + 3, tile_h + 2)
    assert window._marquee == QRect(tile_w, tile_h, tile_w, tile_h)
    assert window._undo_stack.count() == before + 1  # one interaction, one step

    # Double-clicking inside that selection: the press lifts a float, which the
    # double-click must discard rather than leave dangling — and re-select on
    # the tile without having written anything.
    window._on_pixel_pressed(tile_w + 1, tile_h + 1, Qt.MouseButton.LeftButton)
    assert window._float_grid is not None  # the press lifted it
    window._on_pixel_double_clicked(tile_w + 1, tile_h + 1)
    assert window._float_grid is None
    assert window._marquee == QRect(tile_w, tile_h, tile_w, tile_h)

    # Undo walks back to the selection as it stood before the first double-click.
    window._undo_stack.undo()
    assert window._marquee != QRect(tile_w, tile_h, tile_w, tile_h)


def test_double_click_is_inert_for_the_drawing_tools(qtbot, tmp_path) -> None:
    """Only Select claims the double-click; a pencil double-click must not
    conjure a selection out of a painting gesture."""
    window = _window(qtbot, tmp_path)
    window._tool = Tool.PENCIL
    window._on_pixel_pressed(2, 2, Qt.MouseButton.LeftButton)
    window._on_pixel_double_clicked(2, 2)
    assert window._marquee is None


def test_canvas_double_click_reports_a_pixel_and_ends_the_drag(qtbot) -> None:
    """The canvas reports a left double-click in pixel mode only, and closes the
    drag its opening press began — a stray move before the final release would
    otherwise resize whatever the double-click picked."""
    from celpix.core.index_grid import IndexGrid
    from celpix.core.palette import Palette
    from celpix.ui import render_bridge

    canvas = Canvas()
    qtbot.addWidget(canvas)
    canvas.set_tile_size(8, 8)
    canvas.set_zoom(4)
    canvas.set_image(render_bridge.render(IndexGrid(16, 16), Palette([0xFF000000])))
    canvas.set_edit_mode(EditMode.PIXEL)

    seen: list[tuple[int, int]] = []
    canvas.pixel_double_clicked.connect(lambda x, y: seen.append((x, y)))

    def event(kind):
        # Pixel (2,3) at zoom 4 sits at device (8..11, 12..15); pick the middle.
        return QMouseEvent(
            kind,
            QPointF(9, 13),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )

    canvas.mousePressEvent(event(QEvent.Type.MouseButtonPress))
    assert canvas._pixel_dragging
    canvas.mouseDoubleClickEvent(event(QEvent.Type.MouseButtonDblClick))
    assert seen == [(2, 3)]
    assert not canvas._pixel_dragging

    # Tile mode selects tiles by click; a double-click there reports nothing.
    canvas.set_edit_mode(EditMode.TILE)
    canvas.mouseDoubleClickEvent(event(QEvent.Type.MouseButtonDblClick))
    assert seen == [(2, 3)]


def test_history_steps_revert_in_the_mode_they_were_made_in(qtbot, tmp_path) -> None:
    """An editing step takes the window back to the mode that made it, both ways.

    The same bytes are edited from either mode, so a revert landing in the other
    one changes something the user can't place: a stroke undone under the tile
    grid, a pixel selection restored as an outline the tile view can't explain, or
    a tile flip reverting while they are painting pixels.
    """
    window = _window(qtbot, tmp_path)
    window._tool = Tool.SELECT
    window._on_pixel_pressed(1, 1, Qt.MouseButton.LeftButton)
    window._on_pixel_moved(5, 5)
    window._on_pixel_released(5, 5)  # a pixel-mode step: the selection

    # A tile-mode step on top of it, so the history spans both modes.
    window._set_edit_mode(EditMode.TILE)
    window._on_slots_selected(0, 0)
    flipped = _pixel(window, 0, 0)
    window._transform_tiles(OP_FLIP_H)
    assert _pixel(window, 0, 0) != flipped

    # Undoing the flip from pixel mode goes back to the tile view it happened in.
    window._set_edit_mode(EditMode.PIXEL)
    window._undo_stack.undo()
    assert window._edit_mode is EditMode.TILE
    assert _pixel(window, 0, 0) == flipped

    # ...and the step below it belongs to pixel mode, so undo swaps back again.
    window._undo_stack.undo()
    assert window._edit_mode is EditMode.PIXEL
    assert window._marquee is None  # the selection this step made, taken back

    # Redo returns to the mode each step was made in just the same.
    window._undo_stack.redo()
    assert window._edit_mode is EditMode.PIXEL
    assert window._marquee == QRect(1, 1, 5, 5)
    window._undo_stack.redo()
    assert window._edit_mode is EditMode.TILE
    assert _pixel(window, 0, 0) != flipped


def test_eyedropper_inside_a_pinned_region_picks_that_rows_color(qtbot, tmp_path):
    """Picking must name the colour under the cursor, not the one it would have
    had unpinned.

    The window grid holds the *stored* index — pinning deliberately never changes
    that — so resolving it against the view's own subpalette would hand back a
    colour that is nowhere on screen. Selecting through the pinned row also makes
    it the drawing row, which is what keeps the pen's index equal to what was
    picked.
    """
    window = _window(qtbot, tmp_path)
    window._columns.setValue(8)
    window._rows.setValue(1)

    # Pin the second half of the row to subpalette 2, then go back to row 0.
    window._set_linear_selection(4, 7)
    window._subpalette.setValue(2)
    window._pin_selection()
    window._subpalette.setValue(0)

    tile_w, _ = window._pixel_tile_size()
    x, y = tile_w * 4 + 1, 1  # inside the pinned half
    stored = window._window_grid().get(x, y)
    window._eyedrop_at(x, y)

    space = window._index_space()
    assert window._palette_panel.selected_index() == 2 * space + stored
    assert window._subpalette.value() == 2  # the picked row became the drawing row
    assert window._pen_value() == stored  # ...so the pen still writes what was picked

    # Outside the region the old behaviour is untouched.
    window._subpalette.setValue(0)
    stored = window._window_grid().get(1, 1)
    window._eyedrop_at(1, 1)
    assert window._palette_panel.selected_index() == stored
    assert window._pen_value() == stored
