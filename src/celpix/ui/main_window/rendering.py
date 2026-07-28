"""The refresh cycle: current document + view options → what is on screen.

One entry point, :meth:`~RenderingMixin._refresh_view`, which every change that
can alter the picture funnels through — a widget move, an edit, an entry switch,
an undo. It settles the view axes into ``doc.view`` first and then reads the
render back *out* of it, so the stored ``ViewOptions`` is genuinely the input to
what is drawn rather than a mirror that can drift from it.

Decode is **deferred and windowed**: only the visible tiles' bytes are sliced out
and decoded, so the cost of a repaint follows the window rather than the file
(``docs/design/architecture.md`` §2). Two routes reach the canvas from there, and
which one runs is the only thing the tile map changes:

- **by bytes** — the ordinary path, and the one the decompression overlay shares
  (:meth:`~RenderingMixin._render_arrangement`): one contiguous window through 2D
  reflow, decode and block layout.
- **by tiles** (:meth:`~RenderingMixin._render_rearranged`) — when a rearrangement
  is in force the window's tiles come from wherever the map sends them, which is
  not a contiguous slice, so they are gathered through the same ``_decode_run``
  choke point every edit resolves the map with. That shared choke point is what
  keeps what is drawn and what is written in agreement.

The dependent surfaces (palette dock, hex dump, navbar, overlay) are refreshed
from the tail of the same cycle rather than each watching for its own trigger.
"""

from __future__ import annotations

from celpix.core import ceil_div
from celpix.core.arrangement import BlockLayout, tile_first_pixel
from celpix.core.document import ViewOptions
from celpix.core.palette import Palette
from celpix.pipeline import pipeline
from celpix.ui import render_bridge
from celpix.ui.hex_view_panel import BYTES_PER_ROW
from celpix.ui.widgets import signals_blocked


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

    def _window_biases(self, cols: int, rows: int) -> list[int] | None:
        """One index shift per visible slot, or None when nothing is pinned.

        The whole of what pinned palette regions cost at render time. Each slot is
        resolved to the pixel its tile starts at, that pixel is looked up in the
        regions, and the row it lands in becomes ``row * index_space`` - the shift
        :meth:`~celpix.core.index_grid.IndexGrid.shifted` folds into the indices.
        Slots in no region get the view's own subpalette row, so the unpinned part
        of the window renders exactly as it does without this.

        Returning **None rather than a list of zeroes** for an unpinned document is
        load-bearing: it is what keeps every existing view on the original single
        table path, allocating nothing and shifting nothing.

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
        space = self._index_space()
        per_tile = doc.tile_width * doc.tile_height
        tile_map = self._active_tile_map()
        count = cols * rows
        if tile_map.is_identity():
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
            # whichever tile the map sends it, not the one the slot sits on.
            offsets = [
                actual * per_tile
                for actual in tile_map.actual_run(view.tile_offset, count)
            ]
        rows_for = regions.rows_for(offsets, view.subpalette_row)
        return [row * space for row in rows_for]

    def _render_rearranged(self, layout: BlockLayout, rows: int):
        """Render the window when a tile map is in force.

        The byte path above cannot serve this: a rearranged window's tiles are
        gathered from wherever the map sends them, not from one contiguous slice.
        So the tiles come through ``_decode_run`` — the same choke point that
        resolves the map for every edit, which is what keeps what is drawn and
        what is written in agreement — and only the layout is shared.

        A window running past the end of the file is short by the same count it
        always was: the map permutes existing tiles, so the positions with
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
            tile_map=self._tile_map,
            show_rearranged=self._show_rearranged,
            palette_regions=self._palette_regions,
            show_palette_regions=self._show_palette_regions,
        )
        # Deferred decode: only the visible window's bytes are sliced, then decoded
        # and laid out by the shared arrangement path (2D reflow / block layout).
        # Reads back through doc.view (like zoom/grid below) so the freshly stored
        # ViewOptions is genuinely the render input, not a dead mirror.
        view = self._doc.view
        layout = BlockLayout(
            cols, view.block_columns, view.block_rows, view.block_order
        )
        if self._active_tile_map().is_identity():
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
        self._canvas.set_arrangement(
            view.block_columns, view.block_rows, view.block_order
        )
        self._canvas.set_filled_tiles(filled)
        self._canvas.set_image(image)
        # A lifted float's source is shown blank, never written, so a fresh base
        # image has to have that hole punched back into it.
        self._refresh_float_preview()
        self._revalidate_selection(cols * rows)
        # Follows the Pattern picker: a 2D pattern locks the rearrange tool out
        # (see rearrange.py), and nothing else tells it the pattern changed.
        self._sync_rearrange_actions()
        self._refresh_palette_dock()
        self._sync_nav()
        # The pen's colour can move under the preview without the pen itself
        # changing (a palette edit, another subpalette row, a new format).
        self._sync_paint_preview()
        self._refresh_overlay()
        self._refresh_hex()
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
        """
        if not self._hex_dock.isVisible():
            return
        if self._doc is None:
            self._hex_panel.clear()
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
