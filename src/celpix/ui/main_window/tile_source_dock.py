"""The tile source dock: the sheet's surroundings, and what it says about a tile.

:class:`~celpix.ui.tile_source_panel.TileSourcePanel` is a dumb grid of tiles;
everything around it is built here — the Cols and Zoom spins, the scroll area,
and the details readout under the grid.

It is **tabbed with the Palette dock** rather than given a column of its own.
The two are the same kind of thing — the material a picture is made of, addressed
by number — and only one of them is being consulted at a time, so they share the
space and the Palette is the tab a fresh window opens on. That also makes the
cost question answer itself: a background tab is not visible, and the sheet is
composed only while it is (:meth:`~TileSourceDockMixin._refresh_tile_source`),
exactly as the hex dump is.

Picking a tile records *what* a stamp would place; the gesture that places it is
the canvas's (``stamp_tool.py``). The one thing here that writes is **Set Base
Tile**, and it writes the binding rather than a cell — through the Base tile
spin's own path, so the two are one undoable step and cannot disagree
(``docs/design/tilemap-entry.md`` §8).
"""

from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDockWidget,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from celpix.core.tilemap import Cell, resolve_cell
from celpix.pipeline import pipeline
from celpix.project.workspace import TileSource
from celpix.ui import render_bridge
from celpix.ui.tile_source_panel import TileSourcePanel
from celpix.ui.widgets import (
    add_labelled,
    counted,
    pan_scroll_area,
    value_spin,
    zoom_anchored,
)

# The dock's rows carry no vertical margin of their own, and its inset matches
# the palette dock's - the two share a tab bar, so a row that sat on a different
# rhythm would show the moment the tabs were switched.
_ROW_GAP = 4
_ROW_MARGIN = 4

# How wide the sheet opens. 16 across is the width every tile bank is read at
# elsewhere in the editor (and the width the palette grid uses for its own
# addressing), so a tile's column is the low nibble of its ID.
_DEFAULT_COLUMNS = 16
_DEFAULT_ZOOM = 2


class TileSourceDockMixin:
    """The tile source dock's header, sheet and readout.

    A slice of :class:`~celpix.ui.main_window.window.MainWindow`, not a
    standalone object: it reads the window's own single live ``_doc`` and its
    widgets. See the module docstring for what it owns, and the package
    docstring for why these are mixins.
    """

    def _build_tile_source_dock(self) -> None:
        """The tile source dock: a Cols/Zoom header over the sheet, tabbed with
        Palette.

        Built after ``_build_palette_dock``, whose dock it tabs onto — and which
        is raised again afterwards, so a fresh window opens on the Palette and
        the sheet costs nothing until it is asked for.
        """
        self._tile_source_panel = TileSourcePanel()
        self._tile_source_panel.tile_selected.connect(self._on_tile_source_selected)
        self._tile_source_panel.zoom_requested.connect(self._on_tile_source_wheel_zoom)
        self._tile_source_panel.pan_requested.connect(self._pan_tile_source)
        # Which palette row the sheet was last composed through, so a selection
        # that lands on a cell of the same row costs nothing. The row is a *render*
        # input here (it is folded into the indices), so it cannot be applied to a
        # sheet already composed - see :meth:`_tile_source_row`.
        self._tile_source_row_shown: int | None = None
        # The tile a stamp would place. Held on the window rather than read back
        # off the panel because it is session state that outlives a rebuild of
        # the sheet, and because the stamp tool will want it without knowing
        # which widget it came from.
        self._source_tile_id: int | None = None
        # The whole cell the pick was taken *off*, when it was taken off one —
        # the stamp tool's eyedropper. None for a pick made in the sheet, which
        # holds tiles and knows nothing about palette rows or flips. A stamp
        # writes what the pick carried (:meth:`_set_source_tile`).
        self._source_cell: Cell | None = None

        holder = QScrollArea()
        holder.setWidget(self._tile_source_panel)
        holder.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        holder.setWidgetResizable(False)
        self._tile_source_scroll = holder
        # The backing beside a short sheet zooms too: a bank read at 1x fills a
        # sliver of the dock, and the pointer is out on the grey exactly when the
        # user wants it bigger.
        self._tile_source_panel.claim_background(holder)

        # Cols and Zoom, the two questions a sheet of tiles raises that the
        # document cannot answer: how wide to read it, and how big. Both are the
        # reader's, not the file's, so neither is project state - they are the
        # panel's own view of a bank that has no natural width.
        self._tile_source_columns = value_spin(
            1, 64, _DEFAULT_COLUMNS, lambda _value: self._refresh_tile_source()
        )
        self._tile_source_zoom = value_spin(
            1, 8, _DEFAULT_ZOOM, self._on_tile_source_zoom
        )
        self._tile_source_zoom.setSuffix("x")

        header = QHBoxLayout()
        header.setContentsMargins(_ROW_MARGIN, 0, _ROW_MARGIN, 0)
        add_labelled(
            header,
            "Cols",
            self._tile_source_columns,
            "Tiles across the sheet\n"
            "How the bank is read, not how it is stored\n"
            "Shift+Left/Right steps it while the sheet has focus",
        )
        add_labelled(
            header,
            "Zoom",
            self._tile_source_zoom,
            "Magnification of the sheet\nCtrl+wheel over the tiles does the same",
        )
        header.addStretch(1)

        # Details for the picked tile, and the note that stands in for them when
        # there is no sheet. Selectable so an ID can be copied out into the Cell
        # spin or a hex editor. Wrapping, because the chained reading names two
        # entries and a number.
        self._tile_source_details = QLabel()
        self._tile_source_details.setContentsMargins(_ROW_MARGIN, 0, _ROW_MARGIN, 0)
        self._tile_source_details.setWordWrap(True)
        self._tile_source_details.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )

        # Under the readout, because it acts on what the readout describes: the
        # line above says which bank tile the pick resolves to, and this is the
        # button that makes that tile the one a reference of 0 draws. Its tooltip
        # is set per document, the reference being a cell on a map and a subsprite
        # on an object (:meth:`_sync_set_base_tile`).
        self._set_base_tile_button = QPushButton("Set Base Tile")
        self._set_base_tile_button.clicked.connect(self._on_set_base_tile)
        button_row = QHBoxLayout()
        button_row.setContentsMargins(_ROW_MARGIN, 0, _ROW_MARGIN, 0)
        button_row.addWidget(self._set_base_tile_button)
        button_row.addStretch(1)

        container = QWidget()
        column = QVBoxLayout(container)
        column.setContentsMargins(0, _ROW_GAP, 0, _ROW_GAP)
        column.setSpacing(_ROW_GAP)
        column.addLayout(header)
        column.addWidget(holder, 1)
        column.addWidget(self._tile_source_details)
        column.addLayout(button_row)

        self._tile_source_dock = QDockWidget("Tile Source", self)
        self._tile_source_dock.setObjectName("tile-source-dock")  # keeps saveState
        self._tile_source_dock.setWidget(container)
        self.tabifyDockWidget(self._palette_dock, self._tile_source_dock)
        self._palette_dock.raise_()
        # Switching to this tab does not re-run the refresh cycle, so the sheet
        # is composed on show - the hex dock's arrangement, and for the same
        # reason: it is the expensive panel that is usually not looked at.
        self._tile_source_dock.visibilityChanged.connect(
            lambda _visible: self._refresh_tile_source()
        )

    def _on_tile_source_zoom(self, value: int) -> None:
        """Zoom is the panel's own; nothing has to be recomposed for it."""
        self._tile_source_panel.set_zoom(value)

    def _on_tile_source_wheel_zoom(self, steps: int, pos) -> None:  # noqa: ANN001
        """Ctrl+wheel over the sheet, anchored on the tile under the cursor.

        The canvas's wheel zoom (``NavigationMixin._on_zoom_requested``) with its
        one simplification: these levels are whole magnifications a spin steps
        through, so a notch is a step rather than a walk along an uneven list.
        """
        spin = self._tile_source_zoom
        new = min(max(spin.value() + steps, spin.minimum()), spin.maximum())
        zoom_anchored(self._tile_source_scroll, spin, new, pos)

    def _pan_tile_source(self, dx: int, dy: int) -> None:
        """Shift the sheet's scroll view by a space-drag delta (device pixels)."""
        pan_scroll_area(self._tile_source_scroll, dx, dy)

    # -- set base tile -------------------------------------------------------
    def _can_set_base_tile(self) -> bool:
        """Whether the picked tile could become the base — the button's gate.

        Needs a pick, a tilemap with a tile numbering for a base to shift, and a
        binding to hang it on. A **chained** map is the only exclusion, and the
        same one the binding bar makes: its cells are coordinates into another
        map, which carries its own base, so there is nothing here for one to mean
        (``docs/design/tilemap-entry.md`` §3.1).

        A **sprite object** is not one. Its records are not cells, but they hold
        tile numbers in the same space and the base shifts them the same way — the
        binding bar shows it the Base tile spin for exactly that reason, so
        refusing the pointing gesture for the value that spin holds would be this
        panel disagreeing with the bar about the same number.
        """
        doc, entry = self._doc, self._workspace.current
        if doc is None or entry is None or self._source_tile_id is None:
            return False
        if not doc.is_tilemap or doc.chain is not None:
            return False
        source = entry.tile_source
        return source is not None and source.is_bound

    def _on_set_base_tile(self) -> None:
        """Make the picked tile the one a reference of 0 draws.

        The base is stated in the **source's** tile numbers — it *is* the tile a
        reference of 0 draws (:attr:`~celpix.core.document.Document.
        tile_base_index`), a cell holding 0 on a map and a subsprite holding 0 on
        an object — while the panel is addressed in the file's own IDs, so the
        pick has to be resolved through the base in force before it can replace
        it. That is the same arithmetic the readout above the button prints as
        "bank tile $N", so the button does what the line says.

        Through :meth:`~...tilemap_bar.TilemapBarMixin._rebind_tiles`, so this is
        the Base tile spin's own step: one undoable command, one re-read, and the
        spin follows because both read the entry back.

        The pick then moves to **ID 0**, because that is where the tile the user
        picked has just landed — the sheet is re-addressed by the base, so
        leaving the pick on the old number would slide the ring onto a different
        picture and read as the button having chosen the wrong tile.
        """
        if not self._can_set_base_tile():
            return
        entry = self._workspace.current
        assert entry is not None and self._doc is not None
        source = entry.tile_source or TileSource()
        base = self._source_tile_id + self._doc.tile_base_index
        if base == source.base_index:
            return
        self._rebind_tiles(
            entry, replace(source, base_index=base), f"set base tile to ${base:X}"
        )
        # Only where the bind actually landed: a re-read that failed put the
        # entry back as it was, and so is the sheet the pick addresses.
        if (entry.tile_source or TileSource()).base_index == base:
            self._set_source_tile(0)

    def _sync_set_base_tile(self) -> None:
        """Converge the button with the pick — and with what it would shift.

        The tooltip is part of that: the base moves *references*, and what a
        reference is on screen is a cell on a map and a subsprite on a sprite
        object. One sentence describing cells over a file that has none is the
        kind of wrong a user cannot check.
        """
        self._set_base_tile_button.setEnabled(self._can_set_base_tile())
        doc = self._doc
        if doc is not None and doc.is_sprite:
            self._set_base_tile_button.setToolTip(
                "Make the picked tile the one a subsprite holding $0 draws\n"
                "Shifts every subsprite's tile by the same amount - use it\n"
                "when the object and its tiles number from different places"
            )
            return
        self._set_base_tile_button.setToolTip(
            "Make the picked tile the one cell 0 draws\n"
            "Shifts every cell by the same amount - use it when\n"
            "the map and its tiles number from different places"
        )

    def _on_tile_source_selected(self, tile_id: int) -> None:
        # A pick made in the sheet carries an ID and nothing else, so it drops
        # whatever record an earlier eyedrop left held - but only when it names a
        # *different* tile. The panel is driven from this side too: every pick is
        # pushed into it (`_set_source_tile`) and the refresh re-selects the held
        # ID after a recompose dropped it, both of which come back through here.
        # Those echoes must not throw away the record they are echoing.
        if tile_id != self._source_tile_id:
            self._source_cell = None
        self._source_tile_id = tile_id
        self._refresh_tile_source_details()
        self._sync_set_base_tile()

    def _set_source_tile(self, tile_id: int, cell: Cell | None = None) -> None:
        """Hold ``tile_id`` as the tile a stamp would place, from anywhere.

        The panel is one source of the pick and the stamp tool's eyedropper is
        the other (``stamp_tool.py``), so the holding is here rather than in
        either: `_source_tile_id` has to survive a sheet that is not on screen —
        the dock composes nothing while its tab is in the background — which it
        cannot if the panel's own selection is the record of it. Pushing it into
        the panel is a no-op wherever that is the case, and the next refresh
        re-applies it.

        ``cell`` is the record the tile was taken *off* — the eyedropper's, the
        one path that has one. It is held beside the ID because **a stamp writes
        what the pick carried**: a tile taken off a cell lays that cell down
        whole, and one taken off the sheet sets an index and leaves the rest of
        the target alone (:meth:`~...stamp_tool.StampToolMixin._stamp_cell`).
        """
        self._source_tile_id = tile_id
        self._source_cell = cell
        self._tile_source_panel.select_id(tile_id)
        self._refresh_tile_source_details()
        self._sync_set_base_tile()

    def _point_source_at_pixel(self, x: int, y: int) -> None:
        """Pick the tile canvas pixel ``(x, y)`` was drawn from, if a cell drew it.

        The pixel eyedropper's other half. A right-click on a tilemap in pixel
        mode asks "what is this?", and the colour is only one of the two answers
        that has somewhere to go: the pixel came out of a particular tile of the
        bound bank, placed by a particular cell, and this dock is where that tile
        is looked at. So the pick lands here as well as in the palette grid — the
        same pair of answers the stamp tool's eyedropper leaves behind
        (:meth:`~...stamp_tool.StampToolMixin._pick_tile_at`), from the mode where
        the question is about a pixel rather than a cell.

        The ID is the cell's own, before the binding's base tile, because that is
        what the sheet is addressed in — which is why a **grid map** is asked for
        the cell of ``Document.cells`` under the slot rather than for the
        laid-out one the pixel resolved through: a chained map's sheet holds the
        stamps its own cells name, not the tiles those resolve to. A **sprite
        object** has no slots to divide, so its piece is the answer, exactly as
        the ring the canvas selection puts on the sheet reads it.

        The record travels with the ID on a grid map, so a stamp made after the
        pick lays the cell down whole: the gesture is the same "this one" in
        either mode, and it should mean the same thing in both.

        Silent where nothing was drawn — a blank position, a cell pointing
        outside the bank, or a pixel document, which has no cells at all.
        """
        doc = self._doc
        if doc is None or not doc.is_tilemap:
            return
        if doc.is_sprite:
            found = self._bank_pixel_at(x, y)
            if found is not None:
                self._set_source_tile(found[0].index)
            return
        slot = self._slot_at_pixel(x, y)
        at = None if slot is None else self._stamp_cell_at(slot)
        if at is None or doc.cells is None:
            return
        # Through the bank lookup rather than off the cell alone, so a position
        # that draws nothing picks nothing: the two have to agree about what is
        # on screen, since the colour the same click sampled came from there.
        if self._bank_tile_at_slot(slot) is None:
            return
        self._set_source_tile(doc.cells[at].index, doc.cells[at])

    # -- the refresh ---------------------------------------------------------
    def _refresh_tile_source(self) -> None:
        """Put the bound source's tiles into the sheet, or say why there are none.

        The one place the dock converges, reached from the render cycle, from a
        Cols change and from the tab being shown. A cheap no-op while the dock is
        hidden, which includes being the *background tab* — the usual state, the
        Palette being the tab a window opens on.

        What it draws is the map's own picture, one synthetic cell per tile ID
        (:func:`~celpix.pipeline.pipeline.tile_source_image`), in the palette row
        :meth:`_tile_source_row` chooses and on the same two-colour-table rule the
        canvas follows: where the format gives a cell a palette row the row is
        already folded into the indices and the table must not offset again;
        where it does not, the sheet reads under one shifted table exactly as the
        map does.
        """
        if not self._tile_source_dock.isVisible():
            return
        doc = self._doc
        note = self._tile_source_note()
        if note is not None or doc is None:
            self._tile_source_panel.clear()
            self._tile_source_details.setText(note or "")
            self._tile_source_row_shown = None
            self._sync_set_base_tile()
            return
        row = self._tile_source_row()
        sheet = pipeline.tile_source_image(
            doc,
            self._registry,
            self._tile_source_columns.value(),
            self._cell_index_limit(),
            row,
        )
        self._tile_source_row_shown = row
        if not sheet.ids:
            self._tile_source_panel.clear()
            self._tile_source_details.setText(
                "The bound entry has no tiles this map can reach."
            )
            self._sync_set_base_tile()
            return
        # Never offset at the colour table. Unlike the map, whose cells carry
        # whatever row the *file* gave them, this sheet's cells are synthetic and
        # were handed ``row`` above — so :func:`~celpix.pipeline.pipeline.
        # expand_cells` has already folded it into the indices, exactly as it
        # does for a format that states rows of its own. Shifting the table too
        # would apply the row twice and draw the bank in row 2n.
        image = render_bridge.render_pinned(sheet.grid, doc.palette)
        # The stamp, not the cell: where a source states a stamp size an ID names
        # the whole stamp, so the click target has to be the whole stamp too.
        across, down = doc.stamp_tiles
        self._tile_source_panel.set_zoom(self._tile_source_zoom.value())
        self._tile_source_panel.set_sheet(
            image,
            sheet.ids,
            (across * doc.tile_width, down * doc.tile_height),
            self._tile_source_columns.value(),
        )
        # The pick survives a recompose where the ID is still on offer - a
        # palette edit, a Cols change, an edit to the art - and is dropped by the
        # panel where it is not. Either way the readout has to follow.
        if self._source_tile_id is not None:
            self._tile_source_panel.select_id(self._source_tile_id)
        self._sync_tile_source_marker()
        self._refresh_tile_source_details()
        self._sync_set_base_tile()

    def _tile_source_row(self) -> int:
        """Which palette row to read the sheet's tiles in.

        **The selected cell's**, when there is one and the format gives cells a
        row to have. A bank is indices until a row is chosen for it, and the row
        the user cares about is the one the thing they just clicked is drawn in —
        so picking a cell shows its tiles in its own colours, which is what makes
        the panel answer "what else could this cell have named" rather than
        "what would these tiles look like in row 0".

        Otherwise the **Subpal row**, which is the palette dock's selected row:
        clicking a swatch there sets this spin
        (``PalettePanel.subpalette_row_selected``), so the two are one value and
        the sheet follows whichever the user last said.

        Read off the file's own cells rather than the resolved ones, because a
        chained map's resolved rows are its *source's* and the sheet keeps those
        anyway (:func:`~celpix.pipeline.pipeline.tile_source_image`).

        On a **sprite object** the picked subsprite answers instead of a cell,
        which is the same rule reached through the thing that document selects in
        (``sprite_select.py``): a subsprite carries its own row, and the sheet
        shows what else the picked one could have drawn.
        """
        doc = self._doc
        if doc is not None and doc.is_sprite:
            sub = self._picked_subsprite_record()
            return sub.palette_row if sub is not None else self._subpalette.value()
        if doc is not None and doc.cells and doc.cells_carry_palette_rows:
            cells = self._selected_cells()
            if cells:
                return doc.cells[cells[0]].palette_row
        return self._subpalette.value()

    def _tile_source_note(self) -> str | None:
        """Why there is no sheet to show, or ``None`` when there is one.

        Three refusals, and they are three different situations: nothing on
        screen is a map, the map has bound nothing yet, or what it bound has
        since been closed. Naming which one is the whole value of the note — the
        second is a control on the binding bar away from being fixed, the third
        is a file to reopen.

        A **sprite object** is not among them. It has no cell grid, but its
        subsprites name tiles in the same numbers a cell does, so the bank it
        draws from is exactly as worth looking at — and with a subsprite picked
        the ring answers the question the panel exists for, "what is this made
        of", on the one kind of document the canvas could not answer it for
        (``sprite_select.py``).
        """
        doc = self._doc
        if doc is None or not doc.is_tilemap:
            return "No tilemap on screen - open one to see the tiles it draws from."
        entry = self._workspace.current
        source = entry.tile_source if entry is not None else None
        if source is None or not source.is_bound:
            return "No tile source bound - pick one on the bar under the canvas."
        if self._binding_target(source) is None:
            return "The entry it drew from is no longer open."
        return None

    def _sync_tile_source_marker(self) -> None:
        """Ring the tile the canvas's selection names — its cell's, or, on a
        sprite object, its picked subsprite's (``sprite_select.py``).

        Driven by the **selection** pass as well as by the refresh, and it has to
        be: a selection moves without anything being redrawn, and the whole point
        of the ring is that picking a cell over there shows what it is made of
        over here.

        The number is the cell's own index as the file stores it, before the
        binding's base tile — the same number Show Tile IDs writes over the cell
        and the Cell spin holds, so the three cannot disagree about what a cell
        is called.

        The selection also decides the sheet's **colours**
        (:meth:`_tile_source_row`), and a row is folded into the indices at
        compose time rather than applied to a finished sheet — so a pick that
        lands on a different row has to recompose. Guarded on the row actually
        moving, since most selection changes stay inside one row and the sheet is
        the expensive panel.
        """
        if not self._tile_source_dock.isVisible():
            return
        doc = self._doc
        if doc is not None and doc.is_sprite:
            if self._tile_source_row_shown != self._tile_source_row():
                self._refresh_tile_source()  # re-marks and re-reads on the way out
                return
            sub = self._picked_subsprite_record()
            # The corner tile of a large subsprite, which is the number the
            # record holds and the only one of its four that is on the sheet:
            # the sheet is laid out in tiles, both subsprite sizes being made of
            # them (:func:`~celpix.pipeline.pipeline.tile_source_ids`).
            self._tile_source_panel.set_marked_id(None if sub is None else sub.index)
            return
        cells = self._selected_cells() if doc is not None else []
        if doc is None or doc.cells is None or not cells:
            self._tile_source_panel.set_marked_id(None)
            return
        if self._tile_source_row_shown != self._tile_source_row():
            self._refresh_tile_source()  # re-marks and re-reads on the way out
            return
        self._tile_source_panel.set_marked_id(doc.cells[cells[0]].index)

    # -- the readout ---------------------------------------------------------
    def _refresh_tile_source_details(self) -> None:
        tile_id = self._tile_source_panel.selected_id()
        if tile_id is None:
            self._tile_source_details.setText("No tile selected.")
            return
        self._tile_source_details.setText(self._tile_source_line(tile_id))

    def _tile_source_line(self, tile_id: int) -> str:
        """What tile ``tile_id`` is, in the terms the document reads it in.

        Two readings, because the ID means two things. On an ordinary map it is
        a tile in the bound bank, and what is worth saying is where that tile
        actually sits once the base is applied — the two numbers differ exactly
        when the map and its art number from different places, which is when the
        question gets asked. On a **chained** map it is a stamp: a position in
        the map being drawn through, whose own cell supplies the tile and the
        attributes, so the line resolves that hop.

        Both end with how many cells use it, which is the reverse question the
        panel is otherwise silent about: one scan over the cells, run on a click
        rather than on a repaint.

        A **sprite object** takes the first reading and counts its *subsprites*
        instead — over the frames that are drawn, not over the file's records: an
        object has room for a fixed 32 or 64 frames and most of them are empty,
        so counting the records would report every unused one as a user of tile
        ``$0`` (``docs/graphics-formats-reference/scgcad-formats.md`` §8). A
        subsprite is a *rectangle* of tiles, so it counts as a user of every tile
        it draws and not only the one its record names — the corner tile is what
        the record holds, but a 2x2 piece is four tiles on screen and each of them
        would otherwise report one user too few
        (:meth:`~celpix.core.sprite.Subsprite.tile_indices`).
        """
        doc = self._doc
        if doc is None:
            return "No tile selected."
        if doc.is_sprite:
            used = counted(
                sum(
                    1
                    for frame in doc.shown_frames
                    for sub in frame
                    if tile_id in sub.tile_indices()
                ),
                "subsprite",
            )
        else:
            used = counted(
                sum(1 for cell in doc.cells or [] if cell.index == tile_id), "cell"
            )
        chain = doc.chain
        if chain is None:
            bank = tile_id + doc.tile_base_index
            where = f"bank tile ${bank:X}" if bank != tile_id else "no base offset"
            return f"Tile ${tile_id:X} - {where} - used by {used}."
        stamp = resolve_cell(
            Cell(index=tile_id), chain.source, carry_rows=chain.carry_rows
        )
        # The corner cell's own attributes, which is what a stamp's other cells
        # each have their own of - so the size is stated and the rest is left to
        # the picture rather than listed cell by cell.
        across, down = doc.stamp_cells
        parts = [] if (across, down) == (1, 1) else [f"{across}x{down} cells"]
        parts += [
            f"tile ${stamp.index + doc.tile_base_index:X}",
            f"row {stamp.palette_row}",
        ]
        if stamp.flip_h:
            parts.append("H-flip")
        if stamp.flip_v:
            parts.append("V-flip")
        return f"Stamp ${tile_id:X} - {', '.join(parts)} - used by {used}."
