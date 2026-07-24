"""The open-entries list: projects, slices, bookmarks, and writing back.

Everything that creates, re-points, navigates between or saves the entries in
the files dock. A **slice** is an offset+length region of a parent file that
acts as its own document; a **bookmark** is a position plus a snapshot of the
settings at creation time, with no document of its own. Neither ever nests -
both anchor to a whole file.

The jumps share one body (:meth:`_jump_into_parent`): they differ only in which
snapshot they install on the parent before re-reading it. Writing is per pathway
- a palette-only edit leaves the graphic untouched, since the two live in
different files.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from PySide6.QtWidgets import (
    QFileDialog,
    QMessageBox,
)

from celpix.core.document import Document, ViewOptions
from celpix.core.errors import PipelineError
from celpix.pipeline import pipeline
from celpix.plugins.base import NO_DECOMPRESS
from celpix.project import projectfile
from celpix.project.workspace import (
    Entry,
    EntryKind,
    EntrySession,
    PaletteSource,
    SliceParams,
    missing_paths,
    new_slice,
    palette_source_for,
    relocate_path,
)
from celpix.ui.slice_dialog import SliceDialog
from celpix.ui.undo_commands import (
    AddEntryCommand,
    SliceEditCommand,
)


class EntriesMixin:
    """Projects, slices, bookmarks, and writing entries back to disk.

    A slice of :class:`~celpix.ui.main_window.window.MainWindow`, not a
    standalone object: it reads and writes the window's own widgets and its
    single live ``_doc``. See the module docstring for what it owns, and the
    package docstring for why these are mixins.
    """

    # -- projects ------------------------------------------------------------
    _PROJECT_FILTER = "Celpix project (*.celpix)"

    def _open_project(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open project", "", self._PROJECT_FILTER
        )
        if path:
            self._load_project(path)

    def _load_project(self, path: str) -> None:
        """Replace the workspace with the session saved in ``path``.

        Documents stay lazy - nothing is read until an entry is activated - and
        a per-entry problem (missing file, unknown preset) surfaces on that
        entry's activation, never as a failure of the load itself.
        """
        if not self._confirm_discard_project("Loading another project"):
            return
        if not self._resolve_dirty_entries(
            "Loading a project replaces the current workspace, and the unsaved "
            "changes with it"
        ):
            return
        try:
            loaded = projectfile.load_project(path)
        except projectfile.ProjectError as exc:
            self._alert(str(exc), title="Celpix - project")
            return
        if loaded.version > projectfile.PROJECT_VERSION:
            self._alert(
                "This project was saved by a newer Celpix. It opens with what "
                "this version understands, but saving will rewrite it at "
                f"version {projectfile.PROJECT_VERSION}, dropping the rest.",
                title="Celpix - project",
            )
        # Seed the pixel-format filter before the replace: showing the restored
        # current entry rebuilds the dropdown, which must already read the
        # project's filter. A rebuild also happens explicitly below for a project
        # with no shown entry.
        self._workspace.hidden_pixel_presets = set(loaded.hidden_pixel_presets)
        self._workspace.replace(loaded.entries, loaded.current)
        self._fill_pixel_combo(self._pixel_preset_id())
        # The one entry-lifecycle change that bypasses the undo stack: older
        # commands would reference entries the replace discarded, so the
        # history goes with them.
        self._undo_stack.clear()
        self._project_path = path
        # Baseline *after* the replace has settled: showing the restored entry
        # runs its session through the live widgets, which legitimately clamps
        # (an offset past a shrunken file, a subpalette row past the palette).
        # Snapshotting before that would leave the project reading dirty the
        # instant it opened, for changes the user never made.
        self._saved_project = self._project_snapshot()
        # The replace above titled the window from the restored entry (no project
        # path was set yet); now that one is, retitle to name the project file.
        self._update_window_title()
        self.statusBar().showMessage(
            f"Loaded project {Path(path).name} ({len(loaded.entries)} entries)."
        )
        # Referenced files may have moved since the project was saved - offer to
        # re-point them straight away, and arm the menu for later.
        self._update_locate_action()
        if missing_paths(self._workspace):
            self._relocate_missing(prompt_summary=True)

    def _update_locate_action(self) -> None:
        """Arm File ▸ Locate missing files iff the project has missing files."""
        self._locate_missing_action.setEnabled(bool(missing_paths(self._workspace)))

    def _relocate_missing(self, *, prompt_summary: bool) -> None:
        """Walk the missing referenced files, prompting to re-point each.

        ``prompt_summary`` opens with a one-shot confirmation (the project-load
        entry point); the menu dives straight into the file pickers. Each
        located file corrects every entry that shared the old path - a ROM and
        the slices/bookmarks under it move together - and reloads whatever was
        affected. Skipped files stay missing (still highlighted, still armed).
        """
        paths = missing_paths(self._workspace)
        if not paths:
            self.statusBar().showMessage("No missing files.")
            return
        if prompt_summary:
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Warning)
            box.setWindowTitle("Celpix - missing files")
            box.setText(
                f"This project references {len(paths)} file(s) that couldn't be "
                "found. Locate them now?"
            )
            locate = box.addButton("Locate…", QMessageBox.ButtonRole.AcceptRole)
            box.addButton("Not now", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            if box.clickedButton() is not locate:
                return
        start_dir = str(Path(self._project_path).parent) if self._project_path else ""
        relocated = 0
        for old in paths:
            new, _ = QFileDialog.getOpenFileName(
                self, f"Locate {Path(old).name}", start_dir
            )
            if not new:
                continue  # skipped - leave it missing
            # Reject locating a data file onto one already open: that would leave
            # two file entries editing the same path. (A palette-only relocation
            # - no file entry at `old` - can legitimately point into an open ROM,
            # so it isn't blocked.)
            clash = self._workspace.find_file(new)
            if self._workspace.find_file(old) is not None and clash is not None:
                self._alert(
                    f"{Path(new).name} is already open in this project, so "
                    f"{Path(old).name} can't be relocated to it. Pick a "
                    "different file, or close the duplicate first.",
                    title="Celpix - locate",
                )
                continue
            for entry in relocate_path(self._workspace, old, new):
                self._reload_relocated_entry(entry)
            relocated += 1
        self._update_locate_action()
        # Re-show the current entry: a now-resolvable one loads; one whose picked
        # file was invalid (or still skipped) falls back to the unavailable state.
        self._on_current_entry_changed(self._workspace.current)
        remaining = len(missing_paths(self._workspace))
        self.statusBar().showMessage(
            f"Relocated {relocated} file(s)"
            + (f"; {remaining} still missing." if remaining else ".")
        )

    def _reload_relocated_entry(self, entry: Entry) -> None:
        """Refresh one entry after its path(s) were corrected.

        A loaded entry whose palette became reachable reloads that palette in
        place; a never-loaded (or data-relocated) entry simply reloads on its
        next activation. The list item is refreshed either way so its highlight
        clears.
        """
        if entry.doc is not None and entry.missing_palette is not None:
            self._restore_palette_source(entry, entry.missing_palette)
        self._files_panel.refresh_entry(entry)

    def _save_project(self) -> None:
        if self._project_path is None:
            self._save_project_as()
        else:
            self._save_project_to(self._project_path)

    def _save_project_as(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Save project", self._project_path or "", self._PROJECT_FILTER
        )
        if not path:
            return
        if not path.endswith(projectfile.PROJECT_EXTENSION):
            path += projectfile.PROJECT_EXTENSION
        self._save_project_to(path)

    def _save_project_to(self, path: str) -> None:
        if not self._resolve_dirty_entries(
            "A project stores file references, not bytes, so it can't include "
            "the unsaved changes"
        ):
            return
        self._capture_session()  # the on-screen entry's snapshot must be fresh
        try:
            projectfile.save_project(self._workspace, path)
        except OSError as exc:
            self._alert(f"Cannot write {path}: {exc}", title="Celpix - project")
            return
        self._project_path = path
        self._saved_project = self._project_snapshot()  # the new clean baseline
        # A first Save Project As gives the session a project file - title to it.
        self._update_window_title()
        self.statusBar().showMessage(f"Saved project to {path}.")

    def _confirm_discard_project(self, action: str) -> bool:
        """Unsaved-project gate for load/quit; True when OK to proceed.

        The two kinds of unsaved work are asked about separately because they are
        separate things: writing files to disk does not save the project, and
        saving the project does not write a single edited byte. This one covers
        the session - which files are open, how each is being read, where the
        view sits - and is only raised once a project file exists to save it
        into. A session that has never been saved as a project is not silently
        promised one here; it is discarded on quit as it always was.
        """
        if not self._project_is_dirty():
            return True
        assert self._project_path is not None  # implied by _project_is_dirty
        box = QMessageBox(self)
        box.setWindowTitle("Celpix - unsaved project")
        box.setText(
            f"{action} discards unsaved changes to "
            f"{Path(self._project_path).name}. Save the project first?"
        )
        save = box.addButton("Save Project", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Discard", QMessageBox.ButtonRole.DestructiveRole)
        cancel = box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        if box.clickedButton() is cancel:
            return False
        if box.clickedButton() is save:
            self._save_project()
            # A save that failed (or that its own dirty-files gate cancelled)
            # left the project dirty - don't proceed past it.
            return not self._project_is_dirty()
        return True

    def _resolve_dirty_entries(
        self,
        consequence: str,
        *,
        write_label: str = "Write All",
        skip_label: str = "Continue Without",
        default_write: bool = False,
    ) -> bool:
        """Unsaved-file-changes gate; True when OK to proceed.

        The one prompt for "there are unsaved edits in memory": write them to
        disk first (Accept), go ahead without doing so (Destructive), or cancel
        the whole action. The middle option's meaning - and so its label - is the
        caller's: project save/load *keeps* the edits in memory for later
        ("Continue Without"), while quitting drops them for good ("Discard"), so
        that path also defaults to writing, the least-lossy choice when Enter is
        hit blind. A project can't represent unsaved bytes either way, which is
        why saving/loading one has to resolve them first.
        """
        dirty = self._workspace.dirty_entries()
        if not dirty:
            return True
        names = ", ".join(e.name for e in dirty)
        box = QMessageBox(self)
        box.setWindowTitle("Celpix - unsaved changes")
        box.setText(f"{consequence} ({names}). Write them to disk first?")
        write = box.addButton(write_label, QMessageBox.ButtonRole.AcceptRole)
        box.addButton(skip_label, QMessageBox.ButtonRole.DestructiveRole)
        cancel = box.addButton(QMessageBox.StandardButton.Cancel)
        if default_write:
            box.setDefaultButton(write)
        box.exec()
        if box.clickedButton() is cancel:
            return False
        if box.clickedButton() is write:
            self._write_all()
            # A write that failed left its entry dirty - don't proceed past it.
            return not self._workspace.dirty_entries()
        return True

    # -- writing back --------------------------------------------------------
    def _write_current(self) -> None:
        """File ▸ Write: the current file or slice back to disk."""
        entry = self._workspace.current
        if entry is None or entry.doc is None:
            return
        # Read before the write, which clears the flags it acts on.
        palette_only = entry.palette_dirty and not entry.pixel_dirty
        has_palette_file = entry.doc.palette_config.write_enabled
        if self._write_entry(entry):
            # Report what actually went to disk: a palette-only write leaves the
            # graphic alone, and Default/Custom/Emulator palettes have no file
            # behind them at all (docs/design/palette-editing.md).
            wrote = (
                "palette"
                if palette_only
                else "pixel + palette"
                if has_palette_file
                else "pixel"
            )
            self.statusBar().showMessage(f"Wrote {entry.name} ({wrote}).")

    def _write_all(self) -> None:
        """File ▸ Write All: every entry with unsaved in-memory changes."""
        dirty = self._workspace.dirty_entries()
        written = [e.name for e in dirty if e.doc is not None and self._write_entry(e)]
        if written:
            self.statusBar().showMessage(
                f"Wrote {len(written)} item(s): {', '.join(written)}."
            )

    def _write_entry_checked(self, entry: Entry) -> None:
        """The files dock's context-menu Write - guards, then writes."""
        if entry.doc is None:
            return
        # A PALETTE entry writes its own .pal (its pixel half is inert); every
        # other entry writes its graphic, which is view-only without a compressor.
        writable = (
            entry.doc.palette_config.write_enabled
            if entry.kind is EntryKind.PALETTE
            else entry.doc.pixel_config.write_enabled
        )
        if not writable:
            self._alert(
                f"{entry.name} is view-only (its compression has no compressor), "
                "so it can't be written back.",
                title="Celpix - write",
            )
            return
        if self._write_entry(entry):
            self.statusBar().showMessage(f"Wrote {entry.name}.")

    def _write_entry(self, entry: Entry) -> bool:
        """Save one entry through the pipeline; True on success.

        Writes only the pathway that needs it: when the **palette alone** is
        dirty the graphic is left untouched, since the two live in different
        files and rewriting unchanged pixel bytes is at best a needless mtime
        bump (docs/design/palette-editing.md §2). Any other case - pixel edits,
        or an explicit Write on a clean entry - writes both, as it always did.

        A successful write invalidates the cached documents of other entries on
        the same file (their bytes are now stale) - including the one on screen
        when a slice is written back under its parent's feet, which is re-read
        immediately so the view shows the freshly written bytes.
        """
        assert entry.doc is not None
        self._capture_session()  # keep the current entry's session snapshot fresh
        palette_only = entry.palette_dirty and not entry.pixel_dirty
        try:
            pipeline.save(entry.doc, self._registry, pixel=not palette_only)
        except PipelineError as exc:
            self._report(exc)
            return False
        self._workspace.mark_saved(entry, pixel=not palette_only)
        # Invalidated even for a palette-only write: in Offset mode the palette's
        # target *is* this entry's own file, so other entries on it are stale too.
        self._workspace.invalidate_path(entry.path, keep=entry)
        self._refresh_stale_current()
        return True

    def _refresh_stale_current(self) -> None:
        """Re-read the active entry if a save into its file dropped its cache,
        preserving the on-screen view position and palette."""
        entry = self._workspace.current
        if entry is None or entry.doc is not None:
            return
        stale = self._doc  # the document still on screen
        if not self._load_entry(entry):
            return  # reported; the stale view stays until the next activation
        if stale is not None:
            entry.doc.view = stale.view
            entry.doc.palette = stale.palette
            entry.doc.palette_config = stale.palette_config
            entry.doc.palette_ctx = stale.palette_ctx
        self._doc = entry.doc
        self._refresh_view()

    # -- slice creation ------------------------------------------------------
    def _seed_slice_from_parent(self, slice_entry: Entry) -> None:
        """Open a new slice reading its parent the way the parent is read *now*.

        A slice is a region of its parent file viewed through the same codecs,
        so it should inherit the parent's current pixel preset and palette
        (format, mode, and the actual offset/file/colors) rather than the
        app-wide toolbar defaults - otherwise a slice carved from a file being
        viewed as, say, snes-4bpp with an offset palette would open blank as
        the built-in default. Pre-seeding the entry's session/pending-palette
        here means its first load skips :meth:`_seed_session`; both are
        consumed on that load. If the parent isn't open (or was never
        activated) there's nothing to copy - the toolbar seed then applies.
        """
        parent = self._workspace.find_file(slice_entry.path)
        if parent is None or parent.session is None:
            return
        # The current entry's session snapshot lags the live toolbar until a
        # switch captures it; freshen it so we copy what's actually on screen.
        if parent is self._workspace.current:
            self._capture_session()
        src = parent.session
        slice_entry.session = EntrySession(
            pixel_preset_id=src.pixel_preset_id,
            palette_preset_id=src.palette_preset_id,
            palette_mode=src.palette_mode,
            # A slice's bytes are already decompressed - no preview codec.
            compression_id=NO_DECOMPRESS,
        )
        slice_entry.pending_palette = palette_source_for(parent)

    def _slice_prefill_offset(self) -> int:
        """The view position as an absolute file offset (raw sources only)."""
        assert self._doc is not None
        return self._doc.pixel_config.source.offset + self._byte_position()

    def _raw_slice_source(self) -> tuple[Entry, Document] | None:
        """The current entry + document if a slice can be carved from the view.

        A slice reads its parent's bytes directly, so only a live document whose
        pixel source is *raw* (no decompressor in the view) qualifies - a
        decompressed view can't spawn one. ``None`` when nothing qualifies;
        callers add any gesture-specific guard (a selection, a found structure).
        """
        entry, doc = self._workspace.current, self._doc
        if (
            entry is None
            or doc is None
            or doc.pixel_config.decompress_id != NO_DECOMPRESS
        ):
            return None
        return entry, doc

    def _new_slice_current(self) -> None:
        """File ▸ New Slice… on the current entry's file."""
        entry = self._workspace.current
        if entry is not None:
            self._new_slice_for(entry)

    def _new_slice_for(self, entry: Entry) -> None:
        """Open the slice dialog for the file ``entry`` (only files spawn
        slices - slices never nest)."""
        # Prefill from the view only when the dialog targets the file on screen;
        # a right-clicked non-current file has no live viewport to read.
        offset = (
            self._slice_prefill_offset()
            if entry is self._workspace.current and self._doc is not None
            else 0
        )
        self._create_slice_via_dialog(entry.path, offset=offset)

    def _new_slice_from_view_for(self, entry: Entry) -> None:
        """The files dock's New Slice from View - only the on-screen entry has
        a viewport, so anything else (a stale menu) is ignored."""
        if entry is self._workspace.current:
            self._new_slice_from_view()

    def _new_slice_from_view(self) -> None:
        """File ▸ New Slice from View: the dialog prefilled to cover the
        current viewport - the structure in view when the compression preview
        found one (its true extent beats the window's), else the visible
        window's bytes - plus the compression combo."""
        src = self._raw_slice_source()
        if src is None:
            return
        entry, doc = src
        length = None
        if self._structure_extent is not None:
            start, consumed = self._structure_extent
            if start == self._byte_position():
                length = consumed
        if length is None:
            # The visible window's byte extent, clamped to the data so a
            # partially blank last page doesn't slice past the end.
            page = self._columns.value() * self._rows.value() * doc.bytes_per_tile
            length = min(page, len(doc.pixel_data) - self._byte_position())
        self._create_slice_via_dialog(
            entry.path,
            offset=self._slice_prefill_offset(),
            length=length,
            decompress_id=self._compression_id(),
        )

    def _new_slice_from_selection_for(self, entry: Entry) -> None:
        """The files dock's New Slice from Selection - the selection lives on
        the on-screen entry, so anything else (a stale menu) is ignored."""
        if entry is self._workspace.current:
            self._new_slice_from_selection()

    def _new_slice_from_selection(self) -> None:
        """File ▸ New Slice from Selection: the selected tiles' byte range.

        Raw prefill (no decompressor): the selection is a run of *decoded
        raw* tiles, so unlike from-view the compression preview combo does
        not describe it.

        A slice is one offset+length region, so the selection has to be a
        continuous run of tiles. A rectangle narrower than the view isn't -
        its rows sit apart in the file - and is refused rather than quietly
        widened to the enclosing span, which would take in tiles either side
        of every row that the user never selected.
        """
        src = self._raw_slice_source()
        if src is None:
            return
        entry, doc = src
        tiles = self._selection_tiles()
        if tiles and sorted(tiles) != list(range(min(tiles), max(tiles) + 1)):
            self._alert(
                "New Slice from Selection needs a continuous run of tiles.",
                title="Celpix - new slice",
                detail=(
                    "This rectangle's rows are separated in the file, and a "
                    "slice is a single offset and length. Select the tiles as "
                    "one run (Shape ▸ Linear), or widen the rectangle to the "
                    "full width of the view."
                ),
            )
            return
        rng = self._selection_byte_range()
        if rng is None:
            return
        # Same tile→byte mapping as the hex highlight, but the trailing (possibly
        # partial) tile is clamped to the bytes that exist - a slice can't run
        # past end-of-data.
        start, length = rng
        end = min(len(doc.pixel_data), start + length)
        if end <= start:
            return
        self._create_slice_via_dialog(
            entry.path,
            offset=doc.pixel_config.source.offset + start,
            length=end - start,
        )

    def _edit_slice(self, entry: Entry) -> None:
        """The files dock's Edit… - rewrite a slice's coordinates in place.

        The same dialog as New Slice, prefilled with the current values; on OK
        the entry is re-pointed and its cached document dropped, so the region
        is re-read (immediately when it is on screen, else on activation).
        """
        if entry.kind is not EntryKind.SLICE:
            return
        if entry.pixel_dirty or entry.palette_dirty:
            answer = QMessageBox.question(
                self,
                "Celpix - edit slice",
                f"Editing {entry.name} re-reads it from disk, discarding its "
                "unsaved changes. Continue?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        params = SliceDialog.get_slice(
            self,
            self._registry,
            path=entry.path,
            offset=entry.slice_offset,
            length=entry.slice_length,
            decompress_id=entry.decompress_id,
            name=entry.name,
            title="Edit Slice",
        )
        if params is None:
            return
        before = SliceParams(
            entry.name, entry.slice_offset, entry.slice_length, entry.decompress_id
        )
        if params == before:
            return  # OK'd unchanged - nothing happened, nothing to undo
        self._push_command(SliceEditCommand(self, entry, before=before, after=params))

    def _apply_slice_params(self, entry: Entry, params: SliceParams) -> None:
        """Re-point a slice's coordinates and re-read the region - the
        application path for slice edits and their undos; works for
        non-current entries (their reload waits until activation)."""
        entry.name = params.name
        entry.slice_offset = params.offset
        entry.slice_length = params.length
        entry.decompress_id = params.decompress_id
        # Pixel edits die with the old region; the palette does not - it isn't
        # tied to the slice's coordinates, so drop_document carries it across.
        # Nothing is unsaved once the edits themselves are gone.
        self._workspace.mark_saved(entry)
        self._workspace.drop_document(entry)
        self._files_panel.refresh_entry(entry)
        if entry is self._workspace.current:
            self._on_current_entry_changed(entry)  # reload the new region now

    def _jump_to_slice_source(self, slice_entry: Entry) -> None:
        """Files dock ▸ Jump to Source: show a slice's bytes in its parent file.

        The inverse of :meth:`_seed_slice_from_parent` (which seeds a new slice
        from its parent): it reconfigures the *parent* with the *slice's* own
        pixel and palette settings and lands the view on the slice's offset, so
        the slice's tiles appear at their real position in the whole file. The
        parent is opened first if it was closed. The slice's decompression is
        deliberately *not* applied - the parent reads raw, so a raw slice shows
        exactly its own tiles at their true file address (a decompressed slice
        still lands on the right offset, over the packed source bytes).
        """
        if slice_entry.kind is not EntryKind.SLICE:
            return
        parent = self._parent_file_of(slice_entry)
        # The current entry's session snapshot lags the live toolbar until a
        # switch captures it, and the palette mode is read off that snapshot -
        # so freshen it, or jumping from the slice on screen carries the palette
        # it had when it was last switched away from rather than the one it is
        # showing. (Same reason _seed_slice_from_parent captures.)
        if slice_entry is self._workspace.current:
            self._capture_session()
        # The slice's settings live on its session (seeded on first load); seed
        # it from the toolbar if it was never activated, exactly as a load would.
        if slice_entry.session is None:
            slice_entry.session = self._seed_session(slice_entry)
        src = slice_entry.session
        # Keep the parent's view geometry (columns/rows/zoom/grid); the origin
        # is landed after load, once the new preset's tile size is known. Read
        # before _jump_into_parent drops the document it may live on.
        prior_view = parent.doc.view if parent.doc is not None else parent.pending_view
        # Adopt the slice's interpretation but keep the parent's own header skip
        # - a slice has no header concept, and its offsets are absolute anyway.
        prior = parent.session
        self._jump_into_parent(
            parent,
            slice_entry,
            session=EntrySession(
                pixel_preset_id=src.pixel_preset_id,
                palette_preset_id=src.palette_preset_id,
                palette_mode=src.palette_mode,
                headered=prior.headered if prior is not None else False,
                header_length=prior.header_length if prior is not None else 512,
            ),
            view=(
                replace(prior_view, tile_offset=0, byte_nudge=0)
                if prior_view is not None
                else None
            ),
            palette=palette_source_for(slice_entry),
        )

    def _parent_file_of(self, child: Entry) -> Entry:
        """The FILE entry a slice or bookmark anchors to, opening it if closed —
        a jump has to have somewhere to land."""
        return self._workspace.find_file(child.path) or self._workspace.open_file(
            child.path
        )

    def _jump_into_parent(
        self,
        parent: Entry,
        child: Entry,
        *,
        session: EntrySession | None,
        view: ViewOptions | None,
        palette: PaletteSource | None,
    ) -> None:
        """Re-read ``parent`` under a supplied snapshot and land on ``child``'s
        offset — the shared body of Jump to Source and Jump to Bookmark.

        The two gestures differ only in *which* snapshot they hand over: a
        slice's live settings, or a bookmark's recorded ones. From there the move
        is identical — install the snapshot, drop the cached document so the
        pending fields are consumed on the re-read, show the parent, and land
        byte-exactly on the child's absolute offset.

        The document is dropped by clearing it directly rather than through
        :meth:`Workspace.drop_document`, which would *recompute* the pending
        palette off the parent's own document and overwrite the one supplied
        here — the whole point of the jump is to arrive under the child's
        palette, not the parent's.

        This is navigation, not an edit: nothing is pushed onto the undo stack.
        """
        if session is not None:
            parent.session = session
        parent.pending_view = view
        parent.pending_palette = palette
        parent.doc = None
        if parent is self._workspace.current:
            self._on_current_entry_changed(parent)  # reload in place
        else:
            self._activate_entry(parent)
        # Land only if the parent actually loaded - a vanished file or a bad
        # codec leaves the previous view untouched.
        if self._workspace.current is parent and self._doc is not None:
            self._land_on_byte(child.slice_offset)
            self.statusBar().showMessage(f"Jumped to {child.name} in {parent.name}")

    # -- bookmarks -----------------------------------------------------------
    def _new_bookmark_current(self) -> None:
        """File ▸ New Bookmark on the current entry's file."""
        entry = self._workspace.current
        if entry is not None:
            self._new_bookmark_for(entry)

    def _new_bookmark_for(self, entry: Entry) -> None:
        """Bookmark ``entry``'s current position and settings (current FILE
        only - the snapshot reads the live view, which nothing else has).

        The snapshot is the same trio a project persists per entry - session,
        view options, palette source - copied off the live state, plus the
        view origin as an absolute file offset. A bookmark never loads a
        document, so nothing ever consumes its session/pending fields: they
        *are* the bookmark, applied back onto the parent by every jump.
        """
        if (
            entry is not self._workspace.current
            or self._doc is None
            or entry.kind is not EntryKind.FILE
        ):
            return
        self._capture_session()  # the snapshot must read the live toolbar state
        offset = self._slice_prefill_offset()
        assert entry.session is not None  # _capture_session just wrote it
        bookmark = Entry(
            # Named like the offset box shows the position (address format
            # and all) - the icon, not the name, marks it as a bookmark.
            name=self._format_offset(offset),
            kind=EntryKind.BOOKMARK,
            path=entry.path,
            slice_offset=offset,
            session=replace(entry.session),
            # The offset carries the position; the view snapshot keeps the
            # geometry (columns/rows/zoom/grid/subpalette) with the origin
            # zeroed, since the jump lands it byte-exactly itself.
            pending_view=replace(self._doc.view, tile_offset=0, byte_nudge=0),
            pending_palette=palette_source_for(entry),
        )
        self._push_command(
            AddEntryCommand(self, bookmark, f'new bookmark "{bookmark.name}"')
        )
        self.statusBar().showMessage(f"Bookmarked {bookmark.name} in {entry.name}.")

    def _jump_to_bookmark(self, bookmark: Entry) -> None:
        """Files dock ▸ double-click / Jump to Bookmark: reapply the snapshot
        to the parent file and land on the bookmark's offset.

        The :meth:`_jump_to_slice_source` flow, with the snapshot applied
        wholesale - session (header settings included: the snapshot *is* the
        parent's own state as of creation), palette source and view geometry
        are copied onto the parent, its cached document dropped so it re-reads
        through them, and the view lands on the absolute offset. Copies, never
        the originals: the parent's first load consumes its pending fields,
        and the bookmark must survive to be jumped to again.
        """
        if bookmark.kind is not EntryKind.BOOKMARK:
            return
        self._jump_into_parent(
            self._parent_file_of(bookmark),
            bookmark,
            # Copies, never the originals: the parent's first load consumes its
            # pending fields, and the bookmark must survive to be jumped to again.
            session=replace(bookmark.session) if bookmark.session is not None else None,
            view=(
                replace(bookmark.pending_view)
                if bookmark.pending_view is not None
                else None
            ),
            palette=(
                replace(bookmark.pending_palette)
                if bookmark.pending_palette is not None
                else None  # the snapshot renders through the default palette
            ),
        )

    def _use_bookmark_as_palette(self, bookmark: Entry) -> None:
        """Files dock ▸ Use as Palette: set the current view's palette to an
        offset palette read at the bookmark's offset.

        The bookmark's offset is absolute in its parent file, so the parent
        must be the shown document for the read to hit the right bytes - it is
        opened/activated if needed (navigation, like a jump), but the view
        position is left where it is; only the palette changes. The offset is
        handed to the same Offset-mode load a typed palette offset uses, so it
        is undoable and persists as an offset palette exactly like one.
        """
        if bookmark.kind is not EntryKind.BOOKMARK:
            return
        parent = self._workspace.find_file(bookmark.path)
        if parent is None:
            parent = self._workspace.open_file(bookmark.path)
        if self._workspace.current is not parent:
            self._activate_entry(parent)
        if self._workspace.current is not parent or self._doc is None:
            return  # vanished file / bad codec - leave the view untouched
        # slice_offset is file-absolute; _load_palette_at_offset re-adds the
        # header skip, so strip it to land on the absolute byte.
        self._load_palette_at_offset(bookmark.slice_offset - self._header_offset())

    def _create_slice_via_dialog(
        self,
        path: str,
        *,
        offset: int = 0,
        length: int | None = None,
        decompress_id: str = NO_DECOMPRESS,
    ) -> None:
        params = SliceDialog.get_slice(
            self,
            self._registry,
            path=path,
            offset=offset,
            length=length,
            decompress_id=decompress_id,
        )
        if params is None:
            return
        entry = new_slice(
            path, params.name, params.offset, params.length, params.decompress_id
        )
        self._seed_slice_from_parent(entry)
        self._push_command(AddEntryCommand(self, entry, f'new slice "{entry.name}"'))
