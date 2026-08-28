"""The application main window: open pixel/palette data, view it, save it back.

Menus (File, Edit, View, Navigate, Palette, Panels, Help) over a two-column body. The
left column is the Files dock over the Palette and Tile Source docks, which share
a tab bar between them; the right is the editing surface: four bars stacked over a
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
single one of it — the widgets and docks themselves, the undo stack every mixin
pushes onto, the open project's dirty tracking, and the one modal errors reach
the user through. The work each surface does lives in its own mixin beside it
(see the package docstring); the two closest to this file are
:mod:`~celpix.ui.main_window.session`, which swaps entries in and out, and
:mod:`~celpix.ui.main_window.rendering`, which turns the current one into pixels.

The menu bar is assembled here, and the rows are written wherever the gesture
behind them lives: File, Panels and Help are in this file, because what they name
is the shell itself — the project, the docks, the app. Edit is
:mod:`~celpix.ui.main_window.selection`'s, View is
:mod:`~celpix.ui.main_window.view_menu`'s, Navigate
:mod:`~celpix.ui.main_window.navigation`'s and Palette the palette dock's. A menu
is a way into a surface, not a surface of its own.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import (
    QAction,
    QDesktopServices,
    QKeySequence,
    QPalette,
    QUndoCommand,
    QUndoStack,
)
from PySide6.QtWidgets import (
    QApplication,
    QDockWidget,
    QHBoxLayout,
    QMainWindow,
    QMessageBox,
    QScrollArea,
    QScrollBar,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from celpix.core.document import Document
from celpix.core.errors import PipelineError, Stage
from celpix.core.palette import Palette
from celpix.plugins.discovery import PluginLoadIssue
from celpix.plugins.registry import Registry, default_registry
from celpix.project import projectfile
from celpix.project.workspace import (
    Entry,
    MissingPreset,
    PaletteMode,
    SortKey,
    Workspace,
    data_missing,
    sorted_entries,
)
from celpix.ui.animation_overlay import AnimationOverlay
from celpix.ui.canvas import CANVAS_BACKGROUND, Canvas
from celpix.ui.color_editor import ColorEditorDialog
from celpix.ui.decompress_overlay import DecompressOverlay
from celpix.ui.file_list_panel import FileListPanel
from celpix.ui.font_alphabet_window import FontAlphabetWindow
from celpix.ui.help_dialogs import AboutDialog, ShortcutGuide, shortcut_sections
from celpix.ui.hex_view_panel import HexViewPanel
from celpix.ui.main_window.animation import AnimationMixin
from celpix.ui.main_window.capability_sync import CapabilitySyncMixin
from celpix.ui.main_window.clipboard_ops import ClipboardOpsMixin
from celpix.ui.main_window.color_editing import ColorEditingMixin
from celpix.ui.main_window.compression import CompressionMixin
from celpix.ui.main_window.entries import EntriesMixin
from celpix.ui.main_window.entry_clipboard import EntryClipboardMixin
from celpix.ui.main_window.font_alphabet import FontAlphabetMixin
from celpix.ui.main_window.history import HistoryMixin
from celpix.ui.main_window.interpretation import (
    InterpretationMixin,
)
from celpix.ui.main_window.navigation import NavigationMixin
from celpix.ui.main_window.palette_dock import PaletteDockMixin
from celpix.ui.main_window.palette_offset import PaletteOffsetMixin
from celpix.ui.main_window.palette_regions import PaletteRegionsMixin
from celpix.ui.main_window.palette_source import (
    DEFAULT_SESSION_PALETTE_FORMAT,
    PaletteSourceMixin,
)
from celpix.ui.main_window.palette_transfer import PaletteTransferMixin
from celpix.ui.main_window.pixel_edit import PixelEditMixin
from celpix.ui.main_window.rearrange import RearrangeMixin
from celpix.ui.main_window.rendering import RenderingMixin
from celpix.ui.main_window.selection import (
    SelectionMixin,
)
from celpix.ui.main_window.session import SessionMixin
from celpix.ui.main_window.sprite_select import SpriteSelectMixin
from celpix.ui.main_window.stamp_tool import StampToolMixin
from celpix.ui.main_window.subsprites import SubspritesMixin
from celpix.ui.main_window.text import TextMixin
from celpix.ui.main_window.tile_bytes import TileBytesMixin
from celpix.ui.main_window.tile_source_dock import TileSourceDockMixin
from celpix.ui.main_window.tilemap_bar import TilemapBarMixin
from celpix.ui.main_window.tilemap_edit import TilemapEditMixin
from celpix.ui.main_window.transfer import TransferMixin
from celpix.ui.main_window.transform import TransformMixin
from celpix.ui.main_window.view_menu import ViewMenuMixin
from celpix.ui.main_window.writing import WritingMixin
from celpix.ui.subsprite_window import SubspriteWindow
from celpix.ui.text_window import TextWindow
from celpix.ui.tools import EditMode
from celpix.ui.undo_commands import (
    GroupOrderCommand,
    RenameEntryCommand,
    ReorderEntryCommand,
)
from celpix.ui.widgets import (
    counted,
    make_action,
    show_in_file_manager,
)
from celpix.ui.window_layout import WindowLayout

# Rebuilds a registry from built-ins + the plugin folders, returning it with any
# load issues. Injected by the app so the window can hot-reload plugins without
# knowing about data dirs, the trust store, or the confirm dialog. The argument
# is the open project's path (or None), whose folder may carry project plugins -
# the window knows which project is open, the app knows where plugins live.
ReloadPlugins = Callable[["str | None"], "tuple[Registry, list[PluginLoadIssue]]"]

# Where the window's size, position and panel arrangement are stored. Its own
# group rather than `view/`: what is under it is Qt's own opaque state and not a
# preference anyone would edit by hand (celpix.ui.window_layout).
MAIN_WINDOW_LAYOUT_KEY = "layout/main-window"


class MainWindow(
    NavigationMixin,
    AnimationMixin,
    SubspritesMixin,
    TextMixin,
    FontAlphabetMixin,
    HistoryMixin,
    InterpretationMixin,
    PaletteSourceMixin,
    PaletteOffsetMixin,
    PaletteTransferMixin,
    PaletteDockMixin,
    ColorEditingMixin,
    SelectionMixin,
    ClipboardOpsMixin,
    TileBytesMixin,
    TransformMixin,
    PixelEditMixin,
    RearrangeMixin,
    PaletteRegionsMixin,
    SessionMixin,
    TilemapBarMixin,
    TilemapEditMixin,
    TileSourceDockMixin,
    StampToolMixin,
    SpriteSelectMixin,
    CapabilitySyncMixin,
    RenderingMixin,
    ViewMenuMixin,
    EntriesMixin,
    EntryClipboardMixin,
    WritingMixin,
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
        # tile instead of silently sliding onto other data (_revalidate_selection).
        self._rect_size: tuple[int, int] | None = None
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
        # And the stamp tool, whose button the same toolbar builds and whose
        # armed flag _set_edit_mode reads on its way past.
        self._init_stamp()
        # And the pinned palette regions, for the same reason: _refresh_view asks
        # them for every slot's subpalette row before it can draw anything.
        self._init_palette_regions()
        # Whether a sprite map shows its empty frame slots. Here rather than with
        # the binding bar that owns the box, because _refresh_view's view capture
        # reads it and the bar is built after this point.
        self._show_all_frames = False
        # Whether a tilemap draws palette index 0 as the backdrop. Here for the
        # same reason, and read by the same capture.
        self._transparent_zero = False
        # The visit trail, before the first entry can become current (the empty
        # state at the tail of this method already is a current-entry change).
        self._init_history()

        self._canvas = Canvas()
        self._overlay = DecompressOverlay(self)
        self._animation = AnimationOverlay(self)
        self._animation.refresh_requested.connect(self._show_animation)
        # The player's neighbour: the same file read as its parts rather than as
        # its motion. Its own Cols and Zoom, so a change to either asks for the
        # sheet to be composed again through the same path a refresh takes.
        self._subsprites = SubspriteWindow(self)
        self._subsprites.refresh_requested.connect(self._show_subsprites)
        self._text = TextWindow(self)
        # Which run of typing the next text edit belongs to, so consecutive
        # keystrokes merge into one undo step (``main_window/text.py``).
        self._text_run = 0
        # Whether a selection currently on its way *out* of the text window is
        # being applied, so the canvas selection it sets is not pushed straight
        # back in (``main_window/text.py``, ``_sync_text_selection``).
        self._text_syncing = False
        # Whether the user has shut the text window this session. A fontmap opens
        # it by itself; closing it is how that is turned off, and View ▸ Text is
        # how it comes back (``main_window/text.py``, ``_sync_text``).
        self._text_dismissed = False
        self._text.committed.connect(self._on_text_committed)
        self._text.drafted.connect(self._on_text_drafted)
        self._text.dismissed.connect(self._on_text_dismissed)
        self._text.caret_moved.connect(self._on_text_caret)
        # The text window is a `Qt.Tool` - a top-level window of its own - so the
        # Edit menu's window-context shortcuts do not reach it. Ctrl+Z there must
        # still be the session's one history, not a second one in the field.
        self._text.undo_requested.connect(self._undo_stack.undo)
        self._text.redo_requested.connect(self._undo_stack.redo)
        # The third tool window, and the one the other two are read against: a
        # fontmap opens the text beside the alphabet that decides what it says.
        self._font_alphabet = FontAlphabetWindow(self)
        self._font_alphabet_dismissed = False
        self._font_alphabet.edited.connect(self._on_font_alphabet_edited)
        self._font_alphabet.dismissed.connect(self._on_font_alphabet_dismissed)
        self._font_alphabet.tile_selected.connect(self._on_font_alphabet_tile)
        self._font_alphabet.undo_requested.connect(self._undo_stack.undo)
        self._font_alphabet.redo_requested.connect(self._undo_stack.redo)
        self._init_sprite_select()
        self._canvas.slots_selected.connect(self._on_slots_selected)
        # After slots_selected, because the two report one press and this is the
        # more specific of the answers: on a sprite object the status line should
        # end up saying which subsprite, not which square of the sheet.
        self._connect_sprite_canvas()
        self._canvas.color_picked.connect(self._on_color_picked)
        self._connect_pixel_canvas()
        self._canvas.rearrange_started.connect(self._on_rearrange_started)
        self._canvas.rearrange_moved.connect(self._on_rearrange_moved)
        self._canvas.rearrange_dropped.connect(self._on_rearrange_dropped)
        self._canvas.rearrange_cancelled.connect(self._on_rearrange_cancelled)
        self._connect_stamp_canvas()
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
        # ...and the surround answers the canvas's gestures, like the sheet's and
        # the frame's do: the grey is the canvas as far as a user is concerned,
        # and it is exactly where the pointer sits when a small picture is the one
        # wanting a zoom *in*.
        self._canvas.claim_background(scroll)

        # A file-position scrollbar: its range spans the whole file (in tiles), so
        # dragging jumps far through a large file at once. It drives the same offset
        # the buttons/keys do, snapped to whole tile-rows; _sync_nav keeps it in
        # step (with signals blocked).
        # It sits to the LEFT of the canvas and is styled as an accent-colored rail
        # so it reads as a file navigator, not one of the canvas's own scrollbars.
        # That styling is why the capability gate *hides* it on an entry with no
        # view window rather than disabling it (``capability_sync.py``).
        self._tile_offset_bar = QScrollBar(Qt.Orientation.Vertical)
        self._tile_offset_bar.setToolTip("Tile position in the file\nDrag to jump")
        self._tile_offset_bar.setStyleSheet(self._tile_offset_bar_style())
        self._tile_offset_bar.valueChanged.connect(self._on_tile_offset_bar_change)

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
        # The offset bar belongs to this row, not to the whole column: sharing the
        # canvas's row keeps it exactly as tall as the paint surface instead of
        # running up past it alongside the interpretation/transform toolbars.
        canvas_row.addWidget(self._tile_offset_bar)
        canvas_row.addWidget(scroll, 1)
        canvas_row.addWidget(self._build_tools_bar(), 0, Qt.AlignmentFlag.AlignTop)
        canvas_column.addLayout(canvas_row, 1)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addLayout(canvas_column, 1)
        # One strip under the canvas, two pages: the file-position controls, and
        # a tilemap's binding controls. Swapped rather than shown together —
        # a tilemap has no view window to move, so the offset widgets would be a
        # row of dead controls (``docs/design/tilemap-entry.md`` §8).
        self._nav_stack = QStackedWidget()
        self._navbar = self._build_navbar()
        self._tilemap_bar = self._build_tilemap_bar()
        self._nav_stack.addWidget(self._navbar)
        self._nav_stack.addWidget(self._tilemap_bar)
        layout.addWidget(self._nav_stack)
        self.setCentralWidget(central)

        self._build_files_dock()  # before _build_menus: the toggles go in menus
        self._build_palette_dock()
        self._build_tile_source_dock()  # after palette dock: tabs onto it
        self._connect_pixel_palette()  # after palette dock: needs its swatch grid
        self._build_hex_dock()
        self._build_clipboard_actions()  # before _build_menus: shared with it
        self._build_menus()
        self._build_toolbar()
        # Both after _build_toolbar: the spins exist only then. setValue clamps to
        # the spin's range and re-renders through _on_view_change.
        self._palette_panel.subpalette_row_selected.connect(self._subpalette.setValue)
        self._sync_entire_file()  # apply the restored View > Entire File to Rows
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
        # Last, and after every dock is built and placed: what it finds here is
        # the factory layout it hands back to Panels > Reset, and what it applies
        # has to win over the placement each dock did for itself
        # (celpix.ui.window_layout).
        self._window_layout = WindowLayout(self, MAIN_WINDOW_LAYOUT_KEY)
        self._window_layout.restore()

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
        self._files_panel.selection_changed.connect(self._sync_entry_scope)
        self._files_panel.remove_requested.connect(self._remove_entries)
        self._files_panel.reorder_requested.connect(self._reorder_entry)
        self._files_panel.move_requested.connect(self._move_entries)
        self._files_panel.sort_requested.connect(self._sort_entries)
        self._files_panel.copy_requested.connect(self._copy_entry)
        self._files_panel.cut_requested.connect(self._cut_entry)
        self._files_panel.paste_requested.connect(self._paste_entries)
        self._files_panel.duplicate_requested.connect(self._duplicate_entry)
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
        self._files_panel.container_info_requested.connect(self._show_container_info)
        self._files_panel.use_palette_requested.connect(self._use_palette_entry)
        self._files_panel.edit_slice_requested.connect(self._edit_slice)
        self._files_panel.edit_composite_requested.connect(self._edit_composite)
        self._files_panel.jump_to_source_requested.connect(self._jump_to_slice_source)
        self._files_panel.jump_to_bookmark_requested.connect(self._jump_to_bookmark)
        self._files_panel.bookmark_as_palette_requested.connect(
            self._use_bookmark_as_palette
        )
        self._files_panel.show_in_manager_requested.connect(self._show_entry_in_manager)
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
        # A closed entry can't be gone back to, so it leaves the visit trail.
        ws.on_removed.append(self._forget_visits)
        # Swapping the whole list is one operation, not n removals: the panel
        # and the trail drop everything in one go rather than unwinding entry by
        # entry (see Workspace.replace).
        ws.on_reset.append(self._files_panel.clear_entries)
        ws.on_reset.append(self._forget_all_visits)

    def _on_entry_added(self, entry: Entry) -> None:
        # The panel nests a slice under its parent file's item when it's open,
        # and takes its position from the list rather than deriving one: the
        # order of the rows is the user's (they drag them), so the workspace is
        # the only thing that knows it.
        parent = self._workspace.parent_of(entry)
        self._files_panel.add_entry(entry, parent, self._next_row(entry, parent))

    def _next_row(self, entry: Entry, parent: Entry | None) -> Entry | None:
        """The row ``entry``'s must go in front of, read out of the workspace.

        The panel groups rows two ways at once — nested under the file they cut
        into, and gathered under a heading by what they hold — so "the next
        entry in the list" is not by itself the next *row*. The group is
        whichever of the two this entry belongs to, and the answer is the first
        later entry in it. ``None`` (append) for the last of its group, which is
        every ordinary open and every new slice carved past the existing ones.
        """
        entries = self._workspace.entries
        later = entries[entries.index(entry) + 1 :]
        if parent is not None:
            siblings = self._workspace.children_of(parent)
            return next((e for e in later if e in siblings), None)
        return next(
            (
                e
                for e in later
                if self._workspace.parent_of(e) is None
                and e.content_kind is entry.content_kind
            ),
            None,
        )

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

    def _reorder_entry(self, entry: Entry, before: Entry | None) -> None:
        """Move a row so it sits in front of ``before`` — a drop, or a one-row
        Alt+Up/Down (:meth:`_move_entries`).

        Where it came *from* is asked of the panel rather than of the workspace,
        and that is the point of asking at all: the list is flat and the rows are
        grouped, so the neighbour a row has to be put back in front of is its
        neighbour **on screen**. Both directions of the command then say the same
        kind of thing, and the two halves of the reorder cannot disagree about
        what "back" means.
        """
        if self._applying_undo:
            return
        was = self._files_panel.next_sibling(entry)
        if was is before:
            return  # already there
        self._push_command(ReorderEntryCommand(self, entry, before=was, after=before))

    def _apply_reorder_entry(self, entry: Entry, before: Entry | None) -> None:
        if self._workspace.reorder(entry, before):
            self._files_panel.move_item(entry, before)

    def _move_entries(self, entries: list[Entry], delta: int) -> None:
        """Step every row in ``entries`` one place — Alt+Up/Down, or Move Up/Down.

        One row is spelled as the neighbour move a drag makes, so the keyboard and
        the drag stay one model operation and one undo step. Several are spelled
        as **orders**: the rows they pass have to shuffle the other way, and a
        group laid out in a given order says the result without having to describe
        the shuffling. The panel works out both, since the groups are what is on
        screen rather than anything the flat workspace list knows
        (:meth:`~celpix.ui.file_list_panel.FileListPanel.move_orders`).

        A selection can straddle groups — a palette and a file sit under different
        headings — so it can come back as several orders, which go on the stack as
        one macro: the user made one gesture.
        """
        if self._applying_undo:
            return
        if len(entries) == 1:
            can_move, before = self._files_panel.move_target(entries[0], delta)
            if can_move:
                self._reorder_entry(entries[0], before)
            return
        orders = self._files_panel.move_orders(entries, delta)
        if not orders:
            return
        text = f"move {len(entries)} entries"
        macro = len(orders) > 1
        if macro:
            self._undo_stack.beginMacro(text)
        try:
            for order in orders:
                self._push_command(
                    GroupOrderCommand(
                        self,
                        order[0],
                        text,
                        before=self._files_panel.sibling_entries(order[0]),
                        after=order,
                    )
                )
        finally:
            if macro:
                self._undo_stack.endMacro()

    def _sort_entries(self, entry: Entry, key: SortKey) -> None:
        """Put the group ``entry`` sits in into ``key`` order — a context-menu sort.

        The group comes from the panel, for the reason :meth:`_reorder_entry`
        asks it there: on screen the rows are nested and sectioned, and it is that
        arrangement — one file's children, one section's files — the user is
        asking to put in order.

        A **type** sort needs what each map's format declares its cells to be,
        which is the registry's answer and not the entry's — so it is handed in
        from here, where the same declaration already decides whether a map opens
        as a string or as a set of objects (:meth:`_tilemap_declares`).
        """
        if self._applying_undo:
            return
        was = self._files_panel.sibling_entries(entry)
        order = sorted_entries(
            was, key, layout=lambda e: str(self._tilemap_declares(e, "layout") or "")
        )
        if order == was:  # Entry is eq=False, so this compares identity
            return
        what = key.value
        self._push_command(
            GroupOrderCommand(self, entry, f"sort by {what}", before=was, after=order)
        )

    def _apply_entry_order(self, order: list[Entry]) -> None:
        """Lay one group's rows out in ``order``.

        Right to left, each row moved in front of the one that follows it: after
        every step the tail from that row on is contiguous and already right, so
        the next move only has to reach the head of it. Working the other way
        would keep shunting rows past a tail that is still in the old order.

        Spelled in the same "land in front of this row" moves a drag makes, so the
        model and the tree stay in step through the one place that knows how to
        keep them there.
        """
        for at in range(len(order) - 2, -1, -1):
            self._apply_reorder_entry(order[at], order[at + 1])

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
        self._sync_entry_scope()  # a veto that runs after every owner

    # The menu rows that act on **the entry** — the current one, or the file
    # behind it. Every one of them names exactly one, so a Files selection of
    # several has no answer to give them; Write All and Export All are not here,
    # since they are about the project rather than about a row.
    _ENTRY_SCOPED_ACTIONS = (
        "_new_slice_action",
        "_new_slice_from_view_action",
        "_new_slice_from_selection_action",
        "_new_bookmark_action",
        "_change_container_action",
        "_container_info_action",
        "_write_action",
        "_import_png_action",
        "_export_png_action",
        "_export_raw_action",
        "_export_slices_action",
    )

    def _sync_entry_scope(self) -> None:
        """Switch off the one-entry menu rows while the Files list holds several.

        A **veto and never a grant**, the same contract
        :mod:`~celpix.ui.main_window.capability_sync` runs under and for the same
        reason: these rows have owners that each weigh their own conditions, and
        this pass knows only that the question they answer has stopped having one
        answer. So it takes away and never gives back — the owner re-arming on the
        way out of the multi-row selection is what turns them on again, which is
        why every one of them ends by calling this.

        The rows carry real shortcuts (Ctrl+W, Ctrl+E), so it is not enough to
        grey them as the menu opens: a disabled QAction refuses its key too, and
        that is the half of "disabled" a user actually notices here.
        """
        # getattr, not the attribute: several of the owners below run while the
        # window is still being assembled, before the Files dock exists.
        panel = getattr(self, "_files_panel", None)
        if panel is None or not panel.has_multi_selection():
            return
        for name in self._ENTRY_SCOPED_ACTIONS:
            action = getattr(self, name, None)
            if action is not None:
                action.setEnabled(False)

    def _refresh_current_entry_row(self) -> None:
        """Re-render the current entry's row in the files list.

        Its notices live on the document, so the panel's own refresh — which runs
        when an entry is *added* — happens too early to see them: at that point no
        document exists yet. This is the second pass, from the sites that have
        just finished a load or switched which entry is on screen.
        """
        entry = self._workspace.current
        if entry is not None:
            self._files_panel.refresh_entry(entry)

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
        # Paired with the title on purpose: both follow the current entry, so
        # doing them together means the row cannot go stale at one of the several
        # sites that switch which entry is on screen.
        self._refresh_current_entry_row()
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
        return projectfile.project_dict(self._workspace, self._project_path)

    def _project_is_dirty(self) -> bool:
        """True when the open project differs from what is on disk.

        **Which entry is shown is not a change.** It is still written - a project
        reopens on the view you left - but browsing to another file, to read it
        or to take a palette off it, is a pointer at the work rather than a
        change to how the work is set up. Marking the project unsaved for it puts
        a save prompt in front of anyone who merely looked around, which is the
        same reasoning that keeps the tile selection out of the file altogether
        (:func:`~celpix.project.projectfile._entry_dict`). Everything else the
        document holds counts, including the display-only state (a rearrangement,
        pinned regions) that deliberately doesn't dirty the *entry*.
        """
        if self._saved_project is None:
            return False
        snapshot = self._project_snapshot()
        if snapshot is None:
            return True  # a project baseline with no path to compare it against
        return self._comparable_project(snapshot) != self._comparable_project(
            self._saved_project
        )

    @staticmethod
    def _comparable_project(document: dict[str, object]) -> dict[str, object]:
        """``document`` minus the fields a change to is not a project change."""
        return {key: value for key, value in document.items() if key != "current"}

    # -- list commands apply through here ---------------------------------------
    def _apply_add_entry(self, entry: Entry) -> None:
        """Add ``entry`` to the workspace and show it - the application
        path for open-file/new-slice/new-bookmark/add-palette commands and
        their redos. A bookmark or palette only lands in the list; previewing
        one in the dock is the opening gesture's call, not the add's
        (:meth:`_open_palette_data`).

        The end of the list, except a new slice or bookmark, which lands in
        offset order among its parent's children — where the workspace says, so
        that the one rule about it lives with the list it is about
        (:meth:`~celpix.project.workspace.Workspace.add_index_for`)."""
        self._workspace.insert(entry, self._workspace.add_index_for(entry))
        self._sync_locate_action()
        if entry.kind.has_document:
            self._activate_entry(entry)

    def _apply_close_entry(self, entry: Entry) -> None:
        """Take ``entry`` (and, for a file, its slices) out of the workspace;
        the current view repoints to a neighbour via the workspace."""
        # Asked before the close, while the bindings still resolve: every map
        # drawing through this file (or through one of its slices) holds a decoded
        # copy of the art and would go on showing it.
        going = [entry, *self._workspace.children_of(entry)]
        orphaned = self._maps_drawing_from(going)
        self._workspace.close(entry)
        self._sync_locate_action()
        # The banks first: a composite losing a piece has to be re-assembled
        # before the maps drawing through it are re-read, or they would each
        # borrow the stale join and then be right about it (
        # ``docs/design/composite-entry.md``). Maps bound to a rebuilt bank join
        # the list the same way maps bound to the closed entry did.
        rebuilt = self._reassemble_composites(going)
        self._reresolve_bound_art(orphaned + self._maps_drawing_from(rebuilt))
        # Closing the bank a map is painted through takes its pixels away without
        # the view moving, so the mode has to be re-asked here as well as on an
        # entry switch (``session.SessionMixin._drop_unavailable_edit_mode``).
        self._drop_unavailable_edit_mode()
        # And the controls that offer the mode with it. The drop above only acts
        # when pixel mode was *on*; from tile mode nothing ran, and a close that
        # leaves the view where it was renders nothing of its own — so Toggle Edit
        # Mode stayed armed over a map with nothing left to paint into. Still
        # needed after the re-resolve above: a map whose re-read failed keeps the
        # document it had, and the binding under it is gone either way.
        self._sync_edit_actions()

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
        self._sync_locate_action()
        # The mirror of the close, and it has to be asked *here* rather than
        # captured over there: the bindings only resolve again now the entries are
        # back, and it is that resolution which tells a map its art has returned.
        # Before the activation below, so a map that is about to come on screen is
        # already drawing the tiles it had rather than a page of placeholders.
        restored = [e for _, e in victims]
        rebuilt = self._reassemble_composites(restored)
        self._reresolve_bound_art(
            self._maps_drawing_from(restored) + self._maps_drawing_from(rebuilt)
        )
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
        # Past both gates, so this really is the last look at the layout: flush
        # whatever the delayed write is still holding, or a panel dragged and
        # then quit on in the same breath goes back to where it was.
        self._window_layout.save()
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
        # "&" in an action's text marks its mnemonic - the letter that picks the
        # entry while the menu is open, and the reason several are not the
        # obvious first letter: the letters have to be unique within a menu, and
        # an action shown in more than one (the canvas's right-click menu shares
        # File's and Edit's) has to keep clear of every menu it appears in. Where
        # a free letter matches the action's shortcut it takes it (Write/Ctrl+W,
        # Edit File Container/Ctrl+E, Quit/Ctrl+Q).
        file_menu = self.menuBar().addMenu("&File")

        make_action(self, "Open &pixel data…", self._open_pixel, menu=file_menu)
        make_action(
            self,
            "Open palette &data…",
            self._prompt_add_palette_file,
            menu=file_menu,
            tip="Add a palette file to the Palettes list",
        )
        # Mnemonic "t": "d" is the palette row's and "p" the pixel row's.
        make_action(
            self,
            "Open &tilemap data…",
            self._open_tilemap,
            menu=file_menu,
            tip="Read a file as a map of tile indices\n"
            "Bind it to its tiles in the bar under the canvas",
        )

        file_menu.addSeparator()

        make_action(
            self,
            "New Pro&ject",
            self._new_project,
            menu=file_menu,
            tip="Close everything and start a fresh session",
            shortcut=QKeySequence.StandardKey.New,  # Ctrl+N
        )
        make_action(
            self,
            "&Open Project…",
            self._open_project,
            menu=file_menu,
            tip="Open a .celpix project",
            shortcut=QKeySequence.StandardKey.Open,  # Ctrl+O
        )

        self._build_recent_menu(file_menu)

        make_action(
            self,
            "&Save Project",
            self._save_project,
            menu=file_menu,
            tip="Save the session to a .celpix project\nReferences, not bytes",
            shortcut=QKeySequence.StandardKey.Save,  # Ctrl+S
        )
        make_action(
            self,
            "Save Project &As…",
            self._save_project_as,
            menu=file_menu,
            shortcut=QKeySequence.StandardKey.SaveAs,  # Ctrl+Shift+S
        )
        self._locate_missing_action = make_action(
            self,
            "Locate &missing files…",
            lambda: self._relocate_missing(prompt_summary=False),
            menu=file_menu,
            tip="Re-point entries whose file has moved",
            enabled=False,  # armed by missing files
        )

        file_menu.addSeparator()

        # The four "carve something out of here" rows, each armed by what the
        # current entry can give it (:meth:`_sync_entry_actions`).
        self._new_slice_action = make_action(
            self,
            "&New Slice…",
            self._new_slice_current,
            menu=file_menu,
            tip="Mark a region of this file as its own entry",
            enabled=False,
        )
        self._new_slice_from_view_action = make_action(
            self,
            "New Slice from &View",
            self._new_slice_from_view,
            menu=file_menu,
            tip="New slice covering the current view",
            enabled=False,
        )
        self._new_slice_from_selection_action = make_action(
            self,
            "New Slice &from Selection",
            self._new_slice_from_selection,
            menu=file_menu,
            tip="New slice covering the selected tile range",
            enabled=False,
        )
        self._new_bookmark_action = make_action(
            self,
            "New &Bookmark",
            self._new_bookmark_current,
            menu=file_menu,
            tip="Bookmark this position and its settings",
            shortcut=QKeySequence("Ctrl+B"),
            enabled=False,
        )
        # Always available, unlike the four above: a composite view is assembled
        # out of entries rather than carved out of the one on screen, so it needs
        # nothing of the current view — not even an entry.
        #
        # **No mnemonic** (as for Open Project Folder below), and not for want of
        # trying: every letter of "New Composite View…" is spoken for here, "V"
        # most pointedly of all — New Slice from &View holds it, spelling the
        # same word. A silent clash (Qt cycles between the two rows instead of
        # activating either) is worse than a row reached by arrowing to it.
        make_action(
            self,
            "New Composite View…",
            self._new_composite,
            menu=file_menu,
            tip="Assemble one tile source from several files and slices,\n"
            "so a tilemap can index the window the hardware loaded",
        )

        self._change_container_action = make_action(
            self,
            "&Edit File Container…",
            self._change_container_current,
            menu=file_menu,
            tip="Change how this file is unwrapped before decoding:\n"
            "a header to skip, an interleave to undo, or none at all",
            shortcut=QKeySequence("Ctrl+E"),
            enabled=False,
        )
        # Mnemonic "i": beside the container it reports on, since the two answer
        # the same question from opposite ends — what is this file being read as,
        # and what did that reading make of it.
        self._container_info_action = make_action(
            self,
            "Container &Info…",
            self._container_info_current,
            menu=file_menu,
            tip="What this file's container read out of it:\n"
            "the header fields it used, and what it passed on",
            enabled=False,
        )

        file_menu.addSeparator()

        self._write_action = make_action(
            self,
            "&Write",
            self._write_current,
            menu=file_menu,
            tip="Write this file or slice back to disk, with the\n"
            "palette file it shows and, on a map, the tiles\n"
            "it borrows if they have been painted on",
            shortcut=QKeySequence("Ctrl+W"),
            enabled=False,
        )
        self._write_all_action = make_action(
            self,
            "Write A&ll",
            self._write_all,
            menu=file_menu,
            tip="Write all unsaved files and slices",
            shortcut=QKeySequence("Ctrl+Shift+W"),
            enabled=False,  # armed by dirty entries
        )

        file_menu.addSeparator()

        self._build_export_menu(file_menu)

        file_menu.addSeparator()

        # No mnemonic, for the same reason as New Composite View above: every
        # letter of "Open Project Folder" is taken in this menu - "F" by New
        # Slice &from Selection, "O"/"P"/"J" by the project rows themselves.
        self._open_project_folder_action = make_action(
            self,
            "Open Project Folder…",
            self._open_project_folder,
            menu=file_menu,
            tip="Show the open .celpix project in a file manager,\n"
            "where its plugins/ folder and its files live",
            enabled=False,  # armed by an open project
        )
        # A project can be opened, closed or first saved without a menu rebuild -
        # recompute the row whenever the File menu opens (as Export does).
        file_menu.aboutToShow.connect(self._sync_project_folder_action)
        make_action(
            self,
            "Open plu&gins folder…",
            self._open_plugins_folder,
            menu=file_menu,
            tip="Drop .toml presets or .py plugins here",
            enabled=self._plugin_dir is not None,
        )
        make_action(
            self,
            "&Refresh plugins",
            self._refresh_plugins,
            menu=file_menu,
            tip="Reload plugins - yours and the open project's -\nand re-run "
            "on the open file",
            shortcut=QKeySequence.StandardKey.Refresh,  # F5
            enabled=self._reload_plugins is not None,
        )

        file_menu.addSeparator()

        make_action(
            self,
            "&Quit",
            self.close,
            menu=file_menu,
            # Spelled out rather than StandardKey.Quit: on X11 that role resolves
            # to the bare "Exit" media key, which most keyboards don't have.
            # Ctrl+Q is what the menu should promise, and Qt maps Ctrl to Cmd on
            # macOS.
            shortcut=QKeySequence("Ctrl+Q"),
        )

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
        menu = self.menuBar().addMenu("&Help")
        shortcuts = QAction("&Shortcuts…", self)
        shortcuts.setToolTip("Every keyboard shortcut in one page")
        shortcuts.setShortcut(QKeySequence.StandardKey.HelpContents)  # F1
        shortcuts.triggered.connect(self._show_shortcuts)
        menu.addAction(shortcuts)
        menu.addSeparator()
        about = QAction("&About celPix", self)
        about.triggered.connect(self._show_about)
        menu.addAction(about)

    def _show_shortcuts(self) -> None:
        ShortcutGuide(shortcut_sections(self), self).exec()

    def _show_about(self) -> None:
        AboutDialog(self).exec()

    def _build_panels_menu(self) -> None:
        """Panels ▸ show/hide the dockable panels (Files, Palette, Tile Source,
        Hex)."""
        menu = self.menuBar().addMenu("Pane&ls")
        files_toggle = self._files_dock.toggleViewAction()
        files_toggle.setText("&Files Panel")
        menu.addAction(files_toggle)
        palette_toggle = self._palette_dock.toggleViewAction()
        palette_toggle.setText("&Palette Panel")
        menu.addAction(palette_toggle)
        # Listed beside Palette, which it shares a tab bar with: closing one
        # leaves the other holding the space, and the way back to either is here.
        tile_source_toggle = self._tile_source_dock.toggleViewAction()
        tile_source_toggle.setText("&Tile Source Panel")
        menu.addAction(tile_source_toggle)
        # The tools rail is not listed: it is a plain widget rather than a dock,
        # always on screen beside the canvas, and greys itself out whenever pixel
        # editing is off - so there is nothing for a show/hide entry to do.
        hex_toggle = self._hex_dock.toggleViewAction()
        hex_toggle.setText("&Hex Panel")
        menu.addAction(hex_toggle)
        # The way back, and the reason it has to exist: the arrangement is
        # remembered now, so a panel dropped somewhere unusable stays unusable
        # across a restart - and a dock shrunk past its own separator cannot be
        # dragged back out by hand.
        menu.addSeparator()
        reset = menu.addAction("&Reset Panel Layout")
        reset.triggered.connect(self._reset_panel_layout)

    def _reset_panel_layout(self) -> None:
        """Panels ▸ Reset Panel Layout — the docks as a fresh install has them."""
        self._window_layout.reset()
        self.statusBar().showMessage("Panel layout reset.")

    def _sync_project_folder_action(self) -> None:
        """Only a session with a project file behind it has a folder to open."""
        self._open_project_folder_action.setEnabled(self._project_path is not None)

    def _open_project_folder(self) -> None:
        """File ▸ Open Project Folder — the folder the .celpix file sits in.

        Reveals the project file rather than just opening its folder: the folder
        is usually the user's own working directory, and the project is what
        they came looking for in it.
        """
        if self._project_path is None:
            return
        if not show_in_file_manager(self._project_path):
            self.statusBar().showMessage(
                f"Cannot show {self._project_path} in a file manager."
            )

    def _open_plugins_folder(self) -> None:
        if self._plugin_dir is None:
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(self._plugin_dir))
        self._alert_plugin_issues()

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

    def _confirm(
        self,
        message: str,
        *,
        title: str = "celPix",
        accept: str = "OK",
        warn: bool = False,
    ) -> bool:
        """Ask before doing it; True where the user said go ahead.

        The counterpart of :meth:`_alert` and the single surface for its kind:
        a gesture whose consequence reaches past what the user is looking at -
        one control re-declaring a *different* entry, an untick that discards a
        table - stops here first. Cancel is the answer to a dialog dismissed any
        other way, so a stray Escape can only ever leave things as they were.

        ``accept`` labels the button that does the thing, because "OK" says
        nothing about what is about to happen. ``warn`` marks it as the lossy
        answer and puts the focus ring on Cancel, so Enter hit blind keeps the
        work.
        """
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning if warn else QMessageBox.Icon.Question)
        box.setWindowTitle(title)
        box.setText(message)
        role = (
            QMessageBox.ButtonRole.DestructiveRole
            if warn
            else QMessageBox.ButtonRole.AcceptRole
        )
        go = box.addButton(accept, role)
        cancel = box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(cancel if warn else go)
        box.exec()
        return box.clickedButton() is go

    def _report(self, exc: PipelineError) -> None:
        """Surface a pipeline failure. Thin wrapper over :meth:`_alert` kept for
        the many call sites that already hold a :class:`PipelineError`."""
        self._alert(str(exc), title="celPix - pipeline error")

    # What each stage's presets are called in a sentence aimed at the user.
    # "interpret-tilemap" is the pipeline's word for the stage, not a thing the
    # picker the user chose from is labelled with.
    _PRESET_KINDS = {
        Stage.INTERPRET_PIXEL: "pixel format",
        Stage.INTERPRET_PALETTE: "palette format",
        Stage.INTERPRET_TILEMAP: "tilemap format",
    }

    def _alert_missing_presets(self, missing: list[MissingPreset]) -> None:
        """Modal naming the formats entries wanted that this build hasn't got.

        The Interpret-stage counterpart of :meth:`_alert_plugin_issues`, and the
        reason a missing format is a dialog rather than a per-entry notice: the
        substitution has already been written into the entries
        (:func:`~celpix.project.workspace.repair_presets`), so the user has a
        decision to make about *this* project before they touch it — install the
        plugin and reopen, or accept the fallback. A notice they might read later
        is no use for a choice that expires at the next save.
        """
        if not missing:
            return
        detail = "\n".join(
            f"• {item.entry.name}: "
            f"{self._PRESET_KINDS.get(item.stage, item.stage.value)} "
            f"{item.wanted} -> {item.used or 'none'}"
            for item in missing
        )
        self._alert(
            f"{len(missing)} format(s) this project uses aren't installed. Those "
            "entries fall back to a default, so what they show is not what the "
            "files hold. Install the plugin that provides them and reopen the "
            "project - saving it as it stands writes the fallbacks in place of "
            "the formats it named.",
            title="celPix - missing formats",
            detail=detail,
        )

    def _alert_plugin_issues(self) -> None:
        """Say what in the plugins folder did not load - at startup, after a
        refresh, and again from File ▸ Open plugins folder.

        Two surfaces, because there are two kinds of "did not load". A plugin
        that **broke** is news: the user meant it to run, it doesn't, and a modal
        is the only thing they reliably read. A plugin they **declined** at the
        trust prompt did what they asked, and it declines again on every launch
        and every F5 for as long as the answer stands - so a modal there is the
        app arguing with a decision, once per start, forever. That one gets the
        status bar, which says the same thing to anyone who wonders where their
        plugin went and nothing to anyone who doesn't.

        Declined files still appear in a failure modal's details when there is
        one to show: the list is "what is not running", and leaving them out of
        it is how a user hunts a plugin that is sitting right there.
        """
        failed = [i for i in self._plugin_issues if not i.declined]
        declined = [i for i in self._plugin_issues if i.declined]
        if not failed:
            if declined:
                self.statusBar().showMessage(
                    f"{counted(len(declined), 'code plugin')} not run - the trust "
                    "prompt was declined. File ▸ Refresh plugins asks again."
                )
            return
        detail = "\n".join(f"• {i.path}: {i.message}" for i in [*failed, *declined])
        self._alert(
            f"{counted(len(failed), 'plugin')} failed to load. The rest of "
            "the app works normally; see the details, or File ▸ Open plugins "
            "folder.",
            title="celPix - plugin load issues",
            detail=detail,
        )
