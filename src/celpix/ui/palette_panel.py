"""The palette panel: a swatch grid of the decoded palette.

Lives in a right-side dock. It shows every colour the palette pathway decoded
(not just the slice the current bit depth can index) so embedded palettes can be
inspected at a glance, and outlines the active subpalette range. Selecting a
swatch — by click or arrow keys — selects that colour *and* the subpalette row
containing it — the panel
emits the *row* and the main window feeds it to the existing subpalette spin, so
the panel never owns view state. The colour selection itself (which swatch is
being inspected) is the panel's own, announced via :attr:`color_selected` for
the details readout below the grid.

The display is always 16 swatches wide, purely a wrap — the *subpalette row* is
the active range (:meth:`set_active_range`), sized by the pixel format's index
space (``2^bpp``): stepping, click mapping and the outline all use it, so a
2bpp view works in 4-entry subpalettes (four per display row) and an 8bpp view
in one 256-entry block.
"""

from __future__ import annotations

from PySide6.QtCore import QRect, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QWidget

from celpix.ui.widgets import paint_selection_outline

SWATCH = 14  # logical px per swatch; Qt scales logical painting on HiDPI
COLUMNS = 16


class PalettePanel(QWidget):
    subpalette_clicked = Signal(int)  # clicked entry index // subpalette size
    color_selected = Signal(int)  # entry index of the newly selected colour

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._colors: list[int] = []
        self._start = 0
        self._count = 16
        self._selected: int | None = None
        # ClickFocus (the canvas's idiom): clicking a swatch also arms the
        # arrow-key stepping below.
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self._update_size()

    def set_palette(self, colors: list[int]) -> None:
        # Called on every view refresh, including pure navigation where the
        # palette hasn't changed — skip the copy and repaint then.
        if colors == self._colors:
            return
        self._colors = list(colors)
        # A shrunken palette can strand the selection; clamp it back inside so
        # a selection survives a mode/format switch as *some* valid colour.
        # Adjusted silently (no re-emit) — the window re-reads the readout
        # right after.
        if self._selected is not None and self._selected >= len(self._colors):
            self._selected = len(self._colors) - 1 if self._colors else None
        self._update_size()

    def set_active_range(self, start: int, count: int) -> None:
        """Outline entries [start, start+count) — the applied subpalette."""
        start, count = max(0, start), max(1, count)
        if (start, count) != (self._start, self._count):  # skip repaint otherwise
            self._start, self._count = start, count
            self.update()

    def selected_index(self) -> int | None:
        """The selected colour's entry index, or ``None``."""
        return self._selected

    def _select(self, index: int) -> None:
        if index != self._selected:
            self._selected = index
            self.update()
            self.color_selected.emit(index)

    def _update_size(self) -> None:
        rows = max(1, -(-len(self._colors) // COLUMNS))  # ceil; ≥1 keeps it visible
        self.setFixedSize(COLUMNS * SWATCH, rows * SWATCH)
        self.update()

    def mousePressEvent(self, event) -> None:  # noqa: ANN001 — Qt override
        if event.button() == Qt.MouseButton.LeftButton:
            x = int(event.position().x()) // SWATCH
            y = int(event.position().y()) // SWATCH
            index = y * COLUMNS + x
            if 0 <= x < COLUMNS and index < len(self._colors):
                self._select(index)
                self.subpalette_clicked.emit(index // self._count)
        super().mousePressEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: ANN001 — Qt override
        """Arrows move the colour selection through the grid — Left/Right by
        one entry (crossing display rows), Up/Down by one display row — and the
        active subpalette *follows the selection* (the same signal a swatch
        click emits), rather than the selection riding a subpalette step. All
        movement clamps to the loaded colours."""
        if not self._colors:
            super().keyPressEvent(event)
            return
        deltas = {
            Qt.Key.Key_Left: -1,
            Qt.Key.Key_Right: 1,
            Qt.Key.Key_Up: -COLUMNS,
            Qt.Key.Key_Down: COLUMNS,
        }
        delta = deltas.get(event.key())
        if delta is None:
            super().keyPressEvent(event)
            return
        # No selection yet: start from the active subpalette's first entry.
        base = self._selected if self._selected is not None else self._start
        target = base + delta
        if abs(delta) == COLUMNS and not 0 <= target < len(self._colors):
            # No display row above/below — stay put. (A min/max clamp would
            # yank the selection to the palette's corner, changing its column.)
            event.accept()
            return
        target = min(max(0, target), len(self._colors) - 1)
        self._select(target)
        self.subpalette_clicked.emit(target // self._count)
        event.accept()

    def paintEvent(self, event) -> None:  # noqa: ARG002 — Qt supplies the event
        painter = QPainter(self)
        # Hairline cell edge so equal neighbours read as cells; one pen for all.
        painter.setPen(QColor(0, 0, 0, 60))
        for i, color in enumerate(self._colors):
            rect = QRect(
                (i % COLUMNS) * SWATCH, (i // COLUMNS) * SWATCH, SWATCH, SWATCH
            )
            painter.fillRect(rect, QColor.fromRgba(color & 0xFFFFFFFF))
            painter.drawRect(rect.adjusted(0, 0, -1, -1))
        self._paint_active_range(painter)
        self._paint_selection(painter)
        painter.end()

    def _paint_active_range(self, painter: QPainter) -> None:
        # start = subpalette_row * count with count a power of two, so the range
        # is either a segment within one display row (count <= 16, e.g. a 2bpp
        # quarter row) or a whole block of rows (count > 16, e.g. 8bpp = 16
        # rows). Drawn even when the range lies past the loaded colours: a short
        # palette still shows where the active window sits.
        if self._count <= COLUMNS:
            rect = QRect(
                (self._start % COLUMNS) * SWATCH,
                (self._start // COLUMNS) * SWATCH,
                self._count * SWATCH,
                SWATCH,
            )
        else:
            rect = QRect(
                0,
                (self._start // COLUMNS) * SWATCH,
                COLUMNS * SWATCH,
                (self._count // COLUMNS) * SWATCH,
            )
        paint_selection_outline(painter, self.palette(), rect)

    def _paint_selection(self, painter: QPainter) -> None:
        if self._selected is None:
            return
        rect = QRect(
            (self._selected % COLUMNS) * SWATCH,
            (self._selected // COLUMNS) * SWATCH,
            SWATCH,
            SWATCH,
        )
        # White-over-dark nested outline: readable on any swatch colour and
        # distinct from the accent-coloured active-range outline.
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(255, 255, 255, 230), 1))
        painter.drawRect(rect.adjusted(1, 1, -2, -2))
        painter.setPen(QPen(QColor(0, 0, 0, 230), 1))
        painter.drawRect(rect.adjusted(2, 2, -3, -3))

    def sizeHint(self):  # noqa: ANN201 — Qt override
        return self.size()
