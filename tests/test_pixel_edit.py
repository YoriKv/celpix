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
from celpix.ui.main_window.transform import _FLIP_H
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
def test_toggle_enters_pixel_mode(qtbot, tmp_path) -> None:
    window = _window(qtbot, tmp_path)
    assert window._edit_mode is EditMode.PIXEL
    assert window._canvas._edit_mode is EditMode.PIXEL
    # Pixel mode is rectangle-only: the shape picker is forced and locked.
    assert window._selection_shape.currentData().name == "RECT"
    assert not window._selection_shape.isEnabled()
    assert window._tools_dock.isEnabled()
    assert window._edit_mode_action.isChecked()


def test_toggle_back_restores_tile_mode(qtbot, tmp_path) -> None:
    window = _window(qtbot, tmp_path)
    window._set_edit_mode(EditMode.TILE)
    assert window._canvas._edit_mode is EditMode.TILE
    assert window._selection_shape.isEnabled()
    assert not window._tools_dock.isEnabled()


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
    with qtbot.waitSignal(canvas.tiles_selected, timeout=500):
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


# -- floating selection & pixel clipboard ----------------------------------
def test_copy_clear_paste_round_trips(qtbot, tmp_path) -> None:
    window = _window(qtbot, tmp_path)
    window._tool = Tool.SELECT
    original = [[_pixel(window, x, y) for x in range(3)] for y in range(3)]
    window._marquee = QRect(0, 0, 3, 3)
    window._pixel_copy()
    window._pixel_clear()
    assert _pixel(window, 1, 1) == 0
    window._marquee = QRect(0, 0, 1, 1)
    window._pixel_paste()  # lands a float at the marquee origin
    assert window._float_grid is not None
    window._commit_float()  # stamp it back at (0,0)
    assert [[_pixel(window, x, y) for x in range(3)] for y in range(3)] == original


def test_move_lifts_and_stamps_as_single_undo(qtbot, tmp_path) -> None:
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
    assert window._pixel_key(Qt.Key.Key_7, False, False)  # "7" -> Fill
    assert window._tool is Tool.FILL


# -- pixel transforms ------------------------------------------------------
def test_flip_transforms_the_marquee_region(qtbot, tmp_path) -> None:
    window = _window(qtbot, tmp_path)
    window._tool = Tool.SELECT
    row = [_pixel(window, x, 0) for x in range(4)]
    window._marquee = QRect(0, 0, 4, 4)
    window._transform_pixel_region(_FLIP_H)
    # A horizontal flip reverses each row of the region.
    assert [_pixel(window, x, 0) for x in range(4)] == row[::-1]


def test_rotate_needs_a_square_region(qtbot, tmp_path) -> None:
    window = _window(qtbot, tmp_path)
    window._tool = Tool.SELECT
    window._marquee = QRect(0, 0, 4, 2)  # non-square
    window._sync_transform_actions()
    assert not window._tile_group.rotate_cw.isEnabled()
    window._marquee = QRect(0, 0, 4, 4)  # square
    window._sync_transform_actions()
    assert window._tile_group.rotate_cw.isEnabled()


# -- arrangement round-trip ------------------------------------------------
def test_pixel_edit_round_trips_under_block_layout(qtbot, tmp_path) -> None:
    window = _window(qtbot, tmp_path, tiles=16)
    # A 2×2 metatile arrangement: compose and split must agree so a pixel drawn
    # on screen re-decodes to the same on-screen spot (through the byte edit).
    window._block_cols.setValue(2)
    window._block_rows.setValue(2)
    window._tool = Tool.PENCIL
    # Draw in the second metatile column (past the block boundary).
    window._on_pixel_pressed(10, 2, Qt.MouseButton.LeftButton)
    window._on_pixel_released(10, 2)
    # A fresh decode+compose (not the working grid) still shows the pixel there.
    assert _pixel(window, 10, 2) == 5
