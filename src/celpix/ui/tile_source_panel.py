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
dragging here picks one; a **right drag** sweeps a rectangle of them instead,
reported whole through ``area_selected`` for the stamp tool to hold as one rectangle —
the same gesture, with the same meaning, as the right drag over the canvas's
cells (``stamp_tool.py``). A caller that already holds a wider pick — the font
alphabet window's table, where a stretch of rows is a stretch of tiles
(:mod:`celpix.ui.font_alphabet_window`) — states the whole set with
:meth:`~TileSourcePanel.select_ids`. A set-shaped pick is drawn as the canvas
draws a multi-tile selection — one outline per contiguous run of a display row,
so a scattered pick does not claim to be one rectangle — while the sweep keeps its
own shape and rings as the one rectangle it is
(:meth:`~TileSourcePanel._paint_selection`).

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
from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import QWidget

# The caption constants are the sheet's (:mod:`celpix.ui.sheet_surface`) and are
# re-exported here, where the captions were first drawn.
from celpix.ui.sheet_surface import (
    LABEL_COLOR,
    LABEL_MIN_PX,
    LABEL_PLATE,
    SheetSurface,
)
from celpix.ui.theme import GRID_STRUCTURE_COLOR
from celpix.ui.widgets import (
    GRID_ARROWS,
    ShortcutIsland,
    grid_slot_at,
    grid_step,
    paint_mark_ring,
    paint_pick_ring,
)

__all__ = [
    "GRID_STEP_TILES",
    "LABEL_COLOR",
    "LABEL_MIN_PX",
    "LABEL_PLATE",
    "ZOOM_RANGE",
    "TileSourcePanel",
]

# How many tiles apart the lattice sits, both ways. A **fixed** step, unlike the
# canvas's configurable grid: this is not marking tile boundaries — the tiles are
# already visibly separate here — it is marking where the *numbering* rolls over,
# and 16 across by 16 down is the 0x100-tile page a bank is addressed in. At the
# sheet's default 16 columns that lands a line under every 256 IDs, which is the
# unit a base tile is usually a multiple of.
GRID_STEP_TILES = 16

# The magnifications the sheet offers, clamped here so a caller with only the
# wheel (the font alphabet window) stays inside them. The tile source dock's Zoom
# spin ranges over the same levels.
ZOOM_RANGE = (1, 8)


class TileSourcePanel(ShortcutIsland, SheetSurface):
    tile_selected = Signal(int)  # the ID of the newly selected tile
    # A right drag's rectangle, as rows of IDs top to bottom — ``None`` where
    # the sweep overhung the empty tail of a short last row, so the shape
    # survives where the run ran out. Emitted on release, and only for a sweep
    # that left its anchor square: a right click is an ordinary pick and has
    # already said so through ``tile_selected``.
    area_selected = Signal(object)  # list[list[int | None]]
    zoom_requested = Signal(int, object)  # steps, QPointF cursor pos (widget)
    pan_requested = Signal(int, int)  # dx, dy

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._ids: Sequence[int] = range(0)
        self._selected: int | None = None
        # Every picked tile, the current one included. Held beside `_selected`
        # rather than instead of it because the two answer different questions:
        # what is outlined, and which tile a stamp places and the arrows step from.
        self._picked: frozenset[int] = frozenset()
        self._marked: int | None = None
        # A right drag in flight: the anchor slot its press landed on, and the
        # far slot it last swept over. Slots rather than IDs, because the
        # rectangle is a shape on the lattice and the run may step over the
        # IDs between two units — only slots can say what sits next to what.
        self._area_anchor: int | None = None
        self._area_far: int | None = None
        # The shape of the pick, where the pick *has* one: the swept rectangle
        # in cell coordinates (x0, y0, x1, y1), outlasting the drag so the ring
        # stays one box. None for every set-shaped pick, which draws per run.
        self._picked_rect: tuple[int, int, int, int] | None = None
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
        # The pick's rectangle is a shape on the *old* lattice: it survives a
        # recompose that kept the run and the width (a palette edit), and is
        # dropped with anything else, where its slots would name other tiles.
        if list(ids) != list(self._ids) or max(1, columns) != self._columns:
            self._picked_rect = None
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
        # A sweep in flight is anchored to a slot of the *old* run; the new one
        # may not reach it, so the drag is dropped with the selection.
        self._area_anchor = self._area_far = None
        self._update_size()

    _zoom_range = ZOOM_RANGE

    def _slot_count(self) -> int:
        return len(self._ids)

    @property
    def zoom(self) -> int:
        """The magnification on show, for a caller with no spin of its own.

        The dock drives this panel from a spin box and so already knows the
        answer; the font alphabet window has only the wheel, and stepping it
        needs the value it is stepping from.
        """
        return self._zoom

    def set_labels(self, labels: dict[int, str]) -> None:
        """Write a short caption into the corner of each tile in ``labels``.

        Keyed by ID, so a caller states what it knows about a tile rather than
        about a square, and a re-laid sheet carries the captions with it.

        For the font alphabet editor, where the caption is what the tile *says*
        (:mod:`celpix.ui.font_alphabet_window`) — a reading the picture cannot
        give, since a font's tile is a letter shape and the question is which
        letter the game thinks it is. Empty by default and empty in the tile
        source dock, which has nothing of that kind to add.

        A character the UI font cannot draw shows as tofu. That is the
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

    def clear_selection(self) -> None:
        """Drop the pick, leaving the sheet up — nothing is selected now.

        Silent, like the drop :meth:`set_sheet` makes for an ID no longer on
        offer: the caller unpicking is about to re-read the readout itself, and
        an emit would announce a pick the user did not make.
        """
        if self._selected is None and not self._picked:
            return
        self._picked, self._selected = frozenset(), None
        self._picked_rect = None
        self.update()

    def _select(self, tile_id: int, *, announce: bool = False) -> None:
        self._pick(frozenset({tile_id}), tile_id, announce=announce)

    def _pick(
        self,
        picked: frozenset[int],
        current: int,
        *,
        announce: bool = False,
        rect: tuple[int, int, int, int] | None = None,
    ) -> None:
        """Land a selection, reporting only a change of the *current* tile.

        Widening a pick that still has the same tile current is not a new tile
        being picked — everything downstream of the signal (the canvas, the Cell
        spin, the ring in the dock) speaks of one tile, and would be told the
        same one again. ``announce`` is the user gestures' exception, in the
        other direction: a click that *narrows* a wider pick keeps the same
        tile current, but the dock must still hear it — a held area brush
        describes the pick it was swept from, and this is the pick replacing
        it (``tile_source_dock.py``). A pick that changed nothing at all is
        never emitted either way.

        ``rect`` is the pick's *shape*, where it has one — the sweep's
        rectangle in cell coordinates — and defaulting it is what keeps the
        shape honest: every other pick is a set, so any of them landing drops
        the rectangle and the ring falls back to per-run outlines.
        """
        if picked == self._picked and current == self._selected:
            if rect == self._picked_rect:
                return
            self._picked_rect = rect
            self.update()
            return
        moved = current != self._selected
        self._picked, self._selected = picked, current
        self._picked_rect = rect
        self.update()
        if moved or announce:
            self.tile_selected.emit(current)

    # -- geometry ------------------------------------------------------------
    def _slot_at(self, x_px: float, y_px: float, *, clamp: bool = False) -> int | None:
        """The slot under a widget position — or, ``clamp``ed, the nearest one.

        The clamped reading is what a drag wants, so scrubbing off an edge (or
        past the last, partly filled row) keeps the pick following the pointer.
        ``None`` for a click on nothing, or with the sheet empty.
        """
        cell_w, cell_h = self._cell_px
        return grid_slot_at(
            x_px,
            y_px,
            (cell_w * self._zoom_x, cell_h * self._zoom_y),
            self._columns,
            len(self._ids),
            clamp=clamp,
        )

    def _id_at(self, x_px: float, y_px: float, *, clamp: bool = False) -> int | None:
        """The ID under a widget position, on :meth:`_slot_at`'s terms."""
        slot = self._slot_at(x_px, y_px, clamp=clamp)
        return None if slot is None else self._ids[slot]

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
        if event.button() in (Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton):
            slot = self._slot_at(event.position().x(), event.position().y())
            if slot is not None:
                self._select(self._ids[slot], announce=True)
                if event.button() == Qt.MouseButton.RightButton:
                    # The press is an ordinary pick either way; whether it
                    # grows into an area is the drag's to say.
                    self._area_anchor = self._area_far = slot
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: ANN001 — Qt override
        """Drag to scrub the pick across the sheet, edges included — or, with
        the right button, to sweep a rectangle of tiles."""
        if self._pan_move(event):
            return
        buttons = event.buttons()
        anchor = self._area_anchor
        if buttons & Qt.MouseButton.RightButton and anchor is not None:
            slot = self._slot_at(event.position().x(), event.position().y(), clamp=True)
            if slot is not None and slot != self._area_far:
                self._area_far = slot
                rows = self._area_rows(anchor, slot)
                ids = [tid for row in rows for tid in row if tid is not None]
                # The top-left is current — the corner a stamp lays from. It is
                # always a real tile: only the last row can end short, and the
                # rectangle's top-left sits no later than either swept corner.
                corner = rows[0][0]
                assert corner is not None
                self._pick(
                    frozenset(ids),
                    corner,
                    announce=True,
                    rect=self._area_span(anchor, slot),
                )
            event.accept()
            return
        if not buttons & Qt.MouseButton.LeftButton:
            super().mouseMoveEvent(event)
            return
        tile_id = self._id_at(event.position().x(), event.position().y(), clamp=True)
        if tile_id is not None:
            self._select(tile_id, announce=True)
        event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: ANN001 — Qt override
        if self._pan_release(event):
            return
        anchor, far = self._area_anchor, self._area_far
        if event.button() == Qt.MouseButton.RightButton and anchor is not None:
            self._area_anchor = self._area_far = None
            # A drag that never left its anchor square is the click its press
            # already made — the canvas's rule for the same gesture.
            if far is not None and far != anchor:
                self.area_selected.emit(self._area_rows(anchor, far))
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _area_span(self, anchor: int, far: int) -> tuple[int, int, int, int]:
        """The rectangle between two slots, as cell coordinates (x0, y0, x1, y1)."""
        x0, x1 = sorted((anchor % self._columns, far % self._columns))
        y0, y1 = sorted((anchor // self._columns, far // self._columns))
        return x0, y0, x1, y1

    def _area_rows(self, anchor: int, far: int) -> list[list[int | None]]:
        """The IDs in the rectangle between two slots, as rows top to bottom.

        ``None`` where a slot holds no entry — the rectangle can overhang the
        empty tail of a short last row — so the shape survives the gap: what
        is being reported is a rectangle to stamp, and collapsing the holes
        would shear it.
        """
        x0, y0, x1, y1 = self._area_span(anchor, far)
        count = len(self._ids)
        return [
            [
                self._ids[slot] if (slot := y * self._columns + x) < count else None
                for x in range(x0, x1 + 1)
            ]
            for y in range(y0, y1 + 1)
        ]

    def keyPressEvent(self, event) -> None:  # noqa: ANN001 — Qt override
        """Arrows step the pick — Left/Right by one square (crossing display
        rows), Up/Down by one row. Stepped in **slots**, not IDs: the two agree
        on an unbroken run and the run is not always unbroken, and what a user
        means by Right is the next square either way. Movement clamps to the
        sheet; a step off the top or bottom stays put rather than being yanked to
        a corner, which would change the column under the user."""
        if not self._ids or event.key() not in GRID_ARROWS:
            super().keyPressEvent(event)
            return
        current = self._slot_of(self._selected) if self._selected is not None else None
        target = grid_step(current, event.key(), self._columns, len(self._ids))
        if target is not None:
            self._select(self._ids[target], announce=True)
        event.accept()

    # -- painting ------------------------------------------------------------
    def paintEvent(self, event) -> None:  # noqa: ANN001 — Qt override
        painter = QPainter(self)
        self._paint_sheet(painter, event.rect())
        if not self._sheet.isNull():
            self._paint_grid(painter, event.rect())
            self._paint_labels(painter, event.rect())
        # The canvas's cell first, in the structural blue; this panel's own pick
        # inset one pixel inside it and slightly soft — so a tile that is both
        # still reads as two rings rather than one thick one, and the two answer
        # visibly different questions rather than sitting one alpha step apart.
        # Both in physical pixels, so on a scaled screen they sit on the
        # squares' real edges, where the lattice is.
        with self._device_pixels(painter):
            ring = self._device_width(1)
            if self._marked is not None:
                paint_mark_ring(
                    painter, self._device_cell_rect(self._slot_of(self._marked)), ring
                )
            self._paint_selection(painter, event.rect(), ring)
        painter.end()

    def _paint_selection(self, painter: QPainter, exposed: QRect, ring: int) -> None:
        """Outline the picked tiles, one ring per contiguous run of a row —
        in physical pixels, ``ring`` each layer's width in those.

        The canvas's rule for a multi-tile selection (:meth:`~celpix.ui.canvas.
        Canvas._paint_selection`): a run drawn as one box reads as the stretch it
        is, and a gap in it is a gap the user can see. Run in **slots**, since
        the sheet steps over the IDs between two units and only the list can say
        which squares sit next to each other.

        A pick with a **shape** — the right drag's sweep — draws that shape
        instead: one box around the whole rectangle, because that is the one
        thing the user chose and the brush holds, and a stack of row rings
        would claim it is several stretches. Squares past the run's end stay
        inside it — they are part of the swept stamp — and the paint is left
        to Qt's own clip, one rectangle being cheaper than asking what is
        exposed.

        Inset a pixel and slightly soft, so a tile that is also *marked* still
        reads as two rings rather than one thick one.

        The **exposed rows only**, which costs no ring: a run never crosses a
        row and the band starts on a row boundary, so a row outside it is a row
        whose rings would be drawn entirely off the repainted band anyway.
        """
        if self._picked_rect is not None:
            x0, y0, x1, y1 = self._picked_rect
            rect = self._device_cell_rect(y0 * self._columns + x0).united(
                self._device_cell_rect(y1 * self._columns + x1)
            )
            paint_pick_ring(painter, rect, ring)
            return
        if not self._picked:
            return
        slots = [
            slot
            for slot in self._exposed_slots(exposed)
            if self._ids[slot] in self._picked
        ]
        runs: list[tuple[int, int]] = []
        for slot in slots:  # ascending, the band being a range
            # A slot in column 0 starts a row and so starts a run, whatever sat
            # before it: the square before it on the sheet is the far end of the
            # line above, and one ring around both would enclose the whole width.
            if runs and slot == runs[-1][1] + 1 and slot % self._columns:
                runs[-1] = (runs[-1][0], slot)
            else:
                runs.append((slot, slot))
        for first, last in runs:
            paint_pick_ring(
                painter,
                self._device_cell_rect(first).united(self._device_cell_rect(last)),
                ring,
            )

    def _paint_grid(self, painter: QPainter, exposed: QRect) -> None:
        """Rule the sheet every :data:`GRID_STEP_TILES` cells, both ways.

        The canvas's *structural* colour rather than its fine one, because that
        is what this is: the same blue marks a block boundary over there, and one
        grid language across the two is worth more than a second palette of
        lines. Drawn over the tiles and under both rings, so a marked tile on a
        boundary still reads as marked.

        Interior lines only, and only in the exposed band
        (:meth:`~celpix.ui.sheet_surface.SheetSurface._paint_lattice`).
        """
        self._paint_lattice(painter, exposed, GRID_STEP_TILES, GRID_STRUCTURE_COLOR)

    def _paint_labels(self, painter: QPainter, exposed: QRect) -> None:
        """Draw each captioned tile's text over the bottom of its square.

        **Skipped entirely below** :data:`LABEL_MIN_PX`, because a caption that
        does not fit is worse than none: overflowing text spills onto the tiles
        either side and claims to describe them. The zoom control is right there,
        and a sheet read at 1x is being scanned for shape rather than read.

        Drawn on a translucent plate rather than straight onto the art — a font
        sheet is light letters on a dark ground about as often as the reverse, so
        text in any single colour disappears on half of them. Bottom-aligned so
        it covers the part of a letter that carries the least of its identity,
        and clipped to its own square so a wide caption is cut rather than
        borrowed from the neighbour.
        """
        if not self._labels:
            return
        height = self._caption_height(painter, 3, 2)
        if height is None:
            return
        for slot in self._exposed_slots(exposed):
            caption = self._labels.get(self._ids[slot])
            if not caption:
                continue
            cell = self._cell_rect(slot)
            if cell.intersects(exposed):
                self._paint_caption(painter, cell, height, caption)
