"""Projects: starting over, opening, saving — and re-pointing moved files.

A project is the open entries and what the session set up on each, written to a
``.celpix`` file (``docs/design/project-format.md``). This module owns the File
menu's project rows: New Project, Open Project and the recent list, loading a
project's entries, Save and Save As, and the question asked before a project
with unsaved changes is replaced. It also owns **locating missing files**: a
project whose files moved opens with those entries unavailable, and pointing
them at the new place is one undo step (:class:`~celpix.ui.undo_commands.
LocateFilesCommand`), so the relocation is a change to the project like any
other.

Whether the project *is* unsaved is not decided here: the snapshot a save
leaves behind and the comparison against it belong to the shell
(:mod:`~celpix.ui.main_window.window`), since the title bar and the close
prompt read them too.

Reads, through ``self``: ``_project_path`` and ``_saved_project`` (created in
``MainWindow.__init__``, written here on every load and save) and
``_locate_missing_action`` (created by ``MainWindow._build_menus``). Creates
``_recent_menu`` in :meth:`~ProjectsMixin._build_recent_menu`, which the File
menu's construction calls.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from PySide6.QtWidgets import (
    QFileDialog,
    QMessageBox,
)

from celpix.project import documents, projectfile
from celpix.project.workspace import (
    Entry,
    PaletteSource,
    missing_paths,
    one_disk_scan,
    path_is_palette_only,
    relocate_path,
    repair_presets,
)
from celpix.ui.undo_commands import (
    LocatedState,
    LocateFilesCommand,
)
from celpix.ui.widgets import (
    ask_save_path,
    clear_recent_projects,
    confirm_destructive,
    forget_recent_project,
    load_recent_projects,
    remember_recent_project,
    show_in_file_manager,
)


class ProjectsMixin:
    """Projects: New, Open, recent, Save, and locating missing files.

    A slice of :class:`~celpix.ui.main_window.window.MainWindow`, not a
    standalone object. See the module docstring for what it owns and which
    attributes it reads that others create.
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
        # Back to unanswered rather than to square: a new project's first file is
        # entitled to have its container seed the shape, exactly as a loaded one's
        # is (:attr:`~celpix.project.workspace.Workspace.pixel_aspect`).
        self._workspace.pixel_aspect = None
        self._sync_pixel_aspect()
        # The previous project's own plugins go with it - they were part of that
        # project, not of the app (:meth:`_load_project_plugins`).
        if self._project_path is not None:
            self._load_project_plugins(None)
        # Pixels floating over the outgoing entry go with it, unlanded: the user
        # has just agreed to discard that project, so setting them down would
        # write into a document nobody will save (entry switching lands them
        # instead - there the entry stays).
        self._clear_pixel_selection()
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
        self._recent_menu.setToolTip("Reopen a recent project")
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
            clear.setToolTip("Clear the recent projects list")
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

    def open_project(self, path: str) -> None:
        """Open the project at ``path`` in place of the workspace — the entry
        point for a caller outside the window (the command line). The same
        gesture as File ▸ Open Project, confirmations and all
        (:meth:`_load_project`)."""
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
        # One pass over the disk for the whole open. The loader resolves every
        # stored path, then every row of the Files list asks the same question
        # again to draw its warning, and Locate asks a third time - which on a
        # project of several hundred entries, with its files on a slow drive, was
        # the whole of the wait to open it
        # (:func:`~celpix.project.workspace.one_disk_scan`). The scan is closed
        # before the relocation walk below, which is the one part of this that
        # changes what is on disk.
        with one_disk_scan():
            missing = self._load_project_entries(path)
        if missing is None:
            return
        # Referenced files may have moved since the project was saved - offer to
        # re-point them straight away. The menu row was armed inside the scan.
        if missing:
            self._relocate_missing(prompt_summary=True)

    def _load_project_entries(self, path: str) -> list[str] | None:
        """The disk-reading half of :meth:`_load_project`; its missing paths.

        ``None`` when the load did not happen, which is the caller's signal to
        stop rather than an empty worklist. Split out only so the scan above
        wraps a block rather than most of a method.
        """
        try:
            loaded = projectfile.load_project(path)
        except projectfile.ProjectError as exc:
            self._alert(str(exc), title="celPix - project")
            return None
        if loaded.version > projectfile.PROJECT_VERSION:
            # A newer file is the one case with no migration to run: it opens on
            # key-level tolerance alone - and says so, since what it loses it
            # loses silently. An *older* file needed no dialog; it was walked
            # forward on the way in, and the status line below mentions it.
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
        # Also before the replace, and after the repair: a base re-counted in the
        # unit its index counts reads the format the entry now names, and the
        # replace draws the restored entry with the base in force.
        self._alert_recounted_bases(
            documents.count_bases_in_units(self._registry, loaded)
        )
        # Seed the pixel-format filter before the replace: showing the restored
        # current entry rebuilds the dropdown, which must already read the
        # project's filter. A rebuild also happens explicitly below for a project
        # with no shown entry.
        self._workspace.hidden_pixel_presets = set(loaded.hidden_pixel_presets)
        # Before the replace for the same reason the filter is: showing the
        # restored current entry draws it, and drawing it at the previous
        # project's pixel shape would flash the wrong geometry and re-lay the
        # scroll area a moment later.
        self._workspace.pixel_aspect = loaded.pixel_aspect
        self._sync_pixel_aspect()
        # Dropped, not landed, as in :meth:`_new_project`.
        self._clear_pixel_selection()
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
        # (an offset past a shrunken file, a palette row past the palette).
        # Snapshotting before that would leave the project reading dirty the
        # instant it opened, for changes the user never made.
        self._saved_project = self._project_snapshot()
        # An upgraded project is an unsaved one: the file on disk is still at the
        # old version, so the baseline says so and the session reads as edited.
        # Save keeps the upgrade, quitting without saving leaves the file as it
        # was - the same choice as any other edit, and no dialog of its own.
        if loaded.migrated_from is not None and self._saved_project is not None:
            self._saved_project["version"] = loaded.migrated_from
        # The replace above titled the window from the restored entry (no project
        # path was set yet); now that one is, retitle to name the project file -
        # which also raises the unsaved marker for an upgrade.
        self._refresh_window_title()
        upgraded = (
            ""
            if loaded.migrated_from is None
            else f" Upgraded from format version {loaded.migrated_from}"
            f" - save to keep version {projectfile.PROJECT_VERSION}."
        )
        self.statusBar().showMessage(
            f"Loaded project {Path(path).name} "
            f"({len(loaded.entries)} entries).{upgraded}"
        )
        # Referenced files may have moved since the project was saved: arm the
        # menu for later, and hand the worklist back so the caller can offer the
        # walk. Both readings sit inside the scan the caller opened, so the
        # second one costs no disk at all.
        self._sync_locate_action()
        return missing_paths(self._workspace)

    def _sync_locate_action(self) -> None:
        """Arm File ▸ Locate missing files iff the project has missing files.

        Called from the points where the entry list changes shape - once per
        user-level operation, never per entry. The scan behind it stats every
        distinct referenced path, which is cheap enough once and not per row: on
        a slow or disconnected drive a stat costs milliseconds, and there is one
        per open file.
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
        # The whole run is one undo step, and it is interactive, so it is done
        # first and pushed after: every entry's references as they stood, and
        # the list, so the palette rows a reachable file palette registers on
        # the way can be told apart and taken back out.
        before = {id(e): self._located_state(e) for e in self._workspace.entries}
        listed = list(self._workspace.entries)
        touched: list[Entry] = []
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
                    f"{Path(new).name} is already open in this project. Pick a "
                    "different file, or close it first.",
                    title="celPix - locate",
                )
                continue
            for entry in relocate_path(self._workspace, old, new):
                self._refresh_relocated_entry(entry)
                if not any(entry is seen for seen in touched):
                    touched.append(entry)
            relocated += 1
        # Located paths replaced the missing ones in place, which no addition or
        # removal announces: the watch follows them to the files now read.
        self._sync_disk_watch()
        self._sync_locate_action()
        # Re-show the current entry: a now-resolvable one loads; one whose picked
        # file was invalid (or still skipped) falls back to the unavailable state.
        self._on_current_entry_changed(self._workspace.current)
        if relocated:
            added = [
                (index, entry)
                for index, entry in enumerate(self._workspace.entries)
                if not any(entry is was for was in listed)
            ]
            self._push_command(
                LocateFilesCommand(
                    self,
                    [(entry, before[id(entry)]) for entry in touched],
                    added,
                    relocated,
                )
            )
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

    @staticmethod
    def _located_state(entry: Entry) -> LocatedState:
        """``entry``'s references as they stand — one half of a Locate step."""
        doc = entry.doc
        return LocatedState(
            path=entry.path,
            extra_paths=entry.extra_paths,
            name=entry.name,
            missing_palette=_copied(entry.missing_palette),
            pending_palette=_copied(entry.pending_palette),
            doc=doc,
            palette=(
                (
                    doc.palette,
                    doc.palette_config,
                    doc.palette_ctx,
                    doc.palette_base_bytes,
                    frozenset(doc.palette_edits),
                )
                if doc is not None
                else None
            ),
        )

    def _apply_located_states(
        self,
        states: list[tuple[Entry, LocatedState]],
        added: list[tuple[int, Entry]],
        *,
        restore_added: bool,
    ) -> list[tuple[Entry, LocatedState]]:
        """Put each entry's references back as ``states`` has them — a Locate
        step in either direction — and return the states it replaced.

        Each entry gets its document back as well: an entry whose file was found
        loaded a document that the undo has no use for (the path it reads is
        missing again), and an entry whose *palette* was found had it loaded onto
        the document it already had, so the colors it showed before go back onto
        that document. ``added`` are the palette rows the run registered; undo
        takes them out and redo puts the same objects back, before the graphics
        that mirror them are touched.
        """
        self._capture_session()
        leaving = [(entry, self._located_state(entry)) for entry, _ in states]
        if restore_added:
            self._apply_restore_entries(added, None)
        else:
            for _index, entry in reversed(added):
                if entry in self._workspace.entries:
                    self._apply_close_entry(entry)
        for entry, state in states:
            entry.path = state.path
            entry.extra_paths = state.extra_paths
            entry.name = state.name
            entry.missing_palette = _copied(state.missing_palette)
            entry.pending_palette = _copied(state.pending_palette)
            entry.doc = state.doc
            if state.doc is not None and state.palette is not None:
                doc = state.doc
                palette, config, ctx, base, edits = state.palette
                doc.palette, doc.palette_config, doc.palette_ctx = palette, config, ctx
                doc.palette_base_bytes, doc.palette_edits = base, set(edits)
            self._files_panel.refresh_entry(entry)
        # The paths moved under entries already in the list, which no addition or
        # removal announces: the watch follows them to the files now read.
        self._sync_disk_watch()
        self._sync_locate_action()
        self._on_current_entry_changed(self._workspace.current)
        self._refresh_window_title()
        return leaving

    def _refresh_relocated_entry(self, entry: Entry) -> None:
        """Refresh one entry after its path(s) were corrected.

        A loaded entry whose palette became reachable reloads that palette in
        place; a never-loaded (or data-relocated) entry simply reloads on its
        next activation - a failed one included, since the file it now reads is
        not the one that failed. The list item is refreshed either way so its
        highlight clears.
        """
        if entry.doc is not None and entry.missing_palette is not None:
            self._restore_palette_source(entry, entry.missing_palette)
        entry.load_failure = None
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
            projectfile.save_project(self._workspace, path, self._registry)
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

        def save() -> bool:
            self._save_project()
            # A save that failed (or that its own dirty-files gate cancelled)
            # left the project dirty - don't proceed past it.
            return not self._project_is_dirty()

        return confirm_destructive(
            self,
            "celPix - unsaved project",
            f"{action} discards unsaved changes to "
            f"{Path(self._project_path).name}. Save the project first?",
            "Save Project",
            "Discard",
            save,
        )


def _copied(source: PaletteSource | None) -> PaletteSource | None:
    """A copy of ``source``: relocation re-points a palette source in place, so a
    state that must survive the next run cannot share the live one."""
    return replace(source) if source is not None else None
