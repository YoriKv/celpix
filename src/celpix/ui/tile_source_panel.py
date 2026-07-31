"""The tile source panel: the tiles a tilemap can draw from, as a sheet.

Lives in a dock tabbed with the Palette. A tilemap cell *names* a tile that
lives in another entry, and the picture on the canvas is that tile's — so
nothing on screen says which tile it is, or what else the cell could have named.
View ▸ Show Tile IDs answers the first by writing a number over every cell; this
answers both by showing the tiles themselves, addressed by ID
(``docs/design/tilemap-entry.md`` §8).

A dumb view, like :class:`~celpix.ui.palette_panel.PalettePanel`: it is handed a
composed sheet and the ID run it covers, and reports which ID was picked. It
resolves nothing — the sheet comes from
:func:`~celpix.pipeline.pipeline.tile_source_image`, which composes it through
the same path that composes the map, so a tile here and the same tile there are
the same pixels.

**The grid is addressed in IDs, not slots.** The run does not always start at 0:
a map numbering its tiles from ``$100`` against a slice of exactly those has a
negative base tile, and the IDs it holds — the numbers in its bytes, and the
ones its Cell spin sets — start at ``$100``. Nor is it always contiguous: where a
cell draws a whole 16x16 unit the IDs between two units name overlapping windows
rather than pictures of their own, so the sheet steps over them
(:func:`~celpix.pipeline.pipeline.tile_source_ids`). A slot is therefore a
*position in that list*, and the ID is what the list holds there — the one place
the two may not be confused. Every signal and every readout here speaks the ID,
so what the panel says carries to the binding bar, a hex editor or a bank
listing unchanged.

**Two rings, and they are two different questions.** The outer one marks the
tile the *canvas* selection names, so picking a cell over there shows what it is
made of over here; the inner, softer one is this panel's own selection, the tile
a stamp would place. Only the inner is a selection, so only the inner wears the
app's selection white (the palette grid's convention, which this otherwise
follows) — the outer is drawn in the grid's structural blue, the colour that
marks structure rather than choice everywhere else. Two rings in one white on
one small square read as one ring drawn twice.

**A lattice every 16 tiles** marks where the numbering rolls over — the page a
bank is addressed in, not the tile boundaries, which are already visible here
(:data:`GRID_STEP_TILES`). Fixed rather than following View ▸ Grid: that setting
governs a *configurable* lattice over the art, and what this rules off is the ID
space.

**Ctrl+wheel zooms and space-drag pans**, which are the canvas's gestures with
the canvas's meanings (:mod:`celpix.ui.canvas`). A bank read at 8x does not fit
its dock, so this is a surface a user navigates rather than reads at a glance —
and reaching for a different gesture on the second such surface is how one
editor grows two navigation idioms. Both are reported rather than acted on: the
zoom level lives in the dock's spin and the scrolling in its scroll area, and
neither is this widget's.
"""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import QPointF, QRect, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QWidget

from celpix.core import ceil_div
from celpix.ui.canvas import (
    CANVAS_BACKGROUND,
    GRID_COARSE_ALPHA,
    GRID_STRUCTURE_COLOR,
)
from celpix.ui.widgets import ShortcutIsland, paint_selection_outline

# How many tiles apart the lattice sits, both ways. A **fixed** step, unlike the
# canvas's configurable grid: this is not marking tile boundaries — the tiles are
# already visibly separate here — it is marking where the *numbering* rolls over,
# and 16 across by 16 down is the 0x100-tile page a bank is addressed in. At the
# sheet's default 16 columns that lands a line under every 256 IDs, which is the
# unit a base tile is usually a multiple of.
GRID_STEP_TILES = 16


class TileSourcePanel(ShortcutIsland, QWidget):
    tile_selected = Signal(int)  # the ID of the newly selected tile
    zoom_requested = Signal(int, object)  # steps, QPointF cursor pos (widget)
    pan_requested = Signal(int, int)  # dx, dy

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._sheet = QImage()
        self._ids: Sequence[int] = range(0)
        self._cell_px = (8, 8)  # one cell's size in image pixels
        self._columns = 16
        self._zoom = 2
        self._selected: int | None = None
        self._marked: int | None = None
        # Space-drag panning, the canvas's split exactly: ``_pan_active`` is space
        # held (armed, hand shown), ``_panning`` a drag in progress measured from
        # ``_pan_last``. Modal over the mouse — while armed a press pans instead
        # of picking, or a scrub across the sheet would fight the drag.
        self._pan_active = False
        self._panning = False
        self._pan_last = QPointF()
        # ClickFocus, the canvas and palette grid's idiom: clicking a tile also
        # arms the arrow-key stepping below.
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self._update_size()

    # -- what to show --------------------------------------------------------
    def set_sheet(
        self,
        sheet: QImage,
        ids: Sequence[int],
        cell_px: tuple[int, int],
        columns: int,
    ) -> None:
        """Show ``sheet``, whose slots hold ``ids`` laid out ``columns`` across.

        ``cell_px`` is one cell's size in the sheet's own pixels — a 2x2 metatile
        of 8x8 tiles is 16x16 — which is what turns a click into a slot and a
        slot back into a rectangle to outline.

        A selection outside the new run is dropped rather than clamped: unlike a
        palette swatch, an ID is a *name*, and the nearest surviving one names a
        different tile. Dropped silently — the dock re-reads the readout right
        after, and re-emitting here would announce a pick the user did not make.
        """
        self._sheet = sheet
        self._ids = ids
        self._cell_px = (max(1, cell_px[0]), max(1, cell_px[1]))
        self._columns = max(1, columns)
        if self._selected is not None and self._selected not in ids:
            self._selected = None
        if self._marked is not None and self._marked not in ids:
            self._marked = None
        self._update_size()

    def set_zoom(self, zoom: int) -> None:
        if zoom != self._zoom:
            self._zoom = max(1, zoom)
            self._update_size()

    def set_pan_mode(self, on: bool) -> None:
        """Arm/disarm space-drag panning (the window drives this off the space key).

        Disarming ends a drag in progress, since the key can come up mid-drag —
        the canvas's rule, and the reason both keep the armed flag and the
        dragging flag apart.
        """
        if self._pan_active == on:
            return
        self._pan_active = on
        if not on:
            self._panning = False
        self._apply_cursor()

    def _apply_cursor(self) -> None:
        """Hand while panning, the widget's own otherwise."""
        if self._panning:
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
        elif self._pan_active:
            self.setCursor(Qt.CursorShape.OpenHandCursor)
        else:
            self.unsetCursor()

    def set_marked_id(self, tile_id: int | None) -> None:
        """Ring the tile the canvas's selected cell names, or clear the ring."""
        if tile_id is not None and tile_id not in self._ids:
            tile_id = None
        if tile_id != self._marked:
            self._marked = tile_id
            self.update()

    def clear(self) -> None:
        """Show nothing — no document, no binding, or a source with no tiles."""
        self.set_sheet(QImage(), range(0), self._cell_px, self._columns)

    # -- the selection -------------------------------------------------------
    def selected_id(self) -> int | None:
        """The picked tile's ID, or ``None``."""
        return self._selected

    def select_id(self, tile_id: int) -> None:
        """Pick ``tile_id``, as a click would. Ignored for an ID not on show."""
        if tile_id in self._ids:
            self._select(tile_id)

    def _select(self, tile_id: int) -> None:
        if tile_id != self._selected:
            self._selected = tile_id
            self.update()
            self.tile_selected.emit(tile_id)

    # -- geometry ------------------------------------------------------------
    def _rows(self) -> int:
        return max(1, ceil_div(len(self._ids), self._columns))

    def _update_size(self) -> None:
        cw, ch = self._cell_px
        self.setFixedSize(
            self._columns * cw * self._zoom, self._rows() * ch * self._zoom
        )
        self.update()

    def _id_at(self, x_px: float, y_px: float) -> int | None:
        """The ID under a widget position, or None past the last tile."""
        cw, ch = self._cell_px
        col = int(x_px) // (cw * self._zoom)
        row = int(y_px) // (ch * self._zoom)
        slot = row * self._columns + col
        if 0 <= col < self._columns and 0 <= slot < len(self._ids):
            return self._ids[slot]
        return None

    def _id_near(self, x_px: float, y_px: float) -> int | None:
        """The ID nearest a widget position, clamped into the sheet.

        A drag that runs off an edge — or past the last, partly filled row —
        snaps to the closest tile so the selection keeps following the pointer,
        the same scrub the palette grid does. ``None`` only when nothing is on
        show.
        """
        if not self._ids:
            return None
        cw, ch = self._cell_px
        col = min(max(int(x_px) // (cw * self._zoom), 0), self._columns - 1)
        row = min(max(int(y_px) // (ch * self._zoom), 0), self._rows() - 1)
        slot = min(row * self._columns + col, len(self._ids) - 1)
        return self._ids[slot]

    def _cell_rect(self, slot: int) -> QRect:
        """Where the ``slot``-th entry sits — the grid geometry, in one place.

        In slots rather than IDs because the run may step over the IDs between
        two units, so only the list can say which square a number is in.
        """
        cw, ch = self._cell_px[0] * self._zoom, self._cell_px[1] * self._zoom
        return QRect((slot % self._columns) * cw, (slot // self._columns) * ch, cw, ch)

    def _slot_of(self, tile_id: int) -> int:
        """Which square ``tile_id`` sits in. Only asked of an ID on the sheet —
        both rings drop an ID the run does not hold before they get here."""
        return self._ids.index(tile_id)

    # -- interaction ---------------------------------------------------------
    def mousePressEvent(self, event) -> None:  # noqa: ANN001 — Qt override
        # Checked first, so an armed pan wins over the pick the same way it wins
        # over selecting and painting on the canvas.
        if self._pan_active and event.button() == Qt.MouseButton.LeftButton:
            self._panning = True
            self._pan_last = event.globalPosition()
            self._apply_cursor()
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton:
            tile_id = self._id_at(event.position().x(), event.position().y())
            if tile_id is not None:
                self._select(tile_id)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: ANN001 — Qt override
        """Drag to scrub the pick across the sheet, edges included."""
        if self._panning:
            # Global position, not widget-local: the widget shifts under the
            # cursor as the view scrolls, which would feed back into the delta.
            pos = event.globalPosition()
            delta = pos - self._pan_last
            self._pan_last = pos
            self.pan_requested.emit(round(delta.x()), round(delta.y()))
            event.accept()
            return
        if not event.buttons() & Qt.MouseButton.LeftButton:
            super().mouseMoveEvent(event)
            return
        tile_id = self._id_near(event.position().x(), event.position().y())
        if tile_id is not None:
            self._select(tile_id)
        event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: ANN001 — Qt override
        if self._panning and event.button() == Qt.MouseButton.LeftButton:
            self._panning = False
            self._apply_cursor()  # back to the open hand (space may still be held)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event) -> None:  # noqa: ANN001 — Qt override
        """**Ctrl**+wheel zooms; a plain wheel falls through to the scroll area.

        Reports a signed step per notch and the cursor position, leaving the
        range and the cursor-anchoring to the dock — the canvas's division, and
        for its reason: the level is a control's value, not this widget's.
        """
        if not (event.modifiers() & Qt.KeyboardModifier.ControlModifier):
            event.ignore()  # let the scroll area scroll as usual
            return
        if self._sheet.isNull():
            return
        dy = event.angleDelta().y()
        if dy == 0:
            return
        # One step per 120-unit notch, but at least one so a high-resolution
        # wheel sending small deltas still zooms.
        steps = int(dy / 120) or (1 if dy > 0 else -1)
        self.zoom_requested.emit(steps, event.position())
        event.accept()

    def keyPressEvent(self, event) -> None:  # noqa: ANN001 — Qt override
        """Arrows step the pick — Left/Right by one square (crossing display
        rows), Up/Down by one row. Stepped in **slots**, not IDs: the two agree
        on an unbroken run and the run is not always unbroken, and what a user
        means by Right is the next square either way. Movement clamps to the
        sheet; a step off the top or bottom stays put rather than being yanked to
        a corner, which would change the column under the user."""
        if not self._ids:
            super().keyPressEvent(event)
            return
        deltas = {
            Qt.Key.Key_Left: -1,
            Qt.Key.Key_Right: 1,
            Qt.Key.Key_Up: -self._columns,
            Qt.Key.Key_Down: self._columns,
        }
        delta = deltas.get(event.key())
        if delta is None:
            super().keyPressEvent(event)
            return
        base = self._slot_of(self._selected) if self._selected is not None else 0
        target = base + delta
        if abs(delta) == self._columns and not 0 <= target < len(self._ids):
            event.accept()
            return
        self._select(self._ids[min(max(0, target), len(self._ids) - 1)])
        event.accept()

    # -- painting ------------------------------------------------------------
    def paintEvent(self, event) -> None:  # noqa: ANN001 — Qt override
        painter = QPainter(self)
        # The trailing slots of a partial last row are backing, not tiles: the
        # neutral canvas colour says so, the same answer the canvas gives past
        # the end of a file. Painted under the sheet rather than over it, so a
        # full grid costs one fill and no clipping.
        painter.fillRect(event.rect(), CANVAS_BACKGROUND)
        if not self._sheet.isNull():
            # Nearest-neighbour: tiles must stay crisp when magnified.
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
            painter.scale(self._zoom, self._zoom)
            painter.drawImage(0, 0, self._sheet)
            painter.resetTransform()
            self._paint_grid(painter, event.rect())
        # The canvas's cell first, in the structural blue; this panel's own pick
        # inset one pixel inside it and slightly soft — so a tile that is both
        # still reads as two rings rather than one thick one, and the two answer
        # visibly different questions rather than sitting one alpha step apart.
        if self._marked is not None:
            paint_selection_outline(
                painter,
                self._cell_rect(self._slot_of(self._marked)),
                color=GRID_STRUCTURE_COLOR,
            )
        if self._selected is not None:
            rect = self._cell_rect(self._slot_of(self._selected)).adjusted(1, 1, -1, -1)
            paint_selection_outline(painter, rect, alpha=230)
        painter.end()

    def _paint_grid(self, painter: QPainter, exposed: QRect) -> None:
        """Rule the sheet every :data:`GRID_STEP_TILES` cells, both ways.

        The canvas's *structural* colour rather than its fine one, because that
        is what this is: the same blue marks a block boundary over there, and one
        grid language across the two is worth more than a second palette of
        lines. Drawn over the tiles and under both rings, so a marked tile on a
        boundary still reads as marked.

        **Interior lines only** — the step counts from the sheet's own top-left,
        so a line at 0 would be a border around the widget rather than a division
        of it, which is the rule the canvas's lattice follows too. Lines outside
        the exposed band are skipped: a bank read at 8x is mostly off screen.
        """
        cw, ch = self._cell_px[0] * self._zoom, self._cell_px[1] * self._zoom
        if cw <= 0 or ch <= 0:
            return
        width, height = self.width(), self.height()
        color = QColor(GRID_STRUCTURE_COLOR)
        color.setAlpha(GRID_COARSE_ALPHA)
        painter.setPen(color)
        for x in range(cw * GRID_STEP_TILES, width, cw * GRID_STEP_TILES):
            if exposed.left() <= x <= exposed.right():
                painter.drawLine(x, exposed.top(), x, exposed.bottom())
        for y in range(ch * GRID_STEP_TILES, height, ch * GRID_STEP_TILES):
            if exposed.top() <= y <= exposed.bottom():
                painter.drawLine(exposed.left(), y, exposed.right(), y)

    def sizeHint(self):  # noqa: ANN201 — Qt override
        return self.size()
