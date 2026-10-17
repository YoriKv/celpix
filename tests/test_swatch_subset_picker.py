"""The Edit Palette window's subset picker: the gestures that build the set."""

from __future__ import annotations

import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent, QPointingDevice

from celpix.ui.palette_panel import SWATCH_SIZE
from celpix.ui.swatch_subset_picker import SwatchSubsetPicker

CTRL = Qt.KeyboardModifier.ControlModifier
SHIFT = Qt.KeyboardModifier.ShiftModifier
NONE = Qt.KeyboardModifier.NoModifier


def _centre(index: int) -> QPoint:
    return QPoint(
        (index % 16) * SWATCH_SIZE + SWATCH_SIZE // 2,
        (index // 16) * SWATCH_SIZE + SWATCH_SIZE // 2,
    )


def _event(kind, index: int, mods, held: bool) -> QMouseEvent:  # noqa: ANN001
    point = QPointF(_centre(index))
    left = Qt.MouseButton.LeftButton
    return QMouseEvent(
        kind,
        point,
        point,  # global == local: the handlers only read position()
        Qt.MouseButton.NoButton if kind == QMouseEvent.Type.MouseMove else left,
        left if held else Qt.MouseButton.NoButton,
        mods,
        QPointingDevice.primaryPointingDevice(),
    )


def _drag(picker: SwatchSubsetPicker, path: list[int], mods=NONE) -> None:  # noqa: ANN001
    """Press on ``path[0]``, move through the rest with the button held, release."""
    picker.mousePressEvent(
        _event(QMouseEvent.Type.MouseButtonPress, path[0], mods, True)
    )
    for index in path[1:]:
        picker.mouseMoveEvent(_event(QMouseEvent.Type.MouseMove, index, mods, True))
    picker.mouseReleaseEvent(
        _event(QMouseEvent.Type.MouseButtonRelease, path[-1], mods, False)
    )


@pytest.fixture
def picker(qtbot) -> SwatchSubsetPicker:  # noqa: ANN001
    widget = SwatchSubsetPicker()
    qtbot.addWidget(widget)
    widget.set_colors([0xFF000000 | i for i in range(40)])  # 16 + 16 + a short 8
    return widget


def _click(qtbot, picker: SwatchSubsetPicker, index: int, mods=NONE) -> None:  # noqa: ANN001
    qtbot.mouseClick(picker, Qt.MouseButton.LeftButton, mods, _centre(index))


def test_plain_and_shift_click(qtbot, picker: SwatchSubsetPicker) -> None:
    changes: list[None] = []
    picker.selection_changed.connect(lambda: changes.append(None))
    _click(qtbot, picker, 5)
    assert picker.selection() == [5]
    # Shift runs from the anchor by index, wrapping into the next display row.
    _click(qtbot, picker, 18, SHIFT)
    assert picker.selection() == list(range(5, 19))
    _click(qtbot, picker, 3, SHIFT)  # anchor unchanged: the run flips sides
    assert picker.selection() == [3, 4, 5]
    assert len(changes) == 3


def test_ctrl_click_toggles(qtbot, picker: SwatchSubsetPicker) -> None:
    _click(qtbot, picker, 2)
    _click(qtbot, picker, 9, CTRL)
    assert picker.selection() == [2, 9]
    _click(qtbot, picker, 2, CTRL)
    assert picker.selection() == [9]
    # The swatch toggled last (off, here) is the anchor.
    _click(qtbot, picker, 11, SHIFT)
    assert picker.selection() == list(range(2, 12))


def test_rectangle_drag_replaces_with_band(picker: SwatchSubsetPicker) -> None:
    picker.set_selection([0, 30])
    # Columns 2..4 of display rows 0 and 1 — a box, not the run 2..20.
    _drag(picker, [2, 3, 20])
    assert picker.selection() == [2, 3, 4, 18, 19, 20]
    # Shrinking back undoes what the box had covered.
    _drag(picker, [2, 20, 3])
    assert picker.selection() == [2, 3]


def test_ctrl_drag_adds_band_and_tail_never_joins(picker: SwatchSubsetPicker) -> None:
    picker.set_selection([0])
    # Columns 6..10 of rows 1..2: row 2 holds only entries 32..39, so 42 is tail.
    _drag(picker, [22, 42], CTRL)
    assert picker.selection() == [0, 22, 23, 24, 25, 26, 38, 39]


def test_set_colors_shrink_drops_stranded_indices(picker: SwatchSubsetPicker) -> None:
    picker.set_selection([1, 20, 35])
    changes: list[None] = []
    picker.selection_changed.connect(lambda: changes.append(None))
    picker.set_colors([0xFF000000] * 16)
    assert picker.selection() == [1]
    assert changes == [None]
    assert picker.height() == SWATCH_SIZE
