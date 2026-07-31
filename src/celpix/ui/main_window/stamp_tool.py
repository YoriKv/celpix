"""The stamp tool: lay tiles into a tilemap's cells by pointing at them.

The **placing** half of `docs/design/tilemap-entry.md` §9's first item. Setting a
cell's reference by number already existed (the binding bar's Cell spin, over the
selection) and choosing one by looking arrived with the tile source panel; what
was missing was the gesture that puts the two together.

Edit Tiles is a **modal tool over tile mode**, the shape the rearrange tool
already has and for the same reason: it wants both mouse buttons, so it cannot
share the canvas with the selection drag. While armed, a left press lays the
panel's picked tile into the cell under the cursor and a left drag keeps laying
it — a pencil over cells — while a right press picks the tile a cell already
names, which is the eyedropper. It is offered only on a tilemap, because only a
tilemap has cells that name tiles (`Capability.STAMP`).

**A stroke is one undoable step.** A drag across forty cells is one gesture and
has to undo as one, so the drag is previewed on the live document and committed
through :meth:`~...tilemap_edit.TilemapEditMixin._apply_cells` on release, with
the cells as they stood at the press restored underneath it first. That is the
pixel pen's arrangement (paint into a working copy, commit the stroke) at cell
scale.

**What a stamp writes is the index and nothing else.** A cell's palette row,
flips and carried `flags` are its own, and are as likely to be what the user set
up as what they want overwritten — so pointing a cell somewhere else leaves them
where they are, exactly as the Cell spin does. On a chained map that makes the
gesture a restamp, and the attributes then come from the source cell anyway
(§3.1).
"""

from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QKeySequence

from celpix.core.capabilities import Capability
from celpix.pipeline import pipeline
from celpix.ui.tools import EditMode
from celpix.ui.widgets import counted, signals_blocked

STAMP_TIP = (
    "Lay the picked tile into a cell (T)\n"
    "Left click or drag stamps; right click picks a tile\n"
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

        Three levels, and they are the three the Cell spin weighs, because they
        are the same question asked by pointing instead of by typing
        (``docs/design/tilemap-entry.md`` §4). ``STAMP`` is the **kind**'s: only
        a tilemap has a cell that names a tile. ``cells_editable`` is this
        **file**'s: a sprite object's records are subsprites at pixel offsets,
        so there is no cell under the cursor. The limit is the **format**'s: a
        cell with no index field has no number a stamp could set.
        """
        doc = self._doc
        return (
            doc is not None
            and self._can(Capability.STAMP)
            and doc.cells_editable
            and self._cell_index_limit() is not None
        )

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
        if cells[at].index == tile_id:
            return  # already what it names; nothing to draw and nothing to undo
        cells[at] = replace(cells[at], index=tile_id)
        doc.cells = list(cells)
        doc.resolve()
        self._refresh_view()

    def _pick_tile_at(self, slot: int) -> None:
        """The eyedropper: take the tile the cell under ``slot`` already names.

        The number taken is the cell's own index as the file stores it, before
        the binding's base tile — the same number the tile source panel is
        addressed in, the Cell spin holds and Show Tile IDs writes over the cell,
        so picking here and looking there cannot disagree.
        """
        doc = self._doc
        at = self._stamp_cell_at(slot)
        if doc is None or doc.cells is None or at is None:
            return
        self._set_source_tile(doc.cells[at].index)
        self.statusBar().showMessage(f"Picked tile ${doc.cells[at].index:X}.")

    def _refuse_stamp(self) -> None:
        """Say why a click laid nothing down, rather than doing nothing silently.

        Reachable with the tool armed and no tile held — on a fresh session, or
        after a rebind moved the ID run out from under the pick — which is
        exactly when the user has no way to guess what is wrong.
        """
        self.statusBar().showMessage(
            "No tile to stamp - pick one in the Tile Source panel first."
        )
