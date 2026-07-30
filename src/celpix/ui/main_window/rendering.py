"""The refresh cycle: current document + view options → what is on screen.

One entry point, :meth:`~RenderingMixin._refresh_view`, which every change that
can alter the picture funnels through — a widget move, an edit, an entry switch,
an undo. It settles the view axes into ``doc.view`` first and then reads the
render back *out* of it, so the stored ``ViewOptions`` is genuinely the input to
what is drawn rather than a mirror that can drift from it.

Decode is **deferred and windowed**: only the visible tiles' bytes are sliced out
and decoded, so the cost of a repaint follows the window rather than the file
(``docs/design/architecture.md`` §2). Two routes reach the canvas from there, and
which one runs is the only thing a rearrangement changes:

- **by bytes** — the ordinary path, and the one the decompression overlay shares
  (:meth:`~RenderingMixin._render_arrangement`): one contiguous window through 2D
  reflow, decode and block layout.
- **by tiles** (:meth:`~RenderingMixin._render_rearranged`) — when a rearrangement
  is in force the window's tiles come from wherever it sends them, which is not a
  contiguous slice, so they are gathered through the same ``_decode_run`` choke
  point every edit resolves the rearrangement with. That shared choke point is what
  keeps what is drawn and what is written in agreement.

The dependent surfaces (palette dock, hex dump, navbar, overlay) are refreshed
from the tail of the same cycle rather than each watching for its own trigger.
"""

from __future__ import annotations

from celpix.core import ceil_div
from celpix.core.arrangement import BlockLayout, tile_first_pixel
from celpix.core.document import ViewOptions
from celpix.core.palette import Palette
from celpix.core.tilemap import page_assemblies
from celpix.pipeline import pipeline
from celpix.ui import render_bridge
from celpix.ui.hex_view_panel import BYTES_PER_ROW
from celpix.ui.main_window.interpretation import (
    COLS_ASSEMBLED_TIP,
    COLS_TIP,
    SUBPAL_CELLS_TIP,
    SUBPAL_TIP,
)
from celpix.ui.widgets import select_combo_data, signals_blocked


class RenderingMixin:
    """Compose and paint the current view, and the surfaces that follow it.

    A slice of :class:`~celpix.ui.main_window.window.MainWindow`, not a
    standalone object: it drives the window's own widgets and its single live
    ``_doc``. See the module docstring for the two render routes, and the
    package docstring for why these are mixins.
    """

    def _on_view_change(self, *_args) -> None:
        if self._doc is not None:
            self._refresh_view()

    def _view_rows(self) -> int:
        """Tile-rows the window actually shows - the Rows setting, or the file.

        View ▸ Entire File (:meth:`MainWindow._on_entire_file_change`) lifts the
        window to every row the data fills whenever Rows would cut the file
        short, so the offset clamps to 0 and the whole file is on screen in one
        piece instead of being paged through. Only ever a *rise*: a file that
        already fits inside Rows is shown whole as it is, and shrinking the
        canvas to it would move the picture for no gain.

        Rows itself stays the user's own number throughout - it is the setting a
        project stores and the one the spin comes back to when the toggle goes
        off - so this, not the spin, is what every window calculation reads.
        """
        rows = self._rows.value()
        if self._doc is None or not self._entire_file.isChecked():
            return rows
        tb = self._doc.bytes_per_tile
        if not tb:
            return rows
        # The usable tile count the offset clamp works from: the nudge eats into
        # the data, and a trailing partial tile still renders (zero-padded).
        tiles = ceil_div(len(self._doc.pixel_data) - self._nudge, tb)
        return max(rows, ceil_div(tiles, max(1, self._columns.value())))

    def _render_arrangement(
        self,
        pixel_bytes: bytes,
        engine,  # noqa: ANN001 - a pixel-interpret plugin
        params,  # noqa: ANN001 - the preset's engine params
        layout: BlockLayout,
        two_dimensional: bool,
        max_rows: int | None,
        biases: list[int] | None = None,
    ):
        """Decode a pixel-byte buffer through the arrangement into a rendered image.

        The shared core of the live view and the decompression overlay, so blocks
        and 2D behave identically in both: 2D reflow → decode → block layout →
        render. ``pixel_bytes`` begins at the view origin - a window of the doc's
        bytes for the live view, a decompressed scratch for the overlay.
        ``max_rows`` caps the composed height (the live view's fixed window);
        ``None`` sizes to the data (the overlay shows the whole structure). Returns
        ``(QImage, real tile count)`` - the count excludes any 2D reflow padding, so
        the canvas can background the rest.

        ``biases`` carries pinned palette regions (:meth:`_window_biases`) and is
        supplied by the **caller** rather than computed here, because only the live
        view's bytes are the ones the regions address: the overlay renders a
        decompressed scratch buffer, whose positions are not offsets into the
        entry at all. Passing it also selects the render path - with biases the
        row is already in the indices, so the colour table must not offset again.
        """
        assert self._doc is not None
        grid, filled = pipeline.decode_and_compose(
            pixel_bytes, engine, params, layout, two_dimensional, max_rows, biases
        )
        if biases is not None:
            return render_bridge.render_pinned(grid, self._doc.palette), filled
        base = self._doc.view.subpalette_row * self._index_space()
        return render_bridge.render(grid, self._doc.palette, base), filled

    def _window_palette_rows(self, cols: int, rows: int) -> list[int | None] | None:
        """The **pinned** row of each visible slot, ``None`` where none is pinned.

        The whole of what pinned palette regions cost at render time. Each slot is
        resolved to the pixel its tile starts at, and that pixel is looked up in the
        regions.

        A slot in no region answers ``None`` rather than the view's own subpalette
        row, because the two callers want different things from it and only one of
        them can substitute. :meth:`_window_biases` puts the view's row back, since
        the picture has to be drawn through *something*; the row **labels** must not,
        or every unpinned tile in the window is numbered with the view's row — which
        is what the overlay is meant to distinguish the pinned few from
        (:meth:`~celpix.ui.canvas.TileCanvas.set_palette_rows`). Reading the label
        off the recolour hid that for as long as Subpal was 0, where the two agree.

        Returning **None for the whole list** on an unpinned document is
        load-bearing, and a different statement from the per-slot one: it is what
        keeps every existing view on the original single table path, allocating
        nothing and shifting nothing.

        Addressing follows the walk in force rather than assuming tiles are
        contiguous, so this is correct under the 2D wide-bitmap reading too - there
        the pixel space is the bitmap's own and
        :func:`~celpix.core.arrangement.tile_first_pixel` returns the tile's
        top-left. A rearrangement is resolved first, so a pinned tile keeps its row
        when it is dragged somewhere else: the region names where the tile *lives*,
        and a rearrangement moves display positions, not tiles.
        """
        assert self._doc is not None
        regions = self._active_palette_regions()
        if regions.is_empty():
            return None
        doc = self._doc
        view = doc.view
        per_tile = doc.tile_width * doc.tile_height
        tile_rearrangement = self._active_tile_rearrangement()
        count = cols * rows
        if tile_rearrangement.is_identity():
            # The ordinary case: the window is one contiguous run of slots, so the
            # walk's own addressing gives each one's first pixel directly.
            origin = view.tile_offset * per_tile
            offsets = [
                origin
                + tile_first_pixel(
                    slot,
                    doc.tile_width,
                    doc.tile_height,
                    cols,
                    view.two_dimensional,
                )
                for slot in range(count)
            ]
        else:
            # Rearranged (1D only - the tool is off under 2D), so each slot shows
            # whichever tile the rearrangement sends it, not the one it sits on.
            offsets = [
                actual * per_tile
                for actual in tile_rearrangement.actual_run(view.tile_offset, count)
            ]
        return regions.rows_for(offsets, None)

    def _window_biases(self, cols: int, rows: int) -> list[int] | None:
        """The rows above as index shifts — what the composer actually applies.

        Split from :meth:`_window_palette_rows` so the number drawn on a tile and
        the palette it is drawn through come from one computation: a label that
        could disagree with the recolour would be worse than no label.

        This is the end that fills the unpinned slots in, with the view's own row —
        every tile has to be drawn through some row, and an unpinned one is drawn
        through exactly the row it would be without any of this.
        """
        per_row = self._window_palette_rows(cols, rows)
        if per_row is None:
            return None
        space = self._index_space()
        view_row = self._doc.view.subpalette_row if self._doc is not None else 0
        return [(view_row if row is None else row) * space for row in per_row]

    def _tilemap_columns(self) -> int:
        """How many cells across the map is drawn — the Cols setting, clamped.

        The setting, not the format's own width: the width is put *into* Cols
        when the entry loads (:meth:`_apply_tilemap_columns`), so what is on
        screen is what the spin says and changing it works. Only bounded below,
        at 1, and above by the cell count — a width past the map is the same
        picture with empty space beside it, worth avoiding but not refusing.

        An **assembled** document is the exception, and the same one the renderer
        makes: its width is fixed by how its pages are laid out, so it comes from
        the document rather than the spin
        (:attr:`~celpix.core.document.Document.assembled_columns`). Asking the same
        authority is what keeps the selection reading the canvas off the grid the
        picture was actually placed on, instead of relying on a sync pass having
        already pushed that number into the spin.
        """
        assert self._doc is not None and self._doc.cells is not None
        count = len(self._doc.drawn_cells)
        columns = self._doc.assembled_columns or self._columns.value()
        return max(1, min(columns, count or 1))

    def _sprite_sheet(self):  # noqa: ANN201 - a pipeline.SpriteSheet
        """The frame layout of the sprite object on screen, or None for anything else.

        The single answer the render and the selection both take their grid from
        (:class:`~celpix.pipeline.pipeline.SpriteSheet`): a sprite object has no
        cells on the canvas, so what is placed and what is selected is the plain
        tile, and both sides have to count them the same way.
        """
        doc = self._doc
        if doc is None or not doc.is_sprite:
            return None
        return pipeline.sprite_sheet(doc, self._tilemap_columns())

    def _apply_tilemap_columns(self, entry, *, restored: bool) -> None:  # noqa: ANN001
        """Set Cols to the width ``entry``'s format states, if it states one.

        A screen and a panel are 32 cells across and a stamp layout 128; the file
        knows, and a wrong guess **shears** the picture into diagonal stripes
        rather than failing, which is the worst way to be wrong. Applied on load
        only, and skipped when a project ``restored`` a width of its own, so it is
        a starting point rather than something that fights the spin.

        Whether there *was* one is the caller's to say, not something to read off
        the entry here: the restore has already run by this point and consumed the
        pending view it would have been read from
        (:meth:`~...session.SessionMixin._apply_restored_state`).
        """
        width = self._tilemap_columns_hint(entry)
        if width and not restored:
            doc = entry.doc
            # A paged file's width is its assembly's, not one page's — and the
            # document is where that is decided, so this seeds Cols with the same
            # number the layout is about to use rather than one page's worth of it.
            if doc is not None and doc.assembled_columns:
                width = doc.assembled_columns
            with signals_blocked(self._columns):
                self._columns.setValue(width)
            if doc is not None:
                doc.view.columns = width

    def _settle_tilemap_assembly(self) -> None:
        """Fill the assembly picker, and let it own Cols while it applies.

        The tilemap counterpart of :meth:`_settle_bitmap_width_and_columns`, and
        it runs after that one for the same reason it exists: both take the column
        count over, and the last word has to be one of them. A paged tilemap wins
        because its width is not a preference at all — pages are cut at a fixed
        size, so any column count but ``pages_across × page width`` splits them at
        the wrong place and shears the picture into diagonal stripes
        (``docs/design/tilemap-entry.md`` §6).

        The picker is **hidden** rather than disabled on a document with no pages,
        which today is everything but a screen file: an assembly is not a setting
        those have and switched off, it is a question they do not raise. That is
        why it is not in the capability table either — being paged is a property
        of the *format*, not of the content kind, exactly as carrying palette rows
        is (:meth:`_sync_subpalette`), and it is the rule the whole binding bar it
        now sits on is built on (``tilemap_bar``).

        Hidden from **here** rather than from the bar's own refresh, unlike its
        neighbours, because this pass has to run before the render either way — it
        settles Cols — and the bar's refresh runs at the tail of the cycle. Doing it
        twice would be two answers to one question, and nothing later puts it back.

        The document's own ``view.pages_across`` is the source of truth and the
        combo mirrors it, so an entry switch shows that entry's assembly even when
        the option list is identical, and the handler's write is what a refresh
        reads back.
        """
        doc = self._doc
        pages = doc.pages if doc is not None else 0
        self._assembly_label.setVisible(bool(pages))
        self._assembly.setVisible(bool(pages))
        if not pages or doc is None:
            # Cols is left exactly as the bitmap-width pass set it: taking it over
            # is the only thing this pass does to it, so there is nothing to hand
            # back that the earlier owner has not already decided.
            self._label_columns(locked=False)
            return
        self._fill_assembly_combo(pages, doc.pages_across)
        self._columns.setEnabled(False)
        self._label_columns(locked=True)
        # The spin **mirrors** the width rather than setting it: the layout and the
        # selection both take it from the document, so this is what puts the number
        # where the user can read it (and where a project stores it).
        width = doc.assembled_columns
        if self._columns.value() != width:
            with signals_blocked(self._columns):
                self._columns.setValue(width)

    def _label_columns(self, *, locked: bool) -> None:
        """Say what Cols is, or what has taken it over — caption included.

        The caption is half the control's hover target
        (:func:`~celpix.ui.widgets.add_labelled`), so a live-looking label over a
        dead input is exactly where "why can't I type here" lands.
        """
        tip = COLS_ASSEMBLED_TIP if locked else COLS_TIP
        self._columns.setToolTip(tip)
        self._columns_label.setToolTip(tip)
        self._columns_label.setEnabled(self._columns.isEnabled())

    def _fill_assembly_combo(self, pages: int, across: int) -> None:
        """Put this file's assemblies in the picker and select the one in force.

        Only the arrangements that show every page (:func:`~celpix.core.tilemap.
        page_assemblies`), labelled as the grid of pages they make — ``2x2`` for a
        screen file's four screens. Refilled only when the options actually differ,
        so switching between two screen files does not rebuild an identical list;
        the selection is set either way, because those two files can be being read
        in different assemblies.
        """
        wanted = [(f"{a}×{pages // a}", a) for a in page_assemblies(pages)]
        current = [
            (self._assembly.itemText(i), self._assembly.itemData(i))
            for i in range(self._assembly.count())
        ]
        with signals_blocked(self._assembly):
            if current != wanted:
                self._assembly.clear()
                for label, value in wanted:
                    self._assembly.addItem(label, value)
            select_combo_data(self._assembly, across)

    def _pages_across(self) -> int:
        """The assembly the picker holds, or 0 where there is nothing to assemble.

        0 rather than 1 for an unpaged document, because the two say different
        things: 1 is "laid out in a column, having been asked", and 0 is "never
        asked". Only the first is worth storing in a project.

        Gated on the document rather than on whether the picker is on screen: what
        may be stored is a fact about the file, and reading it off a widget's
        visibility would make it a fact about the window.
        """
        doc = self._doc
        if doc is None or not doc.pages:
            return 0
        value = self._assembly.currentData()
        return int(value) if value is not None else doc.pages_across

    def _on_assembly_change(self, _index: int) -> None:
        """Lay the pages out differently — a view change, and nothing more.

        No reload and no re-read: the cells keep the file's own order and only
        where each one is drawn moves, so this is the same kind of change as the
        block arrangement and costs one repaint. Written onto the document first
        because that is what the refresh reads the assembly back out of.
        """
        doc = self._doc
        value = self._assembly.currentData()
        if doc is None or value is None:
            return
        doc.view.pages_across = int(value)
        # A selection is in cells of the *picture*, and the picture has just been
        # rearranged under it — the cells it names would be a different part of
        # the map. Dropping it is the honest conversion, the one a shape switch
        # already makes (``selection._on_selection_shape_change``).
        self._clear_selection()
        self._refresh_view()

    def _render_tilemap(self):
        """Render a tilemap document — a grid of cells, or a sprite object's
        frames laid out one after another.

        Both shapes live in :func:`~celpix.pipeline.pipeline.tilemap_image`, and
        this is only the Qt end of it: the same function is what PNG export
        renders through, so an exported map is the picture on screen rather than
        a second rendering that could drift from it.

        A tilemap is **always drawn entire**. Its extent is the file's, not a
        window into a large bank, and paging a screen would hide the thing being
        read (``docs/design/tilemap-entry.md`` §8).

        Two colour-table paths, decided by whether the **format** gives a cell a
        palette row. Where it does the row is already folded into the indices
        upstream and the table must not offset again — the pinned-region path,
        and the one every hardware map takes, including one whose cells all sit
        on row 0: those zeros are the file's answer and stand until something
        edits them. Where the format has no such field (a Game Boy map's bare
        tile number, a converted screen's low byte) nothing has answered, and the
        map indexes one block of the palette exactly as a pixel document does —
        Subpal picks which.
        """
        assert self._doc is not None
        drawn = pipeline.tilemap_image(
            self._doc, self._registry, self._tilemap_columns()
        )
        if self._doc.cells_carry_palette_rows:
            return (
                render_bridge.render_pinned(drawn.grid, self._doc.palette),
                drawn.drawn,
            )
        base = self._doc.view.subpalette_row * self._index_space()
        return (
            render_bridge.render(drawn.grid, self._doc.palette, base),
            drawn.drawn,
        )

    def _tile_id_labels(self) -> list[int | None] | None:
        """The tile each visible cell names, by canvas slot — or None for no labels.

        The one question a tilemap view cannot otherwise answer: a cell *names* a
        tile that lives in another entry, and which one is not recoverable by
        looking at the picture. Off unless asked for, since a number over every
        cell is a lot of ink for something you want in bursts.

        Indexed by **tile** slot, not by cell, because that is the space the
        canvas places in — a cell covering a 2x2 metatile occupies four slots, and
        only its first carries the number so the label is drawn once per cell.

        The number is the cell's own index **as the file stores it**, before the
        binding's base tile: that is what a hex editor shows at those bytes, what
        the Base tile spin is expressed in, and what stays put when the base
        moves. A stamp layout labels the tiles it resolves *to*, because those are
        the cells being drawn (``Document.drawn_cells``).

        A sprite object gets none. Its subsprites sit at signed pixel offsets rather
        than in slots, so there is no cell for a number to belong to.
        """
        doc = self._doc
        if doc is None or not doc.is_tilemap or not self._show_tile_ids:
            return None
        if doc.is_sprite:
            return None
        per_cell = doc.tiles_per_cell
        labels: list[int | None] = []
        # In the order the cells are *drawn*, since the labels are indexed by
        # canvas slot: an assembled screen file draws its pages side by side, so
        # a label taken in file order would number the wrong half of the picture.
        for cell in doc.laid_out_cells:
            labels.append(cell.index)
            labels.extend([None] * (per_cell - 1))
        return labels

    def _sync_subpalette(self) -> None:
        """Grey Subpal (and its caption) where the format has already answered it.

        A cell that has a palette row to name is the format's word on which
        colours the map is read in, whatever this file's cells set it to: a
        view-wide row on top would shift a map that is already in the colours it
        was authored in, and the way to change one is to edit the cells. Where
        the format has no such field nothing has answered, and the picture still
        has to be read under *some* row — so the spin is that row, as it is on a
        pixel document.

        Owns the enabled state in both directions, because nothing else does:
        every other pass leaves the spin alone, so a veto here would switch it
        off for the rest of the session (``capability_sync``'s rule). Not in the
        capability table because it is the *format* that decides, not the
        content kind — two tilemaps can answer differently.
        """
        assert self._doc is not None
        usable = not (self._doc.is_tilemap and self._doc.cells_carry_palette_rows)
        tip = SUBPAL_TIP if usable else SUBPAL_CELLS_TIP
        for widget in (self._subpalette, self._subpalette_label):
            widget.setEnabled(usable)
            widget.setToolTip(tip)

    def _render_rearranged(self, layout: BlockLayout, rows: int):
        """Render the window when a rearrangement is in force.

        The byte path above cannot serve this: a rearranged window's tiles are
        gathered from wherever it sends them, not from one contiguous slice. So
        the tiles come through ``_decode_run`` — the same choke point that
        resolves it for every edit, which is what keeps what is drawn and
        what is written in agreement — and only the layout is shared.

        A window running past the end of the file is short by the same count it
        always was: a rearrangement permutes existing tiles, so the positions with
        nothing behind them are exactly the ones past the last tile.
        """
        assert self._doc is not None
        view = self._doc.view
        window_tiles = layout.columns * rows
        tiles = self._decode_run(view.tile_offset, window_tiles) or []
        biases = self._window_biases(layout.columns, rows)
        grid = pipeline.compose_tiles(tiles, layout, rows, biases)
        if biases is not None:
            return render_bridge.render_pinned(grid, self._doc.palette), len(tiles)
        base = view.subpalette_row * self._index_space()
        return render_bridge.render(grid, self._doc.palette, base), len(tiles)

    def _refresh_view(self) -> None:
        assert self._doc is not None
        # A bitmap width owns the column count (it *is* the width, in tiles), so
        # settle Cols - and whether the width applies at all - before anything
        # reads them.
        self._settle_bitmap_width_and_columns()
        # And a paged tilemap's assembly owns it in turn, so it settles after: two
        # passes can claim Cols and only the second can have the last word (see
        # :meth:`_settle_tilemap_assembly`).
        self._settle_tilemap_assembly()
        cols = self._columns.value()
        # Rows is a free display-window height (bounded only by the spin's own 256
        # cap), not by the data. Asking for more rows than the file fills just
        # leaves the neutral background showing past the last tile row (see
        # shown_rows below) instead of clamping the input - so the height survives
        # switching to a format whose larger tiles leave far fewer rows of data.
        # Re-clamp the offset next: a smaller file, or a bigger window (cols/rows),
        # can push the previous offset past the last page.
        rows = self._view_rows()  # the height on screen; Rows, or the whole file
        self._offset = self._doc.clamp_tile_offset(
            self._offset, cols, rows, self._nudge
        )
        self._clamp_subpalette(self._doc.palette)
        self._doc.view = ViewOptions(
            columns=cols,
            # The *setting*, not the height above: under View ▸ Entire File the
            # two differ, and it is the setting a project stores and an entry
            # switch restores - saving a file-sized row count would overwrite it.
            rows=self._rows.value(),
            zoom=self._zoom.value(),
            subpalette_row=self._subpalette.value(),
            tile_offset=self._offset,
            byte_nudge=self._nudge,
            block_columns=self._block_cols.value(),
            block_rows=self._block_rows.value(),
            block_order=self._block_order.currentData(),
            two_dimensional=self._two_d.isChecked(),
            bitmap_width=self._bitmap_width.value(),
            tile_rearrangement=self._tile_rearrangement,
            show_rearranged=self._show_rearranged,
            palette_regions=self._palette_regions,
            show_palette_regions=self._show_palette_regions,
            # Off the picker, which the settle pass above has just brought into
            # step with the document — so this stores the assembly in force and
            # not a value one refresh behind it. 0 on everything unpaged, which is
            # what keeps the field out of an ordinary project file.
            pages_across=self._pages_across(),
            # Meaningless on everything but a sprite map, and stored anyway: the
            # window keeps one answer per entry and the box that sets it is
            # hidden where it does not apply, so there is nothing here to gate.
            show_all_frames=self._show_all_frames,
        )
        # Deferred decode: only the visible window's bytes are sliced, then decoded
        # and laid out by the shared arrangement path (2D reflow / block layout).
        # Reads back through doc.view (like zoom/grid below) so the freshly stored
        # ViewOptions is genuinely the render input, not a dead mirror.
        view = self._doc.view
        layout = BlockLayout(
            cols, view.block_columns, view.block_rows, view.block_order
        )
        if self._doc.is_tilemap:
            # A third route beside the two byte/tile ones: the cells are the
            # document, and the tiles come from wherever it is bound. Placement
            # is still the shared composer — see _render_tilemap.
            image, filled = self._render_tilemap()
        elif self._active_tile_rearrangement().is_identity():
            engine, preset = self._registry.engine_for(
                self._doc.pixel_config.interpret_preset_id
            )
            window = self._doc.window_bytes(
                view.tile_offset, cols * rows, view.byte_nudge
            )
            image, filled = self._render_arrangement(
                window,
                engine,
                pipeline.tile_params(self._doc, engine, preset.params),
                layout,
                view.two_dimensional,
                max_rows=rows,
                biases=self._window_biases(cols, rows),
            )
        else:
            image, filled = self._render_rearranged(layout, rows)
        tw, th = self._pixel_tile_size()
        self._canvas.set_tile_size(tw, th)
        self._canvas.set_zoom(view.zoom)
        # Off the workspace, not the view: the grid is one project-wide setting
        # (see MainWindow._on_grid_change), so it survives switching entries.
        self._canvas.set_grid(*self._grid_settings())
        # On a tilemap the block *is* the cell, so the grid's coarse level lands
        # on cell boundaries — which is the structure being read there, where the
        # arrangement axes belong to the pixel view and stay at 1x1. On a **sprite
        # object** the block is the *frame*: it has no cells on screen, and the
        # frame is what its tiles group into — which is also what lets the slots
        # past the last frame be backgrounded as the space they are (see the
        # ``filled`` above and :attr:`~...pipeline.SpriteSheet.slots`).
        sheet = self._sprite_sheet()
        if sheet is not None:
            block = (*sheet.frame, "row")
        elif self._doc.is_tilemap:
            block = (*self._doc.cell_tiles, "row")
        else:
            block = (view.block_columns, view.block_rows, view.block_order)
        self._canvas.set_arrangement(*block)
        self._canvas.set_filled_tiles(filled)
        # The labels are their own switch: a row can be shown without the
        # recolour and either without the other (`palette_regions.py`).
        self._canvas.set_palette_rows(
            self._window_palette_rows(cols, rows) if self._show_palette_rows else None
        )
        # The tilemap-side annotation, and the same kind of thing: a number laid
        # over the art saying what the picture cannot.
        self._canvas.set_tile_ids(self._tile_id_labels())
        self._canvas.set_image(image)
        # A lifted float's source is shown blank, never written, so a fresh base
        # image has to have that hole punched back into it.
        self._refresh_float_preview()
        self._revalidate_selection()
        # Follows the Pattern picker: a 2D pattern locks the rearrange tool out
        # (see rearrange.py), and nothing else tells it the pattern changed.
        self._sync_rearrange_actions()
        # And the stamp tool follows the *format*: a cell codec with no index
        # field has nothing for a stamp to set, and swapping to one reloads
        # through here.
        self._sync_stamp_actions()
        self._refresh_palette_dock()
        # Beside it, and for the same reason: the sheet is drawn in the
        # document's own colours, so anything that recolours the map recolours
        # what it draws from.
        self._refresh_tile_source()
        self._sync_nav()
        # After _sync_nav: the two bars are pages of one stack, and this is what
        # decides which of them the entry has controls for at all.
        self._sync_tilemap_bar()
        # The pen's colour can move under the preview without the pen itself
        # changing (a palette edit, another subpalette row, a new format).
        self._sync_paint_preview()
        self._refresh_overlay()
        self._refresh_hex()
        # Rows owns its own enabled state (two different reasons to have no row
        # count to set), so it is refreshed here rather than gated below.
        self._sync_entire_file()
        # Subpal likewise, and for a reason the capability table cannot hold: it
        # is the cell *format* that decides, not the content kind.
        self._sync_subpalette()
        # The clipboard and transform actions read the *document* as well as the
        # selection - whether its cells are editable, and which transforms its
        # format has a bit for - and both of those move without the selection
        # changing: a cell codec swapped on the toolbar reloads through here and
        # nothing else would re-decide them.
        self._sync_selection_actions()
        # Last, so its veto is the final word: every pass above enables controls
        # on grounds that are true in general and beside the point for a document
        # of the wrong kind (``docs/design/tilemap-entry.md`` §4).
        self._sync_capabilities()
        # Everything above landed in doc.view, which a project save writes out.
        self._refresh_project_modified()

    def _clamp_subpalette(self, palette: Palette) -> int:
        """Hold the subpalette row inside ``palette``; returns the row size.

        Switching to a shorter palette - a File palette holding a single row,
        say - must not leave the view pointing past it. Signals are blocked
        because this is a correction, not a user change, and must not re-enter
        the refresh that called it.
        """
        group = self._index_space()
        max_row = max(0, len(palette) - 1) // group
        if self._subpalette.value() > max_row:
            with signals_blocked(self._subpalette):
                self._subpalette.setValue(max_row)
        return group

    def _refresh_palette_dock(self) -> None:
        """Put the palette on screen into the swatch grid, readout and editor.

        Shared by the graphics view and the two document-less states - a palette
        file shown on its own, and the idle default - so the dock is filled the
        same way whatever is driving it, and a reload that recolors (or drops)
        the selected entry is picked up in all three.
        """
        palette = self._shown_palette()
        group = self._clamp_subpalette(palette)
        self._palette_panel.set_colors(palette.colors)
        self._palette_panel.set_active_range(self._subpalette.value() * group, group)
        self._refresh_color_details()
        self._sync_color_editor()

    def _refresh_hex(self) -> None:
        """Feed the hex panel a dump of the file bytes at the current offset.

        Cheap no-op while the dock is hidden (its usual state). The dump starts
        at the row holding the current view origin - so the offset's row is
        always the top line - and highlights the currently selected tile(s),
        using the same address format as the navbar. Bounded to the on-screen
        window (a minimum of some context, a cap for huge windows) so a
        multi-megabyte file never renders as one giant document.

        A tilemap entry's own bytes are its cells rather than these pixels, so it
        takes its own route (:meth:`_refresh_tilemap_hex`).
        """
        if not self._hex_dock.isVisible():
            return
        if self._doc is None:
            self._hex_panel.clear()
            return
        if self._doc.is_tilemap:
            self._refresh_tilemap_hex()
            return
        data = self._doc.pixel_data
        origin = self._byte_position()
        window = len(
            self._doc.window_bytes(
                self._offset, self._columns.value() * self._view_rows(), self._nudge
            )
        )
        row_start = (origin // BYTES_PER_ROW) * BYTES_PER_ROW
        # Enough rows to cover the visible window, floored so the panel is never
        # nearly empty and capped so a whole-file view can't blow up the dump.
        span = max(window, 16 * BYTES_PER_ROW)
        span = min(span, 256 * BYTES_PER_ROW)
        region_end = min(len(data), row_start + BYTES_PER_ROW + span)
        base = self._display_base()
        self._hex_panel.show_bytes(
            data,
            row_start,
            region_end,
            lambda index: self._format_offset(base + index),
            self._selection_byte_range(),
        )

    def _refresh_tilemap_hex(self) -> None:
        """The dump for a tilemap entry: its **cells**, not the tiles it draws.

        A tilemap document holds two files at once. Its own cells are the entry —
        what it saves, what a selection names positions in — while the bytes of
        whatever tile source it is bound to ride in ``pixel_data`` so that every
        tile path keeps working over the art (``docs/design/tilemap-entry.md``
        §8). Dumping the second would show the user a bank belonging to a
        different entry, with a highlight that could only be a coincidence; this
        shows the cells, so selecting one lights up the record behind it.

        There is no view window to anchor the dump to — a tilemap is always drawn
        entire — so it follows the **selection** instead, and picking a cell
        scrolls its bytes onto the top row. Same span rule as the pixel dump, and
        the cap earns its keep here: a screen is 8 KiB of cells.
        """
        doc = self._doc
        assert doc is not None
        data = doc.tilemap_data
        highlight = self._selection_byte_range()
        origin = highlight[0] if highlight is not None else 0
        row_start = (origin // BYTES_PER_ROW) * BYTES_PER_ROW
        span = min(max(len(data), 16 * BYTES_PER_ROW), 256 * BYTES_PER_ROW)
        region_end = min(len(data), row_start + BYTES_PER_ROW + span)
        base = doc.tilemap_display_base
        self._hex_panel.show_bytes(
            data,
            row_start,
            region_end,
            lambda index: self._format_offset(base + index),
            highlight,
        )
