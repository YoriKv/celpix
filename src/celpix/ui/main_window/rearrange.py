"""The rearrange tool: dragging tiles into a readable order without moving bytes.

Tiles are rarely stored in the order they are drawn in — a face's eyes, mouth and
hair can sit hundreds of tiles apart because the game streams them by animation
frame. This tool lets the user drag them together so the picture reads, while the
file is left exactly as it was: the rearrangement is a permutation of *display*
positions (:class:`~celpix.core.tilerearrangement.TileRearrangement`) held in
``ViewOptions``, and an edit made at a rearranged position still writes back to
the tile's real home.

That last part costs nothing here. Every read of tiles already goes through
:meth:`~celpix.ui.main_window.selection.SelectionMixin._decode_run` and every
write through ``_apply_tile_edit``, and those two resolve it — so painting,
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

Tiles can also be **mirrored and turned**, which is the other half of reading
scattered art: hardware mirrors tiles rather than storing both halves of a
symmetric sprite, and art lifted from one context often sits at ninety degrees to
the one it is being read in. Per tile (H/V/C/X, or the toolbar's group), or per
**block** (Shift + the same letters), which transforms the block as one picture
exactly as the destructive Block group does — orienting every tile *and* permuting
their positions — except that both halves are stored in the rearrangement and no
byte moves.
Those keys are the transform bar's, not this module's: while the tool is armed its
group *is* the group on the bar, so the letters mean here what they mean anywhere
(:meth:`~celpix.ui.main_window.transform.TransformMixin._transform_key`).

A turn swaps a tile's width and height, so the two rotations need **square tiles**
— the same rule the destructive groups follow, and the block group additionally
needs a square block.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QImage, QKeySequence

from celpix.core.capabilities import Capability
from celpix.core.draw import blit_region, extract_region
from celpix.core.tilerearrangement import (
    TILE_ORIENT_NONE,
    TILE_TRANSPOSE,
    TileRearrangement,
    apply_orientation,
    compose_orientation,
)
from celpix.ui import render_bridge
from celpix.ui.main_window.transform import TransformOp
from celpix.ui.tools import EditMode
from celpix.ui.undo_commands import TileRearrangementCommand
from celpix.ui.widgets import signals_blocked

# The tool's own tooltip, re-set by :meth:`RearrangeMixin._sync_rearrange_actions`
# when something locks the tool out, so all three readings live side by side. The
# two lockouts say different things because the user can act on one of them: a 2D
# pattern is a setting to leave, a tilemap is the document they opened.
REARRANGE_TIP = (
    "Drag tiles to new display positions (R)\nRight-drag selects; no bytes move"
)
REARRANGE_BLOCKED_TIP = (
    "Not available for a 2D pattern (R)\nA tile's bytes interleave with its neighbours'"
)
REARRANGE_TILEMAP_TIP = (
    "Not available for a tilemap (R)\nMoving a cell is an edit, not a display order"
)


@dataclass(frozen=True)
class RearrangeDrag:
    """A rearrange gesture in flight.

    ``cells`` are the canvas cells being carried and ``grab`` the one the cursor
    took hold of, so the drop can be pinned by the same cell the user pressed —
    a block picked up by its corner lands by that corner. ``offset`` places the
    float relative to the cursor's cell so it keeps the grip it was picked up
    with.

    ``source`` is the composed window grid captured at press, kept so a transform
    key mid-drag can re-render the float without decoding again — and without
    reading back through the preview map, which by then is showing the *result*
    of the drag rather than what is being carried. ``orient`` is the pending
    display orientation, committed with the drop.
    """

    grab: tuple[int, int]
    cells: tuple[tuple[int, int], ...]
    offset: tuple[int, int]  # image pixels from the hovered cell's top-left
    source: object  # the window grid at press — an IndexGrid or ArgbGrid
    orient: int = TILE_ORIENT_NONE


class RearrangeMixin:
    """The rearrange tool, a slice of :class:`~celpix.ui.main_window.window.MainWindow`.

    Owns the live :class:`~celpix.core.tilerearrangement.TileRearrangement`, the
    toolbar's two
    checkable actions (arm the tool; show the rearranged order or the file's), and
    the drag. See the module docstring for the semantics.
    """

    # -- state -------------------------------------------------------------
    def _init_rearrange(self) -> None:
        """Seed the rearrange state. Called from the window's constructor."""
        self._tile_rearrangement = TileRearrangement()
        self._show_rearranged = True
        self._rearranging = False
        self._rearrange_drag: RearrangeDrag | None = None
        # The cell the drag is hovering, so a transform key mid-drag can re-preview
        # at the same place rather than needing a mouse move to catch up.
        self._rearrange_hover = (0, 0)
        # The map the *preview* renders through while a drag hovers a legal
        # drop: the committed map with the pending swap already in it, so what
        # is on screen during the drag is exactly what releasing would leave.
        self._rearrange_preview: TileRearrangement | None = None

    def _rearrange_available(self) -> bool:
        """Whether tiles can be rearranged at all on the entry as it is being read.

        **Not on a tilemap.** A rearrangement is display state precisely because
        it moves no bytes — but a tilemap's bytes *are* the arrangement, so moving
        a cell is the edit itself and the tool would be a second, invisible order
        laid over the one the file already states
        (:data:`~celpix.core.capabilities.CAPABILITIES`). The table gates the three
        controls; this is the same answer read from the other end, so the state
        goes with them rather than the buttons greying over a live tool.

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
        picture, so it has far less to gain. So the tool switches off and the
        rearrangement goes inert, rather than the writes growing a special case.

        Either way the entry's stored rearrangement is **kept, not discarded**, so
        leaving 2D — or coming back to a pixel entry — brings it back.
        """
        return (
            self._doc is not None
            and self._can(Capability.TILE_REARRANGE)
            and not self._two_d.isChecked()
        )

    def _showing_rearranged(self) -> bool:
        """Whether the view is on the rearranged order rather than the file's.

        The setting, **or** the tool being armed. Dragging tiles around a view
        that is showing the file's own order would be editing a rearrangement
        nobody can see, so the tool overrides the setting while it is armed rather
        than rewriting it — turn the tool off and the view goes back to whatever
        the user asked for.
        """
        return self._show_rearranged or self._rearranging

    def _active_tile_rearrangement(self) -> TileRearrangement:
        """The rearrangement in force for reading and writing tiles.

        Identity while the view is showing the file's true order, so the toggle
        does not merely hide the rearrangement — it takes it out of the edit path
        too, and an edit made with it off lands where the file says. Identity is
        also the fast path through the decode/encode choke points, and what a 2D
        pattern always gets (see :meth:`_rearrange_available`) — it is kept, not
        discarded, so leaving 2D brings the rearrangement back.
        """
        if not self._rearrange_available() or not self._showing_rearranged():
            return TileRearrangement()
        return self._rearrange_preview or self._tile_rearrangement

    # -- toolbar -----------------------------------------------------------
    def _build_rearrange_actions(self, bar) -> None:  # noqa: ANN001 — a QToolBar
        """The three rearrange actions: the tool button on ``bar``, the tool's
        Edit menu row, and the view toggle (menu only).

        Only the tool is a switch you reach for mid-gesture, so only it earns a
        place beside Pixel Mode past the bar's spacer. Whether the view shows a
        rearrangement is a setting you make once — and the tool overrides it
        while armed anyway (:meth:`_showing_rearranged`) — so it lives on the
        menu, which is also where the F1 guide reads it from.

        The tool takes **two** actions over the one state because a bar button
        and a menu row want opposite things from Qt's checkable flag: the button
        needs it (a latched button is how an armed modal tool says it is armed),
        while the menu row must not have it — it sits with Toggle Selection Mode
        and Toggle Edit Mode, which are plain rows, and a lone checkbox among
        three mode switches reads as a different kind of thing from its
        neighbours. Both drive the same :meth:`_set_rearranging`, and
        :meth:`_sync_rearrange_actions` converges them, so they cannot disagree.

        ``R``/``Shift+R`` are set as shortcuts for the label they put in the menu
        and the F1 guide, but with a widget context so they never fire: the bare
        letters are routed by the app-wide event filter (``_handle_nav_key``),
        which yields to focused text inputs — the same treatment View ▸ Grid gets.
        The key is on the menu row, since the guide reads the menu bar.
        """
        # The bar button carries the short label: QToolButton takes its text from
        # iconText, and the full "Toggle Rearrange Mode" would stretch the bar.
        self._rearrange_action = QAction("Toggle Rearrange Mode", self)
        self._rearrange_action.setIconText("Rearrange Mode")
        self._rearrange_action.setCheckable(True)
        self._rearrange_action.setToolTip(REARRANGE_TIP)
        self._rearrange_action.toggled.connect(self._set_rearranging)
        bar.addAction(self._rearrange_action)
        self._toggle_rearrange_action = QAction("Toggle Rearrange Mode", self)
        self._toggle_rearrange_action.setShortcut(QKeySequence("R"))
        self._toggle_rearrange_action.setShortcutContext(
            Qt.ShortcutContext.WidgetShortcut
        )
        self._toggle_rearrange_action.setToolTip(REARRANGE_TIP)
        self._toggle_rearrange_action.triggered.connect(self._toggle_rearranging)
        self._toggle_rearrange_action.setEnabled(False)  # nothing open yet
        self._show_rearranged_action = QAction("Show Rearranged Tiles", self)
        self._show_rearranged_action.setCheckable(True)
        self._show_rearranged_action.setChecked(True)
        self._show_rearranged_action.setShortcut(QKeySequence("Shift+R"))
        self._show_rearranged_action.setShortcutContext(
            Qt.ShortcutContext.WidgetShortcut
        )
        self._show_rearranged_action.setToolTip(
            "Show the rearranged order, or the file's own (Shift+R)\n"
            "Forced on while the Rearrange tool is armed"
        )
        self._show_rearranged_action.toggled.connect(self._set_show_rearranged)

    def _toggle_rearranging(self) -> None:
        """The ``R`` key. Goes through the action so the key and the button can
        only ever do the same thing — including staying inert while it is off."""
        if self._rearrange_action.isEnabled():
            self._rearrange_action.toggle()

    def _toggle_show_rearranged(self) -> None:
        """``Shift+R`` — the view toggle's key, via its action as ``R`` is."""
        if self._show_rearranged_action.isEnabled():
            self._show_rearranged_action.toggle()

    def _set_rearranging(self, on: bool) -> None:
        """Arm/disarm the tool, which is a **tile-mode** tool.

        Arming leaves pixel mode: the two are exclusive because they want the same
        drag, and which one a press belongs to would otherwise be a guess. That
        also sets down any floating pixel selection — the float and the dragged
        tile are the same overlay, and a tile moved out from under pixels still in
        the air would leave them hovering over a tile they were never lifted from.

        The view follows for as long as the tool is armed - the tool has nothing
        to say about the file's own order, and silently editing a rearrangement
        nobody can see is worse than showing it - but that is an override
        (:meth:`_showing_rearranged`), not a write: Show Rearranged Tiles is left
        exactly where the user set it, and disarming honours it again. Rectangle
        selection does change, since a linear run is a run through storage and
        has no shape a drag could carry
        (see ``SelectionMixin._sync_selection_shape``).
        """
        if self._rearranging == on:
            return
        if on:
            # Reads _rearranging, still False here, so the disarm inside is a
            # no-op and the two mode switches can't bounce off each other.
            self._set_edit_mode(EditMode.TILE)
            self._set_stamping(False)
        self._rearranging = on
        self._cancel_rearrange_drag()
        self._canvas.set_rearranging(on)
        self._sync_selection_shape()
        # The bar swaps to the Rearrange groups, whose transforms are display
        # state rather than pixel edits — so the destructive ones can't be reached
        # by muscle memory while a rearrangement is what the user is making.
        self._sync_transform_bar_mode()
        self._sync_rearrange_actions()

    def _set_show_rearranged(self, on: bool) -> None:
        """Switch between the rearranged order and the file's own.

        Turning it off while the tool is armed doesn't disarm it: the tool
        overrides the setting anyway, so the view stays rearranged and the choice
        takes effect when the tool is put down.
        """
        if self._show_rearranged == on:
            return
        self._show_rearranged = on
        self._sync_rearrange_actions()
        if self._doc is not None:
            self._refresh_view()

    def _sync_rearrange_actions(self) -> None:
        """Converge the three actions with the state they drive.

        Also the one place the lockout lands: a 2D pattern or a tilemap entry
        disarms the tool and greys every one of them, saying which it is. Called
        from ``_refresh_view`` and from entry activation, so it follows both the
        pattern picker and the file list without either needing to know this
        module exists.
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
        # The tool's menu row holds no state of its own - it only ever needs to
        # be as reachable, and say as much, as the button it stands in for.
        self._toggle_rearrange_action.setEnabled(available)
        tip = REARRANGE_TIP
        if self._doc is not None and not available:
            # Which lockout it is, since they are answered in different places:
            # the pattern picker, or by opening a different entry.
            tip = (
                REARRANGE_BLOCKED_TIP
                if self._can(Capability.TILE_REARRANGE)
                else REARRANGE_TILEMAP_TIP
            )
        self._rearrange_action.setToolTip(tip)
        self._toggle_rearrange_action.setToolTip(tip)
        # The transform buttons need something to act on: the carried tiles
        # mid-drag, else the selection. The block group additionally needs a 2D
        # block, exactly as the destructive Block group does — and both groups'
        # rotations need a square tile, since a turn swaps the tile's dimensions
        # (:meth:`TransformMixin._square_tiles`).
        has_target = available and (
            self._rearrange_drag is not None or bool(self._selection_tiles())
        )
        square_tiles = self._square_tiles()
        geom = self._block_geometry() if available else None
        square_block = geom is not None and geom[0] == geom[1]
        self._arm_transform_group(self._rearrange_group, has_target, square_tiles)
        self._arm_transform_group(
            self._rearrange_block_group, geom is not None, square_block and square_tiles
        )

    def _set_tile_rearrangement(self, tile_rearrangement: TileRearrangement) -> None:
        """Land a rearrangement — :class:`TileRearrangementCommand`'s apply, and the
        restore path. Rebuilds the view, since it changes what every slot shows."""
        self._tile_rearrangement = tile_rearrangement
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
        """Commit the move and any pending orientation under the cursor, as one step.

        A drop that resolves to nothing — refused, or back where it started with
        nothing pending — just ends the gesture: pushing an empty step would make
        Ctrl+Z walk through drags that did nothing. A drop that *only* orients is a
        real step though, which is what makes "pick it up, press H, put it back"
        the natural way to mirror or turn a tile in place.
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
        if new_map == self._tile_rearrangement:
            return
        label = self._drop_label(drag, moves)
        self._push_command(
            TileRearrangementCommand(
                self, entry, label, self._tile_rearrangement, new_map
            )
        )
        if moves:
            self._select_dropped(drag, over)

    def _select_dropped(self, drag: RearrangeDrag, over: tuple[int, int]) -> None:
        """Take the selection along to where the drag put the tiles down.

        The block the user is working with is the one they just moved, and leaving
        the highlight on the cells it came from would offer them the *tiles it
        swapped with* for the next gesture — a Copy or a second drag would quietly
        act on the wrong thing. Only a drop that moved something comes here, so an
        orientation applied in place leaves the selection alone.

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

    def _dragged_map(
        self, drag: RearrangeDrag, moves: list
    ) -> TileRearrangement | None:
        """The map a drop would leave — ``None`` when the gesture resolves to nothing.

        Shared by the drop and the live preview, so what is rendered mid-drag is
        the very map the release commits rather than a second rendering of the
        same intent. The orientation lands on the carried tiles' **own** indices,
        which is why the order against the swap doesn't matter: a swap moves
        positions, never tiles.
        """
        if not moves and not drag.orient:
            return None
        tile_rearrangement = (
            self._tile_rearrangement.swap_many(moves)
            if moves
            else self._tile_rearrangement
        )
        if drag.orient:
            carried = self._carried_tiles(drag)
            tile_rearrangement = tile_rearrangement.oriented(carried, drag.orient)
        return tile_rearrangement

    def _carried_tiles(self, drag: RearrangeDrag) -> list[int]:
        """The **actual** tile indices under the carried cells, for orienting."""
        layout = self._view_layout()
        shown = (self._cell_tile(layout, *cell) for cell in drag.cells)
        return [self._tile_rearrangement.actual(v) for v in shown if v is not None]

    @staticmethod
    def _orient_verb(orient: int) -> str:
        """An undo label's verb for ``orient`` — the two ``TransformOp.verb`` uses.

        The pending orientation is a composition, not the button that made it, so
        the label reads off the result: anything that swaps the tile's axes reads as
        a rotation, and the rest as mirrors. Deliberately the same two words the ops
        carry, so "rotate tile" and "rotate block" don't describe the same gesture
        two ways.
        """
        return "rotate" if orient & TILE_TRANSPOSE else "flip"

    @staticmethod
    def _orient_object(count: int) -> str:
        """What an undo label says the gesture acted on: "tile", "7 tiles".

        Bare in the singular rather than "1 tile", because the label reads as a
        sentence in the Edit menu — "Undo rotate tile". That is why it is not
        :func:`~celpix.ui.widgets.counted`, which is for status-line counts and
        always shows the number.
        """
        return "tile" if count == 1 else f"{count} tiles"

    @classmethod
    def _drop_label(cls, drag: RearrangeDrag, moves: list) -> str:
        what = cls._orient_object(len(moves) or len(drag.cells))
        verb = cls._orient_verb(drag.orient)
        if not moves:
            return f"{verb} {what}"
        return f"rearrange {what}" + (f" and {verb}" if drag.orient else "")

    def _on_rearrange_cancelled(self) -> None:
        self._cancel_rearrange_drag()

    def _rearrange_key(self, key, shift: bool, ctrl: bool) -> bool:  # noqa: ANN001
        """Escape abandons a drag in flight; routed from the nav event filter.

        Claimed ahead of the other Escape handlers: whatever they would otherwise
        do, they cannot put the carried tile back down.

        The transform keys are not here. While the tool is armed its groups *are*
        the groups on the transform bar, so they are that bar's keys like every
        other flip and rotate (``TransformMixin._transform_key``) — which is what
        keeps H/V/C/X from meaning one thing here and another a mode away.
        """
        if ctrl or shift or not self._rearranging:
            return False
        if key != Qt.Key.Key_Escape or self._rearrange_drag is None:
            return False
        self._cancel_rearrange_drag()
        return True

    def _orient_rearranged_block(self, op: TransformOp) -> None:
        """Flip or turn the block as one picture, entirely as display state.

        The same thing the destructive Block group does — transform every tile
        *and* permute their positions within the block — except that neither half
        touches a byte: the positions move in the rearrangement's permutation, the
        tiles' own transform in its orientation flags. So the picture reads the
        same on screen while the file keeps the tiles exactly where and how it had
        them.

        The block comes from the same :meth:`_block_geometry` the destructive
        group uses, so a lone selected tile expands to its arrangement block and
        one click turns a whole metatile.

        Order doesn't matter between the two halves: the permutation shuffles
        tiles *among* the block's positions, so the set of tiles in the block —
        which is what carries the orientations — is the same before and after.
        """
        if self._doc is None or not self._rearrange_available():
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
        turned = [self._tile_rearrangement.actual(v) for v in sources]
        new_map = (
            self._tile_rearrangement.rearranged(sources)
            .oriented(turned, op.tile_orient)
            .bounded(self._doc.tile_count)
        )
        if new_map == self._tile_rearrangement:
            return
        self._push_command(
            TileRearrangementCommand(
                self, entry, f"{op.verb} block", self._tile_rearrangement, new_map
            )
        )

    def _orient_rearranged_tiles(self, op: TransformOp) -> None:
        """The per-tile display transform as the transform bar calls it — by op
        rather than by flag, the shape every button in that bar is wired with."""
        self._orient_rearranged(op.tile_orient)

    def _orient_rearranged(self, flags: int) -> None:
        """Compose a display orientation onto whatever the tool is pointed at.

        Mid-drag it retargets to the carried tiles and rides along to the drop as
        part of that single step — the only route to orienting what is in the air,
        since the buttons otherwise act on the selection; otherwise it is its own
        undoable step over the selection.
        """
        if self._doc is None or not self._rearrange_available():
            return
        drag = self._rearrange_drag
        if drag is not None:
            pending = compose_orientation(flags, drag.orient)
            self._rearrange_drag = replace(drag, orient=pending)
            self._show_rearrange_drag(self._rearrange_hover)
            return
        entry = self._workspace.current
        tiles = [self._tile_rearrangement.actual(t) for t in self._selection_tiles()]
        if entry is None or not tiles:
            return
        new_map = self._tile_rearrangement.oriented(tiles, flags).bounded(
            self._doc.tile_count
        )
        if new_map == self._tile_rearrangement:
            return
        label = f"{self._orient_verb(flags)} {self._orient_object(len(tiles))}"
        self._push_command(
            TileRearrangementCommand(
                self, entry, label, self._tile_rearrangement, new_map
            )
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
        """Preview the drop on cell ``over``: moved tiles, float, drop target.

        The preview is the rearrangement the release would leave, rendered through
        the
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
            for cy in range(self._view_rows())
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

        A pending orientation is applied **per tile, in place**, not to the whole
        float: that is what will be stored, since an orientation belongs to a tile
        rather than to the group carrying it. Transforming the block would also
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
            tile = apply_orientation(tile, drag.orient)
            blit_region(out, tile, (cx - left) * tw, (cy - top) * th)
        return render_bridge.render(out, self._doc.palette, self._palette_base())
