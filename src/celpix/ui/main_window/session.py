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
    KEY_TILE_PALETTE_ROWS,
    KEY_TILEMAP_CELL_TILES,
    KEY_TILEMAP_COLUMNS,
    PipelineContext,
)
from celpix.core.document import Document
from celpix.core.errors import PipelineError
from celpix.core.paletteregions import PaletteRegion, PaletteRegions
from celpix.core.tilemap import Cell
from celpix.pipeline import pipeline
from celpix.pipeline.pathway import PathwayConfig
from celpix.plugins.base import NO_COMPRESSION, FileRef
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
_DEFAULT_TILEMAP_PRESET = "preset.tilemap.snes-bg"
_DEFAULT_PIXEL_PRESET = "preset.pixel.snes-4bpp"

# The index step between a metatile cell's tile rows. Not the cell's width: SNES
# 16x16 BG tiles are N, N+1, N+0x10, N+0x11 because VRAM behaves as a 16-tile-wide
# array (``docs/graphics-formats-reference/snes-hardware-notes.md`` §5). Applied
# only to cells that cover more than one tile, where it is the only rule the
# formats in hand use.
_CELL_ROW_STRIDE = 16


class _BoundTiles(NamedTuple):
    """The art a tilemap entry draws from, ready to become the document's
    pixel half — or an empty stand-in when nothing is bound."""

    data: bytes
    bytes_per_tile: int
    tile_width: int
    tile_height: int
    ctx: PipelineContext
    config: PathwayConfig


def _resolve_stamp(cell: Cell, panel: list[Cell]) -> Cell:
    """The panel cell a stamp-layout entry names, or a blank when it names none.

    The entry's ``index`` is already the panel cell index — a panel is 32 wide
    and the two coordinate fields are adjacent, so the low bits read as one
    number *are* ``panelY * 32 + panelX``
    (``docs/graphics-formats-reference/scgcad-formats.md`` §4). What comes back
    is the panel's own cell whole: its tile, its palette row, its flips.

    The entry's ``flags`` — the attribute-source bit among them — are not applied.
    Whether a cleared bit means "use a per-bank default attribute instead of the
    panel's" is the part of the format still unconfirmed, and inventing a default
    would draw a picture the file does not describe. Passing the panel's own
    attributes through is the documented behaviour of the *set* bit, and the one
    that can be checked against the panel on screen.
    """
    return panel[cell.index] if 0 <= cell.index < len(panel) else Cell()


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
        self._refresh_view()

    def _load_entry(self, entry: Entry, *, quiet: bool = False) -> bool:
        """Load ``entry``'s document through the pipeline; False on failure.

        Runs on first activation and again whenever the cached document was
        invalidated by a save into the same file. A failure is normally reported
        with a modal; ``quiet`` suppresses it so a bulk caller (export over many
        entries) can collect and summarize failures itself instead of stacking
        one dialog per bad entry."""
        if entry.session is None:
            entry.session = self._seed_session(entry)
        session = entry.session
        if entry.content_kind is ContentKind.TILEMAP:
            return self._load_tilemap_entry(entry, quiet=quiet)
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
        )
        self._apply_restored_state(entry)
        # After the restore: a project that stored regions of its own has just
        # put them back, and the file's are only a starting point.
        if entry.doc.view.palette_regions.is_empty():
            self._seed_tile_palette_rows(entry, px.ctx.get(KEY_TILE_PALETTE_ROWS, b""))
        return True

    def _load_tilemap_entry(self, entry: Entry, *, quiet: bool = False) -> bool:
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
        """
        session = entry.session
        assert session is not None
        preset_id = entry.tilemap_preset_id or _DEFAULT_TILEMAP_PRESET
        cfg = self._tilemap_config(entry, preset_id)
        try:
            loaded = pipeline.load_tilemap_data(cfg, self._registry)
        except PipelineError as exc:
            if not quiet:
                self._report(exc)
            return False
        panel = self._bound_panel(entry)
        if panel is not None:
            # A stamp layout indexes a *panel*, not a tile bank: its cells are
            # resolved through the panel's, and the tiles come from whatever the
            # panel is itself bound to. Two hops, and the second one is an
            # ordinary tilemap's own binding.
            panel_cells = panel.cells or []
            resolved = [_resolve_stamp(c, panel_cells) for c in loaded.cells]
            entry.doc = Document(
                pixel_data=panel.pixel_data,
                bytes_per_tile=panel.bytes_per_tile,
                tile_width=panel.tile_width,
                tile_height=panel.tile_height,
                palette=self._fallback_palette(),
                pixel_config=replace(panel.pixel_config, write_enabled=False),
                palette_config=self._placeholder_palette_config(
                    session.palette_preset_id
                ),
                pixel_ctx=panel.pixel_ctx,
                cells=loaded.cells,
                resolved_cells=resolved,
                # View-only until restamping is designed: writing one would have
                # to decide what an edit meant (`Document.is_indirect`).
                tilemap_config=replace(cfg, write_enabled=False),
                tilemap_ctx=loaded.ctx,
                tilemap_data=loaded.data,
                cell_tiles=panel.cell_tiles,
                cell_row_stride=panel.cell_row_stride,
                tile_base_index=panel.tile_base_index,
            )
            self._apply_restored_state(entry)
            self._apply_tilemap_columns(entry)
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
            # View-only where the cells are sprite parts, for the same reason a
            # stamp layout is: what a canvas gesture would edit is not settled
            # (``Document.cells_editable``).
            tilemap_config=(
                replace(cfg, write_enabled=False) if loaded.frames else cfg
            ),
            tilemap_ctx=loaded.ctx,
            tilemap_data=loaded.data,
            cell_tiles=cell_tiles,
            cell_row_stride=_CELL_ROW_STRIDE if cell_tiles != (1, 1) else 0,
            tile_base_index=(
                entry.tile_source.base_index if entry.tile_source is not None else 0
            ),
            sprite_frames=loaded.frames,
            sprite_size_pair=loaded.size_pair,
        )
        self._apply_restored_state(entry)
        self._apply_tilemap_columns(entry)
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

        Runs of equal rows collapse into one region each, because that is what a
        region *is*; a bank of 1024 tiles usually resolves to a few dozen.
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
                if row:  # row 0 is the default; pinning it would say nothing
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
        reach = high + (down - 1) * _CELL_ROW_STRIDE + (across - 1)
        if low and reach >= count and reach - low < count:
            entry.tile_source = replace(source, base_index=-low)

    def _tilemap_columns_hint(self, entry: Entry) -> int:
        """The width the entry's format states, or 0 when it states none.

        Read back off the loaded document's context rather than re-read, since
        only the container knows and it has already said.
        """
        doc = entry.doc
        if doc is None or not doc.is_tilemap:
            return 0
        return int(doc.tilemap_ctx.get(KEY_TILEMAP_COLUMNS, 0) or 0)

    def _tilemap_is_indirect(self, entry: Entry) -> bool:
        """Whether ``entry``'s cells are coordinates into another map.

        A property of the **format**, declared by its preset, not of whether a
        binding happens to be resolved yet: a stamp layout with nothing bound is
        still a stamp layout, and the controls have to offer it panels rather
        than tile banks before it can draw anything at all.
        """
        preset_id = entry.tilemap_preset_id or _DEFAULT_TILEMAP_PRESET
        try:
            return bool(self._registry.preset(preset_id).params.get("indirect"))
        except KeyError:
            return False

    def _bound_panel(self, entry: Entry) -> Document | None:
        """The tilemap ``entry`` is bound to, loaded — or None if it isn't.

        Only a stamp layout binds to another tilemap. Loading the panel is the
        ordinary entry load, so the panel resolves its *own* binding first and
        this stays two hops rather than a recursion: a panel bound to a panel is
        refused below, and nothing else in the family chains further.
        """
        source = entry.tile_source
        entries = self._workspace.entries
        if not self._tilemap_is_indirect(entry):
            return None
        if source is None or source.mode is not TileMode.ENTRY:
            return None
        index = source.entry_index
        if index is None or not 0 <= index < len(entries):
            return None
        panel = entries[index]
        if panel is entry or panel.content_kind is not ContentKind.TILEMAP:
            return None
        if panel.doc is None and not self._load_entry(panel, quiet=True):
            return None
        doc = panel.doc
        # A panel that is itself indirect would mean a layout of layouts, which
        # nothing in the family produces and which has no defined resolution.
        if doc is None or not doc.is_tilemap or doc.is_indirect:
            return None
        return doc

    def _load_bound_tiles(self, entry: Entry, *, quiet: bool = False) -> _BoundTiles:
        """The tiles a tilemap entry draws from, or an empty stand-in.

        A binding that cannot be read degrades to no tiles rather than failing
        the entry, on the same rule a missing palette follows: the map is still
        worth showing, and the binding is the part the user can re-point.
        """
        source = entry.tile_source
        if source is None or not source.is_bound:
            return self._no_tiles()
        try:
            cfg = self._tile_source_config(entry, source)
            px = pipeline.load_pixel_data(cfg, self._registry)
        except (PipelineError, KeyError) as exc:
            if not quiet:
                self._report_tile_binding(entry, exc)
            return self._no_tiles()
        return _BoundTiles(
            px.data, px.bytes_per_tile, px.tile_width, px.tile_height, px.ctx, cfg
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
        index = source.entry_index
        entries = self._workspace.entries
        if index is None or not 0 <= index < len(entries):
            raise KeyError(f"no open entry at index {index}")
        bound = entries[index]
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
            selection_cells=self._rect_size,
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
        # Pinned palette regions belong to the entry for the same reason. Stored
        # unbounded — _active_palette_regions clips at render time against the
        # picture and the palette that are actually loaded, so a region survives a
        # codec switch that temporarily puts it out of range.
        self._palette_regions = view.palette_regions
        self._show_palette_regions = view.show_palette_regions
        with signals_blocked(self._show_palette_regions_action):
            self._show_palette_regions_action.setChecked(view.show_palette_regions)
        self._selected_tile = session.selected_tile
        self._selected_last = (
            session.selected_last
            if session.selected_last is not None
            else session.selected_tile
        )
        # A stored rectangle is re-resolved against the view that was restored
        # with it, so it comes back covering the same cells it was drawn over.
        self._rect_size, self._rect_tiles = None, ()
        if session.selected_tile is not None and session.selection_cells is not None:
            tiles = self._rect_tiles_for(
                session.selected_tile - self._offset, *session.selection_cells
            )
            if tiles:
                self._rect_size, self._rect_tiles = session.selection_cells, tiles
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
        """Grey out (or restore) the document-editing surfaces as a block.

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
        self._refresh_window_title()
        self._sync_nav()
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
        self._refresh_window_title()
        self._sync_nav()
        self.statusBar().showMessage(
            f"{entry.name}: file not found - use File ▸ Locate missing files."
        )
