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
from celpix.pipeline import pipeline
from celpix.ui import render_bridge
from celpix.ui.canvas import ATTR_FLAGS_BIT, ATTR_PRIORITY_BIT
from celpix.ui.hex_view_panel import BYTES_PER_ROW
from celpix.ui.main_window.interpretation import (
    COLS_ASSEMBLED_TIP,
    COLS_CELLS_TIP,
    COLS_FRAMES_TIP,
    COLS_ROW_PLANE_TIP,
    COLS_STAMPED_TIP,
    COLS_STAMPS_TIP,
    COLS_TIP,
    SUBPAL_CELLS_TIP,
    SUBPAL_TIP,
)
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

    def _window_palette_rows(self, cols: int, rows: int) -> list[int | None] | None:
        """The **pinned** row of each visible slot, ``None`` where none is pinned.

        The whole of what pinned palette regions cost at render time. Each slot is
        resolved to the pixel its tile starts at, that pixel is looked up in the
        regions, and the row it finds is taken through the entry's palette row
        base — a pinned row is a *named* row, so the number that comes back is a
        row of the palette on screen and not of the file's own numbering.

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
        return [
            None if row is None else self._drawn_palette_row(row)
            for row in regions.rows_for(offsets, None)
        ]

    def _window_biases(self, cols: int, rows: int) -> list[int] | None:
        """The rows above as index shifts — what the composer actually applies.

        Split from :meth:`_window_palette_rows` so the number drawn on a tile and
        the palette it is drawn through come from one computation: a label that
        could disagree with the recolour would be worse than no label.

        This is the end that fills the unpinned slots in, with the view's own row —
        every tile has to be drawn through some row, and an unpinned one is drawn
        through exactly the row it would be without any of this. The view's row is
        the one row here that does *not* go through the base: it is a row picked
        in the palette that is loaded, so it is already absolute
        (:attr:`~celpix.core.document.Document.palette_row_base`).
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

        A document that **fixes** its own width is the exception, and the same one
        the renderer makes: an assembly's pages and a dense map's stamps both cut
        the row at a place the file decided, so the number comes from the document
        rather than the spin
        (:attr:`~celpix.core.document.Document.drawn_columns`). Asking the same
        authority is what keeps the selection reading the canvas off the grid the
        picture was actually placed on, instead of relying on a sync pass having
        already pushed that number into the spin.
        """
        assert self._doc is not None and self._doc.cells is not None
        # The **drawn** positions, not the file's cells: a fontmap spells a
        # dictionary code out into the characters it stands for, and each of them
        # takes a position of its own
        # (:attr:`~celpix.core.document.Document.drawn_positions`).
        count = self._doc.drawn_positions
        columns = self._doc.drawn_columns or self._columns.value()
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

        A screen and a PNL panel are 32 cells across and a stamp layout 128; the file
        knows, and a wrong guess **shears** the picture into diagonal stripes
        rather than failing, which is the worst way to be wrong. Applied on load
        only, and skipped when a project ``restored`` a width of its own, so it is
        a starting point rather than something that fights the spin.

        Whether there *was* one is the caller's to say, not something to read off
        the entry here: the restore has already run by this point and consumed the
        pending view it would have been read from
        (:meth:`~...session.SessionMixin._apply_restored_state`).

        The width lands on the **document's view**; the Cols spin is only touched
        where ``entry`` is the one on screen. This runs for every tilemap load,
        and most loads are not that entry's: a bound source resolved on demand
        inside another map's read, a first activation (loaded before ``current``
        moves — writing the spin there would be captured back onto the *outgoing*
        entry's view), an off-screen re-read. Their width written to the widget
        becomes the shown entry's the next time the view is rebuilt from it —
        which is how rebinding a stamp layout to a not-yet-loaded panel used to
        halve the layout's own columns, irrecoverably, since the carried-over
        view then reads as the width it "was seen at". The entry on screen gets
        the spin seeded either here (a reload in place) or by
        :meth:`~...session.SessionMixin._restore_session` reading the view this
        wrote (an activation).
        """
        width = self._tilemap_columns_hint(entry)
        if width and not restored:
            doc = entry.doc
            # A file that fixes its own width states it in its own unit — one
            # page, one row of stamps — and the document is where that is turned
            # into the width the picture is laid out at, so this seeds Cols with
            # the same number the layout is about to use rather than that unit.
            if doc is not None and doc.drawn_columns:
                width = doc.drawn_columns
            if doc is not None:
                doc.view.columns = width
            if entry is self._workspace.current:
                with signals_blocked(self._columns):
                    self._columns.setValue(width)

    def _settle_tilemap_width(self) -> None:
        """Let a tilemap that fixes its own width own Cols while it applies.

        The tilemap counterpart of :meth:`_settle_bitmap_width_and_columns`, and
        it runs after that one for the same reason it exists: both take the column
        count over, and the last word has to be one of them. A tilemap wins
        wherever its width is not a preference at all
        (:attr:`~celpix.core.document.Document.columns_locked`) — pages are cut at
        a fixed size and a *stated* stamp width is the file's, so any column count
        but the one the file implies breaks the row at the wrong place and shears
        the picture into diagonal stripes
        (``docs/design/tilemap-entry.md`` §3.1, §6).

        **A dense map whose format states no width is the other way round.** Its
        entries are a plain rectangle with one per stamp, so the width is the same
        preference an ordinary tilemap's is, and nothing but Cols can supply it —
        a slice lifted out of a disk image runs no container, and its header is
        four bytes before where the entry starts. There the document resolves its
        width *through* ``view.columns``, so the spin's value has to be on the
        view before this reads ``drawn_columns`` back. **The caller puts it
        there**, immediately above the call (:meth:`_refresh_view`), because that
        write leaves the view hybrid — one field ahead of the rest — until the
        whole of ``ViewOptions`` is rebuilt a dozen lines later, and a lifetime
        that short belongs where both ends of it are visible. Nothing here writes
        to the document, so this pass is as safe to call twice, or from somewhere
        else, as its bitmap sibling.

        What comes back on such a map is the spin's value floored to whole stamps:
        setting Cols to 47 on a 2x2 map draws 46, because a row that ends halfway
        through a stamp puts the other half at the start of the next.

        **There is no assembly control.** Every paged format celPix reads states
        its own layout — a screen file's four quadrants are one 64x64 tilemap and
        the editor's own loader says which corner each goes in
        (:attr:`~celpix.core.document.Document.stated_pages_across`) — so the
        arrangement was never the user's to pick, and a picker would have been
        inviting them to shear a picture whose shape is not in question. The model
        still resolves an unstated assembly through ``view.pages_across`` and
        :func:`~celpix.core.tilemap.default_pages_across`, so a format that holds
        pages without stating their layout still lays out sensibly; what went is
        the widget, not the mechanism.

        What is left here is the width, which where the file states one is not a
        choice either: it *is* the assembly (or the stamp), and the spin mirrors it.
        """
        doc = self._doc
        width = doc.drawn_columns if doc is not None else 0
        # The unit Cols moves in, which is the unit it is *read* in: a step of one
        # on a map that floors to whole stamps would land back where it started.
        # Taken as the ratio the document itself lays out at rather than off the
        # chain, so it is the number that actually did the flooring however the
        # width was arrived at (:attr:`~...Document.stamp_columns`).
        entries = doc.stamp_columns if doc is not None else 0
        self._columns.setSingleStep(width // entries if width and entries else 1)
        if not width:
            # Nothing here fixes a width: an ordinary tilemap, a sprite object, or
            # no document at all — `drawn_columns` is 0 for each. Cols is left
            # exactly as the bitmap-width pass set it, since taking it over is the
            # only thing this pass does to it and there is nothing to hand back
            # that the earlier owner has not already decided.
            self._label_columns(locked=False)
            return
        # A width means there is a document: the read above is what produced it.
        locked = doc.columns_locked
        if locked:
            self._columns.setEnabled(False)
        self._label_columns(locked=locked)
        # The spin **mirrors** the width rather than setting it: the layout and the
        # selection both take it from the document, so this is what puts the number
        # where the user can read it (and where a project stores it). Unlocked, that
        # is also what shows the user the flooring — the typed 47 reads back as 46.
        if self._columns.value() != width:
            with signals_blocked(self._columns):
                self._columns.setValue(width)

    def _label_columns(self, *, locked: bool) -> None:
        """Say what Cols counts, or what has taken it over — caption included.

        Three readings, because a column is a different thing in each: a tile on
        a pixel document, a **cell** on a tilemap — which may be a metatile of
        several tiles — and a whole **frame** on a sprite object, whose Cols lays
        out the strip the canvas shows rather than anything inside a frame. One
        wording over all three is wrong on two of them, and wrong in the way a
        user cannot check: the number does not say what it counts.

        The caption is half the control's hover target
        (:func:`~celpix.ui.widgets.add_labelled`), so a live-looking label over a
        dead input is exactly where "why can't I type here" lands.
        """
        doc = self._doc
        if locked:
            # Only a grid map gets here — a sprite object neither pages nor stamps
            # — so the locked wording speaks cells and needs no reading of its own.
            # Which of the three took it over does have to be said, though: the
            # user is being told why the spin is dead, and "pages" on a map that
            # has none sends them looking for something that is not there. In the
            # order :attr:`~...Document.drawn_columns` resolves them, since a
            # paged nametable is locked by two things at once and the assembly is
            # the one that decided the number.
            if doc is not None and doc.pages:
                tip = COLS_ASSEMBLED_TIP
            elif doc is not None and doc.row_plane_columns:
                tip = COLS_ROW_PLANE_TIP
            else:
                tip = COLS_STAMPED_TIP
        elif doc is None or not doc.is_tilemap:
            tip = COLS_TIP
        elif doc.is_sprite:
            tip = COLS_FRAMES_TIP
        else:
            # A live spin on a map that still stamps: the number is the user's,
            # and it is theirs in whole stamps (:meth:`_settle_tilemap_width`).
            tip = COLS_STAMPS_TIP if doc.drawn_columns else COLS_CELLS_TIP
        self._columns.setToolTip(tip)
        self._columns_label.setToolTip(tip)
        self._columns_label.setEnabled(self._columns.isEnabled())

    def _pages_across(self) -> int:
        """The assembly to store for this entry, or 0 where there is none to store.

        0 rather than 1 for an unpaged document, because the two say different
        things: 1 is "laid out in a column, having been asked", and 0 is "never
        asked". Only the first is worth keeping in a project — and a format that
        **states** its assembly was never asked either, so it stores 0 too rather
        than a copy of a fact the container republishes on every read.

        Otherwise a passthrough of what the document already holds. Nothing in the
        UI can set it since the picker went, so the only value it can carry is a
        project's own — and carrying it is what keeps the field a working part of
        the model rather than one that quietly empties on the first save.
        """
        doc = self._doc
        if doc is None or not doc.pages or doc.stated_pages_across:
            return 0
        return doc.view.pages_across

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
        # The composed grid travels with the image because a **float** needs the
        # same one to punch its hole into, and composing a map is the expensive
        # half of a repaint — it is the whole file, not a view window, so asking
        # for it twice doubles the cost of every repaint made while pixels are in
        # the air. The two branches beside this one compose only their window,
        # and hand back None rather than pretending otherwise.
        return (
            self._tilemap_grid_image(drawn.grid, drawn.hidden),
            drawn.drawn,
            drawn.grid,
        )

    def _tilemap_grid_image(self, grid, hidden=()):
        """A composed tilemap grid as a QImage, under this document's colour rule.

        ``hidden`` is the undrawn positions to paint the background over
        (:func:`~celpix.ui.render_bridge.paint_hidden`). It defaults to none for
        the **live preview**, which composes a window of the map mid-stroke and
        has no business blanking positions the stroke is not touching.

        Two colour-table paths, decided by whether the **format** gives a cell a
        palette row (:attr:`~celpix.core.document.Document.folds_palette_rows`).
        Where it does the row is already folded into the indices upstream and the
        table must not offset again — the pinned-region path, and the one every
        hardware map takes, including one whose cells all sit on row 0: those
        zeros are the file's answer and stand until something edits them. Where
        the format has no such field (a Game Boy map's bare tile number, a
        converted screen's low byte) nothing has answered, and the map indexes one
        block of the palette exactly as a pixel document does — Subpal picks which.

        Split out because the **live preview** of a stroke has to pick the same
        way (:meth:`~...pixel_edit.PixelEditMixin._render_preview`): a preview
        drawn through the other table would jump a row the moment the mouse went
        down and jump back on release. Two copies of this choice drifted on two
        arguments within a day of existing.
        """
        assert self._doc is not None
        clear = self._doc.view.transparent_zero
        if self._doc.folds_palette_rows:
            image = render_bridge.render_pinned(
                grid, self._doc.palette, self._index_space(), transparent_zero=clear
            )
        else:
            base = self._doc.view.subpalette_row * self._index_space()
            image = render_bridge.render(
                grid, self._doc.palette, base, transparent_zero=clear
            )
        return render_bridge.paint_hidden(image, hidden)

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

    def _cell_attr_marks(self) -> list[int | None] | None:
        """The attribute badges for each visible cell, by canvas slot — or None.

        :meth:`_tile_id_labels`' badge twin, for the two per-cell fields no
        render can show: **priority** is carried and never drawn (celPix has no
        layers), and **flags** are bits the format has that celPix does not
        interpret. Without a readout, editing either through the property row
        is blind — the change lands and the picture looks exactly the same.

        Marked only for the fields the *format* declares
        (:meth:`~...tilemap_edit.TilemapEditMixin._cell_fields`), so a badge
        can never claim a bit the file has nowhere to store; same shape rules
        as the labels — by tile slot, once per cell, in drawn order — and the
        **drawn** cells, so a chained map badges what is actually on screen.
        """
        doc = self._doc
        if doc is None or not doc.is_tilemap or not self._show_cell_attrs:
            return None
        if doc.is_sprite:
            return None
        fields = self._cell_fields()
        priority = "priority" in fields
        flags = "flags" in fields
        if not priority and not flags:
            return None
        per_cell = doc.tiles_per_cell
        marks: list[int | None] = []
        for cell in doc.laid_out_cells:
            mask = 0
            if priority and cell.priority:
                mask |= ATTR_PRIORITY_BIT
            if flags and cell.flags:
                mask |= ATTR_FLAGS_BIT
            marks.append(mask or None)
            marks.extend([None] * (per_cell - 1))
        return marks

    def _line_end_slots(self) -> frozenset[int]:
        """The canvas slots whose cell ends a line — a **fontmap** only.

        The one thing a text run's picture cannot say for itself. A grid of glyph
        tiles is a correct drawing of the cells and shows nothing of where the
        strings stop, which on most formats is not even where the row does: a
        level-name region breaks mid-row, over and over, and the canvas draws it
        as one unbroken block of letters.

        Which cells those are is :meth:`~celpix.core.font.FontAlphabet.ends_line`'s
        answer and not this method's, so the mark on the picture and the newline
        in the text window come out of one rule. Where **no alphabet** is picked
        the terminator bit is still a fact about the cells and still worth
        drawing — it is read off the format, not off the font, and it is the only
        thing legible about a stream nothing has explained yet.

        Empty for everything that is not a fontmap, and indexed by the slot each
        cell **starts** at, like :meth:`_tile_id_labels`' numbers.
        """
        doc = self._doc
        if doc is None or not doc.is_fontmap:
            return frozenset()
        alphabet = doc.font_alphabet
        per_cell = doc.tiles_per_cell
        return frozenset(
            at * per_cell
            for at, cell in enumerate(doc.laid_out_cells)
            if (
                alphabet.ends_line(cell.index, cell.ends_line)
                if alphabet is not None
                else cell.ends_line
            )
        )

    def _palette_row_labels(self, cols: int, rows: int) -> list[int | None] | None:
        """The row to number each canvas slot with — the overlay, for both stores.

        One switch over two answers, because it is one question: which slots name
        a subpalette row of their own, and which row. A pixel document's named
        rows are its pinned regions (:meth:`_window_palette_rows`); a tilemap's
        are its cells (:meth:`_cell_palette_rows`). Both come back as rows of the
        palette on screen, taken through the base, so the number over a tile and
        the ring in the palette grid are the same number
        (:meth:`~...palette_regions.PaletteRegionsMixin._drawn_palette_row`).
        """
        if not self._show_palette_rows:
            return None
        doc = self._doc
        if doc is not None and doc.is_tilemap:
            return self._cell_palette_rows()
        return self._window_palette_rows(cols, rows)

    def _cell_palette_rows(self) -> list[int | None] | None:
        """The row each visible cell draws through, by canvas slot — or None.

        :meth:`_tile_id_labels`' twin, and the same shape for the same reasons:
        indexed by **tile** slot because that is the space the canvas places in,
        one number per cell so a metatile is labelled once, and walked in the
        order the cells are *drawn* so an assembled screen numbers the half of
        the picture it is looking at.

        The **drawn** cells, so a chained map shows the rows its stamps actually
        carry rather than the zeros its own coordinate words hold
        (:meth:`~celpix.core.document.Document.resolve`).

        None where the format gives a cell no row — there the whole map is read
        under Subpal and a number over every cell would repeat one control — and
        None for a sprite object, whose subsprites sit at pixel offsets rather
        than in slots.
        """
        doc = self._doc
        if doc is None or doc.is_sprite or not doc.cells_carry_palette_rows:
            return None
        per_cell = doc.tiles_per_cell
        labels: list[int | None] = []
        for cell in doc.laid_out_cells:
            labels.append(self._drawn_palette_row(cell.palette_row))
            labels.extend([None] * (per_cell - 1))
        return labels

    def _sync_subpalette(self) -> None:
        """Say what Subpal means here — the view's row, or the row being picked.

        A cell that has a palette row to name is the format's word on which
        colours the map is read in, whatever this file's cells set it to: a
        view-wide row on top would shift a map that is already in the colours it
        was authored in, and the way to change one is to edit the cells. Where
        the format has no such field nothing has answered, and the picture still
        has to be read under *some* row — so the spin is that row, as it is on a
        pixel document.

        Live either way, and the tooltip is the whole of the difference. What the
        spin is on such a map is the row being *pointed at* — the block the
        palette grid outlines, the row the tile sheet is read in, and the row Set
        Selection's Palette Row writes into the cells
        (:meth:`~...tilemap_edit.TilemapEditMixin._assign_cell_palette_row`). Only
        the render ignores it, and greying the input would refuse the one gesture
        that needs it in order to say so.

        Not in the capability table because it is the *format* that decides, not
        the content kind — two tilemaps can answer differently.
        """
        assert self._doc is not None
        view_row = not (self._doc.is_tilemap and self._doc.cells_carry_palette_rows)
        tip = SUBPAL_TIP if view_row else SUBPAL_CELLS_TIP
        for widget in (self._subpalette, self._subpalette_label):
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
        # A dense stamped map with no stated width resolves its own width *from*
        # the view, so the spin's value goes there before the pass below reads it
        # back (:attr:`~celpix.core.document.Document.stamp_columns`). This is the
        # one field that runs ahead of the rest of `ViewOptions`, and the rebuild
        # that brings the rest into step is the assignment at the end of this
        # method — the whole lifetime of the hybrid, in one place.
        if self._doc.is_tilemap and not self._doc.columns_locked:
            self._doc.view.columns = self._columns.value()
        # And a paged tilemap's assembly owns Cols in turn, so it settles after
        # the bitmap pass: two passes can claim it and only the second can have
        # the last word (see :meth:`_settle_tilemap_width`).
        self._settle_tilemap_width()
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
            # Like the toggle above it: a local preference the model reads here
            # rather than a per-entry choice, so the render and the export take
            # one bundle and cannot disagree about how the base ends.
            wrap_palette_rows=self._wrap_palette_rows,
            # Off the picker, which the settle pass above has just brought into
            # step with the document — so this stores the assembly in force and
            # not a value one refresh behind it. 0 on everything unpaged, which is
            # what keeps the field out of an ordinary project file.
            pages_across=self._pages_across(),
            # Meaningless on everything but a sprite map, and stored anyway: the
            # window keeps one answer per entry and the box that sets it is
            # hidden where it does not apply, so there is nothing here to gate.
            show_all_frames=self._show_all_frames,
            # Only the tilemap render reads it (:meth:`_tilemap_render`), and the
            # box that sets it is hidden everywhere else — stored for every entry
            # all the same, on the same rule as the frame count above: the window
            # keeps one answer per entry rather than a second place to gate it.
            transparent_zero=self._transparent_zero,
        )
        # Straight after the view is written and before anything composes from
        # it: on a **font sheet** Cols and the Pattern are what say how big one
        # glyph is, so moving either changes what every string bound to this
        # sheet draws and not only this picture
        # (:meth:`~...session.SessionMixin._resync_glyph_layouts`). A scan of the
        # open entries on any other kind of document, and nothing else.
        current = self._workspace.current
        if current is not None:
            self._resync_glyph_layouts(current)
        # Deferred decode: only the visible window's bytes are sliced, then decoded
        # and laid out by the shared arrangement path (2D reflow / block layout).
        # Reads back through doc.view (like zoom/grid below) so the freshly stored
        # ViewOptions is genuinely the render input, not a dead mirror.
        view = self._doc.view
        layout = BlockLayout(
            cols, view.block_columns, view.block_rows, view.block_order
        )
        composed = None
        if self._doc.is_tilemap:
            # A third route beside the two byte/tile ones: the cells are the
            # document, and the tiles come from wherever it is bound. Placement
            # is still the shared composer — see _render_tilemap.
            image, filled, composed = self._render_tilemap()
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
        # The stamp gestures place in a different unit from the block exactly on
        # a stamped chain — a press lays a whole stamp while the block stays one
        # cell — and the canvas has to be told, so the pick rectangle, the
        # click-vs-drag test and the hover preview anchor all snap to what a
        # press actually places. (0, 0) leaves the block deciding, which is
        # right everywhere stamping is not offered.
        if self._doc.is_tilemap and not self._doc.is_sprite:
            self._canvas.set_stamp_unit(*self._doc.stamp_tiles)
        else:
            self._canvas.set_stamp_unit(0, 0)
        self._canvas.set_filled_tiles(filled)
        # The labels are their own switch: a row can be shown without the
        # recolour and either without the other (`palette_regions.py`). One
        # switch, two stores - a pixel document's pins, a tilemap's cells.
        self._canvas.set_palette_rows(self._palette_row_labels(cols, rows))
        # The tilemap-side annotation, and the same kind of thing: a number laid
        # over the art saying what the picture cannot.
        self._canvas.set_tile_ids(self._tile_id_labels())
        # Its badge twin, for the two fields no render can show: priority and
        # the format's uninterpreted flags.
        self._canvas.set_cell_attrs(self._cell_attr_marks())
        # And the fontmap's, which has no switch because it is not an annotation:
        # where a string stops is content, and the grid of glyphs shows none of it.
        self._canvas.set_line_ends(self._line_end_slots())
        self._canvas.set_image(image)
        # A lifted float's source is shown blank, never written, so a fresh base
        # image has to have that hole punched back into it — over the grid this
        # render just composed, where there is one, rather than a second copy of it.
        self._refresh_float_preview(composed)
        # And the floating pixels themselves, for the other half of the same rule:
        # the overlay carries its own rendered image, so anything that recolours
        # the base — a palette edit, a Subpal move, the Transparent 0 box — leaves
        # a float in the air showing the colours of the render before last. A
        # no-op with nothing up, and never more than the float's own rectangle.
        self._show_float()
        self._revalidate_selection()
        # And the sprite object's pick beside it: Cols re-flows the frames, so a
        # pick that survives is at a different pixel than it was.
        self._revalidate_subsprite()
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
        # Beside the decompression overlay, and for the same reason: both are tool
        # windows holding a picture of *this* entry, so both have to be told when
        # the entry underneath them changes.
        self._animation_action.setEnabled(self._animation_available())
        self._sync_animation()
        # Beside the player, and gated on the same entry for the same reason -
        # what differs is only how wide the gate is, since every sprite map has
        # subsprites where few have sequences.
        self._subsprites_action.setEnabled(self._subsprites_available())
        self._sync_subsprites()
        self._text_action.setEnabled(self._text_available())
        self._sync_text()
        # The third of the tool windows, and the tick that declares one. The
        # window shows the font a fontmap draws through, so it follows the entry
        # for the reason the other two do.
        self._sync_use_as_font()
        self._font_alphabet_action.setEnabled(self._font_alphabet_available())
        self._sync_font_alphabet()
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
        # Which shapes a drag may describe follows the kind on screen, not just
        # the mode the user is in - a tilemap is rectangle-only however it was
        # opened, so the entry changing has to re-decide it.
        self._sync_selection_shape()
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
        # Before the grid: the base decides which rows the marks below land on,
        # and it is shown or hidden by the same document that sizes them.
        self._sync_row_base()
        self._palette_panel.set_colors(palette.colors)
        self._palette_panel.set_active_range(self._subpalette.value() * group, group)
        # After the range, which sizes the mark: the panel counts a pinned row in
        # whole subpalettes of the same index space. Here as well as on the
        # selection path because pinning, unpinning and hiding the pinned render
        # all move the mark without moving the selection.
        self._sync_marked_palette_row()
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
        base = self._address_base()
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
        base = self._tilemap_address_base()
        self._hex_panel.show_bytes(
            data,
            row_start,
            region_end,
            lambda index: self._format_offset(base + index),
            highlight,
        )
