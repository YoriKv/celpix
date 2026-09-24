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

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from PySide6.QtGui import QImage

from celpix.core.arrangement import BlockLayout
from celpix.core.aspect import parse as parse_aspect
from celpix.core.capabilities import ContentKind
from celpix.core.context import (
    KEY_PIXEL_ASPECT,
    KEY_TILE_PALETTE_ROWS,
)
from celpix.core.document import Document
from celpix.core.errors import PipelineError, fault_report
from celpix.core.notices import warn
from celpix.core.tilemap import Cell
from celpix.pipeline import pipeline
from celpix.pipeline.pathway import PathwayConfig
from celpix.plugins.base import NO_COMPRESSION
from celpix.project import documents
from celpix.project.documents import BoundTiles
from celpix.project.workspace import (
    Entry,
    EntryKind,
    EntrySession,
    LoadFailure,
    PaletteMode,
    TileSource,
    backfill_slice_length,
    composite_config,
    composite_layout,
    composite_preset_id,
    load_failed,
    record_composite_layout,
    tilemap_config_for,
    unavailable,
)
from celpix.ui.tools import EditMode
from celpix.ui.widgets import counted, select_combo_data, signals_blocked, wrap_lines


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
        if entry.kind is EntryKind.BOOKMARK:
            return  # no view of its own - selecting one in the list is inert
        # A drag under the pointer belongs to the entry being left: it is
        # abandoned rather than carried onto the next one.
        self._abort_live_gestures()
        # Pixels floating over the entry being left belong to it, so they come
        # down before the view moves on rather than hovering over a stranger.
        self._commit_float()
        # So does anything still being typed in a tool window: a text draft and
        # an alphabet cell left open are edits to *this* entry, and landing them
        # after the switch would put them on the next one.
        self._text.commit_draft()
        self._font_alphabet.commit_edit()
        # And its region settles on the way out, after that landing: leaving an
        # entry is where editing it stops, so the fold it owes is paid at a
        # moment nothing is waiting on rather than carried into whatever the user
        # does next (``docs/design/slices-and-parents.md`` §2). Every reader
        # settles for itself regardless — this only decides *when* the cost lands,
        # and a region owing nothing costs a set test.
        self._settle_region(self._workspace.current)
        fresh = entry.doc is None
        if unavailable(entry) or (fresh and not self._load_entry(entry)):
            # The file moved, or the entry would not open - now, or the last time
            # it was tried. Either way it becomes current, inert: selectable, so
            # it can be edited or closed, with the document UI greyed
            # (:meth:`_show_unavailable`). An entry already marked is not tried
            # again - the row's tooltip says why it failed, and a dialog per
            # click said the same thing; relocation happens through Locate
            # missing files and a retry through changing what the entry reads.
            self._capture_session()
            self._workspace.set_current(entry)  # -> _show_unavailable
            return
        self._capture_session()
        self._workspace.set_current(entry)  # -> _on_current_entry_changed
        # Arm arrow-key navigation on the fresh view - but not when the list is
        # itself being browsed with the arrow keys, or focus would be yanked
        # away from the very keys the user is navigating with.
        if not self._files_panel.is_key_navigating():
            self._canvas.setFocus()
        if fresh:
            self.statusBar().showMessage(self._loaded_message(entry))

    def _loaded_message(self, entry: Entry) -> str:
        """What the status bar says about an entry just read in.

        A palette file counts colors: its tile count is its swatches, and an
        undecodable one is zero swatches, which as "Loaded 0 tiles" would read
        as a success on an empty file rather than a format to correct.
        """
        doc = entry.doc
        if entry.kind is EntryKind.PALETTE:
            if self._palette_error(doc) is not None:
                return (
                    f"Could not decode {entry.name} - pick its format in the "
                    "palette dock's Import as… dropdown"
                )
            return f"Loaded {counted(len(doc.palette), 'color')} from {entry.name}"
        message = f"Loaded {doc.tile_count} tiles from {entry.name}"
        note = self._partial_tile_note()
        return f"{message} - {note}" if note else message

    def _on_current_entry_changed(self, entry: Entry | None) -> None:
        self._files_panel.set_current(entry)
        # Every way the view can move ends here, which is why the Back/Forward
        # trail is recorded here rather than at the activation call sites.
        self._record_visit(entry)
        if entry is None:
            self._show_empty()
            return
        # Already loaded on the _activate_entry path; a close() repointing
        # current to a never-activated (or invalidated) neighbour lands here.
        if unavailable(entry) or (entry.doc is None and not self._load_entry(entry)):
            self._show_unavailable(entry)
            return
        # Here as well as after a load, because a quiet load (a bulk caller, a
        # bound source) reports nothing and this is where its entry is first seen.
        self._warn_entry_faults(entry)
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

        **Every** way a load can fail ends in :meth:`_fail_load`: the entry is
        marked with why, and stays inert until something about what it reads
        changes. This and :meth:`~...palette_source.PaletteSourceMixin.
        _load_palette_entry` are the two funnels (a palette file's is reached
        directly as well), both wrapped by :meth:`_attempt_load`, so nothing
        that opens an entry has a failure path of its own.

        ``quiet`` suppresses the dialog so a bulk caller (export over many
        entries) can collect and summarize failures itself instead of stacking
        one dialog per bad entry.

        ``live`` carries a **tilemap**'s unsaved cell bytes into the read that is
        about to replace them, and means nothing on the pixel path — a pixel
        entry is never re-read out from under its edits, and a bound map's tiles
        come from the entry that owns them (:meth:`_live_bound_tiles`)."""
        if entry.kind is EntryKind.PALETTE:
            # A palette file's document is its colours and its swatches at once,
            # built by the one loader every route to it shares
            # (``docs/design/palette-editing.md`` §2).
            return self._load_palette_entry(entry, quiet=quiet)
        return self._attempt_load(
            entry, lambda: self._read_entry(entry, quiet=quiet, live=live)
        )

    def _attempt_load(self, entry: Entry, read: Callable[[], bool]) -> bool:
        """One attempt to open ``entry`` through ``read``, bracketed.

        Going in, the last failure no longer describes the entry. Coming out
        open, the failure the user was told about is forgotten too: an entry
        that worked and then fails is news, however familiar the message
        (:attr:`~celpix.project.workspace.Entry.reported_failure`).
        """
        entry.load_failure = None
        if not read():
            return False
        entry.reported_failure = None
        return True

    def _read_entry(
        self, entry: Entry, *, quiet: bool = False, live: bytes | None = None
    ) -> bool:
        """The body of :meth:`_load_entry` for a pixel or tilemap entry.

        Runs on first activation and again whenever the cached document was
        invalidated by a save into the same file.
        """
        if entry.session is None:
            entry.session = self._seed_session(entry)
        session = entry.session
        if entry.content_kind is ContentKind.TILEMAP:
            loaded = self._load_tilemap_entry(entry, quiet=quiet, live=live)
            if loaded and not quiet:
                self._warn_entry_faults(entry)
            return loaded
        layout = None
        if entry.kind is EntryKind.COMPOSITE:
            self._open_composite_files(entry)
            # The format is the entry's own, like any other pixel entry's — a
            # composite is read at whatever depth its consumer wants, which is
            # not always a depth its sources use (``docs/design/composite-entry.md``).
            # Assembled here rather than inside ``_pixel_config`` so the one
            # assembly serves both the bytes and the complaints below; joining
            # the sources twice would re-read every one of them.
            layout = self._composite_layout(entry, session.pixel_preset_id)
            cfg = composite_config(
                entry,
                self._registry,
                self._workspace,
                layout=layout,
                preset_id=session.pixel_preset_id,
            )
        else:
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
        except (PipelineError, OSError) as exc:
            return self._fail_load(entry, exc, quiet=quiet)
        if layout is not None:
            # A composite that could not read a source still opens — the run goes
            # blank and everything after it stays put, which is the only outcome
            # a map bound to the composite can survive — so this is a **notice**
            # rather than a failure, landing where everything else a stage had to
            # assume lands (``docs/design/overview.md`` §5). Without it the
            # composite comes up with a hole and nothing saying which piece left it.
            for problem in layout.problems:
                warn(px.ctx, "Composite piece unavailable", problem, "composite")
        if backfill_slice_length(entry, px.ctx):
            # The decompressor discovered the slice's true extent: rebuild the
            # config bounded by it, so save-back is slot-enforced from now on.
            cfg = self._pixel_config(entry, session.pixel_preset_id)
            self._files_panel.refresh_entry(entry)
        px, cfg = self._apply_pixel_preset_hint(entry, px, cfg)
        entry.doc = documents.pixel_document(entry, px, cfg)
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
            loaded = pipeline.load_tilemap_data(
                cfg, self._registry, live, size_pair=entry.sprite_size_pair
            )
        except (PipelineError, OSError) as exc:
            return self._fail_load(entry, exc, quiet=quiet)
        if backfill_slice_length(entry, loaded.ctx):
            # The pixel path's rule, for the same reason: a map sliced without a
            # length is bounded by the extent its decompressor found, or a write
            # that repacks larger runs on into whatever follows it in the file.
            cfg = self._tilemap_config(entry, self._tilemap_preset_id(entry))
            self._files_panel.refresh_entry(entry)
        through = self._bound_tilemap(entry)
        if through is not None:
            # Bound to another tilemap rather than to art: these cells index that
            # map's *cells*, and the tiles come from whatever it is itself bound
            # to. Two hops, and the second is an ordinary binding — which is as
            # far as it goes (:meth:`_bound_tilemap`).
            entry.doc = documents.chained_document(
                self._registry, entry, loaded, cfg, through
            )
            self._apply_restored_state(entry)
            self._apply_tilemap_columns(entry, restored=restored)
            doc = entry.doc
            if (
                not quiet
                and doc.chain is not None
                and doc.chain.stamp != (1, 1)
                and doc.stamp_cells == (1, 1)
            ):
                # A stamp was stated and the resolution cannot lay it out, so
                # the map degrades to one cell per entry everywhere at once
                # (:attr:`~celpix.core.document.Document.stamp_cells`). Said
                # here rather than nowhere: the picture that comes up is not
                # the stamped one, and silence would leave that a puzzle.
                across, down = doc.chain.stamp
                why = (
                    "the source's cells are metatiles"
                    if doc.cell_tiles != (1, 1)
                    else "this format states no width to resolve them at"
                )
                self.statusBar().showMessage(
                    f"{across}x{down} stamps not resolved - {why}; "
                    "drawing one cell per entry."
                )
            return True
        tiles = self._load_bound_tiles(entry)
        entry.doc = documents.tilemap_document(
            self._registry, self._workspace, entry, loaded, cfg, tiles
        )
        self._apply_restored_state(entry)
        self._apply_tilemap_columns(entry, restored=restored)
        return True

    def _fail_load(self, entry: Entry, exc: Exception, *, quiet: bool) -> bool:
        """Record that ``entry`` would not open, say so if it is news, answer False.

        The one exit for a failed load, whatever failed: a stage that raised
        (:class:`~celpix.core.errors.PipelineError`, with the traceback kept for
        the dialog's details pane) or a file that could not be read (an
        ``OSError`` — gone between the missing-file check and the read, or
        unreadable where it stands). The mark is what the list's row shows and
        what keeps the next activation from trying again.

        The dialog is raised for a failure the user has not been told about:
        the first, and any later one that reads differently. A retry that fails
        the same way — the file re-read after a change that did not fix it — is
        silent, because the tooltip already says exactly this and a dialog
        repeating it is the thing this replaces. ``quiet`` leaves even news to a
        caller with its own way of summarising, and does not count as telling:
        the mark is set regardless, since the entry did fail.
        """
        if isinstance(exc, PipelineError):
            failure = LoadFailure.from_error(exc)
        else:
            # OSError's own text already names the path; the errno's phrase is
            # the part worth having.
            why = (exc.strerror if isinstance(exc, OSError) else None) or str(exc)
            failure = LoadFailure(f"Cannot read {entry.path}: {why}")
        entry.load_failure = failure
        self._files_panel.refresh_entry(entry)
        if quiet or failure.summary == entry.reported_failure:
            return False
        entry.reported_failure = failure.summary
        if isinstance(exc, PipelineError):
            self._report(exc)
        else:
            self._alert(failure.summary, title="celPix - open")
        return False

    def _restore_document(self, entry: Entry, previous: Document | None) -> None:
        """Put ``previous`` back on ``entry`` after a re-read of it failed.

        The re-read went through :meth:`_load_entry`, which marked the entry and
        painted its row red; an entry holding a document is not one that failed
        to open, so both are undone here. The failure was still reported (or
        tallied by the caller), and the entry is exactly as it was - which is the
        contract every "drop, re-read, restore on failure" site offers.
        """
        entry.doc = previous
        if previous is not None:
            entry.load_failure = None
            self._files_panel.refresh_entry(entry)

    def _apply_pixel_preset_hint(self, entry: Entry, px, cfg):  # noqa: ANN001
        """:func:`~celpix.project.documents.apply_pixel_preset_hint`, settling."""
        return documents.apply_pixel_preset_hint(
            entry, px, cfg, self._registry, self._pixel_config
        )

    def _seed_tile_palette_rows(self, entry: Entry, table: bytes) -> None:
        """:func:`~celpix.project.documents.seed_tile_palette_rows`, on its document."""
        if entry.doc is not None:
            documents.seed_tile_palette_rows(entry.doc, table)

    def _fit_tile_base(  # noqa: ANN001
        self,
        entry: Entry,
        cells: list[Cell],
        tiles,
        cell_tiles: tuple[int, int],
        named: frozenset[int] = frozenset(),
    ) -> None:
        """:func:`~celpix.project.documents.fit_tile_base`."""
        documents.fit_tile_base(entry, cells, tiles, cell_tiles, named)

    def _row_base_for(
        self,
        entry: Entry,
        declared: int,
        *,
        stated: bool = True,
        bank: int | None = None,
    ) -> int:
        """:func:`~celpix.project.documents.row_base_for`."""
        return documents.row_base_for(entry, declared, stated=stated, bank=bank)

    def _tilemap_preset_id(self, entry: Entry) -> str:
        """:func:`~celpix.project.documents.tilemap_preset_id`."""
        return documents.tilemap_preset_id(entry)

    def _tilemap_declares(self, entry: Entry, name: str) -> object:
        """:func:`~celpix.project.documents.tilemap_declares`."""
        return documents.tilemap_declares(self._registry, entry, name)

    def _preset_declares(self, preset_id: str, name: str) -> object:
        """:func:`~celpix.project.documents.preset_declares` — of a format rather
        than an entry, which the cell-format picker needs while a switch is being
        weighed and the entry still holds the old one."""
        return documents.preset_declares(self._registry, preset_id, name)

    def _tilemap_is_fontmap(self, entry: Entry) -> bool:
        """:func:`~celpix.project.documents.is_fontmap`."""
        return documents.is_fontmap(self._registry, entry)

    def _glyph_layout_for(self, entry: Entry) -> BlockLayout | None:
        """:func:`~celpix.project.documents.glyph_layout_for`."""
        return documents.glyph_layout_for(entry)

    def _resync_glyph_layouts(self, font: Entry) -> None:
        """Carry ``font``'s arrangement onto every open fontmap drawn through it.

        The Pattern and Cols on a font sheet say how big a glyph is
        (:meth:`_glyph_layout_for`), so moving either is a change to what every
        string bound to that sheet draws — not only to the sheet on screen. It is
        the same audience an alphabet edit has
        (:meth:`~...font_alphabet.FontAlphabetMixin._apply_font_alphabet`) and
        the same reason: the fact is the font's, and a second string reading the
        old one is just as wrong as the first.

        Nothing is reloaded. The glyph layout decides which *tiles* a code draws
        and no byte moves when it changes, so the bound documents are amended in
        place and their next repaint composes the new picture. A no-op — a scan
        of the open entries — on every sheet that is not a font.
        """
        if not font.is_font_sheet:
            return
        for other in self._entries_bound_to(font):
            doc = other.doc
            if doc is None or not doc.is_fontmap:
                continue
            if doc.glyph_layout is None and doc.cell_tiles != (1, 1):
                # A cell format that states its own metatile keeps it. The font's
                # grouping only ever fills in where the format said nothing (see
                # the load path), so it cannot take one away here either.
                continue
            layout = self._glyph_layout_for(other)
            doc.glyph_layout = layout
            doc.cell_tiles = (
                (1, 1) if layout is None else (layout.block_columns, layout.block_rows)
            )
            doc.layout_cache = None

    def _tilemap_flag_break(self, entry: Entry) -> bool:
        """:func:`~celpix.project.documents.flag_break`."""
        return documents.flag_break(self._registry, entry)

    def _font_alphabet_for(self, entry: Entry, cell_bytes: int):  # noqa: ANN201
        """:func:`~celpix.project.documents.font_alphabet_for`."""
        return documents.font_alphabet_for(
            self._registry, self._workspace, entry, cell_bytes
        )

    def _tilemap_is_sprite(self, entry: Entry) -> bool:
        """:func:`~celpix.project.documents.is_sprite`."""
        return documents.is_sprite(self._registry, entry)

    def _tilemap_states_subsprite_size(self, entry: Entry) -> bool:
        """:func:`~celpix.project.documents.states_subsprite_size`."""
        return documents.states_subsprite_size(self._registry, entry)

    def _tilemap_columns_hint(self, entry: Entry) -> int:
        """The width the entry's format states, or 0 when it states none.

        Read back off the loaded document's context rather than re-read, since
        only the container knows and it has already said.
        """
        doc = entry.doc
        if doc is None or not doc.is_tilemap:
            return 0
        return doc.stated_columns

    def _chain_stamp_cells(self, entry: Entry, through: Document) -> tuple[int, int]:
        """:func:`~celpix.project.documents.chain_stamp_cells`."""
        return documents.chain_stamp_cells(self._registry, entry, through)

    @staticmethod
    def _chain_source_columns(through: Document) -> int:
        """:func:`~celpix.project.documents.chain_source_columns`."""
        return documents.chain_source_columns(through)

    @staticmethod
    def _chain_column_major(through: Document) -> bool:
        """:func:`~celpix.project.documents.chain_stamp_column_major`."""
        return documents.chain_stamp_column_major(through)

    def _declared_cell_row_stride(self, entry: Entry) -> int:
        """:func:`~celpix.project.documents.declared_cell_row_stride`."""
        return documents.declared_cell_row_stride(self._registry, entry)

    def _tilemap_is_indirect(self, entry: Entry) -> bool:
        """:func:`~celpix.project.documents.is_indirect`."""
        return documents.is_indirect(self._registry, entry)

    def _tilemap_is_dense(self, entry: Entry) -> bool:
        """:func:`~celpix.project.documents.is_dense`."""
        return documents.is_dense(self._registry, entry)

    def _binding_target(self, source: TileSource) -> Entry | None:
        """:func:`~celpix.project.documents.binding_target` — the one place a
        binding becomes a usable entry, for every reader of one."""
        return documents.binding_target(self._workspace, source)

    def _draws_through_tilemap(self, entry: Entry) -> bool:
        """:func:`~celpix.project.documents.draws_through_tilemap`."""
        return documents.draws_through_tilemap(self._workspace, entry)

    def _can_supply_tiles(self, entry: Entry, candidate: Entry) -> bool:
        """:func:`~celpix.project.documents.can_supply_tiles` — behind both the
        binding combo and the "From file..." check, so they cannot disagree."""
        return documents.can_supply_tiles(self._workspace, entry, candidate)

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

    def _composites_using(self, owner: Entry) -> list[Entry]:
        """Every open composite with ``owner`` among its pieces, in list order.

        The audience for a change to ``owner``'s bytes on the assembly side, as
        :meth:`_entries_bound_to` is on the binding side: each of them holds a
        *join* of those bytes, which an edit reaches only if something puts it
        there — and since a join cannot be patched in place
        (:meth:`_reassemble_composites`), what it gets is a dropped cache and a
        fresh assembly.

        Identity, not paths: a piece names the entry itself, so a file and a
        slice of it are different pieces even though the bytes overlap.
        """
        return [
            other
            for other in self._workspace.entries
            if other.kind is EntryKind.COMPOSITE
            and any(piece.entry is owner for piece in other.pieces)
        ]

    def _open_composite_files(self, entry: Entry) -> None:
        """Open the files ``entry``'s pieces are cut from, wherever one is closed.

        A composite owns no bytes, so anything asked in **its own** coordinates is
        answered by a file one of its pieces belongs to — which an Offset palette
        is, and it is the only thing about a composite that is
        (``docs/design/palette-editing.md`` §2,
        :func:`~celpix.project.documents.palette_offset_owner`). A piece that is a
        *slice* of a file nobody opened left that question with no answer at all:
        the mode was refused for having "no piece from a file" while the file sat
        right there on disk. So reading a composite brings those files into the
        list, once, rather than every consumer learning to look behind a slice for
        one.

        The row is added the way a jump to a closed parent adds one
        (:meth:`~...entries.EntriesMixin._jump_into_parent`) — a plain FILE on the
        raw container, since a slice's offsets are file-absolute and a detected
        header skip would move the base out from under them — and **not** through
        an undo command: it follows from the composite being read rather than from
        a gesture, and there is no step for an undo to take back.

        Only a slice hides a file. A FILE piece *is* the row, and one that has
        left the list is the ordinary closed-source case the assembly already
        degrades — a second row over the same path would not put the piece back on
        it. A composite can never be a piece
        (:func:`~celpix.project.workspace.can_compose`), so there is no nesting to
        walk. A file no longer on disk is left alone, the way every other missing
        file here is: the composite still assembles, and the run it could not read
        goes blank and says so
        (:func:`~celpix.project.workspace.composite_layout`).
        """
        for piece in entry.pieces:
            source = piece.entry
            if source is None or source.kind is not EntryKind.SLICE:
                continue
            if self._workspace.parent_of(source) is not None:
                continue
            if not Path(source.path).is_file():
                continue
            if source.parent_kind is EntryKind.PALETTE:
                # A slice of a palette file names a registered palette, and it
                # comes back as one — in the format the slice reads its colour
                # words in, which is the file's own format as the palette had
                # it. The dock's Import as… is only a guess for a file nobody
                # has read yet; here the slice already says what the bytes are.
                seen = source.session and source.session.palette_view_preset_id
                self._workspace.add_palette(
                    source.path,
                    seen
                    if seen and self._registry.has_preset(seen)
                    else self._palette_import_preset_id(),
                    self._detect_palette_container(source.path),
                )
            else:
                self._workspace.open_file(source.path, source.extra_paths)

    def _composite_layout(self, entry: Entry, preset_id: str = ""):
        """Assemble ``entry``'s pieces, settling each one's region first.

        The composite's twin of :meth:`~...interpretation.InterpretationMixin.
        _pixel_config`, and settling is why it exists rather than being a bare
        call to :func:`~celpix.project.workspace.composite_layout`: a piece reads
        its own live buffer, and a *file* piece with an edited slice of its own
        owes that slice a fold before its bytes are true
        (``docs/design/slices-and-parents.md`` §2). Assembling without settling
        would join the pre-fold version and put a stale run in the composite.

        The run lengths it measures are recorded on the entry, so every later
        question about where a piece sits is answered from those rather than by
        assembling again (:attr:`~celpix.project.workspace.Entry.piece_spans`).
        """
        for piece in entry.pieces:
            self._settle_region(piece.entry)
        layout = composite_layout(entry, self._registry, self._workspace, preset_id)
        if record_composite_layout(entry, layout):
            self._files_panel.refresh_entry(entry)
        return layout

    def _region_of(self, entry: Entry) -> list[Entry]:
        """``entry`` and every open entry whose bytes are the same file region.

        A slice's bytes live inside its parent's, so the two are one region with
        two names: an edit through either changes what the other shows
        (``docs/design/slices-and-parents.md``). The answer is the entry first,
        then its parent for a slice or its slices for a file — never sibling
        slices, which share a parent but not necessarily any bytes, and never
        anything for a composite, whose bytes are all somebody else's.
        """
        out = [entry]
        if entry.kind is EntryKind.SLICE:
            parent = self._workspace.parent_of(entry)
            if parent is not None:
                out.append(parent)
        elif entry.kind in (EntryKind.FILE, EntryKind.PALETTE):
            out += [
                child
                for child in self._workspace.children_of(entry)
                if child.kind is EntryKind.SLICE
            ]
        return out

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

    def _reassemble_composites(
        self, owners: list[Entry], *, keep: Entry | None = None
    ) -> list[Entry]:
        """Rebuild every composite assembled out of ``owners``; return the ones hit.

        The **one** way a composite's join is invalidated, whatever made it stale:
        a piece arriving or leaving the list, or a piece's bytes being edited.
        Both are the same problem — a composite holds a copy of bytes it joined,
        and a join cannot be patched in place, because an offset in it is not an
        offset in the piece it came from. So it is dropped and assembled again.
        (:meth:`_reresolve_bound_art` is the map-side twin, and *that* one can
        patch, which is why the two stay separate.)

        Each owner is taken **with its region** (:meth:`_region_of`): a slice's
        bytes are its parent's, so an edit to either is an edit to the other, and
        a composite built on the other is exactly as stale as one built on the
        entry the stroke named. Without that, a composite over a file went on
        showing the old bytes after a stroke on a slice of it, and one over the
        slice after a stroke on the file — until something else happened to drop
        it. The reassembly reads each piece through the settle that pays the
        fold a slice edit owes (:meth:`_composite_layout`), which is what makes
        the parent's run true rather than merely fresh.

        ``keep`` is the composite an edit was **made on**, which already holds the
        bytes and whose buffer is the one the stroke landed in. It is the only
        exemption — a second composite over the same source is stale whether or
        not it happens to be on screen.

        The rebuild is a drop, not a re-read: a composite has no unsaved state of
        its own to carry across one — every edit made on it was deposited into its
        pieces as it landed (``docs/design/composite-entry.md``) — so the next
        activation reads it fresh, and the one on screen is reloaded here because
        nothing else is going to ask. Quiet, because the gesture that reached here
        was about some other entry: a composite whose pieces have gone missing
        must not put a modal in front of the removal that caused it.

        The list comes back because the maps drawing through these composites
        need the same treatment one step later, and the caller is what knows to
        ask.
        """
        seen: list[Entry] = []
        for stale in [e for owner in owners for e in self._region_of(owner)]:
            for composite in self._composites_using(stale):
                # One composite can hold several of these owners — a file and a
                # slice of it, both pieces of the same composite — and rebuilding
                # it once per owner would reload it once per piece.
                if composite is keep or any(composite is other for other in seen):
                    continue
                seen.append(composite)
                self._rebuild_composite(composite)
        return seen

    def _rebuild_composite(self, composite: Entry, pixel_preset_id: str = "") -> None:
        """Drop ``composite``'s join and, when it is on screen, assemble it again.

        The one step both invalidations end in: a piece's bytes or presence
        changing under the composite (:meth:`_reassemble_composites`), and the
        composite's own list being re-written
        (:meth:`~...entries.EntriesMixin._apply_composite_params`). The second
        used to ask the first, which looks for composites *using* the entry and
        so never found the entry itself — a composite is never a piece — and an
        edit to the list changed the name in the Files pane and nothing on
        screen.

        Off screen the drop is the whole of it: the next activation reads the
        entry fresh. On screen it is reloaded here, quietly, because nothing
        else is going to ask — and its session is captured first, so the offset
        and format the widgets hold ride across the reload with the view the
        drop stashes (:meth:`~celpix.project.workspace.Workspace.drop_document`).
        A reload that fails keeps the document it had, so the entry and the
        window never disagree about what is on screen.

        ``pixel_preset_id``, when given, is the format the entry is re-read at —
        the composite dialog's Pixel / Palette choice. It is written over the
        *captured* session, since capturing reads the pixel picker and would
        otherwise put the old format straight back. A composite never opened
        gets a session only where the format differs from the seed it would
        start on anyway, so an unchanged one keeps following its first source.
        """
        current = composite is self._workspace.current
        if current:
            self._capture_session()
        if pixel_preset_id:
            if composite.session is not None:
                composite.session.pixel_preset_id = pixel_preset_id
            elif pixel_preset_id != composite_preset_id(composite, self._registry):
                composite.session = replace(
                    self._seed_session(composite), pixel_preset_id=pixel_preset_id
                )
        previous = composite.doc
        self._workspace.drop_document(composite)
        if not current:
            return
        if self._load_entry(composite, quiet=True):
            self._doc = composite.doc
            self._restore_session(composite)
        else:
            self._restore_document(composite, previous)
        self._refresh_view()

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

        The **geometry** is re-read along with the cells: the stamp size and the
        source's width are the source's answers, and a source whose stated shape
        moved (a codec or preset switch re-reading its header) would otherwise
        leave every dependent stamping at the old one until reloaded.
        """
        through = entry.doc
        cells = through.cells if through is not None else None
        if through is None or cells is None:
            return False
        current = False
        for other in self._workspace.entries:
            doc = other.doc
            source = other.tile_source
            if other is entry or doc is None or doc.chain is None:
                continue
            if source is None or source.entry is not entry:
                continue
            doc.chain = replace(
                doc.chain,
                source=cells,
                stamp=self._chain_stamp_cells(other, through),
                source_columns=self._chain_source_columns(through),
                stamp_column_major=self._chain_column_major(through),
            )
            doc.resolve()
            current = current or other is self._workspace.current
        return current

    def _load_bound_tiles(self, entry: Entry) -> BoundTiles:
        """The tiles a tilemap entry draws from, or an empty stand-in.

        A binding that cannot be read degrades to no tiles rather than failing
        the entry, on the same rule a missing palette follows: the map is still
        worth showing, and the binding is the part the user can re-point. What
        went wrong rides on the stand-in as a **notice**, so the row wears the
        warning mark and its tooltip says which read failed — the same place a
        stage that had to assume something reports, and read the same way. Not
        a dialog: one per load of the map said the same thing every time.
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
            return self._unreadable_tiles(exc)
        live = self._live_bound_tiles(source, cfg)
        if live is not None:
            return live
        try:
            px = pipeline.load_pixel_data(cfg, self._registry)
        except (PipelineError, KeyError, OSError) as exc:
            return self._unreadable_tiles(exc)
        return BoundTiles(
            px.data, px.bytes_per_tile, px.tile_width, px.tile_height, px.ctx, cfg
        )

    def _live_bound_tiles(
        self, source: TileSource, cfg: PathwayConfig
    ) -> BoundTiles | None:
        """:func:`~celpix.project.documents.live_bound_tiles`."""
        return documents.live_bound_tiles(self._workspace, source, cfg)

    def _no_tiles(self) -> BoundTiles:
        """:func:`~celpix.project.documents.no_tiles`, at the combo's format."""
        return documents.no_tiles(self._registry, self._pixel_preset_id())

    def _tilemap_config(self, entry: Entry, preset_id: str) -> PathwayConfig:
        """The pathway that reads ``entry``'s own file as cells.

        The map *is* this file, where the pixel config a tilemap carries points
        somewhere else entirely: the two configs on a tilemap document address
        different files, which is the point.

        Through the workspace, like every other config this window builds, and
        for the reason :func:`~celpix.project.workspace.tilemap_config_for`
        gives — a slice's offset names bytes in its **parent's** buffer, not in
        the file, wherever the parent's container does more than skip a header.
        """
        return tilemap_config_for(entry, preset_id, self._registry, self._workspace)

    def _tile_source_config(self, entry: Entry, source: TileSource) -> PathwayConfig:
        """:func:`~celpix.project.documents.tile_source_config`, through this
        window's :meth:`_pixel_config` so a bound slice's parent settles first."""
        return documents.tile_source_config(
            self._workspace, entry, source, self._pixel_config, self._pixel_preset_id()
        )

    def _unreadable_tiles(self, exc: Exception) -> BoundTiles:
        """The no-tiles stand-in, carrying why the bound tiles could not be read.

        Worded so as not to imply the map failed: its cells are what is on
        screen, and the binding is what to look at. The reason is wrapped here
        because it is a stage's own message, which nothing hard-wrapped for a
        tooltip (``docs/py-qt-reference/pyside6-pitfalls.md``).
        """
        tiles = self._no_tiles()
        fault = exc.fault if isinstance(exc, PipelineError) else None
        if isinstance(exc, KeyError):
            # A KeyError's str() is its key in quotes, which says nothing on a
            # row; what was looked up and not found is a format id.
            why = f"unknown format {exc.args[0]}" if exc.args else "unknown format"
        else:
            why = str(exc)
        warn(
            tiles.ctx,
            "Bound tiles could not be read",
            wrap_lines(
                f"{why}\nRe-point the map's tile source, or fix the entry it names."
            ),
            # Attributed to the plugin that raised where one did, and carrying
            # its traceback: a codec that *crashed* over the bank is a plugin
            # fault, which the fault dialog raises once and its author needs.
            source=(exc.plugin if isinstance(exc, PipelineError) else "") or "host",
            report=fault_report(fault) if fault is not None else "",
        )
        return tiles

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
        self._seed_pixel_aspect(doc)

    def _seed_pixel_aspect(self, doc: Document) -> None:
        """Take a container's stated pixel shape as the project's, once.

        Here because this is where all three load paths meet and where a project's
        own stored answers are consumed — and the aspect is the same kind of
        thing, one hop further out: a hint the file offers, which stands only
        while nothing has been said.

        **Only while the project has never answered.** The setting is one for the
        whole project (:attr:`~celpix.project.workspace.Workspace.pixel_aspect`),
        so a second entry publishing a different ratio must not move it under the
        first — and a user who has chosen must not be overruled by opening a file.
        That makes this a *seed*, the same shape as a tilemap's stated width
        seeding Cols (:meth:`~...rendering.RenderingMixin._apply_tilemap_columns`).

        A ratio that is not one goes in the bin rather than on the screen: a
        container is a plugin, and the one thing a display setting must not do is
        fail to draw.
        """
        if self._workspace.pixel_aspect is not None:
            return
        stated = parse_aspect(doc.pixel_ctx.get(KEY_PIXEL_ASPECT))
        if stated is None:
            return
        self._workspace.pixel_aspect = stated
        self._sync_pixel_aspect()

    def _seed_session(self, entry: Entry) -> EntrySession:
        """A new entry's starting UI state, seeded from the live toolbar so a
        freshly opened file keeps the codec the user is working in. A slice's
        preview combo starts at none - its bytes are already decompressed."""
        return EntrySession(
            # A composite starts on its first source's format rather than on the
            # toolbar's: it is assembled out of entries that already state one,
            # and the toolbar is showing whatever was last on screen. Only a
            # start — the picker is the user's from then on, and has to be,
            # because a tile window is read at the depth its *consumer* wants
            # (``docs/design/composite-entry.md``).
            pixel_preset_id=(
                composite_preset_id(entry, self._registry)
                if entry.kind is EntryKind.COMPOSITE
                else self._pixel_preset_id()
            ),
            palette_preset_id=self._palette_preset_id(),
            preview_compression_id=(
                NO_COMPRESSION
                if entry.kind is EntryKind.SLICE
                else self._compression_id()
            ),
            palette_view_preset_id=self._palette_view_preset_id(),
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
        # The recorded origin, not the drawn one: View > Entire File draws from the
        # file's start without that being where the entry was left.
        entry.doc.view.tile_offset = self._recorded_offset()
        entry.doc.view.byte_nudge = self._nudge
        entry.session = EntrySession(
            pixel_preset_id=self._pixel_preset_id(),
            palette_preset_id=self._palette_preset_id(),
            palette_mode=self._palette_mode,
            preview_compression_id=self._compression_id(),
            palette_view_preset_id=self._palette_view_preset_id(),
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
        # Undo any disabling from a previously shown missing entry.
        self._set_document_ui_enabled(True)
        # The pixel combo goes through the filter, which force-shows the restored
        # format even when hidden (you can't hide the format in force).
        self._fill_pixel_combo(session.pixel_preset_id)
        for combo, data in (
            (self._palette_preset, session.palette_preset_id),
            (self._compression, session.preview_compression_id),
            (self._palette_view_preset, session.palette_view_preset_id),
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
            (self._palette_row, view.palette_row),
            (self._bitmap_width, view.bitmap_width),
        )
        # The grid and the **zoom** are deliberately absent: both are app-wide
        # rather than the entry's, so a switch leaves each exactly where the user
        # set it (``ViewOptions.zoom``, ``main_window/interpretation.py``). The
        # entry's stored zoom is still overwritten by the render that follows this
        # - the view options are one bundle and the window's own value is what
        # goes into it - so nothing here has to clear it.
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
        # A palette file is a whole file too: sliced, bookmarked and framed
        # like any other (``docs/design/palette-editing.md`` §2).
        is_file = entry.kind in (EntryKind.FILE, EntryKind.PALETTE)
        self._place_origin(view.tile_offset, view.byte_nudge)
        # The rearrangement belongs to the entry, like the offset: switching away
        # and back must find the tiles where they were left. Any drag in flight
        # belonged to the entry being left, so it goes with it. Stored unbounded,
        # like the pinned regions below: a codec with bigger tiles leaves fewer of
        # them, and bounding here would drop the rest of the map with no step to
        # bring it back - :meth:`_active_tile_rearrangement` bounds on read.
        self._cancel_rearrange_drag()
        self._tile_rearrangement = view.tile_rearrangement
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
        self._sync_entry_scope()  # a veto that runs after every owner

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
        # After _doc is cleared: no render runs on the way out, so the badges
        # would otherwise go on wearing the last entry's answer.
        self._sync_inputs_badges()
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
        self._reload_action.setEnabled(False)
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
        """Show an entry that cannot open as the current selection, but inert.

        Like :meth:`_show_empty` (blank canvas, no live document) except
        ``current`` stays on the entry with its name in the title and the
        document UI greyed out. Two things land here, told apart by the status
        line: the file it references is gone, so there is nothing to drive until
        it is relocated (File ▸ Locate missing files); or its load failed
        (:attr:`~celpix.project.workspace.Entry.load_failure`), and the row's
        error mark carries the why until something about the entry changes.
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
        failure = load_failed(entry)
        if failure is None:
            message = f"{entry.name}: file not found - use File ▸ Locate missing files."
        else:
            # The first line of the failure is the who-and-where; the rest is on
            # the row, where it can be read at leisure.
            why = failure.summary.split("\n", 1)[0]
            message = f"{entry.name} did not open: {why} (see the mark on its row)"
        self.statusBar().showMessage(message)
