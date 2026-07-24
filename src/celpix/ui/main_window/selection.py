"""What is selected on the canvas, and what the clipboard does with it.

Selection is display state and lives here rather than in ``ViewOptions``: it
does not affect how the window renders. It is held as **absolute tile indices**
so it survives scrolling, with a rectangle additionally recording the cells it
was drawn over and the tiles those resolved to.

The shape (:class:`SelectionShape`) decides what a drag means, and the two
shapes are genuinely different things - a linear run maps onto one byte range,
while a rectangle narrower than the view is *disjoint in the file*. Everything
that has to work in bytes (the hex highlight, a new slice) uses the enclosing
run; everything that must not touch the gaps (copy, clear, paste) uses the tile
list. Pixel edits land through :meth:`_apply_tile_edit`, which encodes the run
and pushes one undoable byte splice.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import Enum

from PySide6.QtCore import QPoint, QSettings, Qt
from PySide6.QtGui import (
    QAction,
    QGuiApplication,
    QImage,
    QKeySequence,
)
from PySide6.QtWidgets import (
    QMenu,
)

from celpix.core.argb_grid import ArgbGrid
from celpix.core.arrangement import (
    BlockLayout,
    compose_window,
)
from celpix.core.errors import PipelineError
from celpix.core.index_grid import IndexGrid
from celpix.core.quantize import QuantizeReport
from celpix.pipeline import importer, pipeline
from celpix.pipeline.importer import ImportedTiles
from celpix.project.workspace import (
    EntryKind,
)
from celpix.ui import clipboard, render_bridge
from celpix.ui.tools import EditMode
from celpix.ui.undo_commands import (
    PixelEditCommand,
)
from celpix.ui.widgets import select_combo_data

# QSettings key for the app-wide selection shape: it changes how the mouse is
# read, not how anything renders, so it is a preference rather than view state.
SELECTION_SHAPE_KEY = "view/selection_shape"


class SelectionShape(Enum):
    """What the two slots of a canvas drag describe. ``value`` is the stable
    string persisted in app settings.

    LINEAR is the storage-order shape: tiles are a linear byte stream, so a drag
    selects the run of tiles between press and pointer - the shape that maps
    straight onto a byte range (slices, the hex highlight). RECT is the *picture*
    shape: the cells of the rectangle the drag spans, which for anything narrower
    than the full view is a set of disjoint runs in the file.
    """

    LINEAR = "linear"
    RECT = "rect"


class SelectionMixin:
    """What is selected on the canvas, and the clipboard/pixel edits over it.

    A slice of :class:`~celpix.ui.main_window.window.MainWindow`, not a
    standalone object: it reads and writes the window's own widgets and its
    single live ``_doc``. See the module docstring for what it owns, and the
    package docstring for why these are mixins.
    """

    def _build_edit_menu(self) -> None:
        """Edit ▸ Undo/Redo and the clipboard actions.

        Undo/Redo are stack-provided (label and enabled state come from the
        unified session stack). The clipboard group operates on the selected
        tile run - see :meth:`_copy_selection` for what a copy actually puts on
        the clipboard.
        """
        menu = self.menuBar().addMenu("Edit")
        undo = self._undo_stack.createUndoAction(self)
        undo.setShortcut(QKeySequence.StandardKey.Undo)  # Ctrl+Z
        redo = self._undo_stack.createRedoAction(self)
        # Ctrl+Shift+Z first (the advertised binding), plus the platform
        # standard (Ctrl+Y on Windows), deduplicated.
        sequences = [QKeySequence("Ctrl+Shift+Z")]
        sequences += [
            s
            for s in QKeySequence.keyBindings(QKeySequence.StandardKey.Redo)
            if s not in sequences
        ]
        redo.setShortcuts(sequences)
        menu.addAction(undo)
        menu.addAction(redo)
        menu.addSeparator()
        for action in self._clipboard_actions():
            menu.addAction(action)
        menu.addSeparator()
        menu.addAction(self._select_all_action)
        menu.addSeparator()
        menu.addAction(self._toggle_selection_mode_action)
        menu.addAction(self._toggle_edit_mode_action)
        # Enabled state depends on the clipboard's contents, which any other
        # program can change while we sit idle - so track the signal rather than
        # only recomputing when the menu opens.
        clip = QGuiApplication.clipboard()
        if clip is not None:
            clip.dataChanged.connect(self._sync_edit_actions)
        menu.aboutToShow.connect(self._sync_edit_actions)

    def _build_clipboard_actions(self) -> None:
        """Create the Cut/Copy/Paste/Clear/Select All actions.

        Built before the menus so both the Edit menu and the canvas's context
        menu can show the *same* QAction objects - one enabled state, one
        shortcut, wherever they appear. Added to the window itself so their
        shortcuts fire regardless of which menu is open.
        """
        specs = (
            ("_cut_action", "Cut", QKeySequence.StandardKey.Cut, self._cut_selection),
            (
                "_copy_action",
                "Copy",
                QKeySequence.StandardKey.Copy,
                self._copy_selection,
            ),
            ("_paste_action", "Paste", QKeySequence.StandardKey.Paste, self._paste),
            (
                "_clear_action",
                "Clear",
                QKeySequence.StandardKey.Delete,
                self._clear_pixels,
            ),
            (
                "_select_all_action",
                "Select All",
                QKeySequence.StandardKey.SelectAll,
                self._select_all,
            ),
        )
        for attr, text, key, slot in specs:
            action = QAction(text, self)
            action.setShortcut(key)
            # Window-scoped: the canvas has focus for most of a session, but the
            # toolbars and docks are part of the same editing surface.
            action.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
            action.triggered.connect(slot)
            action.setEnabled(False)
            setattr(self, attr, action)
            self.addAction(action)
        self._build_mode_toggle_actions()

    def _build_mode_toggle_actions(self) -> None:
        """The Edit ▸ mode toggles, on the bare ``S`` and ``E`` keys.

        Display-only shortcuts, like View ▸ Grid: the bare letters are routed by
        the app-wide event filter (``_handle_nav_key``), which yields to focused
        text inputs — a live shortcut here would steal them mid-word. Each is
        enabled only while its swap is available (see :meth:`_sync_edit_actions`).
        """
        specs = (
            (
                "_toggle_selection_mode_action",
                "Toggle Selection Mode",
                "S",
                "Swap Linear / Rectangle selection",
                self._toggle_selection_mode,
            ),
            (
                "_toggle_edit_mode_action",
                "Toggle Edit Mode",
                "E",
                "Swap tile / pixel editing",
                self._toggle_edit_mode,
            ),
        )
        for attr, text, key, tip, slot in specs:
            action = QAction(text, self)
            action.setShortcut(QKeySequence(key))
            action.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
            action.setToolTip(f"{tip} ({key})")
            action.triggered.connect(slot)
            action.setEnabled(False)
            setattr(self, attr, action)

    def _toggle_selection_mode(self) -> None:
        """Swap Linear ⇄ Rectangle, when a swap is available.

        Inert in pixel mode, which forces Rectangle — the combo carries the
        preference and its change handler does the rest of the work.
        """
        if not self._can_toggle_selection_mode():
            return
        current = self._selection_shape.currentData()
        shape = (
            SelectionShape.LINEAR
            if current is SelectionShape.RECT
            else SelectionShape.RECT
        )
        select_combo_data(self._selection_shape, shape)

    def _toggle_edit_mode(self) -> None:
        """Swap tile ⇄ pixel editing, when a document is open to edit."""
        if not self._can_toggle_edit_mode():
            return
        self._set_edit_mode(
            EditMode.TILE if self._edit_mode is EditMode.PIXEL else EditMode.PIXEL
        )

    def _can_toggle_selection_mode(self) -> bool:
        # Pixel mode is rectangle-only, so there is nothing to swap there.
        return self._doc is not None and self._edit_mode is EditMode.TILE

    def _can_toggle_edit_mode(self) -> bool:
        return self._doc is not None

    def _clipboard_actions(self) -> tuple[QAction, ...]:
        return (
            self._cut_action,
            self._copy_action,
            self._paste_action,
            self._clear_action,
        )

    def _sync_edit_actions(self) -> None:
        """Converge the clipboard actions with the selection and the clipboard."""
        self._toggle_selection_mode_action.setEnabled(self._can_toggle_selection_mode())
        self._toggle_edit_mode_action.setEnabled(self._can_toggle_edit_mode())
        if self._edit_mode is EditMode.PIXEL:
            # Pixel mode gates Cut/Copy/Clear on a pixel marquee, not a tile run.
            has = self._doc is not None and self._marquee is not None
            for action in (self._cut_action, self._copy_action, self._clear_action):
                action.setEnabled(has)
            self._paste_action.setEnabled(
                self._doc is not None and clipboard.has_content()
            )
            self._select_all_action.setEnabled(self._doc is not None)
            return
        has_selection = self._doc is not None and self._selected_tile is not None
        for action in (self._cut_action, self._copy_action, self._clear_action):
            action.setEnabled(has_selection)
        self._paste_action.setEnabled(self._doc is not None and clipboard.has_content())
        self._select_all_action.setEnabled(self._doc is not None)

    # -- tile selection ----------------------------------------------------
    def _view_layout(self) -> BlockLayout:
        """The slot ↔ cell mapping the canvas is currently drawing with."""
        return BlockLayout(
            self._columns.value(),
            self._block_cols.value(),
            self._block_rows.value(),
            self._block_order.currentData(),
        )

    def _on_selection_shape_change(self) -> None:
        """Switching Linear ⇄ Rectangle collapses the selection to its anchor.

        Neither shape is a special case of the other - a linear run isn't a
        rectangle and a rectangle isn't a run - so reinterpreting the existing
        range would silently select tiles the user never dragged over. Falling
        back to the one tile they are demonstrably on is the honest conversion.
        """
        QSettings().setValue(
            SELECTION_SHAPE_KEY, self._selection_shape.currentData().value
        )
        if self._doc is not None and self._selected_tile is not None:
            self._select_tiles(self._selected_tile, self._selected_tile)

    def _rect_tiles_for(
        self, origin_slot: int, cols: int, rows: int
    ) -> tuple[int, ...]:
        """Absolute tiles of the ``cols`` × ``rows`` cell block at ``origin_slot``.

        Cell row-major - reading order on screen, which is the order a copy of a
        rectangle travels in and the order a paste stamps back. Cells that hold
        no tile (a partial-width block column) are skipped; the origin slot may
        be negative or past the window, so a rectangle stays computable while it
        is scrolled out of view.
        """
        layout = self._view_layout()
        x0, y0 = layout.slot_to_cell(origin_slot)
        tiles = []
        for dy in range(rows):
            for dx in range(cols):
                slot = layout.cell_to_slot(x0 + dx, y0 + dy)
                if slot is not None:
                    tiles.append(self._offset + slot)
        return tuple(tiles)

    def _cell_tile(self, layout: BlockLayout, cx: int, cy: int) -> int | None:
        """The absolute tile at canvas cell ``(cx, cy)`` — ``None`` if none lands
        there: a block-layout gap column, or a slot past the document's end.

        The single place the cell → slot → absolute-tile → in-bounds chain lives,
        shared by everything that writes through the arrangement (block transforms,
        block paste). Unlike :meth:`_rect_tiles_for`, it clamps to the document, so
        callers building a write get only tiles that actually exist.
        """
        assert self._doc is not None
        slot = layout.cell_to_slot(cx, cy)
        if slot is None:
            return None
        tile = self._offset + slot
        return tile if 0 <= tile < self._doc.tile_count else None

    def _on_tiles_selected(self, anchor_slot: int, moving_slot: int) -> None:
        """Select the pressed slot, or what a drag to ``moving_slot`` spans.

        Fired on press (anchor == moving) and again as a drag reaches other
        slots. The two slots describe either a linear run or the corners of a
        cell rectangle, per the Shape picker. Blank padding past the file is
        clamped out of a linear range; a press that *starts* there is ignored,
        as the single click always was.
        """
        if self._doc is None:
            return
        count = self._doc.tile_count
        if self._offset + min(anchor_slot, moving_slot) >= count:
            return
        if self._selection_shape.currentData() is SelectionShape.RECT:
            layout = self._view_layout()
            ax, ay = layout.slot_to_cell(anchor_slot)
            mx, my = layout.slot_to_cell(moving_slot)
            origin = layout.cell_to_slot(min(ax, mx), min(ay, my))
            if origin is None:
                return
            cells = (abs(mx - ax) + 1, abs(my - ay) + 1)
            tiles = self._rect_tiles_for(origin, *cells)
            if not tiles or tiles[0] >= count:
                return
            self._set_rect_selection(cells, tiles)
        else:
            first = self._offset + min(anchor_slot, moving_slot)
            last = min(self._offset + max(anchor_slot, moving_slot), count - 1)
            self._set_linear_selection(first, last)
        self._announce_selection()

    def _set_linear_selection(self, first: int, last: int) -> None:
        self._selected_tile, self._selected_last = first, last
        self._rect_cells, self._rect_tiles = None, ()
        self._after_selection_change()

    def _set_rect_selection(
        self, cells: tuple[int, int], tiles: tuple[int, ...]
    ) -> None:
        # The anchor is the top-left cell's tile, not the lowest index in the
        # block: under a column-major or interleaved arrangement those differ,
        # and everything anchored on the selection (paste, palette-from-selection)
        # means "where the user's rectangle starts on screen".
        self._selected_tile, self._selected_last = tiles[0], max(tiles)
        self._rect_cells, self._rect_tiles = cells, tiles
        self._after_selection_change()

    def _after_selection_change(self) -> None:
        self._update_selection_actions()
        self._refresh_selection(self._columns.value() * self._rows.value())
        self._refresh_hex()  # the hex highlight tracks the selection

    def _announce_selection(self) -> None:
        """Status-line summary of what is selected, in the shape it was made."""
        tiles = self._selection_tiles()
        if not tiles:
            return
        first = self._selected_tile
        assert first is not None
        at_first = self._format_offset(self._tile_byte_offset(first))
        if self._rect_cells is not None:
            cols, rows = self._rect_cells
            self.statusBar().showMessage(
                f"Selected {cols}×{rows} tiles ({len(tiles)}) from {at_first}"
            )
        elif len(tiles) == 1:
            self.statusBar().showMessage(f"Selected tile {first:,} at {at_first}")
        else:
            self.statusBar().showMessage(
                f"Selected tiles {first:,}–{tiles[-1]:,} ({len(tiles)} tiles) "
                f"from {at_first}"
            )

    def _clear_selection(self) -> None:
        self._selected_tile = None
        self._selected_last = None
        self._rect_cells, self._rect_tiles = None, ()
        self._canvas.set_selection(None)
        self._update_selection_actions()
        self._refresh_hex()

    def _update_selection_actions(self) -> None:
        """Converge everything gated on 'a selection exists' with the state."""
        has = self._selected_tile is not None
        self._sync_edit_actions()
        self._sync_transform_actions()
        self._palette_from_selection_action.setEnabled(has)
        # Only whole files spawn slices - slices never nest.
        current = self._workspace.current
        can_slice = current is not None and current.kind is EntryKind.FILE
        self._new_slice_from_selection_action.setEnabled(has and can_slice)
        self._files_panel.set_has_selection(has)

    # -- clipboard & pixel editing -----------------------------------------
    def _selection_tiles(self) -> list[int]:
        """Every selected tile, in selection order, clamped to the document.

        Selection order is storage order for a linear run and screen reading
        order for a rectangle - the order copies travel in either way.
        """
        if self._doc is None or self._selected_tile is None:
            return []
        count = self._doc.tile_count
        if self._rect_cells is not None:
            return [t for t in self._rect_tiles if 0 <= t < count]
        last = min(self._selected_last or self._selected_tile, count - 1)
        return list(range(self._selected_tile, last + 1))

    def _selection_offscreen(self) -> bool:
        """Whether the selection's anchor tile has scrolled out of the view.

        The anchor maps to a stamp cell through ``anchor - self._offset``, so a
        tile outside the visible window resolves to a cell off the grid and an
        import/paste anchored there lands nothing. Callers pull the selection
        back on-screen before stamping. False with no document or no selection -
        there is no off-screen anchor to correct.
        """
        if self._doc is None or self._selected_tile is None:
            return False
        window_tiles = self._columns.value() * self._rows.value()
        return not (0 <= self._selected_tile - self._offset < window_tiles)

    def _anchor_tile(self) -> int:
        """The tile the selection anchors on: the selected tile - the top-left
        cell of a rectangle - or the view's top-left tile when nothing is
        selected. Where a paste or an import lands."""
        return self._selected_tile if self._selected_tile is not None else self._offset

    def _stamp_anchor(self) -> int:
        """:meth:`_anchor_tile`, guaranteed on-screen.

        A stamp maps its anchor to a cell through ``anchor - self._offset``, so a
        selection scrolled out of the visible window resolves to a cell off the
        grid and writes nothing. Snap it onto the visible top-left tile first, so
        a paste or import lands where the user can see it. The single guard every
        stamping entry point (paste, Import from PNG, a dropped PNG) goes through.
        """
        if self._selection_offscreen():
            self._select_tiles(self._offset, self._offset)
        return self._anchor_tile()

    def _selection_bounding_run(self) -> tuple[int, int] | None:
        """The selection's *bounding* run as ``(first_tile, count)``.

        For a linear selection that is the selection itself; for a rectangle it
        is the enclosing span of the file, which is what the byte-oriented
        consumers (the hex highlight) have to work in. Operations that must not
        touch the gaps read :meth:`_selection_tiles` instead.
        """
        tiles = self._selection_tiles()
        if not tiles:
            return None
        first, last = min(tiles), max(tiles)
        return first, last - first + 1

    @staticmethod
    def _tiles_label(count: int) -> str:
        return f"{count:,} tile" + ("" if count == 1 else "s")

    def _is_direct_color(self) -> bool:
        """Whether the current interpretation stores colors, not palette indices."""
        if self._doc is None:
            return False
        try:
            return pipeline.pixel_is_direct_color(
                self._doc.pixel_config.interpret_preset_id, self._registry
            )
        except (KeyError, PipelineError):
            return False

    def _import_target(self) -> importer.ImportTarget:
        """The shape incoming pixels have to be fitted into: this view's format.

        The candidate colors are the **active subpalette window** - exactly the
        entries a tile can reference here - so a pasted color lands on an index
        that renders as that color in the view the user is looking at.
        """
        assert self._doc is not None
        direct = self._is_direct_color()
        space = self._index_space()
        base = self._subpalette.value() * space
        return importer.ImportTarget(
            tile_width=self._doc.tile_width,
            tile_height=self._doc.tile_height,
            colors=()
            if direct
            else tuple(self._doc.palette.color(base + i) for i in range(space)),
            direct_color=direct,
            block_columns=self._block_cols.value(),
            block_rows=self._block_rows.value(),
            block_order=self._block_order.currentData(),
        )

    def _blank_tiles(self, count: int) -> list:
        """``count`` empty tiles of this document's geometry - index 0 (or
        transparent black for direct color), what Clear and Cut leave behind."""
        assert self._doc is not None
        kind = ArgbGrid if self._is_direct_color() else IndexGrid
        return [kind(self._doc.tile_width, self._doc.tile_height) for _ in range(count)]

    def _decode_run(self, first: int, count: int) -> list | None:
        """Decode a tile run in the view's frame; None if the pipeline refuses."""
        assert self._doc is not None
        try:
            return pipeline.decode_tiles(
                self._doc,
                self._registry,
                first,
                count,
                nudge=self._nudge,
                columns=self._columns.value(),
                two_dimensional=self._two_d.isChecked(),
                anchor=self._offset,
            )
        except PipelineError as exc:
            self._report(exc)
            return None

    def _copy_selection(self) -> bool:
        """Put the selected tiles on the clipboard; False if there are none.

        Both representations go out at once (see :mod:`celpix.ui.clipboard`):
        the tiles themselves for a lossless paste back into Celpix, and a
        rendered image so every other program sees an ordinary picture. A
        rectangle selection copies only its own cells - the enclosing run is
        decoded (the file is linear), then the gap tiles are dropped.
        """
        if self._edit_mode is EditMode.PIXEL:
            self._pixel_copy()
            return True
        selected = self._selection_tiles()
        run = self._selection_bounding_run()
        if self._doc is None or run is None:
            return False
        first, count = run
        decoded = self._decode_run(first, count)
        if not decoded:
            return False
        tiles = [decoded[t - first] for t in selected if t - first < len(decoded)]
        if not tiles:
            return False
        target = self._import_target()
        cols = self._copy_columns(len(tiles))
        clipboard.put(
            clipboard.TilePayload.from_tiles(tiles, target.colors, columns=cols),
            self._copy_image(tiles, cols),
        )
        self._sync_edit_actions()
        self.statusBar().showMessage(f"Copied {self._tiles_label(len(tiles))}.")
        return True

    def _copy_columns(self, count: int) -> int:
        """How many cells wide a copy of ``count`` tiles reads on screen.

        A rectangle copies at its own width; a linear run wraps at the view's
        columns, or is a single short row when it doesn't reach that far.
        """
        if self._rect_cells is not None:
            return max(1, min(self._rect_cells[0], count))
        view_cols = self._columns.value()
        return view_cols if count > view_cols else max(1, count)

    def _copy_image(self, tiles: list, columns: int) -> QImage:
        """Render a copied run the way the canvas shows it.

        A linear run is laid out through the view's own arrangement, so a blocked
        view copies a 16×16 metatile as a square rather than as a strip of four
        tiles. A **rectangle** is already in screen order, so it composes plainly
        at its own width - re-applying the block layout would scramble it. Colors
        are the canvas's - no forced index-0 transparency, so a copy that goes out
        to an image editor and comes back matches its own palette exactly.
        """
        assert self._doc is not None
        layout = (
            BlockLayout(columns)
            if self._rect_cells is not None
            else BlockLayout(
                columns,
                self._block_cols.value(),
                self._block_rows.value(),
                self._block_order.currentData(),
            )
        )
        rows = 1 + max(layout.slot_to_cell(slot)[1] for slot in range(len(tiles)))
        grid = compose_window(tiles, columns, 0, rows, layout)
        base = self._subpalette.value() * self._index_space()
        return render_bridge.render(grid, self._doc.palette, base)

    def _blank_selection(self, text: str) -> int:
        """Blank every selected tile as one edit; returns how many were written.

        The edit is expressed over the selection's *enclosing* run because that
        is what encodes back to a contiguous byte region; a rectangle's gap tiles
        are decoded and written back unchanged, so only its own cells clear.
        """
        selected = self._selection_tiles()
        run = self._selection_bounding_run()
        if run is None:
            return 0
        first, count = run
        if len(selected) == count:  # contiguous - nothing to preserve
            tiles = self._blank_tiles(count)
        else:
            tiles = self._decode_run(first, count)
            if not tiles:
                return 0
            for blank, tile in zip(self._blank_tiles(len(selected)), selected):
                if tile - first < len(tiles):
                    tiles[tile - first] = blank
        written = self._apply_tile_edit(first, tiles, text)
        return sum(1 for tile in selected if tile - first < written)

    def _cut_selection(self) -> None:
        if self._edit_mode is EditMode.PIXEL:
            self._pixel_cut()
            return
        if not self._copy_selection():
            return
        written = self._blank_selection("cut tiles")
        if written:
            self.statusBar().showMessage(f"Cut {self._tiles_label(written)}.")

    def _clear_pixels(self) -> None:
        if self._edit_mode is EditMode.PIXEL:
            self._pixel_clear()
            return
        written = self._blank_selection("clear tiles")
        if written:
            self.statusBar().showMessage(f"Cleared {self._tiles_label(written)}.")

    def _paste(self) -> None:
        """Stamp the clipboard over the tiles from the selection anchor onward.

        Overwrite, never insert: the bytes sit in a fixed slot in the source
        file, so a paste replaces exactly as many tiles as it carries and is
        clipped at the end of the data. With nothing selected - or a selection
        scrolled off-screen (:meth:`_stamp_anchor`) - it lands at the top-left
        tile of the view.

        In **Rectangle** shape the anchor is a *cell*, so the clipboard is
        stamped as a block of its own width down from there - copy a 2×2
        metatile, click anywhere, and it lands as a 2×2 metatile. In Linear
        shape a paste is what it has always been: a contiguous run.
        """
        if self._doc is None:
            return
        if self._edit_mode is EditMode.PIXEL:
            self._pixel_paste()
            return
        first = self._stamp_anchor()
        incoming = self._clipboard_tiles()
        if not incoming.tiles:
            self.statusBar().showMessage("Nothing on the clipboard to paste here.")
            return
        note = self._fit_note(incoming.report)
        if self._selection_shape.currentData() is SelectionShape.RECT:
            written = self._stamp_block(first, incoming, "paste tiles")
        else:
            written = self._stamp_run(first, incoming, "paste tiles")
        if not written:
            self.statusBar().showMessage("Nothing pasted - no room at this offset.")
            return
        message = f"Pasted {self._tiles_label(written)}"
        if len(incoming.tiles) > written:
            clipped = len(incoming.tiles) - written
            message += f" ({clipped} clipped at the end of the data)"
        self.statusBar().showMessage(message + (f" - {note}." if note else "."))

    def _stamp_run(self, first: int, incoming: ImportedTiles, text: str) -> int:
        """Write ``incoming`` as a contiguous run from ``first`` - a linear paste.

        Partly covered edge tiles are filled out from the run already in the file
        before the write, so an image that doesn't end on a tile boundary leaves
        the pixels it never covered alone.
        """
        tiles = incoming.tiles
        if incoming.partial:
            existing = self._decode_run(first, len(tiles))
            if existing:
                tiles = [
                    importer.merge_uncovered(
                        tile,
                        existing[i] if i < len(existing) else None,
                        incoming.covered(i),
                    )
                    for i, tile in enumerate(tiles)
                ]
        written = self._apply_tile_edit(first, tiles, text)
        if written:
            self._select_tiles(first, first + written - 1)
        return written

    def _stamp_block(self, anchor: int, incoming: ImportedTiles, text: str) -> int:
        """Stamp ``incoming`` as a block of its own width at ``anchor``'s cell.

        The block's cells become absolute tiles through the view's arrangement,
        so the write lands where it looks like it lands; cells that fall off the
        right edge of the view are dropped rather than wrapped, since wrapping
        would scatter the block. The write itself goes out over the enclosing
        run, with the untouched tiles decoded and put back unchanged - and each
        partly covered edge tile merged with the one already there, so only the
        pixels the source actually reached change.
        """
        assert self._doc is not None
        columns = max(1, incoming.columns)
        layout = self._view_layout()
        x0, y0 = layout.slot_to_cell(anchor - self._offset)
        placed: dict[int, tuple[object, tuple[int, int] | None]] = {}
        for i, tile in enumerate(incoming.tiles):
            covered = incoming.covered(i)
            if covered == (0, 0):
                continue  # a block-layout gap the image never reached
            target = self._cell_tile(layout, x0 + i % columns, y0 + i // columns)
            if target is not None:
                placed[target] = (tile, covered)
        if not placed:
            return 0
        first, last = min(placed), max(placed)

        def mutate(run: list) -> None:
            for target, (tile, covered) in placed.items():
                if target - first < len(run):
                    run[target - first] = importer.merge_uncovered(
                        tile, run[target - first], covered
                    )

        if not self._edit_run(first, last - first + 1, mutate, text):
            return 0
        rows = 1 + (len(incoming.tiles) - 1) // columns
        cells = (columns, rows)
        rect = self._rect_tiles_for(anchor - self._offset, *cells)
        if rect:
            self._set_rect_selection(cells, rect)
        return len(placed)

    def _clipboard_tiles(self) -> ImportedTiles:
        """The clipboard as tiles in this document's format, with the fit report
        and how many cells wide the copy read on screen (what a block stamp uses
        as its width; ignored by a linear paste).

        Three ways in, in decreasing fidelity:

        1. A Celpix copy of the same tile geometry whose indices fit this
           format's index space - used **verbatim**. Indices are the data; a
           copy between two spots in a ROM must move them untouched, whatever
           palette either view happens to render through.
        2. A Celpix copy that doesn't fit (a 4bpp run into a 2bpp view) - its
           own palette turns the indices back into colors, which are re-matched
           into this view's subpalette.
        3. Anything else on the clipboard that is an image - the import pathway
           (:mod:`celpix.pipeline.importer`), quantized to the subpalette. This
           is the cross-application case, shared with PNG import.

        The first two carry whole tiles, so they report no partial coverage; only
        an image can stop part-way into an edge tile.
        """
        assert self._doc is not None
        target = self._import_target()
        payload = clipboard.take_payload()
        same_geometry = payload is not None and (
            payload.tile_width == self._doc.tile_width
            and payload.tile_height == self._doc.tile_height
        )
        if payload is not None and same_geometry:
            fits = payload.max_index < len(target.colors)
            if payload.direct_color == target.direct_color and (
                target.direct_color or fits
            ):
                tiles = payload.tiles()
                return ImportedTiles(tiles, payload.columns, 0, QuantizeReport())
            if not payload.direct_color:
                tiles, report = importer.import_indexed(
                    payload.tiles(), payload.colors, target
                )
                return ImportedTiles(tiles, payload.columns, 0, report)
            # A direct-color copy into an indexed view: fall through to the
            # image, which the same copy also put on the clipboard.
        image = clipboard.take_image()
        if image is None:
            return ImportedTiles()
        # A foreign image has no tile grid of its own; import_argb takes its own
        # pixel width in whole tiles as the block width it visibly has.
        return importer.import_argb(clipboard.image_to_argb(image), target)

    @staticmethod
    def _fit_note(report: QuantizeReport) -> str:
        """How faithfully an import landed, for the status line."""
        if report.source_colors == 0:
            return ""
        if report.lossless:
            return f"all {report.source_colors} colors matched exactly"
        return (
            f"{report.approximated_colors} of {report.source_colors} "
            "colors approximated"
        )

    def _edit_run(
        self, first: int, count: int, mutate: Callable[[list], None], text: str
    ) -> int:
        """Decode the run at ``first``, let ``mutate`` rewrite it, push one edit.

        The shared spine of every pixel edit that reworks *existing* tiles — a
        transform, a merged stamp — which differ only in how they mutate the
        decoded list. Untouched tiles between the edited ones are decoded here and
        written straight back, so a rectangle's gaps ride along unchanged. Returns
        how many tiles were written (0 if the run won't decode). ``mutate`` gets the
        decoded list in place and may read the originals it overwrites — snapshot
        first if source and destination overlap (a block permutation does).
        """
        decoded = self._decode_run(first, count)
        if not decoded:
            return 0
        mutate(decoded)
        return self._apply_tile_edit(first, decoded, text)

    def _map_selected_tiles(self, fn: Callable[[object], object], text: str) -> int:
        """Rewrite each selected tile through ``fn(tile) -> tile`` as one edit.

        The write covers the selection's enclosing run — what encodes back to a
        contiguous byte region — but only the selected tiles pass through ``fn``, so
        a rectangle's gap tiles are left exactly as they were. Returns how many tiles
        the edit wrote (0 with nothing selected, or if the run won't decode).
        """
        selected = self._selection_tiles()
        run = self._selection_bounding_run()
        if run is None:
            return 0
        first, count = run

        def mutate(decoded: list) -> None:
            for tile in selected:
                idx = tile - first
                if 0 <= idx < len(decoded):
                    decoded[idx] = fn(decoded[idx])

        return self._edit_run(first, count, mutate, text)

    def _apply_tile_edit(self, first: int, tiles: list, text: str) -> int:
        """Encode ``tiles`` over the run at ``first`` as one undoable edit.

        Returns how many tiles were written - fewer than offered when the run
        would overrun the data (editing never grows a file). An edit that would
        write back the bytes already there is skipped rather than pushed, so a
        redundant paste doesn't clutter the history.
        """
        assert self._doc is not None
        entry = self._workspace.current
        if entry is None or self._applying_undo:
            return 0
        tiles = tiles[: max(0, self._doc.tile_count - first)]
        if not tiles:
            return 0
        try:
            start, data = pipeline.encode_tiles(
                self._doc,
                self._registry,
                first,
                tiles,
                nudge=self._nudge,
                columns=self._columns.value(),
                two_dimensional=self._two_d.isChecked(),
                anchor=self._offset,
            )
        except PipelineError as exc:
            self._report(exc)
            return 0
        if not data:
            return 0
        before = self._doc.pixel_data[start : start + len(data)]
        if before != data:
            self._push_command(
                PixelEditCommand(
                    self, entry, text, start=start, before=before, after=data
                )
            )
        return len(tiles)

    def _apply_pixel_bytes(self, start: int, data: bytes, revision: int) -> None:
        """Land one pixel-byte region - :class:`PixelEditCommand`'s apply.

        The decompressed bytes are the document's source of truth, so an edit is
        a splice into them and Write picks it up from there. ``revision`` is the
        command's token for the state it just produced: stamping it on the
        *pixel* pathway makes the entry read dirty against what was last
        written, so an undo back to those bytes reports clean again.
        """
        if self._doc is None:
            return
        self._doc.replace_bytes(start, data)
        entry = self._workspace.current
        if entry is not None:
            self._workspace.set_pixel_revision(entry, revision)
        self._refresh_view()

    def _select_tiles(self, first: int, last: int) -> None:
        """Set a linear selection directly (an edit landing, not a gesture)."""
        if self._doc is None:
            return
        self._set_linear_selection(first, last)

    def _select_all(self) -> None:
        """Select every tile of the visible window.

        Scoped to the window, not the file: the selection is what Copy acts on,
        and selecting a multi-megabyte ROM would mean decoding and rendering the
        whole thing onto the clipboard.
        """
        if self._doc is None:
            return
        if self._edit_mode is EditMode.PIXEL:
            self._pixel_select_all()
            return
        count = min(
            self._columns.value() * self._rows.value(),
            self._doc.tile_count - self._offset,
        )
        if count <= 0:
            return
        self._select_tiles(self._offset, self._offset + count - 1)

    def _show_canvas_menu(self, pos: QPoint) -> None:
        """The canvas's right-click menu - the same QActions the Edit, Palette
        and File menus hold, gathered around what the selection can become.

        Suppressed in pixel mode, where right-click (and a right-drag sweep) is
        the eyedropper: a popup here would swallow the sample gesture.
        """
        if self._doc is None or self._edit_mode is EditMode.PIXEL:
            return
        self._sync_edit_actions()
        menu = QMenu(self)
        for action in self._clipboard_actions():
            menu.addAction(action)
        menu.addSeparator()
        # Built here rather than shared: import has no shortcut and no other
        # menu shows it, so there is nothing to keep in sync.
        import_png = menu.addAction("Import from PNG…")
        import_png.setToolTip(
            "Fit an image into this format and stamp it at the selected tile"
        )
        import_png.triggered.connect(self._import_png_here)
        menu.addSeparator()
        menu.addAction(self._palette_from_selection_action)
        menu.addAction(self._new_slice_from_selection_action)
        menu.exec(self._canvas.mapToGlobal(pos))

    def _selection_byte_range(self) -> tuple[int, int] | None:
        """The selection's enclosing ``(start, length)`` byte range in the
        document, or None with nothing selected - the hex panel's highlight.

        Same tile→byte mapping as New Slice from Selection: tiles are laid out
        linearly at ``bytes_per_tile`` each, shifted by the grid's byte nudge.
        A rectangle highlights the span it *encloses* - the bytes its rows are
        spread across - since a byte range is all the hex dump can shade.
        """
        assert self._doc is not None
        run = self._selection_bounding_run()
        if run is None:
            return None
        first, count = run
        tb = self._doc.bytes_per_tile
        return self._nudge + first * tb, count * tb

    def _refresh_selection(self, window_tiles: int) -> None:
        """Re-derive the canvas highlight after the window moved or resized.

        Scrolling away hides the highlight but keeps the selection, so scrolling
        back restores it; a selection half in view paints just its visible part.
        A selection starting past the file's end (file shrank) is dropped, one
        merely running past it is trimmed.

        A **rectangle** additionally has to survive the view changing under it.
        Its cells are re-resolved against the current columns/arrangement, and if
        they no longer land on the tiles that were selected - a column count or
        block layout that shuffles the picture - the rectangle is collapsed to
        its top-left tile rather than left pointing at whatever moved underneath.
        """
        assert self._doc is not None
        if self._selected_tile is not None:
            if self._selected_tile >= self._doc.tile_count:
                self._clear_selection()
                return
            if self._rect_cells is not None:
                self._revalidate_rect()
            else:
                self._selected_last = min(
                    self._selected_last or self._selected_tile,
                    self._doc.tile_count - 1,
                )
        slots = {
            tile - self._offset
            for tile in self._selection_tiles()
            if 0 <= tile - self._offset < window_tiles
        }
        self._canvas.set_selection(slots, as_block=self._rect_cells is not None)

    def _revalidate_rect(self) -> None:
        """Collapse the rectangle selection unless its cells still cover its tiles."""
        assert self._rect_cells is not None
        origin = self._selected_tile
        assert origin is not None
        if self._rect_tiles_for(origin - self._offset, *self._rect_cells) != (
            self._rect_tiles
        ):
            self._selected_last = origin
            self._rect_cells, self._rect_tiles = None, ()
            self._update_selection_actions()
