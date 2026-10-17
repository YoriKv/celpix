"""The Edit Palette window's Subset scope: a swatch grid with a multi-selection.

The palette panel picks *one* colour and announces palette rows; a subset is
any set of entries, scattered or not, so this is a separate small widget rather
than a mode of that panel. It shares the panel's geometry (16 swatches wide,
:data:`~celpix.ui.palette_panel.SWATCH_SIZE` each) so the palette reads the same
in both places, and the gestures are the ones a file list or a text field
already taught: click, Shift+click for a run, Ctrl+click to toggle, drag a
rectangle, Ctrl+A.

**The band is index-space.** A rectangle drag spans display *columns and rows*,
not a run of indices: the grid is the only picture of the palette the user has,
so the swatches inside the drawn box are the ones that join — a column of four
SNES sub-palettes' first entries is one drag. Shift+click stays linear, wrapping
display rows like a text selection, because a run of entries is the other shape
palettes are organised in. Cells are clamped into the grid, so dragging off an
edge keeps the band following the pointer; the empty tail of a short last row
can be part of the box but never of the set.

**The set updates live during a drag**, one :attr:`SwatchSubsetPicker.selection_changed`
per change, because the host previews the operation on whatever is selected:
the user watches the edit land on the swatches as the box grows, and lets go
when it covers what they meant.

**Escape is not swallowed.** The grid lives in a modal dialog that cancels on
Escape; a widget that ate the key to clear its selection would leave the user
no keyboard way out while the grid held focus.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from PySide6.QtCore import QEvent, QRect, Qt, Signal
from PySide6.QtGui import QColor, QKeySequence, QPainter
from PySide6.QtWidgets import QWidget

from celpix.ui.palette_panel import (
    SWATCH_COLUMNS,
    SWATCH_SIZE,
    swatch_grid_size,
    swatch_rect,
)
from celpix.ui.widgets import (
    grid_slot_at,
    paint_pick_ring,
    paint_selection_outline,
    take_shortcuts,
)

_CTRL = Qt.KeyboardModifier.ControlModifier
_SHIFT = Qt.KeyboardModifier.ShiftModifier


class SwatchSubsetPicker(QWidget):
    """A palette's swatches, any set of which the user can select.

    A press applies the click rule at once; a drag only begins once the pointer
    reaches a *different* cell, and from then on every move recomputes the set
    from what was captured at the press — so a press that wobbles inside its
    swatch stays a click, and moving back over ground already covered undoes
    what it added instead of accumulating it.
    """

    selection_changed = Signal()  # once per change of the selected set

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._colors: list[int] = []
        self._selected: set[int] = set()
        # Where Shift+click runs from: the last plainly- or Ctrl-clicked swatch.
        self._anchor: int | None = None
        # The press a drag grows from: its cell, the modifiers held then, and
        # the set as it stood *before* the press applied its click rule.
        self._press_cell: tuple[int, int] | None = None
        self._press_mods = Qt.KeyboardModifier.NoModifier
        self._press_base: frozenset[int] = frozenset()
        self._dragging = False
        self._band: QRect | None = None  # painted while a rectangle drag runs
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.setToolTip(
            "Click a swatch to select it alone.\n"
            "Shift+click selects the run from the last-clicked swatch.\n"
            "Ctrl+click adds or removes one swatch.\n"
            "Drag a rectangle to select the swatches inside it;\n"
            "hold Ctrl to add them to the selection.\n"
            "Ctrl+A selects every entry."
        )
        self._update_size()

    # -- model -------------------------------------------------------------

    def set_colors(self, colors: Sequence[int]) -> None:
        """Show these ARGB colours; drop selected indices past the new end
        (emitting :attr:`selection_changed` if that changed the set); resize;
        repaint."""
        colors = list(colors)
        if colors == self._colors:
            return
        self._colors = colors
        if self._anchor is not None and self._anchor >= len(colors):
            self._anchor = None
        self._apply({i for i in self._selected if i < len(colors)})
        self._update_size()

    def colors(self) -> list[int]:
        return list(self._colors)

    def selection(self) -> list[int]:
        """Sorted indices of the selected swatches."""
        return sorted(self._selected)

    def set_selection(self, indices: Iterable[int]) -> None:
        """Replace the set (indices out of range ignored); emits
        :attr:`selection_changed` only if the set actually changed. The anchor
        moves to the smallest selected index (``None`` when empty)."""
        chosen = {i for i in indices if 0 <= i < len(self._colors)}
        self._anchor = min(chosen) if chosen else None
        self._apply(chosen)

    def select_all(self) -> None:
        self._apply(set(range(len(self._colors))))

    def _apply(self, chosen: set[int]) -> None:
        if chosen != self._selected:
            self._selected = chosen
            self.update()
            self.selection_changed.emit()

    def _update_size(self) -> None:
        self.setFixedSize(swatch_grid_size(len(self._colors)))
        self.update()

    # -- geometry ----------------------------------------------------------

    def _cell_at(self, x_px: float, y_px: float) -> tuple[int, int]:
        """The (column, row) under a point, clamped into the grid.

        Not :func:`grid_slot_at`'s clamp, which folds the empty tail onto the
        last entry: the band needs the tail's own column, or a box dragged
        there would jump sideways to wherever the palette happens to end.
        """
        col = min(max(int(x_px) // SWATCH_SIZE, 0), SWATCH_COLUMNS - 1)
        rows = swatch_grid_size(len(self._colors)).height() // SWATCH_SIZE
        row = min(max(int(y_px) // SWATCH_SIZE, 0), rows - 1)
        return col, row

    def _slot_at(self, x_px: float, y_px: float, *, clamp: bool = False) -> int | None:
        return grid_slot_at(
            x_px,
            y_px,
            (SWATCH_SIZE, SWATCH_SIZE),
            SWATCH_COLUMNS,
            len(self._colors),
            clamp=clamp,
        )

    def _band_between(self, a: tuple[int, int], b: tuple[int, int]) -> QRect:
        left, right = min(a[0], b[0]), max(a[0], b[0])
        top, bottom = min(a[1], b[1]), max(a[1], b[1])
        return QRect(
            left * SWATCH_SIZE,
            top * SWATCH_SIZE,
            (right - left + 1) * SWATCH_SIZE,
            (bottom - top + 1) * SWATCH_SIZE,
        )

    def _band_slots(self, a: tuple[int, int], b: tuple[int, int]) -> set[int]:
        count = len(self._colors)
        return {
            row * SWATCH_COLUMNS + col
            for row in range(min(a[1], b[1]), max(a[1], b[1]) + 1)
            for col in range(min(a[0], b[0]), max(a[0], b[0]) + 1)
            if row * SWATCH_COLUMNS + col < count  # the tail never joins
        }

    def _run_from_anchor(self, index: int) -> set[int]:
        assert self._anchor is not None
        low, high = sorted((self._anchor, index))
        return set(range(low, high + 1))

    # -- mouse -------------------------------------------------------------

    def mousePressEvent(self, event) -> None:  # noqa: ANN001 — Qt override
        if event.button() != Qt.MouseButton.LeftButton or not self._colors:
            super().mousePressEvent(event)
            return
        pos = event.position()
        mods = event.modifiers()
        self._press_cell = self._cell_at(pos.x(), pos.y())
        self._press_mods = mods
        self._press_base = frozenset(self._selected)
        self._dragging = False
        index = self._slot_at(pos.x(), pos.y())
        if index is not None:  # the empty tail only ever starts a band
            if mods & _CTRL:
                self._anchor = index
                self._apply(self._selected ^ {index})
            elif mods & _SHIFT and self._anchor is not None:
                self._apply(self._run_from_anchor(index))
            else:
                self._anchor = index
                self._apply({index})
        event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: ANN001 — Qt override
        held = bool(event.buttons() & Qt.MouseButton.LeftButton)
        if not held or self._press_cell is None:
            super().mouseMoveEvent(event)
            return
        pos = event.position()
        cell = self._cell_at(pos.x(), pos.y())
        if not self._dragging and cell == self._press_cell:
            event.accept()  # still a click until the pointer leaves its swatch
            return
        self._dragging = True
        if self._press_mods & _SHIFT and self._anchor is not None:
            # Shift scrubs the linear run, so no box is drawn: it would claim
            # a shape the selection does not have.
            self._band = None
            index = self._slot_at(pos.x(), pos.y(), clamp=True)
            run = self._run_from_anchor(index) if index is not None else set()
            self._apply(set(self._press_base) | run)
        else:
            self._band = self._band_between(self._press_cell, cell)
            band = self._band_slots(self._press_cell, cell)
            if self._press_mods & _CTRL:
                band |= self._press_base
            self._apply(band)
        self.update()
        event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: ANN001 — Qt override
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_cell = None
            self._dragging = False
            if self._band is not None:
                self._band = None
                self.update()
        super().mouseReleaseEvent(event)

    # -- keys --------------------------------------------------------------

    def event(self, event: QEvent) -> bool:  # noqa: D102 — Qt override
        # Claim Select All off any window-wide binding so Ctrl+A lands here.
        if take_shortcuts(event, (QKeySequence.StandardKey.SelectAll,)):
            return True
        return super().event(event)

    def keyPressEvent(self, event) -> None:  # noqa: ANN001 — Qt override
        if event.matches(QKeySequence.StandardKey.SelectAll):
            self.select_all()
            event.accept()
            return
        # Everything else — Escape above all — goes on to the dialog.
        super().keyPressEvent(event)

    # -- paint -------------------------------------------------------------

    def paintEvent(self, event) -> None:  # noqa: ARG002 — Qt supplies the event
        painter = QPainter(self)
        # Aliased fillRect, as the palette panel paints: hard edges at any
        # display scale, and adjacent swatches tile with no gap.
        for i, color in enumerate(self._colors):
            painter.fillRect(swatch_rect(i), QColor.fromRgba(color & 0xFFFFFFFF))
        for i in self._selected:
            paint_pick_ring(painter, swatch_rect(i))
        if self._band is not None:
            paint_selection_outline(painter, self._band)
        painter.end()

    def sizeHint(self):  # noqa: ANN201 — Qt override
        return self.size()
