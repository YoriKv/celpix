"""Editing a tilemap: flipping cells, and moving them around within the app.

The tilemap half of the shared controls. A flip and a copy exist on both kinds
of document and mean different things on each — flipping a *tile* rewrites
pixels, flipping a *cell* toggles the attribute bit hardware put there for
exactly this — so the control is one and the behaviour resolves here
(``docs/design/tilemap-entry.md`` §4).

Two rules the pixel side does not need:

- **A block flip is two operations.** Reversing the cells' order mirrors the
  layout while leaving each tile facing its original way; toggling each cell's
  bit mirrors every tile in place. A mirrored picture needs both, which is why
  :meth:`~TilemapEditMixin._transform_cell_block` does the permutation *and* the
  toggle, exactly as :meth:`~celpix.core.tilemap.CellGrid.flipped_h` does.
- **The clipboard stays inside celPix.** Cells are indices into a tile source
  that the receiving program knows nothing about — pasted into another editor
  they would be meaningless numbers, and pasted back from one they could name
  anything. So a cell copy goes to an in-app buffer and the system clipboard is
  left alone, holding whatever the user last put there deliberately.

  A **sprite object** is the exception, and for the reason the rule is stated
  that way: it has no cells on the canvas to lift, so what a copy there takes is
  the *pixels of the drawn sheet* — which are a picture, and travel as one
  (:meth:`~TilemapEditMixin._copy_sprite_pixels`).

An edit replaces the whole cell list through one undo command. A map is a few
thousand frozen cells, so a snapshot is cheap next to the bookkeeping a delta
would need — the same trade
:class:`~celpix.ui.undo_commands.TileRearrangementCommand` already makes.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from celpix.core import ceil_div
from celpix.core.arrangement import BlockLayout, compose_window, split_grid
from celpix.core.capabilities import Capability, ContentKind
from celpix.core.errors import PipelineError
from celpix.core.tilemap import Cell, CellGrid
from celpix.pipeline import pipeline
from celpix.ui import clipboard, render_bridge
from celpix.ui.undo_commands import TilemapCellsCommand
from celpix.ui.widgets import counted


class TilemapEditMixin:
    """Cell flips and the in-app cell clipboard.

    A slice of :class:`~celpix.ui.main_window.window.MainWindow`, not a
    standalone object.
    """

    # -- addressing ----------------------------------------------------------
    def _cells_per_row(self) -> int:
        """How many cells the map is drawn across — the layout's own width."""
        return max(1, self._tilemap_columns())

    def _selected_cells(self) -> list[int]:
        """The cell indices the selection covers, in order and without repeats.

        The canvas selects *tiles*, and a cell may be several of them (a panel
        cell is a 2x2 metatile), so a selection can name the same cell four
        times. Cells are emitted in order with their tiles consecutive
        (:func:`~celpix.pipeline.pipeline.tilemap_tiles`), which is what makes
        the slot-to-cell step a division rather than a layout question.

        Indices into :attr:`~celpix.core.document.Document.cells` — the file's own
        order — resolved from the drawn positions through
        :meth:`~celpix.core.document.Document.cell_at`, so a selection over an
        assembled screen file names the cells that are actually under it. They come
        back in **screen** order rather than sorted, because that is the order a
        copy of them travels in and the first of them is the one a paste anchors
        on; on an unassembled map the two orders are the same list.

        The repeats the docstring promises to drop are two different repeats, and
        both have to go. A cell drawn as several tiles is one already, which
        :meth:`_selected_positions` handles. A **stamped** map adds the other
        direction: several drawn positions share one entry, so a selection over a
        block would otherwise name it once per position — and an operation that
        *toggles* rather than sets would cancel itself out on the second visit.
        """
        doc = self._doc
        if doc is None or not doc.is_tilemap:
            return []
        count = len(doc.cells or [])
        seen: dict[int, None] = {}
        for at in self._selected_positions():
            index = doc.cell_at(at)
            if 0 <= index < count:
                seen.setdefault(index, None)
        return list(seen)

    def _selected_positions(self) -> list[int]:
        """The **drawn** positions the selection covers, in screen order.

        The half of the answer above that is about the picture rather than the
        file: where a paste starts, and which cell of the map a rectangle's corner
        is. Kept apart because an assembly makes the two different numbers, and
        every site that mixed them up would look right on every unpaged file.

        Empty on a **sprite object**, whose slots are squares of the drawn sheet
        rather than cells (:meth:`~...selection.SelectionMixin._view_layout`): the
        division below would turn one into a subsprite record it has no relation to.
        """
        doc = self._doc
        if doc is None or not doc.is_tilemap or doc.is_sprite:
            return []
        per_cell = doc.tiles_per_cell
        seen: dict[int, None] = {}
        for slot in self._selection_tiles():
            seen.setdefault(slot // per_cell, None)
        return sorted(seen)

    def _cell_rect(self) -> tuple[int, int, int, int] | None:
        """The selected rectangle in **cell** coordinates, or None.

        The block geometry arrives in canvas **slots**, which are tiles
        (:meth:`~...transform.TransformMixin._block_geometry`); a metatile map
        divides down to its own cell grid. A rectangle narrower than one cell has
        no block to flip, so it comes back None rather than as an empty one.
        """
        doc = self._doc
        geom = self._block_geometry()
        if doc is None or geom is None:
            return None
        cols, rows, x0, y0 = geom
        across, down = max(1, doc.cell_tiles[0]), max(1, doc.cell_tiles[1])
        return (cols // across, rows // down, x0 // across, y0 // down)

    # -- asking the format ---------------------------------------------------
    def _tilemap_engine(self):  # noqa: ANN201 — an (engine, Preset) pair
        """The current entry's cell codec and its preset, or None for neither.

        Every question the editor asks a **format** about its cells goes through
        here — which transforms it can express, how wide its index field is, what
        it is called — and through the same preset id the entry was *read* under
        (:meth:`~...session.SessionMixin._tilemap_preset_id`). That is the point
        of it being one lookup: a probe answering for a different format than the
        document holds would disable the flips and the Cell spin over a map that
        is drawing perfectly well. A tilemap carved out by hand carries no preset
        id of its own — no container declared one — and is read under the default,
        so reading the entry's field raw would answer "no format" for it.

        None off a tilemap entirely, and for a preset id the registry no longer
        has: a format celPix does not have cannot have been asked what its cells
        can do, which is the safe direction for every caller here.
        """
        entry = self._workspace.current
        if entry is None or entry.content_kind is not ContentKind.TILEMAP:
            return None
        try:
            return self._registry.engine_for(self._tilemap_preset_id(entry))
        except KeyError:
            return None

    # -- transforms ----------------------------------------------------------
    def _cell_transform(self, op) -> Callable[[Cell], Cell] | None:  # noqa: ANN001
        """``op`` as this entry's *format* performs it, or None if it cannot.

        The dispatch the whole tilemap transform story hangs on: which of these a
        map supports is a property of its format — a console BG entry has both
        mirror bits, a Game Boy map entry has neither — and only the codec knows
        which bits say it. So the tool names the operation and the codec answers
        (``docs/design/tilemap-entry.md`` §4).

        Probed with a blank cell, so asking costs nothing and changes nothing —
        which is what lets the toolbar ask on every sync and *disable* the
        buttons a format has no bit for, instead of offering them and refusing
        the click (:meth:`~...transform.TransformMixin._transform_allowed`). The
        probe still runs here on the way in, as the backstop for the window
        between a format changing and the bar being re-armed.

        A codec with no answer at all — one written before the method existed —
        refuses, which is the safe direction: it cannot have been asked which of
        its fields a flip means.
        """
        found = self._tilemap_engine()
        if found is None:
            return None
        engine, preset = found
        apply = getattr(engine, "transform_cell", None)
        if apply is None or apply(Cell(), op.cell_op, preset.params) is None:
            return None
        return lambda cell: apply(cell, op.cell_op, preset.params)

    def _tilemap_format_name(self) -> str:
        """This entry's cell format, named for a sentence about what it cannot do."""
        found = self._tilemap_engine()
        return found[1].name if found is not None else "This tilemap format"

    def _refuse_transform(self, op) -> None:  # noqa: ANN001
        """Say which format cannot do this, and why it is the format's answer.

        The button is disabled where this is known in advance, so reaching here
        means the format moved under an armed bar — named rather than silent,
        because a click that does nothing with no reason given is the worst of
        the three outcomes.
        """
        self.statusBar().showMessage(
            f"{self._tilemap_format_name()} has no {op.cell_op.label}"
            " - nothing changed."
        )

    # -- cell references -----------------------------------------------------
    def _cell_index_limit(self) -> int | None:
        """The highest reference this entry's cells can hold, or None for none.

        The codec's answer (:meth:`~celpix.plugins.base.TilemapCodecPlugin.
        index_limit`), on the same protocol the flips follow: a format that does
        not answer has its references left alone rather than clamped to a guess,
        because it cannot have been asked where its index field sits.
        """
        found = self._tilemap_engine()
        if found is None:
            return None
        engine, preset = found
        ask = getattr(engine, "index_limit", None)
        if ask is None:
            return None
        try:
            top = ask(preset.params)
        except Exception:  # noqa: BLE001 — a probe must not break the bar
            return None
        return top if top and top > 0 else None

    def _cell_reference_settable(self) -> bool:
        """Whether this entry has a cell reference to point somewhere at all.

        **Three levels, and they are three different questions**
        (``docs/design/tilemap-entry.md`` §4). ``STAMP`` is the **kind**'s: only a
        tilemap has a cell that names a tile. ``cells_editable`` is this
        **file**'s: a sprite object's records are subsprites at pixel offsets, so
        there is no cell under the cursor to set. The limit is the **format**'s: a
        cell with no index field has no number to hold.

        One predicate because there are two ways to ask it — the binding bar's
        Cell spin types the number and the Edit Tiles tool points at it
        (:meth:`~...stamp_tool.StampToolMixin._stamp_available`) — and a control
        that offered one gesture while the other was refused would be describing
        two different documents.
        """
        doc = self._doc
        return (
            doc is not None
            and self._can(Capability.STAMP)
            and doc.cells_editable
            and self._cell_index_limit() is not None
        )

    # -- cell palette rows ---------------------------------------------------
    def _cell_palette_row_limit(self) -> int | None:
        """The highest palette row this entry's own cells can hold, or None.

        :meth:`_cell_index_limit` for the colour field, on the same protocol
        (:meth:`~celpix.plugins.base.TilemapCodecPlugin.palette_row_limit`) and
        refusing on the same terms: a codec that does not answer has its rows
        left alone rather than clamped to a guess.

        It answers **this file's** format, which is what makes it the right test
        for a chained map: a stamp layout's word is a coordinate with no palette
        field, so the row a position draws through is the *panel's* and the panel
        is where it can be changed (``docs/design/tilemap-entry.md`` §3.1). The
        drawn row and the writable row are two different questions there, and
        only this one is about bytes this entry owns.

        None too on a sprite object, whose records this path cannot reach at all
        (:attr:`~celpix.core.document.Document.cells_editable`) — stated here so
        the caller has one predicate rather than two.
        """
        doc = self._doc
        found = self._tilemap_engine()
        if doc is None or not doc.cells_editable or found is None:
            return None
        engine, preset = found
        ask = getattr(engine, "palette_row_limit", None)
        if ask is None:
            return None
        try:
            top = ask(preset.params)
        except Exception:  # noqa: BLE001 — a probe must not break the bar
            return None
        return top if top and top > 0 else None

    def _assign_cell_palette_row(self) -> None:
        """The tilemap reading of the pin gesture: write the row into the cells.

        Same question as pinning a region and the same row picked the same way
        (:meth:`~...palette_regions.PaletteRegionsMixin._named_row_picked`) —
        what differs is where it is kept. A pixel document has nothing in its
        bytes that could say a row, so a pin is display state the project
        carries; a cell has the field already, so this is an ordinary cell edit
        and the file keeps the answer.

        Clamped to what the field can hold rather than left for :meth:`encode` to
        mask down later, the rule :meth:`_set_cell_index` follows: a row wider
        than three bits would come back as a row nobody asked for.
        """
        doc = self._doc
        indices = self._selected_cells()
        if doc is None or doc.cells is None or not indices:
            return
        limit = self._cell_palette_row_limit()
        if limit is None:
            self.statusBar().showMessage(
                f"{self._tilemap_format_name()} has no palette row to set"
                " - nothing changed."
            )
            return
        row = max(0, min(self._named_row_picked(), limit))
        cells = list(doc.cells)
        for at in indices:
            cells[at] = replace(cells[at], palette_row=row)
        if self._apply_cells(cells, "set cell palette row"):
            shown = self._drawn_palette_row(row)
            self.statusBar().showMessage(
                f"Set {counted(len(indices), 'cell')} to subpalette {shown}."
            )

    def _set_cell_index(self, value: int) -> None:
        """Point every selected cell at reference ``value`` — one undoable step.

        What the number *means* follows the document, and both readings are the
        same edit to the same field. On an ordinary map it is a tile in the bound
        source. On a **chained** map it is a position in the map being drawn
        through, so setting it is the **restamp**: this position now takes that
        source cell's tile and attributes, and the stamp itself stays editable on
        the map it came from (``docs/design/tilemap-entry.md`` §3.1).

        Clamped to what the format can hold rather than left for :meth:`encode` to
        mask down later, so the cell that lands is the one that was asked for.
        """
        doc = self._doc
        indices = self._selected_cells()
        if doc is None or doc.cells is None or not indices:
            return
        limit = self._cell_index_limit()
        if limit is None:
            self.statusBar().showMessage(
                f"{self._tilemap_format_name()} has no cell reference to set"
                " - nothing changed."
            )
            return
        value = max(0, min(value, limit))
        cells = list(doc.cells)
        for at in indices:
            cells[at] = replace(cells[at], index=value)
        if self._apply_cells(cells, "set cell reference"):
            what = "stamp" if doc.is_indirect else "tile"
            self.statusBar().showMessage(
                f"Pointed {counted(len(indices), 'cell')} at {what} ${value:X}."
            )

    def _selected_cell_index(self) -> int:
        """The reference the first selected cell holds — what the spin shows."""
        doc = self._doc
        indices = self._selected_cells()
        if doc is None or doc.cells is None or not indices:
            return 0
        return doc.cells[indices[0]].index

    def _transform_cells(self, op) -> None:  # noqa: ANN001 — a TransformOp
        """Apply ``op`` to every selected cell, in place.

        No permutation: the cells stay where they are and each one's own tile is
        transformed, which is what the Tile group means on the pixel side too.
        """
        doc = self._doc
        indices = self._selected_cells()
        if doc is None or doc.cells is None or not indices:
            return
        apply = self._cell_transform(op)
        if apply is None:
            self._refuse_transform(op)
            return
        cells = list(doc.cells)
        for index in indices:
            cells[index] = apply(cells[index])
        if self._apply_cells(cells, f"{op.verb} cells"):
            self.statusBar().showMessage(f"{op.past} {counted(len(indices), 'cell')}.")

    def _transform_cell_block(self, op) -> None:  # noqa: ANN001 — a TransformOp
        """Transform the selected block: reorder the cells **and** transform each.

        ``op.cell_src`` gives the permutation, shared with the pixel block path
        so the two cannot disagree about direction; the per-cell half goes
        through the format, and a format that cannot do it stops the whole block
        rather than leaving it reordered but unturned.
        """
        doc = self._doc
        rect = self._cell_rect()
        if doc is None or doc.cells is None or rect is None:
            return
        cols, rows, x0, y0 = rect
        if cols <= 0 or rows <= 0:
            return
        apply = self._cell_transform(op)
        if apply is None:
            self._refuse_transform(op)
            return
        width = self._cells_per_row()
        cells = list(doc.cells)
        original = list(doc.cells)
        moved = 0
        for dy in range(rows):
            for dx in range(cols):
                sx, sy = op.cell_src(dx, dy, cols, rows)
                # Both ends through the document, because the block is a rectangle
                # of the *picture* and the cells it holds need not be a run of the
                # file: on an assembled screen a block spanning two pages moves
                # cells between them, which is what the user drew over.
                dest = doc.cell_at((y0 + dy) * width + (x0 + dx))
                src = doc.cell_at((y0 + sy) * width + (x0 + sx))
                if 0 <= dest < len(cells) and 0 <= src < len(original):
                    cells[dest] = apply(original[src])
                    moved += 1
        if moved and self._apply_cells(cells, f"{op.verb} cell block"):
            self.statusBar().showMessage(f"{op.past} the {cols}x{rows} cell block.")

    # -- the in-app clipboard ------------------------------------------------
    def _copy_cells(self) -> bool:
        """Lift the selected rectangle of cells into the in-app buffer.

        A rectangle so a paste can put it back with its shape; a linear
        selection copies as one row, which is what it looks like on screen.

        A **sprite object** copies its pixels instead. Its cells are not what is
        on screen — a canvas position there is a *subsprite* through an overlap
        order,
        so lifting cells would take records the user never pointed at — but the
        picture under the selection is perfectly well defined, and copying what
        you can see is the gesture that was missing rather than one to refuse
        (:meth:`_copy_sprite_pixels`).
        """
        doc = self._doc
        if doc is not None and doc.is_sprite:
            return self._copy_sprite_pixels()
        if doc is None or doc.cells is None or self._refuse_view_only():
            return False
        rect = self._cell_rect()
        width = self._cells_per_row()
        if rect is not None and rect[0] > 0 and rect[1] > 0:
            cols, rows, x0, y0 = rect
            lifted = CellGrid(cols, rows)
            for dy in range(rows):
                for dx in range(cols):
                    at = doc.cell_at((y0 + dy) * width + (x0 + dx))
                    if 0 <= at < len(doc.cells):
                        lifted.set(dx, dy, doc.cells[at])
        else:
            indices = self._selected_cells()
            if not indices:
                return False
            lifted = CellGrid.from_cells(
                len(indices), 1, [doc.cells[i] for i in indices]
            )
        self._cell_clipboard = lifted
        self._sync_edit_actions()
        self.statusBar().showMessage(f"Copied {counted(len(lifted), 'cell')}.")
        return True

    def _copy_sprite_pixels(self) -> bool:
        """Lift the selected tiles of a sprite sheet as pixels; False if none.

        The one tilemap copy that goes out to the **system** clipboard, and the
        module docstring's rule is why: what this lifts is not cells naming tiles
        in a file the receiving program has never heard of, it is the picture on
        screen. So it travels the way a pixel document's copy does — the tiles
        themselves for a lossless paste back into celPix, and a rendered image so
        every other program sees an ordinary sprite.

        Cut out of the **composed sheet** rather than fetched from the tile bank,
        which is what makes it the picture and not the ingredients: a subsprite sits
        at a signed pixel offset and they overlap, so an 8x8 of the sheet is
        generally pieces of two source tiles and neither of them whole
        (:func:`~celpix.pipeline.pipeline.sprite_image`). ``split_grid`` undoes
        exactly the placement that composed it, given the same layout the
        selection reads its slots off.

        The indices carry their subsprites' palette rows already folded in, as they do
        everywhere a tilemap is drawn, so the colours that go with them are the
        whole window those rows reach across — the same table an export sizes —
        and the image renders through the pinned path that expects them.
        """
        doc = self._doc
        slots = self._selection_tiles()
        if doc is None or not slots:
            return False
        drawn = pipeline.tilemap_image(doc, self._registry, self._tilemap_columns())
        tile_w, tile_h = doc.tile_width, doc.tile_height
        sheet = split_grid(drawn.grid, tile_w, tile_h, self._view_layout())
        tiles = [sheet[slot] for slot in slots if 0 <= slot < len(sheet)]
        if not tiles:
            return False
        space = self._index_space()
        colors = tuple(
            doc.palette.color(at) for at in range(drawn.palette_rows * space)
        )
        columns = self._copy_columns(len(tiles))
        rows = ceil_div(len(tiles), columns)
        picture = compose_window(tiles, columns, 0, rows, BlockLayout(columns))
        clipboard.put(
            clipboard.TilePayload.from_tiles(tiles, colors, columns=columns),
            render_bridge.render_pinned(picture, doc.palette),
        )
        self._sync_edit_actions()
        self.statusBar().showMessage(f"Copied {counted(len(tiles), 'tile')}.")
        return True

    def _cut_cells(self) -> None:
        # Up front, so a sprite object refuses here rather than copying its pixels
        # and then discovering there is nothing to blank behind them.
        if self._refuse_view_only():
            return
        if self._copy_cells():
            self._clear_cells("cut cells")

    def _clear_cells(self, text: str = "clear cells") -> None:
        """Blank the selected cells — index 0, no attributes.

        A tilemap has a fixed extent, so clearing is writing the empty cell
        rather than removing anything: there is no shorter map to leave behind.

        Refused up front, as a copy is, rather than on the way into
        :meth:`_apply_cells`: a sprite object's selection names no cells at all
        (:meth:`_selected_positions`), so the empty list below would leave with
        nothing said about why.
        """
        doc = self._doc
        if doc is None or doc.cells is None or self._refuse_view_only():
            return
        indices = self._selected_cells()
        if not indices:
            return
        cells = list(doc.cells)
        for index in indices:
            # The format's own bits stay: `visible` says whether the *position* is
            # drawn and `flags` carries what celPix does not model, neither of
            # which is content this is being asked to clear. Writing a bare `Cell`
            # made clearing the only way to un-hide a position, and did it while
            # silently dropping bit 15 of a stamp layout's entry.
            cells[index] = Cell(visible=cells[index].visible, flags=cells[index].flags)
        if self._apply_cells(cells, text):
            self.statusBar().showMessage(f"Cleared {counted(len(indices), 'cell')}.")

    def _paste_cells(self) -> None:
        """Stamp the buffer over the map from the selection's first cell.

        Overwrite and clipped, never inserting: the map's extent is the file's,
        so a paste replaces exactly as many cells as there is room for.
        """
        doc = self._doc
        block = getattr(self, "_cell_clipboard", None)
        if doc is None or doc.cells is None or block is None or not len(block):
            self.statusBar().showMessage("No cells copied yet.")
            return
        # The anchor is where the selection is *drawn*, not which cell of the file
        # it holds: a paste lays a rectangle over the picture, and on an assembled
        # map those are different numbers (:meth:`_selected_positions`).
        positions = self._selected_positions()
        start = positions[0] if positions else 0
        width = self._cells_per_row()
        x0, y0 = start % width, start // width
        cells = list(doc.cells)
        written = 0
        for dy in range(block.height):
            for dx in range(block.width):
                x, y = x0 + dx, y0 + dy
                at = doc.cell_at(y * width + x)
                if x < width and 0 <= at < len(cells):
                    cells[at] = block.get(dx, dy)
                    written += 1
        if not written:
            self.statusBar().showMessage("Nothing pasted - no room here.")
            return
        if self._apply_cells(cells, "paste cells"):
            clipped = len(block) - written
            note = f" ({clipped} clipped)" if clipped else ""
            self.statusBar().showMessage(f"Pasted {counted(written, 'cell')}{note}.")

    def _has_cell_clipboard(self) -> bool:
        """Whether a cell paste would have anything to put down."""
        block = getattr(self, "_cell_clipboard", None)
        return block is not None and len(block) > 0

    # -- committing ----------------------------------------------------------
    def _apply_cells(self, cells: list[Cell], text: str) -> bool:
        """Push ``cells`` as one undoable edit; False when nothing changed.

        The no-change guard is what keeps a flip of an empty selection, or a
        paste of identical cells, from putting a step on the undo stack that
        would appear to do nothing when it came back.

        Refused outright only on a sprite object, whose cells are subsprites placed at
        pixel offsets rather than positions in a grid
        (:attr:`~celpix.core.document.Document.cells_editable`). A chained map does
        land here: its cells are coordinates, and writing one restamps that
        position.
        """
        doc = self._doc
        entry = self._workspace.current
        if self._refuse_view_only():
            return False
        if doc is None or doc.cells is None or entry is None or cells == doc.cells:
            return False
        self._push_command(
            TilemapCellsCommand(self, entry, text, list(doc.cells), cells)
        )
        return True

    def _refuse_view_only(self) -> bool:
        """True — with the reason on the status bar — when cells cannot be edited.

        Only a **sprite object** reaches this: a canvas position there resolves to
        a *subsprite* through an overlap order rather than to a cell through a grid,
        so
        there is no cell under the cursor for an edit to change
        (:attr:`~celpix.core.document.Document.cells_editable`). A chained map is
        editable — a cell edit restamps it — and what its own format cannot express
        is refused per operation by the codec instead (:meth:`_cell_transform`,
        :meth:`_cell_index_limit`), which is a narrower answer than a whole
        document being read-only.

        The controls this catches are disabled there
        (:meth:`~...selection.SelectionMixin._sync_edit_actions`), so this is the
        guard behind them rather than the way the user finds out. It stays a
        *message* and not an assertion because the reason is not guessable from
        the object on screen.
        """
        doc = self._doc
        if doc is None or not doc.is_tilemap or doc.cells_editable:
            return False
        self.statusBar().showMessage(
            "A sprite object is view-only - edit the tiles it draws from."
        )
        return True

    def _set_cells(self, entry, cells: list[Cell], revision: int) -> None:  # noqa: ANN001
        """Land a cell list on ``entry`` — the command's apply, both directions.

        ``revision`` stamps the data pathway, so the entry reads dirty against
        what was last written and an undo back to the saved state reads clean
        again. A tilemap's cells are its own data, which is the same pathway a
        pixel entry's bytes use (:func:`~celpix.pipeline.pipeline.save`).

        Both directions of the chain are settled here, which is what makes a
        restamp show up: this document re-resolves its own new coordinates, and
        anything drawing *through* it is re-pointed at the cells it now has
        (:meth:`~...session.SessionMixin._rechain_dependents`).
        """
        if entry.doc is not None:
            entry.doc.cells = list(cells)
            entry.doc.resolve()
            self._reencode_cells(entry.doc)
        touched = self._rechain_dependents(entry)
        self._workspace.set_pixel_revision(entry, revision)
        if entry is self._workspace.current or touched:
            self._refresh_view()

    def _reencode_cells(self, doc) -> None:  # noqa: ANN001 — a Document
        """Bring ``doc.tilemap_data`` back in step with the cells above it.

        The cells are the source of truth and the buffer is what they were read
        from, so an edit leaves the two disagreeing. Everything that reads the
        *bytes* instead of the cells then shows the file as it was opened rather
        than as it stands — the hex dump under the map, and Export Raw. Doing it
        here, at the one writer of the cells, is what keeps those two from each
        needing their own answer, and costs one encode per committed edit: the
        same order as the whole-list snapshot the undo command already takes.

        **Spliced, not replaced.** A decode drops a trailing partial cell — a file
        need not hold a whole number of them — so the re-encode can be shorter
        than the buffer it came from, and anything past the last cell stays as it
        was read. The save path preserves it the same way, one level up, through
        the container's own write.

        A format that cannot encode what is now in the cells leaves the buffer
        alone rather than emptying it; the save is where that has to be reported,
        and it asks the codec again.
        """
        if not doc.is_tilemap or doc.tilemap_config is None:
            return
        try:
            data = pipeline.encode_cells(
                doc.cells or [],
                doc.tilemap_config.interpret_preset_id,
                self._registry,
                doc.tilemap_ctx,
            )
        except (KeyError, PipelineError):
            return
        doc.tilemap_data = data + doc.tilemap_data[len(data) :]
