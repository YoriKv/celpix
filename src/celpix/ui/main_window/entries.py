"""The open-entries list: projects, slices, bookmarks, containers.

Everything that creates, re-points or navigates between the entries in the files
dock. A **slice** is an offset+length region of a parent file that acts as its
own document; a **bookmark** is a position plus a snapshot of the settings at
creation time, with no document of its own. Neither ever nests - both anchor to
a whole file.

The jumps share one body (:meth:`_jump_into_parent`): they differ only in which
snapshot they install on the parent before re-reading it.

Removal closes the list off at the other end, and is the one path that has to
look outside the entry being removed: a file takes its slices and bookmarks with
it, and a **palette** other graphics are rendering with cannot simply vanish -
each user is re-homed onto a Custom copy of its colors first, as one undoable
step, so nothing is left pointing at a palette that is gone.

Two neighbours own what this one deliberately does not. Putting an entry's bytes
*back on disk* — and the file/slice reconciliation that makes a region safe to
write — is :mod:`~celpix.ui.main_window.writing`. What happens when the view
*moves* between these entries is :mod:`~celpix.ui.main_window.session`'s.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from PySide6.QtWidgets import (
    QFileDialog,
    QMessageBox,
)

from celpix.core.capabilities import Capability, ContentKind
from celpix.core.document import Document, ViewOptions
from celpix.pipeline.pathway import PathwayConfig
from celpix.pipeline.pipeline import inspect_container
from celpix.plugins.base import NO_COMPRESSION, FileRef
from celpix.project import projectfile
from celpix.project.workspace import (
    Entry,
    EntryKind,
    EntrySession,
    PaletteSource,
    SliceParams,
    missing_paths,
    palette_source_for,
    path_is_palette_only,
    relocate_path,
    repair_presets,
    retarget_files,
    slice_of,
)
from celpix.ui.container_dialog import ContainerDialog, ContainerEdit
from celpix.ui.container_info_dialog import ContainerInfoDialog
from celpix.ui.slice_dialog import SliceDialog
from celpix.ui.undo_commands import (
    AddEntryCommand,
    ContainerEditCommand,
    PaletteConsumerLink,
    RemoveEntriesCommand,
    RemovePaletteWithConsumersCommand,
    SliceEditCommand,
)
from celpix.ui.widgets import (
    ask_save_path,
    clear_recent_projects,
    forget_recent_project,
    load_recent_projects,
    remember_recent_project,
    show_in_file_manager,
)


class EntriesMixin:
    """Projects, slices, bookmarks, and the file lists behind them.

    A slice of :class:`~celpix.ui.main_window.window.MainWindow`, not a
    standalone object: it reads and writes the window's own widgets and its
    single live ``_doc``. See the module docstring for what it owns, and the
    package docstring for why these are mixins.
    """

    # -- projects ------------------------------------------------------------
    _PROJECT_FILTER = "celPix project (*.celpix)"

    def _new_project(self) -> None:
        """File ▸ New Project: close everything and start over.

        The same replace :meth:`_load_project` makes, onto an empty workspace
        instead of a saved one - so it is gated by the same two questions (an
        unsaved project, unsaved bytes) and drops the same session state: the
        history, which references entries that are going away, and the project
        file this session was tied to.

        What it deliberately does *not* touch is the app's own settings - the
        grid, the theme, the recent list, the window geometry. Those outlive a
        relaunch too, so resetting them here would be less like a fresh start
        than the fresh start is.
        """
        if not self._confirm_discard_project("Starting a new project"):
            return
        if not self._resolve_dirty_entries(
            "Starting a new project closes every open file, and the unsaved "
            "changes with it"
        ):
            return
        self._workspace.hidden_pixel_presets = set()
        # The previous project's own plugins go with it - they were part of that
        # project, not of the app (:meth:`_load_project_plugins`).
        if self._project_path is not None:
            self._load_project_plugins(None)
        # -> _on_current_entry_changed(None) -> _show_empty: the canvas, the
        # palette dock and every document-bound action land on the idle state.
        self._workspace.replace([], None)
        self._fill_pixel_combo(self._pixel_preset_id())
        self._sync_locate_action()  # an empty project references nothing
        self._undo_stack.clear()
        self._project_path = None
        self._saved_project = None
        self._refresh_window_title()
        self.statusBar().showMessage("New project - nothing open.")

    def _open_project(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open project", "", self._PROJECT_FILTER
        )
        if path:
            self._load_project(path)

    def _build_recent_menu(self, file_menu) -> None:  # noqa: ANN001 - QMenu
        """File ▸ Open Recent: the projects opened before this one.

        Filled from app settings each time the File menu opens, not once at
        build time - the list changes as projects are opened and saved, and a
        second window shares the same one.
        """
        self._recent_menu = file_menu.addMenu("Open Re&cent")
        self._recent_menu.setToolTip("Reopen a recently opened project")
        file_menu.aboutToShow.connect(self._sync_recent_menu)

    def _sync_recent_menu(self) -> None:
        """Rebuild the recent-projects submenu from settings."""
        menu = self._recent_menu
        menu.clear()
        recent = load_recent_projects()
        # Nothing opened yet: the submenu greys out rather than opening onto an
        # empty box (with no rows there is nothing to clear either).
        menu.menuAction().setEnabled(bool(recent))
        for number, path in enumerate(recent, start=1):
            # Names are shown, not paths - a menu row is not a place to read a
            # path out of, and the full one is on the action for the tooltip.
            # "&" in a file name would otherwise be eaten as a mnemonic marker.
            label = Path(path).name.replace("&", "&&")
            # 1-9 get a digit mnemonic; a tenth row keeps the number as plain
            # text, since Qt has no second digit to give it.
            action = menu.addAction(f"&{number} {label}" if number < 10 else label)
            action.setToolTip(path)
            action.triggered.connect(
                lambda _checked=False, p=path: self._open_recent(p)
            )
        if recent:
            menu.addSeparator()
            clear = menu.addAction("Clear &List")
            clear.setToolTip("Forget every recent project")
            clear.triggered.connect(lambda *_: clear_recent_projects())

    def _open_recent(self, path: str) -> None:
        """Open a project off the recent list, dropping a row that has gone.

        The list outlives the files it names - a project deleted, renamed, or on
        a drive that isn't mounted is a dead row the user has no other way to
        get rid of, so a miss prunes it instead of failing again next time.
        """
        if not Path(path).exists():
            forget_recent_project(path)
            self._alert(
                f"{path} is no longer there. It has been removed from the "
                "recent projects list.",
                title="celPix - project",
            )
            return
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
            # No upgrade shims while the format is in alpha, so a file written at
            # a version this build doesn't know opens on key-level tolerance
            # alone - and says so, since what it loses it loses silently.
            self._alert(
                f"This project is at format version {loaded.version}, which this "
                f"build doesn't know (it writes version "
                f"{projectfile.PROJECT_VERSION}). It opens with what this build "
                "understands; saving will rewrite it, dropping the rest.",
                title="celPix - project",
            )
        # The project's own plugins first of all: an entry may be saved against a
        # format that only the project's plugins/ folder provides, and the
        # replace below decodes the restored entry straight away.
        self._load_project_plugins(path)
        # Now that the registry is final, point the restored entries at formats it
        # actually has — before the replace, which shows the current entry and
        # decodes it. An entry naming a format this build hasn't got would
        # otherwise fail its first decode with nothing on screen to say why.
        self._alert_missing_presets(repair_presets(loaded.entries, self._registry))
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
        remember_recent_project(path)
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
        """Arm File ▸ Locate missing files iff the project has missing files.

        Called from the points where the entry list changes shape - once per
        user-level operation, never per entry. The scan behind it stats *every*
        referenced path, so hanging it off the per-entry removal notification
        made a closed file with slices, or a project load, probe the disk
        quadratically: seconds of frozen UI when the files sit on a slow or
        disconnected drive.
        """
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
        # Which files, and what each is *for*: a palette file the user never
        # picked by hand (it followed the graphic that uses it) is otherwise an
        # unexplained name in a file picker, and a graphic that still opens on
        # the default palette looks like a missing ROM rather than a missing
        # palette.
        palette_only = {p for p in paths if path_is_palette_only(self._workspace, p)}
        if prompt_summary:
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Warning)
            box.setWindowTitle("celPix - missing files")
            box.setText(
                f"This project references {len(paths)} file(s) that couldn't be "
                "found. Locate them now?"
            )
            box.setInformativeText(self._missing_summary(paths, palette_only))
            locate = box.addButton("Locate…", QMessageBox.ButtonRole.AcceptRole)
            box.addButton("Not now", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            if box.clickedButton() is not locate:
                return
        start_dir = str(Path(self._project_path).parent) if self._project_path else ""
        relocated = 0
        for old in paths:
            what = "palette file " if old in palette_only else ""
            new, _ = QFileDialog.getOpenFileName(
                self, f"Locate {what}{Path(old).name}", start_dir
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
                self._refresh_relocated_entry(entry)
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

    @staticmethod
    def _missing_summary(paths: list[str], palette_only: set[str]) -> str:
        """The missing files listed by name, each tagged with what it is for.

        Bounded, because the count is already in the headline and a project can
        reference a lot of files - the list is here to answer "which, and is it
        my graphics or my colors", not to be the worklist (the pickers that
        follow are).
        """
        shown = paths[:6]
        lines = [
            f"{Path(p).name} (palette)" if p in palette_only else Path(p).name
            for p in shown
        ]
        if len(paths) > len(shown):
            lines.append(f"…and {len(paths) - len(shown)} more")
        return "\n".join(lines)

    def _refresh_relocated_entry(self, entry: Entry) -> None:
        """Refresh one entry after its path(s) were corrected.

        A loaded entry whose palette became reachable reloads that palette in
        place; a never-loaded (or data-relocated) entry simply reloads on its
        next activation. The list item is refreshed either way so its highlight
        clears.
        """
        if entry.doc is not None and entry.missing_palette is not None:
            self._restore_palette_source(entry, entry.missing_palette)
        self._files_panel.refresh_entry(entry)

    def _show_entry_in_manager(self, entry: Entry) -> None:
        """Files list ▸ Show in File Manager: reveal the entry's file on disk.

        The answer to "which file is this row, and where did it come from" -
        the one question the list itself can only answer with a tooltip. A
        missing file still says something useful (its folder opens, if that is
        even there), so it is reported rather than pre-disabled: the entry's
        path is exactly what the user is trying to go look at.
        """
        if not show_in_file_manager(entry.path):
            self.statusBar().showMessage(f"Cannot show {entry.path} in a file manager.")

    def _save_project(self) -> None:
        if self._project_path is None:
            self._save_project_as()
        else:
            self._save_project_to(self._project_path)

    def _save_project_as(self) -> None:
        path = ask_save_path(
            self,
            "Save project",
            self._project_path or "",
            self._PROJECT_FILTER,
            projectfile.PROJECT_EXTENSION,
        )
        if path is not None:
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
        # Saved as well as opened: a Save As is how a session first becomes a
        # project, and it is exactly the one you would reach back for.
        remember_recent_project(path)
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
            preview_compression_id=NO_COMPRESSION,
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

    def _slice_prefill_offset(self, position: int | None = None) -> int:
        """A view position in the coordinates a slice offset is written in.

        The same numbers the offset box shows, which is what a slice addresses:
        past whatever the container skipped for a whole file, 0-based in the
        reordered buffer under a reshape. Taking the *config's* requested offset
        instead would prefill a headered file's slices short by its header - the
        config never names the start the container worked out for itself.

        ``position`` defaults to the grid's current byte position; the selection
        and structure gestures pass their own document-relative start.
        """
        assert self._doc is not None
        at = self._byte_position() if position is None else position
        return self._display_base() + at

    def _slice_source(self) -> tuple[Entry, Document] | None:
        """The current entry + document if a slice can be carved from the view.

        Whatever is on screen can be carved out of, as long as its positions are
        the coordinates a slice offset is written in
        (:attr:`~celpix.pipeline.pathway.PathwayConfig.positions_are_slice_offsets`)
        - which a reshaped or interleaved view's are, because a slice of such a
        parent reads that same reordered buffer. Only a decompressed view is
        excluded. ``None`` when nothing qualifies; callers add any
        gesture-specific guard (a selection, a found structure).
        """
        entry, doc = self._workspace.current, self._doc
        if entry is None or doc is None:
            return None
        return (entry, doc) if doc.pixel_config.positions_are_slice_offsets else None

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
        self._create_slice_via_dialog(entry, offset=offset)

    def _new_slice_from_view_for(self, entry: Entry) -> None:
        """The files dock's New Slice from View - only the on-screen entry has
        a viewport, so anything else (a stale menu) is ignored."""
        if entry is self._workspace.current:
            self._new_slice_from_view()

    def _new_slice_from_view(self) -> None:
        """File ▸ New Slice from View: the dialog prefilled to cover the
        current viewport - the structure in view when the compression preview
        found one (its true extent beats the window's), else the visible
        window's bytes - plus the compression combo.

        Refused where there is no viewport to read, as well as on the action:
        the files dock builds its own row for this, so a guard on the gesture is
        what covers both. On a tilemap the prefill was not merely unhelpful, it
        was measured in another file's units - ``pixel_data`` there is the
        *bound* entry's tile bytes, so the length came off the tile bank's
        geometry and was then written into a slice of the map.
        """
        if not self._can(Capability.NAVIGATION):
            return
        src = self._slice_source()
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
            page = self._columns.value() * self._view_rows() * doc.bytes_per_tile
            length = min(page, len(doc.pixel_data) - self._byte_position())
        self._create_slice_via_dialog(
            entry,
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
        src = self._slice_source()
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
            entry,
            offset=self._slice_prefill_offset(start),
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
            paths=entry.paths,  # a slice carries its parent's whole file list
            offset=entry.slice_offset,
            length=entry.slice_length,
            compression_id=entry.compression_id,
            reshape_id=entry.reshape_id,
            slot_fill=entry.slot_fill,
            name=entry.name,
            title="Edit Slice",
            # Carried in and back out untouched: an edit re-points a live entry's
            # coordinates, and re-reading it as another kind of thing would take
            # its binding and its section in the Files list with it.
            content_kind=entry.content_kind,
        )
        if params is None:
            return
        before = SliceParams(
            entry.name,
            entry.slice_offset,
            entry.slice_length,
            entry.compression_id,
            entry.reshape_id,
            entry.content_kind,
            entry.slot_fill,
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
        entry.slot_fill = params.slot_fill
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
        # Pixel edits die with the old region; the palette does not - it isn't
        # tied to the slice's coordinates, so drop_document carries it across.
        # Nothing is unsaved once the edits themselves are gone.
        self._reread_entries([entry])

    def _reread_entries(self, entries: list[Entry]) -> None:
        """Drop each entry's document so its next activation re-reads the file.

        The view is stashed as ``pending_view`` on the way out and the entry is
        marked saved: what changed is which bytes arrive, not how they are read
        once they do, so the format, arrangement and view have to survive the
        re-read rather than coming back on the codec's defaults. The current
        entry is reloaded immediately — the rest wait until they are activated.
        The caller captures the session first if it needs the *live* widget state
        rather than what was last stored.
        """
        for entry in entries:
            if entry.doc is not None:
                entry.pending_view = entry.doc.view
            self._workspace.mark_saved(entry)
            self._workspace.drop_document(entry)
            self._files_panel.refresh_entry(entry)
        current = self._workspace.current
        if current in entries:
            self._on_current_entry_changed(current)  # re-read the new bytes now
        for entry in entries:
            # A palette is never the current entry, so nothing above reloads it —
            # but the graphics showing its colors are still holding the ones the
            # old framing produced, and those are exactly what just changed.
            if entry.kind is EntryKind.PALETTE:
                self._reload_palette_consumers(entry)

    def _reload_palette_consumers(self, entry: Entry) -> None:
        """Re-read a PALETTE entry and push its colors back onto every graphic.

        A graphic renders a file palette by reference (:meth:`_mirror_palette`),
        so re-reading the file is only half the job — without the mirror the view
        keeps showing colors decoded from bytes the entry no longer offers.
        A load failure leaves the old colors up rather than blanking the view;
        the entry's own error palette reports it when it is next opened.

        The session is snapshotted first for the reason :meth:`_remove_entry`
        does it: the current graphic's palette mode only reaches its session on a
        switch, so a palette in use *right now* would otherwise look unused and
        keep the colors it is being changed out of.
        """
        self._capture_session()
        if not self._workspace.palette_consumers(entry):
            return
        if self._load_palette_entry(entry):
            self._mirror_palette(entry)
            self._refresh_palette_dock()
            self._refresh_view()

    # -- containers ----------------------------------------------------------
    def _container_info_current(self) -> None:
        # Follows the graphic on screen, like Edit File Container… beside it; a
        # palette is never *current* and is inspected from the Files dock.
        entry = self._workspace.current
        if entry is not None and entry.kind is EntryKind.FILE:
            self._show_container_info(entry)

    def _show_container_info(self, entry: Entry) -> None:
        """Files dock / File ▸ Container Info…: what ``entry``'s container read.

        The kinds that have a container of their own, which is the same pair
        :meth:`_change_container_for` acts on — a slice reads through its parent's
        coordinates, so the report a user wants for one is the parent's.

        The config is built here rather than taken from
        :func:`~celpix.project.workspace.pixel_config_for`: the container stage
        needs only the files and the container id, and asking for a whole pathway
        would mean naming a codec preset that nothing in this report interprets.
        The **stored** container id is passed, not the resolved one, so an entry
        whose plugin this build hasn't got says which one is missing instead of
        reporting on the plain-bytes fallback it degraded to.
        """
        if entry.kind not in (EntryKind.FILE, EntryKind.PALETTE):
            return
        cfg = PathwayConfig(
            source=FileRef(entry.paths),
            interpret_preset_id="",
            container_id=entry.container_id,
        )
        ContainerInfoDialog.show_report(self, inspect_container(cfg, self._registry))

    def _change_container_current(self) -> None:
        # A palette is never *current* — the File menu's action follows the
        # graphic on screen, and a palette entry is reached from the Files dock.
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

        A **palette** entry is included, and gets a different list: its file can
        be framed too (colours that stop before the bytes do), and the dialog is
        filtered to the containers that frame a palette so the two sets of
        formats never appear in each other's menu.
        """
        if entry.kind not in (EntryKind.FILE, EntryKind.PALETTE):
            return
        edit = ContainerDialog.edit_container(
            self,
            self._registry,
            paths=entry.paths,
            container_id=entry.container_id,
            reshape_id=entry.reshape_id,
            kind=entry.content_kind,
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
        before = ContainerEdit(entry.container_id, entry.paths, entry.reshape_id)
        self._push_command(ContainerEditCommand(self, entry, before=before, after=edit))

    def _apply_container_edit(self, entry: Entry, edit: ContainerEdit) -> None:
        """Put ``edit``'s file list, container and reshape on ``entry`` and
        re-read - the application path for container edits and their undos.

        The children come along whenever the file list moved: a slice's offset
        addresses the parent's *joined* buffer, so it has to be joined the same
        way to mean anything, and it finds its parent by the path that is about
        to change (:func:`~celpix.project.workspace.retarget_files`). They are
        collected before the move, while that path is still the old one.
        """
        moved = edit.paths != entry.paths
        family = [entry, *self._workspace.children_of(entry)] if moved else [entry]
        entry.container_id = edit.container_id
        entry.reshape_id = edit.reshape_id
        # Format, arrangement and view survive the re-read for the same reason
        # they survive a slice re-point (see :meth:`_apply_slice_params`): what
        # changes is which bytes arrive, not how they are read once they do.
        if entry is self._workspace.current:
            self._capture_session()
        if moved:
            retarget_files(self._workspace, entry, edit.paths)
        self._sync_locate_action()  # the new list may name a file that isn't there
        self._reread_entries(family)

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
        the user never asked about. A palette entry's unsaved work is colors
        rather than pixels, and the re-read discards those just the same.
        """
        dirty = [e for e in entries if e.pixel_dirty or e.palette_dirty]
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
        parent is opened first if it was closed.

        A compressed slice arrives with its codec in the *preview* combo, not in
        the parent's own read: the main view always shows raw bytes, so the
        packed structure is what sits at that address, and the decompression
        preview overlay is where those bytes become the slice's tiles again. A
        raw slice leaves the combo at none, which is its ``compression_id``.
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
                # How the slice's bytes are read, not what its own combo was
                # showing: a decompressed slice previews as none (there is
                # nothing left to unpack), and it is the codec that unpacked it
                # that makes the packed bytes at this address readable.
                preview_compression_id=slice_entry.compression_id,
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

        The palette lands on whatever is on screen as long as it is anchored to
        the bookmark's file - a **slice** of it included, because a slice's
        Offset palette is addressed in its parent's coordinates too and reaches
        outside its own window by design (``docs/design/palette-editing.md``
        §2). So bookmarking where a palette sits and then colouring a slice with
        it costs no round trip through the parent. Only a view onto some *other*
        file has to navigate there first, or the offset would name the wrong
        bytes; even then the view position is left where it is, and only the
        palette changes. The offset is handed to the same Offset-mode load a
        typed palette offset uses, so it is undoable and persists as an offset
        palette exactly like one.
        """
        if bookmark.kind is not EntryKind.BOOKMARK:
            return
        current = self._workspace.current
        anchored = (
            current is not None
            and current.kind in (EntryKind.FILE, EntryKind.SLICE)
            and current.path == bookmark.path
        )
        if not anchored:
            parent = self._workspace.find_file(bookmark.path)
            if parent is None:
                parent = self._workspace.open_file(bookmark.path)
            if self._workspace.current is not parent:
                self._activate_entry(parent)
            if self._workspace.current is not parent:
                return  # vanished file / bad codec - leave the view untouched
        if self._doc is None:
            return
        # A bookmark's offset is already in the parent's coordinates, which is
        # what an Offset palette addresses - hand it over as it stands.
        self._load_palette_at_offset(bookmark.slice_offset)

    def _create_slice_via_dialog(
        self,
        parent: Entry,
        *,
        offset: int = 0,
        length: int | None = None,
        compression_id: str = NO_COMPRESSION,
    ) -> None:
        # The parent's whole file list, both to bound the dialog's offsets (a
        # region spread over several chips is addressed as the concatenation)
        # and so the slice inherits the list its offsets are relative to.
        #
        # The **Content** row is offered wherever the parent's own answer could be
        # wrong, which is both graphic readings and neither of the other two: a
        # palette file's kind comes from its ``EntryKind`` rather than a choice
        # (:meth:`~celpix.project.workspace.Entry.__post_init__`), so there is
        # nothing to pick.
        params = SliceDialog.get_slice(
            self,
            self._registry,
            paths=parent.paths,
            offset=offset,
            length=length,
            compression_id=compression_id,
            content_kind=parent.content_kind,
            choose_content=parent.content_kind
            in (ContentKind.PIXELS, ContentKind.TILEMAP),
        )
        if params is None:
            return
        entry = slice_of(
            parent,
            params.name,
            params.offset,
            params.length,
            params.compression_id,
            reshape_id=params.reshape_id,
        )
        # ``slice_of`` carried the parent's kind down; the dialog's answer is the
        # user's word over it, and is the parent's own value when unchanged.
        entry.content_kind = params.content_kind
        entry.slot_fill = params.slot_fill
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
            users = self._workspace.palette_consumers(entry)
            if users:
                self._remove_palette_with_consumers(entry, users)
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

    def _remove_palette_with_consumers(
        self, palette: Entry, consumers: list[Entry]
    ) -> None:
        """Confirm, then remove a file palette that graphics render - re-homing
        each onto a Custom copy so none is left showing a palette that's gone.

        The user is told exactly where it is used before the colors are frozen into
        each graphic's own Custom palette. Undoable as one step.
        """
        names = ", ".join(c.name for c in consumers)
        answer = QMessageBox.question(
            self,
            "celPix - remove palette",
            f"Remove {palette.name}? It is used by {len(consumers)} "
            f"graphic(s): {names}.\n\nEach keeps these colors as its own custom "
            "palette, stored in the project.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        index = self._workspace.entries.index(palette)
        links = []
        for consumer in consumers:
            src = palette_source_for(consumer)
            links.append(
                PaletteConsumerLink(
                    entry=consumer,
                    path=src.path if src and src.path else palette.path,
                    offset=src.offset if src else 0,
                    preset_id=(
                        consumer.session.palette_preset_id
                        if consumer.session is not None
                        else self._palette_preset_id()
                    ),
                    loaded=consumer.doc is not None,
                )
            )
        self._push_command(
            RemovePaletteWithConsumersCommand(
                self, palette, index=index, consumers=links
            )
        )

    def _apply_remove_palette_to_custom(
        self, palette: Entry, consumers: list[PaletteConsumerLink]
    ) -> None:
        """Freeze the palette's colors into each graphic as a Custom copy, then
        drop the palette - :class:`RemovePaletteWithConsumersCommand`'s redo."""
        colors = self._file_palette_colors(palette)
        preset = palette.palette_preset_id or self._palette_preset_id()
        for link in consumers:
            self._convert_graphic_to_custom(link.entry, colors, preset)
        self._workspace.close(palette)
        self._sync_locate_action()
        self._reshow_current_entry()

    def _apply_restore_palette_consumers(
        self, palette: Entry, index: int, consumers: list[PaletteConsumerLink]
    ) -> None:
        """Re-register the palette and relink every graphic - the command's undo."""
        self._workspace.insert(palette, index)
        self._sync_locate_action()
        for link in consumers:
            self._relink_graphic_to_file_palette(link)
        self._reshow_current_entry()

    def _reshow_current_entry(self) -> None:
        """Re-apply the current entry's (possibly changed) palette to the dock and
        canvas after a re-home, so the on-screen mode/label follow the entry."""
        current = self._workspace.current
        if current is not None and current.doc is not None:
            self._restore_session(current)
        self._refresh_view()
