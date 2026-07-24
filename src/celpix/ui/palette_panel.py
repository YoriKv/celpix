"""The palette panel: a swatch grid of the decoded palette.

Lives in a right-side dock. It shows every color the palette pathway decoded
(not just the slice the current bit depth can index) so embedded palettes can be
inspected at a glance, and outlines the active subpalette range. Selecting a
swatch — by click or arrow keys — selects that color *and* the subpalette row
containing it — the panel
emits the *row* and the main window feeds it to the existing subpalette spin, so
the panel never owns view state. The color selection itself (which swatch is
being inspected) is the panel's own, announced via :attr:`color_selected` for
the details readout below the grid.

**Editing.** Double-clicking a swatch opens the shared color editor on it
(:mod:`celpix.ui.color_editor`); the grid is also one of the eyedropper's
sampling surfaces, and while armed a click reports the swatch's color instead
of selecting it — the selected swatch is the one being *edited*, so moving it
would retarget the editor mid-pick (``docs/design/palette-editing.md``).
Copy/Paste — from the keyboard (Ctrl+C/V for the selected color, Ctrl+Shift+C/V
for the whole active subpalette) or a right-click menu — move colors through the
system clipboard as hex text. The panel only reports the intent (the
``*_requested`` signals and ``customContextMenuRequested``); the window owns the
clipboard, the menu, and the undoable write-back.

The display is always 16 swatches wide, purely a wrap — the *subpalette row* is
the active range (:meth:`set_active_range`), sized by the pixel format's index
space (``2^bpp``): stepping, click mapping and the outline all use it, so a
2bpp view works in 4-entry subpalettes (four per display row) and an 8bpp view
in one 256-entry block.
"""

from __future__ import annotations

from PySide6.QtCore import QRect, Qt, Signal
from PySide6.QtGui import QColor, QKeySequence, QPainter, QPen
from PySide6.QtWidgets import QWidget

from celpix.core import ceil_div
from celpix.core.palette import FULL_PALETTE_COUNT
from celpix.ui.widgets import paint_selection_outline, take_editing_shortcut

SWATCH = 14  # logical px per swatch; Qt scales logical painting on HiDPI
COLUMNS = 16


class PalettePanel(QWidget):
    subpalette_clicked = Signal(int)  # clicked entry index // subpalette size
    color_selected = Signal(int)  # entry index of the newly selected color
    edit_requested = Signal(int)  # double-clicked entry index — open the editor
    # ARGB sampled while the eyedropper is armed. ``object``, not ``int``: Qt's
    # int is 32-bit *signed*, and any ARGB with alpha >= 0x80 overflows it.
    color_picked = Signal(object)
    # Copy/paste the selected color (Ctrl+C/V) or the whole active subpalette
    # (Ctrl+Shift+C/V), when the grid holds focus. The panel just reports intent;
    # the window owns the clipboard and the undoable write-back.
    copy_requested = Signal()
    paste_requested = Signal()
    copy_subpalette_requested = Signal()
    paste_subpalette_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._colors: list[int] = []
        self._start = 0
        self._count = 16
        self._selected: int | None = None
        # Eyedropper: while armed, a click samples a swatch's color instead of
        # selecting it (see :meth:`set_eyedropper`).
        self._eyedropper = False
        # ClickFocus (the canvas's idiom): clicking a swatch also arms the
        # arrow-key stepping below.
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        # Right-click opens the copy/paste menu (built by the window, which knows
        # the clipboard state); the press below first moves the selection there.
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._update_size()

    def set_eyedropper(self, on: bool) -> None:
        """Arm/disarm color sampling from the grid.

        While armed a click emits :attr:`color_picked` and leaves the selection
        alone — the selected swatch is the one being *edited*, so moving it
        would retarget the editor mid-pick instead of filling it.
        """
        if self._eyedropper == on:
            return
        self._eyedropper = on
        if on:
            self.setCursor(Qt.CursorShape.CrossCursor)
        else:
            self.unsetCursor()

    def _index_at(self, x_px: float, y_px: float) -> int | None:
        """The entry index under a widget position, or None past the colors."""
        x = int(x_px) // SWATCH
        y = int(y_px) // SWATCH
        index = y * COLUMNS + x
        if 0 <= x < COLUMNS and 0 <= index < len(self._colors):
            return index
        return None

    def _index_near(self, x_px: float, y_px: float) -> int | None:
        """The entry index nearest a widget position, clamped into the grid.

        Unlike :meth:`_index_at` a miss never reads as None: a drag that runs off
        an edge — or past the last, partly filled row — snaps to the closest
        swatch so the selection keeps following the pointer. ``None`` only when
        there are no colors at all.
        """
        if not self._colors:
            return None
        col = min(max(int(x_px) // SWATCH, 0), COLUMNS - 1)
        rows = ceil_div(len(self._colors), COLUMNS)
        row = min(max(int(y_px) // SWATCH, 0), rows - 1)
        # Past the last color (the empty tail of a short final row) lands on the
        # last color — dragging off the end selects the end.
        return min(row * COLUMNS + col, len(self._colors) - 1)

    def set_palette(self, colors: list[int]) -> None:
        # Called on every view refresh, including pure navigation where the
        # palette hasn't changed — skip the copy and repaint then.
        if colors == self._colors:
            return
        self._colors = list(colors)
        # A shrunken palette can strand the selection; clamp it back inside so
        # a selection survives a mode/format switch as *some* valid color.
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
        """The selected color's entry index, or ``None``."""
        return self._selected

    def select_index(self, index: int) -> None:
        """Select entry ``index`` and move the active subpalette to it.

        The programmatic equivalent of clicking a swatch — used by the pixel
        eyedropper to make the picked color the active drawing color. Emits the
        same ``color_selected`` / ``subpalette_clicked`` signals a click does, so
        the readout and the view follow. Ignored for an out-of-range index.
        """
        if 0 <= index < len(self._colors):
            self._select(index)
            self.subpalette_clicked.emit(index // self._count)

    def _select(self, index: int) -> None:
        if index != self._selected:
            self._selected = index
            self.update()
            self.color_selected.emit(index)

    def _update_size(self) -> None:
        rows = max(1, ceil_div(len(self._colors), COLUMNS))  # ≥1 keeps it visible
        self.setFixedSize(COLUMNS * SWATCH, rows * SWATCH)
        self.update()

    @staticmethod
    def full_grid_height() -> int:
        """How tall the grid stands at a **full** palette's worth of rows.

        The dock opens tall enough to show that much without scrolling, since a
        full-length palette is the common case (Default and Custom both are). It
        can't be read off the live grid, which is sized to the palette actually
        loaded — nothing at all until one is.
        """
        return ceil_div(FULL_PALETTE_COUNT, COLUMNS) * SWATCH

    def mousePressEvent(self, event) -> None:  # noqa: ANN001 — Qt override
        if event.button() == Qt.MouseButton.LeftButton:
            index = self._index_at(event.position().x(), event.position().y())
            if index is not None:
                if self._eyedropper:
                    self.color_picked.emit(self._colors[index])
                    event.accept()
                    return
                self._select(index)
                self.subpalette_clicked.emit(index // self._count)
        elif event.button() == Qt.MouseButton.RightButton and not self._eyedropper:
            # Move the selection (and the active subpalette with it) onto the
            # right-clicked swatch, so the menu that follows acts on it — the
            # file-manager rule the canvas uses. An already-selected swatch stays.
            index = self._index_at(event.position().x(), event.position().y())
            if index is not None and index != self._selected:
                self._select(index)
                self.subpalette_clicked.emit(index // self._count)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: ANN001 — Qt override
        """Drag to scrub the selection: while the left button is held the color
        under (or nearest) the pointer becomes selected, edges included — the
        same move a press or an arrow key makes.

        The eyedropper is left to discrete clicks: a drag over the grid must not
        spray the editor with every color it crosses.
        """
        held = bool(event.buttons() & Qt.MouseButton.LeftButton)
        if self._eyedropper or not held:
            super().mouseMoveEvent(event)
            return
        index = self._index_near(event.position().x(), event.position().y())
        if index is not None and index != self._selected:
            self._select(index)
            self.subpalette_clicked.emit(index // self._count)
        event.accept()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: ANN001 — Qt override
        """Double-click opens the color editor on that entry (the Tile
        Molester idiom — see ``docs/design-reference/palette-workflow.md``)."""
        if event.button() == Qt.MouseButton.LeftButton and not self._eyedropper:
            index = self._index_at(event.position().x(), event.position().y())
            if index is not None:
                # The press already selected it; the editor reads the selection.
                self.edit_requested.emit(index)
                event.accept()
                return
        super().mouseDoubleClickEvent(event)

    def event(self, event) -> bool:  # noqa: ANN001 — Qt override
        # A shortcut island while focused: the canvas editing shortcuts
        # (Cut/Copy/Paste/Select All/Delete) yield here rather than acting on the
        # canvas selection behind the dock. Copy/Paste act on the selected color
        # (see :meth:`keyPressEvent`); the rest have no meaning here and simply do
        # nothing. Its other keys are the arrow-step selection below.
        if take_editing_shortcut(event):
            return True
        return super().event(event)

    def keyPressEvent(self, event) -> None:  # noqa: ANN001 — Qt override
        """Copy/paste the selected color, and arrows move the color selection
        through the grid — Left/Right by one entry (crossing display rows),
        Up/Down by one display row — with the active subpalette *following the
        selection* (the same signal a swatch click emits), rather than the
        selection riding a subpalette step. All movement clamps to the loaded
        colors."""
        # Copy/Paste reach here as key presses because ``event()`` claimed their
        # shortcut override; the window does the actual clipboard + write-back.
        # Ctrl+Shift+C/V (whole subpalette) aren't standard sequences, so they're
        # matched by hand; check them first, as they subsume the plain ones.
        ctrl_shift = (
            Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier
        )
        if event.modifiers() == ctrl_shift and event.key() == Qt.Key.Key_C:
            self.copy_subpalette_requested.emit()
            event.accept()
            return
        if event.modifiers() == ctrl_shift and event.key() == Qt.Key.Key_V:
            self.paste_subpalette_requested.emit()
            event.accept()
            return
        if event.matches(QKeySequence.StandardKey.Copy):
            self.copy_requested.emit()
            event.accept()
            return
        if event.matches(QKeySequence.StandardKey.Paste):
            self.paste_requested.emit()
            event.accept()
            return
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
        # No grid lines: the swatches are pure squares of color, contiguously
        # connected, with only the active-range and selection outlines drawn over
        # them. Aliased fillRect keeps every edge hard at any display scale (it
        # rounds to whole device pixels), and adjacent cells share a logical
        # boundary so they tile with no gap or overlap.
        for i, color in enumerate(self._colors):
            rect = QRect(
                (i % COLUMNS) * SWATCH, (i // COLUMNS) * SWATCH, SWATCH, SWATCH
            )
            painter.fillRect(rect, QColor.fromRgba(color & 0xFFFFFFFF))
        self._paint_active_range(painter)
        self._paint_selection(painter)
        painter.end()

    def _paint_active_range(self, painter: QPainter) -> None:
        # start = subpalette_row * count with count a power of two, so the range
        # is either a segment within one display row (count <= 16, e.g. a 2bpp
        # quarter row) or a whole block of rows (count > 16, e.g. 8bpp = 16
        # rows). Drawn even when the range lies past the loaded colors: a short
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
        paint_selection_outline(painter, rect)

    def _paint_selection(self, painter: QPainter) -> None:
        if self._selected is None:
            return
        rect = QRect(
            (self._selected % COLUMNS) * SWATCH,
            (self._selected // COLUMNS) * SWATCH,
            SWATCH,
            SWATCH,
        )
        # The same nested white/black language as the active-range outline
        # (paint_selection_outline), one pixel further in so a one-swatch
        # selection inside the active range still reads as its own ring.
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(255, 255, 255, 230), 1))
        painter.drawRect(rect.adjusted(1, 1, -2, -2))
        painter.setPen(QPen(QColor(0, 0, 0, 230), 1))
        painter.drawRect(rect.adjusted(2, 2, -3, -3))

    def sizeHint(self):  # noqa: ANN201 — Qt override
        return self.size()
