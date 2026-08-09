"""The stamp tool: lay tiles into a tilemap's cells by pointing at them.

The **placing** gesture (`docs/design/tilemap-entry.md` §8, "Edit Tiles"), and the
third of three ways a cell is pointed somewhere: the binding bar's Cell spin sets
a reference by number over the selection, the tile source panel chooses one by
looking, and this puts a chosen tile into the cell under the cursor.

Edit Tiles is a **modal tool over tile mode**, the shape the rearrange tool
already has and for the same reason: it wants both mouse buttons, so it cannot
share the canvas with the selection drag. While armed, a left press lays the
panel's picked tile into the cell under the cursor and a left drag keeps laying
it — a pencil over cells — while a right press picks the tile a cell already
names *and the palette row it is drawn in*, which is the eyedropper. It is
offered only on a tilemap, because only a tilemap has cells that name tiles
(`Capability.STAMP`).

**A stroke is one undoable step.** A drag across forty cells is one gesture and
has to undo as one, so the drag is previewed on the live document and committed
through :meth:`~...tilemap_edit.TilemapEditMixin._apply_cells` on release, with
the cells as they stood at the press restored underneath it first. That is the
pixel pen's arrangement (paint into a working copy, commit the stroke) at cell
scale.

**What a stamp writes is what the pick carried.** An eyedropped tile brings the
whole cell it was taken off — palette row, flips, priority, the format's
uninterpreted `flags` — because the gesture is "put *that* one here", and a copy
that kept only the tile number lays down a cell the user can see is the wrong
colour. A tile picked in the **tile source sheet** has no such record behind it:
a sheet holds tiles, and a tile has no palette row, so only the index lands and
the target keeps the attributes it had, exactly as the Cell spin does. One rule,
two pickers, and the difference is what each of them knows
(:meth:`StampToolMixin._stamp_cell`). On a chained map the referrer's attributes
are moot either way — they come from the source cell (§3.1).
"""

from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QKeySequence

from celpix.core.capabilities import Capability
from celpix.core.tilemap import Cell
from celpix.pipeline import pipeline
from celpix.ui.tools import EditMode
from celpix.ui.widgets import counted, signals_blocked

STAMP_TIP = (
    "Lay the picked tile into a cell (T)\n"
    "Left click or drag stamps; right click picks a tile\n"
    "A picked cell carries its palette row and flips, and\n"
    "stamps them back; a tile picked in the panel sets the\n"
    "tile alone\n"
    "Pick from the Tile Source panel"
)
# Why the tool is off where it looks like it should apply. A format whose cells
# have no index field has nothing for a stamp to set - the same answer that hides
# the Cell spin, said in the place the user is reaching for.
STAMP_BLOCKED_TIP = (
    "This format's cells hold no tile reference (T)\nNothing for a stamp to place"
)


class StampToolMixin:
    """The Edit Tiles mode: its two actions, its arming, and its gestures.

    A slice of :class:`~celpix.ui.main_window.window.MainWindow`, not a
    standalone object. See the module docstring for what the tool is and for why
    it is modal over tile mode rather than an edit mode of its own.
    """

    # -- state & construction ----------------------------------------------
    def _init_stamp(self) -> None:
        """Seed stamp-tool state; called from the window's ``__init__`` before
        the transform toolbar, which builds the tool's button."""
        self._stamping = False
        # The stroke in progress: the cells as they stood at the press (what the
        # committed step undoes to), the working list being painted, and which
        # positions it has already reached so a drag re-crossing one is free.
        self._stamp_before: list | None = None
        self._stamp_cells: list | None = None
        self._stamp_touched: set[int] = set()

    def _build_stamp_actions(self, bar) -> None:  # noqa: ANN001 — a QToolBar
        """The tool's two actions: the toolbar button and the Edit menu row.

        Two actions over one state for the reason the rearrange tool has two
        (see ``rearrange.py``): a bar button needs Qt's checkable flag to latch,
        and the menu row must not have it, sitting among plain mode-toggle rows.
        Both drive :meth:`_set_stamping` and :meth:`_sync_stamp_actions`
        converges them, so they cannot disagree.

        ``T`` is set as a shortcut for the label it puts in the menu and the F1
        guide, but with a widget context so it never fires: the bare letter is
        routed by the app-wide event filter (``_handle_nav_key``), which yields
        to focused text inputs — the treatment every other bare letter gets.
        """
        self._stamp_action = QAction("Toggle Edit Tiles Mode", self)
        self._stamp_action.setIconText("Edit Tiles")
        self._stamp_action.setCheckable(True)
        self._stamp_action.setToolTip(STAMP_TIP)
        self._stamp_action.toggled.connect(self._set_stamping)
        bar.addAction(self._stamp_action)
        # No mnemonic: "T" is Cut's in this menu, and the rearrange rows it sits
        # with carry none either, so the row reads as one of that group.
        self._toggle_stamp_action = QAction("Toggle Edit Tiles Mode", self)
        self._toggle_stamp_action.setShortcut(QKeySequence("T"))
        self._toggle_stamp_action.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
        self._toggle_stamp_action.setToolTip(STAMP_TIP)
        self._toggle_stamp_action.triggered.connect(self._toggle_stamping)
        self._toggle_stamp_action.setEnabled(False)  # nothing open yet

    def _connect_stamp_canvas(self) -> None:
        """Wire the canvas's stamp gestures (called once the canvas exists)."""
        self._canvas.stamp_pressed.connect(self._on_stamp_pressed)
        self._canvas.stamp_moved.connect(self._on_stamp_moved)
        self._canvas.stamp_finished.connect(self._on_stamp_finished)

    # -- arming --------------------------------------------------------------
    def _stamp_available(self) -> bool:
        """Whether there is anything here to stamp into.

        The Cell spin's own question, asked by pointing instead of by typing, so
        it is literally the same predicate
        (:meth:`~...tilemap_edit.TilemapEditMixin._cell_reference_settable`) —
        the two gestures set one field, and one being offered while the other was
        refused would describe two different documents.
        """
        return self._cell_reference_settable()

    def _toggle_stamping(self) -> None:
        """The ``T`` key. Goes through the action so the key and the button can
        only ever do the same thing — including staying inert while it is off."""
        if self._stamp_action.isEnabled():
            self._stamp_action.toggle()

    def _set_stamping(self, on: bool) -> None:
        """Arm/disarm the tool, which is a **tile-mode** tool.

        Arming leaves pixel mode and puts the rearrange tool down. All three want
        the same drag, so which one a press belongs to would otherwise be a
        guess; in practice neither of the other two is reachable on a tilemap,
        and the disarm is here so that stays a fact about the capability table
        rather than a thing this method relies on.

        Any tile selection goes with it. The tool acts on the cell under the
        cursor and never on the selection, so a highlight left behind would
        advertise a target the next click ignores.
        """
        if self._stamping == on:
            return
        if on:
            # Both read their own flag, still False/TILE here, so the disarms
            # inside cannot bounce back off this one.
            self._set_edit_mode(EditMode.TILE)
            self._set_rearranging(False)
        self._stamping = on
        self._canvas.set_stamping(on)
        if on:
            self._clear_selection()
        self._sync_stamp_actions()

    def _sync_stamp_actions(self) -> None:
        """Converge the two actions with the state they drive.

        Also where the tool disarms itself when the document moves out from under
        it — a codec swapped for one with no index field, or an entry switch onto
        a sprite object. Called from ``_refresh_view``, so it follows both without
        either needing to know this module exists.
        """
        available = self._stamp_available()
        if self._stamping and not available:
            # Re-enters here once with _stamping already False, so it settles.
            self._set_stamping(False)
        if self._stamp_action.isChecked() != self._stamping:
            with signals_blocked(self._stamp_action):
                self._stamp_action.setChecked(self._stamping)
        self._stamp_action.setEnabled(available)
        self._toggle_stamp_action.setEnabled(available)
        # The blocked tip only applies where the tool would otherwise be offered:
        # off a tilemap entirely, the actions are hidden and say nothing.
        blocked = (
            self._doc is not None and self._can(Capability.STAMP) and not available
        )
        tip = STAMP_BLOCKED_TIP if blocked else STAMP_TIP
        self._stamp_action.setToolTip(tip)
        self._toggle_stamp_action.setToolTip(tip)

    # -- the gestures --------------------------------------------------------
    def _stamp_cell_at(self, slot: int) -> int | None:
        """Which cell of :attr:`Document.cells` the canvas slot ``slot`` draws.

        Two steps, and both are needed. The canvas places in **tile** slots, so a
        2x2 metatile covers four of them and the division is what finds the cell;
        an assembled screen file then draws its pages side by side, so the drawn
        position is not the file's own and ``cell_at`` maps it back
        (``docs/design/tilemap-entry.md`` §6).
        """
        doc = self._doc
        if doc is None or doc.cells is None:
            return None
        at = doc.cell_at(slot // doc.tiles_per_cell)
        return at if 0 <= at < len(doc.cells) else None

    def _held_tile_id(self) -> int | None:
        """The tile a stamp would place, or ``None`` with nothing usable held.

        Validated against the IDs this map can actually reach rather than trusted
        from the panel, because the panel only composes while its tab is showing
        — so the pick has to survive being made against one document and used
        against another. :func:`~celpix.pipeline.pipeline.tile_source_span`
        answers that without composing anything.

        The **span**, not the sheet's own narrower run: an ID picked off a cell
        by the eyedropper is whatever that cell holds, and a map is free to hold
        one that starts a unit halfway through another. Refusing to place a tile
        the map already draws would be the panel's layout overruling the file.
        """
        doc = self._doc
        held = self._source_tile_id
        if doc is None or held is None:
            return None
        return (
            held
            if held in pipeline.tile_source_span(doc, self._cell_index_limit())
            else None
        )

    def _on_stamp_pressed(self, slot: int, button) -> None:  # noqa: ANN001 — Qt button
        if button == Qt.MouseButton.RightButton:
            self._pick_tile_at(slot)
            return
        held = self._held_tile_id()
        if held is None:
            self._refuse_stamp()
            return
        doc = self._doc
        assert doc is not None and doc.cells is not None
        self._stamp_before = list(doc.cells)
        self._stamp_cells = list(doc.cells)
        self._stamp_touched = set()
        self._stamp_into(slot, held)

    def _on_stamp_moved(self, slot: int) -> None:
        held = self._held_tile_id()
        if held is not None and self._stamp_cells is not None:
            self._stamp_into(slot, held)

    def _on_stamp_finished(self) -> None:
        """Commit the stroke as one step, or drop it if it changed nothing.

        The document is carrying the painted cells at this point, so the cells as
        they stood at the press go back first: ``_apply_cells`` reads the *live*
        list as the step's before, and handed the painted one it would push a
        command that undoes to itself.
        """
        before, cells = self._stamp_before, self._stamp_cells
        self._stamp_before = self._stamp_cells = None
        touched, self._stamp_touched = self._stamp_touched, set()
        doc = self._doc
        if doc is None or before is None or cells is None:
            return
        doc.cells = before
        doc.resolve()
        if not self._apply_cells(cells, "stamp tiles"):
            # Nothing landed - a drag that only ever crossed cells already
            # naming the held tile. Put the picture back, since the preview
            # below has been drawing the painted list.
            self._refresh_view()
            return
        self.statusBar().showMessage(f"Stamped {counted(len(touched), 'cell')}.")

    def _stamp_into(self, slot: int, tile_id: int) -> None:
        """Point the cell under ``slot`` at ``tile_id`` and show it immediately.

        Previewed on the live document rather than pushed per cell: a drag is one
        gesture and undoes as one (:meth:`_on_stamp_finished`), but it still has
        to be *visible* as it is made, and the map's own render is the only thing
        that can show a cell drawing a different tile.
        """
        doc, cells = self._doc, self._stamp_cells
        if doc is None or cells is None:
            return
        at = self._stamp_cell_at(slot)
        if at is None or at in self._stamp_touched:
            return
        self._stamp_touched.add(at)
        landing = self._stamp_cell(tile_id, cells[at])
        if cells[at] == landing:
            return  # already this exactly; nothing to draw and nothing to undo
        cells[at] = landing
        doc.cells = list(cells)
        doc.resolve()
        self._refresh_view()

    def _stamp_cell(self, tile_id: int, over: Cell) -> Cell:
        """The record a stamp lays into ``over`` — **what the pick carried**.

        An **eyedropped** tile brings its whole cell: the palette row, the flips,
        the priority and the uninterpreted ``flags`` the format round-trips. The
        gesture is "put *that* one here", and a copy that kept only the number
        would put down a cell the user can see is the wrong colour or facing the
        wrong way — the attributes are as much what they pointed at as the tile
        is. Everything the codec reads travels, so a new field a format grows is
        carried without this method learning its name.

        A tile picked in the **sheet** has no such record behind it — the sheet
        holds tiles, and a tile has no palette row — so only the index lands and
        the target keeps its own attributes.

        The guard is against a **stale** record: the held ID is re-validated
        against the map on every stamp (:meth:`_held_tile_id`) and a rebind can
        move it, so the cell rides along only while it still describes the tile
        actually being placed.

        ``visible`` is forced either way, because stamping **makes the position
        drawn**. A cell the layout leaves blank paints the background, so placing
        a tile there would otherwise write the entry and show nothing — no
        feedback, and on a layout that is largely undrawn (`-CLR-.MAP` is
        entirely so) every click a silent no-op. It is also what the authoring
        tool does: `scr_map_cnv` sets the drawn byte on every block it registers
        (``scgcad-formats.md`` §4).
        """
        held = self._source_cell
        if held is not None and held.index == tile_id:
            return replace(held, visible=True)
        return replace(over, index=tile_id, visible=True)

    def _pick_tile_at(self, slot: int) -> None:
        """The eyedropper: take the **whole cell** under ``slot``.

        Three things leave with it, and they are three different mechanisms.

        The **tile number** is the cell's own index as the file stores it, before
        the binding's base tile — the same number the tile source panel is
        addressed in, the Cell spin holds and Show Tile IDs writes over the cell,
        so picking here and looking there cannot disagree.

        The **record** rides beside it, held for the next stamp to lay down whole
        (:meth:`_stamp_cell`). Nothing on screen reads it; it is what makes the
        gesture a copy of the cell rather than of its tile number.

        The **row goes into Subpal**, which is the *displayed* half and separate
        from the row the stamp writes (:meth:`_pick_palette_row_at`). A pick is
        the tool's way of saying "this one", and the same thing said by a
        left-click selection in tile mode moves the row everywhere it is read.
        """
        doc = self._doc
        at = self._stamp_cell_at(slot)
        if doc is None or doc.cells is None or at is None:
            return
        index = doc.cells[at].index
        self._set_source_tile(index, doc.cells[at])
        row = self._pick_palette_row_at(slot)
        picked = f"Picked tile ${index:X}"
        self.statusBar().showMessage(
            f"{picked}." if row is None else f"{picked}, palette row {row}."
        )

    def _pick_palette_row_at(self, slot: int) -> int | None:
        """Take the picked cell's palette row into Subpal; the row, or None.

        The row **shown**, not the row stamped: what a stamp writes travels in the
        held record (:meth:`_stamp_cell`) and needs no control to carry it. Subpal
        is where the row is read from by everything a selection would have moved
        — the palette grid's outlined block, the colours the tile sheet is drawn
        in, and the row Set Selection's Palette Row writes into cells
        (:meth:`~...rendering.RenderingMixin._sync_subpalette`). The tool clears
        the selection and acts on the cell under the cursor instead, so none of
        that follows a pick unless the row lands here, and the user is left
        looking at the tile they picked in whichever row Subpal was on.

        The **drawn** row at the **drawn** position, which is the pair
        :meth:`~...palette_regions.PaletteRegionsMixin._selection_palette_row`
        answers with for the same reason: a chained map's own cells are
        coordinates whose row field is a 0 nobody chose, and the row that reaches
        the screen is the source cell's.

        Nothing to take where the format gives a cell no row — Subpal there is
        the *view's* row, which the render obeys, so writing a cell's into it
        would recolour the map on a gesture that is supposed to sample it.
        """
        doc = self._doc
        if doc is None or not doc.cells_carry_palette_rows:
            return None
        cells = doc.laid_out_cells
        at = slot // doc.tiles_per_cell
        if not 0 <= at < len(cells):
            return None
        row = self._drawn_palette_row(cells[at].palette_row)
        self._subpalette.setValue(row)
        return row

    def _refuse_stamp(self) -> None:
        """Say why a click laid nothing down, rather than doing nothing silently.

        Reachable with the tool armed and no tile held — on a fresh session, or
        after a rebind moved the ID run out from under the pick — which is
        exactly when the user has no way to guess what is wrong.
        """
        self.statusBar().showMessage(
            "No tile to stamp - pick one in the Tile Source panel first."
        )
