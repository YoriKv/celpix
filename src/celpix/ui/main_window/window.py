"""The application main window: open pixel/palette data, view it, save it back.

Menus (File, Edit, View, Navigate, Palette, Panels), a control strip (pixel
format, columns, rows, zoom, subpalette row - the palette format lives in the
palette dock) plus an arrangement
strip (Pattern presets, block grouping, fill order, 2D), a scrollable
:class:`~celpix.ui.canvas.Canvas` showing a windowed view with tile-range
selection, a navigation bar with the address/bank readout, and docks for the
files list, palette, and an optional hex view. Undo/redo spans one session-wide
history, and a compression scan/preview overlays decodable structures.

It drives the Qt-free pipeline through the plugin registry and never interprets
bytes itself; all decode/encode goes through ``pipeline``.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path

from PySide6.QtCore import QSettings, Qt, QUrl
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QDesktopServices,
    QImage,
    QKeySequence,
    QPalette,
    QUndoCommand,
    QUndoStack,
)
from PySide6.QtWidgets import (
    QApplication,
    QDockWidget,
    QFileDialog,
    QHBoxLayout,
    QMainWindow,
    QMessageBox,
    QScrollArea,
    QScrollBar,
    QVBoxLayout,
    QWidget,
)

from celpix.core.arrangement import (
    BlockLayout,
)
from celpix.core.document import Document, ViewOptions
from celpix.core.errors import PipelineError
from celpix.core.palette import Palette
from celpix.pipeline import pipeline
from celpix.plugins.base import NO_DECOMPRESS
from celpix.plugins.discovery import PluginLoadIssue
from celpix.plugins.registry import Registry, default_registry
from celpix.project import projectfile
from celpix.project.workspace import (
    Entry,
    EntryKind,
    EntrySession,
    PaletteMode,
    Workspace,
    backfill_slice_length,
    data_missing,
    palette_source_for,
)
from celpix.ui import render_bridge
from celpix.ui.canvas import CANVAS_BACKGROUND, Canvas, GridStyle
from celpix.ui.color_editor import ColorEditorDialog
from celpix.ui.decompress_overlay import DecompressOverlay
from celpix.ui.file_list_panel import FileListPanel
from celpix.ui.hex_view_panel import BYTES_PER_ROW, HexViewPanel
from celpix.ui.main_window.color_editing import ColorEditingMixin
from celpix.ui.main_window.compression import CompressionMixin
from celpix.ui.main_window.entries import EntriesMixin
from celpix.ui.main_window.interpretation import InterpretationMixin
from celpix.ui.main_window.navigation import NavigationMixin
from celpix.ui.main_window.palette_dock import PaletteDockMixin
from celpix.ui.main_window.palette_source import (
    _DEFAULT_SESSION_PALETTE_FORMAT,
    PaletteSourceMixin,
)
from celpix.ui.main_window.pixel_edit import PixelEditMixin
from celpix.ui.main_window.selection import (
    SelectionMixin,
)
from celpix.ui.main_window.transfer import TransferMixin
from celpix.ui.main_window.transform import TransformMixin
from celpix.ui.tools import EditMode
from celpix.ui.undo_commands import (
    AddEntryCommand,
    PaletteUserLink,
    RemoveEntriesCommand,
    RemovePaletteWithUsersCommand,
    RenameEntryCommand,
)
from celpix.ui.widgets import (
    load_enum_setting,
    select_combo_data,
    signals_blocked,
)

# Rebuilds a registry from built-ins + the current plugin folder, returning it
# with any load issues. Injected by the app so the window can hot-reload plugins
# without knowing about data dirs, the trust store, or the confirm dialog.
ReloadPlugins = Callable[[], "tuple[Registry, list[PluginLoadIssue]]"]

# QSettings key for the app-wide grid style (an appearance preference shared by
# every view, unlike the per-document Grid toggle).
GRID_STYLE_KEY = "view/grid_style"


class MainWindow(
    NavigationMixin,
    InterpretationMixin,
    PaletteSourceMixin,
    PaletteDockMixin,
    ColorEditingMixin,
    SelectionMixin,
    TransformMixin,
    PixelEditMixin,
    EntriesMixin,
    TransferMixin,
    CompressionMixin,
    QMainWindow,
):
    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        registry: Registry | None = None,
        plugin_dir: str | None = None,
        plugin_issues: list[PluginLoadIssue] | None = None,
        reload_plugins: ReloadPlugins | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Celpix")
        self.resize(1024, 768)
        self.setAcceptDrops(True)  # drop a file on the window to open it as pixels

        # The app bootstrap builds a registry (built-ins + user plugins) and passes
        # it in; standalone construction (e.g. tests) falls back to built-ins only.
        self._registry = registry if registry is not None else default_registry()
        self._plugin_dir = plugin_dir
        self._plugin_issues = plugin_issues or []
        self._reload_plugins = reload_plugins
        # The open files/slices. self._doc is always the *current* entry's
        # document (or None with nothing open) - the single-active-view model:
        # switching entries swaps the document under the one canvas.
        self._workspace = Workspace()
        self._doc: Document | None = None
        # The unified per-launch undo history: one chronological stack for
        # every surface - files-pane structure, per-document config, view
        # moves, pixel/color edits (docs/design/undo-redo.md).
        # Document-scoped commands re-activate their entry before applying.
        self._undo_stack = QUndoStack(self)
        # Every structural change (open/close/rename/new slice/…) arrives as a
        # command, so one signal covers all of them - and covers undoing back
        # onto the saved state, which clears the marker again. Connected as a
        # bound method, not a lambda: Qt then drops the connection with the
        # window, instead of firing into a half-destroyed one as the stack
        # unwinds during teardown.
        self._undo_stack.indexChanged.connect(self._on_undo_index_changed)
        # True while a command's undo/redo is applying state - push sites bail
        # on it, so an apply can never cascade into pushing a second command.
        self._applying_undo = False
        # The .celpix file this session was loaded from / last saved to, so
        # File ▸ Save Project can rewrite it without re-asking for a path.
        self._project_path: str | None = None
        # The project document as last written to (or read from) that file.
        # "Has unsaved changes" is the live workspace re-serialized and compared
        # against this, rather than a flag some gesture might forget to set: the
        # comparison can't drift, and it reads clean again when a change is
        # undone back to what is on disk. None while no project is open.
        self._saved_project: dict[str, object] | None = None
        # Where the palette comes from (:class:`PaletteMode`). The dock's mode
        # dropdown is a view of this member, and the mode's own properties -
        # is_real / has_source / decodes_raw_bytes / has_external_file /
        # is_exportable - are what the window branches on, rather than
        # re-listing which modes mean what at each site.
        self._palette_mode = PaletteMode.DEFAULT
        # The session's default palette color format, inherited by a Custom
        # palette forked off the generated default (which has no format of its
        # own). Starts RGB888 and follows the last format actually chosen - an
        # import/re-decode, the format dropdown, or a future ROM file hint - via
        # _set_session_palette_format. Global and session-lifetime: it survives
        # entry switches and is not part of any entry's saved session.
        self._session_palette_format = _DEFAULT_SESSION_PALETTE_FORMAT
        # The shared color editor, while open (None otherwise). One non-modal
        # dialog is reused and retargeted as the palette selection moves, so the
        # eyedropper can reach the canvas and the swatch grid underneath it.
        self._color_editor: ColorEditorDialog | None = None
        # Top-left tile index of the view window. The scroll area no longer scrolls
        # the whole file; this offset does, and only the window is composed/rendered.
        self._offset = 0
        # Sub-tile byte shift of the whole tile grid (0 <= nudge < bytes_per_tile),
        # for aligning graphics that don't start on a tile boundary. Byte steps
        # (+B/−B) move it; tile/row/page steps leave it alone.
        self._nudge = 0
        # Scratch byte position for cycling pixel formats to eyeball which one
        # renders. Captured (in byte space) on the first switch of a run and
        # reused on every consecutive switch, so a format whose huge tiles force
        # the offset back to page 0 (e.g. whole-bank) can't drag the position
        # down and strand later switches there. Cleared when the pixel dropdown
        # loses focus (see _on_pixel_preset_change / the focus_lost hookup).
        self._pixel_switch_target: int | None = None
        # The selection as absolute tile indices (they survive scrolling; the
        # canvas only paints the highlight while it is inside the window).
        # ``_selected_tile`` is the anchor - the single "selected tile" every
        # one-tile consumer (palette-from-selection, paste, the session) reads -
        # and ``_selected_last`` the highest tile in it, so the bounding run is
        # always ``_selected_tile .. _selected_last``.
        self._selected_tile: int | None = None
        self._selected_last: int | None = None
        # Rectangle selections additionally carry their cell extent and the exact
        # tiles those cells resolved to under the view they were made in; None
        # means the selection is a plain linear run. The tiles are cached rather
        # than recomputed on demand because they are the *record* of what was
        # selected: when a view/arrangement change would resolve the same
        # rectangle to different tiles, the selection collapses to its top-left
        # tile instead of silently sliding onto other data (_refresh_selection).
        self._rect_cells: tuple[int, int] | None = None
        self._rect_tiles: tuple[int, ...] = ()
        # Compression navigation: byte position right after the structure in
        # view (the Jump-to-Next target, None = end unknown/invalid), and the
        # scan interlock (the Scan button doubles as Stop while one runs).
        self._next_structure: int | None = None
        # The complete structure in view as (start byte position, byte extent)
        # - the promote-to-slice source. Kept separately from _next_structure,
        # which is deliberately None when the structure ends at end-of-file
        # (nowhere to jump) even though promoting it is still valid.
        self._structure_extent: tuple[int, int] | None = None
        self._scanning = False
        self._scan_stop = False

        # Pixel-edit state (mode, tool, pen, stroke/float scratch) must exist
        # before the transform toolbar builds its mode toggle off _edit_mode.
        self._init_pixel_edit()

        self._canvas = Canvas()
        self._overlay = DecompressOverlay(self)
        self._canvas.tiles_selected.connect(self._on_tiles_selected)
        self._canvas.color_picked.connect(self._on_color_picked)
        self._connect_pixel_canvas()
        # Right-click the canvas for the clipboard actions (the canvas selects
        # the tile under the cursor first, unless it is already in the run).
        self._canvas.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._canvas.customContextMenuRequested.connect(self._show_canvas_menu)
        # ClickFocus so clicking the view takes focus off any dropdown/spin box (which
        # would otherwise keep the arrow keys), letting navigation resume. Navigation
        # itself is window-wide via eventFilter, not tied to canvas focus.
        self._canvas.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        scroll = QScrollArea()
        scroll.setWidget(self._canvas)
        # Pin the (small) window to the top-left; the scroll area only scrolls now
        # when zoom makes the window itself larger than the viewport.
        scroll.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        # The neutral surround around the rendered pixels - the same color the
        # canvas paints over any past-end tiles, so surround and backing meet
        # seamlessly.
        viewport = scroll.viewport()
        viewport_palette = viewport.palette()
        viewport_palette.setColor(QPalette.ColorRole.Window, CANVAS_BACKGROUND)
        viewport.setPalette(viewport_palette)
        viewport.setAutoFillBackground(True)

        # A file-position scrollbar: its range spans the whole file (in tiles), so
        # dragging jumps far through a large file at once. It drives the same offset
        # the buttons/keys do; _sync_nav keeps it in step (with signals blocked).
        # It sits to the LEFT of the canvas and is styled as an accent-colored rail
        # so it reads as a file navigator, not one of the canvas's own scrollbars.
        self._offset_bar = QScrollBar(Qt.Orientation.Vertical)
        self._offset_bar.setToolTip("File position - drag to jump")
        self._offset_bar.setStyleSheet(self._offset_bar_style())
        self._offset_bar.valueChanged.connect(self._set_offset)

        # The transform toolbar sits directly on top of the canvas (not docked at
        # the window top like the interpretation bars), so it reads as part of the
        # editing surface. It spans the canvas column, right of the offset rail.
        self._transform_toolbar = self._build_transform_toolbar()
        canvas_column = QVBoxLayout()
        canvas_column.setContentsMargins(0, 0, 0, 0)
        canvas_column.setSpacing(0)
        canvas_column.addWidget(self._transform_toolbar)
        canvas_column.addWidget(scroll, 1)

        view_row = QHBoxLayout()
        view_row.setContentsMargins(0, 0, 0, 0)
        view_row.setSpacing(0)
        view_row.addWidget(self._offset_bar)
        view_row.addLayout(canvas_column, 1)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addLayout(view_row, 1)
        layout.addWidget(self._build_navbar())
        self.setCentralWidget(central)

        self._build_files_dock()  # before _build_menus: the toggles go in menus
        self._build_palette_dock()
        self._build_tools_dock()  # after palette dock: it stacks below it
        self._build_hex_dock()
        self._build_clipboard_actions()  # before _build_menus: shared with it
        self._build_menus()
        self._build_toolbar()
        # After _build_toolbar: the spin exists only then. setValue clamps to the
        # spin's range and re-renders through _on_view_change.
        self._palette_panel.subpalette_clicked.connect(self._subpalette.setValue)
        self._build_nav_keys()
        self._sync_nav()
        # Navigation keys are handled window-wide via an application event filter (see
        # eventFilter / _handle_nav_key) rather than QShortcut, so they work wherever
        # focus is except inside an arrow-consuming input.
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)
        self._announce_ready()

    def _announce_ready(self) -> None:
        self.statusBar().showMessage("Open pixel data to begin.")
        # A failed plugin load is a warning the user should see, not a status
        # line lost behind the next message - surface it as a modal at startup.
        self._alert_plugin_issues()

    # -- construction ------------------------------------------------------
    def _build_files_dock(self) -> None:
        """The left-side open-files dock, mirroring the workspace model."""
        self._files_panel = FileListPanel()
        self._files_panel.entry_activated.connect(self._activate_entry)
        self._files_panel.remove_requested.connect(self._remove_entry)
        self._files_panel.write_requested.connect(self._write_entry_checked)
        self._files_panel.export_png_requested.connect(self._export_png)
        self._files_panel.export_raw_requested.connect(self._export_raw)
        self._files_panel.export_slices_requested.connect(self._export_file_slices)
        self._files_panel.import_png_requested.connect(self._import_png_into)
        self._files_panel.new_slice_requested.connect(self._new_slice_for)
        self._files_panel.new_slice_from_view_requested.connect(
            self._new_slice_from_view_for
        )
        self._files_panel.new_slice_from_selection_requested.connect(
            self._new_slice_from_selection_for
        )
        self._files_panel.new_bookmark_requested.connect(self._new_bookmark_for)
        self._files_panel.use_palette_requested.connect(self._use_palette_entry)
        self._files_panel.edit_slice_requested.connect(self._edit_slice)
        self._files_panel.jump_to_source_requested.connect(self._jump_to_slice_source)
        self._files_panel.jump_to_bookmark_requested.connect(self._jump_to_bookmark)
        self._files_panel.bookmark_as_palette_requested.connect(
            self._use_bookmark_as_palette
        )
        self._files_panel.rename_committed.connect(self._rename_entry)
        self._files_dock = QDockWidget("Files", self)
        self._files_dock.setObjectName("files-dock")  # keeps saveState usable
        self._files_dock.setWidget(self._files_panel)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self._files_dock)

        ws = self._workspace
        ws.on_added.append(self._on_entry_added)
        ws.on_removed.append(self._files_panel.remove_entry)
        ws.on_current_changed.append(self._on_current_entry_changed)
        ws.on_dirty_changed.append(self._on_entry_dirty_changed)
        # Removing (or restoring, via undo) an entry can change whether any
        # references are missing - keep the Locate menu's enabled state honest.
        ws.on_removed.append(lambda _entry: self._update_locate_action())

    def _on_entry_added(self, entry: Entry) -> None:
        # The panel nests a slice under its parent file's item when it's open.
        self._files_panel.add_entry(entry, self._workspace.parent_of(entry))

    @contextmanager
    def _undo_apply(self):
        """Mark a command's undo/redo application as in progress.

        Applying pokes the same widgets and paths as user gestures; push sites
        bail while this is set, so an apply can never push a second command.
        """
        self._applying_undo = True
        try:
            yield
        finally:
            self._applying_undo = False

    def _push_command(self, command: QUndoCommand) -> None:
        """Push onto the session stack (push() runs the command's redo).

        Dirty tracking rides on the commands themselves, not on the stack:
        one stack spans every entry, so ``QUndoStack``'s single clean-index
        can't express per-entry state. Byte-editing commands stamp a revision
        token per entry instead (see :class:`Entry`).
        """
        self._undo_stack.push(command)

    def _ensure_current(self, entry: Entry) -> bool:
        """Make ``entry`` the current view for a document-scoped command.

        Undoing a change made in another entry first switches back to it, so
        the revert happens where the user can see it. False when activation
        fails (vanished file) - the command then skips its apply.
        """
        if self._workspace.current is not entry:
            self._activate_entry(entry)
        return self._workspace.current is entry

    def _on_entry_dirty_changed(self, entry: Entry) -> None:
        self._files_panel.refresh_entry(entry)
        self._write_all_action.setEnabled(bool(self._workspace.dirty_entries()))

    def _rename_entry(self, entry: Entry, name: str) -> None:
        if self._applying_undo or name == entry.name:
            return
        self._push_command(RenameEntryCommand(self, entry, entry.name, name))

    def _apply_entry_name(self, entry: Entry, name: str) -> None:
        entry.name = name
        self._files_panel.refresh_entry(entry)
        if entry is self._workspace.current:
            self._update_window_title()

    def _update_window_title(self) -> None:
        """Set the window title.

        When a project is loaded the title names the **project file**, not the
        graphic on screen: a project is the whole session, so switching entries
        within it shouldn't rename the window. It carries Qt's ``[*]``
        placeholder, which :meth:`_refresh_project_modified` turns into the
        platform's unsaved marker (a trailing ``*`` here, the close-button dot on
        macOS) while the session differs from the file. With no project open it
        falls back to the current entry's name (``(missing)`` when its file is
        gone), or a bare ``Celpix`` when nothing is open - and carries no marker,
        since there is no project file those changes could be saved to.
        """
        if self._project_path is not None:
            self.setWindowTitle(f"Celpix - {Path(self._project_path).name}[*]")
            self._refresh_project_modified()
            return
        self.setWindowModified(False)
        entry = self._workspace.current
        if entry is None:
            self.setWindowTitle("Celpix")
        elif data_missing(entry):
            self.setWindowTitle(f"Celpix - {entry.name} (missing)")
        else:
            self.setWindowTitle(f"Celpix - {entry.name}")

    def _on_undo_index_changed(self, _index: int) -> None:
        self._refresh_project_modified()

    def _refresh_project_modified(self) -> None:
        """Re-evaluate the title's unsaved-project marker.

        Called from the choke points every project-visible change passes through
        (a view refresh, a selection change, an undo-stack move). A missed one
        only leaves the *marker* briefly stale - the prompts that matter re-ask
        :meth:`_project_is_dirty` at the moment they need the answer.
        """
        if self._project_path is not None:
            self.setWindowModified(self._project_is_dirty())

    def _project_snapshot(self) -> dict[str, object] | None:
        """The project document the open workspace would save right now.

        ``None`` with no project open. The live toolbar/view state is captured
        into the current entry first - it is part of what a save writes, so a
        comparison that skipped it would call an edited session clean.
        """
        if self._project_path is None:
            return None
        self._capture_session()
        return projectfile.project_document(self._workspace, self._project_path)

    def _project_is_dirty(self) -> bool:
        """True when the open project differs from what is on disk."""
        if self._saved_project is None:
            return False
        return self._project_snapshot() != self._saved_project

    def _apply_add_entry(self, entry: Entry) -> None:
        """Append ``entry`` to the workspace and show it - the application
        path for open-file/new-slice/new-bookmark/add-palette commands and
        their redos. A bookmark or palette only lands in the list; there is
        nothing of it to show."""
        self._workspace.insert(entry, len(self._workspace.entries))
        if entry.kind in (EntryKind.FILE, EntryKind.SLICE):
            self._activate_entry(entry)

    def _apply_close_entry(self, entry: Entry) -> None:
        """Take ``entry`` (and, for a file, its slices) out of the workspace;
        the current view repoints to a neighbour via the workspace."""
        self._workspace.close(entry)

    def _apply_restore_entries(
        self, victims: list[tuple[int, Entry]], was_current: Entry | None
    ) -> None:
        """Reinstate removed entries at their recorded list positions.

        Ascending order puts a file back before its slices, so the panel can
        nest them under it as they arrive. The view returns to the removed
        entry only if it was current at removal time.
        """
        for index, entry in sorted(victims, key=lambda pair: pair[0]):
            self._workspace.insert(entry, index)
        if any(entry is was_current for _, entry in victims):
            self._activate_entry(was_current)

    # -- entry switching -----------------------------------------------------
    def _activate_entry(self, entry: Entry) -> None:
        """Switch the view to ``entry`` - every activation path funnels here."""
        if entry is None or entry is self._workspace.current:
            return
        if entry.kind in (EntryKind.BOOKMARK, EntryKind.PALETTE):
            return  # no view of its own - selecting one in the list is inert
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
        header = (
            session.header_length
            if entry.kind is EntryKind.FILE and session.headered
            else 0
        )
        cfg = self._pixel_config(entry, session.pixel_preset_id, header)
        try:
            px = pipeline.load_pixel_data(cfg, self._registry)
        except PipelineError as exc:
            if not quiet:
                self._report(exc)
            return False
        if backfill_slice_length(entry, px.ctx):
            # The decompressor discovered the slice's true extent: rebuild the
            # config bounded by it, so save-back is slot-enforced from now on.
            cfg = self._pixel_config(entry, session.pixel_preset_id, header)
            self._files_panel.refresh_entry(entry)
        entry.doc = Document(
            pixel_data=px.data,
            bytes_per_tile=px.bytes_per_tile,
            tile_width=px.tile_width,
            tile_height=px.tile_height,
            palette=Palette.default(self._index_space(session.pixel_preset_id)),
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
                NO_DECOMPRESS
                if entry.kind is EntryKind.SLICE
                else self._compression_id()
            ),
            headered=entry.kind is EntryKind.FILE and self._headered.isChecked(),
            header_length=self._header_len.value(),
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
            headered=self._headered.isChecked(),
            header_length=self._header_len.value(),
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
            (self._header_len, session.header_length),
            (self._block_cols, view.block_columns),
            (self._block_rows, view.block_rows),
        )
        checks = (
            (self._grid, view.show_grid),
            (self._headered, session.headered),
            (self._two_d, view.two_dimensional),
        )
        with signals_blocked(*(w for w, _ in (*spins, *checks))):
            for spin, value in spins:
                spin.setValue(value)
            for check, value in checks:
                check.setChecked(value)
        # Reselect the Pattern preset (or Custom) that matches the block/order/2D
        # values just restored, and lock the controls to match.
        self._sync_pattern_selection()
        # Header skip is FILE display state; a slice's offsets are absolute
        # file offsets and must not shift under it.
        is_file = entry.kind is EntryKind.FILE
        self._headered.setEnabled(is_file)
        self._header_len.setEnabled(is_file)
        self._offset, self._nudge = view.tile_offset, view.byte_nudge
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
        self._update_selection_actions()
        self._set_palette_mode(session.palette_mode)
        self._write_action.setEnabled(entry.doc.pixel_config.write_enabled)
        # Only whole files spawn slices and bookmarks - neither nests (and a
        # file's byte stream is always raw, so its positions map straight to
        # file offsets).
        self._new_slice_action.setEnabled(is_file)
        self._new_slice_from_view_action.setEnabled(is_file)
        self._new_bookmark_action.setEnabled(is_file)
        self._update_window_title()

    def _clear_document_view(self) -> None:
        """Blank the canvas and disable every document-bound action - shared by
        the nothing-open and missing-file (unavailable) states."""
        self._doc = None
        self._selected_tile = None
        self._selected_last = None
        self._rect_cells, self._rect_tiles = None, ()
        self._canvas.set_selection(None)
        self._update_selection_actions()
        self._canvas.set_image(QImage())
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
        self._write_action.setEnabled(False)
        self._new_slice_action.setEnabled(False)
        self._new_slice_from_view_action.setEnabled(False)
        self._new_bookmark_action.setEnabled(False)

    def _set_document_ui_enabled(self, enabled: bool) -> None:
        """Grey out (or restore) the document-editing surfaces as a block.

        A missing (unavailable) entry has no document to drive, so its codec,
        arrangement and view toolbars and the palette dock are disabled until a
        real document is shown again.
        """
        for bar in (
            self._codecs_toolbar,
            self._arrange_toolbar,
            self._view_toolbar,
            self._transform_toolbar,
        ):
            bar.setEnabled(enabled)
        self._palette_dock.setEnabled(enabled)
        # The tools dock is only live in pixel mode with a document to paint on.
        self._tools_dock.setEnabled(enabled and self._edit_mode is EditMode.PIXEL)

    def _show_empty(self) -> None:
        """Nothing open: clear the canvas, disable everything document-bound."""
        self._clear_document_view()
        self._set_document_ui_enabled(True)  # idle, but live for the next open
        self._update_window_title()
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
        self._update_window_title()
        self._sync_nav()
        self.statusBar().showMessage(
            f"{entry.name}: file not found - File ▸ Locate missing files "
            "to re-point it."
        )

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
        answer = QMessageBox.question(self, "Celpix - remove", message)
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
            "Celpix - remove palette",
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
        self._resync_current_palette()

    def _apply_restore_palette_users(
        self, palette: Entry, index: int, users: list[PaletteUserLink]
    ) -> None:
        """Re-register the palette and relink every user - the command's undo."""
        self._workspace.insert(palette, index)
        for link in users:
            self._relink_user_to_file_palette(link)
        self._resync_current_palette()

    def _resync_current_palette(self) -> None:
        """Re-apply the current entry's (possibly changed) palette to the dock and
        canvas after a re-home, so the on-screen mode/label follow the entry."""
        current = self._workspace.current
        if current is not None and current.doc is not None:
            self._restore_session(current)
        self._refresh_view()

    def closeEvent(self, event) -> None:  # noqa: ANN001 - Qt override
        """Quit, having asked about both kinds of unsaved work.

        The project first: saving it may itself write the dirty files out (its
        own gate offers that), so asking the other way round could ask about the
        same files twice.
        """
        if not self._confirm_discard_project("Quitting"):
            event.ignore()
            return
        # The files gate, via the shared unsaved-changes prompt: on quit the
        # edits are lost for good, so its middle option is "Discard" (not the
        # project paths' "Continue Without"), and Enter defaults to writing.
        if not self._resolve_dirty_entries(
            "Quitting discards unsaved changes to",
            write_label="Write Changes",
            skip_label="Discard",
            default_write=True,
        ):
            event.ignore()
            return
        super().closeEvent(event)

    def _build_hex_dock(self) -> None:
        """The raw-hex-dump dock: a presentation-only view of the file bytes at
        the current offset. Hidden by default (opened from the Panels menu), so
        the main window only refreshes it while it is visible."""
        self._hex_panel = HexViewPanel()
        self._hex_dock = QDockWidget("Hex", self)
        self._hex_dock.setObjectName("hex-dock")  # keeps saveState usable
        self._hex_dock.setWidget(self._hex_panel)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self._hex_dock)
        self._hex_dock.hide()
        # Toggling the dock open won't re-run _refresh_view, so refresh on show.
        self._hex_dock.visibilityChanged.connect(lambda _visible: self._refresh_hex())

    def _build_menus(self) -> None:
        file_menu = self.menuBar().addMenu("File")

        open_pixel = QAction("Open pixel data…", self)
        open_pixel.triggered.connect(self._open_pixel)
        file_menu.addAction(open_pixel)

        open_palette_data = QAction("Open palette data…", self)
        open_palette_data.setToolTip(
            "Add a palette file to the Palettes section of the files list - "
            "double-click it there to apply it to the view"
        )
        open_palette_data.triggered.connect(self._prompt_add_palette_file)
        file_menu.addAction(open_palette_data)

        file_menu.addSeparator()

        open_project = QAction("Open Project…", self)
        open_project.setToolTip("Resume a saved session from a .celpix project file")
        open_project.setShortcut(QKeySequence.StandardKey.Open)  # Ctrl+O
        open_project.triggered.connect(self._open_project)
        file_menu.addAction(open_project)

        save_project = QAction("Save Project", self)
        save_project.setToolTip(
            "Save the open files/slices and their settings (references, "
            "never the edited bytes) to a .celpix project file"
        )
        save_project.setShortcut(QKeySequence.StandardKey.Save)  # Ctrl+S
        save_project.triggered.connect(self._save_project)
        file_menu.addAction(save_project)

        save_project_as = QAction("Save Project As…", self)
        save_project_as.setShortcut(QKeySequence.StandardKey.SaveAs)  # Ctrl+Shift+S
        save_project_as.triggered.connect(self._save_project_as)
        file_menu.addAction(save_project_as)

        self._locate_missing_action = QAction("Locate missing files…", self)
        self._locate_missing_action.setToolTip(
            "Re-point project entries whose referenced file (or palette) has "
            "moved since the project was saved"
        )
        self._locate_missing_action.triggered.connect(
            lambda: self._relocate_missing(prompt_summary=False)
        )
        self._locate_missing_action.setEnabled(False)  # armed by missing files
        file_menu.addAction(self._locate_missing_action)

        file_menu.addSeparator()

        self._new_slice_action = QAction("New Slice…", self)
        self._new_slice_action.setToolTip(
            "Mark an offset+length region of the current file as its own entry"
        )
        self._new_slice_action.triggered.connect(self._new_slice_current)
        self._new_slice_action.setEnabled(False)
        file_menu.addAction(self._new_slice_action)

        self._new_slice_from_view_action = QAction("New Slice from View", self)
        self._new_slice_from_view_action.setToolTip(
            "New slice covering the current viewport - its position and "
            "visible extent (or the structure in view, when the compression "
            "preview found one)"
        )
        self._new_slice_from_view_action.triggered.connect(self._new_slice_from_view)
        self._new_slice_from_view_action.setEnabled(False)
        file_menu.addAction(self._new_slice_from_view_action)

        self._new_slice_from_selection_action = QAction(
            "New Slice from Selection", self
        )
        self._new_slice_from_selection_action.setToolTip(
            "New slice covering the selected tile range"
        )
        self._new_slice_from_selection_action.triggered.connect(
            self._new_slice_from_selection
        )
        self._new_slice_from_selection_action.setEnabled(False)
        file_menu.addAction(self._new_slice_from_selection_action)

        self._new_bookmark_action = QAction("New Bookmark", self)
        self._new_bookmark_action.setToolTip(
            "Bookmark the current position, with a snapshot of the current "
            "settings - jumping back restores both"
        )
        self._new_bookmark_action.setShortcut(QKeySequence("Ctrl+B"))
        self._new_bookmark_action.triggered.connect(self._new_bookmark_current)
        self._new_bookmark_action.setEnabled(False)
        file_menu.addAction(self._new_bookmark_action)

        file_menu.addSeparator()

        self._write_action = QAction("Write", self)
        self._write_action.setToolTip(
            "Write the current file or slice's bytes back to disk"
        )
        self._write_action.setShortcut(QKeySequence("Ctrl+W"))
        self._write_action.triggered.connect(self._write_current)
        self._write_action.setEnabled(False)
        file_menu.addAction(self._write_action)

        self._write_all_action = QAction("Write All", self)
        self._write_all_action.setToolTip(
            "Write every open file and slice with unsaved changes"
        )
        self._write_all_action.setShortcut(QKeySequence("Ctrl+Shift+W"))
        self._write_all_action.triggered.connect(self._write_all)
        self._write_all_action.setEnabled(False)  # armed by dirty entries
        file_menu.addAction(self._write_all_action)

        file_menu.addSeparator()

        self._build_export_menu(file_menu)

        file_menu.addSeparator()

        open_plugins = QAction("Open plugins folder…", self)
        open_plugins.setToolTip(
            "Drop plugins into pixel/, palette/, compression/ or containers/ "
            "(preset .toml or code .py)"
        )
        open_plugins.triggered.connect(self._open_plugins_folder)
        open_plugins.setEnabled(self._plugin_dir is not None)
        file_menu.addAction(open_plugins)

        refresh = QAction("Refresh plugins", self)
        refresh.setShortcut(QKeySequence.StandardKey.Refresh)  # F5
        refresh.setToolTip(
            "Reload plugins from the folder and re-run on the open file (developer aid)"
        )
        refresh.triggered.connect(self._refresh_plugins)
        refresh.setEnabled(self._reload_plugins is not None)
        file_menu.addAction(refresh)

        file_menu.addSeparator()

        quit_action = QAction("Quit", self)
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        self._build_edit_menu()
        self._build_view_menu()
        self._build_navigate_menu()
        self._build_palette_menu()
        self._build_panels_menu()

    def _build_view_menu(self) -> None:
        """View ▸ display toggles that change how the pixels are drawn (as
        opposed to Navigate, which moves the window). Home of the grid toggle,
        which used to live on the toolbar."""
        menu = self.menuBar().addMenu("View")
        # A checkable action, not a toolbar checkbox: same isChecked/setChecked/
        # toggled surface the rest of the code already drives, so the view-state
        # capture/restore paths need no special-casing.
        self._grid = QAction("Grid", self, checkable=True)
        self._grid.setToolTip("Overlay a per-tile grid (at zoom ≥ 2)")
        self._grid.toggled.connect(self._on_view_change)
        # Display-only shortcut, like Palette ▸ Load from Selection: the bare "G"
        # is routed by the app-wide event filter (_handle_nav_key), which yields
        # to focused text inputs - a live shortcut here would steal it from them.
        self._grid.setShortcut(QKeySequence("G"))
        self._grid.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
        menu.addAction(self._grid)
        self._build_grid_style_menu(menu)

    def _build_grid_style_menu(self, view_menu) -> None:  # noqa: ANN001 - QMenu
        """View ▸ Grid Style ▸ the YY-CHR style set (Point/Dot/Dash/Line).

        Unlike the Grid toggle (per-view session state), the style is one
        app-wide appearance choice persisted in QSettings - remembered across
        launches and shared by every view, so it isn't part of a document's
        saved ViewOptions.
        """
        style = load_enum_setting(GRID_STYLE_KEY, GridStyle.LINE)
        self._canvas.set_grid_style(style)
        submenu = view_menu.addMenu("Grid Style")
        group = QActionGroup(self)  # exclusive: one style checked at a time
        self._grid_style_group = group
        labels = (
            (GridStyle.POINT, "Point"),
            (GridStyle.DOT, "Dot"),
            (GridStyle.DASH, "Dash"),
            (GridStyle.LINE, "Line"),
        )
        for value, text in labels:
            action = QAction(text, self, checkable=True)
            action.setData(value)
            action.setChecked(value is style)
            group.addAction(action)
            submenu.addAction(action)
        group.triggered.connect(self._on_grid_style_change)

    def _on_grid_style_change(self, action: QAction) -> None:
        style = action.data()
        QSettings().setValue(GRID_STYLE_KEY, style.value)
        self._canvas.set_grid_style(style)

    def _build_panels_menu(self) -> None:
        """Panels ▸ show/hide the dockable panels (Files, Palette, Hex)."""
        menu = self.menuBar().addMenu("Panels")
        files_toggle = self._files_dock.toggleViewAction()
        files_toggle.setText("Files Panel")
        menu.addAction(files_toggle)
        palette_toggle = self._palette_dock.toggleViewAction()
        palette_toggle.setText("Palette Panel")
        menu.addAction(palette_toggle)
        tools_toggle = self._tools_dock.toggleViewAction()
        tools_toggle.setText("Tools Panel")
        menu.addAction(tools_toggle)
        hex_toggle = self._hex_dock.toggleViewAction()
        hex_toggle.setText("Hex Panel")
        menu.addAction(hex_toggle)

    def _open_plugins_folder(self) -> None:
        if self._plugin_dir is None:
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(self._plugin_dir))
        self._alert_plugin_issues()

    # -- actions -----------------------------------------------------------
    def _open_pixel(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Open pixel data")
        if path:
            self._load_pixel(path)

    def _load_pixel(self, path: str) -> None:
        """Open ``path`` as a workspace entry and switch the view to it.

        The shared entry point for both File ▸ Open and drag-and-drop, so a
        dropped file behaves exactly like an opened one. A file that is
        already open activates its existing entry - identity is the path -
        so only a genuinely new entry becomes an undoable step.
        """
        existing = self._workspace.find_file(path)
        if existing is not None:
            self._activate_entry(existing)
            return
        entry = Entry(name=Path(path).name, kind=EntryKind.FILE, path=path)
        self._push_command(AddEntryCommand(self, entry, f"open {entry.name}"))

    # -- view --------------------------------------------------------------
    def _on_view_change(self, *_args) -> None:
        if self._doc is not None:
            self._refresh_view()

    def _render_arrangement(
        self,
        pixel_bytes: bytes,
        engine,  # noqa: ANN001 - a pixel-interpret plugin
        params,  # noqa: ANN001 - the preset's engine params
        layout: BlockLayout,
        two_dimensional: bool,
        max_rows: int | None,
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
        """
        assert self._doc is not None
        grid, filled = pipeline.decode_and_compose(
            pixel_bytes, engine, params, layout, two_dimensional, max_rows
        )
        base = self._doc.view.subpalette_row * self._index_space()
        return render_bridge.render(grid, self._doc.palette, base), filled

    def _refresh_view(self) -> None:
        assert self._doc is not None
        cols = self._columns.value()
        # Rows is a free display-window height (bounded only by the spin's own 256
        # cap), not by the data. Asking for more rows than the file fills just
        # leaves the neutral background showing past the last tile row (see
        # shown_rows below) instead of clamping the input - so the height survives
        # switching to a format whose larger tiles leave far fewer rows of data.
        # Re-clamp the offset next: a smaller file, or a bigger window (cols/rows),
        # can push the previous offset past the last page.
        self._offset = self._doc.clamp_offset(
            self._offset, cols, self._rows.value(), self._nudge
        )
        rows = self._rows.value()
        # Clamp the subpalette row to the rows the loaded palette actually has -
        # switching to a smaller palette (e.g. Offset's 16 rows back to Default's
        # one) must not leave the view pointing past it. Signals blocked: this
        # is a correction, not a user change, and must not re-enter here.
        group = self._index_space()  # the subpalette row size
        max_row = max(0, len(self._doc.palette) - 1) // group
        if self._subpalette.value() > max_row:
            with signals_blocked(self._subpalette):
                self._subpalette.setValue(max_row)
        self._doc.view = ViewOptions(
            columns=cols,
            rows=rows,
            zoom=self._zoom.value(),
            show_grid=self._grid.isChecked(),
            subpalette_row=self._subpalette.value(),
            tile_offset=self._offset,
            byte_nudge=self._nudge,
            block_columns=self._block_cols.value(),
            block_rows=self._block_rows.value(),
            block_order=self._block_order.currentData(),
            two_dimensional=self._two_d.isChecked(),
        )
        # Deferred decode: only the visible window's bytes are sliced, then decoded
        # and laid out by the shared arrangement path (2D reflow / block layout).
        # Reads back through doc.view (like zoom/grid below) so the freshly stored
        # ViewOptions is genuinely the render input, not a dead mirror.
        view = self._doc.view
        layout = BlockLayout(
            cols, view.block_columns, view.block_rows, view.block_order
        )
        engine, preset = self._registry.engine_for(
            self._doc.pixel_config.interpret_preset_id
        )
        window = self._doc.window_bytes(view.tile_offset, cols * rows, view.byte_nudge)
        image, filled = self._render_arrangement(
            window, engine, preset.params, layout, view.two_dimensional, max_rows=rows
        )
        base = view.subpalette_row * group
        tw, th = self._pixel_tile_size()
        self._canvas.set_tile_size(tw, th)
        self._canvas.set_zoom(view.zoom)
        self._canvas.set_grid(view.show_grid)
        self._canvas.set_arrangement(
            view.block_columns, view.block_rows, view.block_order
        )
        self._canvas.set_filled_tiles(filled)
        self._canvas.set_image(image)
        self._refresh_selection(cols * rows)
        self._palette_panel.set_palette(self._doc.palette.colors)
        self._palette_panel.set_active_range(base, group)
        # A reload can recolor (or drop) the selected entry under the same index.
        self._update_color_details()
        self._sync_color_editor()
        self._sync_nav()
        self._refresh_overlay()
        self._refresh_hex()
        # Everything above landed in doc.view, which a project save writes out.
        self._refresh_project_modified()

    def _refresh_hex(self) -> None:
        """Feed the hex panel a dump of the file bytes at the current offset.

        Cheap no-op while the dock is hidden (its usual state). The dump starts
        at the row holding the current view origin - so the offset's row is
        always the top line - and highlights the currently selected tile(s),
        using the same address format as the navbar. Bounded to the on-screen
        window (a minimum of some context, a cap for huge windows) so a
        multi-megabyte file never renders as one giant document.
        """
        if not self._hex_dock.isVisible():
            return
        if self._doc is None:
            self._hex_panel.clear()
            return
        data = self._doc.pixel_data
        origin = self._byte_position()
        window = len(
            self._doc.window_bytes(
                self._offset, self._columns.value() * self._rows.value(), self._nudge
            )
        )
        row_start = (origin // BYTES_PER_ROW) * BYTES_PER_ROW
        # Enough rows to cover the visible window, floored so the panel is never
        # nearly empty and capped so a whole-file view can't blow up the dump.
        span = max(window, 16 * BYTES_PER_ROW)
        span = min(span, 256 * BYTES_PER_ROW)
        region_end = min(len(data), row_start + BYTES_PER_ROW + span)
        base = self._display_base()
        self._hex_panel.show_bytes(
            data,
            row_start,
            region_end,
            lambda index: self._format_offset(base + index),
            self._selection_byte_range(),
        )

    def _alert(self, message: str, *, title: str = "Celpix", detail: str = "") -> None:
        """The one place errors and warnings reach the user, as a modal dialog.

        A status-bar line is easy to miss - it's silent and scrolls away - so
        anything that actually went wrong (a failed load, an unreadable file, an
        unrecognised format, a blocked write) blocks with a dialog the user must
        acknowledge. Success and progress notes still belong in the status bar;
        this is only for failures. ``detail`` fills the dialog's expandable
        details pane for long specifics (e.g. a per-plugin error list).
        """
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(title)
        box.setText(message)
        if detail:
            box.setDetailedText(detail)
        box.exec()

    def _report(self, exc: PipelineError) -> None:
        """Surface a pipeline failure. Thin wrapper over :meth:`_alert` kept for
        the many call sites that already hold a :class:`PipelineError`."""
        self._alert(str(exc), title="Celpix - pipeline error")

    def _alert_plugin_issues(self) -> None:
        """Modal listing plugins that failed to load - shown at startup and
        after a refresh, and reachable again from File ▸ Open plugins folder."""
        if not self._plugin_issues:
            return
        detail = "\n".join(f"• {i.path}: {i.message}" for i in self._plugin_issues)
        self._alert(
            f"{len(self._plugin_issues)} plugin(s) failed to load. The rest of "
            "the app works normally; see the details, or File ▸ Open plugins "
            "folder.",
            title="Celpix - plugin load issues",
            detail=detail,
        )
