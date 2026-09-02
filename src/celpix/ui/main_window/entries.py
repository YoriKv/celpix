"""The open-entries list: files, projects, slices, bookmarks, containers.

Everything that creates, re-points or navigates between the entries in the files
dock. It starts at the plainest of them - opening a file at all, which is one
entry appended to the workspace and the view switched onto it, and the single
funnel behind File ▸ Open, a dropped file and the open-as prompt. A **slice** is
an offset+length region of a parent file that acts as its own document; a
**bookmark** is a position plus a snapshot of the settings at creation time, with
no document of its own. Neither ever nests - both anchor to a whole file.

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
from celpix.core.context import KEY_SOURCE_OFFSET
from celpix.core.document import Document, ViewOptions
from celpix.core.errors import PipelineError, Stage
from celpix.pipeline import pipeline
from celpix.pipeline.pathway import PathwayConfig
from celpix.pipeline.pipeline import inspect_container
from celpix.plugins.base import NO_COMPRESSION, STAGE_DEFAULT_PRESET, FileRef
from celpix.plugins.detect import (
    content_kind_for,
    detect_container,
    tilemap_preset_for,
)
from celpix.project import projectfile
from celpix.project.workspace import (
    Entry,
    EntryKind,
    EntrySession,
    PaletteSource,
    SliceParams,
    composite_preset_id,
    missing_paths,
    new_composite,
    one_disk_scan,
    palette_source_for,
    path_is_palette_only,
    pixel_config_for,
    relocate_path,
    repair_presets,
    retarget_files,
    slice_of,
    tilemap_config_for,
)
from celpix.ui.composite_dialog import CompositeDialog, CompositeParams
from celpix.ui.container_dialog import ContainerDialog, ContainerEdit
from celpix.ui.container_info_dialog import ContainerInfoDialog
from celpix.ui.new_file_dialog import NewFileDialog, NewFileParams
from celpix.ui.slice_dialog import SliceDialog
from celpix.ui.undo_commands import (
    AddEntryCommand,
    CompositeEditCommand,
    ContainerEditCommand,
    PaletteConsumerLink,
    RemoveEntriesCommand,
    RemovePaletteWithConsumersCommand,
    SliceEditCommand,
)
from celpix.ui.widgets import (
    ask_save_path,
    clear_recent_projects,
    confirm_destructive,
    counted,
    forget_recent_project,
    load_recent_projects,
    remember_recent_project,
    show_in_file_manager,
)


class EntriesMixin:
    """Opening files, and the projects, slices and bookmarks over them.

    A slice of :class:`~celpix.ui.main_window.window.MainWindow`, not a
    standalone object: it reads and writes the window's own widgets and its
    single live ``_doc``. See the module docstring for what it owns, and the
    package docstring for why these are mixins.
    """

    # -- creating a file -----------------------------------------------------
    # What the save picker offers for each kind, when the container has no
    # extension of its own to suggest. A palette's ``.pal`` is the conventional
    # name for a file that is nothing but colours; the other two are raw payloads
    # with no convention at all, so ``.bin`` says exactly that.
    _NEW_FILTERS = {
        ContentKind.PIXELS: ("Binary files (*.bin);;All files (*)", ".bin"),
        ContentKind.TILEMAP: ("Binary files (*.bin);;All files (*)", ".bin"),
        ContentKind.PALETTE: ("Palette files (*.pal *.col);;All files (*)", ".pal"),
    }

    def _new_file(self) -> None:
        """File ▸ New File… — create a blank file on disk and open it as an entry.

        The one gesture that does not start from somebody else's bytes. It runs
        in the order the answers depend on each other: the dialog settles *what*
        is being made (content, container, codec, size), the save picker settles
        *where*, and only then is anything written — so a cancel at either step
        has left no file behind.

        The file is written before the entry exists because an entry is a
        reference to a path, and everything downstream of one — the load, a save,
        the project file — is written against a file that is there. The entry
        then carries exactly the answers the dialog gave rather than
        re-detecting them: detection reads a signature, and a blank payload has
        none to read.

        Undo removes the entry, as it does for an opened file. The file itself
        stays on disk — deleting a user's file is not something an undo of "add
        a row to a list" should do, and redo simply opens it again.
        """
        params = NewFileDialog.get_params(
            self,
            self._registry,
            pixel_preset_id=self._pixel_preset_id(),
            palette_preset_id=self._palette_preset_id(),
        )
        if params is None:
            return
        path = self._ask_new_file_path(params)
        if path is None:
            return
        # A file already open would be blanked under the entry editing it, which
        # no amount of undo puts back — the same refusal relocating onto an open
        # path makes, and for the same reason.
        if self._workspace.find_file(path) or self._workspace.find_palette(path):
            self._alert(
                f"{Path(path).name} is already open in this project. "
                "Close it first, or create the new file under another name.",
                title="celPix - new file",
            )
            return
        try:
            size = pipeline.create_file(
                path,
                kind=params.content_kind,
                container_id=params.container_id,
                codec_id=params.codec_id,
                units=params.units,
                reg=self._registry,
            )
        except PipelineError as exc:
            self._report(exc)
            return
        except OSError as exc:
            self._alert(f"Cannot write {path}: {exc}", title="celPix - new file")
            return
        entry = self._new_file_entry(path, params)
        self._push_command(AddEntryCommand(self, entry, f"new file {entry.name}"))
        self.statusBar().showMessage(
            f"Created {entry.name} - {self._new_file_extent(params)}, {size:,} bytes"
        )

    def _ask_new_file_path(self, params: NewFileParams) -> str | None:
        """Where the new file goes, defaulting to the container's own extension.

        A container that claims a suffix is claiming it for files of its format,
        and this is about to write one, so its first extension is the right
        default — that is also what lets the file be *re-detected* as that format
        when it is opened again from disk in another session.
        """
        info = self._registry.plugin(Stage.CONTAINER, params.container_id).info
        file_filter, suffix = self._NEW_FILTERS[params.content_kind]
        if info.extensions:
            suffix = info.extensions[0]
            file_filter = f"{info.name} (*{suffix});;All files (*)"
        return ask_save_path(
            self,
            "New file",
            str(Path(self._export_dir(self._workspace.current)) / f"untitled{suffix}"),
            file_filter,
            suffix,
        )

    def _new_file_entry(self, path: str, params: NewFileParams) -> Entry:
        """The workspace entry for a file just created with ``params``.

        Every answer the dialog gave is stamped on rather than re-derived: the
        codec goes on the session (or, for a map, on the entry's own cell format
        and, for a palette file, on its recorded import format), and the size
        goes on the pending view so the sheet opens at the shape it was asked
        for instead of at the window's default 16x16.
        """
        name = Path(path).name
        if params.content_kind is ContentKind.PALETTE:
            # A palette entry is registered, never activated: it has no session
            # and no view of its own, and the codec it was written with is the
            # one it must be read back with (``docs/design/project-format.md`` §4).
            return Entry(
                name=name,
                kind=EntryKind.PALETTE,
                path=path,
                container_id=params.container_id,
                palette_preset_id=params.codec_id,
            )
        tilemap = params.content_kind is ContentKind.TILEMAP
        entry = Entry(
            name=name,
            kind=EntryKind.FILE,
            path=path,
            container_id=params.container_id,
            content_kind=params.content_kind,
            tilemap_preset_id=params.codec_id if tilemap else None,
        )
        entry.session = EntrySession(
            pixel_preset_id=(self._pixel_preset_id() if tilemap else params.codec_id),
            palette_preset_id=self._palette_preset_id(),
        )
        entry.pending_view = ViewOptions(columns=params.columns, rows=params.rows)
        return entry

    @staticmethod
    def _new_file_extent(params: NewFileParams) -> str:
        """The size as the dialog stated it, for the status line."""
        if params.content_kind is ContentKind.PALETTE:
            return f"{params.units} colors"
        noun = "cells" if params.content_kind is ContentKind.TILEMAP else "tiles"
        return f"{params.columns}x{params.rows} {noun}"

    # -- opening a file ------------------------------------------------------
    def _open_pixel(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Open pixel data")
        if path:
            self._load_pixel(path, content_kind=ContentKind.PIXELS)

    def _open_tilemap(self) -> None:
        """File ▸ Open tilemap data — read any file as a map of tile indices.

        The tilemap twin of Open pixel data, and forcing in the same way: a file
        whose signature says nothing (a region of a ROM, a raw dump) has no way
        to be recognised as a map, so asking for one is how it is said. A file
        that *is* a known tilemap format opens the same either way.
        """
        path, _ = QFileDialog.getOpenFileName(self, "Open tilemap data")
        if path:
            self._load_pixel(path, content_kind=ContentKind.TILEMAP)

    def _open_as_chosen(self, path: str) -> None:
        """Ask what ``path`` holds, then open it that way — the Ctrl-drop gesture.

        Detection is a guess from a signature and a suffix, and it is silent
        about being one. Holding Ctrl is how the user says they know better,
        without having to find the matching menu entry for a file they are
        already dropping.
        """
        kind = self._ask_content_kind(path)
        if kind is None:
            return
        if kind is ContentKind.PALETTE:
            self._open_palette_data(path)
        else:
            self._load_pixel(path, content_kind=kind)

    def _ask_content_kind(self, path: str) -> ContentKind | None:
        """Which of the three readings to open ``path`` as, or None if cancelled."""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("celPix - open as")
        box.setText(f"Open {Path(path).name} as:")
        box.setInformativeText(
            "Pixels are tile graphics, a palette is colors, and a tilemap is\n"
            "indices into tiles that live somewhere else."
        )
        role = QMessageBox.ButtonRole.ActionRole
        buttons = {
            box.addButton("&Pixels", role): ContentKind.PIXELS,
            box.addButton("Pa&lette", role): ContentKind.PALETTE,
            box.addButton("&Tilemap", role): ContentKind.TILEMAP,
        }
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        return buttons.get(box.clickedButton())

    def _load_pixel(
        self, path: str, *, content_kind: ContentKind | None = None
    ) -> None:
        """Open ``path`` as a workspace entry and switch the view to it.

        The shared entry point for both File ▸ Open and drag-and-drop, so a
        dropped file behaves exactly like an opened one. A file that is
        already open activates its existing entry - identity is the path -
        so only a genuinely new entry becomes an undoable step.

        The container is picked here, once, from the file's name and leading
        bytes: it is a property of the file, so detecting it at open time means
        every later load reads through the same one, and the answer is on the
        entry where the user can see and change it.

        ``content_kind`` overrides what the container implies, for the gestures
        that *say* what a file is — File ▸ Open pixel/tilemap data, and the
        open-as prompt. Detection can only recognise a format it knows, so a raw
        region of a ROM has no way to announce itself as a map; asking is how
        that is said. ``None`` keeps the container's own answer.
        """
        existing = self._workspace.find_file(path)
        if existing is not None:
            self._activate_entry(existing)
            return
        container_id = detect_container(self._registry, path)
        # Follows from the container, which was itself chosen from the file's
        # signature — so a screen or panel file opens into the Tilemaps section
        # without being asked about — unless the caller said otherwise.
        detected = content_kind_for(self._registry, container_id)
        entry = Entry(
            name=Path(path).name,
            kind=EntryKind.FILE,
            path=path,
            container_id=container_id,
            content_kind=content_kind or detected,
            tilemap_preset_id=tilemap_preset_for(self._registry, container_id) or None,
        )
        self._push_command(AddEntryCommand(self, entry, f"open {entry.name}"))

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
        # The replace above titled the window from the restored entry (no project
        # path was set yet); now that one is, retitle to name the project file.
        self._refresh_window_title()
        # An upgrade is worth a line but not a dialog: nothing was lost, and the
        # only thing the user can act on is that the next save rewrites the file
        # at the new version - which is what makes it worth saying at all.
        upgraded = (
            ""
            if loaded.migrated_from is None
            else f" Upgraded from format version {loaded.migrated_from}"
            f" - saving writes version {projectfile.PROJECT_VERSION}."
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

        The **palette row** and the **arrangement** (the Pattern picker's
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
        # Only the palette row and the arrangement: the rest of the geometry
        # is left at the defaults a fresh slice gets anyway, since the parent's
        # window size describes a different region than the one being carved
        # out. (Columns is the exception the bitmap width owns - it is
        # re-derived from the width on the render path.)
        view = parent.doc.view if parent.doc is not None else parent.pending_view
        if view is not None:
            slice_entry.pending_view = ViewOptions(
                palette_row=view.palette_row,
                block_columns=view.block_columns,
                block_rows=view.block_rows,
                block_order=view.block_order,
                two_dimensional=view.two_dimensional,
                bitmap_width=view.bitmap_width,
            )

    def _slice_prefill_offset(self, position: int | None = None) -> int:
        """A view position in the coordinates a slice offset is written in.

        The parent's own coordinates (:meth:`_anchor_base`), which is what a slice
        addresses: past whatever the container skipped for a whole file, 0-based in
        the reordered buffer under a reshape. Deliberately *not* the number the
        offset box shows while a slice is on screen - that counts from the slice's
        own first byte, while a slice carved here is anchored in the file the
        parent lineage reads. Taking the *config's* requested offset instead would
        prefill a headered file's slices short by its header - the config never
        names the start the container worked out for itself.

        ``position`` defaults to the grid's current byte position; the selection
        and structure gestures pass their own document-relative start.
        """
        assert self._doc is not None
        at = self._byte_position() if position is None else position
        return self._anchor_base() + at

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

    def _new_composite(self) -> None:
        """File ▸ New Composite View…: assemble a tile source out of open entries.

        The tile window a converted console tilemap actually indexes: one flat
        index space filled from several files, which is what the hardware had in
        VRAM and what no single file holds (``docs/design/composite-entry.md``).

        Created through an ``AddEntryCommand`` like every other interactive add,
        so it can be undone — and so the *same object* comes back on redo, which
        every piece of every other composite and every tilemap binding names.
        """
        entry = new_composite("")
        params = CompositeDialog.get_composite(
            self,
            entry=entry,
            candidates=list(self._workspace.entries),
            tile_bytes=self._composite_tile_bytes(entry),
            name=self._unused_composite_name(),
        )
        if params is None:
            return
        entry.name = params.name
        entry.pieces = params.pieces
        self._push_command(
            AddEntryCommand(self, entry, f'new composite "{entry.name}"')
        )

    def _unused_composite_name(self) -> str:
        """``Composite``, ``Composite 2``, … — the first the list has not got.

        A composite has no file to be named after, so it needs one made up; two
        rows called the same thing in the same section is the confusion this
        avoids. Only a starting point — the user renames it in the dialog or in
        the list, and nothing keeps the numbering true afterwards.
        """
        taken = {e.name for e in self._workspace.entries}
        if "Composite" not in taken:
            return "Composite"
        return next(
            f"Composite {n}"
            for n in range(2, len(taken) + 3)
            if f"Composite {n}" not in taken
        )

    def _composite_tile_bytes(self, entry: Entry) -> int:
        """One tile's size in the format ``entry`` is *read* at.

        The dialog states each run's position twice — as a byte and as a tile —
        and this is what converts between them. The entry's **own** format, not
        its first source's: those differ exactly where the feature is most used,
        since a tile window assembled from 4bpp banks is routinely read at 2bpp,
        and taking the source's would print a tile column off by a factor of two
        against the view the user is checking it against.

        Falls back to the seed for an entry with no session yet, which is a
        composite being created — it has no sources to disagree with either. A
        format this build hasn't got costs the reader that one column rather than
        the dialog.
        """
        session = entry.session
        preset = (
            session.pixel_preset_id
            if session is not None
            else composite_preset_id(entry, self._registry)
        )
        try:
            return pipeline.pixel_tile_bytes(preset, self._registry)
        except PipelineError:
            return 0

    def _edit_composite(self, entry: Entry) -> None:
        """The files dock's Edit… on a composite — re-list its pieces in place.

        No unsaved-changes warning, unlike :meth:`_edit_slice`: a composite holds
        no edits of its own to discard. Anything painted through it is already in
        the pieces, and stays there however the list is rearranged.
        """
        if entry.kind is not EntryKind.COMPOSITE:
            return
        before = CompositeParams(entry.name, entry.pieces)
        params = CompositeDialog.get_composite(
            self,
            entry=entry,
            candidates=list(self._workspace.entries),
            tile_bytes=self._composite_tile_bytes(entry),
            name=entry.name,
            pieces=entry.pieces,
            title="Edit Composite View",
        )
        if params is None or params == before:
            return  # cancelled, or OK'd unchanged - nothing to undo
        self._push_command(
            CompositeEditCommand(self, entry, before=before, after=params)
        )

    def _apply_composite_params(self, entry: Entry, params: CompositeParams) -> None:
        """Re-list a composite's pieces and re-assemble — the application path
        for composite edits and their undos.

        The re-assembly is a plain re-read, and the maps drawing through the
        composite are re-resolved after it: their tiles came out of the join that
        has just changed shape, so leaving them would have them drawing the old
        one indefinitely (:meth:`~...session.SessionMixin._reassemble_composites`).
        """
        entry.name = params.name
        entry.pieces = params.pieces
        if entry is self._workspace.current:
            self._capture_session()
        self._reassemble_composites([entry])
        self._reresolve_bound_art(self._maps_drawing_from([entry]))
        self._files_panel.refresh_entry(entry)

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
        codec_id = self._entry_codec_id(entry)
        edit = ContainerDialog.edit_container(
            self,
            self._registry,
            paths=entry.paths,
            container_id=entry.container_id,
            reshape_id=entry.reshape_id,
            kind=entry.content_kind,
            codec_id=codec_id,
            units=self._entry_units(entry, codec_id),
        )
        if edit is None:
            return
        moved = edit.paths != entry.paths
        if (
            not moved
            and edit.units is None
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
        # it, so their edits are on the table too. A resize is the same story
        # told about the file rather than the reading of it, so it joins the gate.
        family = [entry, *self._workspace.children_of(entry)] if moved else [entry]
        if not self._confirm_container_discard(family):
            return
        # Before the command, and outside it: this one writes the file, and a
        # truncated tail is not something an undo can put back — so it is settled
        # while there is still a Cancel, and the undo stack is told nothing about
        # it. A refusal here calls the whole edit off rather than applying half
        # of what the dialog was left holding.
        if edit.units is not None and not self._resize_entry_file(
            entry, edit, codec_id
        ):
            return
        before = ContainerEdit(entry.container_id, entry.paths, entry.reshape_id)
        # The size is dropped on the way in: it is already on disk, and a command
        # holding it would offer a redo of a write that has no undo.
        self._push_command(
            ContainerEditCommand(
                self, entry, before=before, after=replace(edit, units=None)
            )
        )

    # -- resizing a file -----------------------------------------------------
    def _entry_codec_id(self, entry: Entry) -> str:
        """The format ``entry``'s own bytes are read through — what measures it.

        A size row counts tiles, cells or colours, and only the codec knows what
        one of those costs, so the size question cannot be asked without this.
        Each content kind keeps it in its own place
        (``docs/design/project-format.md`` §4), and an entry that has never been
        activated has no session to keep it in at all — hence the stage's own
        default, which is what a fresh sheet opens on.
        """
        kind = entry.content_kind
        if kind is ContentKind.PALETTE:
            return entry.palette_preset_id or self._palette_import_preset_id()
        if kind is ContentKind.TILEMAP:
            return (
                entry.tilemap_preset_id or STAGE_DEFAULT_PRESET[Stage.INTERPRET_TILEMAP]
            )
        if entry.session is not None:
            return entry.session.pixel_preset_id
        return STAGE_DEFAULT_PRESET[Stage.INTERPRET_PIXEL]

    def _resize_config(
        self, entry: Entry, edit: ContainerEdit, codec_id: str
    ) -> PathwayConfig:
        """The pathway a resize of ``entry`` reads and writes its file through.

        Built from the **edit** rather than from the entry as it stands: the
        container and reshape the dialog was left holding are the ones the file
        is about to be read through, so they are the ones the resized bytes have
        to go back out through. Framing a payload with the old container and
        re-reading it with the new one would not produce the region the size row
        was describing.

        Through the same three builders a load uses, on an entry copied with the
        edit applied, rather than a config assembled here: they are what decide
        whether a stage can write at all — a plugin this build hasn't got leaves
        the pathway view-only — and a second answer to that question is exactly
        the kind that goes quietly out of date.
        """
        edited = replace(
            entry,
            path=edit.paths[0],
            extra_paths=tuple(edit.paths[1:]),
            container_id=edit.container_id,
            reshape_id=edit.reshape_id,
        )
        if entry.content_kind is ContentKind.PALETTE:
            return self._file_palette_config(
                edited.path, 0, codec_id, edited.container_id
            )
        if entry.content_kind is ContentKind.TILEMAP:
            return tilemap_config_for(edited, codec_id, self._registry)
        return pixel_config_for(edited, codec_id, self._registry)

    def _entry_units(self, entry: Entry, codec_id: str) -> int:
        """How many tiles, cells or colours ``entry``'s region holds right now.

        Read rather than remembered: only the container knows how much of the
        file is payload, and the entry may never have been loaded. A region that
        cannot be measured — an unreadable file, a codec that refuses the preset
        — comes back 0, and the dialog says so in place of a size rather than
        failing to open over a row that is not why it was reached for.
        """
        if not codec_id:
            return 0
        before = ContainerEdit(entry.container_id, entry.paths, entry.reshape_id)
        try:
            data, _ = pipeline.read_region(
                self._resize_config(entry, before, codec_id), self._registry
            )
            return pipeline.blank_units(
                entry.content_kind, codec_id, len(data), self._registry
            )
        except (PipelineError, OSError):
            return 0

    def _resize_entry_file(
        self, entry: Entry, edit: ContainerEdit, codec_id: str
    ) -> bool:
        """Resize ``entry``'s file to ``edit.units``; False if it did not happen.

        The one gesture in this dialog that changes the file rather than the
        reading of it, so it is also the one that has to ask: shrinking drops the
        tail, and no undo puts those bytes back (that is why this runs outside
        the command). Growing needs no question — it adds zeroes past everything
        that was there.

        A false answer calls the *whole* edit off, container and file list
        included: the user cancelled at a prompt about this dialog, and applying
        the half they did not cancel would be a change they never confirmed.
        """
        assert edit.units is not None
        cfg = self._resize_config(entry, edit, codec_id)
        try:
            current, ctx = pipeline.read_region(cfg, self._registry)
            before = len(current)
            after = pipeline.blank_size(
                entry.content_kind, codec_id, edit.units, self._registry
            )
        except PipelineError as exc:
            self._report(exc)
            return False
        except OSError as exc:
            self._alert(f"Cannot read {entry.path}: {exc}", title="celPix - resize")
            return False
        # Where the region starts in the file, which is the container's answer and
        # nobody else's — a slice's offset is file-absolute, so the region's new
        # end has to be put back into those coordinates before the two compare.
        base = int(ctx.get(KEY_SOURCE_OFFSET, 0) or 0)
        if after < before and not self._confirm_shrink(
            entry, before - after, base + after
        ):
            return False
        try:
            pipeline.resize_file(
                cfg,
                kind=entry.content_kind,
                codec_id=codec_id,
                units=edit.units,
                reg=self._registry,
            )
        except PipelineError as exc:
            self._report(exc)
            return False
        except (OSError, ValueError) as exc:
            self._alert(f"Cannot resize {entry.path}: {exc}", title="celPix - resize")
            return False
        self.statusBar().showMessage(
            f"Resized {entry.name} - {before:,} bytes to {after:,}"
        )
        return True

    def _confirm_shrink(self, entry: Entry, dropped: int, end: int) -> bool:
        """Ask before truncating ``entry``'s file; True to go ahead.

        Names the children that will not survive it as well as the byte count. A
        slice or bookmark is an offset into this file, and one anchored at or past
        ``end`` — the region's new last byte — has nothing left to read. That is
        the part of a shrink that costs more than the bytes, and it is the part
        the size row cannot show.
        """
        message = f"Shrinking {entry.name} drops {dropped:,} bytes from the end."
        orphaned = [
            child
            for child in self._workspace.children_of(entry)
            if child.slice_offset >= end
        ]
        if orphaned:
            names = ", ".join(child.name for child in orphaned)
            message += (
                f"\n\n{counted(len(orphaned), 'entry')} starting past the new "
                f"end will no longer load: {names}."
            )
        message += "\n\nThis cannot be undone."
        return self._confirm(
            message, title="celPix - resize", accept="Shrink", warn=True
        )

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

        def write() -> bool:
            for entry in dirty:
                self._write_entry_checked(entry)
            # A failed write must not proceed — its edits would go with the re-read.
            return not any(e.pixel_dirty for e in dirty)

        return confirm_destructive(
            self,
            "celPix - unsaved changes",
            f"{names} {'have' if len(dirty) > 1 else 'has'} unsaved edits. "
            "Applying this re-reads the bytes and discards them. Write them first?",
            "Write",
            "Discard",
            write,
            default_safe=True,  # least-lossy choice when Enter is hit blind
        )

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
            # geometry (columns/rows/palette row/arrangement) with the origin
            # zeroed, since the jump lands it byte-exactly itself. Not the zoom:
            # it is app-wide, so a jump leaves it where the user is standing
            # rather than pulling them back to where they were when they marked
            # the spot (:class:`~celpix.core.document.ViewOptions`).
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
            and current.kind.has_document
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
    def _remove_entry(self, entry: Entry, *, confirm: bool = True) -> None:
        """Remove one entry — :meth:`_remove_entries` for a list of one."""
        self._remove_entries([entry], confirm=confirm)

    def _remove_entries(self, entries: list[Entry], *, confirm: bool = True) -> None:
        """Remove every entry in ``entries`` (a file takes its slices and
        bookmarks with it), confirming once for the lot - Remove is also on the
        Delete key, and a slip there costs each entry's whole session setup.

        ``confirm=False`` is **Cut**, which has already said where the row is
        going: it is on the clipboard before this runs, so the question the
        prompt asks has an answer the gesture itself gave. A palette that graphics
        render still asks either way — re-homing them is a change to *those*
        entries, which no clipboard holds.

        Several rows are removed as a **macro** over the one-entry commands rather
        than by one command that knows about lists. Each is built and pushed in
        turn, so each captures the list positions it is actually removing from,
        and undo unwinds them in reverse and puts every row back where it was.
        A palette in the set keeps its own command, which is what carries the
        re-homing; the macro is the only thing that has to know they belong
        together.
        """
        roots = self._removal_roots(entries)
        if not roots:
            return
        # The current graphic's palette mode is only written to its session on a
        # switch, so snapshot it first - otherwise a palette in use *right now*
        # looks unused and would be dropped without re-homing it.
        if any(root.kind is EntryKind.PALETTE for root in roots):
            self._capture_session()
        going = {
            e for root in roots for e in (root, *self._workspace.children_of(root))
        }
        # Only the graphics that are *staying* need re-homing: one being removed
        # in the same gesture would be re-pointed at a custom palette on its way
        # out of the project.
        rehomed: dict[Entry, list[Entry]] = {}
        for root in roots:
            if root.kind is not EntryKind.PALETTE:
                continue
            staying = [
                c for c in self._workspace.palette_consumers(root) if c not in going
            ]
            if staying:
                rehomed[root] = staying
        if not self._confirm_removal(roots, rehomed, confirm=confirm):
            return
        if len(roots) == 1:
            self._push_removal(roots[0], rehomed.get(roots[0]))
            return
        self._undo_stack.beginMacro(f"remove {len(roots)} entries")
        try:
            for root in roots:
                self._push_removal(root, rehomed.get(root))
        finally:
            self._undo_stack.endMacro()

    def _removal_roots(self, entries: list[Entry]) -> list[Entry]:
        """``entries`` in list order, minus every row a selected *parent* already
        takes with it — a file picked along with two of its own slices is one
        removal, not three.
        """
        chosen = set(entries)
        return [
            entry
            for entry in self._workspace.entries
            if entry in chosen and self._workspace.parent_of(entry) not in chosen
        ]

    def _confirm_removal(
        self,
        roots: list[Entry],
        rehomed: dict[Entry, list[Entry]],
        *,
        confirm: bool,
    ) -> bool:
        """Ask before removing ``roots``; True to go ahead.

        One prompt however many rows are going, naming what travels with them:
        the slices and bookmarks a file takes, the unsaved edits that are
        discarded, and the graphics a palette leaves needing colors of their own.

        A palette with consumers is asked about **even when ``confirm`` is
        False**: the caller that skips the question is Cut, and re-homing a
        graphic is a change to that graphic, which the clipboard is not holding.
        """
        victims = [(root, self._workspace.children_of(root)) for root in roots]
        if len(roots) == 1:
            root = roots[0]
            message = f"Remove {root.name}?"
        else:
            names = ", ".join(root.name for root in roots)
            message = f"Remove {len(roots)} entries ({names})?"
        parts = []
        counts = [
            f"{n} {label}(s)"
            for label, n in (
                ("slice", self._kind_count(victims, EntryKind.SLICE)),
                ("bookmark", self._kind_count(victims, EntryKind.BOOKMARK)),
            )
            if n
        ]
        if counts:
            whose = "its " if len(roots) == 1 else ""
            parts.append(f"removes {whose}{' and '.join(counts)}")
        dirty = [
            e.name
            for root, children in victims
            for e in (root, *children)
            if e.pixel_dirty or e.palette_dirty
        ]
        if dirty:
            parts.append(f"discards unsaved changes ({', '.join(dirty)})")
        users = sorted({c.name for consumers in rehomed.values() for c in consumers})
        if users:
            parts.append(
                f"leaves {len(users)} graphic(s) ({', '.join(users)}) keeping "
                "these colors as their own custom palette, stored in the project"
            )
        if parts:
            message += " This also " + " and ".join(parts) + "."
        if not confirm and not rehomed:
            return True
        answer = QMessageBox.question(self, "celPix - remove", message)
        return answer == QMessageBox.StandardButton.Yes

    @staticmethod
    def _kind_count(victims: list[tuple[Entry, list[Entry]]], kind: EntryKind) -> int:
        """How many rows of ``kind`` come along as *children* of what is going."""
        return sum(e.kind is kind for _root, children in victims for e in children)

    def _push_removal(self, entry: Entry, rehomed: list[Entry] | None) -> None:
        """Push the command that takes ``entry`` out — no questions asked.

        ``rehomed`` is the graphics still rendering it, for a file palette; each
        keeps its colors as a Custom copy so none is left showing a palette that
        is gone, and the whole thing is one undo step.
        """
        if rehomed:
            self._push_command(
                RemovePaletteWithConsumersCommand(
                    self,
                    entry,
                    index=self._workspace.entries.index(entry),
                    consumers=[self._consumer_link(entry, c) for c in rehomed],
                )
            )
            return
        victims = [entry, *self._workspace.children_of(entry)]
        entries = self._workspace.entries
        self._push_command(
            RemoveEntriesCommand(
                self,
                entry,
                victims=[(entries.index(e), e) for e in victims],
                was_current=self._workspace.current,
            )
        )

    def _consumer_link(self, palette: Entry, consumer: Entry) -> PaletteConsumerLink:
        """``consumer``'s File-mode link to ``palette``, captured before the
        re-home so undo can point it back at the palette it had."""
        src = palette_source_for(consumer)
        return PaletteConsumerLink(
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
