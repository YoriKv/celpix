"""The stamp tool: lay tiles into a tilemap's cells by pointing at them.

The **placing** gesture (`docs/design/tilemap-entry.md` §8, "Edit Tiles"), and the
third of three ways a cell is pointed somewhere: the binding bar's Cell spin sets
a reference by number over the selection, the tile source panel chooses one by
looking, and this puts a chosen tile into the cell under the cursor.

Edit Tiles is a **modal tool over tile mode**, the shape the rearrange tool
already has and for the same reason: it wants both mouse buttons, so it cannot
share the canvas with the selection drag. While armed, a left press lays the
held tiles into the cell under the cursor and a left drag keeps laying them — a
pencil over cells — while the right button is the eyedropper: a click picks the
tile a cell already names *and the palette row it is drawn in*, and a **drag**
picks a whole rectangle of cells that the next left press lays down as one
block, its top-left cell landing under the cursor. On a **stamped chain** the
gesture's unit is the whole stamp: the sweep grows out to the stamp lattice and
the brush holds one entry per stamp (:meth:`StampToolMixin._on_stamp_area_picked`).
It is offered only on a
tilemap, because only a tilemap has cells that name tiles (`Capability.STAMP`).

**What is held is previewed on the canvas.** The held tiles are rendered in the
colours they would land in and shown over the hovered cell — the pixel pen's
one-pixel preview at cell scale (:meth:`StampToolMixin._sync_stamp_preview`,
:meth:`~celpix.ui.canvas.Canvas.set_stamp_preview`).

**A stroke is one undoable step.** A drag across forty cells is one gesture and
has to undo as one, so the drag is previewed on the live document and committed
through :meth:`~...tilemap_edit.TilemapEditMixin._apply_cells` on release, with
the cells as they stood at the press restored underneath it first. That is the
pixel pen's arrangement (paint into a working copy, commit the stroke) at cell
scale.

**What a stamp writes is what the pick carried, in the colours on show.** An
eyedropped cell — and every cell of a picked area — brings its whole record:
flips, priority, the format's uninterpreted `flags`, because the gesture is
"put *that* one here" and a copy that kept only the tile number lays down a cell
the user can see is facing the wrong way. A tile picked in the **tile source
sheet** has no such record behind it — a sheet holds tiles — so only the index
lands and the target keeps its other attributes, exactly as the Cell spin does;
a rectangle right-dragged there (:class:`~celpix.ui.tile_source_panel.
TileSourcePanel`) is the same pick widened, one bare index per square, and each
lands on the same terms.
The **palette row** is the one field that follows neither pick: it is Subpal's,
the row the tile source sheet and the canvas preview are both drawn in, so what
lands is always the colours being shown — a pick sets Subpal to the picked row,
which is what keeps "put that one here" true, and moving Subpal afterwards
recolours the next stamp along with its preview
(:meth:`StampToolMixin._settle_stamp_row`). On a chained map the referrer's
attributes are moot either way — they come from the source cell (§3.1).
"""

from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QImage, QKeySequence

from celpix.core.capabilities import Capability
from celpix.core.errors import PipelineError
from celpix.core.tilemap import BLANK, Cell, CellGrid, expand_stamp
from celpix.pipeline import pipeline
from celpix.ui import render_bridge
from celpix.ui.tools import EditMode
from celpix.ui.widgets import counted, signals_blocked

STAMP_TIP = (
    "Draw tiles/stamps with left click. "
    "Select a tile or drag select a stamp with right click (T)"
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
        # drawn positions the pointer has already anchored a stamp on, so a drag
        # re-crossing one is free.
        self._stamp_before: list | None = None
        self._stamp_cells: list | None = None
        self._stamp_touched: set[int] = set()
        # The rectangle a right drag picked — the brush a left press lays down,
        # its top-left cell landing under the cursor. Whole cell records when
        # swept off the canvas, bare index records when swept off the tile
        # source sheet (told apart by `_source_cell`, which only a canvas pick
        # carries). None while the held stamp is the single picked tile; any
        # single pick drops it
        # (:meth:`~...tile_source_dock.TileSourceDockMixin._set_source_tile`).
        self._stamp_brush: CellGrid | None = None
        # The property row's settings for what a sheet pick lays — concrete
        # per-field values, the format's defaults where never touched
        # (:meth:`~...cell_props_bar.CellPropsMixin._stamp_attr_value`), landed
        # whole so a sheet pick and a canvas brush agree about what a press
        # writes. Session state like Subpal: no undo step, it outlives sheet
        # picks so "set flip H once, paint many tiles" is one gesture per tile,
        # and a pick that displaces a held record takes that record's values
        # over (:meth:`~...cell_props_bar.CellPropsMixin._seed_stamp_attrs`).
        # `visible` rides here too but stamp-wide — False is the eraser,
        # applied whatever is held (:meth:`_apply_stamp_eraser`).
        self._stamp_attrs: dict[str, bool | int] = {}
        # The cell the pointer is over while the tool is armed, as a drawn
        # position — the base record the ghost's landing computation reads
        # (:meth:`_stamp_target_at`). None off the canvas.
        self._stamp_hover_anchor: int | None = None

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
        self._canvas.stamp_area_picked.connect(self._on_stamp_area_picked)
        self._canvas.stamp_hovered.connect(self._on_stamp_hovered)

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
        """The Edit ▸ row's slot: press the bar's button, if it is offering itself.

        The row and the button are two spellings of one switch, so the row acts
        through the button rather than setting the state itself. ``T`` presses
        that button directly (:class:`~...navigation.KeyControl`)."""
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
        else:
            self._stamp_hover_anchor = None
        self._sync_stamp_actions()
        # The property row retargets with the mode — held stamp or selection —
        # and neither the selection pass nor the refresh runs on an arm alone.
        self._sync_cell_props()
        # So does the transform bar; its sync inside the row's covers only the
        # armed side, and a disarm has to hand the groups back to the (empty)
        # selection rather than leave them armed for a stamp no longer in play.
        self._sync_transform_actions()

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
        # The preview follows everything this pass follows — arming, the entry,
        # the format — plus the render inputs the refresh brings it here for: a
        # palette edit or a Subpal move recolours what the held tiles would land
        # as, and this pass is on the refresh path.
        self._sync_stamp_preview()

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
        from the tile source panel, which only composes while its tab is showing
        — so the pick has to survive being made against one document and used
        against another. :func:`~celpix.pipeline.pipeline.tile_source_span`
        answers that without composing anything.

        The **span**, not the sheet's own narrower run: an ID picked off a cell
        by the eyedropper is whatever that cell holds, and a map is free to hold
        one that starts a unit halfway through another. Refusing to place a tile
        the map already draws would be the tile source panel's layout overruling
        the file.
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
        self._stamp_touched = set()
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
        # The cells that actually changed, not the positions crossed: a brush
        # writes several per anchor and a re-cross writes none, so the anchor
        # count is the wrong number in both directions.
        changed = sum(1 for was, now in zip(before, cells, strict=True) if was != now)
        self.statusBar().showMessage(f"Stamped {counted(changed, 'cell')}.")

    def _stamp_into(self, slot: int, tile_id: int) -> None:
        """Lay the held stamp with its top-left cell under ``slot``, and show it.

        Previewed on the live document rather than pushed per cell: a drag is one
        gesture and undoes as one (:meth:`_on_stamp_finished`), but it still has
        to be *visible* as it is made, and the map's own render is the only thing
        that can show a cell drawing a different tile.

        The anchor is a **drawn** position and the brush extends over drawn
        neighbours, clipped at the row's end rather than wrapped: laying a
        rectangle is :meth:`~...tilemap_edit.TilemapEditMixin._paste_cells`'
        geometry, and each landing cell is resolved through
        :meth:`~celpix.core.document.Document.cell_at` for the reason a paste's
        is — on an assembled map the drawn position is not the file's own.

        On a **stamped chain** the brush holds one entry per stamp
        (:meth:`_on_stamp_area_picked`), so the neighbours step by the stamp —
        each landing position falls inside a different stamp and ``cell_at``
        snaps it to that stamp's entry, which is exactly what a single click on
        the same position would have re-pointed.
        """
        doc, cells = self._doc, self._stamp_cells
        if doc is None or cells is None:
            return
        anchor = slot // doc.tiles_per_cell
        if anchor in self._stamp_touched:
            return
        self._stamp_touched.add(anchor)
        brush = self._stamp_brush
        width = self._cells_per_row()
        unit_w, unit_h = doc.stamp_cells
        x0, y0 = anchor % width, anchor // width
        laying = (
            [(0, 0, None)]
            if brush is None or not len(brush)
            else [
                (dx, dy, brush.get(dx, dy))
                for dy in range(brush.height)
                for dx in range(brush.width)
            ]
        )
        changed = False
        for dx, dy, record in laying:
            x = x0 + dx * unit_w
            if x >= width:
                continue
            at = doc.cell_at((y0 + dy * unit_h) * width + x)
            if not 0 <= at < len(cells):
                continue
            if record is None:
                landing = self._stamp_cell(tile_id, cells[at])
            elif self._source_cell is None:
                # A brush swept off the tile source sheet - the one area pick
                # with no record behind it (`_source_cell` is only ever held by
                # a canvas pick). The sheet holds tiles, not cells, so each
                # square lands as a single sheet pick does: the index, with the
                # property row's settings (:meth:`_stamp_cell` with nothing
                # held).
                landing = self._stamp_cell(record.index, cells[at])
            else:
                landing = self._settle_stamp_row(
                    self._apply_stamp_eraser(replace(record, visible=True)),
                    cells[at],
                )
            if cells[at] != landing:
                cells[at] = landing
                changed = True
        if not changed:
            return  # already this exactly; nothing to draw and nothing to undo
        doc.cells = list(cells)
        doc.resolve()
        self._refresh_view()

    def _stamp_cell(self, tile_id: int, over: Cell) -> Cell:
        """The record a stamp lays into ``over`` — **what the pick carried**.

        An **eyedropped** tile brings its whole cell: the flips, the priority and
        the uninterpreted ``flags`` the format round-trips. The gesture is "put
        *that* one here", and a copy that kept only the number would put down a
        cell the user can see is facing the wrong way — the attributes are as
        much what they pointed at as the tile is. Everything the codec reads
        travels, so a new field a format grows is carried without this method
        learning its name.

        A tile picked in the **sheet** has no such record behind it — the sheet
        holds tiles — so the property row's **settings** are the record: the
        index lands with every declared attribute set concretely
        (:meth:`~...cell_props_bar.CellPropsMixin._stamp_sheet_attrs`), exactly
        as a canvas-swept brush lays its records, so the two picks cannot
        disagree about what a press writes. The **palette row** follows neither
        pick but the Subpal spin, which is the row every preview of the stamp
        is drawn in (:meth:`_settle_stamp_row`).

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
        (``scgcad-formats.md`` §4). The one thing that outweighs the force is
        the property row's own Drawn box: unchecked, ``visible=False`` lands
        over it and the stamp is the **eraser** — every press lays undrawn
        cells, which is Clear Cells said by pointing.
        """
        held = self._source_cell
        if held is not None and held.index == tile_id:
            laid = replace(held, visible=True)
        else:
            laid = replace(over, index=tile_id, visible=True)
            laid = replace(laid, **self._stamp_sheet_attrs())
        return self._settle_stamp_row(self._apply_stamp_eraser(laid), over)

    def _apply_stamp_eraser(self, laid: Cell) -> Cell:
        """``laid`` undrawn, wherever the Drawn box armed the eraser.

        The one row setting that is **stamp-wide**: the eraser must erase with
        whatever is held — a record and a canvas-swept brush lay their own
        attributes otherwise — so it runs on every landing rather than only
        the sheet pick's. Gated on the format declaring the bit
        (:meth:`~...tilemap_edit.TilemapEditMixin._cell_fields`) rather than
        trusted from the row: the row prunes itself on every sync, but a codec
        can change in the window between, and a bit set here that the format
        cannot store would hide cells the save then silently redraws.
        """
        if (
            self._stamp_attrs.get("visible") is False
            and "visible" in self._cell_fields()
        ):
            return replace(laid, visible=False)
        return laid

    def _settle_stamp_row(self, laid: Cell, over: Cell) -> Cell:
        """``laid`` with the palette row a stamp actually writes into ``over``.

        The row is **Subpal's**, not the record's or the target's, wherever the
        format gives this file's cells a row of their own to hold: Subpal is the
        row the tile source sheet and the canvas preview are drawn in, so it is
        the one number that keeps "what you see is what lands" true. A pick sets
        Subpal to the picked row, which is what makes the eyedrop still mean
        "put *that* one here" — and moving Subpal after the pick recolours the
        stamp along with its previews instead of laying down the colours of a
        preview no longer on screen. Clamped to what the field can hold, the
        rule every cell-row writer follows
        (:meth:`~...tilemap_edit.TilemapEditMixin._assign_cell_palette_row`).

        The **target's row stays** where the document stores one row for several
        cells (an NES nametable's 2x2 quadrant). There the row is not this
        cell's to change: writing it would recolour up to three neighbours the
        user never pointed at, on a gesture whose whole meaning is "put that
        tile *here*". Asked as
        :attr:`~celpix.core.document.Document.has_row_groups` and not as "is the
        granularity coarse", because those are different questions on a format
        that declares a group and states no width to resolve it against — the
        host has no group it can name there and edits rows per cell everywhere
        else.

        And nothing is written where the format's cells hold **no row at all** —
        :meth:`_stamp_row_override` answers ``None`` for both refusals, and a
        chained map lands there too: its own words are coordinates whose row
        field is not a row (§3.1).
        """
        doc = self._doc
        if doc is not None and doc.has_row_groups:
            return replace(laid, palette_row=over.palette_row)
        row = self._stamp_row_override()
        return laid if row is None else replace(laid, palette_row=row)

    def _stamp_row_override(self) -> int | None:
        """The **named** row a stamp writes — Subpal's — or None to leave rows be.

        None on the same terms every cell-row writer refuses: a format whose
        cells state no row limit has no field for the number to land in
        (:meth:`~...tilemap_edit.TilemapEditMixin._cell_palette_row_limit`), and
        a row-group format's row is not one cell's to set
        (:meth:`_settle_stamp_row` keeps the target's there). The value is the
        Subpal row taken back through the base, because a cell stores a *named*
        row and the base is applied again on the way out
        (:meth:`~...palette_regions.PaletteRegionsMixin._named_row_picked`).
        """
        doc = self._doc
        if doc is None or not doc.cells_carry_palette_rows or doc.has_row_groups:
            return None
        limit = self._cell_palette_row_limit()
        if limit is None:
            return None
        return max(0, min(self._named_row_picked(), limit))

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

        A cell pointing **outside the tile source** clears the pick instead —
        the span test every stamp passes through (:meth:`_held_tile_id`), asked
        at pick time. There is no tile there to take: holding the number anyway
        would leave the panel ringing the *previous* pick against a readout and
        a status line describing this one, and every later stamp refusing for a
        reason set several gestures ago. The row stays out of Subpal on the same
        refusal — a sample that found nothing has nothing to recolour the sheet
        with.
        """
        doc = self._doc
        at = self._stamp_cell_at(slot)
        if doc is None or doc.cells is None or at is None:
            return
        index = doc.cells[at].index
        if index not in pipeline.tile_source_span(doc, self._cell_index_limit()):
            self._clear_source_tile()
            self.statusBar().showMessage(
                f"Tile ${index:X} is not in the tile source - nothing picked."
            )
            return
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

    def _on_stamp_area_picked(self, anchor: int, far: int) -> None:
        """A right drag's pick: hold the swept rectangle of cells as the stamp.

        The rectangle in **drawn** positions, resolved to file cells through
        :meth:`~celpix.core.document.Document.cell_at` — the pair every
        rectangle gesture here uses, so an area picked off an assembled screen
        holds the cells actually under it. Records travel whole, as the single
        eyedrop's does, and the top-left cell doubles as an ordinary pick — the
        panel's ring, the readout and Subpal all follow it, so the area pick
        answers every question a single one does plus the shape.

        On a **stamped chain** the placed unit is the whole stamp, so the sweep
        is read in stamps: the rectangle grows out to the stamp lattice — every
        stamp it touches, whole — and the brush holds **one entry per stamp**,
        found at each stamp's corner. Per drawn position it would hold every
        entry once per position it covers, and laying that back would write the
        same stamps again a tile apart. The lattice sits on the resolved grid
        (:meth:`~celpix.core.document.Document.cell_at` snaps to the same one),
        so a sweep and a click cannot disagree about which entry a position is.

        A click never lands here: the canvas reports a drag that stayed inside
        one unit as the single-cell ``stamp_pressed`` it is.
        """
        doc = self._doc
        if doc is None or doc.cells is None:
            return
        width = self._cells_per_row()
        unit_w, unit_h = doc.stamp_cells
        a, b = anchor // doc.tiles_per_cell, far // doc.tiles_per_cell
        x0, x1 = sorted((a % width, b % width))
        y0, y1 = sorted((a // width, b // width))
        # Out to the lattice: the corner floors onto it, and counting the units
        # from there to the far edge is the round *up* — a rectangle that enters
        # a stamp holds all of it.
        x0 -= x0 % unit_w
        y0 -= y0 % unit_h
        lifted = CellGrid((x1 - x0) // unit_w + 1, (y1 - y0) // unit_h + 1)
        for dy in range(lifted.height):
            for dx in range(lifted.width):
                at = doc.cell_at((y0 + dy * unit_h) * width + (x0 + dx * unit_w))
                if 0 <= at < len(doc.cells):
                    lifted.set(dx, dy, doc.cells[at])
        corner = doc.cell_at(y0 * width + x0)
        if not 0 <= corner < len(doc.cells):
            return
        record = doc.cells[corner]
        self._set_source_tile(record.index, record, area=lifted)
        self._pick_palette_row_at((y0 * width + x0) * doc.tiles_per_cell)
        what = "stamps" if doc.is_indirect else "cells"
        self.statusBar().showMessage(
            f"Picked {lifted.width}x{lifted.height} {what} - "
            "left click stamps them from the top-left."
        )

    # -- the preview ---------------------------------------------------------
    def _sync_stamp_preview(self) -> None:
        """Converge the canvas's stamp preview with what a left press would lay.

        Rendered through the same machinery as the map itself —
        :func:`~celpix.pipeline.pipeline.expand_cells` over the landing records,
        then this document's own colour-table rule
        (:meth:`~...rendering.RenderingMixin._tilemap_grid_image`) — so the
        preview and the landing cannot come out looking different; that promise
        is the whole point of it. Cleared with the tool down or nothing held.

        A stroke in progress skips the recompose: nothing the preview depends on
        can change mid-stroke, and this runs on the refresh path the stroke's
        every step re-enters.
        """
        if self._stamp_cells is not None:
            return
        if not self._stamping or self._doc is None:
            self._canvas.set_stamp_preview(None)
            return
        held = self._held_stamp_cells()
        if held is None:
            self._canvas.set_stamp_preview(None)
            return
        cells, columns = held
        try:
            tiles, layout = pipeline.expand_cells(
                self._doc, self._registry, cells, columns, self._doc.stamp_tiles
            )
            grid = pipeline.compose_tiles(tiles, layout, None)
        except (KeyError, PipelineError):
            self._canvas.set_stamp_preview(None)
            return
        image = self._tilemap_grid_image(grid)
        if self._stamp_attrs.get("visible") is False:
            # The eraser: every landing position paints the background, so
            # that is the ghost — the held tiles would promise a picture the
            # press does not make. Same footprint, so the outline still says
            # where the erase lands.
            image = image.convertToFormat(QImage.Format.Format_ARGB32)
            image.fill(render_bridge.HIDDEN_BACKGROUND)
        self._canvas.set_stamp_preview(image)

    def _held_stamp_cells(self) -> tuple[list[Cell], int] | None:
        """The cells a left press would lay, ready to compose, and their width
        in placed units — or None with nothing usable held.

        Built by the **landing computation itself** — each unit through
        :meth:`_stamp_cell` (or a record through :meth:`_settle_stamp_row`)
        against the cell actually under the pointer
        (:meth:`_stamp_target_at`) — so the ghost and the landing cannot come
        out different by construction. The target still matters: a row-group
        format's landing keeps a row only the target knows, and a field the
        format declares outside the row's remit stays the target's own. Off
        the canvas there is no target yet, and a blank one is the closest
        honest answer.
        """
        brush = self._stamp_brush
        if brush is not None and len(brush):
            if self._source_cell is None:
                # A sheet-swept brush lands square by square as sheet picks do —
                # each index with the row's settings — so it previews that way,
                # each square against the cell it would land over.
                units = [
                    self._stamp_cell(brush.get(x, y).index, self._stamp_target_at(x, y))
                    for y in range(brush.height)
                    for x in range(brush.width)
                ]
            else:
                units = [
                    self._settle_stamp_row(
                        self._apply_stamp_eraser(
                            replace(brush.get(x, y), visible=True)
                        ),
                        self._stamp_target_at(x, y),
                    )
                    for y in range(brush.height)
                    for x in range(brush.width)
                ]
            width = brush.width
        else:
            held = self._held_tile_id()
            if held is None:
                return None
            units = [self._stamp_cell(held, self._stamp_target_at(0, 0))]
            width = 1
        return [cell for unit in units for cell in self._stamp_unit_cells(unit)], width

    def _stamp_target_at(self, dx: int, dy: int) -> Cell:
        """The cell ``(dx, dy)`` placed units from the hovered anchor would land
        over — the landing's base record — or blank off the canvas.

        The landing's own geometry (:meth:`_stamp_into`): units step by the
        stamp lattice, a row's end clips rather than wraps, and the drawn
        position resolves through
        :meth:`~celpix.core.document.Document.cell_at`.
        """
        doc = self._doc
        anchor = self._stamp_hover_anchor
        if doc is None or doc.cells is None or anchor is None:
            return BLANK
        width = self._cells_per_row()
        unit_w, unit_h = doc.stamp_cells
        x = anchor % width + dx * unit_w
        if x >= width:
            return BLANK
        at = doc.cell_at((anchor // width + dy * unit_h) * width + x)
        return doc.cells[at] if 0 <= at < len(doc.cells) else BLANK

    def _on_stamp_hovered(self, slot: object) -> None:
        """The pointer crossed into another cell: re-aim the ghost's target.

        Coalesced to the placed unit — the canvas already reports per tile
        slot, and a metatile map would otherwise re-render an identical ghost
        once per tile crossed. Re-rendered only where the target can show:
        a held *record* lands the same everywhere, except on a row-group
        format, whose landing keeps the target's row.
        """
        doc = self._doc
        anchor = None
        if slot is not None and doc is not None:
            # Snapped to the unit lattice the ghost is painted on — a plain
            # map's unit is one cell, a chained map's is the whole stamp.
            unit_w, unit_h = doc.stamp_cells
            width = self._cells_per_row()
            at = int(slot) // doc.tiles_per_cell
            x, y = at % width, at // width
            anchor = (y - y % unit_h) * width + (x - x % unit_w)
        if anchor == self._stamp_hover_anchor:
            return
        self._stamp_hover_anchor = anchor
        if not self._stamping or self._stamp_cells is not None or doc is None:
            return
        if self._source_cell is None or doc.has_row_groups:
            self._sync_stamp_preview()

    def _stamp_unit_cells(self, cell: Cell) -> list[Cell]:
        """What ``cell`` draws as, one record per composed cell.

        ``cell`` itself for every ordinary map. On a **chained** map a held ID
        is a position in the map being drawn through, so the unit is that
        stamp's source cells resolved — through the resolution's own helper
        (:func:`~celpix.core.tilemap.expand_stamp`), so the ghost and the map
        cannot resolve one coordinate two different ways. The **whole** record
        goes in, not just its index: the landing composes the laid entry's
        flips, row and visibility over the source (§3.1), and a ghost built
        from a bare coordinate would preview a stamp facing a different way
        than the press lays wherever the format carries those fields.
        """
        doc = self._doc
        chain = doc.chain if doc is not None else None
        if doc is None or chain is None:
            return [cell]
        return expand_stamp(
            cell,
            chain.source,
            doc.stamp_cells,
            chain.source_columns,
            carry_rows=chain.carry_rows,
        )

    def _refuse_stamp(self) -> None:
        """Say why a click laid nothing down, rather than doing nothing silently.

        Reachable with the tool armed and no tile held — on a fresh session, or
        after a rebind moved the ID run out from under the pick — which is
        exactly when the user has no way to guess what is wrong.
        """
        self.statusBar().showMessage(
            "No tile to stamp - pick one in the Tile Source panel first."
        )

    # -- the transform bar, while armed --------------------------------------
    def _sync_stamp_transform_actions(self) -> None:
        """Arm the Tile/Block groups for the held stamp.

        The bar's own sync reads the selection, and arming the tool cleared it,
        so while armed the groups arm off what a press would lay instead
        (:meth:`~...transform.TransformMixin._sync_transform_actions` branches
        here) — the same retargeting the property row makes. Tile transforms
        take any holding; Block ones need a swept brush, the one holding with
        positions to permute. Rotation demands the square tile every group
        does, a square brush for the Block half, and a **record-backed**
        holding besides: a sheet pick's flips travel as the row's boolean
        settings, and a rotation is not a boolean a setting can carry. The
        per-operation veto is the format's, exactly as over a selection
        (:meth:`~...transform.TransformMixin._transform_allowed`).
        """
        allows = self._transform_allowed
        target = self._cell_props_target()
        record_backed = target in ("record", "brush")
        square = record_backed and self._square_tiles()
        self._arm_transform_group(self._tile_group, target is not None, square, allows)
        brush = self._stamp_brush
        held = brush is not None and len(brush) > 0
        square_block = held and brush.width == brush.height
        self._arm_transform_group(
            self._block_group, held, square and square_block, allows
        )

    def _stamp_transform_tiles(self, op) -> None:  # noqa: ANN001 — a TransformOp
        """The Tile buttons against the held stamp: each unit, in place.

        What the operation means follows what is held, exactly as the property
        row's boxes do (:mod:`~celpix.ui.main_window.cell_props_bar`):

        - an **eyedropped record** or a **canvas-swept brush** transforms its
          records through the format's own answer
          (:meth:`~...tilemap_edit.TilemapEditMixin._cell_transform`) — a flip
          toggles each cell's bit, positions untouched;
        - a **sheet pick**, single or swept, toggles the row's setting — the
          checkbox's own gesture, so the key and the click agree. Its bare
          records' bits never land, so the setting is the only place the flip
          can be said — and a rotation is refused there, being no boolean a
          setting could hold.
        """
        target = self._cell_props_target()
        if target is None:
            self._refuse_stamp()
            return
        field = op.cell_op.value
        said = op.cell_op.label
        if target == "attrs":
            if field not in ("flip_h", "flip_v") or field not in self._cell_fields():
                self._refuse_transform(op)
                return
            now = not bool(self._stamp_attr_value(field))
            self._stamp_attrs[field] = now
            message = f"Next stamp lays {said} {'on' if now else 'off'}."
        else:
            apply = self._cell_transform(op)
            if apply is None:
                self._refuse_transform(op)
                return
            if target == "record":
                self._source_cell = apply(self._source_cell)
                if field in ("flip_h", "flip_v"):
                    state = "on" if getattr(self._source_cell, field) else "off"
                    message = f"Held stamp's {said} {state}."
                else:
                    message = f"{op.past} the held stamp."
            else:
                brush = self._stamp_brush
                for y in range(brush.height):
                    for x in range(brush.width):
                        brush.set(x, y, apply(brush.get(x, y)))
                message = (
                    f"{op.past} each cell of the held "
                    f"{brush.width}x{brush.height} brush."
                )
        self._after_stamp_prop_edit(message)

    def _stamp_transform_block(self, op) -> None:  # noqa: ANN001 — a TransformOp
        """The Block buttons against the held brush: positions *and* cells.

        The Block reading everywhere else — permute the units and transform
        each, so the brush mirrors as one picture
        (:meth:`~...tilemap_edit.TilemapEditMixin._transform_cell_selection`)
        — applied to the rectangle a right drag swept; only a brush has
        positions to permute, so the group disarms for any single holding. A
        **canvas-swept** brush transforms its records through the format's
        answer; a **sheet-swept** one mirrors its layout and says the per-cell
        half by toggling the row's setting, since its bare records' bits never
        land — the setting is where every square's flip is said.
        """
        brush = self._stamp_brush
        if brush is None or not len(brush):
            return  # the group is disarmed without a brush; a backstop
        field = op.cell_op.value
        if self._source_cell is None:
            if field not in ("flip_h", "flip_v") or field not in self._cell_fields():
                self._refuse_transform(op)
                return
            self._stamp_brush = (
                brush.flipped_h() if field == "flip_h" else brush.flipped_v()
            )
            self._stamp_attrs[field] = not bool(self._stamp_attr_value(field))
        else:
            apply = self._cell_transform(op)
            if apply is None:
                self._refuse_transform(op)
                return
            cols, rows = brush.width, brush.height
            if field.startswith("rotate") and cols != rows:
                return  # disarmed for a non-square brush; a backstop
            turned = CellGrid(cols, rows)
            for dy in range(rows):
                for dx in range(cols):
                    sx, sy = op.cell_src(dx, dy, cols, rows)
                    turned.set(dx, dy, apply(brush.get(sx, sy)))
            self._stamp_brush = turned
        self._after_stamp_prop_edit(
            f"{op.past} the held {brush.width}x{brush.height} brush."
        )
