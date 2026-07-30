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

Nothing here writes cells. Picking a tile records *what* a stamp would place; the
gesture that places it is the canvas's, and is not built yet
(``docs/design/tilemap-entry.md`` §9).
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDockWidget,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from celpix.core.tilemap import Cell, resolve_cell
from celpix.pipeline import pipeline
from celpix.ui import render_bridge
from celpix.ui.tile_source_panel import TileSourcePanel
from celpix.ui.widgets import counted

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
        # The tile a stamp would place. Held on the window rather than read back
        # off the panel because it is session state that outlives a rebuild of
        # the sheet, and because the stamp tool will want it without knowing
        # which widget it came from.
        self._source_tile_id: int | None = None

        holder = QScrollArea()
        holder.setWidget(self._tile_source_panel)
        holder.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        holder.setWidgetResizable(False)
        self._tile_source_scroll = holder

        # Cols and Zoom, the two questions a sheet of tiles raises that the
        # document cannot answer: how wide to read it, and how big. Both are the
        # reader's, not the file's, so neither is project state - they are the
        # panel's own view of a bank that has no natural width.
        self._tile_source_columns = QSpinBox()
        self._tile_source_columns.setRange(1, 64)
        self._tile_source_columns.setValue(_DEFAULT_COLUMNS)
        self._tile_source_columns.setKeyboardTracking(False)
        self._tile_source_columns.setToolTip(
            "Tiles across the sheet\nHow the bank is read, not how it is stored"
        )
        self._tile_source_columns.valueChanged.connect(
            lambda _value: self._refresh_tile_source()
        )
        columns_label = QLabel("Cols")
        columns_label.setToolTip(self._tile_source_columns.toolTip())
        columns_label.setBuddy(self._tile_source_columns)

        self._tile_source_zoom = QSpinBox()
        self._tile_source_zoom.setRange(1, 8)
        self._tile_source_zoom.setValue(_DEFAULT_ZOOM)
        self._tile_source_zoom.setKeyboardTracking(False)
        self._tile_source_zoom.setSuffix("x")
        self._tile_source_zoom.setToolTip("Magnification of the sheet")
        self._tile_source_zoom.valueChanged.connect(self._on_tile_source_zoom)
        zoom_label = QLabel("Zoom")
        zoom_label.setToolTip(self._tile_source_zoom.toolTip())
        zoom_label.setBuddy(self._tile_source_zoom)

        header = QHBoxLayout()
        header.setContentsMargins(_ROW_MARGIN, 0, _ROW_MARGIN, 0)
        header.addWidget(columns_label)
        header.addWidget(self._tile_source_columns)
        header.addWidget(zoom_label)
        header.addWidget(self._tile_source_zoom)
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

        container = QWidget()
        column = QVBoxLayout(container)
        column.setContentsMargins(0, _ROW_GAP, 0, _ROW_GAP)
        column.setSpacing(_ROW_GAP)
        column.addLayout(header)
        column.addWidget(holder, 1)
        column.addWidget(self._tile_source_details)

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

    def _on_tile_source_selected(self, tile_id: int) -> None:
        self._source_tile_id = tile_id
        self._refresh_tile_source_details()

    def _set_source_tile(self, tile_id: int) -> None:
        """Hold ``tile_id`` as the tile a stamp would place, from anywhere.

        The panel is one source of the pick and the stamp tool's eyedropper is
        the other (``stamp_tool.py``), so the holding is here rather than in
        either: `_source_tile_id` has to survive a sheet that is not on screen —
        the dock composes nothing while its tab is in the background — which it
        cannot if the panel's own selection is the record of it. Pushing it into
        the panel is a no-op wherever that is the case, and the next refresh
        re-applies it.
        """
        self._source_tile_id = tile_id
        self._tile_source_panel.select_id(tile_id)
        self._refresh_tile_source_details()

    # -- the refresh ---------------------------------------------------------
    def _refresh_tile_source(self) -> None:
        """Put the bound source's tiles into the sheet, or say why there are none.

        The one place the dock converges, reached from the render cycle, from a
        Cols change and from the tab being shown. A cheap no-op while the dock is
        hidden, which includes being the *background tab* — the usual state, the
        Palette being the tab a window opens on.

        What it draws is the map's own picture, one synthetic cell per tile ID
        (:func:`~celpix.pipeline.pipeline.tile_source_image`), rendered on the
        same two-colour-table rule the canvas follows: where the format gives a
        cell a palette row the row is already folded into the indices and the
        table must not offset again; where it does not, the sheet reads under the
        view's subpalette row exactly as the map does.
        """
        if not self._tile_source_dock.isVisible():
            return
        doc = self._doc
        note = self._tile_source_note()
        if note is not None or doc is None:
            self._tile_source_panel.clear()
            self._tile_source_details.setText(note or "")
            return
        sheet = pipeline.tile_source_image(
            doc,
            self._registry,
            self._tile_source_columns.value(),
            self._cell_index_limit(),
        )
        if not sheet.ids:
            self._tile_source_panel.clear()
            self._tile_source_details.setText(
                "The bound entry has no tiles this map can reach."
            )
            return
        if doc.cells_carry_palette_rows:
            image = render_bridge.render_pinned(sheet.grid, doc.palette)
        else:
            base = doc.view.subpalette_row * self._index_space()
            image = render_bridge.render(sheet.grid, doc.palette, base)
        across, down = max(1, doc.cell_tiles[0]), max(1, doc.cell_tiles[1])
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

    def _tile_source_note(self) -> str | None:
        """Why there is no sheet to show, or ``None`` when there is one.

        Three refusals, and they are three different situations: nothing on
        screen is a map, the map has bound nothing yet, or what it bound has
        since been closed. Naming which one is the whole value of the note — the
        second is a control on the binding bar away from being fixed, the third
        is a file to reopen.
        """
        doc = self._doc
        if doc is None or not doc.is_tilemap:
            return "No tilemap on screen - open one to see the tiles it draws from."
        if doc.is_sprite:
            # A sprite object's records sit at signed pixel offsets, so there is
            # no cell naming a tile and no ID space to lay out.
            return "A sprite object has no tile IDs - its frames place tiles directly."
        entry = self._workspace.current
        source = entry.tile_source if entry is not None else None
        if source is None or not source.is_bound:
            return "No tile source bound - pick one on the bar under the canvas."
        if self._binding_target(source) is None:
            return "The entry it drew from is no longer open."
        return None

    def _sync_tile_source_marker(self) -> None:
        """Ring the tile the canvas's selected cell names.

        Driven by the **selection** pass as well as by the refresh, and it has to
        be: a selection moves without anything being redrawn, and the whole point
        of the ring is that picking a cell over there shows what it is made of
        over here.

        The number is the cell's own index as the file stores it, before the
        binding's base tile — the same number Show Tile IDs writes over the cell
        and the Cell spin holds, so the three cannot disagree about what a cell
        is called.
        """
        if not self._tile_source_dock.isVisible():
            return
        doc = self._doc
        cells = self._selected_cells() if doc is not None else []
        if doc is None or doc.cells is None or not cells:
            self._tile_source_panel.set_marked_id(None)
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
        """
        doc = self._doc
        if doc is None:
            return "No tile selected."
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
        parts = [
            f"tile ${stamp.index + doc.tile_base_index:X}",
            f"row {stamp.palette_row}",
        ]
        if stamp.flip_h:
            parts.append("H-flip")
        if stamp.flip_v:
            parts.append("V-flip")
        return f"Stamp ${tile_id:X} - {', '.join(parts)} - used by {used}."
