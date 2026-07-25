"""The rearrange tool: dragging tiles into a readable order without moving bytes.

Tiles are rarely stored in the order they are drawn in — a face's eyes, mouth and
hair can sit hundreds of tiles apart because the game streams them by animation
frame. This tool lets the user drag them together so the picture reads, while the
file is left exactly as it was: the rearrangement is a permutation of *display*
positions (:class:`~celpix.core.tilemap.TileMap`) held in ``ViewOptions``, and an
edit made at a rearranged position still writes back to the tile's real home.

That last part costs nothing here. Every read of tiles already goes through
:meth:`~celpix.ui.main_window.selection.SelectionMixin._decode_run` and every
write through ``_apply_tile_edit``, and those two resolve the map — so painting,
the clipboard and the transforms all act on the tiles the user sees without
knowing this module exists. What is left for the controller is the gesture, the
preview, and one undo step per drop.

The tool is armed **over** tile mode and is **exclusive with pixel mode**: both
want the same drag, so arming either puts the other away rather than leaving which
one a press belongs to to be guessed. The canvas treats it as a modal flag checked
ahead of the tile/pixel split, joining the pan and the eyedropper
(``set_rearranging``).

While it is armed the **right** drag selects tiles and the context menu stays shut
— the left button is picking tiles up, so that is the only button left to pick out
the block a drag then carries as one. Selection is **Rectangle-only** there, since
a linear run is a run through *storage*: the tiles it holds need not be adjacent on
screen, so there is no shape for a drag to lift or land.

A drag swaps: the carried tile (or block of tiles) exchanges display positions
with what it lands on. A drop whose destination overlaps its own source is
**refused** — the pairwise exchanges would no longer be independent, and the
result would be a rotation nobody asked for rather than a swap.

Tiles can also be **flipped**, which is the other half of reading scattered art:
hardware mirrors tiles rather than storing both halves of a symmetric sprite. Per
tile (H/V, or the toolbar's Rearrange pair), or per **block** (Shift+H/V), which
mirrors the block as one picture exactly as the destructive Block group does —
flipping every tile *and* permuting their positions — except that both halves are
stored in the map and no byte moves.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QImage

from celpix.core.draw import extract_region
from celpix.core.tilemap import (
    TILE_FLIP_NONE,
    TileMap,
    apply_flip,
)
from celpix.ui import render_bridge
from celpix.ui.main_window.transform import OP_FLIP_H, OP_FLIP_V, TransformOp
from celpix.ui.tools import EditMode
from celpix.ui.undo_commands import TileMapCommand
from celpix.ui.widgets import signals_blocked


@dataclass(frozen=True)
class RearrangeDrag:
    """A rearrange gesture in flight.

    ``cells`` are the canvas cells being carried and ``grab`` the one the cursor
    took hold of, so the drop can be pinned by the same cell the user pressed —
    a block picked up by its corner lands by that corner. ``offset`` places the
    float relative to the cursor's cell so it keeps the grip it was picked up
    with.

    ``source`` is the composed window grid captured at press, kept so pressing
    H or V mid-drag can re-render the float without decoding again — and without
    reading back through the preview map, which by then is showing the *result*
    of the drag rather than what is being carried. ``flip`` is the pending flip,
    committed with the drop.
    """

    grab: tuple[int, int]
    cells: tuple[tuple[int, int], ...]
    offset: tuple[int, int]  # image pixels from the hovered cell's top-left
    source: object  # the window grid at press — an IndexGrid or ArgbGrid
    flip: int = TILE_FLIP_NONE


class RearrangeMixin:
    """The rearrange tool, a slice of :class:`~celpix.ui.main_window.window.MainWindow`.

    Owns the live :class:`~celpix.core.tilemap.TileMap`, the toolbar's two
    checkable actions (arm the tool; show the rearranged order or the file's), and
    the drag. See the module docstring for the semantics.
    """

    # -- state -------------------------------------------------------------
    def _init_rearrange(self) -> None:
        """Seed the rearrange state. Called from the window's constructor."""
        self._tile_map = TileMap()
        self._show_rearranged = True
        self._rearranging = False
        self._rearrange_drag: RearrangeDrag | None = None
        # The cell the drag is hovering, so an H/V mid-drag can re-preview at the
        # same place rather than needing a mouse move to catch up.
        self._rearrange_hover = (0, 0)
        # The map the *preview* renders through while a drag hovers a legal
        # drop: the committed map with the pending swap already in it, so what
        # is on screen during the drag is exactly what releasing would leave.
        self._rearrange_preview: TileMap | None = None

    def _rearrange_available(self) -> bool:
        """Whether tiles can be rearranged at all under the current pattern.

        **Not under the 2D wide-bitmap walk.** There the file is read as one
        bitmap ``columns`` tiles across, so a tile's pixel-rows are strided a
        whole bitmap-row apart and interleave with its neighbours': no tile owns
        a contiguous byte range, and any write widens to the whole stripe those
        tiles share. Two rearranged tiles landing in one stripe would then each
        rewrite it, and the second write would carry through the first's
        pre-edit bytes and quietly undo it.

        That is fixable, but it buys a corner nobody asked for at the cost of
        making every write path reason about stripe overlap. The rearrangement is
        for reading scattered *tiles* as a picture; a 2D pattern is already one
        picture, so it has far less to gain. So the tool switches off and the map
        goes inert, rather than the writes growing a special case.
        """
        return self._doc is not None and not self._two_d.isChecked()

    def _active_tile_map(self) -> TileMap:
        """The rearrangement in force for reading and writing tiles.

        Identity while the view is showing the file's true order, so the toggle
        does not merely hide the rearrangement — it takes it out of the edit path
        too, and an edit made with it off lands where the file says. Identity is
        also the fast path through the decode/encode choke points, and what a 2D
        pattern always gets (see :meth:`_rearrange_available`) — the map is kept,
        not discarded, so leaving 2D brings the rearrangement back.
        """
        if not self._rearrange_available() or not self._show_rearranged:
            return TileMap()
        return self._rearrange_preview or self._tile_map

    # -- toolbar -----------------------------------------------------------
    def _build_rearrange_actions(self, bar) -> None:  # noqa: ANN001 — a QToolBar
        """The two rearrange actions, beside the edit-mode toggle at the bar's end.

        They belong together: one arms the tool that *makes* a rearrangement, the
        other says whether the view is showing it. Both are whole-surface
        switches like Pixel Mode, which is why they sit past the spacer rather
        than among the transform groups.
        """
        self._rearrange_action = QAction("Rearrange", self)
        self._rearrange_action.setCheckable(True)
        self._rearrange_action.setToolTip(
            "Drag tiles to new display positions; the file is not changed\n"
            "Right-drag selects tiles while the tool is armed"
        )
        self._rearrange_action.toggled.connect(self._set_rearranging)
        bar.addAction(self._rearrange_action)
        self._show_rearranged_action = QAction("Rearranged View", self)
        self._show_rearranged_action.setCheckable(True)
        self._show_rearranged_action.setChecked(True)
        self._show_rearranged_action.setToolTip(
            "Show tiles where they were rearranged, or in the file's own order"
        )
        self._show_rearranged_action.toggled.connect(self._set_show_rearranged)
        bar.addAction(self._show_rearranged_action)

    def _set_rearranging(self, on: bool) -> None:
        """Arm/disarm the tool, which is a **tile-mode** tool.

        Arming leaves pixel mode: the two are exclusive because they want the same
        drag, and which one a press belongs to would otherwise be a guess. That
        also sets down any floating pixel selection — the float and the dragged
        tile are the same overlay, and a tile moved out from under pixels still in
        the air would leave them hovering over a tile they were never lifted from.

        It forces the rearranged view on as well — the tool has nothing to say
        about the file's own order, and silently editing a map nobody can see is
        worse than switching the view for them — and Rectangle selection with it,
        since a linear run is a run through storage and has no shape a drag could
        carry (see ``SelectionMixin._sync_selection_shape``).
        """
        if self._rearranging == on:
            return
        if on:
            # Reads _rearranging, still False here, so the disarm inside is a
            # no-op and the two mode switches can't bounce off each other.
            self._set_edit_mode(EditMode.TILE)
            if not self._show_rearranged:
                self._show_rearranged_action.setChecked(True)
        self._rearranging = on
        self._cancel_rearrange_drag()
        self._canvas.set_rearranging(on)
        self._sync_selection_shape()
        # The bar swaps to the Rearrange group, whose flips are display state
        # rather than pixel edits — so the destructive ones can't be reached by
        # muscle memory while a rearrangement is what the user is making.
        self._sync_transform_bar_mode()
        self._sync_rearrange_actions()

    def _set_show_rearranged(self, on: bool) -> None:
        """Switch between the rearranged order and the file's own."""
        if self._show_rearranged == on:
            return
        self._show_rearranged = on
        if not on:
            # Nothing to drag when the positions on screen are the file's.
            self._rearrange_action.setChecked(False)
        self._sync_rearrange_actions()
        if self._doc is not None:
            self._refresh_view()

    def _sync_rearrange_actions(self) -> None:
        """Converge the two actions with the state they drive.

        Also the one place the 2D lockout lands: switching a view to a 2D pattern
        disarms the tool and greys both actions, saying why. Called from
        ``_refresh_view``, so it follows the pattern picker without the
        arrangement toolbar needing to know this module exists.
        """
        available = self._rearrange_available()
        if self._rearranging and not available:
            # Re-enters here once with _rearranging already False, so it settles.
            self._set_rearranging(False)
        for action, checked in (
            (self._rearrange_action, self._rearranging),
            (self._show_rearranged_action, self._show_rearranged),
        ):
            if action.isChecked() != checked:
                with signals_blocked(action):
                    action.setChecked(checked)
            action.setEnabled(available)
        blocked = self._doc is not None and not available
        self._rearrange_action.setToolTip(
            "Rearranging is not available for a 2D pattern - a tile's bytes "
            "interleave with its neighbours' there"
            if blocked
            else "Drag tiles to new display positions; the file is not changed"
        )
        # The flip buttons need something to act on: the carried tiles mid-drag,
        # else the selection. The block pair additionally needs a 2D block,
        # exactly as the destructive Block group does.
        can_flip = available and (
            self._rearrange_drag is not None or bool(self._selection_tiles())
        )
        for action in self._rearrange_group:
            action.setEnabled(can_flip)
        for action in self._rearrange_block_group:
            action.setEnabled(available and self._block_geometry() is not None)

    def _set_tile_map(self, tile_map: TileMap) -> None:
        """Land a rearrangement — :class:`TileMapCommand`'s apply, and the
        restore path. Rebuilds the view, since it changes what every slot shows."""
        self._tile_map = tile_map
        if self._doc is not None:
            self._refresh_view()

    # -- the drag ----------------------------------------------------------
    def _on_rearrange_started(self, slot: int) -> None:
        """Lift the pressed tile — or the whole selection it sits in."""
        if self._doc is None:
            return
        self._cancel_rearrange_drag()
        layout = self._view_layout()
        grab = layout.slot_to_cell(slot)
        cells = self._carried_cells(grab)
        source = self._window_grid()
        if not cells or source is None:
            return
        # The float keeps the grip it was picked up with: its top-left sits this
        # far from the hovered cell's, so a block dragged by its middle doesn't
        # jump to align its corner with the cursor.
        tw, th = self._pixel_tile_size()
        left = min(cx for cx, _ in cells)
        top = min(cy for _, cy in cells)
        self._rearrange_drag = RearrangeDrag(
            grab, cells, ((left - grab[0]) * tw, (top - grab[1]) * th), source
        )
        self._show_rearrange_drag(grab)

    def _on_rearrange_moved(self, slot: int) -> None:
        if self._rearrange_drag is not None:
            self._show_rearrange_drag(self._view_layout().slot_to_cell(slot))

    def _on_rearrange_dropped(self, slot: int) -> None:
        """Commit the move and any pending flip under the cursor, as one step.

        A drop that resolves to nothing — refused, or back where it started with
        no flip pending — just ends the gesture: pushing an empty step would make
        Ctrl+Z walk through drags that did nothing. A drop that *only* flips is a
        real step though, which is what makes "pick it up, press H, put it back"
        the natural way to mirror a tile in place.
        """
        drag = self._rearrange_drag
        entry = self._workspace.current
        if drag is None or self._doc is None or entry is None:
            return
        over = self._view_layout().slot_to_cell(slot)
        moves = self._drop_moves(drag, over)
        new_map = self._dragged_map(drag, moves)
        self._cancel_rearrange_drag()
        if new_map is None:
            return
        new_map = new_map.bounded(self._doc.tile_count)
        if new_map == self._tile_map:
            return
        label = self._drop_label(drag, moves)
        self._push_command(TileMapCommand(self, entry, label, self._tile_map, new_map))
        if moves:
            self._select_dropped(drag, over)

    def _select_dropped(self, drag: RearrangeDrag, over: tuple[int, int]) -> None:
        """Take the selection along to where the drag put the tiles down.

        The block the user is working with is the one they just moved, and leaving
        the highlight on the cells it came from would offer them the *tiles it
        swapped with* for the next gesture — a Copy or a second drag would quietly
        act on the wrong thing. Only a drop that moved something comes here, so a
        flip in place leaves the selection alone.

        The destination is the source rectangle shifted by the drag, which is the
        selection the tool always makes (Rectangle is forced while it is armed).
        A destination outside the window has no cells to select, so the selection
        is left where it is rather than pointed at nothing.
        """
        dx, dy = over[0] - drag.grab[0], over[1] - drag.grab[1]
        cells = [(cx + dx, cy + dy) for cx, cy in drag.cells]
        x0, y0 = min(c[0] for c in cells), min(c[1] for c in cells)
        cols = max(c[0] for c in cells) - x0 + 1
        rows = max(c[1] for c in cells) - y0 + 1
        origin = self._view_layout().cell_to_slot(x0, y0)
        if origin is None:
            return
        tiles = self._rect_tiles_for(origin, cols, rows)
        if tiles:
            self._set_rect_selection((cols, rows), tiles)

    def _dragged_map(self, drag: RearrangeDrag, moves: list) -> TileMap | None:
        """The map a drop would leave — ``None`` when the gesture resolves to nothing.

        Shared by the drop and the live preview, so what is rendered mid-drag is
        the very map the release commits rather than a second rendering of the
        same intent. The flip lands on the carried tiles' **own** indices, which
        is why the order against the swap doesn't matter: a swap moves positions,
        never tiles.
        """
        if not moves and not drag.flip:
            return None
        tile_map = self._tile_map.swap_many(moves) if moves else self._tile_map
        if drag.flip:
            carried = self._carried_tiles(drag)
            tile_map = tile_map.flip(carried, drag.flip)
        return tile_map

    def _carried_tiles(self, drag: RearrangeDrag) -> list[int]:
        """The **actual** tile indices under the carried cells, for flipping."""
        layout = self._view_layout()
        shown = (self._cell_tile(layout, *cell) for cell in drag.cells)
        return [self._tile_map.actual(v) for v in shown if v is not None]

    @staticmethod
    def _drop_label(drag: RearrangeDrag, moves: list) -> str:
        count = len(moves) or len(drag.cells)
        what = "tile" if count == 1 else f"{count} tiles"
        if not moves:
            return f"flip {what}"
        return f"rearrange {what}" + (" and flip" if drag.flip else "")

    def _on_rearrange_cancelled(self) -> None:
        self._cancel_rearrange_drag()

    def _rearrange_key(self, key, shift: bool, ctrl: bool) -> bool:  # noqa: ANN001
        """Escape abandons a drag in flight; routed from the nav event filter.

        Claimed ahead of the other Escape handlers: whatever they would otherwise
        do, they cannot put the carried tile back down.

        ``H``/``V`` flip, and read the same either side of a drag — the tile in
        the air while one is in flight, the selection otherwise — so the keys and
        the toolbar's Rearrange pair never mean different things. **Shift** makes
        them flip the *block*, mirroring the Tile/Block pairing the transform bar
        already teaches.
        """
        if ctrl or not self._rearranging:
            return False
        if key == Qt.Key.Key_Escape:
            if shift or self._rearrange_drag is None:
                return False
            self._cancel_rearrange_drag()
            return True
        op = {Qt.Key.Key_H: OP_FLIP_H, Qt.Key.Key_V: OP_FLIP_V}.get(key)
        if op is None:
            return False
        if shift:
            self._flip_rearranged_block(op)
        else:
            self._flip_rearranged(op.tile_flip)
        return True

    def _flip_rearranged_block(self, op: TransformOp) -> None:
        """Mirror the block as one picture, entirely as display state.

        The same thing the destructive Block group does — flip every tile *and*
        permute their positions within the block — except that neither half
        touches a byte: the positions move in the tile map's permutation, the
        mirroring in its flip flags. So the picture reads the same on screen while
        the file keeps the tiles exactly where and how it had them.

        The block comes from the same :meth:`_block_geometry` the destructive
        group uses, so a lone selected tile expands to its arrangement block and
        one click turns a whole metatile.

        Order doesn't matter between the two halves: the permutation shuffles
        tiles *among* the block's positions, so the set of tiles in the block —
        which is what carries the flips — is the same before and after.
        """
        if self._doc is None or not self._rearrange_available() or not op.tile_flip:
            return
        entry = self._workspace.current
        geom = self._block_geometry()
        if entry is None or geom is None:
            return
        cols, rows, x0, y0 = geom
        layout = self._view_layout()
        sources: dict[int, int] = {}
        for dy in range(rows):
            for dx in range(cols):
                dest = self._cell_tile(layout, x0 + dx, y0 + dy)
                sx, sy = op.cell_src(dx, dy, cols, rows)
                src = self._cell_tile(layout, x0 + sx, y0 + sy)
                if dest is None or src is None:
                    return  # a gap or off-window cell: the block isn't all there
                sources[dest] = src
        if not sources:
            return
        flipped = [self._tile_map.actual(v) for v in sources]
        new_map = (
            self._tile_map.rearranged(sources)
            .flip(flipped, op.tile_flip)
            .bounded(self._doc.tile_count)
        )
        if new_map == self._tile_map:
            return
        self._push_command(
            TileMapCommand(self, entry, "flip block", self._tile_map, new_map)
        )

    def _flip_rearranged_tiles(self, op: TransformOp) -> None:
        """Toolbar entry point for the per-tile display flip (the keys pass flags)."""
        self._flip_rearranged(op.tile_flip)

    def _flip_rearranged(self, flags: int) -> None:
        """Toggle a display flip on whatever the tool is currently pointed at.

        The one entry point behind both the toolbar buttons and the H/V keys, so
        the two can never drift apart. Mid-drag it retargets to the carried tiles
        and rides along to the drop as part of that single step; otherwise it is
        its own undoable step over the selection.
        """
        if self._doc is None or not self._rearrange_available():
            return
        drag = self._rearrange_drag
        if drag is not None:
            self._rearrange_drag = replace(drag, flip=drag.flip ^ flags)
            self._show_rearrange_drag(self._rearrange_hover)
            return
        entry = self._workspace.current
        tiles = [self._tile_map.actual(t) for t in self._selection_tiles()]
        if entry is None or not tiles:
            return
        new_map = self._tile_map.flip(tiles, flags).bounded(self._doc.tile_count)
        if new_map == self._tile_map:
            return
        what = "tile" if len(tiles) == 1 else f"{len(tiles)} tiles"
        self._push_command(
            TileMapCommand(self, entry, f"flip {what}", self._tile_map, new_map)
        )

    def _cancel_rearrange_drag(self) -> None:
        """Drop the gesture and put the view back the way it was.

        Safe to call with nothing in flight, so every path that ends a drag —
        release, Esc, disarming the tool, a right-click — can just call it.
        """
        if self._rearrange_drag is None and self._rearrange_preview is None:
            return
        self._rearrange_drag = None
        self._rearrange_preview = None
        self._canvas.set_float(None)
        self._canvas.set_drop_target(None)
        if self._doc is not None:
            self._refresh_view()

    def _show_rearrange_drag(self, over: tuple[int, int]) -> None:
        """Preview the drop on cell ``over``: swapped tiles, float, drop target.

        The preview is the map the release would leave, rendered through the
        ordinary view path — so what the user is looking at mid-drag *is* the
        result, not an impression of it. A refused drop previews nothing and
        marks the target red.
        """
        drag = self._rearrange_drag
        if drag is None or self._doc is None:
            return
        moves = self._drop_moves(drag, over)
        layout = self._view_layout()
        self._rearrange_preview = self._dragged_map(drag, moves)
        self._refresh_view()
        targets = [
            slot
            for cx, cy in drag.cells
            if (
                slot := layout.cell_to_slot(
                    cx + over[0] - drag.grab[0], cy + over[1] - drag.grab[1]
                )
            )
            is not None
        ]
        self._canvas.set_drop_target(targets, valid=bool(moves))
        tw, th = self._pixel_tile_size()
        self._canvas.set_float(
            self._carried_image(drag),
            over[0] * tw + drag.offset[0],
            over[1] * th + drag.offset[1],
        )
        self._rearrange_hover = over

    # -- what a gesture resolves to ----------------------------------------
    def _carried_cells(self, grab: tuple[int, int]) -> tuple[tuple[int, int], ...]:
        """The cells a press on ``grab`` picks up.

        The whole selection when the press lands inside a multi-tile one — the
        block was picked out precisely so it could be moved as a block — and the
        single cell otherwise. Cells whose tile is past the end of the file are
        dropped: there is nothing there to rearrange.
        """
        layout = self._view_layout()
        grabbed = self._cell_tile(layout, *grab)
        if grabbed is None:
            return ()
        selected = set(self._selection_tiles())
        if len(selected) < 2 or grabbed not in selected:
            return (grab,)
        cells = tuple(
            cell
            for cell in self._window_cells()
            if self._cell_tile(layout, *cell) in selected
        )
        return cells or (grab,)

    def _window_cells(self) -> list[tuple[int, int]]:
        """Every cell of the visible window, row by row."""
        return [
            (cx, cy)
            for cy in range(self._rows.value())
            for cx in range(self._columns.value())
        ]

    def _drop_moves(
        self, drag: RearrangeDrag, over: tuple[int, int]
    ) -> list[tuple[int, int]]:
        """The ``(from, to)`` virtual-index swaps dropping on ``over`` would make.

        Cells pair up **rigidly on screen** — each carried cell swaps with the one
        the same distance from the drop as it was from the grab — so a block
        stays the shape it was picked up as, whatever arrangement is placing the
        tiles underneath.

        Empty when the drop cannot be made: it goes nowhere, it leaves the window
        or the file, or its destinations overlap its own sources. That last one is
        the refusal the module docstring explains — the swaps would stop being
        independent, and the result would be a rotation rather than the exchange
        the gesture promises.
        """
        dx, dy = over[0] - drag.grab[0], over[1] - drag.grab[1]
        if (dx, dy) == (0, 0):
            return []
        layout = self._view_layout()
        moves = []
        for cx, cy in drag.cells:
            source = self._cell_tile(layout, cx, cy)
            target = self._cell_tile(layout, cx + dx, cy + dy)
            if source is None or target is None:
                return []
            moves.append((source, target))
        touched = [index for move in moves for index in move]
        if len(set(touched)) != len(touched):
            return []
        return moves

    # -- the floating image ------------------------------------------------
    def _carried_image(self, drag: RearrangeDrag) -> QImage | None:
        """Render the carried tiles as one image, blank where nothing is carried.

        Cut out of the window grid captured at press rather than decoded tile by
        tile: the window was already decoded to draw the canvas, and the float
        has to match what was on screen anyway. A ragged selection — a linear run
        that wraps a row — leaves the cells it doesn't cover blank, so what floats
        is the shape that will actually land.

        A pending flip is applied **per tile, in place**, not by mirroring the
        whole float: that is what will be stored, since a flip belongs to a tile
        rather than to the group carrying it. Mirroring the block would also
        permute the tiles' positions, which is the Block transform's job and not
        this gesture's — so the float would be promising something the drop
        wouldn't deliver.
        """
        assert self._doc is not None
        grid = drag.source
        tw, th = self._pixel_tile_size()
        cells = drag.cells
        left, top = min(c[0] for c in cells), min(c[1] for c in cells)
        right, bottom = max(c[0] for c in cells), max(c[1] for c in cells)
        out = type(grid)((right - left + 1) * tw, (bottom - top + 1) * th)
        for cx, cy in cells:
            tile = extract_region(grid, cx * tw, cy * th, tw, th)
            tile = apply_flip(tile, drag.flip)
            for y in range(th):
                for x in range(tw):
                    out.set((cx - left) * tw + x, (cy - top) * th + y, tile.get(x, y))
        base = self._subpalette.value() * self._index_space()
        return render_bridge.render(out, self._doc.palette, base)
