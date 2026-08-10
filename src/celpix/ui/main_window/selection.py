"""What is selected on the canvas, and the Edit menu the selection arms.

Selection is display state and lives here rather than in ``ViewOptions``: it
does not affect how the window renders. It is held as **absolute tile indices**
so it survives scrolling, with a rectangle additionally recording the cells it
was drawn over and the tiles those resolved to.

The shape (:class:`SelectionShape`) decides what a drag means, and the two
shapes are genuinely different things - a linear run maps onto one byte range,
while a rectangle narrower than the view is *disjoint in the file*. Everything
that has to work in bytes (the hex highlight, a new slice) reads the enclosing
run (:meth:`~SelectionMixin._selection_bounding_run`); everything that must not
touch the gaps reads the tile list (:meth:`~SelectionMixin._selection_tiles`).
Both answers are here, and so is the pair of guards that keeps them honest when
the view they were drawn under changes underneath them
(:meth:`~SelectionMixin._revalidate_selection`).

The module also builds the Edit menu and the clipboard actions, because arming
them is a question about the selection - what there is to copy, whether the
modes can be switched - rather than about what the verbs do. What they *do* is
:mod:`~celpix.ui.main_window.clipboard_ops`, and the bytes underneath that are
:mod:`~celpix.ui.main_window.tile_bytes`; what a cell selection means on a
tilemap is :mod:`~celpix.ui.main_window.tilemap_edit`'s.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import Enum

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import (
    QAction,
    QGuiApplication,
    QKeySequence,
)
from PySide6.QtWidgets import (
    QMenu,
)

from celpix.core.argb_grid import ArgbGrid
from celpix.core.arrangement import (
    BlockLayout,
)
from celpix.core.capabilities import Capability, ContentKind
from celpix.core.errors import PipelineError
from celpix.core.index_grid import IndexGrid
from celpix.core.tilemap import Cell
from celpix.pipeline import importer, pipeline
from celpix.project.workspace import (
    EntryKind,
)
from celpix.ui import clipboard
from celpix.ui.tools import EditMode
from celpix.ui.widgets import (
    counted,
    load_enum_setting,
    save_enum_setting,
    select_combo_data,
    signals_blocked,
)

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
    """What is selected on the canvas, and the Edit menu over it.

    A slice of :class:`~celpix.ui.main_window.window.MainWindow`, not a
    standalone object: it reads and writes the window's own widgets and its
    single live ``_doc``. See the module docstring for what it owns, and the
    package docstring for why these are mixins.
    """

    def _build_edit_menu(self) -> None:
        """Edit ▸ Undo/Redo, the clipboard actions, Import from PNG, and the
        mode switches.

        Undo/Redo are stack-provided (label and enabled state come from the
        unified session stack). The clipboard group operates on the selected
        tile run - see :meth:`~...clipboard_ops.ClipboardOpsMixin.
        _copy_selection` for what a copy actually puts on the clipboard. The
        four switches at the end are bare-key toggles whose home is elsewhere on
        screen (the transform bar); the menu is where they are named and their
        keys written down.
        """
        menu = self.menuBar().addMenu("&Edit")
        undo = self._undo_stack.createUndoAction(self)
        undo.setShortcut(QKeySequence.StandardKey.Undo)  # Ctrl+Z
        redo = self._undo_stack.createRedoAction(self)
        # The stack rewrites these labels to name the pending command ("Undo
        # Paste"), which reads as noise in the shortcut guide - pin the plain
        # name for it (see celpix.ui.help_dialogs).
        undo.setProperty("guideLabel", "Undo")
        redo.setProperty("guideLabel", "Redo")
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
        menu.addAction(self._import_png_action)
        menu.addSeparator()
        menu.addAction(self._select_all_action)
        menu.addSeparator()
        menu.addAction(self._toggle_selection_mode_action)
        menu.addAction(self._toggle_edit_mode_action)
        # The rearrange pair: the tool - as a plain row like the two mode toggles
        # above it, the transform bar holding the checkable button form of it
        # (see _build_rearrange_actions) - and the view toggle, which lives here
        # alone. Both are listed so the shortcut guide - which reads the menu bar
        # - documents R/Shift+R by the same route every other key is.
        menu.addAction(self._toggle_rearrange_action)
        menu.addAction(self._show_rearranged_action)
        # The stamp tool's row, on the same terms: the transform bar holds its
        # checkable button, and this is what documents T for the F1 guide, which
        # reads the menu bar. Hidden off a tilemap rather than greyed - a mode
        # for placing cells is not a feature switched off on a pixel document.
        menu.addAction(self._toggle_stamp_action)
        # Enabled state depends on the clipboard's contents, which any other
        # program can change while we sit idle - so track the signal rather than
        # only recomputing when the menu opens.
        clip = QGuiApplication.clipboard()
        if clip is not None:
            clip.dataChanged.connect(self._sync_edit_actions)
        menu.aboutToShow.connect(self._sync_edit_actions)

    def _build_clipboard_actions(self) -> None:
        """Create the Cut/Copy/Paste/Clear/Select All and Import from PNG actions.

        Built before the menus so both the Edit menu and the canvas's context
        menu can show the *same* QAction objects - one enabled state, one
        shortcut, wherever they appear. Added to the window itself so their
        shortcuts fire regardless of which menu is open.
        """
        # "&" marks the mnemonic. These actions appear in both the Edit menu and
        # the canvas's right-click menu, so a letter has to stay free of what
        # either one puts beside them - hence Copy taking the "C" and Clear the
        # "r", and Paste the "a" rather than the "P" the canvas menu's Palette
        # from Selection holds (its own shortcut key; Paste's "V" is not a letter
        # of its label, and a mnemonic can only be one of those).
        specs = (
            ("_cut_action", "Cu&t", QKeySequence.StandardKey.Cut, self._cut_selection),
            (
                "_copy_action",
                "&Copy",
                QKeySequence.StandardKey.Copy,
                self._copy_selection,
            ),
            ("_paste_action", "P&aste", QKeySequence.StandardKey.Paste, self._paste),
            (
                "_clear_action",
                "Clea&r",
                QKeySequence.StandardKey.Delete,
                self._clear_selection_contents,
            ),
            (
                "_select_all_action",
                "Select A&ll",
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
        # Import from PNG travels with this group rather than with File ▸ Open:
        # it stamps an image at the selection's anchor, which is the gesture
        # Paste makes from a different source. Shared for the same reason the
        # four above are - the Edit menu and the canvas menu show one action.
        self._import_png_action = QAction("&Import from PNG…", self)
        self._import_png_action.setToolTip(
            "Fit an image into this format and stamp it\nat the selected tile"
        )
        self._import_png_action.triggered.connect(self._import_png_here)
        self._import_png_action.setEnabled(False)
        self._build_mode_toggle_actions()

    def _build_mode_toggle_actions(self) -> None:
        """The Edit ▸ mode toggles, on the bare ``S`` and ``E`` keys.

        Display-only shortcuts, like View ▸ Grid: the bare letters are routed by
        the app-wide event filter (``_handle_nav_key``), which yields to focused
        text inputs — a live shortcut here would steal them mid-word. What the
        letters actually press is the control each row stands for — the Selection
        Shape picker and the Pixel Mode button
        (:class:`~...navigation.KeyControl`) — so these two rows carry the key's
        *label*, for the menu and the F1 guide, and are greyed in step with it
        (:meth:`_sync_edit_actions`).
        """
        specs = (
            (
                "_toggle_selection_mode_action",
                "Toggle &Selection Mode",
                "S",
                "Swap Linear / Rectangle selection",
                self._toggle_selection_mode,
            ),
            (
                "_toggle_edit_mode_action",
                "Toggle &Edit Mode",
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

        Inert wherever Rectangle is forced (see :meth:`_sync_selection_shape`) —
        the combo carries the preference and its change handler does the rest of
        the work.

        Set through the combo's **own signal**, not :func:`select_combo_data`:
        this is the user choosing a shape, so it has to reach
        :meth:`_on_selection_shape_change` — the one place that persists the
        preference and collapses the selection. Blocking the signal would leave a
        shape on screen that nothing remembers, and the next document that forces
        Rectangle would hand back the stale stored one.
        """
        if not self._can_toggle_selection_mode():
            return
        current = self._selection_shape.currentData()
        shape = (
            SelectionShape.LINEAR
            if current is SelectionShape.RECT
            else SelectionShape.RECT
        )
        index = self._selection_shape.findData(shape)
        if index >= 0:
            self._selection_shape.setCurrentIndex(index)

    def _toggle_edit_mode(self) -> None:
        """Swap tile ⇄ pixel editing, when a document is open to edit."""
        if not self._can_toggle_edit_mode():
            return
        self._set_edit_mode(
            EditMode.TILE if self._edit_mode is EditMode.PIXEL else EditMode.PIXEL
        )

    def _can_toggle_selection_mode(self) -> bool:
        """Whether a swap is on offer — asked of the picker, not re-derived.

        Everything that leaves only one shape (pixel editing, the rearrange tool,
        a tilemap, no document at all) already disables the combo, either
        directly (:meth:`_sync_selection_shape`) or by greying the transform bar
        it sits on — and a QWidget's ``isEnabled`` answers for both. Asking it is
        what stops the menu row and the ``S`` key from offering a swap the picker
        is refusing, which is what a second copy of the conditions did.
        """
        return self._selection_shape.isEnabled()

    def _can_toggle_edit_mode(self) -> bool:
        return self._doc is not None and self._pixel_edit_available()

    def _pixel_edit_available(self) -> bool:
        """Whether the document on screen has pixels a gesture could paint.

        The **kind**'s answer sharpened by the document's, which is the shape the
        rearrange tool's availability already has: a capability can only say what
        is true of every entry of a kind, and two tilemaps differ here
        (``docs/design/tilemap-entry.md`` §4).

        A pixel document always qualifies. A tilemap — of either shape — qualifies
        when it has **a bank to write into**: the art belongs to the bound entry,
        so an unbound map, or one whose binding no longer names anything, has
        nothing to deposit into and must not offer a brush over a picture of
        placeholders.

        A sprite object qualifies on the same terms as a grid map, which it did
        not always: what a pixel belongs to there is an overlap order rather than
        a slot, and that is now answered rather than avoided
        (:func:`~celpix.pipeline.pipeline.sprite_hit`, §8.5). The question is per
        *pixel* on an object where a map can answer per slot, but it is the same
        question and it has the same answer — the piece the eyedropper samples is
        the piece the pen writes through.
        """
        doc = self._doc
        if doc is None or not self._can(Capability.PIXEL_EDIT):
            return False
        if not doc.is_tilemap:
            return True
        entry = self._workspace.current
        return entry is not None and self._tile_bank_owner(entry) is not None

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
        # Both spellings of the same toggle — the Edit menu's and the transform
        # bar's — settle here, because the answer moves with the *document* and
        # this pass is the one the refresh cycle runs. The capability gate used to
        # own the toolbar one, and can no longer: it answers per kind, and two
        # tilemaps differ (:meth:`_pixel_edit_available`).
        can_paint = self._can_toggle_edit_mode()
        self._toggle_edit_mode_action.setEnabled(can_paint)
        self._edit_mode_action.setEnabled(can_paint)
        has_doc = self._doc is not None
        # Pixel mode gates Cut/Copy/Clear on a pixel marquee, tile mode on a
        # tile run; everything else about the five is the same in both.
        target = (
            self._marquee if self._edit_mode is EditMode.PIXEL else self._selected_tile
        )
        # What a Cut or a Clear has to be able to **write**, which is a different
        # thing in each mode and has to be asked of the mode rather than of the
        # document alone.
        #
        # Tile mode blanks *cells*, and a tilemap whose cells are not what is on
        # screen has none to blank: a sprite object's are subsprites placed at
        # signed pixel offsets, so there is none under the cursor
        # (``Document.cells_editable``).
        #
        # Pixel mode blanks *pixels*, and that same object has them — its pieces
        # draw a bank's tiles exactly as a grid map's cells do
        # (``docs/design/tilemap-entry.md`` §8.5), which is the question
        # :meth:`_pixel_edit_available` already answers for the brush. Asking the
        # cells here offered the brush and refused to take back what it laid down.
        editable = (
            self._pixel_edit_available()
            if self._edit_mode is EditMode.PIXEL
            else not (has_doc and self._doc.is_tilemap) or self._doc.cells_editable
        )
        for action in (self._cut_action, self._clear_action):
            action.setEnabled(has_doc and target is not None and editable)
        # Copy is not one of them, because it is a **read**: every kind on screen
        # has something well-defined to lift, and where the cells are not it, the
        # picture is - a sprite object copies the pixels of its sheet
        # (:meth:`~...tilemap_edit.TilemapEditMixin._copy_sprite_pixels`).
        self._copy_action.setEnabled(has_doc and target is not None)
        # A tilemap pastes *cells* from its own in-app buffer, so the system
        # clipboard's contents say nothing about whether a paste here would do
        # anything — but only in tile mode. In pixel mode the same map pastes
        # pixels through :meth:`~...pixel_edit.PixelEditMixin._pixel_paste`, which
        # reads the system clipboard exactly as a pixel document's paste does, so
        # asking the cell buffer there greys out the only paste on offer.
        cells = (
            has_doc and self._doc.is_tilemap and self._edit_mode is not EditMode.PIXEL
        )
        self._paste_action.setEnabled(
            has_doc
            and editable
            and (self._has_cell_clipboard() if cells else clipboard.has_content())
        )
        # An import needs no selection: with none, it lands at the view's start.
        # The capability is asked here rather than left to the gating pass
        # because this method runs on every selection change and that pass does
        # not — a veto it applied at the end of the last render would be handed
        # back by the next click on a cell (``capability_sync._GATED_IN_PLACE``).
        self._import_png_action.setEnabled(
            has_doc and self._can(Capability.IMPORT_IMAGE)
        )
        self._select_all_action.setEnabled(has_doc)

    # -- tile selection ----------------------------------------------------
    def _grid_tilemap(self):  # noqa: ANN201 - a Document
        """The document on screen when its cells form a **grid**, else None.

        The one test the three helpers below share, so a tilemap is recognised
        the same way by all of them. A sprite object is deliberately not one: its
        subsprites sit at signed pixel offsets rather than in a grid, so no layout
        describes what is drawn there and none of the cell arithmetic applies
        (``docs/design/tilemap-entry.md`` §6, OBJ).
        """
        doc = self._doc
        if doc is None or not doc.is_tilemap or doc.is_sprite:
            return None
        return doc

    def _view_layout(self) -> BlockLayout:
        """The slot ↔ cell mapping the canvas is currently drawing with.

        On a **tilemap** that is the map's own layout rather than the arrangement
        axes: a cell is the block — which is what places a 2x2 metatile's four
        consecutive tiles as a square — and a row is that many tiles wider than
        Cols says. It has to be the layout
        :func:`~celpix.pipeline.pipeline.tilemap_tiles` composed the picture with,
        or the selection reads the canvas's slots off a different grid than the
        one they were placed on.

        A **sprite object** has no cells for its slots to be cells *of*: its
        subsprites
        sit at signed pixel offsets, so what is drawn is a sheet of plain tiles and
        the selection falls back to selecting those — the frame being the block they
        group into, which is the layout the render placed them under
        (:class:`~celpix.pipeline.pipeline.SpriteSheet`).
        """
        sheet = self._sprite_sheet()
        if sheet is not None:
            return BlockLayout(sheet.columns, *sheet.frame, "row")
        doc = self._grid_tilemap()
        if doc is not None:
            across, down = self._cell_unit()
            return BlockLayout(self._tilemap_columns() * across, across, down, "row")
        return BlockLayout(
            self._columns.value(),
            self._block_cols.value(),
            self._block_rows.value(),
            self._block_order.currentData(),
        )

    def _cell_unit(self) -> tuple[int, int]:
        """The block a selection snaps to, in canvas **slots** — i.e. in tiles.

        1x1 on a pixel document: the tile is the unit there, and every snap below
        reduces to what it always did. On a tilemap the unit is the map's own
        **cell**, which may be a 2x2 metatile — and a quarter of a cell is not
        something the file has. Everything a tilemap selection feeds works a whole
        cell at a time (:meth:`~...tilemap_edit.TilemapEditMixin._selected_cells`,
        the cell clipboard, a block flip), so selecting part of one would show a
        highlight that no edit through it could honour.
        """
        doc = self._grid_tilemap()
        if doc is None:
            return 1, 1
        across, down = doc.cell_tiles
        return max(1, across), max(1, down)

    def _grid_hidden(self) -> tuple[tuple[int, int, int, int], ...]:
        """The undrawn positions of the map on screen, as pixel rectangles.

        Beside :meth:`_grid_tilemap` because it answers for the same document and
        is wanted in the same places: the live preview composes its own grid and
        has to paint the map's blank positions over it exactly as the committed
        render does (:func:`~celpix.pipeline.pipeline.hidden_rects`). Empty for
        every document with no visibility bit, at the cost of one scan.
        """
        doc = self._grid_tilemap()
        if doc is None:
            return ()
        return pipeline.hidden_rects(doc, self._tilemap_columns())

    def _bank_tile_at_slot(
        self, slot: int, cells: list[Cell] | None = None
    ) -> tuple[Cell, int] | None:
        """Which tile of the bound bank canvas ``slot`` draws, and through which cell.

        The step a pixel edit on a tilemap turns on: the canvas places in tile
        slots, a slot names one tile of one cell, and the cell names where in the
        bank that tile lives. Both halves come from the document rather than being
        re-derived — :attr:`~celpix.core.document.Document.laid_out_cells` is what
        the renderer walked (so the assembly and a chained map's resolution are
        already in it) and
        :meth:`~celpix.core.document.Document.cell_tile_indices` is the walk it
        took (so the metatile stride, the index mask and the base index are the
        renderer's own). Reading either off anything else is how the edit ends up
        one tile from where the user pointed.

        The cell travels back with the index because the tile alone is not enough
        to write through: what is on screen has the cell's flips applied and its
        palette row folded into the indices, and both have to come off again.

        None where the slot draws nothing — past the last cell, a cell pointing
        outside the bank, or a position the map does **not draw**, all of which
        render blank and have no bytes to edit.

        The undrawn position is the one of those three that has a tile behind it
        and still refuses. What the user is pointing at there is the background,
        not a picture: the cell resolves and the composer even fills its tile in
        (``visible`` reaches no tile — :class:`~celpix.core.tilemap.Cell`), but the
        picture paints over it. Editing through it would write into a tile drawn
        *elsewhere* on the map, from a spot showing nothing, with the stroke
        invisible the whole way. An object's twin is a pixel no piece's box
        covers, which :func:`~celpix.pipeline.pipeline.sprite_hit` refuses the
        same way — one rule, asked per position on both shapes (§8.5).

        ``cells`` lets a caller resolving many slots hoist
        :attr:`~celpix.core.document.Document.laid_out_cells` out of its loop.
        That property *builds* a list on an assembled document, so asking per slot
        turns a fill into one pass over every cell in the file per pixel it
        touches. Callers that ask once are unaffected and pass nothing.
        """
        doc = self._grid_tilemap()
        if doc is None:
            return None
        position, ordinal = divmod(slot, max(1, doc.tiles_per_cell))
        if cells is None:
            cells = doc.laid_out_cells
        if not 0 <= position < len(cells):
            return None
        cell = cells[position]
        if not cell.visible:
            return None
        run = doc.cell_tile_indices(cell)
        if not 0 <= ordinal < len(run):
            return None
        index = run[ordinal]
        if not 0 <= index < doc.tile_count:
            return None
        return cell, index

    def _bank_tile_at_pixel(
        self,
        x: int,
        y: int,
        cells: list[Cell] | None = None,
        layout: BlockLayout | None = None,
    ) -> tuple[Cell, int] | None:
        """:meth:`_bank_tile_at_slot` for a canvas *pixel* — what the pen lands on.

        ``cells`` and ``layout`` are the same hoist :meth:`_bank_tile_at_slot`
        offers, one level up: a stroke resolves this per tile it touches, and both
        of them are rebuilt per call otherwise — the cell list on an assembled
        document, and a fresh :class:`BlockLayout` whose derived sizes are cached
        on the instance and so thrown away with it.
        """
        slot = self._slot_at_pixel(x, y, layout)
        return None if slot is None else self._bank_tile_at_slot(slot, cells)

    def _slot_at_pixel(
        self, x: int, y: int, layout: BlockLayout | None = None
    ) -> int | None:
        """The canvas tile slot the pixel ``(x, y)`` falls in, or None off the grid.

        The pixel → slot half of :meth:`_bank_tile_at_pixel`, on its own because
        a caller may want the slot rather than what the bank holds under it — the
        eyedropper's, which names the *cell* a pixel came from
        (:meth:`~...tile_source_dock.TileSourceDockMixin._point_source_at_pixel`).
        """
        tile_w, tile_h = self._pixel_tile_size()
        if tile_w <= 0 or tile_h <= 0:
            return None
        if layout is None:
            layout = self._view_layout()
        return layout.pos_to_slot(x // tile_w, y // tile_h)

    def _bank_pixel_at(self, x: int, y: int, hoist=None):  # noqa: ANN001, ANN201
        """The byte canvas pixel ``(x, y)`` draws: ``(owner, tile, tx, ty)`` or None.

        The **pixel**-grained twin of :meth:`_bank_tile_at_pixel`, and the one
        thing the pen, the eyedropper and a sprite's commit all need to agree
        about: which tile of the bound bank a pixel came from, where in that tile
        it sits with the drawing flips undone, and the *owner* that put it there —
        a cell on a grid map, a subsprite on an object. Only ``palette_row`` is
        read off the owner (:meth:`~...palette_regions.PaletteRegionsMixin.
        _cell_paint_base`), which the two kinds spell the same way.

        A grid map answers through its slots, since a cell's tiles land on the
        canvas's own tile grid. An object cannot: its pieces sit at signed pixel
        offsets that are mostly not 8-aligned and they overlap, so one canvas tile
        routinely holds parts of three (:func:`~celpix.pipeline.pipeline.
        sprite_hit`). That is why this is per pixel and not per slot — and why the
        sprite side of a stroke costs a resolution per pixel rather than per tile.

        ``hoist`` is whatever the caller lifted out of its loop, opaque here and
        built by :meth:`_bank_pixel_hoist`: the cell list and layout for a map,
        the sheet and the decoded bank for an object.
        """
        doc = self._doc
        if doc is None:
            return None
        tile_w, tile_h = self._pixel_tile_size()
        if tile_w <= 0 or tile_h <= 0:
            return None
        if doc.is_sprite:
            hit = pipeline.sprite_hit(
                doc, self._registry, self._tilemap_columns(), x, y, hoist=hoist
            )
            if hit is None or hit.tile is None:
                return None
            return hit.piece, hit.tile, hit.x, hit.y
        cells, layout = hoist or (None, None)
        found = self._bank_tile_at_pixel(x, y, cells, layout)
        if found is None:
            return None
        cell, index = found
        tx, ty = x % tile_w, y % tile_h
        if cell.flip_h:
            tx = tile_w - 1 - tx
        if cell.flip_v:
            ty = tile_h - 1 - ty
        return cell, index, tx, ty

    def _bank_pixel_hoist(self):  # noqa: ANN201 — an opaque pair
        """What :meth:`_bank_pixel_at` wants lifted out of a per-pixel loop.

        Both kinds rebuild something expensive per call — the cell list of an
        assembled map, or an object's sheet box and decoded bank — and a fill asks
        per pixel. Opaque on purpose: the caller carries it from one call to the
        next without knowing which kind it is holding.
        """
        doc = self._doc
        if doc is None:
            return None
        if doc.is_sprite:
            return pipeline.sprite_hoist(doc, self._registry, self._tilemap_columns())
        grid = self._grid_tilemap()
        return None if grid is None else (grid.laid_out_cells, self._view_layout())

    def _selection_extent(self) -> int:
        """How many canvas slots hold something a selection can name.

        A pixel document's slots are its own tiles, so the file's tile count is
        the bound. A tilemap's are its cells expanded into tiles, and
        ``tile_count`` there counts the **tile bank it borrows from** — which says
        nothing about how many cells there are. A 4096-cell screen over a
        256-tile bank is the ordinary case, and bounding it by the bank would put
        seven eighths of the map out of reach
        (``docs/design/tilemap-entry.md`` §8).

        A **sprite object**'s are the tiles of its drawn frames — the same
        distinction arrived at from the other side: the bank it borrows from says
        nothing about how big the sheet is, and the slots past the last frame hold
        no picture to select (:attr:`~celpix.pipeline.pipeline.SpriteSheet.slots`).
        """
        doc = self._doc
        if doc is None:
            return 0
        sheet = self._sprite_sheet()
        if sheet is not None:
            return sheet.slots
        grid = self._grid_tilemap()
        if grid is not None:
            return len(grid.drawn_cells) * grid.tiles_per_cell
        return doc.tile_count

    def _window_slots(self) -> int:
        """How many canvas slots the picture on screen has room for.

        The view window on a pixel document; the whole map on a tilemap, which is
        always drawn entire and has no window to page through. A sprite sheet is
        drawn entire too, and does have room past its last frame — the space beside
        a partial bottom row of frames — so it answers with the sheet rather than
        with the filled part of it.
        """
        if self._doc is None:
            return 0
        sheet = self._sprite_sheet()
        if sheet is not None:
            return sheet.columns * sheet.rows
        if self._grid_tilemap() is not None:
            return self._selection_extent()
        return self._columns.value() * self._view_rows()

    def _on_selection_shape_change(self) -> None:
        """Switching Linear ⇄ Rectangle collapses the selection to its anchor.

        Neither shape is a special case of the other - a linear run isn't a
        rectangle and a rectangle isn't a run - so reinterpreting the existing
        range would silently select tiles the user never dragged over. Falling
        back to the one tile they are demonstrably on is the honest conversion.
        """
        save_enum_setting(SELECTION_SHAPE_KEY, self._selection_shape.currentData())
        if self._doc is not None and self._selected_tile is not None:
            self._select_tiles(self._selected_tile, self._selected_tile)

    def _sync_selection_shape(self) -> None:
        """Force Rectangle where a run has no meaning, else restore the preference.

        Three things work on rectangles alone. **Pixel editing** selects an *area*
        of pixels. The **rearrange tool**'s drag carries a rectangle whose shape
        has to survive being put down somewhere else — a linear run is a run
        through *storage*, so the tiles it holds need not be adjacent on screen at
        all, and there is nothing coherent for a drag to pick up or land. And a
        **tilemap** is edited as the picture it draws: everything downstream of a
        selection there — the cell clipboard, a cell-selection flip, a stamp — acts
        on a rectangle of cells, so a run wrapping the map's rows would name a set
        no edit through it could treat as one thing. A sprite object goes with it:
        its sheet is composed from frames, so a run over it is not even the
        contiguous byte range that is Linear's whole justification.

        The picker is forced to Rectangle and disabled for any of them, with
        signals blocked so the user's own preference survives and comes back when
        they let go.

        A linear run already on screen collapses to its anchor tile rather than
        being reinterpreted as a rectangle — the same honest conversion
        :meth:`_on_selection_shape_change` makes when the user switches by hand.

        Only the *crossing* touches the combo. The kind is re-read on every
        render, so a pass that re-seeded the box each time would overwrite any
        shape set programmatically rather than through the picker's own handler
        (which is what persists it).
        """
        forced = (
            self._edit_mode is EditMode.PIXEL
            or self._rearranging
            or self._content_kind() is ContentKind.TILEMAP
        )
        if forced is not self._selection_shape_forced:
            self._selection_shape_forced = forced
            with signals_blocked(self._selection_shape):
                select_combo_data(
                    self._selection_shape,
                    SelectionShape.RECT
                    if forced
                    else load_enum_setting(SELECTION_SHAPE_KEY, SelectionShape.LINEAR),
                )
            if forced and self._rect_size is None and self._selected_tile is not None:
                self._select_tiles(self._selected_tile, self._selected_tile)
        self._selection_shape.setEnabled(not forced)
        # The keyboard route to the same swap has to go with it.
        self._sync_edit_actions()

    def _rect_tiles_for(
        self, origin_slot: int, cols: int, rows: int
    ) -> tuple[int, ...]:
        """Absolute tiles of the ``cols`` × ``rows`` cell rectangle at ``origin_slot``.

        Cell row-major - reading order on screen, which is the order a copy of a
        rectangle travels in and the order a paste stamps back. Cells that hold
        no tile (a partial-width block column) are skipped; the origin slot may
        be negative or past the window, so a rectangle stays computable while it
        is scrolled out of view.
        """
        layout = self._view_layout()
        x0, y0 = layout.slot_to_pos(origin_slot)
        tiles = []
        for dy in range(rows):
            for dx in range(cols):
                slot = layout.pos_to_slot(x0 + dx, y0 + dy)
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
        slot = layout.pos_to_slot(cx, cy)
        if slot is None:
            return None
        tile = self._offset + slot
        return tile if 0 <= tile < self._doc.tile_count else None

    def _on_slots_selected(self, anchor_slot: int, moving_slot: int) -> None:
        """Select the pressed slot, or what a drag to ``moving_slot`` spans.

        Fired on press (anchor == moving) and again as a drag reaches other
        slots. The two slots describe either a linear run or the corners of a
        cell rectangle, per the Shape picker. Blank padding past the file is
        clamped out of a linear range; a press that *starts* there is ignored,
        as the single click always was.

        Both shapes snap **outward to whole units** (:meth:`_cell_unit`) before
        anything is selected. That is a no-op on a pixel document, where the unit
        is the tile the slots already count in, and is what makes a tilemap of
        16x16 cells select one whole cell per click and grow a cell at a time.
        """
        if self._doc is None:
            return
        count = self._selection_extent()
        if self._offset + min(anchor_slot, moving_slot) >= count:
            return
        across, down = self._cell_unit()
        if self._selection_shape.currentData() is SelectionShape.RECT:
            layout = self._view_layout()
            ax, ay = layout.slot_to_pos(anchor_slot)
            mx, my = layout.slot_to_pos(moving_slot)
            x0, y0 = min(ax, mx) // across * across, min(ay, my) // down * down
            x1 = (max(ax, mx) // across + 1) * across
            y1 = (max(ay, my) // down + 1) * down
            origin = layout.pos_to_slot(x0, y0)
            if origin is None:
                return
            size = (x1 - x0, y1 - y0)
            tiles = self._rect_tiles_for(origin, *size)
            if not tiles or tiles[0] >= count:
                return
            self._set_rect_selection(size, tiles)
        else:
            # A cell's tiles are consecutive slots (``tilemap_tiles``), so a run
            # of whole cells is still one contiguous run.
            unit = across * down
            first = self._offset + min(anchor_slot, moving_slot) // unit * unit
            last = min(
                self._offset + max(anchor_slot, moving_slot) // unit * unit + unit - 1,
                count - 1,
            )
            self._set_linear_selection(first, last)
        self._announce_selection()

    def _set_linear_selection(self, first: int, last: int) -> None:
        self._selected_tile, self._selected_last = first, last
        self._rect_size, self._rect_tiles = None, ()
        self._after_selection_change()

    def _set_rect_selection(
        self, size: tuple[int, int], tiles: tuple[int, ...]
    ) -> None:
        # The anchor is the top-left cell's tile, not the lowest index in the
        # block: under a column-major or interleaved arrangement those differ,
        # and everything anchored on the selection (paste, palette-from-selection)
        # means "where the user's rectangle starts on screen".
        self._selected_tile, self._selected_last = tiles[0], max(tiles)
        self._rect_size, self._rect_tiles = size, tiles
        self._after_selection_change()

    def _after_selection_change(self) -> None:
        self._sync_selection_actions()
        self._revalidate_selection()
        self._refresh_hex()  # the hex highlight tracks the selection

    def _announce_selection(self) -> None:
        """Status-line summary of what is selected, in the shape it was made.

        One sentence shape throughout: **what** was selected, **where** it starts,
        and the total last. The count trails rather than sitting between the two,
        so the shape and the address read as one phrase instead of being split by
        a number belonging to neither — and it lands in the same place in every
        message, which is where the eye goes for it.
        """
        tiles = self._selection_tiles()
        if not tiles:
            return
        first = self._selected_tile
        assert first is not None
        if self._grid_tilemap() is not None:
            self._announce_cell_selection()
            return
        total = counted(len(tiles), "tile")
        if self._doc is not None and self._doc.is_sprite:
            # Counted, and no byte address: a slot here is a square of the *drawn*
            # sheet, and the bytes the tiles came from are the bank's - an offset
            # would point into somebody else's file (:meth:`_cell_byte_range`).
            if self._rect_size is not None:
                cols, rows = self._rect_size
                self.statusBar().showMessage(
                    f"Selected {cols}×{rows} tiles of the sheet ({total})"
                )
            else:
                self.statusBar().showMessage(f"Selected {total} of the sheet")
            return
        at_first = self._format_offset(self._tile_byte_offset(first))
        if self._rect_size is not None:
            cols, rows = self._rect_size
            self.statusBar().showMessage(
                f"Selected {cols}×{rows} tiles from {at_first} ({total})"
            )
        elif len(tiles) == 1:
            self.statusBar().showMessage(f"Selected tile {first:,} at {at_first}")
        else:
            self.statusBar().showMessage(
                f"Selected tiles {first:,}–{tiles[-1]:,} from {at_first} ({total})"
            )

    def _announce_cell_selection(self) -> None:
        """The same summary for a tilemap, counted in the unit it selects in.

        Cells rather than tiles, and no byte address: the selection names
        positions in the *map*, while the bytes the window is over are the tile
        bank it borrows from, so a file offset here would point at somebody
        else's data (:meth:`_selection_byte_range`). The grid position takes the
        address's place in the sentence — where a graphic says which byte the
        selection starts at, a map says which cell of the picture it starts on,
        the same "and where is it" the reading wants answered.

        The numbers are the cells' positions in the **file**, which is what the hex
        dump beside them highlights and what a save writes. On an assembled screen
        file those need not run consecutively across a rectangle, so the range is
        its lowest and highest rather than its first and last.
        """
        cells = self._selected_cells()
        if not cells:
            return
        at_first = self._cell_position_text(self._selected_positions()[0])
        total = counted(len(cells), "cell")
        if self._rect_size is not None:
            across, down = self._cell_unit()
            cols, rows = self._rect_size[0] // across, self._rect_size[1] // down
            self.statusBar().showMessage(
                f"Selected {cols}×{rows} cells from {at_first} ({total})"
            )
        elif len(cells) == 1:
            self.statusBar().showMessage(f"Selected cell {cells[0]:,} at {at_first}")
        else:
            self.statusBar().showMessage(
                f"Selected cells {min(cells):,}–{max(cells):,} "
                f"from {at_first} ({total})"
            )

    def _cell_position_text(self, position: int) -> str:
        """A drawn cell position as its ``(column, row)`` on the map.

        The **drawn** position, not the file index: an assembled screen file
        interleaves its pages, so the two disagree exactly where a coordinate is
        most wanted — and it is the picture the user is pointing at. Zero-based,
        like the cell numbers it sits beside.
        """
        columns = self._tilemap_columns()
        return f"({position % columns}, {position // columns})"

    def _clear_selection(self) -> None:
        self._selected_tile = None
        self._selected_last = None
        self._rect_size, self._rect_tiles = None, ()
        self._canvas.set_selection(None)
        self._sync_selection_actions()
        self._refresh_hex()

    def _sync_selection_actions(self) -> None:
        """Converge everything gated on 'a selection exists' with the state."""
        has = self._selected_tile is not None
        self._sync_edit_actions()
        self._sync_transform_actions()
        # The rearrange tool's groups act on the selection as well, and they are
        # not part of the sync above: they sit over *either* edit mode, so nothing
        # else would tell them a right-drag has just picked a different block.
        self._sync_rearrange_actions()
        # The binding bar's Cell spin *reads* the selection as well as writing it,
        # so it belongs to this pass and not to the refresh cycle: a selection
        # changes without anything being re-rendered, and left to the render it
        # would sit greyed over a live selection showing the cell before last
        # (:meth:`~...tilemap_bar.TilemapBarMixin._sync_cell_index`).
        self._sync_cell_index()
        # The tile source panel's ring is in the same position for the same
        # reason: it marks the tile the selected cell names, and a selection
        # moves without anything being re-rendered. The palette grid's pinned-row
        # mark is the third of the same kind.
        self._sync_tile_source_marker()
        self._sync_marked_palette_row()
        self._palette_from_selection_action.setEnabled(has)
        self._sync_pin_actions()
        # Only whole files spawn slices - slices never nest. Nor does a tilemap:
        # a slice is a pixel entry over a byte region, and the region a cell
        # selection names is a run of records in a map, which is not a thing the
        # pixel machinery on the other end of the dialog can open.
        current = self._workspace.current
        can_slice = (
            current is not None
            and current.kind is EntryKind.FILE
            and not (self._doc is not None and self._doc.is_tilemap)
        )
        self._new_slice_from_selection_action.setEnabled(has and can_slice)
        self._files_panel.set_has_selection(has)

    # -- what the selection is, to the edits over it ------------------------
    # The answers every clipboard verb, transform and import asks for before it
    # can act: which tiles, which enclosing run, and the document's own shape of
    # a tile. The verbs themselves are ``clipboard_ops``/``transform``.
    def _selection_tiles(self) -> list[int]:
        """Every selected tile, in selection order, clamped to the document.

        Selection order is storage order for a linear run and screen reading
        order for a rectangle - the order copies travel in either way.
        """
        if self._doc is None or self._selected_tile is None:
            return []
        count = self._selection_extent()
        if self._rect_size is not None:
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
        return not (0 <= self._selected_tile - self._offset < self._window_slots())

    def _anchor_tile(self) -> int:
        """The tile the selection anchors on: the selected tile - the top-left
        cell of a rectangle - or the view's top-left tile when nothing is
        selected. Where a paste or an import lands."""
        return self._selected_tile if self._selected_tile is not None else self._offset

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
        )

    def _blank_tiles(self, count: int) -> list:
        """``count`` empty tiles of this document's geometry - index 0 (or
        transparent black for direct color), what Clear and Cut leave behind."""
        assert self._doc is not None
        kind = ArgbGrid if self._is_direct_color() else IndexGrid
        return [kind(self._doc.tile_width, self._doc.tile_height) for _ in range(count)]

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

    def _select_tiles(self, first: int, last: int) -> None:
        """Set a linear selection directly (an edit landing, not a gesture)."""
        if self._doc is None:
            return
        self._set_linear_selection(first, last)

    def _select_all(self) -> None:
        """Select every tile of the visible window.

        Scoped to the window, not the file: the selection is what Copy acts on,
        and selecting a multi-megabyte ROM would mean decoding and rendering the
        whole thing onto the clipboard. On a tilemap the window *is* the file —
        it is always drawn entire — so this takes every cell.
        """
        if self._doc is None:
            return
        if self._edit_mode is EditMode.PIXEL:
            self._pixel_select_all()
            return
        count = min(self._window_slots(), self._selection_extent() - self._offset)
        if count <= 0:
            return
        self._select_tiles(self._offset, self._offset + count - 1)

    def _show_canvas_menu(self, pos: QPoint) -> None:
        """The canvas's right-click menu - the same QActions the Edit, Palette
        and File menus hold, gathered around what the selection can become.

        Suppressed in pixel mode, where right-click (and a right-drag sweep) is
        the eyedropper: a popup here would swallow the sample gesture. Suppressed
        while the rearrange tool is armed for the same reason — the right button
        carries the tile-selection drag there, the left one being busy picking
        tiles up — and while Edit Tiles is armed, where the right button picks
        the tile a cell names.
        """
        if self._doc is None or self._edit_mode is EditMode.PIXEL:
            return
        if self._rearranging or self._stamping:
            return
        self._sync_edit_actions()
        menu = QMenu(self)
        # Carving the file up comes first: all three ways to cut a slice, then a
        # bookmark. A bookmark records the *view position* rather than the
        # selection, but it belongs to the same "make something out of where I
        # am" group - and the canvas is where the user is when they decide to
        # mark the spot.
        menu.addAction(self._new_slice_action)
        menu.addAction(self._new_slice_from_view_action)
        menu.addAction(self._new_slice_from_selection_action)
        menu.addAction(self._new_bookmark_action)
        menu.addSeparator()
        for action in self._clipboard_actions():
            menu.addAction(action)
        menu.addSeparator()
        menu.addAction(self._import_png_action)
        menu.addSeparator()
        menu.addAction(self._palette_from_selection_action)
        menu.addAction(self._pin_palette_action)
        menu.addAction(self._unpin_palette_action)
        menu.addAction(self._unpin_all_action)
        menu.exec(self._canvas.mapToGlobal(pos))

    def _selection_byte_range(self) -> tuple[int, int] | None:
        """The selection's enclosing ``(start, length)`` byte range in the
        document, or None with nothing selected - the hex panel's highlight.

        Same tile→byte mapping as New Slice from Selection: tiles are laid out
        linearly at ``bytes_per_tile`` each, shifted by the grid's byte nudge.
        A rectangle highlights the span it *encloses* - the bytes its rows are
        spread across - since a byte range is all the hex dump can shade.

        A **tilemap** answers in its own cells' bytes (:meth:`_cell_byte_range`),
        which is the file its dump is showing.
        """
        assert self._doc is not None
        if self._doc.is_tilemap:
            return self._cell_byte_range()
        run = self._selection_bounding_run()
        if run is None:
            return None
        first, count = run
        tb = self._doc.bytes_per_tile
        return self._nudge + first * tb, count * tb

    def _cell_byte_range(self) -> tuple[int, int] | None:
        """The selected cells' enclosing ``(start, length)`` in the map's bytes.

        The tilemap counterpart of the range above, over the entry's **own** file
        rather than over the tiles it draws: cells are fixed-width records in file
        order, so a run of cell indices is a byte span. A rectangle spans the
        bytes it *encloses* for the same reason a rectangle of tiles does - its
        rows sit apart in the file, and a byte range is all a dump can shade.

        No nudge and no display base: both belong to the pixel view, and the range
        is an index into the buffer the dump is rendering
        (:meth:`~...rendering.RenderingMixin._refresh_tilemap_hex` adds the base
        when it labels a row).

        None for a sprite object - a canvas position there resolves to a *subsprite*
        through an overlap order rather than to a cell, so there is no record for
        the highlight to land on.

        The span is read off the lowest and highest cell rather than the first and
        last selected, because the selection is in *screen* order and an assembled
        screen file draws its pages side by side: a rectangle over the right-hand
        page starts at a higher record than one over the left, whichever was
        dragged first.
        """
        doc = self._grid_tilemap()
        if doc is None or doc.cell_bytes <= 0:
            return None
        cells = self._selected_cells()
        if not cells:
            return None
        first, last = min(cells), max(cells)
        return first * doc.cell_bytes, (last - first + 1) * doc.cell_bytes

    def _revalidate_selection(self) -> None:
        """Re-derive the canvas highlight after the window moved or resized.

        Scrolling away hides the highlight but keeps the selection, so scrolling
        back restores it; a selection half in view paints just its visible part.
        A selection starting past the end of what can be selected (the file
        shrank, or a map lost cells) is dropped, one merely running past it is
        trimmed - both against :meth:`_selection_extent`, which on a tilemap is
        its cells and not the bank it draws from.

        A **rectangle** additionally has to survive the view changing under it.
        Its cells are re-resolved against the current columns/arrangement, and if
        they no longer land on the tiles that were selected - a column count or
        block layout that shuffles the picture - the rectangle is collapsed to
        its top-left tile rather than left pointing at whatever moved underneath.
        """
        assert self._doc is not None
        extent = self._selection_extent()
        if self._selected_tile is not None:
            if self._selected_tile >= extent:
                self._clear_selection()
                return
            if self._rect_size is not None:
                self._revalidate_rect()
            else:
                self._selected_last = min(
                    self._selected_last or self._selected_tile, extent - 1
                )
        window_slots = self._window_slots()
        slots = {
            tile - self._offset
            for tile in self._selection_tiles()
            if 0 <= tile - self._offset < window_slots
        }
        self._canvas.set_selection(slots, as_rect=self._rect_size is not None)

    def _revalidate_rect(self) -> None:
        """Collapse the rectangle selection unless its cells still cover its tiles."""
        assert self._rect_size is not None
        origin = self._selected_tile
        assert origin is not None
        if self._rect_tiles_for(origin - self._offset, *self._rect_size) != (
            self._rect_tiles
        ):
            self._selected_last = origin
            self._rect_size, self._rect_tiles = None, ()
            self._sync_selection_actions()
