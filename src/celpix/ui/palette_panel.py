"""The palette panel: a swatch grid of the decoded palette.

Lives in a dock under the Files list. It shows every color the palette pathway decoded
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

**Three marks, and they answer three questions.** The active range is the row the
view draws through, the swatch ring is the color being inspected, and the third —
:meth:`set_marked_row` — is the row the *canvas selection* draws through when it
has one of its own: a pinned region on a pixel view, a cell's or a subsprite's
stored palette row on a tilemap. The first two are the panel's own state and wear
the app's selection white; the marked row is somewhere else's, so it is drawn in
the grid's structural blue, as the tile source panel marks the tile the canvas is
pointing at.
"""

from __future__ import annotations

from PySide6.QtCore import QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QKeySequence, QPainter
from PySide6.QtWidgets import QWidget

from celpix.core import ceil_div
from celpix.core.palette import FULL_PALETTE_COUNT
from celpix.ui.canvas import GRID_STRUCTURE_COLOR
from celpix.ui.widgets import (
    ShortcutIsland,
    grid_slot_at,
    paint_selection_outline,
)

SWATCH_SIZE = 14  # logical px per swatch; Qt scales logical painting on HiDPI
SWATCH_COLUMNS = 16


class PalettePanel(ShortcutIsland, QWidget):
    subpalette_row_selected = Signal(int)  # clicked entry index // subpalette size
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
        self._marked_row: int | None = None
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

    def _grid_slot(
        self, x_px: float, y_px: float, *, clamp: bool = False
    ) -> int | None:
        """The entry index under (or, clamped, nearest) a widget position.

        A swatch's index *is* its slot in the grid, the palette being one
        unbroken run — unlike the tile source sheet, whose slots and IDs part
        company.
        """
        return grid_slot_at(
            x_px,
            y_px,
            (SWATCH_SIZE, SWATCH_SIZE),
            SWATCH_COLUMNS,
            len(self._colors),
            clamp=clamp,
        )

    def set_colors(self, colors: list[int]) -> None:
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

    def set_marked_row(self, row: int | None) -> None:
        """Mark subpalette ``row`` as the one the canvas selection draws through.

        ``None`` when nothing is selected, when nothing the selection covers
        names a row of its own, or when it spans rows that disagree — a mark that
        names one row cannot answer for several. Sized like the active range, in
        whole subpalettes of the pixel format's index space.
        """
        if row != self._marked_row:
            self._marked_row = row
            self.update()

    def selected_index(self) -> int | None:
        """The selected color's entry index, or ``None``."""
        return self._selected

    def select_index(self, index: int) -> None:
        """Select entry ``index`` and move the active subpalette to it.

        The programmatic equivalent of clicking a swatch — used by the pixel
        eyedropper to make the picked color the active drawing color. Emits the
        same ``color_selected`` / ``subpalette_row_selected`` signals a click does, so
        the readout and the view follow. Ignored for an out-of-range index.
        """
        if 0 <= index < len(self._colors):
            self._select(index)
            self.subpalette_row_selected.emit(index // self._count)

    def _select(self, index: int) -> None:
        if index != self._selected:
            self._selected = index
            self.update()
            self.color_selected.emit(index)

    def _update_size(self) -> None:
        rows = max(
            1, ceil_div(len(self._colors), SWATCH_COLUMNS)
        )  # ≥1 keeps it visible
        self.setFixedSize(SWATCH_COLUMNS * SWATCH_SIZE, rows * SWATCH_SIZE)
        self.update()

    @staticmethod
    def full_grid_size() -> QSize:
        """How much room the grid takes at a **full** palette's worth of rows.

        The dock reserves that much whatever is loaded, so a full-length palette
        — the common case, Default and Custom both being one — is always on
        screen entire and never scrolls. It can't be read off the live grid,
        which is sized to the palette actually loaded — nothing at all until
        one is.
        """
        rows = ceil_div(FULL_PALETTE_COUNT, SWATCH_COLUMNS)
        return QSize(SWATCH_COLUMNS * SWATCH_SIZE, rows * SWATCH_SIZE)

    def mousePressEvent(self, event) -> None:  # noqa: ANN001 — Qt override
        if event.button() == Qt.MouseButton.LeftButton:
            index = self._grid_slot(event.position().x(), event.position().y())
            if index is not None:
                if self._eyedropper:
                    self.color_picked.emit(self._colors[index])
                    event.accept()
                    return
                self._select(index)
                self.subpalette_row_selected.emit(index // self._count)
        elif event.button() == Qt.MouseButton.RightButton and not self._eyedropper:
            # Move the selection (and the active subpalette with it) onto the
            # right-clicked swatch, so the menu that follows acts on it — the
            # file-manager rule the canvas uses. An already-selected swatch stays.
            index = self._grid_slot(event.position().x(), event.position().y())
            if index is not None and index != self._selected:
                self._select(index)
                self.subpalette_row_selected.emit(index // self._count)
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
        index = self._grid_slot(event.position().x(), event.position().y(), clamp=True)
        if index is not None and index != self._selected:
            self._select(index)
            self.subpalette_row_selected.emit(index // self._count)
        event.accept()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: ANN001 — Qt override
        """Double-click opens the color editor on that entry — the established
        idiom for a swatch grid (``docs/design-reference/palette-workflow.md``)."""
        if event.button() == Qt.MouseButton.LeftButton and not self._eyedropper:
            index = self._grid_slot(event.position().x(), event.position().y())
            if index is not None:
                # The press already selected it; the editor reads the selection.
                self.edit_requested.emit(index)
                event.accept()
                return
        super().mouseDoubleClickEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: ANN001 — Qt override
        """Copy/paste the selected color, and arrows move the color selection
        through the grid — Left/Right by one entry (crossing display rows),
        Up/Down by one display row — with the active subpalette *following the
        selection* (the same signal a swatch click emits), rather than the
        selection riding a subpalette step. All movement clamps to the loaded
        colors."""
        # Copy/Paste reach here as key presses because the island claimed their
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
            Qt.Key.Key_Up: -SWATCH_COLUMNS,
            Qt.Key.Key_Down: SWATCH_COLUMNS,
        }
        delta = deltas.get(event.key())
        if delta is None:
            super().keyPressEvent(event)
            return
        # No selection yet: start from the active subpalette's first entry.
        base = self._selected if self._selected is not None else self._start
        target = base + delta
        if abs(delta) == SWATCH_COLUMNS and not 0 <= target < len(self._colors):
            # No display row above/below — stay put. (A min/max clamp would
            # yank the selection to the palette's corner, changing its column.)
            event.accept()
            return
        target = min(max(0, target), len(self._colors) - 1)
        self._select(target)
        self.subpalette_row_selected.emit(target // self._count)
        event.accept()

    def paintEvent(self, event) -> None:  # noqa: ARG002 — Qt supplies the event
        painter = QPainter(self)
        # No grid lines: the swatches are pure squares of color, contiguously
        # connected, with only the active-range and selection outlines drawn over
        # them. Aliased fillRect keeps every edge hard at any display scale (it
        # rounds to whole device pixels), and adjacent cells share a logical
        # boundary so they tile with no gap or overlap.
        for i, color in enumerate(self._colors):
            painter.fillRect(self._swatch_rect(i), QColor.fromRgba(color & 0xFFFFFFFF))
        self._paint_active_range(painter)
        self._paint_marked_row(painter)
        self._paint_selection(painter)
        painter.end()

    @staticmethod
    def _swatch_rect(index: int) -> QRect:
        """Where swatch ``index`` sits — the grid geometry, in one place."""
        return QRect(
            (index % SWATCH_COLUMNS) * SWATCH_SIZE,
            (index // SWATCH_COLUMNS) * SWATCH_SIZE,
            SWATCH_SIZE,
            SWATCH_SIZE,
        )

    def _range_rect(self, start: int) -> QRect:
        """The swatches of one subpalette, from entry ``start``.

        ``count`` is a power of two, so a subpalette is either a segment within
        one display row (count <= 16, e.g. a 2bpp quarter row) or a whole block
        of rows (count > 16, e.g. 8bpp = 16 rows) — never a ragged wrap.
        """
        if self._count <= SWATCH_COLUMNS:
            rect = self._swatch_rect(start)
            rect.setWidth(self._count * SWATCH_SIZE)
            return rect
        return QRect(
            0,
            (start // SWATCH_COLUMNS) * SWATCH_SIZE,
            SWATCH_COLUMNS * SWATCH_SIZE,
            (self._count // SWATCH_COLUMNS) * SWATCH_SIZE,
        )

    def _paint_active_range(self, painter: QPainter) -> None:
        # Drawn even when the range lies past the loaded colors: a short palette
        # still shows where the active window sits.
        paint_selection_outline(painter, self._range_rect(self._start))

    def _paint_marked_row(self, painter: QPainter) -> None:
        """The row the canvas selection draws through, in the grid's structural
        blue — the tile source panel's language for "what the canvas is pointing
        at", against the white the panel wears for its own choices.

        Inset a pixel so a row that is *both* marked and active reads as two
        rings rather than one thick one, which is not a rare coincidence: pinning
        a selection takes the row the view is already on, so the two land
        together the moment after the gesture.
        """
        if self._marked_row is None:
            return
        rect = self._range_rect(self._marked_row * self._count).adjusted(1, 1, -1, -1)
        paint_selection_outline(painter, rect, color=GRID_STRUCTURE_COLOR)

    def _paint_selection(self, painter: QPainter) -> None:
        if self._selected is None:
            return
        # The same outline as the active range, one pixel further in so a
        # one-swatch selection inside that range still reads as its own ring,
        # and slightly soft so it doesn't overpower a single swatch.
        rect = self._swatch_rect(self._selected).adjusted(1, 1, -1, -1)
        paint_selection_outline(painter, rect, alpha=230)

    def sizeHint(self):  # noqa: ANN201 — Qt override
        return self.size()
