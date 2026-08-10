"""Which entry is on screen, and the UI state that travels with it.

Switching entries is not just repointing ``_doc``: every entry carries its own
codec choice, preview compression, arrangement, view window, selection and palette mode,
and all of it has to be put back exactly as it was left. So an activation is
always **capture the outgoing, then restore the incoming**
(:meth:`~SessionMixin._capture_session` / :meth:`~SessionMixin._restore_session`),
with the restore done as one signal-blocked swap followed by a single refresh
rather than a cascade of per-widget reloads.

The split against its neighbours: :mod:`~celpix.ui.main_window.entries` owns the
*list* — what is in it, and writing it back — while this module owns what happens
when the view moves from one of its rows to another. The two document-less states
live here too — nothing open, and an entry whose file has gone missing — because
they are the same swap with nothing to swap in, and they share the blanking half
of it (:meth:`~SessionMixin._clear_document_view`).

``EntrySession`` is the toolbar half of that state and ``Document.view`` the
arrangement half; the division is which of them outlives a document being
dropped and re-read (see :meth:`~SessionMixin._load_entry`).
"""

from __future__ import annotations

from dataclasses import replace
from typing import NamedTuple

from PySide6.QtGui import QImage

from celpix.core.capabilities import ContentKind
from celpix.core.context import (
    KEY_PIXEL_PRESET,
    KEY_TILE_PALETTE_ROW_BASE,
    KEY_TILE_PALETTE_ROWS,
    KEY_TILEMAP_CELL_TILES,
    KEY_TILEMAP_PALETTE_ROW_BASE,
    KEY_TILEMAP_STAMP_TILES,
    PipelineContext,
)
from celpix.core.document import CellChain, Document
from celpix.core.errors import PipelineError, Stage
from celpix.core.paletteregions import PaletteRegion, PaletteRegions
from celpix.core.tilemap import VRAM_ROW_STRIDE, Cell
from celpix.pipeline import pipeline
from celpix.pipeline.pathway import PathwayConfig
from celpix.plugins.base import NO_COMPRESSION, STAGE_DEFAULT_PRESET, FileRef
from celpix.project.workspace import (
    Entry,
    EntryKind,
    EntrySession,
    PaletteMode,
    TileMode,
    TileSource,
    backfill_slice_length,
    data_missing,
)
from celpix.ui.tools import EditMode
from celpix.ui.widgets import select_combo_data, signals_blocked

# What a tilemap entry falls back to when nothing else named a cell codec — a
# container normally supplies one (``detect.tilemap_preset_for``), so this is
# for a tilemap that was carved out by hand rather than detected.
_DEFAULT_TILEMAP_PRESET = STAGE_DEFAULT_PRESET[Stage.INTERPRET_TILEMAP]
_DEFAULT_PIXEL_PRESET = STAGE_DEFAULT_PRESET[Stage.INTERPRET_PIXEL]


class _BoundTiles(NamedTuple):
    """The art a tilemap entry draws from, ready to become the document's
    pixel half — or an empty stand-in when nothing is bound."""

    data: bytes
    bytes_per_tile: int
    tile_width: int
    tile_height: int
    ctx: PipelineContext
    config: PathwayConfig


class SessionMixin:
    """Entry activation, per-entry session capture/restore, the empty states.

    A slice of :class:`~celpix.ui.main_window.window.MainWindow`, not a
    standalone object: it drives the window's own widgets and its single live
    ``_doc``. See the module docstring for what it owns, and the package
    docstring for why these are mixins.
    """

    # -- entry switching -----------------------------------------------------
    def _activate_entry(self, entry: Entry) -> None:
        """Switch the view to ``entry`` - every activation path funnels here."""
        if entry is None or entry is self._workspace.current:
            return
        if entry.kind in (EntryKind.BOOKMARK, EntryKind.PALETTE):
            return  # no view of its own - selecting one in the list is inert
        # Pixels floating over the entry being left belong to it, so they come
        # down before the view moves on rather than hovering over a stranger.
        self._commit_float()
        # And its region settles on the way out, after that landing: leaving an
        # entry is where editing it stops, so the fold it owes is paid at a
        # moment nothing is waiting on rather than carried into whatever the user
        # does next (``docs/design/slices-and-parents.md`` §2). Every reader
        # settles for itself regardless — this only decides *when* the cost lands,
        # and a region owing nothing costs a set test.
        self._settle_region(self._workspace.current)
        if data_missing(entry):
            # The file moved: make it current anyway, but show the disabled
            # unavailable state (no _load_entry, so no pipeline-error alert -
            # relocation happens through Locate missing files, not every click).
            self._capture_session()
            self._workspace.set_current(entry)  # -> _show_unavailable
            return
        fresh = entry.doc is None
        if fresh and not self._load_entry(entry):
            # Load failed (bad codec/invalid file): stay put, and snap the
            # list highlight back onto the entry actually shown.
            self._files_panel.set_current(self._workspace.current)
            return
        self._capture_session()
        self._workspace.set_current(entry)  # -> _on_current_entry_changed
        # Arm arrow-key navigation on the fresh view - but not when the list is
        # itself being browsed with the arrow keys, or focus would be yanked
        # away from the very keys the user is navigating with.
        if not self._files_panel.is_key_navigating():
            self._canvas.setFocus()
        if fresh:
            message = f"Loaded {entry.doc.tile_count} tiles from {entry.name}"
            note = self._partial_tile_note()
            self.statusBar().showMessage(f"{message} - {note}" if note else message)

    def _on_current_entry_changed(self, entry: Entry | None) -> None:
        self._files_panel.set_current(entry)
        # Every way the view can move ends here, which is why the Back/Forward
        # trail is recorded here rather than at the activation call sites.
        self._record_visit(entry)
        if entry is None:
            self._show_empty()
            return
        if data_missing(entry):
            self._show_unavailable(entry)
            return
        # Already loaded on the _activate_entry path; a close() repointing
        # current to a never-activated (or invalidated) neighbour lands here.
        if entry.doc is None and not self._load_entry(entry):
            self._show_unavailable(entry)
            return
        # A file's buffer is the authority for its bytes, but its slices hold
        # their edits in derived buffers of their own until something reconciles
        # them - so reconcile before showing it. Looking at a ROM has to show
        # what was edited through a slice of it; they are the same bytes.
        self._fold_slice_edits_into(entry)
        self._restore_session(entry)
        self._drop_unavailable_edit_mode()
        self._refresh_view()

    def _drop_unavailable_edit_mode(self) -> None:
        """Leave pixel mode when the entry now on screen has no pixels to paint.

        The mode is app-wide interaction state and nothing else resets it on a
        switch (``docs/design/pixel-editing.md``), so without this the canvas keeps
        reporting pixel gestures over a document that cannot take them — a sprite
        object, or a map with nothing bound — with the rail hidden and the toggle
        grey, which reads as "off" and is not. What the gestures would then reach
        is not nothing: on a tilemap they would land in the borrowed tile buffer
        and mark the *map* dirty, whose save writes cells, so the edit would be
        visible until the next repaint and then gone.

        Left alone in every other case: the mode is a preference, and coming back
        to a document that can paint should find the brush where it was put down.
        """
        if self._edit_mode is EditMode.PIXEL and not self._pixel_edit_available():
            self._set_edit_mode(EditMode.TILE)

    def _load_entry(
        self, entry: Entry, *, quiet: bool = False, live: bytes | None = None
    ) -> bool:
        """Load ``entry``'s document through the pipeline; False on failure.

        Runs on first activation and again whenever the cached document was
        invalidated by a save into the same file. A failure is normally reported
        with a modal; ``quiet`` suppresses it so a bulk caller (export over many
        entries) can collect and summarize failures itself instead of stacking
        one dialog per bad entry.

        ``live`` carries a **tilemap**'s unsaved cell bytes into the read that is
        about to replace them, and means nothing on the pixel path — a pixel
        entry is never re-read out from under its edits, and a bound map's tiles
        come from the entry that owns them (:meth:`_live_bound_tiles`)."""
        if entry.session is None:
            entry.session = self._seed_session(entry)
        session = entry.session
        if entry.content_kind is ContentKind.TILEMAP:
            return self._load_tilemap_entry(entry, quiet=quiet, live=live)
        cfg = self._pixel_config(entry, session.pixel_preset_id)
        # A pending bitmap width re-cuts the codec's tile geometry, so it is an
        # input to this first load rather than something the view applied
        # afterwards can express - the entry would otherwise open at the codec's
        # own tile size and only re-cut when a widget was next touched. Read off
        # the pending view because that is where a not-yet-loaded entry's
        # arrangement lives (a restored project, a slice seeded from its parent).
        # Gated on the 2D walk exactly as _effective_bitmap_width is: the width
        # describes a wide-bitmap read and means nothing to a tile-by-tile one.
        pending = entry.pending_view
        width = (
            pending.bitmap_width
            if pending is not None and pending.two_dimensional
            else 0
        )
        try:
            px = pipeline.load_pixel_data(cfg, self._registry, width)
        except PipelineError as exc:
            if not quiet:
                self._report(exc)
            return False
        if backfill_slice_length(entry, px.ctx):
            # The decompressor discovered the slice's true extent: rebuild the
            # config bounded by it, so save-back is slot-enforced from now on.
            cfg = self._pixel_config(entry, session.pixel_preset_id)
            self._files_panel.refresh_entry(entry)
        px, cfg = self._apply_pixel_preset_hint(entry, px, cfg)
        entry.doc = Document(
            pixel_data=px.data,
            bytes_per_tile=px.bytes_per_tile,
            tile_width=px.tile_width,
            tile_height=px.tile_height,
            palette=self._fallback_palette(),
            pixel_config=cfg,
            palette_config=self._placeholder_palette_config(session.palette_preset_id),
            pixel_ctx=px.ctx,
            # A bank states where its own rows count from, and its per-tile table
            # (seeded as pinned regions below) counts from exactly there. Nothing
            # in a preset can say it — it is a fact about this file — so the
            # declared answer is 0 and the header is the only other voice.
            palette_row_base=self._row_base_for(
                entry, 0, stated=False, bank=px.ctx.get(KEY_TILE_PALETTE_ROW_BASE)
            ),
        )
        self._apply_restored_state(entry)
        # After the restore: a project that stored regions of its own has just
        # put them back, and the file's are only a starting point.
        if entry.doc.view.palette_regions.is_empty():
            self._seed_tile_palette_rows(entry, px.ctx.get(KEY_TILE_PALETTE_ROWS, b""))
        return True

    def _load_tilemap_entry(
        self, entry: Entry, *, quiet: bool = False, live: bytes | None = None
    ) -> bool:
        """Load a tilemap entry: its own cells, plus whatever tiles it is bound to.

        Two reads rather than one, into the two halves of the same document. The
        entry's file gives the **cells**; the tile source gives the bytes that
        land in ``pixel_data``, so every tile path — decode, the window slicing,
        ``replace_bytes`` — keeps working over the art rather than over the map
        (``docs/design/tilemap-entry.md`` §8).

        An unbound tilemap still opens. The binding is project state that no
        file states, so a map with nowhere to get tiles from is the ordinary
        first moment of one, not a failure: it loads with no tiles and every
        cell draws blank until it is pointed at a source.

        ``live`` is the entry's unsaved cell bytes, decoded in place of the
        file's where a re-read would otherwise discard them
        (:func:`~celpix.pipeline.pipeline.load_tilemap_data`).
        """
        session = entry.session
        assert session is not None
        # Noted before the restore below consumes it: the width hint applies only
        # to an entry the project had no width for, and by the time it is applied
        # the pending view it would be read off has already been taken
        # (:meth:`~...rendering.RenderingMixin._apply_tilemap_columns`).
        restored = entry.pending_view is not None
        cfg = self._tilemap_config(entry, self._tilemap_preset_id(entry))
        try:
            loaded = pipeline.load_tilemap_data(cfg, self._registry, live)
        except PipelineError as exc:
            if not quiet:
                self._report(exc)
            return False
        through = self._bound_tilemap(entry)
        if through is not None:
            # Bound to another tilemap rather than to art: these cells index that
            # map's *cells*, and the tiles come from whatever it is itself bound
            # to. Two hops, and the second is an ordinary binding — which is as
            # far as it goes (:meth:`_bound_tilemap`).
            entry.doc = Document(
                pixel_data=through.pixel_data,
                bytes_per_tile=through.bytes_per_tile,
                tile_width=through.tile_width,
                tile_height=through.tile_height,
                palette=self._fallback_palette(),
                pixel_config=replace(through.pixel_config, write_enabled=False),
                palette_config=self._placeholder_palette_config(
                    session.palette_preset_id
                ),
                pixel_ctx=through.pixel_ctx,
                cells=loaded.cells,
                # `resolved_cells` follows from this and is not passed: the
                # document derives it, so an edit can re-derive it the same way.
                #
                # The stamp size and the source's width come off the *source's*
                # context, which is the only place either is stated: a PNL panel's
                # header says how big a stamp its callers index in, and a layout's
                # own file has no idea (`docs/design/tilemap-entry.md` §3.1).
                chain=CellChain(
                    through.cells or [],
                    loaded.palette_rows,
                    stamp=self._stamp_tiles(through),
                    source_columns=through.stated_columns,
                ),
                # Writable, like any other tilemap: a cell edit here restamps, and
                # what it writes back is this file's own entry table. The *pixel*
                # config stays read-only above - the art belongs to the map at the
                # end of the chain, and a restamp must never reach it.
                tilemap_config=cfg,
                tilemap_ctx=loaded.ctx,
                tilemap_data=loaded.data,
                # This entry's own record size, unlike the drawing geometry below
                # it: what the hex dump shows here is this file's cells.
                cell_bytes=loaded.cell_bytes,
                # The geometry is the source map's, because what is drawn is its
                # cells: how many tiles one covers, and where its own base puts
                # them, are answers this entry has no version of.
                cell_tiles=through.cell_tiles,
                cell_row_stride=through.cell_row_stride,
                tile_base_index=through.tile_base_index,
                # The source map's too: what is expanded into tiles is its cells,
                # so the field they wrap inside is its format's.
                index_mask=through.index_mask,
                # The source map's rows are what get drawn, so its base applies -
                # unless this entry states one of its own.
                palette_row_base=self._row_base_for(entry, through.palette_row_base),
                # Rows are stated if *either* side states them — the referrer's
                # win where its format has the field, the source's come through
                # where it does not (:func:`_resolve_through`). Either way the
                # view must not add a subpalette row over the top.
                cells_carry_palette_rows=(
                    through.cells_carry_palette_rows or loaded.palette_rows
                ),
            )
            self._apply_restored_state(entry)
            self._apply_tilemap_columns(entry, restored=restored)
            return True
        tiles = self._load_bound_tiles(entry, quiet=quiet)
        # The cell size is the header's answer over the preset's assumption: the
        # file is a better authority on its own geometry than a preset written
        # for the format in general.
        cell_tiles = loaded.ctx.get(KEY_TILEMAP_CELL_TILES) or loaded.cell_tiles
        self._fit_tile_base(entry, loaded.cells, tiles, cell_tiles)
        entry.doc = Document(
            pixel_data=tiles.data,
            bytes_per_tile=tiles.bytes_per_tile,
            tile_width=tiles.tile_width,
            tile_height=tiles.tile_height,
            palette=self._fallback_palette(),
            pixel_config=tiles.config,
            palette_config=self._placeholder_palette_config(session.palette_preset_id),
            pixel_ctx=tiles.ctx,
            cells=loaded.cells,
            # View-only where the cells are subsprites, for the same reason a
            # stamp layout is: what a canvas gesture would edit is not settled
            # (``Document.cells_editable``).
            tilemap_config=(
                replace(cfg, write_enabled=False) if loaded.frames else cfg
            ),
            tilemap_ctx=loaded.ctx,
            tilemap_data=loaded.data,
            cell_bytes=loaded.cell_bytes,
            cell_tiles=cell_tiles,
            cell_row_stride=VRAM_ROW_STRIDE if cell_tiles != (1, 1) else 0,
            index_mask=loaded.index_mask,
            palette_row_base=self._row_base_for(
                entry,
                loaded.palette_row_base,
                stated=loaded.ctx.get(KEY_TILEMAP_PALETTE_ROW_BASE) is not None,
                bank=tiles.ctx.get(KEY_TILE_PALETTE_ROW_BASE),
            ),
            tile_base_index=(
                entry.tile_source.base_index if entry.tile_source is not None else 0
            ),
            sprite_frames=loaded.frames,
            sprite_size_pair=self._size_pair_for(entry, loaded.size_pair),
            cells_carry_palette_rows=loaded.palette_rows,
            text_layout=self._tilemap_is_fontmap(entry),
            font_alphabet=self._font_alphabet_for(entry, tiles, loaded.cell_bytes),
        )
        self._apply_restored_state(entry)
        self._apply_tilemap_columns(entry, restored=restored)
        return True

    def _apply_pixel_preset_hint(self, entry: Entry, px, cfg):  # noqa: ANN001
        """Adopt the format the container says its payload is in, if it says.

        A tile bank that records its own bit depth should not need one guessed:
        2bpp, 4bpp and 8bpp all decode into something that *looks* like graphics,
        so a wrong pick is plausible garbage rather than an obvious error.

        Only the geometry is re-derived — :func:`reinterpret_pixel_data` re-reads
        nothing, so this costs a recompute rather than a second pass over the
        file. Applied only on a **fresh** entry: once a project has stored a
        format, or the user has picked one, that is the answer and a re-read must
        not overrule it.
        """
        wanted = str(px.ctx.get(KEY_PIXEL_PRESET, "") or "")
        session = entry.session
        if not wanted or session is None or entry.pending_view is not None:
            return px, cfg
        if wanted == session.pixel_preset_id:
            return px, cfg
        try:
            regeared = pipeline.reinterpret_pixel_data(
                px.data, px.ctx, cfg, self._registry
            )
        except PipelineError:
            return px, cfg  # the hint named something this build hasn't got
        session.pixel_preset_id = wanted
        return regeared, self._pixel_config(entry, wanted)

    def _seed_tile_palette_rows(self, entry: Entry, table: bytes) -> None:
        """Turn a bank's per-tile palette rows into pinned palette regions.

        The file is saying what pinned regions otherwise have to be told by hand
        — which subpalette row each tile is meant to be read under — so it seeds
        them and they behave like any other pin from there: visible, editable,
        and saved with the project.

        The table's rows are stored as the file states them, which is **relative
        to the entry's palette row base** (the same header states both). So they
        are pinned unshifted and the base reaches them at render, like a cell's
        row — which is what lets the base spin re-aim a whole bank at the palette
        that actually got loaded without rewriting a region.

        Runs of equal rows collapse into one region each, because that is what a
        region *is*; a bank of 1024 tiles usually resolves to a few dozen.

        Row 0 is pinned like any other. It is a row the file *named*, not an
        absence of one — the only other implementation of the format renders it
        through the same base-plus-attribute arithmetic as rows 1-7, and a
        surveyed 4bpp bank uses all eight values with 0 the commonest at 39% of
        tiles. Leaving it out would let exactly those tiles drift with the view's
        subpalette selector while their neighbours stayed put, so the picture
        stops matching the file the moment the view is not on row 0.
        """
        doc = entry.doc
        if doc is None or not table or doc.bytes_per_tile <= 0:
            return
        per_tile = doc.tile_width * doc.tile_height
        if per_tile <= 0:
            return
        regions, start, row = [], 0, table[0]
        for index in range(1, len(table) + 1):
            here = table[index] if index < len(table) else None
            if here != row:
                regions.append(
                    PaletteRegion(start * per_tile, (index - start) * per_tile, row)
                )
                start, row = index, here
        if regions:
            doc.view.palette_regions = PaletteRegions.from_regions(regions)

    def _fit_tile_base(  # noqa: ANN001
        self, entry: Entry, cells: list[Cell], tiles, cell_tiles: tuple[int, int]
    ) -> None:
        """Shift a map onto a source it overflows, when its own indices say how.

        A map's cells and the entry supplying its tiles routinely number from
        different places — the art is often a *slice*, whose tiles start at 0
        whatever the map calls them. The map itself says by how much: scan its
        indices, and if the lowest one is the amount by which the highest
        overflows the source, the map is the same picture shifted and
        ``-min`` lands it (:class:`~celpix.project.workspace.TileSource`).

        Deliberately narrow, because the guess has a wrong answer as well as a
        right one. A map bound to a *whole* bank indexes it absolutely and needs
        no shift, so this only fires when the map does not fit as it stands and
        does fit once shifted — a condition an absolutely-indexed map never meets.
        It also never overrides a base the user set, and never runs on a map with
        no binding to be judged against.
        """
        source = entry.tile_source
        if source is None or not source.is_bound or source.base_index or not cells:
            return
        count = len(tiles.data) // max(1, tiles.bytes_per_tile)
        if not count:
            return  # unreadable binding: nothing to fit against
        indices = [cell.index for cell in cells]
        low, high = min(indices), max(indices)
        # A cell covering several tiles reaches past its own index, so the span
        # has to allow for what the widest of them draws.
        across, down = max(1, cell_tiles[0]), max(1, cell_tiles[1])
        reach = high + (down - 1) * VRAM_ROW_STRIDE + (across - 1)
        if low and reach >= count and reach - low < count:
            entry.tile_source = replace(source, base_index=-low)

    def _row_base_for(
        self,
        entry: Entry,
        declared: int,
        *,
        stated: bool = True,
        bank: int | None = None,
    ) -> int:
        """The palette row ``entry``'s named rows count their row 0 from.

        Four answers, most specific first, resolved here so the document carries
        the base **in force** and every render reads one number rather than
        choosing between several. One question for both kinds of entry: what a
        tilemap's cells count from, a tile bank's pinned rows count from too.

        The entry's own value wins outright: what no file can know is which
        palette got loaded (:attr:`~celpix.project.workspace.Entry.palette_row_base`).
        Then the map's own header, where its format has one — ``stated`` is
        whether ``declared`` came from the file rather than from the preset. A
        pixel entry has no such field, so it passes ``stated=False`` and a
        declared 0.

        Then, and this is the one that needs saying, **the bank's**. A sprite
        object names a 3-bit palette row and carries nothing to count it from,
        so the preset's 8 is standing in for the commonest case rather than
        reading anything; the tile bank those subsprites draw from *does* state
        a base, and it is the same origin its own per-tile row table counts from
        (:data:`~celpix.core.context.KEY_TILE_PALETTE_ROW_BASE`). Where the art
        says, the art wins over a constant. The preset is the last resort, for a
        bank whose header is absent or a format with no such field at all.
        """
        chosen = entry.palette_row_base
        if chosen is not None:
            return chosen
        if stated or bank is None:
            return declared
        return bank

    def _size_pair_for(
        self, entry: Entry, declared: tuple[int, int]
    ) -> tuple[int, int]:
        """The sprite-size pair in force: the entry's choice, else the format's.

        Both are (small, large) **in tiles**. ``declared`` is the preset's, which can
        only be the commonest setting — the pair was a PPU register the scene set and
        no sprite file records it
        (``docs/graphics-formats-reference/scgcad-formats.md`` §8.2). So the entry
        overrules it, and the document carries the pair **in force** the way it does
        the two bases, leaving one answer for the render to read.
        """
        chosen = entry.sprite_size_pair
        if chosen is None or chosen[0] < 1 or chosen[1] < 1:
            return declared
        return chosen

    def _tilemap_preset_id(self, entry: Entry) -> str:
        """The cell format ``entry``'s own file is read under.

        A container names one when the file is opened
        (``detect.tilemap_preset_for``), so the fallback is for a tilemap carved
        out by hand, which had no container to have said.
        """
        return entry.tilemap_preset_id or _DEFAULT_TILEMAP_PRESET

    def _tilemap_declares(self, entry: Entry, name: str) -> object:
        """What ``entry``'s cell **format** declares under ``name``, or None.

        The format's answer and not the document's, which is the whole of what
        these declarations are for: they are readable before anything is loaded
        or bound, so the binding bar can describe an entry it has not read yet.
        An object with no tile source is still an object, and a stamp layout with
        none is still a stamp layout (:meth:`_tilemap_is_sprite`,
        :meth:`_tilemap_is_indirect`).

        None for a preset id nothing is registered under, which is the same
        answer as a preset that declares nothing: a format celPix does not have
        cannot have claimed anything about its cells.
        """
        try:
            preset = self._registry.preset(self._tilemap_preset_id(entry))
        except KeyError:
            return None
        return preset.params.get(name)

    def _tilemap_is_fontmap(self, entry: Entry) -> bool:
        """Whether ``entry``'s **format** says its cells are character codes.

        A *fontmap* is the tilemap variant whose cells index a font rather than
        an arbitrary tile bank, so they can be read as words
        (``docs/design/fontmap-entry.md``). Declared rather than inferred, for
        the reason every one of these is: it has to answer before anything is
        loaded or bound, and a string with no font picked is still a string —
        which is precisely when the user wants the text window, to be told the
        codes mean nothing yet.
        """
        return self._tilemap_declares(entry, "layout") == "text"

    def _tilemap_flag_break(self, entry: Entry) -> bool:
        """Whether ``entry``'s format ends a line on a bit rather than a code.

        The presence of the codec's ``terminator`` field, asked of the preset for
        the same reason ``controls`` is: it is the *stream's* punctuation, and the
        alphabet has to know before a newline can be typed into one
        (:attr:`~celpix.core.font.Alphabet.flag_break`).
        """
        return bool(self._tilemap_declares(entry, "terminator"))

    def _font_alphabet_for(self, entry: Entry, tiles: _BoundTiles, cell_bytes: int):
        """The lookup ``entry``'s codes read through — its font's, plus its own.

        The halves meet here because this is the only place that holds both: the
        **font** is whatever ``entry`` is bound to, and its table is that entry's
        own data (:attr:`~celpix.project.workspace.Entry.font_chars`,
        :attr:`~celpix.project.workspace.Entry.font_codes` and the origin
        :attr:`~celpix.project.workspace.Entry.font_base` beside them); the
        **controls** are on ``entry``'s own cell format. Neither knows about the
        other, and nothing downstream should have to ask twice
        (:func:`~celpix.pipeline.pipeline.load_font_alphabet`).

        Read only from a sheet that says it is a font — **Use as Font**
        (:attr:`~celpix.project.workspace.Entry.use_as_font`). Unticking keeps the
        table, since it is the user's work, so reading it anyway would leave the
        tick meaning nothing.

        ``cell_bytes`` sets how wide an unmapped code prints, so a one-byte
        stream says ``[$1F]`` and a two-byte one ``[$FFFE]``. It is the stream's
        measure and not the font's: the same sheet may be indexed at either
        width, and a code shown at the wrong one does not type back.
        """
        if not self._tilemap_is_fontmap(entry):
            return None
        bound = self._binding_target(entry.tile_source) if entry.tile_source else None
        font = bound if bound is not None and bound.use_as_font else None
        controls = self._tilemap_declares(entry, "controls") or ()
        return pipeline.load_font_alphabet(
            font.font_chars if font is not None else "",
            font.font_codes if font is not None else (),
            tiles.ctx,
            controls=controls,
            code_digits=max(1, cell_bytes) * 2,
            base=font.font_base if font is not None else 0,
            flag_break=self._tilemap_flag_break(entry),
        )

    def _tilemap_is_sprite(self, entry: Entry) -> bool:
        """Whether ``entry``'s **format** says its cells are subsprites.

        A *sprite map* is the tilemap variant whose cells are freely-placed
        subsprites grouped into frames rather than positions in a grid
        (``docs/design/tilemap-entry.md`` §6). Declared rather than inferred, so
        it answers before anything is loaded (:meth:`_tilemap_declares`): an
        object with no tile source is still an object, and its size pair is still
        the control it needs.
        """
        return self._tilemap_declares(entry, "layout") == "sprite"

    def _tilemap_states_subsprite_size(self, entry: Entry) -> bool:
        """Whether ``entry``'s **format** gives each subsprite its own rectangle.

        The sprite records split two ways on this. Most hold a size *bit* picking
        between two squares the file never records, so the pair is a setting the
        user supplies (:data:`~celpix.core.sprite.DEFAULT_SUBSPRITE_TILES`); a
        Mega Drive record holds the console's own size nibble and states a
        rectangle outright, so there is nothing to resolve and no pair to offer.

        Declared rather than inferred, on the rule every one of these follows: it
        has to answer before anything is loaded or bound.
        """
        return self._tilemap_declares(entry, "subsprite_size") == "stated"

    def _tilemap_columns_hint(self, entry: Entry) -> int:
        """The width the entry's format states, or 0 when it states none.

        Read back off the loaded document's context rather than re-read, since
        only the container knows and it has already said.
        """
        doc = entry.doc
        if doc is None or not doc.is_tilemap:
            return 0
        return doc.stated_columns

    @staticmethod
    def _stamp_tiles(through: Document) -> tuple[int, int]:
        """How many of ``through``'s cells one coordinate into it names.

        The source map's own answer, published by its container from its header
        (:data:`~celpix.core.context.KEY_TILEMAP_STAMP_TILES`). ``(1, 1)`` for
        every format that states nothing, which is the reading that leaves a chain
        resolving one coordinate to one cell.
        """
        stated = through.tilemap_ctx.get(KEY_TILEMAP_STAMP_TILES)
        if not stated:
            return (1, 1)
        across, down = stated
        return max(1, int(across)), max(1, int(down))

    def _tilemap_is_indirect(self, entry: Entry) -> bool:
        """Whether ``entry``'s **format** says its cells are coordinates.

        Declared rather than inferred, so it is an answer before any binding
        exists (:meth:`_tilemap_declares`) — which is the only thing it is for.
        Chaining itself is generic and gated on depth (:meth:`_bound_tilemap`), so
        this never decides what a map may draw through; it decides how the bar
        reads while nothing is bound yet. A stamp layout with no source is still a
        stamp layout, and should be offered PNL panels first and no Base tile rather
        than being described as a map that merely has not picked its art.
        """
        return bool(self._tilemap_declares(entry, "indirect"))

    def _binding_target(self, source: TileSource) -> Entry | None:
        """The open entry ``source`` names, or None when it names nothing usable.

        The one place a binding becomes a usable entry
        (:class:`~celpix.project.workspace.TileSource`). Everything that asks
        where a map's tiles come from resolves it here — the depth gate below, the
        chained load, the pathway that reads the tiles, and the bar's combo, note
        and jump button — so a binding that no longer names anything reads the
        same way to all of them instead of each carrying its own check.

        The check is that the entry is still **open**, which is the one thing
        holding it by identity cannot answer on its own: a closed entry is a live
        object that undo may yet put back, so the binding keeps it and simply does
        not resolve while it is out of the list. Scanned by identity rather than
        by ``in``, which would ask :class:`Entry` for an equality it deliberately
        does not have.
        """
        if source.mode is not TileMode.ENTRY:
            return None
        entry = source.entry
        if entry is None or not any(
            open_ is entry for open_ in self._workspace.entries
        ):
            return None
        return entry

    def _draws_through_tilemap(self, entry: Entry) -> bool:
        """Whether ``entry``'s binding names another tilemap rather than art.

        Read off the **binding**, not off a loaded document, which is what makes
        it safe to ask while loading: the answer needs no pipeline run, so the
        depth gate in :meth:`_bound_tilemap` can settle a chain before anything
        is read and a pair of maps pointed at each other cannot recurse.
        """
        if entry.content_kind is not ContentKind.TILEMAP:
            return False
        source = entry.tile_source
        bound = self._binding_target(source) if source is not None else None
        return (
            bound is not None
            and bound is not entry
            and bound.content_kind is ContentKind.TILEMAP
        )

    def _can_supply_tiles(self, entry: Entry, candidate: Entry) -> bool:
        """Whether ``candidate`` is a source ``entry`` could draw through.

        The one rule behind both the binding combo and the "From file..." check,
        so what is offered and what is accepted cannot disagree: art always, and
        a tilemap only while it reaches art itself.

        Never the entry itself, which would bind it to its own bytes, and never a
        bookmark, which marks a position rather than holding content.
        """
        if candidate is entry or candidate.kind is EntryKind.BOOKMARK:
            return False
        if candidate.content_kind is ContentKind.PIXELS:
            return True
        if candidate.content_kind is not ContentKind.TILEMAP:
            return False
        return not self._draws_through_tilemap(candidate)

    def _bound_tilemap(self, entry: Entry) -> Document | None:
        """The tilemap ``entry`` draws through, loaded — or None if it draws art.

        Any tilemap may take its cells from another tilemap's; what stops the
        chain is **depth, not format** (``docs/design/tilemap-entry.md`` §3.1).
        One hop is resolved, and the map at the end of it must reach a graphics
        file itself, because a coordinate into a coordinate has no defined
        meaning — an index would be resolved against cells that are not tiles.

        The gate is :meth:`_draws_through_tilemap`, checked on the binding before
        the source is loaded. That ordering is what keeps this from recursing: two
        maps bound to each other both fail the gate rather than each loading the
        other, and the ``is_indirect`` check the loaded document would offer is
        then redundant — a source that passed the gate cannot come back resolved.

        Loading the source is the ordinary entry load, so it settles its *own*
        binding first and the second hop is an ordinary one.
        """
        source = entry.tile_source
        candidate = self._binding_target(source) if source is not None else None
        if candidate is None or not self._can_supply_tiles(entry, candidate):
            return None
        if candidate.content_kind is not ContentKind.TILEMAP:
            return None
        if candidate.doc is None and not self._load_entry(candidate, quiet=True):
            return None
        doc = candidate.doc
        # A sprite object holds records at signed pixel offsets, not a grid to
        # index into, so there is no cell at position N to stamp.
        if doc is None or not doc.is_tilemap or doc.is_sprite:
            return None
        return doc

    def _tile_bank_owner(self, entry: Entry) -> Entry | None:
        """The pixel entry whose bytes ``entry`` draws its tiles from.

        A tilemap's ``pixel_data`` is a *copy* of that entry's art, read through
        its pathway — so a pixel edit made on the map has to be deposited into the
        entry that owns those bytes rather than spliced into the borrowed buffer,
        which nothing else can see and no save would write. This is the same "one
        region, one authority" rule a slice follows into its parent
        (``docs/design/slices-and-parents.md``); the owner here is reached through
        the binding instead of through the file list.

        Walks the binding to the art: one hop for an ordinary map, two for a
        chained one, whose tiles belong to the map at the end of the chain rather
        than to the stamps in between.

        **Every hop is gated, not just the first.** The depth rule is checked
        where a binding is *made* (:meth:`_can_supply_tiles`), and a source can
        gain a binding of its own afterwards — bind `a` to `b` while `b` is
        unbound, then bind `b` to `c`, and `a` is suddenly three deep with nothing
        having re-asked. Walking ungated then found art at the end of a chain the
        resolution refuses (:meth:`_bound_tilemap` stops at the same gate), so the
        map drew a coordinate file as pixels and a pen stroke deposited into a
        real art file the user was not looking at. Re-asking per hop is what keeps
        this answer, the load, and the bar's note saying one thing.

        None when the binding names nothing, names something that is not art, or
        reaches it only through a chain too deep to resolve: an edit with no owner
        is refused rather than deposited into a guess.
        """
        seen: set[int] = set()
        at: Entry | None = entry
        while at is not None and id(at) not in seen:
            seen.add(id(at))
            if at.content_kind is ContentKind.PIXELS:
                return at
            if at.content_kind is not ContentKind.TILEMAP:
                return None
            source = at.tile_source
            target = self._binding_target(source) if source is not None else None
            if target is None or not self._can_supply_tiles(at, target):
                return None
            at = target
        return None

    def _entries_bound_to(self, owner: Entry) -> list[Entry]:
        """Every open entry that draws its tiles from ``owner``'s bytes.

        The audience for a change to those bytes: each of them holds a decoded
        copy, so an edit made on any one of them — or on ``owner`` itself — has to
        reach the rest or two views of one bank drift apart
        (:meth:`~celpix.ui.main_window.tile_bytes.TileBytesMixin._apply_pixel_bytes`).

        Resolved through :meth:`_tile_bank_owner`, so a chained map counts as
        drawing from the bank at the end of its chain — which is where its own
        ``pixel_data`` came from.
        """
        return [
            other
            for other in self._workspace.entries
            if other is not owner
            and other.doc is not None
            and other.doc.is_tilemap
            and self._tile_bank_owner(other) is owner
        ]

    def _maps_drawing_from(self, owners: list[Entry]) -> list[Entry]:
        """Every open map whose art comes from one of ``owners``.

        The audience for an owner *arriving or leaving*, where
        :meth:`_entries_bound_to` is the audience for its bytes changing — same
        question, asked of several owners because a file is removed with its
        slices and a map may be bound to any of them.

        The answer has to be taken **before** a removal and **after** a restore:
        a binding resolves only while the entry it names is open
        (:meth:`_binding_target`), so at the other end of either there is nothing
        left to ask.
        """
        found: list[Entry] = []
        for owner in owners:
            for other in self._entries_bound_to(owner):
                if not any(other is seen for seen in found):
                    found.append(other)
        return found

    def _reresolve_bound_art(self, maps: list[Entry]) -> None:
        """Re-read ``maps`` against whatever their bindings reach **now**.

        A map holds a decoded *copy* of its bank, so closing that bank — or an
        undo putting it back — changes nothing about the map until it is read
        again: it went on drawing art out of a file no longer in the list, and
        the "no tiles bound" state a map with an unresolved source is supposed to
        show never arrived (``docs/design/tilemap-entry.md`` §1). That is the
        arriving-and-leaving twin of :meth:`_resync_tile_bindings`, which patches
        the same copies when the bytes change underneath them.

        An ordinary re-read, so an unsaved cell edit rides across it and a map
        bound through a chain resolves its hop exactly as a fresh load would.
        Quiet, because the gesture was about another entry: a map whose own file
        has since gone missing must not put a modal in front of a removal.

        Entries closed along with the owner are skipped — nothing is left to
        redraw them into — and the view is only repainted if one of them is what
        is on screen, which is the same signal :meth:`_rechain_dependents` gives
        its caller.
        """
        repaint = False
        for entry in maps:
            if not any(open_ is entry for open_ in self._workspace.entries):
                continue
            if not self._reread_tilemap(entry, quiet=True):
                continue
            if entry is self._workspace.current:
                self._doc = entry.doc
                repaint = True
        if repaint:
            self._drop_unavailable_edit_mode()
            self._refresh_view()

    def _resync_tile_bindings(
        self, owner: Entry, splices: list[tuple[int, bytes]]
    ) -> None:
        """Carry a pixel edit into every open map that borrows ``owner``'s bytes.

        A map holds a decoded **copy** of the bank it is bound to, so an edit to
        those bytes reaches it only if it is put there. That is the pixel twin of
        :meth:`_rechain_dependents`, which does the same for the cells a chained
        map borrows, and it runs in both directions from one place: whether the
        stroke was made on the bank's own entry or on a map drawing through it,
        the bytes end up in ``owner`` and every other view of them is caught here.

        The same splices, because the copies were decoded through the *same*
        pathway — ``_tile_source_config`` builds the map's reader from the bound
        entry's own config, so the two buffers hold the same bytes at the same
        offsets and a splice that is right for one is right for the other. Each
        map's cached bank is patched rather than dropped, so the repaint the
        caller is about to do costs only the tiles that changed
        (``docs/design/tilemap-entry.md`` §8.2).
        """
        for other in self._entries_bound_to(owner):
            self._land_splices(other.doc, splices)

    def _rechain_dependents(self, entry: Entry) -> bool:
        """Re-point every open map drawing through ``entry`` at its new cells.

        True when one of them is the entry on screen, so the caller knows a
        repaint is owed — which happens when an undo lands on a map the view has
        since moved off, onto one that draws through it.

        A cell edit replaces the entry's cell list rather than mutating it, so a
        chained map still holding the old one would keep drawing the stamps as
        they were (:class:`~celpix.core.document.CellChain`). Called from the one
        place a cell list changes, which is what keeps two views of the same stamps
        in step without either being reloaded.
        """
        cells = entry.doc.cells if entry.doc is not None else None
        if cells is None:
            return False
        current = False
        for other in self._workspace.entries:
            doc = other.doc
            source = other.tile_source
            if other is entry or doc is None or doc.chain is None:
                continue
            if source is None or source.entry is not entry:
                continue
            doc.chain = replace(doc.chain, source=cells)
            doc.resolve()
            current = current or other is self._workspace.current
        return current

    def _load_bound_tiles(self, entry: Entry, *, quiet: bool = False) -> _BoundTiles:
        """The tiles a tilemap entry draws from, or an empty stand-in.

        A binding that cannot be read degrades to no tiles rather than failing
        the entry, on the same rule a missing palette follows: the map is still
        worth showing, and the binding is the part the user can re-point.
        """
        source = entry.tile_source
        if source is None or not source.is_bound:
            return self._no_tiles()
        # The bank's own region settles first: its bytes are what this read is
        # about to take, and a slice of it may owe them
        # (:meth:`~...writing.WritingMixin._settle_region`).
        self._settle_region(source.entry)
        try:
            cfg = self._tile_source_config(entry, source)
        except (PipelineError, KeyError) as exc:
            if not quiet:
                self._report_tile_binding(entry, exc)
            return self._no_tiles()
        live = self._live_bound_tiles(source, cfg)
        if live is not None:
            return live
        try:
            px = pipeline.load_pixel_data(cfg, self._registry)
        except (PipelineError, KeyError) as exc:
            if not quiet:
                self._report_tile_binding(entry, exc)
            return self._no_tiles()
        return _BoundTiles(
            px.data, px.bytes_per_tile, px.tile_width, px.tile_height, px.ctx, cfg
        )

    def _live_bound_tiles(
        self, source: TileSource, cfg: PathwayConfig
    ) -> _BoundTiles | None:
        """The bound entry's **loaded** art, taken from its document rather than read.

        A binding names an entry and not a path precisely so the map draws what
        that entry currently holds (:class:`
        ~celpix.project.workspace.TileSource`) — and an entry's unsaved edits live
        only in its document, so re-reading the pathway would show the file as it
        was on disk. That is the difference between a bank edited in its own view
        showing through in the map straight away and not showing at all until
        both are saved; with pixel editing *through* a map it is sharper still,
        since a rebind re-reads and would take an undeposited edit back out
        (``docs/design/tilemap-entry.md`` §8.4).

        **The same rule as** :func:`~celpix.project.workspace.entry_view_bytes`,
        which is that rule's single definition — "the live document's bytes when
        one is loaded, else the region read fresh" — and this is a second
        implementation of it rather than a caller. They are not merged because
        that function returns ``(data, base)`` and drops the
        :class:`~celpix.core.context.PipelineContext` a bound read has to carry,
        and because the geometry below has no counterpart there. Anything that
        changes what "live bytes" means has to change both; that is the cost of
        the split, recorded here so it is not discovered.

        None when there is no document to take, which is the ordinary case on a
        project load: entries are lazy, and the pathway read below is what fills
        this in. The config still comes from the bound entry either way, so the
        two routes produce the same bytes — this one just cannot be stale.

        The **geometry** is taken from that document too, not from the config: a
        document read under one pixel preset and a config naming another would
        cut the same buffer into different tiles.
        """
        bound = self._binding_target(source)
        doc = bound.doc if bound is not None else None
        if doc is None or doc.is_tilemap:
            return None
        return _BoundTiles(
            doc.pixel_data,
            doc.bytes_per_tile,
            doc.tile_width,
            doc.tile_height,
            doc.pixel_ctx,
            cfg,
        )

    def _no_tiles(self) -> _BoundTiles:
        """The stand-in for an unbound or unreadable source: geometry, no bytes.

        The tile size still comes from a real codec so the cells have a size to
        be drawn at — a blank map at the right scale reads as "no tiles yet",
        where a zero-sized one reads as a broken window.
        """
        preset_id = self._pixel_preset_id() or _DEFAULT_PIXEL_PRESET
        cfg = PathwayConfig(
            source=FileRef(""), interpret_preset_id=preset_id, write_enabled=False
        )
        try:
            engine, preset = self._registry.engine_for(preset_id)
            width, height = engine.tile_size(preset.params)
            per_tile = engine.bytes_per_tile(preset.params)
        except Exception:  # noqa: BLE001 — a stand-in must not be able to fail
            width = height = 8
            per_tile = 32
        return _BoundTiles(b"", per_tile, width, height, PipelineContext(), cfg)

    def _tilemap_config(self, entry: Entry, preset_id: str) -> PathwayConfig:
        """The pathway that reads ``entry``'s own file as cells.

        Built from the entry's own container and reshape — the map *is* this
        file — where the pixel config a tilemap carries points somewhere else
        entirely. The two configs on a tilemap document address different files,
        which is the point.
        """
        return PathwayConfig(
            source=FileRef(entry.paths, entry.slice_offset, entry.slice_length),
            interpret_preset_id=preset_id,
            container_id=entry.container_id,
            reshape_id=entry.reshape_id,
            compression_id=entry.compression_id,
            # A map's cells are this entry's own data, so a compressed one meets
            # the same slot with the same room to spare as a pixel slice does.
            slot_fill=entry.slot_fill,
        )

    def _tile_source_config(self, entry: Entry, source: TileSource) -> PathwayConfig:
        """The pathway that reads the tiles ``source`` points at.

        Resolved through the bound entry's own config, so the tiles are read
        exactly as that entry reads them — its container, its reshape, its pixel
        codec, and, when it has unsaved edits, its live buffer rather than the
        stale file (``pixel_config_for``). That is what makes an edit to the art
        show through in the map straight away, and why binding names an entry
        rather than a file: a file would have to restate all of it.
        """
        bound = self._binding_target(source)
        if bound is None:
            name = source.entry.name if source.entry is not None else "nothing"
            raise KeyError(f"the tiles are bound to {name}, which is not open")
        if not self._can_supply_tiles(entry, bound):
            # Refused here as well as at the binding, because a source can gain a
            # binding of its own after this one was made and nothing re-asks
            # (:meth:`_tile_bank_owner`). Without it the map reads a file of
            # *coordinates* through a pixel codec and draws it as art — a picture
            # the bar is at that moment describing as not resolved.
            raise KeyError(f"{bound.name} draws through a tilemap itself")
        preset = (
            bound.session.pixel_preset_id
            if bound.session is not None
            else self._pixel_preset_id()
        )
        # Read exactly as that entry reads itself, but **never written**: the
        # tiles belong to the bound entry, which saves them itself. Without this
        # the map's own Write would deposit into a second file the user never
        # asked to save (``docs/design/tilemap-entry.md`` §3).
        return replace(
            self._pixel_config(bound, preset),
            write_enabled=False,
            writes_through_parent=False,
        )

    def _report_tile_binding(self, entry: Entry, exc: Exception) -> None:
        """Say the tiles could not be read, without implying the map failed."""
        self._alert(
            f"{entry.name}: could not read the tiles it is bound to.",
            detail=str(exc),
        )

    def _apply_restored_state(self, entry: Entry) -> None:
        """Apply project-restored view/palette state on the document's first load.

        One-shot: the pending fields are consumed. A palette that can't be
        restored (vanished file, bad offset) degrades the entry to the default
        palette - a project load never fails on it.
        """
        doc = entry.doc
        assert doc is not None and entry.session is not None
        if entry.pending_view is not None:
            doc.view = entry.pending_view
            entry.pending_view = None
        source, entry.pending_palette = entry.pending_palette, None
        if source is not None:
            self._restore_palette_source(entry, source)

    def _seed_session(self, entry: Entry) -> EntrySession:
        """A new entry's starting UI state, seeded from the live toolbar so a
        freshly opened file keeps the codec the user is working in. A slice's
        preview combo starts at none - its bytes are already decompressed."""
        return EntrySession(
            pixel_preset_id=self._pixel_preset_id(),
            palette_preset_id=self._palette_preset_id(),
            preview_compression_id=(
                NO_COMPRESSION
                if entry.kind is EntryKind.SLICE
                else self._compression_id()
            ),
        )

    def _capture_session(self) -> None:
        """Snapshot the live toolbar/view state into the current entry, so
        switching back later restores exactly this setup."""
        entry = self._workspace.current
        # A missing (unavailable) entry has no live document driving the
        # widgets, so there is nothing to snapshot - capturing here would
        # overwrite its restored session with stale, disabled widget values.
        if entry is None or entry.doc is None:
            return
        entry.doc.view.tile_offset = self._offset
        entry.doc.view.byte_nudge = self._nudge
        entry.session = EntrySession(
            pixel_preset_id=self._pixel_preset_id(),
            palette_preset_id=self._palette_preset_id(),
            palette_mode=self._palette_mode,
            preview_compression_id=self._compression_id(),
            selected_tile=self._selected_tile,
            selected_last=self._selected_last,
            selection_slots=self._rect_size,
        )

    def _restore_session(self, entry: Entry) -> None:
        """Push ``entry``'s cached state into the toolbar/nav widgets.

        Every widget is set with its signals blocked (the _repopulate_presets
        pattern): the restore must be one coherent swap followed by a single
        _refresh_view, not a cascade of per-widget reloads.
        """
        assert entry.doc is not None and entry.session is not None
        session, view = entry.session, entry.doc.view
        self._doc = entry.doc
        # The dock follows what is on screen: a document's own palette takes over
        # from any .pal that was filling an otherwise empty dock.
        self._clear_palette_preview()
        # Undo any disabling from a previously shown missing entry.
        self._set_document_ui_enabled(True)
        # The pixel combo goes through the filter, which force-shows the restored
        # format even when hidden (you can't hide the format in force).
        self._fill_pixel_combo(session.pixel_preset_id)
        for combo, data in (
            (self._palette_preset, session.palette_preset_id),
            (self._compression, session.preview_compression_id),
        ):
            select_combo_data(combo, data)
        # The four arrangement axes move as one coherent change, through the
        # method that owns that rule.
        self._set_arrangement(
            view.block_columns, view.block_rows, view.block_order, view.two_dimensional
        )
        spins = (
            (self._columns, view.columns),
            (self._rows, view.rows),
            (self._zoom, view.zoom),
            (self._subpalette, view.subpalette_row),
            (self._bitmap_width, view.bitmap_width),
        )
        # The grid is deliberately absent: it is project-wide, so an entry switch
        # leaves it exactly where the user set it.
        with signals_blocked(*(w for w, _ in spins)):
            for spin, value in spins:
                spin.setValue(value)
        # The Cols the outgoing entry had before its bitmap width took over means
        # nothing to this one, whose Cols has just been restored from its own
        # view - drop it before the sync below can read it back.
        self._columns_before_bitmap = None
        # Reselect the Pattern preset (or Custom) that matches the block/order/2D
        # values just restored, and lock the controls to match.
        self._sync_pattern_selection()
        is_file = entry.kind is EntryKind.FILE
        self._offset, self._nudge = view.tile_offset, view.byte_nudge
        # The rearrangement belongs to the entry, like the offset: switching away
        # and back must find the tiles where they were left. Any drag in flight
        # belonged to the entry being left, so it goes with it.
        self._cancel_rearrange_drag()
        self._tile_rearrangement = view.tile_rearrangement.bounded(entry.doc.tile_count)
        self._show_rearranged = view.show_rearranged
        self._sync_rearrange_actions()
        # A sprite map's frame count is the entry's too, and for the reason the
        # rearrangement is: switching away and back must show the same sheet. The
        # box itself is filled from the binding bar's own refresh, which runs at
        # the tail of the render this leads into.
        self._show_all_frames = view.show_all_frames
        # And the backdrop toggle with it: which cells read as empty is a fact
        # about the map being looked at, so it must not follow the user from the
        # last entry onto this one.
        self._transparent_zero = view.transparent_zero
        # Pinned palette regions belong to the entry for the same reason. Stored
        # unbounded — _active_palette_regions clips at render time against the
        # picture and the palette that are actually loaded, so a region survives a
        # codec switch that temporarily puts it out of range. Whether they are
        # *shown* does not switch with the entry: that is an app-wide preference
        # in QSettings (``main_window/palette_regions.py``).
        self._palette_regions = view.palette_regions
        self._selected_tile = session.selected_tile
        self._selected_last = (
            session.selected_last
            if session.selected_last is not None
            else session.selected_tile
        )
        # A stored rectangle is re-resolved against the view that was restored
        # with it, so it comes back covering the same cells it was drawn over.
        self._rect_size, self._rect_tiles = None, ()
        if session.selected_tile is not None and session.selection_slots is not None:
            tiles = self._rect_tiles_for(
                session.selected_tile - self._offset, *session.selection_slots
            )
            if tiles:
                self._rect_size, self._rect_tiles = session.selection_slots, tiles
                self._selected_last = max(tiles)
        self._sync_selection_actions()
        self._set_palette_mode(session.palette_mode)  # also arms Write
        self._set_file_actions_enabled(is_file)
        self._refresh_window_title()

    def _set_file_actions_enabled(self, enabled: bool) -> None:
        """Arm (or disarm) the actions only a whole FILE entry offers.

        Slices and bookmarks don't nest, and a file's byte stream is always raw,
        so its positions map straight to file offsets. A slice also has no
        container of its own — it reads through its parent's coordinates — so
        that follows the same rule. One list, so the enable and the disable paths
        cannot disagree about what is on it.
        """
        for action in (
            self._new_slice_action,
            self._new_slice_from_view_action,
            self._new_bookmark_action,
            self._change_container_action,
            self._container_info_action,
        ):
            action.setEnabled(enabled)

    def _clear_document_view(self) -> None:
        """Blank the canvas and disable every document-bound action - shared by
        the nothing-open and missing-file (unavailable) states."""
        self._doc = None
        self._selected_tile = None
        self._selected_last = None
        self._rect_size, self._rect_tiles = None, ()
        self._canvas.set_selection(None)
        # No render runs on the way out of a document, so the sprite object's
        # pick has to be dropped here as well as in the refresh — an outline left
        # behind would sit over whatever is shown next.
        self._set_picked_subsprite(None)
        self._sync_selection_actions()
        # Called after _doc is cleared, so it settles on "unavailable": the tool
        # disarms and both its switches grey out. They need saying explicitly
        # because they are shown in the Edit menu as well as on the transform bar,
        # and a menu row does not inherit that bar's disabled state.
        self._sync_rearrange_actions()
        # Also after _doc is cleared, and here rather than only in the refresh:
        # closing a tilemap runs no render, so the binding bar would otherwise
        # stay on screen describing an entry that is gone.
        self._sync_tilemap_bar()
        self._canvas.set_image(QImage())
        # Also after _doc is cleared: nothing else runs on the way out of a
        # document, so the tile size would otherwise still read the old entry's.
        self._refresh_tile_size()
        self._overlay.hide_overlay()
        self._animation.hide_overlay()
        self._animation_action.setEnabled(False)
        self._subsprites.hide_overlay()
        self._subsprites_action.setEnabled(False)
        self._text.hide_overlay()
        self._text_action.setEnabled(False)
        self._font_alphabet.hide_overlay()
        self._font_alphabet_action.setEnabled(False)
        self._hex_panel.clear()
        # No document, no palette source - blank the dock's per-mode widgets
        # (the mode member itself is left alone: it still mirrors the entry's
        # session, which a later _restore_session re-applies).
        self._palette_offset_edit.hide()
        self._palette_offset_prev.hide()
        self._palette_offset_next.hide()
        self._palette_file_label.hide()
        self._palette_format_label.hide()
        self._palette_preset.hide()
        self._sync_palette_export_action()  # no document, nothing to export
        self._sync_palette_mode_items()  # ...and only File left to load
        self._write_action.setEnabled(False)
        self._set_file_actions_enabled(False)

    def _set_document_ui_enabled(self, enabled: bool) -> None:
        """Grey out (or restore) the document-editing surfaces in one go.

        A missing (unavailable) entry has no document to drive, so its codec,
        arrangement and view toolbars and the palette dock are disabled until a
        real document is shown again.

        The interpretation bars stay live with *nothing* open — they configure how
        the next file will be read. The transform bar does not: flip/rotate and the
        mode toggles act on a document, so it follows ``_doc`` itself, the same
        gate the Edit ▸ mode toggles use.
        """
        for bar in (
            self._codecs_toolbar,
            self._arrange_toolbar,
            self._view_toolbar,
        ):
            bar.setEnabled(enabled)
        self._transform_toolbar.setEnabled(enabled and self._doc is not None)
        self._palette_dock.setEnabled(enabled)
        # The tools rail is only live in pixel mode with a document to paint on.
        self._tools_panel.setEnabled(enabled and self._edit_mode is EditMode.PIXEL)

    def _show_empty(self) -> None:
        """Nothing open: clear the canvas, disable everything document-bound.

        The palette dock stays live rather than blank - it shows the generated
        default read-only, which is what a file would open on anyway, and the
        modes that need a graphic are the ones disabled (see
        :meth:`_sync_palette_mode_items`).
        """
        self._clear_document_view()
        self._set_document_ui_enabled(True)  # idle, but live for the next open
        self._set_palette_mode(PaletteMode.DEFAULT)
        self._refresh_palette_dock()
        # Unlike the palette, which has a read-only default to fall back on, a
        # tile sheet with no document is nothing - so this empties it and says so.
        self._refresh_tile_source()
        self._refresh_window_title()
        self._sync_nav()
        # Nothing open reads as pixels, so this is what hands the shape picker
        # back after a tilemap was closed.
        self._sync_selection_shape()
        # The empty state is a *kind* like any other, and the gating pass is what
        # says so: nothing open reads as pixels
        # (:meth:`~...capability_sync.CapabilitySyncMixin._content_kind`), so the
        # bars that configure the next open stay and the tilemap ones go. Without
        # this the pass only ever ran from the render, which needs a document -
        # so the cell format picker sat on the bar before anything was open, and
        # stayed there after the last entry was closed.
        #
        # Last here for the reason it is last in the refresh: every pass above
        # arms controls on grounds that hold in general, and this is the veto.
        self._sync_capabilities()
        self._announce_ready()

    def _show_unavailable(self, entry: Entry) -> None:
        """Show a missing-file entry as the current selection, but inert.

        Like :meth:`_show_empty` (blank canvas, no live document) except
        ``current`` stays on the entry with its name in the title and the
        document UI greyed out: the file it references is gone, so there is
        nothing to drive until it is relocated (File ▸ Locate missing files).
        """
        self._clear_document_view()
        self._set_document_ui_enabled(False)
        self._refresh_tile_source()
        self._refresh_window_title()
        self._sync_nav()
        # The third state the gating pass has to run from, and the one where it
        # is least obvious: no render happens here, and unlike the empty state
        # this one *has* an entry, so its kind is the answer. Without it the bars
        # kept whatever the last document shown needed — a missing tilemap wore
        # the pixel format picker and the position bar, a missing pixel file wore
        # the cell format and the Edit Tiles mode. The greyed toolbars hid most of
        # that; the two View/Palette menu toggles this pass owns outright
        # (_GATE_OWNS) are on no toolbar and said the wrong thing outright.
        self._sync_capabilities()
        self.statusBar().showMessage(
            f"{entry.name}: file not found - use File ▸ Locate missing files."
        )
