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

**The selection is a set of tiles with one of them current.** Clicking or
dragging here picks one, which is all a stamp can place; a caller that already
holds a wider pick — the font alphabet window's table, where a stretch of rows is
a stretch of tiles (:mod:`celpix.ui.font_alphabet_window`) — states the whole set
with :meth:`~TileSourcePanel.select_ids`. It is drawn as the canvas draws a
multi-tile selection: one outline per contiguous run of a display row, so a
picked block reads as a block and a scattered pick does not claim to be one.

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

from PySide6.QtCore import QRect, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QWidget

from celpix.core import ceil_div
from celpix.ui.canvas import (
    CANVAS_BACKGROUND,
    GRID_COARSE_ALPHA,
    GRID_STRUCTURE_COLOR,
)
from celpix.ui.widgets import (
    PanZoomSurface,
    ShortcutIsland,
    grid_slot_at,
    paint_selection_outline,
)

# How many tiles apart the lattice sits, both ways. A **fixed** step, unlike the
# canvas's configurable grid: this is not marking tile boundaries — the tiles are
# already visibly separate here — it is marking where the *numbering* rolls over,
# and 16 across by 16 down is the 0x100-tile page a bank is addressed in. At the
# sheet's default 16 columns that lands a line under every 256 IDs, which is the
# unit a base tile is usually a multiple of.
GRID_STEP_TILES = 16

# Below this many screen pixels a square, a caption is dropped rather than drawn
# (:meth:`TileSourcePanel._paint_labels`). 24 is where a 7-pixel font stops
# fitting inside one cell with the tile still visible above it.
LABEL_MIN_PX = 24

# The plate a caption sits on, and the ink on it. Near-opaque black under white
# because a font sheet is as often light-on-dark as dark-on-light, and a single
# ink colour vanishes into half of them.
LABEL_PLATE = QColor(0, 0, 0, 190)
LABEL_COLOR = QColor(255, 255, 255)


class TileSourcePanel(ShortcutIsland, PanZoomSurface, QWidget):
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
        # Every picked tile, the current one included. Held beside `_selected`
        # rather than instead of it because the two answer different questions:
        # what is outlined, and which tile a stamp places and the arrows step from.
        self._picked: frozenset[int] = frozenset()
        self._marked: int | None = None
        self._labels: dict[int, str] = {}
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
        on_sheet = set(ids)
        if self._selected is not None and self._selected not in on_sheet:
            self._selected = None
        self._picked &= on_sheet
        if self._marked is not None and self._marked not in ids:
            self._marked = None
        self._update_size()

    def set_zoom(self, zoom: int) -> None:
        if zoom != self._zoom:
            self._zoom = max(1, zoom)
            self._update_size()

    def _has_content(self) -> bool:
        return not self._sheet.isNull()

    def set_labels(self, labels: dict[int, str]) -> None:
        """Write a short caption into the corner of each tile in ``labels``.

        Keyed by ID, so a caller states what it knows about a tile rather than
        about a square, and a re-laid sheet carries the captions with it.

        For the font alphabet editor, where the caption is what the tile *says*
        (:mod:`celpix.ui.font_alphabet_window`) — a reading the picture cannot
        give, since a glyph tile is a letter shape and the question is which
        letter the game thinks it is. Empty by default and empty in the tile
        source dock, which has nothing of that kind to add.

        A character the UI font has no glyph for draws as tofu. That is the
        honest outcome and not worth defending against: the tile beside it is the
        truth, and substituting a placeholder would hide which of the two the
        user is looking at.
        """
        if labels != self._labels:
            self._labels = dict(labels)
            self.update()

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

    def selected_ids(self) -> frozenset[int]:
        """Every picked tile's ID — one of them, unless a caller stated more."""
        return self._picked

    def select_id(self, tile_id: int) -> None:
        """Pick ``tile_id`` alone, as a click would. Ignored for an ID not on show."""
        if tile_id in self._ids:
            self._select(tile_id)

    def select_ids(self, ids: Sequence[int]) -> None:
        """Pick every ID in ``ids``, the first of them current.

        For a caller whose own reading of the sheet has a selection wider than
        one tile. IDs the run does not hold are dropped rather than refused: a
        table that lists codes past the last tile is the ordinary case
        (:mod:`celpix.ui.font_alphabet_window`), and the tiles among them are
        still the answer.
        """
        on_sheet = set(self._ids)
        picked = [tile_id for tile_id in ids if tile_id in on_sheet]
        if picked:
            self._pick(frozenset(picked), picked[0])

    def _select(self, tile_id: int) -> None:
        self._pick(frozenset({tile_id}), tile_id)

    def _pick(self, picked: frozenset[int], current: int) -> None:
        """Land a selection, reporting only a change of the *current* tile.

        Widening a pick that still has the same tile current is not a new tile
        being picked — everything downstream of the signal (the canvas, the Cell
        spin, the ring in the dock) speaks of one tile, and would be told the
        same one again.
        """
        if picked == self._picked and current == self._selected:
            return
        moved = current != self._selected
        self._picked, self._selected = picked, current
        self.update()
        if moved:
            self.tile_selected.emit(current)

    # -- geometry ------------------------------------------------------------
    def _rows(self) -> int:
        return max(1, ceil_div(len(self._ids), self._columns))

    def _update_size(self) -> None:
        cw, ch = self._cell_px
        self.setFixedSize(
            self._columns * cw * self._zoom, self._rows() * ch * self._zoom
        )
        self.update()

    def _id_at(self, x_px: float, y_px: float, *, clamp: bool = False) -> int | None:
        """The ID under a widget position — or, ``clamp``ed, the nearest one.

        The clamped reading is what a drag wants, so scrubbing off an edge (or
        past the last, partly filled row) keeps the pick following the pointer.
        ``None`` for a click on nothing, or with the sheet empty.
        """
        cell_w, cell_h = self._cell_px
        slot = grid_slot_at(
            x_px,
            y_px,
            (cell_w * self._zoom, cell_h * self._zoom),
            self._columns,
            len(self._ids),
            clamp=clamp,
        )
        return None if slot is None else self._ids[slot]

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
        if self._pan_press(event):
            return
        if event.button() == Qt.MouseButton.LeftButton:
            tile_id = self._id_at(event.position().x(), event.position().y())
            if tile_id is not None:
                self._select(tile_id)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: ANN001 — Qt override
        """Drag to scrub the pick across the sheet, edges included."""
        if self._pan_move(event):
            return
        if not event.buttons() & Qt.MouseButton.LeftButton:
            super().mouseMoveEvent(event)
            return
        tile_id = self._id_at(event.position().x(), event.position().y(), clamp=True)
        if tile_id is not None:
            self._select(tile_id)
        event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: ANN001 — Qt override
        if self._pan_release(event):
            return
        super().mouseReleaseEvent(event)

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
            self._paint_labels(painter, event.rect())
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
        self._paint_selection(painter)
        painter.end()

    def _paint_selection(self, painter: QPainter) -> None:
        """Outline the picked tiles, one ring per contiguous run of a row.

        The canvas's rule for a multi-tile selection (:meth:`~celpix.ui.canvas.
        Canvas._paint_selection`): a run drawn as one box reads as the stretch it
        is, and a gap in it is a gap the user can see. Run in **slots**, since
        the sheet steps over the IDs between two units and only the list can say
        which squares sit next to each other.

        Inset a pixel and slightly soft, so a tile that is also *marked* still
        reads as two rings rather than one thick one.
        """
        if not self._picked:
            return
        slots = [
            slot for slot, tile_id in enumerate(self._ids) if tile_id in self._picked
        ]
        runs: list[tuple[int, int]] = []
        for slot in slots:  # ascending, since they come out of `enumerate`
            # A slot in column 0 starts a row and so starts a run, whatever sat
            # before it: the square before it on the sheet is the far end of the
            # line above, and one ring around both would enclose the whole width.
            if runs and slot == runs[-1][1] + 1 and slot % self._columns:
                runs[-1] = (runs[-1][0], slot)
            else:
                runs.append((slot, slot))
        for first, last in runs:
            rect = self._cell_rect(first).united(self._cell_rect(last))
            paint_selection_outline(painter, rect.adjusted(1, 1, -1, -1), alpha=230)

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

    def _paint_labels(self, painter: QPainter, exposed: QRect) -> None:
        """Draw each captioned tile's text over the bottom of its square.

        **Skipped entirely below** :data:`LABEL_MIN_PX`, because a caption that
        does not fit is worse than none: overflowing text spills onto the tiles
        either side and claims to describe them. The zoom control is right there,
        and a sheet read at 1x is being scanned for shape rather than read.

        Drawn on a translucent plate rather than straight onto the art — a glyph
        sheet is light letters on a dark ground about as often as the reverse, so
        text in any single colour disappears on half of them. Bottom-aligned so
        it covers the part of a letter that carries the least of its identity,
        and clipped to its own square so a wide caption is cut rather than
        borrowed from the neighbour.
        """
        if not self._labels:
            return
        cw, ch = self._cell_px[0] * self._zoom, self._cell_px[1] * self._zoom
        if min(cw, ch) < LABEL_MIN_PX:
            return
        font = painter.font()
        font.setPixelSize(max(7, min(ch // 3, cw // 2)))
        painter.setFont(font)
        height = painter.fontMetrics().height()
        for slot, tile_id in enumerate(self._ids):
            caption = self._labels.get(tile_id)
            if not caption:
                continue
            cell = self._cell_rect(slot)
            if not cell.intersects(exposed):
                continue
            strip = QRect(cell.left(), cell.bottom() - height, cell.width(), height)
            painter.fillRect(strip, LABEL_PLATE)
            painter.setPen(LABEL_COLOR)
            painter.drawText(strip, Qt.AlignmentFlag.AlignCenter, caption)

    def sizeHint(self):  # noqa: ANN201 — Qt override
        return self.size()
