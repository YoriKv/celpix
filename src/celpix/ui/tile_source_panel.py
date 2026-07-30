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
ones its Cell spin sets — start at ``$100``. Every signal and every readout here
speaks that number, so what the panel says carries to the binding bar, a hex
editor or a bank listing unchanged.

**Two rings, in the palette grid's own convention.** The outer one marks the
tile the *canvas* selection names, so picking a cell over there shows what it is
made of over here; the inner, softer one is this panel's own selection, the tile
a stamp would place. One outline language across both grids, so a ring means the
same thing wherever it is seen.
"""

from __future__ import annotations

from PySide6.QtCore import QRect, Qt, Signal
from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import QWidget

from celpix.core import ceil_div
from celpix.ui.canvas import CANVAS_BACKGROUND
from celpix.ui.widgets import ShortcutIsland, paint_selection_outline


class TileSourcePanel(ShortcutIsland, QWidget):
    tile_selected = Signal(int)  # the ID of the newly selected tile

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._sheet = QImage()
        self._ids = range(0)
        self._cell_px = (8, 8)  # one cell's size in image pixels
        self._columns = 16
        self._zoom = 2
        self._selected: int | None = None
        self._marked: int | None = None
        # ClickFocus, the canvas and palette grid's idiom: clicking a tile also
        # arms the arrow-key stepping below.
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self._update_size()

    # -- what to show --------------------------------------------------------
    def set_sheet(
        self, sheet: QImage, ids: range, cell_px: tuple[int, int], columns: int
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
            return self._ids.start + slot
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
        return self._ids.start + slot

    def _cell_rect(self, tile_id: int) -> QRect:
        """Where ``tile_id`` sits on the widget — the grid geometry, in one place."""
        slot = tile_id - self._ids.start
        cw, ch = self._cell_px[0] * self._zoom, self._cell_px[1] * self._zoom
        return QRect((slot % self._columns) * cw, (slot // self._columns) * ch, cw, ch)

    # -- interaction ---------------------------------------------------------
    def mousePressEvent(self, event) -> None:  # noqa: ANN001 — Qt override
        if event.button() == Qt.MouseButton.LeftButton:
            tile_id = self._id_at(event.position().x(), event.position().y())
            if tile_id is not None:
                self._select(tile_id)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: ANN001 — Qt override
        """Drag to scrub the pick across the sheet, edges included."""
        if not event.buttons() & Qt.MouseButton.LeftButton:
            super().mouseMoveEvent(event)
            return
        tile_id = self._id_near(event.position().x(), event.position().y())
        if tile_id is not None:
            self._select(tile_id)
        event.accept()

    def keyPressEvent(self, event) -> None:  # noqa: ANN001 — Qt override
        """Arrows step the pick — Left/Right by one ID (crossing display rows),
        Up/Down by one row. Movement clamps to the run on show; a step off the
        top or bottom stays put rather than being yanked to a corner, which would
        change the column under the user."""
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
        base = self._selected if self._selected is not None else self._ids.start
        target = base + delta
        if abs(delta) == self._columns and target not in self._ids:
            event.accept()
            return
        self._select(min(max(self._ids.start, target), self._ids[-1]))
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
        # The canvas's cell first, this panel's own pick inset one pixel inside
        # it and slightly soft — so a tile that is both still reads as two rings
        # rather than one thick one. The palette grid's convention exactly.
        if self._marked is not None:
            paint_selection_outline(painter, self._cell_rect(self._marked))
        if self._selected is not None:
            rect = self._cell_rect(self._selected).adjusted(1, 1, -1, -1)
            paint_selection_outline(painter, rect, alpha=230)
        painter.end()

    def sizeHint(self):  # noqa: ANN201 — Qt override
        return self.size()
