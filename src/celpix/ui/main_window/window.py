"""The application main window: open pixel/palette data, view it, save it back.

Menus (File, Edit, View, Navigate, Palette, Panels, Help) over a two-column body. The
left column is the Files and Palette docks, splitting the window's full height
between them; the right is the editing surface: four bars stacked over a
scrollable :class:`~celpix.ui.canvas.Canvas` - codecs (pixel format,
compression), arrangement (Pattern presets, block grouping, fill order, 2D),
view (columns, rows, zoom, subpalette row - the palette format lives in the
palette dock) and transform - with the canvas showing a windowed view with
tile-range selection and a navigation bar under it carrying the address/bank
readout. None of those bars is a QMainWindow toolbar: that area spans the whole
window width and would cut across the top of the left column. An optional hex
view docks at the bottom. Undo/redo spans one session-wide history, and a
compression scan/preview overlays decodable structures.

It drives the Qt-free pipeline through the plugin registry and never interprets
bytes itself; all decode/encode goes through ``pipeline``.

This module is the **shell**: it builds that surface and holds what belongs to no
single one of it — the widgets and docks themselves, the menu bar, the undo stack
every mixin pushes onto, the open project's dirty tracking, and the one modal
errors reach the user through. The work each surface does lives in its own mixin
beside it (see the package docstring); the two closest to this file are
:mod:`~celpix.ui.main_window.session`, which swaps entries in and out, and
:mod:`~celpix.ui.main_window.rendering`, which turns the current one into pixels.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QDesktopServices,
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

from celpix.core.document import Document
from celpix.core.errors import PipelineError
from celpix.core.palette import Palette
from celpix.plugins.detect import detect_container
from celpix.plugins.discovery import PluginLoadIssue
from celpix.plugins.registry import Registry, default_registry
from celpix.project import projectfile
from celpix.project.workspace import (
    Entry,
    EntryKind,
    PaletteMode,
    Workspace,
    data_missing,
)
from celpix.ui.canvas import CANVAS_BACKGROUND, Canvas, GridStyle
from celpix.ui.color_editor import ColorEditorDialog
from celpix.ui.decompress_overlay import DecompressOverlay
from celpix.ui.file_list_panel import FileListPanel
from celpix.ui.help_dialogs import AboutDialog, ShortcutGuide, shortcut_sections
from celpix.ui.hex_view_panel import HexViewPanel
from celpix.ui.main_window.color_editing import ColorEditingMixin
from celpix.ui.main_window.compression import CompressionMixin
from celpix.ui.main_window.entries import EntriesMixin
from celpix.ui.main_window.interpretation import InterpretationMixin
from celpix.ui.main_window.navigation import NavigationMixin
from celpix.ui.main_window.palette_dock import PaletteDockMixin
from celpix.ui.main_window.palette_source import (
    DEFAULT_SESSION_PALETTE_FORMAT,
    PaletteSourceMixin,
)
from celpix.ui.main_window.pixel_edit import PixelEditMixin
from celpix.ui.main_window.rearrange import RearrangeMixin
from celpix.ui.main_window.rendering import RenderingMixin
from celpix.ui.main_window.selection import (
    SelectionMixin,
)
from celpix.ui.main_window.session import SessionMixin
from celpix.ui.main_window.transfer import TransferMixin
from celpix.ui.main_window.transform import TransformMixin
from celpix.ui.tools import EditMode
from celpix.ui.undo_commands import (
    AddEntryCommand,
    RenameEntryCommand,
)
from celpix.ui.widgets import (
    load_enum_setting,
    save_enum_setting,
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
    RearrangeMixin,
    SessionMixin,
    RenderingMixin,
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
        self.setWindowTitle("celPix")
        # The interpretation bars sit over the canvas rather than spanning the
        # window, so they start right of the Files/Palette column. The default
        # width carries that column on top of what the bars need, keeping the
        # codecs row (the longest) out of an overflow chevron.
        self.resize(1120, 768)
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
        self._session_palette_format = DEFAULT_SESSION_PALETTE_FORMAT
        # The generated palette the dock shows while nothing at all is open -
        # built on first use (see _idle_palette) and never edited, so one
        # instance serves the whole session.
        self._idle_palette_cache: Palette | None = None
        # A registered .pal being *previewed* in the dock, or None. Only ever set
        # with no document open: with nowhere to write an edit back to, the dock
        # shows a palette file's colors rather than editing them, so this is
        # display state and nothing more (see _preview_palette_file).
        self._preview_palette: Entry | None = None
        # The shared color editor, while open (None otherwise). One non-modal
        # dialog is reused and retargeted as the palette selection moves, so the
        # eyedropper can reach the canvas and the swatch grid underneath it.
        self._color_editor: ColorEditorDialog | None = None
        # Top-left tile index of the view window. This offset is what scrolls
        # through the file - only the window is composed and rendered, so the
        # scroll area moves nothing but the (zoomed) window inside its viewport.
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
        # The column count from before a bitmap width took Cols over, so dropping
        # the width gives the user's own choice back instead of leaving the
        # derived count behind. None = no width is currently overriding Cols. It
        # is scratch state of the entry on screen, so switching entries clears it
        # (the incoming entry's Cols is its own).
        self._columns_before_bitmap: int | None = None
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
        # Likewise the rearrangement: _refresh_view renders through the map, so
        # it has to be there before anything can draw.
        self._init_rearrange()

        self._canvas = Canvas()
        self._overlay = DecompressOverlay(self)
        self._canvas.tiles_selected.connect(self._on_tiles_selected)
        self._canvas.color_picked.connect(self._on_color_picked)
        self._connect_pixel_canvas()
        self._canvas.rearrange_started.connect(self._on_rearrange_started)
        self._canvas.rearrange_moved.connect(self._on_rearrange_moved)
        self._canvas.rearrange_dropped.connect(self._on_rearrange_dropped)
        self._canvas.rearrange_cancelled.connect(self._on_rearrange_cancelled)
        # Right-click the canvas for the clipboard actions (the canvas selects
        # the tile under the cursor first, unless it is already in the run).
        self._canvas.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._canvas.customContextMenuRequested.connect(self._show_canvas_menu)
        # ClickFocus so clicking the view takes focus off any dropdown/spin box (which
        # would otherwise keep the arrow keys), letting navigation resume. Navigation
        # itself is window-wide via eventFilter, not tied to canvas focus.
        self._canvas.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        # Space-drag panning: the canvas emits view deltas, the scroll bars absorb
        # them (and clamp so the image can't be dragged off screen).
        self._canvas.pan_requested.connect(self._pan_view)
        # Wheel zoom (both modes): the canvas reports steps + cursor, the window
        # drives the zoom control and re-anchors the view under the cursor.
        self._canvas.zoom_requested.connect(self._on_zoom_requested)
        scroll = self._scroll = QScrollArea()
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
        self._offset_bar.setToolTip("File position\nDrag to jump")
        self._offset_bar.setStyleSheet(self._offset_bar_style())
        self._offset_bar.valueChanged.connect(self._set_offset)

        # Every bar lives in this column rather than in QMainWindow's toolbar
        # area: that area spans the whole window width and would cut across the
        # top of the docks, so the interpretation bars are stacked in here above
        # the canvas instead and the left column runs the window's full height.
        # Kept on self because _build_toolbar runs later (it needs the palette
        # dock) and inserts its three rows above the transform bar.
        self._transform_toolbar = self._build_transform_toolbar()
        canvas_column = self._canvas_column = QVBoxLayout()
        canvas_column.setContentsMargins(0, 0, 0, 0)
        canvas_column.setSpacing(0)
        canvas_column.addWidget(self._transform_toolbar)
        # The tools rail runs down the right of the paint surface, below the
        # transform bar; the two frame the canvas as toolbars of the editing
        # surface. Top-aligned so it hugs the top rather than stretching.
        canvas_row = QHBoxLayout()
        canvas_row.setContentsMargins(0, 0, 0, 0)
        canvas_row.setSpacing(0)
        canvas_row.addWidget(scroll, 1)
        canvas_row.addWidget(self._build_tools_bar(), 0, Qt.AlignmentFlag.AlignTop)
        canvas_column.addLayout(canvas_row, 1)

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
        self._connect_pixel_palette()  # after palette dock: needs its swatch grid
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
        # A fresh window *is* the nothing-open state, so it is entered through
        # the same path a close lands on rather than being half-assembled here -
        # which is what puts the read-only default palette in the dock.
        self._show_empty()

    def _announce_ready(self) -> None:
        self.statusBar().showMessage("Open pixel data to begin.")
        # A failed plugin load is a warning the user should see, not a status
        # line lost behind the next message - surface it as a modal at startup.
        self._alert_plugin_issues()

    # -- construction ------------------------------------------------------
    def _build_files_dock(self) -> None:
        """The left-side open-files dock, mirroring the workspace model."""
        self._files_panel = FileListPanel(self._registry)
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
        self._files_panel.change_container_requested.connect(self._change_container_for)
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
        ws.on_removed.append(lambda _entry: self._sync_locate_action())

    def _on_entry_added(self, entry: Entry) -> None:
        # The panel nests a slice under its parent file's item when it's open.
        self._files_panel.add_entry(entry, self._workspace.parent_of(entry))

    # -- the undo stack ------------------------------------------------------
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

    def _ensure_edit_context(self, entry: Entry, mode: EditMode) -> bool:
        """Return to the entry *and* the edit mode an editing command was made in.

        Tile and pixel editing are two views of the same bytes: a tile paste
        reverting while the user is painting pixels — or a pixel stroke reverting
        under the tile grid — lands somewhere the gesture couldn't have happened,
        and a pixel step's selection has nowhere to go in tile mode. So a step is
        always undone where it was made, the same reasoning that takes the view
        back to the entry it happened in.

        Switching modes sets a live float down (it can't push mid-apply, so those
        pixels are simply taken back out of the air, writing nothing) and drops
        the selection — which the command is about to restore anyway.
        """
        if not self._ensure_current(entry):
            return False
        self._set_edit_mode(mode)  # a no-op when the window is already there
        return True

    # -- an entry's name and dirty state ---------------------------------------
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
            self._refresh_window_title()

    def _sync_write_action(self) -> None:
        """Arm File ▸ Write for whatever the current view can actually save.

        The graphic's own bytes when its pathway is writable, and *also* the file
        palette it renders - so a view-only graphic (a decompressed slice with no
        compressor) can still save the ``.pal`` it is showing, which is the one
        thing about it Ctrl+W has to offer. Re-run from _set_palette_mode, since
        loading or dropping a file palette changes the answer mid-entry.
        """
        entry = self._workspace.current
        doc = entry.doc if entry is not None else None
        writable = doc is not None and doc.pixel_config.write_enabled
        self._write_action.setEnabled(
            writable or self._linked_palette_entry() is not None
        )

    def _refresh_window_title(self) -> None:
        """Re-render the window title from what is currently open.

        When a project is loaded the title names the **project file**, not the
        graphic on screen: a project is the whole session, so switching entries
        within it shouldn't rename the window. It carries Qt's ``[*]``
        placeholder, which :meth:`_refresh_project_modified` turns into the
        platform's unsaved marker (a trailing ``*`` here, the close-button dot on
        macOS) while the session differs from the file. With no project open it
        falls back to the current entry's name (``(missing)`` when its file is
        gone), or a bare ``celPix`` when nothing is open - and carries no marker,
        since there is no project file those changes could be saved to.
        """
        if self._project_path is not None:
            self.setWindowTitle(f"celPix - {Path(self._project_path).name}[*]")
            self._refresh_project_modified()
            return
        self.setWindowModified(False)
        entry = self._workspace.current
        if entry is None:
            self.setWindowTitle("celPix")
        elif data_missing(entry):
            self.setWindowTitle(f"celPix - {entry.name} (missing)")
        else:
            self.setWindowTitle(f"celPix - {entry.name}")

    # -- the open project's dirty state -----------------------------------------
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

    # -- list commands apply through here ---------------------------------------
    def _apply_add_entry(self, entry: Entry) -> None:
        """Append ``entry`` to the workspace and show it - the application
        path for open-file/new-slice/new-bookmark/add-palette commands and
        their redos. A bookmark or palette only lands in the list; previewing
        one in the dock is the opening gesture's call, not the add's
        (:meth:`_open_palette_data`)."""
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

    # -- docks and menus --------------------------------------------------------
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
        open_palette_data.setToolTip("Add a palette file to the Palettes list")
        open_palette_data.triggered.connect(self._prompt_add_palette_file)
        file_menu.addAction(open_palette_data)

        file_menu.addSeparator()

        open_project = QAction("Open Project…", self)
        open_project.setToolTip("Open a .celpix project")
        open_project.setShortcut(QKeySequence.StandardKey.Open)  # Ctrl+O
        open_project.triggered.connect(self._open_project)
        file_menu.addAction(open_project)

        save_project = QAction("Save Project", self)
        save_project.setToolTip(
            "Save the session to a .celpix project\nReferences, not bytes"
        )
        save_project.setShortcut(QKeySequence.StandardKey.Save)  # Ctrl+S
        save_project.triggered.connect(self._save_project)
        file_menu.addAction(save_project)

        save_project_as = QAction("Save Project As…", self)
        save_project_as.setShortcut(QKeySequence.StandardKey.SaveAs)  # Ctrl+Shift+S
        save_project_as.triggered.connect(self._save_project_as)
        file_menu.addAction(save_project_as)

        self._locate_missing_action = QAction("Locate missing files…", self)
        self._locate_missing_action.setToolTip("Re-point entries whose file has moved")
        self._locate_missing_action.triggered.connect(
            lambda: self._relocate_missing(prompt_summary=False)
        )
        self._locate_missing_action.setEnabled(False)  # armed by missing files
        file_menu.addAction(self._locate_missing_action)

        file_menu.addSeparator()

        self._new_slice_action = QAction("New Slice…", self)
        self._new_slice_action.setToolTip("Mark a region of this file as its own entry")
        self._new_slice_action.triggered.connect(self._new_slice_current)
        self._new_slice_action.setEnabled(False)
        file_menu.addAction(self._new_slice_action)

        self._new_slice_from_view_action = QAction("New Slice from View", self)
        self._new_slice_from_view_action.setToolTip(
            "New slice covering the current view"
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
        self._new_bookmark_action.setToolTip("Bookmark this position and its settings")
        self._new_bookmark_action.setShortcut(QKeySequence("Ctrl+B"))
        self._new_bookmark_action.triggered.connect(self._new_bookmark_current)
        self._new_bookmark_action.setEnabled(False)
        file_menu.addAction(self._new_bookmark_action)

        self._change_container_action = QAction("Change Container…", self)
        self._change_container_action.setToolTip(
            "Change how this file is unwrapped before decoding:\n"
            "a header to skip, an interleave to undo, or none at all"
        )
        self._change_container_action.triggered.connect(self._change_container_current)
        self._change_container_action.setEnabled(False)
        file_menu.addAction(self._change_container_action)

        file_menu.addSeparator()

        self._write_action = QAction("Write", self)
        self._write_action.setToolTip("Write this file or slice back to disk")
        self._write_action.setShortcut(QKeySequence("Ctrl+W"))
        self._write_action.triggered.connect(self._write_current)
        self._write_action.setEnabled(False)
        file_menu.addAction(self._write_action)

        self._write_all_action = QAction("Write All", self)
        self._write_all_action.setToolTip("Write all unsaved files and slices")
        self._write_all_action.setShortcut(QKeySequence("Ctrl+Shift+W"))
        self._write_all_action.triggered.connect(self._write_all)
        self._write_all_action.setEnabled(False)  # armed by dirty entries
        file_menu.addAction(self._write_all_action)

        file_menu.addSeparator()

        self._build_export_menu(file_menu)

        file_menu.addSeparator()

        open_plugins = QAction("Open plugins folder…", self)
        open_plugins.setToolTip("Drop .toml presets or .py plugins here")
        open_plugins.triggered.connect(self._open_plugins_folder)
        open_plugins.setEnabled(self._plugin_dir is not None)
        file_menu.addAction(open_plugins)

        refresh = QAction("Refresh plugins", self)
        refresh.setShortcut(QKeySequence.StandardKey.Refresh)  # F5
        refresh.setToolTip("Reload plugins and re-run on the open file")
        refresh.triggered.connect(self._refresh_plugins)
        refresh.setEnabled(self._reload_plugins is not None)
        file_menu.addAction(refresh)

        file_menu.addSeparator()

        quit_action = QAction("Quit", self)
        # Spelled out rather than StandardKey.Quit: on X11 that role resolves to
        # the bare "Exit" media key, which most keyboards don't have. Ctrl+Q is
        # what the menu should promise, and Qt maps Ctrl to Cmd on macOS.
        quit_action.setShortcut(QKeySequence("Ctrl+Q"))
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        self._build_edit_menu()
        self._build_view_menu()
        self._build_navigate_menu()
        self._build_palette_menu()
        self._build_panels_menu()
        self._build_help_menu()

    def _build_help_menu(self) -> None:
        """Help ▸ the shortcut guide and About.

        Built last so the guide, which reads the finished menu bar, sees every
        other menu (see :mod:`celpix.ui.help_dialogs`).
        """
        menu = self.menuBar().addMenu("Help")
        shortcuts = QAction("Shortcuts…", self)
        shortcuts.setToolTip("Every keyboard shortcut in one page")
        shortcuts.setShortcut(QKeySequence.StandardKey.HelpContents)  # F1
        shortcuts.triggered.connect(self._show_shortcuts)
        menu.addAction(shortcuts)
        menu.addSeparator()
        about = QAction("About celPix", self)
        about.triggered.connect(self._show_about)
        menu.addAction(about)

    def _show_shortcuts(self) -> None:
        ShortcutGuide(shortcut_sections(self), self).exec()

    def _show_about(self) -> None:
        AboutDialog(self).exec()

    def _build_view_menu(self) -> None:
        """View ▸ display toggles that change how the pixels are drawn (as
        opposed to Navigate, which moves the window): the grid toggle, the
        app-wide grid style, and the zoom steps."""
        menu = self.menuBar().addMenu("View")
        # A checkable action, not a toolbar checkbox: same isChecked/setChecked/
        # toggled surface the rest of the code already drives, so the view-state
        # capture/restore paths need no special-casing.
        self._grid = QAction("Grid", self, checkable=True)
        self._grid.setToolTip("Overlay a tile grid (zoom >= 2)")
        self._grid.toggled.connect(self._on_view_change)
        # Display-only shortcut, like Palette ▸ Load from Selection: the bare "G"
        # is routed by the app-wide event filter (_handle_nav_key), which yields
        # to focused text inputs - a live shortcut here would steal it from them.
        self._grid.setShortcut(QKeySequence("G"))
        self._grid.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
        menu.addAction(self._grid)
        self._build_grid_style_menu(menu)
        menu.addSeparator()
        self._build_zoom_actions(menu)

    def _build_zoom_actions(self, view_menu) -> None:  # noqa: ANN001 - QMenu
        """View ▸ Zoom In / Zoom Out - the keyboard route to the zoom spin.

        Real shortcuts (not the event-filter kind the bare-key nav uses): the
        bare +/- are already the byte nudge, so zoom takes the platform's standard
        Ctrl combos, which nothing routes through the nav map. Ctrl+= joins Zoom
        In because the standard Ctrl++ needs Shift on most layouts.

        The wheel gesture is what the entries advertise, written into the label
        after a tab (the Navigate menu's idiom) because no QKeySequence can
        express a scroll direction. Qt renders that tab text *instead* of the
        registered shortcut, so the Ctrl combos still fire - they just aren't the
        thing in the shortcut column, and the tooltip names them so they stay
        discoverable.
        """
        zoom_in = QAction("Zoom In\tCtrl + Scroll Up", self)
        sequences = QKeySequence.keyBindings(QKeySequence.StandardKey.ZoomIn)
        sequences.append(QKeySequence("Ctrl+="))
        zoom_in.setShortcuts(sequences)
        zoom_in.setToolTip("Zoom in (Ctrl++)")
        zoom_in.triggered.connect(lambda: self._zoom_steps(1))
        view_menu.addAction(zoom_in)
        zoom_out = QAction("Zoom Out\tCtrl + Scroll Down", self)
        zoom_out.setShortcut(QKeySequence.StandardKey.ZoomOut)
        zoom_out.setToolTip("Zoom out (Ctrl+-)")
        zoom_out.triggered.connect(lambda: self._zoom_steps(-1))
        view_menu.addAction(zoom_out)

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
        save_enum_setting(GRID_STYLE_KEY, style)
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
        # The tools rail is a plain widget, not a dock, so its toggle is a plain
        # checkable action driving the widget's visibility.
        tools_toggle = QAction("Tools Panel", self, checkable=True)
        tools_toggle.setChecked(self._tools_panel.isVisible())
        tools_toggle.toggled.connect(self._tools_panel.setVisible)
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

        The container is picked here, once, from the file's name and leading
        bytes: it is a property of the file, so detecting it at open time means
        every later load reads through the same one, and the answer is on the
        entry where the user can see and change it.
        """
        existing = self._workspace.find_file(path)
        if existing is not None:
            self._activate_entry(existing)
            return
        entry = Entry(
            name=Path(path).name,
            kind=EntryKind.FILE,
            path=path,
            container_id=detect_container(self._registry, path),
        )
        self._push_command(AddEntryCommand(self, entry, f"open {entry.name}"))

    # -- reaching the user ------------------------------------------------------
    def _alert(self, message: str, *, title: str = "celPix", detail: str = "") -> None:
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
        self._alert(str(exc), title="celPix - pipeline error")

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
            title="celPix - plugin load issues",
            detail=detail,
        )
