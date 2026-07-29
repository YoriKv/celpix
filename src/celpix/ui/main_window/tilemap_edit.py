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

An edit replaces the whole cell list through one undo command. A map is a few
thousand frozen cells, so a snapshot is cheap next to the bookkeeping a delta
would need — the same trade
:class:`~celpix.ui.undo_commands.TileRearrangementCommand` already makes.
"""

from __future__ import annotations

from collections.abc import Callable

from celpix.core.tilemap import Cell, CellGrid
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
        """
        doc = self._doc
        if doc is None or not doc.is_tilemap:
            return []
        per_cell = doc.tiles_per_cell
        count = len(doc.cells or [])
        seen = {slot // per_cell for slot in self._selection_tiles()}
        return sorted(index for index in seen if 0 <= index < count)

    def _cell_rect(self) -> tuple[int, int, int, int] | None:
        """The selected rectangle in **cell** coordinates, or None.

        The block geometry is in canvas cells, which are tiles; a metatile map
        divides down to its own grid. A rectangle narrower than one cell has no
        block to flip, so it comes back None rather than as an empty one.
        """
        doc = self._doc
        geom = self._block_geometry()
        if doc is None or geom is None:
            return None
        cols, rows, x0, y0 = geom
        across, down = max(1, doc.cell_tiles[0]), max(1, doc.cell_tiles[1])
        return (cols // across, rows // down, x0 // across, y0 // down)

    # -- transforms ----------------------------------------------------------
    def _cell_transform(self, op) -> Callable[[Cell], Cell] | None:  # noqa: ANN001
        """``op`` as this entry's *format* performs it, or None if it cannot.

        The dispatch the whole tilemap transform story hangs on: which of these a
        map supports is a property of its format — a console BG entry has both
        mirror bits, a Game Boy map entry has neither — and only the codec knows
        which bits say it. So the tool names the operation and the codec answers
        (``docs/design/tilemap-entry.md`` §4).

        Probed with a blank cell before anything is touched, so a refusal costs a
        message rather than a half-applied selection. A codec with no answer at
        all — one written before the method existed — refuses, which is the safe
        direction: it cannot have been asked which of its fields a flip means.
        """
        entry = self._workspace.current
        if entry is None or not entry.tilemap_preset_id:
            return None
        try:
            engine, preset = self._registry.engine_for(entry.tilemap_preset_id)
        except KeyError:
            return None
        apply = getattr(engine, "transform_cell", None)
        if apply is None or apply(Cell(), op.cell_op, preset.params) is None:
            return None
        return lambda cell: apply(cell, op.cell_op, preset.params)

    def _refuse_transform(self, op) -> None:  # noqa: ANN001
        """Say which format cannot do this, and why it is the format's answer."""
        entry = self._workspace.current
        name = ""
        if entry is not None and entry.tilemap_preset_id:
            try:
                name = self._registry.preset(entry.tilemap_preset_id).name
            except KeyError:
                name = ""
        subject = name or "This tilemap format"
        self.statusBar().showMessage(
            f"{subject} has no {op.cell_op.label} - nothing changed."
        )

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
                dest = (y0 + dy) * width + (x0 + dx)
                src = (y0 + sy) * width + (x0 + sx)
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

        Refused on a view-only map for the same reason an edit is, and the same
        reason stated: on a sprite object what is under the cursor is a *part*
        rather than a cell, so a copy taken there would lift records the user
        never pointed at and a later paste would write them somewhere real.
        """
        doc = self._doc
        if doc is None or doc.cells is None or self._refuse_view_only():
            return False
        rect = self._cell_rect()
        width = self._cells_per_row()
        if rect is not None and rect[0] > 0 and rect[1] > 0:
            cols, rows, x0, y0 = rect
            block = CellGrid(cols, rows)
            for dy in range(rows):
                for dx in range(cols):
                    at = (y0 + dy) * width + (x0 + dx)
                    if 0 <= at < len(doc.cells):
                        block.set(dx, dy, doc.cells[at])
        else:
            indices = self._selected_cells()
            if not indices:
                return False
            block = CellGrid.from_cells(
                len(indices), 1, [doc.cells[i] for i in indices]
            )
        self._cell_clipboard = block
        self._sync_edit_actions()
        self.statusBar().showMessage(f"Copied {counted(len(block), 'cell')}.")
        return True

    def _cut_cells(self) -> None:
        if self._copy_cells():
            self._clear_cells("cut cells")

    def _clear_cells(self, text: str = "clear cells") -> None:
        """Blank the selected cells — index 0, no attributes.

        A tilemap has a fixed extent, so clearing is writing the empty cell
        rather than removing anything: there is no shorter map to leave behind.
        """
        doc = self._doc
        indices = self._selected_cells()
        if doc is None or doc.cells is None or not indices:
            return
        cells = list(doc.cells)
        for index in indices:
            cells[index] = Cell()
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
        indices = self._selected_cells()
        start = indices[0] if indices else 0
        width = self._cells_per_row()
        x0, y0 = start % width, start // width
        cells = list(doc.cells)
        written = 0
        for dy in range(block.height):
            for dx in range(block.width):
                x, y = x0 + dx, y0 + dy
                at = y * width + x
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

        Refused outright where the cells are not what is on screen
        (:attr:`~celpix.core.document.Document.cells_editable`): a stamp layout's
        name panel cells, and a sprite object's are parts placed at pixel
        offsets. Both would have to decide what a canvas gesture meant before
        they could apply one.
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

        Said out loud rather than left as a dead control, because *why* differs
        between the two documents this catches and neither reason is guessable
        from the map on screen (:attr:`~celpix.core.document.Document.cells_editable`).
        """
        doc = self._doc
        if doc is None or not doc.is_tilemap or doc.cells_editable:
            return False
        self.statusBar().showMessage(
            "A sprite object is view-only - edit the tiles it draws from."
            if doc.is_sprite
            else "A stamp layout is view-only - edit the panel it draws from."
        )
        return True

    def _set_cells(self, entry, cells: list[Cell], revision: int) -> None:  # noqa: ANN001
        """Land a cell list on ``entry`` — the command's apply, both directions.

        ``revision`` stamps the data pathway, so the entry reads dirty against
        what was last written and an undo back to the saved state reads clean
        again. A tilemap's cells are its own data, which is the same pathway a
        pixel entry's bytes use (:func:`~celpix.pipeline.pipeline.save`).
        """
        if entry.doc is not None:
            entry.doc.cells = list(cells)
        self._workspace.set_pixel_revision(entry, revision)
        if entry is self._workspace.current:
            self._refresh_view()
