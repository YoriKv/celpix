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

from PySide6.QtGui import QImage

from celpix.core.document import Document
from celpix.core.errors import PipelineError
from celpix.pipeline import pipeline
from celpix.plugins.base import NO_COMPRESSION
from celpix.project.workspace import (
    Entry,
    EntryKind,
    EntrySession,
    PaletteMode,
    backfill_slice_length,
    data_missing,
)
from celpix.ui.tools import EditMode
from celpix.ui.widgets import select_combo_data, signals_blocked


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
        return True

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
            compression_id=(
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
            compression_id=self._compression_id(),
            selected_tile=self._selected_tile,
            selected_last=self._selected_last,
            selection_cells=self._rect_cells,
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
            (self._compression, session.compression_id),
            (self._block_order, view.block_order),
        ):
            select_combo_data(combo, data)
        spins = (
            (self._columns, view.columns),
            (self._rows, view.rows),
            (self._zoom, view.zoom),
            (self._subpalette, view.subpalette_row),
            (self._block_cols, view.block_columns),
            (self._block_rows, view.block_rows),
            (self._bitmap_width, view.bitmap_width),
        )
        checks = (
            (self._grid, view.show_grid),
            (self._two_d, view.two_dimensional),
        )
        with signals_blocked(*(w for w, _ in (*spins, *checks))):
            for spin, value in spins:
                spin.setValue(value)
            for check, value in checks:
                check.setChecked(value)
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
        self._tile_map = view.tile_map.bounded(entry.doc.tile_count)
        self._show_rearranged = view.show_rearranged
        self._sync_rearrange_actions()
        self._selected_tile = session.selected_tile
        self._selected_last = (
            session.selected_last
            if session.selected_last is not None
            else session.selected_tile
        )
        # A stored rectangle is re-resolved against the view that was restored
        # with it, so it comes back covering the same cells it was drawn over.
        self._rect_cells, self._rect_tiles = None, ()
        if session.selected_tile is not None and session.selection_cells is not None:
            tiles = self._rect_tiles_for(
                session.selected_tile - self._offset, *session.selection_cells
            )
            if tiles:
                self._rect_cells, self._rect_tiles = session.selection_cells, tiles
                self._selected_last = max(tiles)
        self._sync_selection_actions()
        self._set_palette_mode(session.palette_mode)  # also arms Write
        # Only whole files spawn slices and bookmarks - neither nests (and a
        # file's byte stream is always raw, so its positions map straight to
        # file offsets).
        self._new_slice_action.setEnabled(is_file)
        self._new_slice_from_view_action.setEnabled(is_file)
        self._new_bookmark_action.setEnabled(is_file)
        # A slice has no container of its own — it reads through its parent's
        # coordinates — so this follows the same is_file rule as the rest.
        self._change_container_action.setEnabled(is_file)
        self._refresh_window_title()

    def _clear_document_view(self) -> None:
        """Blank the canvas and disable every document-bound action - shared by
        the nothing-open and missing-file (unavailable) states."""
        self._doc = None
        self._selected_tile = None
        self._selected_last = None
        self._rect_cells, self._rect_tiles = None, ()
        self._canvas.set_selection(None)
        self._sync_selection_actions()
        # Called after _doc is cleared, so it settles on "unavailable": the tool
        # disarms and both its switches grey out. They need saying explicitly
        # because they are shown in the Edit menu as well as on the transform bar,
        # and a menu row does not inherit that bar's disabled state.
        self._sync_rearrange_actions()
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
        self._new_slice_action.setEnabled(False)
        self._new_slice_from_view_action.setEnabled(False)
        self._new_bookmark_action.setEnabled(False)
        self._change_container_action.setEnabled(False)

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
