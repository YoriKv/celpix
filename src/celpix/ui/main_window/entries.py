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

Removal closes the list off at the other end, and is the one path that has to
look outside the entry being removed: a file takes its slices and bookmarks with
it, and a **palette** other graphics are rendering with cannot simply vanish -
each user is re-homed onto a Custom copy of its colors first, as one undoable
step, so nothing is left pointing at a palette that is gone.

What happens when the view *moves* between these entries is
:mod:`~celpix.ui.main_window.session`'s, not this module's.
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
from celpix.plugins.base import NO_COMPRESSION, NO_RESHAPE
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
    retarget_files,
    slice_of,
)
from celpix.ui.container_dialog import ContainerDialog
from celpix.ui.slice_dialog import SliceDialog
from celpix.ui.undo_commands import (
    AddEntryCommand,
    PaletteUserLink,
    RemoveEntriesCommand,
    RemovePaletteWithUsersCommand,
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
    _PROJECT_FILTER = "celPix project (*.celpix)"

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
            self._alert(str(exc), title="celPix - project")
            return
        if loaded.version > projectfile.PROJECT_VERSION:
            self._alert(
                "This project was saved by a newer celPix. It opens with what "
                "this version understands, but saving will rewrite it at "
                f"version {projectfile.PROJECT_VERSION}, dropping the rest.",
                title="celPix - project",
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
        self._refresh_window_title()
        self.statusBar().showMessage(
            f"Loaded project {Path(path).name} ({len(loaded.entries)} entries)."
        )
        # Referenced files may have moved since the project was saved - offer to
        # re-point them straight away, and arm the menu for later.
        self._sync_locate_action()
        if missing_paths(self._workspace):
            self._relocate_missing(prompt_summary=True)

    def _sync_locate_action(self) -> None:
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
            box.setWindowTitle("celPix - missing files")
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
                    title="celPix - locate",
                )
                continue
            for entry in relocate_path(self._workspace, old, new):
                self._reload_relocated_entry(entry)
            relocated += 1
        self._sync_locate_action()
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
            self._alert(f"Cannot write {path}: {exc}", title="celPix - project")
            return
        self._project_path = path
        self._saved_project = self._project_snapshot()  # the new clean baseline
        # A first Save Project As gives the session a project file - title to it.
        self._refresh_window_title()
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
        box.setWindowTitle("celPix - unsaved project")
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
        box.setWindowTitle("celPix - unsaved changes")
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
        """File ▸ Write: the current file or slice, and the palette file it shows.

        A File-mode palette is owned by its own PALETTE entry, so a color edit
        dirties *that* and never the graphic rendering it
        (``docs/design/palette-editing.md`` §2). Ctrl+W means "save what I am
        looking at", and the palette on screen is part of that - so an unsaved
        one is written with the graphic rather than needing a second gesture in
        the Files list. Only when it actually has changes: writing a clean
        ``.pal`` would bump the mtime of a file other graphics may share, for
        nothing.

        The two are separate files and are written independently, so a failure on
        one (already reported) doesn't take the other with it; the status line
        names whatever really landed.
        """
        entry = self._workspace.current
        if entry is None or entry.doc is None:
            return
        # Read before the write, which clears the flags it acts on.
        palette_only = entry.palette_dirty and not entry.pixel_dirty
        has_palette_file = entry.doc.palette_config.write_enabled
        palette = self._linked_palette_entry()
        if palette is None or palette.doc is None or not palette.palette_dirty:
            palette = None
        wrote_entry = self._write_entry(entry)
        wrote_palette = palette is not None and self._write_entry(palette)
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
        landed = [f"{entry.name} ({wrote})"] if wrote_entry else []
        if wrote_palette:
            landed.append(palette.name)
        if landed:
            self.statusBar().showMessage(f"Wrote {' and '.join(landed)}.")

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
                title="celPix - write",
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

        Pixels still floating over the current entry are set down first: a float
        is on screen but not in the document, and a file that doesn't match what
        the user is looking at is not what Write means.
        """
        assert entry.doc is not None
        if entry is self._workspace.current:
            self._commit_float()
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
        # Every file of a region, since a save can have rewritten any of them.
        for path in entry.paths:
            self._workspace.invalidate_path(path, keep=entry)
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

        The **subpalette row** and the **arrangement** (the Pattern picker's
        block size/order/2D, and the bitmap width that belongs to 2D) ride along
        too. Both are part of how the bytes are read rather than merely where the
        window sits: the row picks which colors the tiles index (with a 4bpp
        format over a 256-color palette, which 16), and the arrangement decides
        which bytes land in which tile at all - so a slice carved out of
        graphics being viewed on row 3 in 2x2 blocks has to arrive that way, or
        it opens in the wrong colors or as scrambled tiles. Both live in the
        view options rather than the session, hence the second hand-off.
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
            compression_id=NO_COMPRESSION,
        )
        slice_entry.pending_palette = palette_source_for(parent)
        # Only the subpalette row and the arrangement: the rest of the geometry
        # is left at the defaults a fresh slice gets anyway, since the parent's
        # window size and zoom describe a different region than the one being
        # carved out. (Columns is the exception the bitmap width owns - it is
        # re-derived from the width on the render path.)
        view = parent.doc.view if parent.doc is not None else parent.pending_view
        if view is not None:
            slice_entry.pending_view = ViewOptions(
                subpalette_row=view.subpalette_row,
                block_columns=view.block_columns,
                block_rows=view.block_rows,
                block_order=view.block_order,
                two_dimensional=view.two_dimensional,
                bitmap_width=view.bitmap_width,
            )

    def _slice_prefill_offset(self) -> int:
        """The view position in the coordinates a slice offset is written in.

        The same numbers the offset box shows, which is what a slice addresses:
        past whatever the container skipped for a whole file, the slice offset
        for a raw slice. Taking the *config's* requested offset instead would
        prefill a headered file's slices short by its header - the config never
        names the start the container worked out for itself.
        """
        assert self._doc is not None
        return self._display_base() + self._byte_position()

    def _raw_slice_source(self) -> tuple[Entry, Document] | None:
        """The current entry + document if a slice can be carved from the view.

        A slice reads its parent's bytes directly, so only a live document whose
        pixel source is *raw* (no decompressor, no reshape in the view)
        qualifies - a decompressed view can't spawn one, and a reshaped view is
        a byte permutation whose positions name no file offset. ``None`` when
        nothing qualifies; callers add any gesture-specific guard (a selection,
        a found structure).
        """
        entry, doc = self._workspace.current, self._doc
        if (
            entry is None
            or doc is None
            or doc.pixel_config.compression_id != NO_COMPRESSION
            or doc.pixel_config.reshape_id != NO_RESHAPE
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
            compression_id=self._compression_id(),
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
                title="celPix - new slice",
                detail=(
                    "This rectangle's rows are separated in the file, and a "
                    "slice is a single offset and length. Select the tiles as "
                    "one run (Selection ▸ Linear), or widen the rectangle to the "
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
                "celPix - edit slice",
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
            compression_id=entry.compression_id,
            reshape_id=entry.reshape_id,
            name=entry.name,
            title="Edit Slice",
        )
        if params is None:
            return
        before = SliceParams(
            entry.name,
            entry.slice_offset,
            entry.slice_length,
            entry.compression_id,
            entry.reshape_id,
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
        entry.compression_id = params.compression_id
        entry.reshape_id = params.reshape_id
        # How the region is *read* and *laid out* belongs to the entry, not to
        # the coordinates: re-pointing changes which bytes arrive, not the
        # format or arrangement they arrive in. The session snapshot only
        # tracks the live toolbar at explicit capture points, and the view lives
        # on the document about to be dropped - so both are taken here, or the
        # re-read comes back on the entry's stale format and the codec's default
        # geometry, silently dropping a wide-bitmap width (which _load_entry
        # needs *before* the load, to re-cut the tile size) along with the
        # columns, zoom and 2D walk built around it. Undo runs through here too,
        # so it restores the setup its own re-read is about to discard.
        if entry is self._workspace.current:
            self._capture_session()
        if entry.doc is not None:
            entry.pending_view = entry.doc.view
        # Pixel edits die with the old region; the palette does not - it isn't
        # tied to the slice's coordinates, so drop_document carries it across.
        # Nothing is unsaved once the edits themselves are gone.
        self._workspace.mark_saved(entry)
        self._workspace.drop_document(entry)
        self._files_panel.refresh_entry(entry)
        if entry is self._workspace.current:
            self._on_current_entry_changed(entry)  # reload the new region now

    # -- containers ----------------------------------------------------------
    def _change_container_current(self) -> None:
        entry = self._workspace.current
        if entry is not None and entry.kind is EntryKind.FILE:
            self._change_container_for(entry)

    def _change_container_for(self, entry: Entry) -> None:
        """Files dock / File ▸ Edit File Container…: repick ``entry``'s files and
        how they are unwrapped.

        Detection chose the container when the file was opened; this is the
        override for what only a person can settle (an interleaved image is
        indistinguishable from a plain one, a headerless dump still ends in
        ``.nes``) — and, beside it, the list of files whose bytes make up the
        region and the region's reshape, neither of which anything can detect
        at all. Slices are excluded for the reason on
        :attr:`Entry.container_id` — theirs are their parent's coordinates, and
        its file list; a *slice's* reshape is edited in the slice dialog.
        """
        if entry.kind is not EntryKind.FILE:
            return
        edit = ContainerDialog.edit_container(
            self,
            self._registry,
            paths=entry.paths,
            container_id=entry.container_id,
            reshape_id=entry.reshape_id,
        )
        if edit is None:
            return
        moved = edit.paths != entry.paths
        if (
            not moved
            and edit.container_id == entry.container_id
            and edit.reshape_id == entry.reshape_id
        ):
            return
        if moved and not self._retarget_allowed(entry, edit.paths[0]):
            return
        # A container decides which bytes the file even has, and so does the file
        # list — so applying either is a re-read, and pixel edits describe
        # positions the new bytes may not have. They cannot come across, so the
        # user gets the choice first. A re-pointed file re-reads its slices with
        # it, so their edits are on the table too.
        family = [entry, *self._workspace.children_of(entry)] if moved else [entry]
        if not self._confirm_container_discard(family):
            return
        entry.container_id = edit.container_id
        entry.reshape_id = edit.reshape_id
        # Format, arrangement and view survive the re-read for the same reason
        # they survive a slice re-point (see :meth:`_apply_slice_params`): what
        # changes is which bytes arrive, not how they are read once they do.
        if entry is self._workspace.current:
            self._capture_session()
        if moved:
            retarget_files(self._workspace, entry, edit.paths)
        for touched in family:
            if touched.doc is not None:
                touched.pending_view = touched.doc.view
            self._workspace.mark_saved(touched)
            self._workspace.drop_document(touched)
            self._files_panel.refresh_entry(touched)
        self._sync_locate_action()  # the new list may name a file that isn't there
        if self._workspace.current in family:
            self._on_current_entry_changed(self._workspace.current)  # re-read now

    def _retarget_allowed(self, entry: Entry, first: str) -> bool:
        """Whether ``entry`` may take ``first`` as its file — i.e. its identity.

        The same rule as relocating onto an open file (:meth:`_relocate_missing`),
        and for the same reason: an entry is its first file, so two entries naming
        one would be two documents editing the same bytes.
        """
        clash = self._workspace.find_file(first)
        if clash is None or clash is entry:
            return True
        self._alert(
            f"{Path(first).name} is already open in this project, so it can't "
            f"become {entry.name}'s file. Pick a different one, or close the "
            "duplicate first.",
            title="celPix - files",
        )
        return False

    def _confirm_container_discard(self, entries: list[Entry]) -> bool:
        """Unsaved-edits gate for a container/file-list change; True to proceed.

        The per-entry sibling of :meth:`_resolve_dirty_entries`: only these
        entries are about to be re-read, so offering Write All would touch files
        the user never asked about.
        """
        dirty = [e for e in entries if e.pixel_dirty]
        if not dirty:
            return True
        names = ", ".join(e.name for e in dirty)
        box = QMessageBox(self)
        box.setWindowTitle("celPix - unsaved changes")
        box.setText(
            f"{names} {'have' if len(dirty) > 1 else 'has'} unsaved edits. "
            "Applying this re-reads the bytes and discards them. Write them first?"
        )
        write = box.addButton("Write", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Discard", QMessageBox.ButtonRole.DestructiveRole)
        cancel = box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(write)  # least-lossy choice when Enter is hit blind
        box.exec()
        if box.clickedButton() is cancel:
            return False
        if box.clickedButton() is write:
            for entry in dirty:
                self._write_entry_checked(entry)
            # A failed write must not proceed — its edits would go with the re-read.
            return not any(e.pixel_dirty for e in dirty)
        return True

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
        self._jump_into_parent(
            parent,
            slice_entry,
            session=EntrySession(
                pixel_preset_id=src.pixel_preset_id,
                palette_preset_id=src.palette_preset_id,
                palette_mode=src.palette_mode,
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
        # A bookmark's offset is already in the parent's coordinates, which is
        # what an Offset palette addresses - hand it over as it stands.
        self._load_palette_at_offset(bookmark.slice_offset)

    def _create_slice_via_dialog(
        self,
        path: str,
        *,
        offset: int = 0,
        length: int | None = None,
        compression_id: str = NO_COMPRESSION,
    ) -> None:
        params = SliceDialog.get_slice(
            self,
            self._registry,
            path=path,
            offset=offset,
            length=length,
            compression_id=compression_id,
        )
        if params is None:
            return
        # Through the parent entry when it is open, so a slice of a several-file
        # region inherits the whole list its offsets are relative to.
        parent = self._workspace.find_file(path)
        args = (params.name, params.offset, params.length, params.compression_id)
        entry = (
            slice_of(parent, *args, reshape_id=params.reshape_id)
            if parent is not None
            else new_slice(path, *args, reshape_id=params.reshape_id)
        )
        self._seed_slice_from_parent(entry)
        self._push_command(AddEntryCommand(self, entry, f'new slice "{entry.name}"'))

    # -- removal -------------------------------------------------------------
    def _remove_entry(self, entry: Entry) -> None:
        """Remove ``entry`` from the list (a file takes its slices and
        bookmarks with it), always confirming first - Remove is also on the
        Delete key, and a slip there costs the entry's whole session setup."""
        if entry.kind is EntryKind.PALETTE:
            # The current graphic's palette mode is only written to its session on
            # a switch, so snapshot it first - otherwise a palette in use *right
            # now* looks unused and would be dropped without re-homing it.
            self._capture_session()
            users = self._workspace.palette_users(entry)
            if users:
                self._remove_used_palette(entry, users)
                return
        victims = [entry, *self._workspace.children_of(entry)]
        dirty = [e.name for e in victims if e.pixel_dirty or e.palette_dirty]
        message = f"Remove {entry.name}?"
        parts = []
        counts = [
            f"{n} {label}(s)"
            for label, n in (
                ("slice", sum(e.kind is EntryKind.SLICE for e in victims[1:])),
                ("bookmark", sum(e.kind is EntryKind.BOOKMARK for e in victims[1:])),
            )
            if n
        ]
        if counts:
            parts.append(f"removes its {' and '.join(counts)}")
        if dirty:
            parts.append(f"discards unsaved changes ({', '.join(dirty)})")
        if parts:
            message = f"Remove {entry.name}? This also " + " and ".join(parts) + "."
        answer = QMessageBox.question(self, "celPix - remove", message)
        if answer != QMessageBox.StandardButton.Yes:
            return
        entries = self._workspace.entries
        self._push_command(
            RemoveEntriesCommand(
                self,
                entry,
                victims=[(entries.index(e), e) for e in victims],
                was_current=self._workspace.current,
            )
        )

    def _remove_used_palette(self, palette: Entry, users: list[Entry]) -> None:
        """Confirm, then remove a file palette that graphics use - re-homing each
        graphic onto a Custom copy so none is left rendering a palette that's gone.

        The user is told exactly where it is used before the colors are frozen into
        each graphic's own Custom palette. Undoable as one step.
        """
        names = ", ".join(u.name for u in users)
        answer = QMessageBox.question(
            self,
            "celPix - remove palette",
            f"Remove {palette.name}? It is used by {len(users)} "
            f"graphic(s): {names}.\n\nEach keeps these colors as its own custom "
            "palette, stored in the project.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        index = self._workspace.entries.index(palette)
        links = []
        for user in users:
            src = palette_source_for(user)
            links.append(
                PaletteUserLink(
                    entry=user,
                    path=src.path if src and src.path else palette.path,
                    offset=src.offset if src else 0,
                    preset_id=(
                        user.session.palette_preset_id
                        if user.session is not None
                        else self._palette_preset_id()
                    ),
                    loaded=user.doc is not None,
                )
            )
        self._push_command(
            RemovePaletteWithUsersCommand(self, palette, index=index, users=links)
        )

    def _apply_remove_palette_to_custom(
        self, palette: Entry, users: list[PaletteUserLink]
    ) -> None:
        """Freeze the palette's colors into each user as a Custom copy, then drop
        the palette from the list - :class:`RemovePaletteWithUsersCommand`'s redo."""
        colors = self._file_palette_colors(palette)
        preset = palette.palette_preset_id or self._palette_preset_id()
        for link in users:
            self._convert_user_to_custom(link.entry, colors, preset)
        self._workspace.close(palette)
        self._refresh_current_palette()

    def _apply_restore_palette_users(
        self, palette: Entry, index: int, users: list[PaletteUserLink]
    ) -> None:
        """Re-register the palette and relink every user - the command's undo."""
        self._workspace.insert(palette, index)
        for link in users:
            self._relink_user_to_file_palette(link)
        self._refresh_current_palette()

    def _refresh_current_palette(self) -> None:
        """Re-apply the current entry's (possibly changed) palette to the dock and
        canvas after a re-home, so the on-screen mode/label follow the entry."""
        current = self._workspace.current
        if current is not None and current.doc is not None:
            self._restore_session(current)
        self._refresh_view()
