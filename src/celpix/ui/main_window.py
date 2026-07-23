"""The application main window: open pixel/palette data, view it, save it back.

Menus (File, Edit, View, Navigate, Palette, Panels), a control strip (pixel
format, palette format, columns, rows, zoom, subpalette row) plus an arrangement
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
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QEvent, QSettings, Qt, QUrl
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QDesktopServices,
    QDragEnterEvent,
    QDropEvent,
    QImage,
    QKeySequence,
    QPalette,
    QUndoCommand,
    QUndoStack,
)
from PySide6.QtWidgets import (
    QAbstractSlider,
    QAbstractSpinBox,
    QApplication,
    QCheckBox,
    QComboBox,
    QDockWidget,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QScrollBar,
    QSpinBox,
    QStyle,
    QToolBar,
    QTreeWidget,
    QVBoxLayout,
    QWidget,
)

from celpix.core import ceil_div, emustate
from celpix.core.address import (
    BANK_PRESETS,
    BankLayout,
    BankPreset,
    SplitBankLayout,
    format_hex,
    parse_hex,
)
from celpix.core.arrangement import (
    ARRANGEMENT_PRESETS,
    ArrangementPreset,
    BlockLayout,
    arrangement_preset_for,
    compose_window,
    reflow_2d,
)
from celpix.core.context import (
    KEY_COMPRESSED_SIZE,
    KEY_DECOMPRESS_COMPLETE,
    KEY_DECOMPRESS_PARTIAL,
    PipelineContext,
)
from celpix.core.document import Document, ViewOptions
from celpix.core.errors import Pathway, PipelineError, Stage
from celpix.core.palette import Palette
from celpix.pipeline import pipeline
from celpix.pipeline.pathway import PathwayConfig
from celpix.plugins.base import FileRef
from celpix.plugins.builtins.lz16 import KEY_LZ16_ROWS
from celpix.plugins.discovery import PluginLoadIssue
from celpix.plugins.registry import Registry, default_registry
from celpix.project import projectfile
from celpix.project.workspace import (
    Entry,
    EntryKind,
    EntrySession,
    PaletteSource,
    SliceParams,
    Workspace,
    backfill_slice_length,
    data_missing,
    default_slice_name,
    missing_paths,
    palette_source_for,
    pixel_config_for,
    relocate_path,
)
from celpix.ui import render_bridge
from celpix.ui.canvas import CANVAS_BACKGROUND, Canvas, GridStyle
from celpix.ui.decompress_overlay import DecompressOverlay
from celpix.ui.file_list_panel import FileListPanel
from celpix.ui.hex_view_panel import BYTES_PER_ROW, HexViewPanel
from celpix.ui.palette_panel import PalettePanel
from celpix.ui.slice_dialog import SliceDialog
from celpix.ui.undo_commands import (
    AddEntryCommand,
    OffsetMoveCommand,
    PaletteCommand,
    PaletteState,
    PixelConfigCommand,
    RemoveEntriesCommand,
    RenameEntryCommand,
    SliceEditCommand,
)
from celpix.ui.widgets import (
    CommittingLineEdit,
    CompactComboBox,
    select_combo_data,
)

# Rebuilds a registry from built-ins + the current plugin folder, returning it
# with any load issues. Injected by the app so the window can hot-reload plugins
# without knowing about data dirs, the trust store, or the confirm dialog.
ReloadPlugins = Callable[[], "tuple[Registry, list[PluginLoadIssue]]"]


class MainWindow(QMainWindow):
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
        # document (or None with nothing open) — the single-active-view model:
        # switching entries swaps the document under the one canvas.
        self._workspace = Workspace()
        self._doc: Document | None = None
        # The unified per-launch undo history: one chronological stack for
        # every surface — files-pane structure, per-document config, view
        # moves, future pixel/colour edits (docs/design/undo-redo.md).
        # Document-scoped commands re-activate their entry before applying.
        self._undo_stack = QUndoStack(self)
        # True while a command's undo/redo is applying state — push sites bail
        # on it, so an apply can never cascade into pushing a second command.
        self._applying_undo = False
        # The .celpix file this session was loaded from / last saved to, so
        # File ▸ Save Project can rewrite it without re-asking for a path.
        self._project_path: str | None = None
        # Where the palette comes from: "default" (the generated fallback),
        # "file" (Open palette…), "offset" (read from the pixel file), or
        # "emulator" (imported from an emulator save state). The dock's mode
        # dropdown is a view of this member.
        self._palette_mode = "default"
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
        # The selected tile range as absolute, inclusive tile indices (they
        # survive scrolling; the canvas only paints the highlight while it is
        # inside the window). A click selects one tile (first == last); a drag
        # spans the linear run between press and pointer. ``_selected_tile``
        # is the range start — the single "selected tile" every one-tile
        # consumer (palette-from-selection, the session) reads.
        self._selected_tile: int | None = None
        self._selected_last: int | None = None
        # Compression navigation: byte position right after the structure in
        # view (the Jump-to-Next target, None = end unknown/invalid), and the
        # scan interlock (the Scan button doubles as Stop while one runs).
        self._next_structure: int | None = None
        # The complete structure in view as (start byte position, byte extent)
        # — the promote-to-slice source. Kept separately from _next_structure,
        # which is deliberately None when the structure ends at end-of-file
        # (nowhere to jump) even though promoting it is still valid.
        self._structure_extent: tuple[int, int] | None = None
        self._scanning = False
        self._scan_stop = False

        self._canvas = Canvas()
        self._overlay = DecompressOverlay(self)
        self._canvas.tiles_selected.connect(self._on_tiles_selected)
        # ClickFocus so clicking the view takes focus off any dropdown/spin box (which
        # would otherwise keep the arrow keys), letting navigation resume. Navigation
        # itself is window-wide via eventFilter, not tied to canvas focus.
        self._canvas.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        scroll = QScrollArea()
        scroll.setWidget(self._canvas)
        # Pin the (small) window to the top-left; the scroll area only scrolls now
        # when zoom makes the window itself larger than the viewport.
        scroll.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        # The neutral surround around the rendered pixels — the same colour the
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
        # It sits to the LEFT of the canvas and is styled as an accent-coloured rail
        # so it reads as a file navigator, not one of the canvas's own scrollbars.
        self._offset_bar = QScrollBar(Qt.Orientation.Vertical)
        self._offset_bar.setToolTip("File position — drag to jump")
        self._offset_bar.setStyleSheet(self._offset_bar_style())
        self._offset_bar.valueChanged.connect(self._set_offset)

        view_row = QHBoxLayout()
        view_row.setContentsMargins(0, 0, 0, 0)
        view_row.setSpacing(0)
        view_row.addWidget(self._offset_bar)
        view_row.addWidget(scroll, 1)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addLayout(view_row, 1)
        layout.addWidget(self._build_navbar())
        self.setCentralWidget(central)

        self._build_files_dock()  # before _build_menus: the toggles go in menus
        self._build_palette_dock()
        self._build_hex_dock()
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
        # line lost behind the next message — surface it as a modal at startup.
        self._alert_plugin_issues()

    # -- construction ------------------------------------------------------
    @property
    def _has_real_palette(self) -> bool:
        """Whether a real palette is loaded (file/offset/emulator) vs the
        generated fallback. Derived from the load mode so the two can never
        diverge — any non-``default`` mode means real colours are in play.
        """
        return self._palette_mode != "default"

    def _build_files_dock(self) -> None:
        """The left-side open-files dock, mirroring the workspace model."""
        self._files_panel = FileListPanel()
        self._files_panel.entry_activated.connect(self._activate_entry)
        self._files_panel.remove_requested.connect(self._remove_entry)
        self._files_panel.write_requested.connect(self._write_entry_checked)
        self._files_panel.new_slice_requested.connect(self._new_slice_for)
        self._files_panel.new_slice_from_view_requested.connect(
            self._new_slice_from_view_for
        )
        self._files_panel.new_slice_from_selection_requested.connect(
            self._new_slice_from_selection_for
        )
        self._files_panel.new_bookmark_requested.connect(self._new_bookmark_for)
        self._files_panel.edit_slice_requested.connect(self._edit_slice)
        self._files_panel.jump_to_source_requested.connect(self._jump_to_slice_source)
        self._files_panel.jump_to_bookmark_requested.connect(self._jump_to_bookmark)
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
        # references are missing — keep the Locate menu's enabled state honest.
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

        When byte-editing commands land, per-entry dirty tracking hooks in
        here-abouts: such commands flip an entry revision counter in
        redo/undo, compared against the revision recorded at write time —
        today's commands never change file bytes, so dirty stays manual.
        """
        self._undo_stack.push(command)

    def _ensure_current(self, entry: Entry) -> bool:
        """Make ``entry`` the current view for a document-scoped command.

        Undoing a change made in another entry first switches back to it, so
        the revert happens where the user can see it. False when activation
        fails (vanished file) — the command then skips its apply.
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
            self.setWindowTitle(f"Celpix — {name}")

    def _apply_add_entry(self, entry: Entry) -> None:
        """Append ``entry`` to the workspace and show it — the application
        path for open-file/new-slice/new-bookmark commands and their redos.
        A bookmark only lands in the list; there is nothing of it to show."""
        self._workspace.insert(entry, len(self._workspace.entries))
        if entry.kind is not EntryKind.BOOKMARK:
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
        """Switch the view to ``entry`` — every activation path funnels here."""
        if entry is None or entry is self._workspace.current:
            return
        if entry.kind is EntryKind.BOOKMARK:
            return  # no view of its own — selecting one in the list is inert
        if data_missing(entry):
            # The file moved: make it current anyway, but show the disabled
            # unavailable state (no _load_entry, so no pipeline-error alert —
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
        # Arm arrow-key navigation on the fresh view — but not when the list is
        # itself being browsed with the arrow keys, or focus would be yanked
        # away from the very keys the user is navigating with.
        if not self._files_panel.is_key_navigating():
            self._canvas.setFocus()
        if fresh:
            message = f"Loaded {entry.doc.tile_count} tiles from {entry.name}"
            note = self._partial_tile_note()
            self.statusBar().showMessage(f"{message} — {note}" if note else message)

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

    def _load_entry(self, entry: Entry) -> bool:
        """Load ``entry``'s document through the pipeline; False (reported) on
        failure. Runs on first activation and again whenever the cached
        document was invalidated by a save into the same file."""
        if entry.session is None:
            entry.session = self._seed_session(entry)
        session = entry.session
        header = (
            session.header_length
            if entry.kind is EntryKind.FILE and session.headered
            else 0
        )
        cfg = pixel_config_for(entry, session.pixel_preset_id, header, self._registry)
        try:
            px = pipeline.load_pixel_data(cfg, self._registry)
        except PipelineError as exc:
            self._report(exc)
            return False
        if backfill_slice_length(entry, px.ctx):
            # The decompressor discovered the slice's true extent: rebuild the
            # config bounded by it, so save-back is slot-enforced from now on.
            cfg = pixel_config_for(
                entry, session.pixel_preset_id, header, self._registry
            )
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
        palette — a project load never fails on it.
        """
        doc = entry.doc
        assert doc is not None and entry.session is not None
        if entry.pending_view is not None:
            doc.view = entry.pending_view
            entry.pending_view = None
        source, entry.pending_palette = entry.pending_palette, None
        if source is not None:
            self._restore_palette_source(entry, source)

    def _restore_palette_source(self, entry: Entry, source: PaletteSource) -> bool:
        """Load ``source`` onto ``entry``'s document palette; True on success.

        Shared by first-load restore and post-relocation reload. An external
        palette whose file is missing degrades **quietly**: the entry keeps its
        palette_mode for display, renders on the default palette, and stashes the
        source on ``missing_palette`` so Locate missing files can re-point it and
        save keeps the reference. Any other failure degrades to the default
        palette with an alert.
        """
        doc, session = entry.doc, entry.session
        assert doc is not None and session is not None
        if source.colors is not None:
            doc.palette = Palette(source.colors)
            entry.missing_palette = None
            return True
        if source.path is not None and not Path(source.path).exists():
            # The file moved: hold this mode on the default palette and remember
            # the source. No alert — the files-list highlight signals it instead.
            entry.missing_palette = source
            doc.palette = self._fallback_palette()
            return False
        try:
            if session.palette_mode == "emulator" and source.path is not None:
                # Re-detect the save state: the palette offset and the console's
                # codec are derived from the file, not carried in the project.
                _fmt, cfg = self._emulator_palette_config(source.path)
            elif source.path is not None:  # an external palette file
                cfg = PathwayConfig(
                    source=FileRef(source.path, offset=source.offset),
                    interpret_preset_id=session.palette_preset_id,
                )
            else:  # palette bytes at an offset in the entry's own file
                ref = self._selection_palette_source(
                    doc.pixel_config.source.path,
                    source.offset,
                    session.palette_preset_id,
                )
                if ref is None:
                    raise PipelineError(
                        Stage.READ,
                        Pathway.PALETTE,
                        "not enough data at the palette offset",
                    )
                cfg = PathwayConfig(
                    source=ref,
                    interpret_preset_id=session.palette_preset_id,
                    write_enabled=False,
                )
            doc.palette, doc.palette_ctx = pipeline.load_palette(cfg, self._registry)
            doc.palette_config = cfg
            entry.missing_palette = None
            return True
        except (PipelineError, OSError, emustate.StateError) as exc:
            session.palette_mode = "default"
            entry.missing_palette = None
            self._alert(
                f"{entry.name}: palette not restored, using the default "
                f"palette instead.\n\n{exc}",
                title="Celpix — palette",
            )
            return False

    def _seed_session(self, entry: Entry) -> EntrySession:
        """A new entry's starting UI state, seeded from the live toolbar so a
        freshly opened file keeps the codec the user is working in. A slice's
        preview combo starts at none — its bytes are already decompressed."""
        return EntrySession(
            pixel_preset_id=self._pixel_preset_id(),
            palette_preset_id=self._palette_preset_id(),
            compression_id=(
                "decompress.none"
                if entry.kind is EntryKind.SLICE
                else self._compression.currentData() or "decompress.none"
            ),
            headered=entry.kind is EntryKind.FILE and self._headered.isChecked(),
            header_length=self._header_len.value(),
        )

    def _capture_session(self) -> None:
        """Snapshot the live toolbar/view state into the current entry, so
        switching back later restores exactly this setup."""
        entry = self._workspace.current
        # A missing (unavailable) entry has no live document driving the
        # widgets, so there is nothing to snapshot — capturing here would
        # overwrite its restored session with stale, disabled widget values.
        if entry is None or entry.doc is None:
            return
        entry.doc.view.tile_offset = self._offset
        entry.doc.view.byte_nudge = self._nudge
        entry.session = EntrySession(
            pixel_preset_id=self._pixel_preset_id(),
            palette_preset_id=self._palette_preset_id(),
            palette_mode=self._palette_mode,
            compression_id=self._compression.currentData() or "decompress.none",
            headered=self._headered.isChecked(),
            header_length=self._header_len.value(),
            selected_tile=self._selected_tile,
            selected_last=self._selected_last,
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
        for combo, data in (
            (self._pixel_preset, session.pixel_preset_id),
            (self._palette_preset, session.palette_preset_id),
            (self._compression, session.compression_id),
            (self._block_order, view.block_order),
        ):
            select_combo_data(combo, data)
        for spin, value in (
            (self._columns, view.columns),
            (self._rows, view.rows),
            (self._zoom, view.zoom),
            (self._subpalette, view.subpalette_row),
            (self._header_len, session.header_length),
            (self._block_cols, view.block_columns),
            (self._block_rows, view.block_rows),
        ):
            spin.blockSignals(True)
            spin.setValue(value)
            spin.blockSignals(False)
        for check, value in (
            (self._grid, view.show_grid),
            (self._headered, session.headered),
            (self._two_d, view.two_dimensional),
        ):
            check.blockSignals(True)
            check.setChecked(value)
            check.blockSignals(False)
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
        self._update_selection_actions()
        self._set_palette_mode(session.palette_mode)
        self._write_action.setEnabled(entry.doc.pixel_config.write_enabled)
        # Only whole files spawn slices and bookmarks — neither nests (and a
        # file's byte stream is always raw, so its positions map straight to
        # file offsets).
        self._new_slice_action.setEnabled(is_file)
        self._new_slice_from_view_action.setEnabled(is_file)
        self._new_bookmark_action.setEnabled(is_file)
        self.setWindowTitle(f"Celpix — {entry.name}")

    def _clear_document_view(self) -> None:
        """Blank the canvas and disable every document-bound action — shared by
        the nothing-open and missing-file (unavailable) states."""
        self._doc = None
        self._selected_tile = None
        self._selected_last = None
        self._canvas.set_selection(None)
        self._update_selection_actions()
        self._canvas.set_image(QImage())
        self._overlay.hide_overlay()
        self._hex_panel.clear()
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
        for bar in (self._codecs_toolbar, self._arrange_toolbar, self._view_toolbar):
            bar.setEnabled(enabled)
        self._palette_dock.setEnabled(enabled)

    def _show_empty(self) -> None:
        """Nothing open: clear the canvas, disable everything document-bound."""
        self._clear_document_view()
        self._set_document_ui_enabled(True)  # idle, but live for the next open
        self.setWindowTitle("Celpix")
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
        self.setWindowTitle(f"Celpix — {entry.name} (missing)")
        self._sync_nav()
        self.statusBar().showMessage(
            f"{entry.name}: file not found — File ▸ Locate missing files "
            "to re-point it."
        )

    def _remove_entry(self, entry: Entry) -> None:
        """Remove ``entry`` from the list (a file takes its slices and
        bookmarks with it), always confirming first — Remove is also on the
        Delete key, and a slip there costs the entry's whole session setup."""
        victims = [entry, *self._workspace.children_of(entry)]
        dirty = [e.name for e in victims if e.dirty]
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
        answer = QMessageBox.question(self, "Celpix — remove", message)
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

    def closeEvent(self, event) -> None:  # noqa: ANN001 — Qt override
        dirty = self._workspace.dirty_entries()
        if dirty:
            names = ", ".join(e.name for e in dirty)
            answer = QMessageBox.question(
                self,
                "Celpix — unsaved changes",
                f"Discard unsaved changes to {names}?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        super().closeEvent(event)

    def _build_palette_dock(self) -> None:
        """The right-side palette dock: a load-mode header over the swatch grid.

        Built after _build_navbar, whose address-format machinery the offset
        field here shares (_parse_address / _palette_offset_text).
        """
        self._palette_panel = PalettePanel()
        # A scroll area guards against a pathologically large opened palette;
        # a typical 256-colour grid is small and never scrolls.
        holder = QScrollArea()
        holder.setWidget(self._palette_panel)
        holder.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)

        self._palette_mode_combo = QComboBox()
        self._palette_mode_combo.setToolTip(
            "Where the palette comes from: the generated default, a palette "
            "file, an offset into the open pixel file, or an emulator save "
            "state (console auto-detected)"
        )
        for label, mode in (
            ("Default", "default"),
            ("File", "file"),
            ("Offset", "offset"),
            ("Emulator State", "emulator"),
        ):
            self._palette_mode_combo.addItem(label, mode)
        # Connected after population so the addItem calls don't fire it. Qt only
        # emits on index *change*, so re-selecting the current "File" entry
        # doesn't re-prompt — re-opening a different palette file goes through
        # the Palette menu.
        self._palette_mode_combo.currentIndexChanged.connect(
            self._on_palette_mode_change
        )

        # Same parse + rendering conventions as the navbar offset box (shared
        # address-format dropdown and bank settings). Disabled-but-visible
        # outside Offset mode, like the bank spins under flat hex.
        self._palette_offset_edit = CommittingLineEdit(
            self._parse_address, self._palette_offset_text
        )
        self._palette_offset_edit.setFixedWidth(104)
        self._palette_offset_edit.setToolTip(
            "Palette offset in the open pixel file — Enter to load"
        )
        self._palette_offset_edit.setEnabled(False)
        self._palette_offset_edit.committed.connect(self._on_palette_offset_committed)

        header = QHBoxLayout()
        header.setContentsMargins(4, 4, 4, 2)
        header.addWidget(self._palette_mode_combo)
        header.addWidget(self._palette_offset_edit)
        header.addStretch(1)

        # Details readout for the panel's selected colour. Selectable text so
        # values can be copied out.
        self._color_details = QLabel("No colour selected")
        self._color_details.setContentsMargins(4, 0, 4, 4)
        self._color_details.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self._palette_panel.color_selected.connect(
            lambda _index: self._update_color_details()
        )

        container = QWidget()
        column = QVBoxLayout(container)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(2)
        column.addLayout(header)
        column.addWidget(holder, 1)
        column.addWidget(self._color_details)

        self._palette_dock = QDockWidget("Palette", self)
        self._palette_dock.setObjectName("palette-dock")  # keeps saveState usable
        self._palette_dock.setWidget(container)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self._palette_dock)

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
        open_pixel.setShortcut(QKeySequence.StandardKey.Open)
        open_pixel.triggered.connect(self._open_pixel)
        file_menu.addAction(open_pixel)

        file_menu.addSeparator()

        open_project = QAction("Open Project…", self)
        open_project.setToolTip("Resume a saved session from a .celpix project file")
        open_project.triggered.connect(self._open_project)
        file_menu.addAction(open_project)

        save_project = QAction("Save Project", self)
        save_project.setToolTip(
            "Save the open files/slices and their settings (references, "
            "never the edited bytes) to a .celpix project file"
        )
        save_project.triggered.connect(self._save_project)
        file_menu.addAction(save_project)

        save_project_as = QAction("Save Project As…", self)
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
            "New slice covering the current viewport — its position and "
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
            "settings — jumping back restores both"
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
        self._write_action.setShortcut(QKeySequence.StandardKey.Save)
        self._write_action.triggered.connect(self._write_current)
        self._write_action.setEnabled(False)
        file_menu.addAction(self._write_action)

        self._write_all_action = QAction("Write All", self)
        self._write_all_action.setToolTip(
            "Write every open file and slice with unsaved changes"
        )
        self._write_all_action.setShortcut(QKeySequence("Ctrl+Shift+S"))
        self._write_all_action.triggered.connect(self._write_all)
        self._write_all_action.setEnabled(False)  # armed by dirty entries
        file_menu.addAction(self._write_all_action)

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

    def _build_edit_menu(self) -> None:
        """Edit ▸ Undo/Redo — stack-provided actions over the unified session
        history (label and enabled state come from the stack)."""
        menu = self.menuBar().addMenu("Edit")
        undo = self._undo_stack.createUndoAction(self)
        undo.setShortcut(QKeySequence.StandardKey.Undo)  # Ctrl+Z
        redo = self._undo_stack.createRedoAction(self)
        # Ctrl+Shift+Z first (the advertised binding), plus the platform
        # standard (Ctrl+Y on Windows), deduplicated.
        sequences = [QKeySequence("Ctrl+Shift+Z")]
        sequences += [
            s
            for s in QKeySequence.keyBindings(QKeySequence.StandardKey.Redo)
            if s not in sequences
        ]
        redo.setShortcuts(sequences)
        menu.addAction(undo)
        menu.addAction(redo)

    def _build_palette_menu(self) -> None:
        """Palette ▸ everything palette-flavoured: open, load-from-selection, panel."""
        menu = self.menuBar().addMenu("Palette")

        open_palette = QAction("Open palette…", self)
        open_palette.triggered.connect(self._open_palette)
        menu.addAction(open_palette)

        self._load_selection_action = QAction("Load from Selection", self)
        self._load_selection_action.setToolTip(
            "Read palette data from the open pixel file at the selected tile's offset"
        )
        self._load_selection_action.triggered.connect(self._load_palette_from_selection)
        self._load_selection_action.setEnabled(False)  # needs a doc + a selection
        # Display-only shortcut, like the View menu's: the bare key is routed by
        # the app-wide event filter (_handle_nav_key), which yields to focused
        # text inputs — a live shortcut here would steal "p" from them.
        self._load_selection_action.setShortcut(QKeySequence("P"))
        self._load_selection_action.setShortcutContext(
            Qt.ShortcutContext.WidgetShortcut
        )
        menu.addAction(self._load_selection_action)

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
        # to focused text inputs — a live shortcut here would steal it from them.
        self._grid.setShortcut(QKeySequence("G"))
        self._grid.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
        menu.addAction(self._grid)
        self._build_grid_style_menu(menu)

    def _build_grid_style_menu(self, view_menu) -> None:  # noqa: ANN001 — QMenu
        """View ▸ Grid Style ▸ the YY-CHR style set (None/Point/Dot/Dash/Line).

        Unlike the Grid toggle (per-view session state), the style is one
        app-wide appearance choice persisted in QSettings — remembered across
        launches and shared by every view, so it isn't part of a document's
        saved ViewOptions.
        """
        style = self._load_grid_style()
        self._canvas.set_grid_style(style)
        submenu = view_menu.addMenu("Grid Style")
        group = QActionGroup(self)  # exclusive: one style checked at a time
        self._grid_style_group = group
        labels = (
            (GridStyle.NONE, "None"),
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
        QSettings().setValue("view/grid_style", style.value)
        self._canvas.set_grid_style(style)

    @staticmethod
    def _load_grid_style() -> GridStyle:
        """The persisted grid style, defaulting to solid lines. Tolerates a
        missing or stale stored value (e.g. an older/newer build)."""
        stored = QSettings().value("view/grid_style", GridStyle.LINE.value)
        try:
            return GridStyle(stored)
        except ValueError:
            return GridStyle.LINE

    def _build_navigate_menu(self) -> None:
        """Navigate ▸ the navigation actions — the menu home for every nav key.

        Some of these also have navbar buttons; the rest (first/last page, page
        steps, window resizing) live only here and on the keyboard, so the menu
        doubles as the discoverable list of navigation shortcuts.
        """
        menu = self.menuBar().addMenu("Navigate")
        groups: tuple[tuple[tuple[str, str, Callable[[], None]], ...], ...] = (
            (
                ("First page", "Home", self._nav_home),
                ("Last page", "End", self._nav_end),
            ),
            (
                ("Previous byte", "- / Ctrl+Left", lambda: self._nav_bytes(-1)),
                ("Next byte", "+ / Ctrl+Right", lambda: self._nav_bytes(1)),
                ("Zero byte offset", "0", self._clear_nudge),
                ("Previous tile", "Left", lambda: self._nav_tiles(-1)),
                ("Next tile", "Right", lambda: self._nav_tiles(1)),
                ("Row up", "Up", lambda: self._nav_rows(-1)),
                ("Row down", "Down", lambda: self._nav_rows(1)),
                ("Page up", "PgUp", lambda: self._nav_rows(-self._rows.value())),
                ("Page down", "PgDown", lambda: self._nav_rows(self._rows.value())),
            ),
            (
                (
                    "Fewer columns",
                    "Shift+Left",
                    lambda: self._adjust_spin(self._columns, -1),
                ),
                (
                    "More columns",
                    "Shift+Right",
                    lambda: self._adjust_spin(self._columns, 1),
                ),
                ("Fewer rows", "Shift+Up", lambda: self._adjust_spin(self._rows, -1)),
                ("More rows", "Shift+Down", lambda: self._adjust_spin(self._rows, 1)),
            ),
        )
        for i, group in enumerate(groups):
            if i:
                menu.addSeparator()
            for text, key, handler in group:
                # The key text goes in the label after a tab, which Qt renders in
                # the menu's shortcut column. No real shortcut is registered:
                # these keys are routed by the app-wide event filter
                # (_handle_nav_key), which yields to arrow-consuming inputs — a
                # live shortcut here would fire even then. Plain text also shows
                # alternate keys ("+ / Ctrl+Right"), which QKeySequence can't.
                action = QAction(f"{text}\t{key}", menu)
                action.triggered.connect(handler)
                menu.addAction(action)

    def _build_panels_menu(self) -> None:
        """Panels ▸ show/hide the dockable panels (Files, Palette, Hex)."""
        menu = self.menuBar().addMenu("Panels")
        files_toggle = self._files_dock.toggleViewAction()
        files_toggle.setText("Files Panel")
        menu.addAction(files_toggle)
        palette_toggle = self._palette_dock.toggleViewAction()
        palette_toggle.setText("Palette Panel")
        menu.addAction(palette_toggle)
        hex_toggle = self._hex_dock.toggleViewAction()
        hex_toggle.setText("Hex Panel")
        menu.addAction(hex_toggle)

    def _open_plugins_folder(self) -> None:
        if self._plugin_dir is None:
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(self._plugin_dir))
        self._alert_plugin_issues()

    def _build_toolbar(self) -> None:
        # Three stacked rows: the codec selects (what the bytes *are*) on top, the
        # tile arrangement (how those tiles are grouped/addressed) directly below
        # it, and the view settings (how they're shown) at the bottom. Each break
        # drops the following bar onto its own row instead of flowing after the
        # previous one.
        codecs = QToolBar("Codecs")
        self.addToolBar(codecs)
        self._codecs_toolbar = codecs  # greyed out wholesale for a missing entry
        self.addToolBarBreak()
        arrange = QToolBar("Arrangement")
        self.addToolBar(arrange)
        self._arrange_toolbar = arrange  # frozen wholesale during a scan
        self.addToolBarBreak()
        view = QToolBar("View")
        self.addToolBar(view)
        self._view_toolbar = view  # frozen wholesale during a scan
        for bar in (codecs, arrange, view):
            bar.layout().setSpacing(10)

        self._pixel_preset = self._preset_combo(Stage.INTERPRET_PIXEL, "snes-4bpp")
        self._pixel_preset.currentIndexChanged.connect(self._on_pixel_preset_change)
        # End a format-cycling run when focus leaves the dropdown: the next switch
        # then re-anchors on the live position rather than the stale target.
        self._pixel_preset.focus_lost.connect(self._end_pixel_switch_run)
        codecs.addWidget(QLabel("Pixel:"))
        codecs.addWidget(self._pixel_preset)

        self._palette_preset = self._preset_combo(Stage.INTERPRET_PALETTE, "bgr555")
        self._palette_preset.currentIndexChanged.connect(self._reload_palette)
        codecs.addWidget(QLabel("Palette:"))
        codecs.addWidget(self._palette_preset)
        self._match_preset_widths()

        # Compression preview: the main view stays raw; the chosen Decompress
        # plugin runs over the current window and shows in the floating overlay.
        self._compression = CompactComboBox(0.75)
        self._populate_compression()
        self._compression.currentIndexChanged.connect(self._on_view_change)
        codecs.addWidget(QLabel("Compression:"))
        codecs.addWidget(self._compression)

        # Structure navigation for contiguously packed compressed data: hop
        # past the structure in view, or walk forward looking for the next one.
        self._jump_next = QPushButton("Jump to Next")
        self._jump_next.setToolTip(
            "Jump to the byte right after the structure in view "
            "(assumes structures are packed back-to-back)."
        )
        self._jump_next.setEnabled(False)
        self._jump_next.clicked.connect(self._on_jump_next)
        codecs.addWidget(self._jump_next)
        self._scan_button = QPushButton("Scan")
        self._scan_button.setToolTip(
            "Scan forward byte-by-byte for the next complete compressed structure. "
            "Click again to stop."
        )
        self._scan_button.setEnabled(False)
        self._scan_button.clicked.connect(self._on_scan)
        codecs.addWidget(self._scan_button)
        # One click promotes the complete structure in view into a decompressed
        # slice entry in the files list — the overlay preview made editable.
        self._promote_button = QPushButton("To Slice")
        self._promote_button.setToolTip(
            "Add the structure in view to the file list as a decompressed slice."
        )
        self._promote_button.setEnabled(False)
        self._promote_button.clicked.connect(self._on_promote_structure)
        codecs.addWidget(self._promote_button)

        # Manual header skip for headered ROMs: when checked, the first N file
        # bytes are ignored — the view and every offset start after the header
        # (so bank-address formats line up with the ROM proper), and saves
        # splice back after it. 512 B default = copier headers; iNES is 16 B.
        self._headered = QCheckBox("Header")
        self._headered.setToolTip(
            "Skip a file header: view and offsets start after it."
        )
        self._headered.toggled.connect(self._on_header_change)
        view.addWidget(self._headered)
        self._header_len = self._spin(0, 0x10000, 512, self._on_header_change)
        self._header_len.setSuffix(" B")
        # The hint is sized for the 5-digit maximum, but real headers are at
        # most 3 digits — trim the box so the view row stays compact.
        self._header_len.setFixedWidth(int(self._header_len.sizeHint().width() * 0.84))
        view.addWidget(self._header_len)

        self._columns = self._spin(1, 64, 16, self._on_view_change)
        view.addWidget(QLabel("Cols:"))
        view.addWidget(self._columns)

        # How many tile-rows the window shows — the "render N rows" view setting.
        self._rows = self._spin(1, 256, 16, self._on_view_change)
        view.addWidget(QLabel("Rows:"))
        view.addWidget(self._rows)
        # Cols maxes at 2 digits, rows at 3, so their hints differ — pin both
        # to the rows hint so the pair reads as a matched set.
        rows_width = self._rows.sizeHint().width()
        self._columns.setFixedWidth(rows_width)
        self._rows.setFixedWidth(rows_width)

        self._zoom = self._spin(1, 16, 4, self._on_view_change)
        view.addWidget(QLabel("Zoom:"))
        view.addWidget(self._zoom)

        # Range 255: enough rows for a 512-entry palette under a 2-colour (1bpp)
        # index space; the view refresh clamps to the loaded palette anyway.
        self._subpalette = self._spin(0, 255, 0, self._on_view_change)
        view.addWidget(QLabel("Subpal:"))
        view.addWidget(self._subpalette)

        # Arrangement (display-only placement/addressing, so these re-render like
        # zoom/grid — not undoable). Block W×H groups tiles into blocks; Order sets
        # how each block fills; 2D reads the source as one wide bitmap Cols across.
        # These share the codecs bar's second row (see _build_toolbar) rather than
        # the view row.
        #
        # Pattern names documented block/order/2D combinations and, like the Offset
        # format picker, fills + locks the individual controls when a preset is
        # chosen; "Custom" unlocks them so they can be hand-edited.
        self._pattern = CompactComboBox(0.75)
        for preset in ARRANGEMENT_PRESETS:
            self._pattern.addItem(preset.name, preset)
        self._pattern.addItem("Custom", "custom")
        self._pattern.setToolTip(
            "Tile arrangement preset — fills the Block / Order / 2D controls.\n"
            "Pick Custom to edit them yourself."
        )
        self._pattern.currentIndexChanged.connect(self._on_pattern_change)
        arrange.addWidget(QLabel("Pattern:"))
        arrange.addWidget(self._pattern)

        self._block_cols = self._spin(1, 64, 1, self._on_view_change)
        self._block_rows = self._spin(1, 256, 1, self._on_view_change)
        self._block_cols.setFixedWidth(rows_width)
        self._block_rows.setFixedWidth(rows_width)
        self._block_cols.setToolTip("Tiles per block, horizontally")
        self._block_rows.setToolTip("Tiles per block, vertically")
        arrange.addWidget(QLabel("Block:"))
        arrange.addWidget(self._block_cols)
        arrange.addWidget(QLabel("×"))
        arrange.addWidget(self._block_rows)
        self._block_order = QComboBox()
        self._block_order.setToolTip(
            "How each block fills:\n"
            "• Row — left-to-right, then down\n"
            "• Column — top-to-bottom, then right (Mega Drive / Neo Geo sprites)\n"
            "• Row-interleave — a tile-row across every block (8×16 sprite sheets)"
        )
        for label, data in (
            ("Row", "row"),
            ("Column", "column"),
            ("Row-interleave", "row-interleave"),
        ):
            self._block_order.addItem(label, data)
        self._block_order.currentIndexChanged.connect(self._on_view_change)
        arrange.addWidget(QLabel("Order:"))
        arrange.addWidget(self._block_order)
        self._two_d = QCheckBox("2D")
        self._two_d.setToolTip(
            "Read the source as one wide bitmap Cols tiles across, not back-to-back "
            "tiles (N64/NDS-style)."
        )
        self._two_d.toggled.connect(self._on_view_change)
        arrange.addWidget(self._two_d)
        # The default view is Linear (the first preset), so start with the block
        # controls locked until Custom is picked.
        self._apply_pattern_lock()

    @property
    def _arrangement_controls(self) -> tuple[QWidget, ...]:
        """The individual block/order/2D widgets a Pattern preset drives."""
        return (self._block_cols, self._block_rows, self._block_order, self._two_d)

    def _apply_pattern_lock(self) -> None:
        """Enable the individual arrangement controls only under Custom; a named
        preset owns them, so they're read-only while one is selected."""
        custom = self._pattern.currentData() == "custom"
        for widget in self._arrangement_controls:
            widget.setEnabled(custom)

    def _set_arrangement(
        self, block_columns: int, block_rows: int, block_order: str, two_d: bool
    ) -> None:
        """Push the four arrangement values onto their widgets with signals
        blocked — a preset fill (or a session restore) is one coherent change the
        caller re-renders once, not four cascading _on_view_change calls."""
        for spin, value in (
            (self._block_cols, block_columns),
            (self._block_rows, block_rows),
        ):
            spin.blockSignals(True)
            spin.setValue(value)
            spin.blockSignals(False)
        select_combo_data(self._block_order, block_order)
        self._two_d.blockSignals(True)
        self._two_d.setChecked(two_d)
        self._two_d.blockSignals(False)

    def _on_pattern_change(self) -> None:
        """Apply a chosen Pattern: a preset fills + locks the block/order/2D
        controls; Custom just unlocks them (leaving the current values as the
        starting point). Either way, re-render."""
        data = self._pattern.currentData()
        if isinstance(data, ArrangementPreset):
            self._set_arrangement(
                data.block_columns,
                data.block_rows,
                data.block_order,
                data.two_dimensional,
            )
        self._apply_pattern_lock()
        self._on_view_change()

    def _sync_pattern_selection(self) -> None:
        """Reselect the Pattern entry that matches the live block/order/2D widgets
        (or Custom), and relock accordingly. Called after a session restore, whose
        widget values are the truth; signals stay blocked so this reselection does
        not re-enter _on_pattern_change and re-render."""
        preset = arrangement_preset_for(
            self._block_cols.value(),
            self._block_rows.value(),
            self._block_order.currentData(),
            self._two_d.isChecked(),
        )
        target = preset if preset is not None else "custom"
        select_combo_data(self._pattern, target)
        self._apply_pattern_lock()

    def _header_offset(self) -> int:
        """File bytes to skip before data begins (0 while 'Header' is unchecked)."""
        return self._header_len.value() if self._headered.isChecked() else 0

    def _on_header_change(self, *_args) -> None:
        entry = self._workspace.current
        if self._doc is None or entry is None or self._applying_undo:
            return
        if entry.kind is not EntryKind.FILE:
            return  # header skip is FILE state; the widgets are disabled anyway
        # The effective skip folds the checkbox and the length spin into one
        # number, so "toggle" and "length edit" are the same command; an edit
        # while unchecked (or an uncheck with no skip applied) changes nothing.
        old_header = self._doc.pixel_config.source.offset
        header = self._header_offset()
        if header == old_header:
            return
        preset_id = self._pixel_preset_id()
        before = (preset_id, old_header, self._byte_position())
        cfg = pixel_config_for(entry, preset_id, header, self._registry)
        try:
            px = pipeline.load_pixel_data(cfg, self._registry)
        except PipelineError as exc:
            self._report(exc)
            # The doc never changed — snap the widgets back onto its config.
            self._sync_header_widgets(old_header)
            return
        self._push_command(
            PixelConfigCommand(
                self,
                entry,
                "change header",
                before=before,
                after=(preset_id, header, self._byte_position()),
                preloaded=px,
            )
        )

    def _sync_header_widgets(self, header_offset: int) -> None:
        """Snap the header checkbox + length spin to ``header_offset``, signals
        blocked — seeds them on a config apply and reverts them when a header
        change fails to load. A zero offset just unticks the box and leaves the
        spin's last value, so re-ticking restores the previous skip length."""
        for widget in (self._headered, self._header_len):
            widget.blockSignals(True)
        self._headered.setChecked(header_offset > 0)
        if header_offset:
            self._header_len.setValue(header_offset)
        for widget in (self._headered, self._header_len):
            widget.blockSignals(False)

    def _populate_compression(self) -> None:
        """Fill the compression combo from the registry, in registration order
        (the built-ins group naturally: none first, then the LZ family)."""
        for plugin in self._registry.plugins(Stage.DECOMPRESS):
            self._compression.addItem(plugin.info.name, plugin.info.id)
            if plugin.info.id == "decompress.none":
                self._compression.setCurrentIndex(self._compression.count() - 1)

    def _preset_combo(self, stage: Stage, default_suffix: str) -> QComboBox:
        # Compact: preset names are long and two of these share a toolbar row, so
        # the closed button takes 3/4 of its natural width; the popup stays full.
        combo = CompactComboBox(0.75)
        for preset in sorted(self._registry.presets(stage), key=lambda p: p.name):
            combo.addItem(preset.name, preset.id)
            if preset.id.endswith(default_suffix):
                combo.setCurrentIndex(combo.count() - 1)
        return combo

    @staticmethod
    def _spin(low: int, high: int, value: int, on_change) -> QSpinBox:
        spin = QSpinBox()
        spin.setRange(low, high)
        spin.setValue(value)
        # Commit on Enter / focus-out / stepping, not on every keystroke, so typing
        # a multi-digit value doesn't re-render (and re-clamp) for each character.
        spin.setKeyboardTracking(False)
        spin.valueChanged.connect(on_change)
        return spin

    def _build_navbar(self) -> QWidget:
        """The strip under the canvas: the current position + tile/row step buttons.

        Two rows — the address row (offset box, format dropdown, bank settings)
        and below it the step-button row — so the bank settings don't push the
        buttons off-screen at narrow widths.

        Up/Down step one tile-row (``columns`` tiles); Left/Right step one tile;
        +B/−B nudge the grid one byte (sub-tile alignment) and 0B clears the
        nudge; Pg Up/Dn step a whole window — the same actions the keys drive
        (:meth:`_build_nav_keys`).
        First/last page are keyboard + View menu only. The position box
        reads/writes addresses in the format the
        dropdown next to it selects: flat hex, or a ``bank:offset`` mapping
        parameterized by the three bank-setting spins (a preset fills them; a
        hand-edit flips the dropdown to Custom; the piecewise ExHiROM/ExLoROM
        presets hide them instead).
        """
        bar = QWidget()
        rows = QVBoxLayout(bar)
        rows.setContentsMargins(6, 2, 6, 2)
        rows.setSpacing(2)
        row = QHBoxLayout()  # the address row
        step_row = QHBoxLayout()
        rows.addLayout(row)
        rows.addLayout(step_row)

        # Bank settings — created before the dropdown whose handler fills them.
        # Hex spin boxes (not line edits) so they clamp and step like the rest of
        # the toolbar; disabled while the flat-hex format needs none of them.
        self._bank_size = self._hex_spin(0x1, 0x1000000, 0x8000, "Bank size (bytes)")
        self._bank_addr = self._hex_spin(
            0x0, 0xFFFFFF, 0x8000, "In-bank address of a bank's first byte"
        )
        self._bank_first = self._hex_spin(
            0x0, 0xFF, 0x00, "Bank number of the file's first byte"
        )
        # The bank anchor is the setting users actually retune (mirror
        # conventions), so give it room beyond its two-digit size hint.
        self._bank_first.setFixedWidth(int(self._bank_first.sizeHint().width() * 1.4))
        self._bank_spins = (self._bank_size, self._bank_addr, self._bank_first)

        row.addWidget(QLabel("Offset "))
        # Half-width closed button (the format names are long), full-width popup —
        # the same compact treatment the pixel/palette pickers get.
        self._addr_format = CompactComboBox(0.5)
        self._addr_format.addItem("Hex", "hex")
        for preset in BANK_PRESETS:
            self._addr_format.addItem(preset.name, preset)
        self._addr_format.addItem("Custom", "custom")
        self._addr_format.setToolTip("Address format for the offset box")
        self._addr_format.currentIndexChanged.connect(self._on_addr_format_change)
        row.addWidget(self._addr_format)

        # Editable file offset. A CommittingLineEdit commits on Enter / focus-out
        # (not per keystroke) and always re-renders on commit, so an invalid entry
        # reverts and a valid one shows its canonical form (byte-exact: a sub-tile
        # address becomes the grid's byte nudge); it keeps
        # its own arrow/Home keys, so the navigation shortcuts don't fire while
        # focused.
        self._offset_edit = CommittingLineEdit(self._parse_address, self._offset_text)
        self._offset_edit.setFixedWidth(104)
        self._offset_edit.setToolTip(self._offset_edit_tip())
        self._offset_edit.committed.connect(self._jump_to_offset)
        row.addWidget(self._offset_edit)
        row.addSpacing(12)

        # The settings live in one container so the piecewise presets
        # (ExHiROM/ExLoROM), which the three-number model can't express, can
        # hide them wholesale instead of showing misleading values.
        self._bank_settings = QWidget()
        bank_row = QHBoxLayout(self._bank_settings)
        bank_row.setContentsMargins(0, 0, 0, 0)
        for label, spin in (
            ("Size", self._bank_size),
            ("Addr", self._bank_addr),
            ("Bank", self._bank_first),
        ):
            bank_row.addWidget(QLabel(f" {label} "))
            bank_row.addWidget(spin)
        row.addWidget(self._bank_settings)
        row.addStretch(1)

        # Arrow steps use the style's standard icons rather than triangle glyphs:
        # the left/right triangles are emoji-capable codepoints, so font fallback
        # can render them in a different style from the up/down pair.
        sp = QStyle.StandardPixmap
        for text, icon, tip, handler in (
            (
                "Pg Dn",
                None,
                "Down one page (PgDown)",
                lambda: self._nav_rows(self._rows.value()),
            ),
            ("", sp.SP_ArrowDown, "Down one row (Down)", lambda: self._nav_rows(1)),
            ("", sp.SP_ArrowUp, "Up one row (Up)", lambda: self._nav_rows(-1)),
            (
                "Pg Up",
                None,
                "Up one page (PgUp)",
                lambda: self._nav_rows(-self._rows.value()),
            ),
            ("", sp.SP_ArrowLeft, "Back one tile (Left)", lambda: self._nav_tiles(-1)),
            (
                "",
                sp.SP_ArrowRight,
                "Forward one tile (Right)",
                lambda: self._nav_tiles(1),
            ),
            (
                "−B",
                None,
                "Back one byte (- or Ctrl+Left) — realign sub-tile",
                lambda: self._nav_bytes(-1),
            ),
            (
                "+B",
                None,
                "Forward one byte (+, = or Ctrl+Right) — realign sub-tile",
                lambda: self._nav_bytes(1),
            ),
            (
                "0B",
                None,
                "Clear the byte nudge (0) — snap the grid back to tile alignment",
                self._clear_nudge,
            ),
        ):
            btn = QPushButton(text)
            if icon is not None:
                btn.setIcon(self.style().standardIcon(icon))
            btn.setToolTip(tip)
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # keep arrow keys global
            btn.setFixedWidth(40)
            btn.clicked.connect(handler)
            step_row.addWidget(btn)

        # Surface the byte nudge when active — the tile grid looks ordinary, so
        # without this the sub-tile shift would be invisible state. Sits next to
        # the −B/+B buttons that change it.
        self._nudge_info = QLabel()
        step_row.addSpacing(8)
        step_row.addWidget(self._nudge_info)
        step_row.addStretch(1)
        return bar

    def _hex_spin(self, low: int, high: int, value: int, tip: str) -> QSpinBox:
        """A bank-setting spin box: hex display, commit-on-finish, $-prefixed."""
        spin = QSpinBox()
        spin.setRange(low, high)
        spin.setValue(value)
        spin.setDisplayIntegerBase(16)
        spin.setPrefix("$")
        spin.setKeyboardTracking(False)
        spin.setEnabled(False)  # the default format (flat hex) has no bank settings
        spin.setToolTip(f"{tip} — hex")
        spin.valueChanged.connect(self._on_bank_setting_change)
        return spin

    def _offset_bar_style(self) -> str:
        """Accent-coloured QSS for the file-position bar.

        Derived from the app's Highlight colour so it stays theme-appropriate; a
        rounded accent handle on a tinted rail with the step arrows hidden makes it
        read clearly as a file navigator, distinct from the canvas's own scrollbars.
        """
        accent = self.palette().color(QPalette.ColorRole.Highlight)
        r, g, b = accent.red(), accent.green(), accent.blue()
        handle = accent.name()
        handle_hover = accent.lighter(120).name()
        return f"""
            QScrollBar:vertical {{
                width: 16px;
                background: rgba({r}, {g}, {b}, 38);
                border: none;
                margin: 0;
            }}
            QScrollBar::handle:vertical {{
                background: {handle};
                min-height: 28px;
                border-radius: 5px;
                margin: 3px 2px;
            }}
            QScrollBar::handle:vertical:hover {{ background: {handle_hover}; }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0; background: none; border: none;
            }}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
                background: none;
            }}
        """

    # Input widgets that use the arrow keys themselves; while one of these has focus
    # the navigation keys are left alone so it can cycle options / move the cursor.
    # The palette panel is one: focused (clicked), its Up/Down step subpalettes.
    # The files tree is another: its arrows walk the open-entries list (selection
    # is activation, so Up/Down switch the shown file/slice).
    _ARROW_INPUT_TYPES = (
        QComboBox,
        QAbstractSpinBox,
        QLineEdit,
        QAbstractSlider,
        PalettePanel,
        QTreeWidget,
    )

    def _build_nav_keys(self) -> None:
        """Map navigation keys to handlers, applied window-wide by :meth:`eventFilter`.

        Arrow / Home / End / PageUp-Down drive the view window (scroll is locked to the
        tile offset; PageUp/Down step a whole window of rows). Shift+arrows resize the
        window instead of moving it (↕ rows, ↔ cols); Ctrl+arrows nudge bytes. Keyed
        by ``(key, shift_held, ctrl_held)``.
        """
        no_mod = (False, False)
        shift = (True, False)
        ctrl = (False, True)
        self._nav_keys = {
            (Qt.Key.Key_Up, *no_mod): lambda: self._nav_rows(-1),
            (Qt.Key.Key_Down, *no_mod): lambda: self._nav_rows(1),
            (Qt.Key.Key_Left, *no_mod): lambda: self._nav_tiles(-1),
            (Qt.Key.Key_Right, *no_mod): lambda: self._nav_tiles(1),
            (Qt.Key.Key_PageUp, *no_mod): lambda: self._nav_rows(-self._rows.value()),
            (Qt.Key.Key_PageDown, *no_mod): lambda: self._nav_rows(self._rows.value()),
            (Qt.Key.Key_Home, *no_mod): self._nav_home,
            (Qt.Key.Key_End, *no_mod): self._nav_end,
            # Byte nudge. Plus is registered under both shift states: on many
            # layouts it is Shift+= (shift held), on the keypad it is bare. Bare
            # = also steps forward, so -/= work as a shiftless pair; Ctrl+arrows
            # mirror the pair for one-handed use, and 0 clears the nudge.
            (Qt.Key.Key_Minus, *no_mod): lambda: self._nav_bytes(-1),
            (Qt.Key.Key_Plus, *no_mod): lambda: self._nav_bytes(1),
            (Qt.Key.Key_Plus, *shift): lambda: self._nav_bytes(1),
            (Qt.Key.Key_Equal, *no_mod): lambda: self._nav_bytes(1),
            (Qt.Key.Key_Left, *ctrl): lambda: self._nav_bytes(-1),
            (Qt.Key.Key_Right, *ctrl): lambda: self._nav_bytes(1),
            (Qt.Key.Key_0, *no_mod): self._clear_nudge,
            (Qt.Key.Key_Up, *shift): lambda: self._adjust_spin(self._rows, -1),
            (Qt.Key.Key_Down, *shift): lambda: self._adjust_spin(self._rows, 1),
            (Qt.Key.Key_Left, *shift): lambda: self._adjust_spin(self._columns, -1),
            (Qt.Key.Key_Right, *shift): lambda: self._adjust_spin(self._columns, 1),
            # Not navigation, but the same routing need: bare letter keys that
            # must yield to focused text inputs (Palette ▸ Load from Selection,
            # View ▸ Grid).
            (Qt.Key.Key_P, *no_mod): self._load_palette_from_selection,
            (Qt.Key.Key_G, *no_mod): self._grid.toggle,
        }

    def eventFilter(self, obj, event) -> bool:  # noqa: ARG002 — Qt supplies obj
        # Installed on the QApplication so navigation keys act wherever focus is —
        # unlike a QShortcut, which a focused dropdown would pre-empt. Only while this
        # window is active, and _handle_nav_key defers to arrow-consuming inputs.
        if (
            event.type() == QEvent.Type.KeyPress
            and self.isActiveWindow()
            and self._handle_nav_key(event)
        ):
            return True
        return super().eventFilter(obj, event)

    def _handle_nav_key(self, event) -> bool:
        """Run the navigation handler for ``event``; return True if it was consumed.

        Yields (returns False) when an arrow-consuming input has focus, a popup
        (e.g. an open menu, which arrow keys navigate) is up, or the event carries
        Alt/Meta, so only bare / Shift-ed / Ctrl-ed navigation keys ever act (an
        unregistered Ctrl combo still falls through to the normal shortcuts).
        """
        if self._scanning:
            return True  # a running scan owns the view position; swallow keys
        if QApplication.activePopupWidget() is not None:
            return False
        if isinstance(QApplication.focusWidget(), self._ARROW_INPUT_TYPES):
            return False
        mods = event.modifiers()
        blocked = Qt.KeyboardModifier.AltModifier | Qt.KeyboardModifier.MetaModifier
        if mods & blocked:
            return False
        shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)
        ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
        handler = self._nav_keys.get((event.key(), shift, ctrl))
        if handler is None:
            return False
        handler()
        return True

    @staticmethod
    def _adjust_spin(spin: QSpinBox, delta: int) -> None:
        # setValue clamps to the spinbox range and fires valueChanged, which
        # re-renders (and re-clamps the offset) through _on_view_change.
        spin.setValue(spin.value() + delta)

    # -- navigation --------------------------------------------------------
    def _nav_rows(self, delta_rows: int) -> None:
        """Move the window ``delta_rows`` tile-rows (± ``columns`` tiles each)."""
        self._set_offset(self._offset + delta_rows * self._columns.value())

    def _nav_tiles(self, delta_tiles: int) -> None:
        """Move the window ``delta_tiles`` single tiles."""
        self._set_offset(self._offset + delta_tiles)

    def _nav_bytes(self, delta: int) -> None:
        """Nudge the view origin ``delta`` bytes — sub-tile realignment.

        Works in byte space and carries across tile boundaries: nudging past
        ``bytes_per_tile`` rolls into the next tile with the nudge wrapped, so
        repeated +B/−B walks the file one byte at a time.
        """
        if self._doc is None or not self._doc.bytes_per_tile:
            return
        self._set_byte_position(self._byte_position() + delta)

    def _clear_nudge(self) -> None:
        """Snap the grid back to tile alignment, keeping the tile offset."""
        self._set_offset(self._offset, nudge=0)

    def _nav_home(self) -> None:
        self._set_offset(0)

    def _nav_end(self) -> None:
        if self._doc is not None:
            self._set_offset(self._doc.tile_count)  # clamped to the last page

    def _set_offset(self, offset: int, nudge: int | None = None) -> None:
        """Clamp the origin to a valid page and, if it moved, re-render.

        Tile-based moves pass only ``offset`` and keep the current byte nudge —
        the nudge is alignment state, not position, so paging/rowing preserves
        it. Byte-based moves (:meth:`_set_byte_position`) supply both.
        """
        if self._doc is None or self._applying_undo:
            return
        if nudge is None:
            nudge = self._nudge
        offset = self._doc.clamp_offset(
            offset, self._columns.value(), self._rows.value(), nudge
        )
        if (offset, nudge) == (self._offset, self._nudge):
            # No move (e.g. a scrollbar drag past the end clamped to here) — still
            # snap the scrollbar/box back onto the clamped position.
            self._sync_nav()
            return
        entry = self._workspace.current
        assert entry is not None  # a document implies a current entry
        self._push_command(
            OffsetMoveCommand(
                self,
                entry,
                before=(self._offset, self._nudge),
                after=(offset, nudge),
            )
        )

    def _apply_offset(self, offset: int, nudge: int) -> None:
        """Land the view on an already-clamped position (commands only —
        gestures go through :meth:`_set_offset`, which clamps and pushes)."""
        self._offset, self._nudge = offset, nudge
        self._refresh_view()  # re-clamps defensively if cols/rows changed since

    def _byte_position(self) -> int:
        """The view origin as a byte position on the tile grid (0 = file start)."""
        assert self._doc is not None
        return self._offset * self._doc.bytes_per_tile + self._nudge

    def _set_byte_position(self, pos: int) -> None:
        """Move the view origin to byte ``pos`` of the tile grid (0 = file start).

        The model clamps the position in byte space and splits it into a tile
        offset plus a sub-tile nudge (:meth:`Document.clamp_byte_position`).
        """
        assert self._doc is not None
        offset, nudge = self._doc.clamp_byte_position(
            pos, self._columns.value(), self._rows.value()
        )
        self._set_offset(offset, nudge=nudge)

    def _bank_layout(self) -> BankLayout | SplitBankLayout | None:
        """The bank mapping in effect, or None when the format is flat hex.

        A preset supplies its own layout object — it may fold a mirror anchor
        or be a piecewise split, neither of which the spins can express. Custom
        builds a plain three-number layout from the spins (which any hand-edit
        of a preset's values flips to, so the spins stay the truth there).
        """
        data = self._addr_format.currentData()
        if data == "hex":
            return None
        if isinstance(data, BankPreset):
            return data.layout
        return BankLayout(
            bank_size=self._bank_size.value(),
            addr_base=self._bank_addr.value(),
            bank_base=self._bank_first.value(),
        )

    def _format_offset(self, byte_off: int) -> str:
        """Render a byte offset in the active address format (box + status text)."""
        layout = self._bank_layout()
        return format_hex(byte_off) if layout is None else layout.format(byte_off)

    def _parse_address(self, text: str) -> int | None:
        """Parse the offset box's text as a file byte offset, or None if invalid."""
        layout = self._bank_layout()
        return parse_hex(text) if layout is None else layout.parse(text)

    def _offset_edit_tip(self) -> str:
        return f"File position ({self._addr_format.currentText()}) — Enter to jump"

    def _refresh_offset_display(self) -> None:
        self._offset_edit.setToolTip(self._offset_edit_tip())
        if self._doc is not None and not self._offset_edit.hasFocus():
            self._offset_edit.refresh()
        # The palette offset field shares the address conventions, so a format
        # or bank-setting change must re-render it too (its provider returns ""
        # when Offset mode isn't active, so this is safe at any time).
        if not self._palette_offset_edit.hasFocus():
            self._palette_offset_edit.refresh()
        # The hex dump's address column follows the same format.
        self._refresh_hex()

    def _on_addr_format_change(self) -> None:
        """Apply a newly chosen format: fill settings from a preset, re-render."""
        data = self._addr_format.currentData()
        layout = data.layout if isinstance(data, BankPreset) else None
        if isinstance(layout, BankLayout):
            # Block the spins' signals: this programmatic fill is the preset
            # itself, not a divergence, so it must not flip the box to Custom.
            for spin, value in (
                (self._bank_size, layout.bank_size),
                (self._bank_addr, layout.addr_base),
                (self._bank_first, layout.bank_base),
            ):
                spin.blockSignals(True)
                spin.setValue(value)
                spin.blockSignals(False)
        # Piecewise (split) layouts have no three-number equivalent — hide the
        # settings rather than display values that don't describe the mapping.
        self._bank_settings.setVisible(not isinstance(layout, SplitBankLayout))
        for spin in self._bank_spins:
            spin.setEnabled(data != "hex")
        self._refresh_offset_display()

    def _on_bank_setting_change(self) -> None:
        """A hand-edited bank setting means the selected preset no longer holds."""
        if isinstance(self._addr_format.currentData(), BankPreset):
            # Fires _on_addr_format_change, which re-renders the offset box.
            self._addr_format.setCurrentIndex(self._addr_format.findData("custom"))
        else:
            self._refresh_offset_display()

    def _display_base(self) -> int:
        """The file byte the view's position 0 corresponds to — display policy.

        Raw sources (no decompressor) show source-file-absolute addresses: the
        header skip for a whole file, the slice offset for a raw slice — so ROM
        bank addresses stay meaningful wherever the bytes came from. A
        decompressed stream has no linear mapping back to file offsets, so it
        shows its own 0-based positions instead of lying with file addresses.
        """
        assert self._doc is not None
        cfg = self._doc.pixel_config
        return cfg.source.offset if cfg.decompress_id == "decompress.none" else 0

    def _tile_byte_offset(self, tile: int) -> int:
        """The displayed byte offset of ``tile`` on the current (nudged) grid."""
        assert self._doc is not None
        return self._display_base() + self._nudge + tile * self._doc.bytes_per_tile

    def _offset_text(self) -> str:
        """The current byte offset rendered in the chosen address format.

        Also the offset box's ``current_text`` provider — it re-renders from this on
        every commit, so it must be safe to call with no document loaded.
        """
        if self._doc is None:
            return ""
        return self._format_offset(self._tile_byte_offset(self._offset))

    def _jump_to_offset(self, byte_off: int) -> None:
        """Jump to a file byte offset — the offset box's commit handler.

        Byte-exact: a sub-tile address sets the byte nudge, so typing any offset
        lands the grid on it. The box re-renders itself from :meth:`_offset_text`
        after this, so there's no text handling to do here; an out-of-range value
        is clamped by _set_byte_position.
        """
        if self._doc is None:
            return
        self._set_byte_position(byte_off - self._display_base())

    def _sync_nav(self) -> None:
        """Mirror the current offset into the hex box and the position bar."""
        has_doc = self._doc is not None
        self._offset_edit.setEnabled(has_doc)
        self._offset_bar.setEnabled(has_doc)
        if not has_doc:
            self._offset_edit.clear()
            self._nudge_info.clear()
            return

        cols, rows = self._columns.value(), self._rows.value()
        # Don't overwrite what the user is mid-way through typing; a commit re-renders
        # the box itself (CommittingLineEdit.commit), so this guard is safe.
        if not self._offset_edit.hasFocus():
            self._offset_edit.refresh()
        self._nudge_info.setText(f"+{self._nudge} B" if self._nudge else "")

        # Scrollbar spans the whole file: value = offset, page = one window of tiles,
        # so the handle size reflects how much of the file is on screen.
        page = max(1, cols) * max(1, rows)
        max_off = self._doc.clamp_offset(self._doc.tile_count, cols, rows, self._nudge)
        bar = self._offset_bar
        bar.blockSignals(True)  # setValue here must not re-enter _set_offset
        bar.setEnabled(max_off > 0)
        bar.setRange(0, max_off)
        bar.setSingleStep(cols)
        bar.setPageStep(page)
        bar.setValue(self._offset)
        bar.blockSignals(False)

    # -- current selections ------------------------------------------------
    def _pixel_preset_id(self) -> str:
        return self._pixel_preset.currentData()

    def _palette_preset_id(self) -> str:
        return self._palette_preset.currentData()

    def _pixel_bpp(self) -> int:
        return pipeline.pixel_bpp(self._pixel_preset_id(), self._registry)

    def _index_space(self, preset_id: str | None = None) -> int:
        """The pixel format's colour count — the subpalette row size.

        Capped at 256: a direct-colour preset's bpp can be up to 32, and both
        the palette maths and the fallback palette top out at 256 entries. The
        bpp comes from the resolved codec's geometry (:func:`pipeline.pixel_bpp`),
        so a preset with no ``bpp`` param — a wide/odd-tile codec, a code format —
        is sized correctly rather than crashing on a missing key.

        Defaults to the currently selected preset; pass ``preset_id`` to size
        another format's index space (e.g. _apply_pixel_config's outgoing preset).
        A stale id (preset removed by a plugin refresh) falls back to the
        current preset rather than failing the reload.
        """
        if preset_id is not None:
            try:
                bpp = pipeline.pixel_bpp(preset_id, self._registry)
                return min(256, 1 << bpp)
            except (KeyError, PipelineError):
                pass
        return min(256, 1 << self._pixel_bpp())

    def _pixel_tile_size(self) -> tuple[int, int]:
        # The atomic tile size is the codec's (recorded on the document at load) — not
        # a preset field (geometry is the engine's fixed unit; display grouping into
        # larger tiles is a separate view option, not yet implemented).
        if self._doc is not None:
            return self._doc.tile_width, self._doc.tile_height
        return 8, 8

    # -- drag & drop -------------------------------------------------------
    @staticmethod
    def _dropped_paths(event: QDragEnterEvent | QDropEvent) -> list[str]:
        """All local-file paths in a drag payload (empty when it has none)."""
        mime = event.mimeData()
        if not mime.hasUrls():
            return []
        return [path for url in mime.urls() if (path := url.toLocalFile())]

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # Qt override
        # Only offer to accept when the drag carries local files; ignore otherwise
        # so the user gets accurate feedback (no drop cursor for non-file drags).
        if self._dropped_paths(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:  # Qt override
        paths = self._dropped_paths(event)
        if not paths:
            return
        event.acceptProposedAction()
        for path in paths:  # every file becomes an entry; the last one is shown
            self._load_pixel(path)

    # -- actions -----------------------------------------------------------
    def _open_pixel(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Open pixel data")
        if path:
            self._load_pixel(path)

    def _load_pixel(self, path: str) -> None:
        """Open ``path`` as a workspace entry and switch the view to it.

        The shared entry point for both File ▸ Open and drag-and-drop, so a
        dropped file behaves exactly like an opened one. A file that is
        already open activates its existing entry — identity is the path —
        so only a genuinely new entry becomes an undoable step.
        """
        existing = self._workspace.find_file(path)
        if existing is not None:
            self._activate_entry(existing)
            return
        entry = Entry(name=Path(path).name, kind=EntryKind.FILE, path=path)
        self._push_command(AddEntryCommand(self, entry, f"open {entry.name}"))

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

        Documents stay lazy — nothing is read until an entry is activated — and
        a per-entry problem (missing file, unknown preset) surfaces on that
        entry's activation, never as a failure of the load itself.
        """
        if not self._resolve_dirty_entries(
            "Loading a project replaces the current workspace, and the unsaved "
            "changes with it"
        ):
            return
        try:
            loaded = projectfile.load_project(path)
        except projectfile.ProjectError as exc:
            self._alert(str(exc), title="Celpix — project")
            return
        if loaded.version > projectfile.PROJECT_VERSION:
            self._alert(
                "This project was saved by a newer Celpix. It opens with what "
                "this version understands, but saving will rewrite it at "
                f"version {projectfile.PROJECT_VERSION}, dropping the rest.",
                title="Celpix — project",
            )
        self._workspace.replace(loaded.entries, loaded.current)
        # The one entry-lifecycle change that bypasses the undo stack: older
        # commands would reference entries the replace discarded, so the
        # history goes with them.
        self._undo_stack.clear()
        self._project_path = path
        self.statusBar().showMessage(
            f"Loaded project {Path(path).name} ({len(loaded.entries)} entries)."
        )
        # Referenced files may have moved since the project was saved — offer to
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
        located file corrects every entry that shared the old path — a ROM and
        the slices/bookmarks under it move together — and reloads whatever was
        affected. Skipped files stay missing (still highlighted, still armed).
        """
        paths = missing_paths(self._workspace)
        if not paths:
            self.statusBar().showMessage("No missing files.")
            return
        if prompt_summary:
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Warning)
            box.setWindowTitle("Celpix — missing files")
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
                continue  # skipped — leave it missing
            # Reject locating a data file onto one already open: that would leave
            # two file entries editing the same path. (A palette-only relocation
            # — no file entry at `old` — can legitimately point into an open ROM,
            # so it isn't blocked.)
            clash = self._workspace.find_file(new)
            if self._workspace.find_file(old) is not None and clash is not None:
                self._alert(
                    f"{Path(new).name} is already open in this project, so "
                    f"{Path(old).name} can't be relocated to it. Pick a "
                    "different file, or close the duplicate first.",
                    title="Celpix — locate",
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
            self._alert(f"Cannot write {path}: {exc}", title="Celpix — project")
            return
        self._project_path = path
        self.statusBar().showMessage(f"Saved project to {path}.")

    def _resolve_dirty_entries(self, consequence: str) -> bool:
        """Dirty-entries gate for project save/load; True when OK to proceed.

        A project can't represent unsaved in-memory edits, so the user either
        writes them to disk first or knowingly continues without them.
        """
        dirty = self._workspace.dirty_entries()
        if not dirty:
            return True
        names = ", ".join(e.name for e in dirty)
        box = QMessageBox(self)
        box.setWindowTitle("Celpix — unsaved changes")
        box.setText(f"{consequence} ({names}). Write them to disk first?")
        write = box.addButton("Write All", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Continue Without", QMessageBox.ButtonRole.DestructiveRole)
        cancel = box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        if box.clickedButton() is cancel:
            return False
        if box.clickedButton() is write:
            self._write_all()
            # A write that failed left its entry dirty — don't proceed past it.
            return not self._workspace.dirty_entries()
        return True

    def _store_pixel_data(self, px: pipeline.PixelData, cfg: PathwayConfig) -> None:
        """Update the open document's pixel bytes + geometry from a fresh load."""
        assert self._doc is not None
        self._doc.pixel_data = px.data
        self._doc.bytes_per_tile = px.bytes_per_tile
        self._doc.tile_width = px.tile_width
        self._doc.tile_height = px.tile_height
        self._doc.pixel_config = cfg
        self._doc.pixel_ctx = px.ctx
        if not self._has_real_palette:
            self._doc.palette = self._fallback_palette()

    def _open_palette(self) -> bool:
        """Load a palette from a separate file; ``False`` on cancel/failure so
        the mode dropdown can revert instead of lying about the source."""
        if self._doc is None:
            self.statusBar().showMessage("Open pixel data first.")
            return False
        path, _ = QFileDialog.getOpenFileName(self, "Open palette")
        if not path:
            return False
        cfg = PathwayConfig(
            source=FileRef(path), interpret_preset_id=self._palette_preset_id()
        )
        try:
            colors, ctx = pipeline.load_palette(cfg, self._registry)
        except PipelineError as exc:
            self._report(exc)
            return False
        self._commit_palette(
            cfg,
            colors,
            ctx,
            mode="file",
            label=f"load palette from {Path(path).name}",
            status=f"Loaded {len(colors)} colours from {path}",
        )
        return True

    def _emulator_palette_config(
        self, path: str
    ) -> tuple[emustate.StateFormat, PathwayConfig]:
        """Detect the emulator state at ``path`` and build its palette config.

        The console is auto-detected from the file's bytes/extension, and the
        palette codec is the one that console dictates (BGR555 for SNES, the NES
        master-palette index table, …) — not whatever the format dropdown was
        on. View-only: the state is a memory dump, never a palette we write back.
        Raises :class:`emustate.StateError` (unrecognised / palette not located)
        or the usual pipeline/OS errors; the read window is floored to what fits.
        """
        data = Path(path).read_bytes()
        fmt, region = emustate.locate_palette(data, Path(path).suffix)
        if region.data is not None:
            # The palette was extracted from a container/memory image, not found
            # at a file offset — feed those bytes straight through the pipeline.
            entry_bytes = pipeline.palette_entry_size(region.preset_id, self._registry)
            length = min(len(region.data), region.count * entry_bytes)
            ref: FileRef | None = FileRef(
                path, offset=0, length=length, data=region.data
            )
        else:
            ref = self._selection_palette_source(
                path, region.offset, region.preset_id, max_entries=region.count
            )
        if ref is None:
            raise emustate.StateError(
                f"{fmt.name} state: no palette data at the detected offset "
                f"({self._format_offset(region.offset)})."
            )
        return fmt, PathwayConfig(
            source=ref, interpret_preset_id=region.preset_id, write_enabled=False
        )

    def _open_emulator_state(self) -> bool:
        """Load a palette from an emulator save state; ``False`` on cancel/failure
        so the mode dropdown can revert instead of lying about the source."""
        if self._doc is None:
            self.statusBar().showMessage("Open pixel data first.")
            return False
        path, _ = QFileDialog.getOpenFileName(self, "Open emulator save state")
        if not path:
            return False
        try:
            fmt, cfg = self._emulator_palette_config(path)
            colors, ctx = pipeline.load_palette(cfg, self._registry)
        except emustate.StateError as exc:
            self._alert(str(exc), title="Celpix — emulator state")
            return False
        except OSError as exc:
            self._alert(f"Cannot read {path}: {exc}", title="Celpix — emulator state")
            return False
        except PipelineError as exc:
            self._report(exc)
            return False
        self._commit_palette(
            cfg,
            colors,
            ctx,
            mode="emulator",
            label=f"load {fmt.console} palette from {fmt.name} state",
            status=(
                f"Loaded {len(colors)} {fmt.console} colours from {fmt.name} "
                f"state (view-only)"
            ),
        )
        return True

    # -- palette load modes ------------------------------------------------
    def _placeholder_palette_config(
        self, preset_id: str | None = None
    ) -> PathwayConfig:
        """The no-palette-loaded config: empty source, never written back.

        ``preset_id`` overrides the combo when loading a non-current entry,
        whose session may name a different palette format.
        """
        return PathwayConfig(
            source=FileRef(""),
            interpret_preset_id=preset_id or self._palette_preset_id(),
            write_enabled=False,
        )

    def _set_palette_mode(self, mode: str) -> None:
        """Converge mode member, dropdown, and offset field (the success path).

        Signals are blocked while syncing the combo so programmatic updates
        never re-enter _on_palette_mode_change.
        """
        self._palette_mode = mode
        select_combo_data(self._palette_mode_combo, mode)
        self._palette_offset_edit.setEnabled(mode == "offset")
        # Mid-commit the box refreshes itself afterwards; don't fight it.
        if not self._palette_offset_edit.hasFocus():
            self._palette_offset_edit.refresh()

    def _capture_palette_state(self) -> PaletteState:
        """Snapshot the palette pathway + selectors — an undo command's side.

        The preset comes from the document's config, not the format combo: in
        the combo's own change handler the widget has already moved, and only
        the config still holds the outgoing format (the _on_pixel_preset_change
        trick), so undo can restore the combo correctly.
        """
        assert self._doc is not None
        return PaletteState(
            preset_id=self._doc.palette_config.interpret_preset_id,
            mode=self._palette_mode,
            palette=self._doc.palette,
            config=self._doc.palette_config,
            ctx=self._doc.palette_ctx,
        )

    def _apply_palette_state(self, state: PaletteState) -> None:
        """Land a :class:`PaletteState` on the document and its widgets — the
        one application path for palette commands and plugin refreshes; never
        pushes, and stays silent (status messages belong to the gestures)."""
        assert self._doc is not None
        select_combo_data(self._palette_preset, state.preset_id)
        self._doc.palette = state.palette
        self._doc.palette_config = state.config
        self._doc.palette_ctx = state.ctx
        self._set_palette_mode(state.mode)  # already signal-safe
        self._refresh_view()

    def _commit_palette(
        self,
        cfg: PathwayConfig,
        colors: Palette,
        ctx: PipelineContext,
        *,
        mode: str,
        label: str,
        status: str | None = None,
    ) -> None:
        """Push one palette-source change (before→after) and optionally note it.

        The shared tail of every palette gesture — load-from-file, offset,
        emulator state, format re-decode, and back-to-default: snapshot the live
        palette as the undo *before*, land the freshly decoded ``colors``/``cfg``
        as the *after*, and report ``status`` for the user-initiated loads. Each
        caller keeps its own source-specific load and error reporting; only this
        uniform push/report is shared.
        """
        self._push_command(
            PaletteCommand(
                self,
                self._workspace.current,
                label,
                before=self._capture_palette_state(),
                after=PaletteState(cfg.interpret_preset_id, mode, colors, cfg, ctx),
            )
        )
        if status:
            self.statusBar().showMessage(status)

    def _on_palette_mode_change(self) -> None:
        """Act on a user pick in the mode dropdown; revert the combo on failure.

        self._palette_mode still holds the OLD mode here (it is only updated by
        _set_palette_mode on success), so reverting is just re-syncing to it.
        """
        mode = self._palette_mode_combo.currentData()
        if mode == self._palette_mode or self._applying_undo:
            return
        if self._doc is None:
            self.statusBar().showMessage("Open pixel data first.")
            self._set_palette_mode(self._palette_mode)
            return
        if mode == "default":
            self._use_default_palette()
        elif mode == "file":
            if not self._open_palette():
                self._set_palette_mode(self._palette_mode)
        elif mode == "offset":
            if not self._load_palette_at_offset(self._initial_palette_offset()):
                self._set_palette_mode(self._palette_mode)
        elif mode == "emulator":
            if not self._open_emulator_state():
                self._set_palette_mode(self._palette_mode)

    def _use_default_palette(self) -> None:
        """Back to the generated default palette (mode "default")."""
        assert self._doc is not None
        self._commit_palette(
            self._placeholder_palette_config(),
            self._fallback_palette(),
            PipelineContext(),
            mode="default",
            label="use default palette",
            status="Using the default palette.",
        )

    def _initial_palette_offset(self) -> int:
        """Where Offset mode starts: the selected tile, else the window top-left
        — the same byte numbers the offset box and status bar already show."""
        assert self._doc is not None
        tile = self._selected_tile if self._selected_tile is not None else self._offset
        return self._tile_byte_offset(tile)

    def _palette_offset_text(self) -> str:
        """The palette offset field's text provider; safe with no document."""
        if self._doc is None or self._palette_mode != "offset":
            return ""
        return self._format_offset(self._doc.palette_config.source.offset)

    def _on_palette_offset_committed(self, byte_off: int) -> None:
        # On failure the commit's own unconditional refresh reverts the text.
        if self._doc is not None:
            self._load_palette_at_offset(byte_off)

    # -- tile selection ----------------------------------------------------
    def _on_tiles_selected(self, anchor_slot: int, moving_slot: int) -> None:
        """Select the pressed slot, or the linear run a drag spans.

        Fired on press (anchor == moving) and again as a drag reaches other
        slots. Blank padding past the file is clamped out of the range; a
        press that *starts* there is ignored, as the single click always was.
        """
        if self._doc is None:
            return
        count = self._doc.tile_count
        first = self._offset + min(anchor_slot, moving_slot)
        last = self._offset + max(anchor_slot, moving_slot)
        if first >= count:
            return
        last = min(last, count - 1)
        self._selected_tile, self._selected_last = first, last
        self._update_selection_actions()
        self._refresh_selection(self._columns.value() * self._rows.value())
        self._refresh_hex()  # the hex highlight tracks the selection
        at_first = self._format_offset(self._tile_byte_offset(first))
        if first == last:
            self.statusBar().showMessage(f"Selected tile {first:,} at {at_first}")
        else:
            self.statusBar().showMessage(
                f"Selected tiles {first:,}–{last:,} ({last - first + 1} tiles) "
                f"from {at_first}"
            )

    def _clear_selection(self) -> None:
        self._selected_tile = None
        self._selected_last = None
        self._canvas.set_selection(None)
        self._update_selection_actions()
        self._refresh_hex()

    def _update_selection_actions(self) -> None:
        """Converge everything gated on 'a selection exists' with the state."""
        has = self._selected_tile is not None
        self._load_selection_action.setEnabled(has)
        # Only whole files spawn slices — slices never nest.
        current = self._workspace.current
        can_slice = current is not None and current.kind is EntryKind.FILE
        self._new_slice_from_selection_action.setEnabled(has and can_slice)
        self._files_panel.set_has_selection(has)

    def _selection_palette_source(
        self,
        path: str,
        byte_off: int,
        preset_id: str | None = None,
        max_entries: int = 256,
    ) -> FileRef | None:
        """A read window for up to ``max_entries`` palette entries at ``byte_off``.

        Floored to whole entries — the colour codecs reject a partial trailing
        entry, so clamping at EOF alone is not enough. ``None`` when not even one
        entry fits. ``preset_id`` overrides the combo when sizing entries for a
        non-current entry's palette format (project restore). ``max_entries``
        caps the window: the 256-entry default suits a free offset read; an
        emulator state passes its console's exact palette size instead.
        """
        bpe = pipeline.palette_entry_size(
            preset_id or self._palette_preset_id(), self._registry
        )
        avail = Path(path).stat().st_size - byte_off
        entries = min(max_entries, max(0, avail) // bpe)
        if entries == 0:
            return None
        return FileRef(path, offset=byte_off, length=entries * bpe)

    def _load_palette_at_offset(self, byte_off: int) -> bool:
        """Load palette data from the pixel source file at ``byte_off`` (Offset mode).

        The offset is in the pixel *source's* coordinate space (the same numbers
        the offset box shows — i.e. after any header skip, which is re-added for
        the file read), and the palette pathway re-reads the raw file — for
        container/compressed pixel sources the bytes at that offset differ from the
        decoded pixel data. Accepted for now; it mirrors the offset box semantics.
        For a **slice**, the source file is the *parent*, so the offset is an
        absolute parent-file offset — deliberately unbounded by the slice, since
        a graphics block's palette usually lives elsewhere in the ROM.
        The palette is view-only (never written back): the "palette file" here is
        the pixel file, and saving palette edits into it would clobber tile data.
        """
        if self._doc is None:
            return False
        src = self._doc.pixel_config.source
        try:
            ref = self._selection_palette_source(
                src.path, byte_off + self._header_offset()
            )
        except PipelineError as exc:
            self._report(exc)
            return False
        except OSError as exc:
            self._alert(f"Cannot read {src.path}: {exc}", title="Celpix — palette")
            return False
        if ref is None:
            self._alert(
                "Not enough data at that offset for a palette entry.",
                title="Celpix — palette",
            )
            return False
        cfg = PathwayConfig(
            source=ref,
            interpret_preset_id=self._palette_preset_id(),
            write_enabled=False,
        )
        try:
            colors, ctx = pipeline.load_palette(cfg, self._registry)
        except PipelineError as exc:
            self._report(exc)
            return False
        # Mode "offset" keeps pixel reloads from restoring the default palette.
        self._commit_palette(
            cfg,
            colors,
            ctx,
            mode="offset",
            label=f"load palette from {self._format_offset(byte_off)}",
            status=(
                f"Loaded {len(colors)} colours from "
                f"{self._format_offset(byte_off)} (view-only)"
            ),
        )
        return True

    def _load_palette_from_selection(self) -> None:
        """Palette ▸ Load from Selection: Offset mode at the selected tile."""
        if self._doc is None or self._selected_tile is None:
            return
        self._load_palette_at_offset(self._tile_byte_offset(self._selected_tile))

    def _on_pixel_preset_change(self) -> None:
        """The pixel combo changed: validate the new interpretation, then push
        one undoable command whose first redo applies the pre-validated load.

        Anchor on the target from the first switch of this run, if one is live,
        so a series of switches all measure from the same intended position
        instead of from wherever the previous format's clamping happened to
        land. The first switch has none yet, so it seeds it from the live view.
        """
        entry = self._workspace.current
        if self._doc is None or entry is None or self._applying_undo:
            return
        if self._pixel_switch_target is None:
            self._pixel_switch_target = self._byte_position()
        # The doc still holds the outgoing interpretation here (only the combo
        # has moved), so the undo state reads straight off it.
        old_preset = self._doc.pixel_config.interpret_preset_id
        old_header = (
            self._doc.pixel_config.source.offset
            if entry.kind is EntryKind.FILE
            else self._header_offset()  # ignored for slices
        )
        before = (old_preset, old_header, self._byte_position())
        preset_id = self._pixel_preset_id()
        header = self._header_offset()
        # Rebuild from the entry, not the old config: a slice keeps its bounds
        # and codec ids, and a file re-derives the header skip.
        cfg = pixel_config_for(entry, preset_id, header, self._registry)
        try:
            px = pipeline.load_pixel_data(cfg, self._registry)
        except PipelineError as exc:
            self._report(exc)
            # The doc never switched — snap the combo back onto its preset.
            select_combo_data(self._pixel_preset, old_preset)
            return
        self._push_command(
            PixelConfigCommand(
                self,
                entry,
                f"switch pixel format to {self._pixel_preset.currentText()}",
                before=before,
                after=(preset_id, header, self._pixel_switch_target),
                preloaded=px,
            )
        )
        note = self._partial_tile_note()
        if note:
            self.statusBar().showMessage(f"Preset changed — {note}")

    def _apply_pixel_config(
        self,
        preset_id: str,
        header_offset: int,
        byte_position: int,
        preloaded: pipeline.PixelData | None = None,
    ) -> bool:
        """Re-interpret the current entry's bytes and land on ``byte_position``.

        The one application path for preset switches, header changes, plugin
        refreshes and their undos: syncs the codec widgets (signals blocked,
        the _restore_session pattern) and never pushes a command. ``preloaded``
        carries a push site's already-validated load; without it the pipeline
        re-runs here, and a failure (reported) leaves the view untouched.

        The view offset is a tile index, so it maps to a different *byte*
        position under a new bytes-per-tile — ``byte_position`` re-lands the
        view exactly, with the sub-tile remainder becoming the byte nudge. The
        subpalette row is likewise re-anchored: the same row index means a
        different palette base under the new colour count, so it is recomputed
        from the selected colour (or the old base) to keep pointing at the
        same palette entries.
        """
        entry = self._workspace.current
        if self._doc is None or entry is None:
            return False
        old_group = self._index_space(self._doc.pixel_config.interpret_preset_id)
        cfg = pixel_config_for(entry, preset_id, header_offset, self._registry)
        if preloaded is not None:
            px = preloaded
        else:
            try:
                px = pipeline.load_pixel_data(cfg, self._registry)
            except PipelineError as exc:
                self._report(exc)
                return False
        select_combo_data(self._pixel_preset, preset_id)
        if entry.kind is EntryKind.FILE:
            self._sync_header_widgets(header_offset)
        self._store_pixel_data(px, cfg)
        # _refresh_view clamps the offset; the nudge stays < the new tile size.
        self._offset, self._nudge = divmod(byte_position, px.bytes_per_tile)
        anchor = self._palette_panel.selected_index()
        if anchor is None:
            anchor = self._subpalette.value() * old_group
        # Signals blocked: _refresh_view below re-renders (and re-clamps) once.
        self._subpalette.blockSignals(True)
        self._subpalette.setValue(anchor // self._index_space())
        self._subpalette.blockSignals(False)
        self._clear_selection()  # the same tile index covers different bytes now
        self._refresh_view()
        return True

    def _end_pixel_switch_run(self) -> None:
        """Drop the scratch target when the pixel dropdown loses focus.

        The target only spans one uninterrupted bout of format-cycling; once the
        user moves on, the current view *is* the position, so the next switch
        should re-anchor there rather than resurrect a stale byte offset.
        """
        self._pixel_switch_target = None

    def _reload_palette(self) -> None:
        """The palette combo changed: re-decode under the new colour format,
        as one undoable command (a failed decode reverts the combo)."""
        if self._doc is None or not self._has_real_palette or self._applying_undo:
            return
        before = self._capture_palette_state()
        result = self._reinterpret_palette()
        if result is None:
            # The load failed (reported): snap the combo back to the live format.
            select_combo_data(self._palette_preset, before.preset_id)
            return
        colors, cfg, ctx = result
        self._commit_palette(
            cfg, colors, ctx, mode=self._palette_mode, label="change palette format"
        )

    def _reinterpret_palette(
        self,
    ) -> tuple[Palette, PathwayConfig, PipelineContext] | None:
        """Decode the loaded palette source under the format combo's preset;
        ``None`` (reported) on failure, without touching the document.

        ``write_enabled`` must carry over from the old config: a from-selection
        palette reads out of the pixel file, and re-arming Write here would make
        Save splice palette bytes into it. Such a config's read window is also
        re-floored, since the new format's entry size may not divide the old
        window's length.
        """
        assert self._doc is not None
        old = self._doc.palette_config
        source = old.source
        if not old.write_enabled and source.length is not None:
            try:
                source = self._selection_palette_source(source.path, source.offset)
            except PipelineError as exc:
                self._report(exc)
                return None
            except OSError as exc:
                self._alert(
                    f"Cannot read {old.source.path}: {exc}", title="Celpix — palette"
                )
                return None
            if source is None:
                self._alert(
                    "Not enough data at the palette offset for this format.",
                    title="Celpix — palette",
                )
                return None
        cfg = PathwayConfig(
            source=source,
            interpret_preset_id=self._palette_preset_id(),
            write_enabled=old.write_enabled,
        )
        try:
            colors, ctx = pipeline.load_palette(cfg, self._registry)
        except PipelineError as exc:
            self._report(exc)
            return None
        return colors, cfg, ctx

    def _refresh_plugins(self) -> None:
        """Developer aid: reload plugins from disk and re-run on the open file.

        Rebuilds the registry (picking up added/changed/removed presets and code
        plugins — a changed code plugin passes the trust gate; one you approved this
        run reloads without a prompt), refreshes the preset menus, and re-decodes the
        currently open pixel/palette through the reloaded plugins.
        """
        if self._reload_plugins is None:
            return
        self._registry, self._plugin_issues = self._reload_plugins()
        self._repopulate_presets()
        if self._doc is not None:
            # Re-decode the open file's sources through the new registry — via
            # the application paths, never commands: a plugin refresh isn't an
            # edit and must not pollute the undo history.
            self._apply_pixel_config(
                self._pixel_preset_id(), self._header_offset(), self._byte_position()
            )
            if self._has_real_palette:
                result = self._reinterpret_palette()
                if result is not None:
                    colors, cfg, ctx = result
                    self._apply_palette_state(
                        PaletteState(
                            cfg.interpret_preset_id,
                            self._palette_mode,
                            colors,
                            cfg,
                            ctx,
                        )
                    )

        parts = ["Plugins refreshed"]
        if self._doc is not None:
            parts.append("re-ran on current file")
        self.statusBar().showMessage("; ".join(parts) + ".")
        # Any plugin that failed the reload is a warning, surfaced modally.
        self._alert_plugin_issues()

    def _repopulate_presets(self) -> None:
        """Rebuild the preset combos from the (reloaded) registry, keeping the
        current selection when it still exists."""
        for combo, stage in (
            (self._pixel_preset, Stage.INTERPRET_PIXEL),
            (self._palette_preset, Stage.INTERPRET_PALETTE),
        ):
            current = combo.currentData()
            # Block signals so repopulating doesn't fire a reload per item; the
            # refresh does one explicit reload afterwards.
            combo.blockSignals(True)
            combo.clear()
            for preset in sorted(self._registry.presets(stage), key=lambda p: p.name):
                combo.addItem(preset.name, preset.id)
            index = combo.findData(current)
            combo.setCurrentIndex(index if index >= 0 else 0)
            combo.blockSignals(False)
        # The compression combo lists Decompress *plugins*, not presets, but
        # refreshes the same way (keep the selection when it survives the reload).
        current = self._compression.currentData()
        self._compression.blockSignals(True)
        self._compression.clear()
        self._populate_compression()
        index = self._compression.findData(current)
        if index >= 0:
            self._compression.setCurrentIndex(index)
        self._compression.blockSignals(False)
        self._match_preset_widths()  # new presets may have changed the hint

    def _match_preset_widths(self) -> None:
        """Pin the pixel combo to the palette combo's width.

        Each compact combo sizes to its own longest entry, which would leave
        the side-by-side pair ragged — pinning makes them read as a matched
        set (the columns/rows spin idiom). The palette hint is the anchor
        (it stays content-sized); re-run after repopulating the combos.
        """
        self._pixel_preset.setFixedWidth(self._palette_preset.sizeHint().width())

    # -- writing back --------------------------------------------------------
    def _write_current(self) -> None:
        """File ▸ Write: the current file or slice back to disk."""
        entry = self._workspace.current
        if entry is None or entry.doc is None:
            return
        if self._write_entry(entry):
            # A from-selection palette is view-only — don't claim it was written.
            wrote = (
                "pixel + palette"
                if entry.doc is not None and entry.doc.palette_config.write_enabled
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
        """The files dock's context-menu Write — guards, then writes."""
        if entry.doc is None:
            return
        if not entry.doc.pixel_config.write_enabled:
            self._alert(
                f"{entry.name} is view-only (its compression has no compressor), "
                "so it can't be written back.",
                title="Celpix — write",
            )
            return
        if self._write_entry(entry):
            self.statusBar().showMessage(f"Wrote {entry.name}.")

    def _write_entry(self, entry: Entry) -> bool:
        """Save one entry through the pipeline; True on success.

        A successful write invalidates the cached documents of other entries on
        the same file (their bytes are now stale) — including the one on screen
        when a slice is written back under its parent's feet, which is re-read
        immediately so the view shows the freshly written bytes.
        """
        assert entry.doc is not None
        self._capture_session()  # keep the current entry's session snapshot fresh
        try:
            pipeline.save(entry.doc, self._registry)
        except PipelineError as exc:
            self._report(exc)
            return False
        self._workspace.set_dirty(entry, False)
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
        (format, mode, and the actual offset/file/colours) rather than the
        app-wide toolbar defaults — otherwise a slice carved from a file being
        viewed as, say, snes-4bpp with an offset palette would open blank as
        the built-in default. Pre-seeding the entry's session/pending-palette
        here means its first load skips :meth:`_seed_session`; both are
        consumed on that load. If the parent isn't open (or was never
        activated) there's nothing to copy — the toolbar seed then applies.
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
            # A slice's bytes are already decompressed — no preview codec.
            compression_id="decompress.none",
        )
        slice_entry.pending_palette = palette_source_for(parent)

    def _slice_prefill_offset(self) -> int:
        """The view position as an absolute file offset (raw sources only)."""
        assert self._doc is not None
        return self._doc.pixel_config.source.offset + self._byte_position()

    def _raw_slice_source(self) -> tuple[Entry, Document] | None:
        """The current entry + document if a slice can be carved from the view.

        A slice reads its parent's bytes directly, so only a live document whose
        pixel source is *raw* (no decompressor in the view) qualifies — a
        decompressed view can't spawn one. ``None`` when nothing qualifies;
        callers add any gesture-specific guard (a selection, a found structure).
        """
        entry, doc = self._workspace.current, self._doc
        if (
            entry is None
            or doc is None
            or doc.pixel_config.decompress_id != "decompress.none"
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
        slices — slices never nest)."""
        # Prefill from the view only when the dialog targets the file on screen;
        # a right-clicked non-current file has no live viewport to read.
        offset = (
            self._slice_prefill_offset()
            if entry is self._workspace.current and self._doc is not None
            else 0
        )
        self._create_slice_via_dialog(entry.path, offset=offset)

    def _new_slice_from_view_for(self, entry: Entry) -> None:
        """The files dock's New Slice from View — only the on-screen entry has
        a viewport, so anything else (a stale menu) is ignored."""
        if entry is self._workspace.current:
            self._new_slice_from_view()

    def _new_slice_from_view(self) -> None:
        """File ▸ New Slice from View: the dialog prefilled to cover the
        current viewport — the structure in view when the compression preview
        found one (its true extent beats the window's), else the visible
        window's bytes — plus the compression combo."""
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
            decompress_id=self._compression.currentData() or "decompress.none",
        )

    def _new_slice_from_selection_for(self, entry: Entry) -> None:
        """The files dock's New Slice from Selection — the selection lives on
        the on-screen entry, so anything else (a stale menu) is ignored."""
        if entry is self._workspace.current:
            self._new_slice_from_selection()

    def _new_slice_from_selection(self) -> None:
        """File ▸ New Slice from Selection: the selected tiles' byte range.

        Raw prefill (no decompressor): the selection is a run of *decoded
        raw* tiles, so unlike from-view the compression preview combo does
        not describe it.
        """
        src = self._raw_slice_source()
        if src is None:
            return
        entry, doc = src
        rng = self._selection_byte_range()
        if rng is None:
            return
        # Same tile→byte mapping as the hex highlight, but the trailing (possibly
        # partial) tile is clamped to the bytes that exist — a slice can't run
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
        """The files dock's Edit… — rewrite a slice's coordinates in place.

        The same dialog as New Slice, prefilled with the current values; on OK
        the entry is re-pointed and its cached document dropped, so the region
        is re-read (immediately when it is on screen, else on activation).
        """
        if entry.kind is not EntryKind.SLICE:
            return
        if entry.dirty:
            answer = QMessageBox.question(
                self,
                "Celpix — edit slice",
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
            return  # OK'd unchanged — nothing happened, nothing to undo
        self._push_command(SliceEditCommand(self, entry, before=before, after=params))

    def _apply_slice_params(self, entry: Entry, params: SliceParams) -> None:
        """Re-point a slice's coordinates and re-read the region — the
        application path for slice edits and their undos; works for
        non-current entries (their reload waits until activation)."""
        entry.name = params.name
        entry.slice_offset = params.offset
        entry.slice_length = params.length
        entry.decompress_id = params.decompress_id
        self._workspace.set_dirty(entry, False)  # edits die with the old region
        entry.doc = None
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
        deliberately *not* applied — the parent reads raw, so a raw slice shows
        exactly its own tiles at their true file address (a decompressed slice
        still lands on the right offset, over the packed source bytes).
        """
        if slice_entry.kind is not EntryKind.SLICE:
            return
        parent = self._workspace.find_file(slice_entry.path)
        if parent is None:
            parent = self._workspace.open_file(slice_entry.path)
        # The slice's settings live on its session (seeded on first load); seed
        # it from the toolbar if it was never activated, exactly as a load would.
        if slice_entry.session is None:
            slice_entry.session = self._seed_session(slice_entry)
        src = slice_entry.session
        # Adopt the slice's interpretation but keep the parent's own header skip
        # — a slice has no header concept, and its offsets are absolute anyway.
        prior = parent.session
        parent.session = EntrySession(
            pixel_preset_id=src.pixel_preset_id,
            palette_preset_id=src.palette_preset_id,
            palette_mode=src.palette_mode,
            headered=prior.headered if prior is not None else False,
            header_length=prior.header_length if prior is not None else 512,
        )
        parent.pending_palette = palette_source_for(slice_entry)
        # Keep the parent's view geometry (columns/rows/zoom/grid); the origin
        # is landed after load, once the new preset's tile size is known.
        prior_view = parent.doc.view if parent.doc is not None else parent.pending_view
        parent.pending_view = (
            replace(prior_view, tile_offset=0, byte_nudge=0)
            if prior_view is not None
            else None
        )
        # Drop the cached document so the parent re-reads through the slice's
        # codecs (no edits exist to lose today); reload in place if it is already
        # current, else on activation.
        parent.doc = None
        if parent is self._workspace.current:
            self._on_current_entry_changed(parent)
        else:
            self._activate_entry(parent)
        # Land on the slice only if the parent actually loaded — a vanished file
        # or bad codec leaves the previous view untouched.
        if self._workspace.current is parent and self._doc is not None:
            self._land_on_byte(slice_entry.slice_offset)
            self.statusBar().showMessage(
                f"Jumped to {slice_entry.name} in {parent.name}"
            )

    def _land_on_byte(self, file_offset: int) -> None:
        """Move the view origin to an absolute file byte, without pushing an undo.

        Same byte→tile/nudge split as :meth:`_set_byte_position`, but it applies
        the position directly: a jump-to-source is navigation, not an edit.
        """
        if self._doc is None:
            return
        pos = file_offset - self._display_base()
        offset, nudge = self._doc.clamp_byte_position(
            pos, self._columns.value(), self._rows.value()
        )
        self._apply_offset(offset, nudge)

    # -- bookmarks -----------------------------------------------------------
    def _new_bookmark_current(self) -> None:
        """File ▸ New Bookmark on the current entry's file."""
        entry = self._workspace.current
        if entry is not None:
            self._new_bookmark_for(entry)

    def _new_bookmark_for(self, entry: Entry) -> None:
        """Bookmark ``entry``'s current position and settings (current FILE
        only — the snapshot reads the live view, which nothing else has).

        The snapshot is the same trio a project persists per entry — session,
        view options, palette source — copied off the live state, plus the
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
            # and all) — the icon, not the name, marks it as a bookmark.
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
        wholesale — session (header settings included: the snapshot *is* the
        parent's own state as of creation), palette source and view geometry
        are copied onto the parent, its cached document dropped so it re-reads
        through them, and the view lands on the absolute offset. Copies, never
        the originals: the parent's first load consumes its pending fields,
        and the bookmark must survive to be jumped to again.
        """
        if bookmark.kind is not EntryKind.BOOKMARK:
            return
        parent = self._workspace.find_file(bookmark.path)
        if parent is None:
            parent = self._workspace.open_file(bookmark.path)
        if bookmark.session is not None:
            parent.session = replace(bookmark.session)
        parent.pending_view = (
            replace(bookmark.pending_view)
            if bookmark.pending_view is not None
            else None
        )
        parent.pending_palette = (
            replace(bookmark.pending_palette)
            if bookmark.pending_palette is not None
            else None  # the snapshot renders through the default palette
        )
        # Drop the cached document so the parent re-reads through the snapshot
        # (no edits exist to lose today); reload in place if it is already
        # current, else on activation.
        parent.doc = None
        if parent is self._workspace.current:
            self._on_current_entry_changed(parent)
        else:
            self._activate_entry(parent)
        # Land on the bookmark only if the parent actually loaded — a vanished
        # file or bad codec leaves the previous view untouched.
        if self._workspace.current is parent and self._doc is not None:
            self._land_on_byte(bookmark.slice_offset)
            self.statusBar().showMessage(f"Jumped to {bookmark.name} in {parent.name}")

    def _create_slice_via_dialog(
        self,
        path: str,
        *,
        offset: int = 0,
        length: int | None = None,
        decompress_id: str = "decompress.none",
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
        entry = Entry(
            name=params.name,
            kind=EntryKind.SLICE,
            path=path,
            slice_offset=params.offset,
            slice_length=params.length,
            decompress_id=params.decompress_id,
        )
        self._seed_slice_from_parent(entry)
        self._push_command(AddEntryCommand(self, entry, f'new slice "{entry.name}"'))

    def _on_promote_structure(self) -> None:
        """One click: the complete structure in view becomes a slice entry."""
        src = self._raw_slice_source()
        if src is None or self._structure_extent is None:
            return
        entry, doc = src
        start, consumed = self._structure_extent
        abs_off = doc.pixel_config.source.offset + start
        decompress_id = self._compression.currentData() or "decompress.none"
        slice_entry = Entry(
            name=default_slice_name(abs_off, consumed, decompress_id),
            kind=EntryKind.SLICE,
            path=entry.path,
            slice_offset=abs_off,
            slice_length=consumed,
            decompress_id=decompress_id,
        )
        self._seed_slice_from_parent(slice_entry)
        self._push_command(
            AddEntryCommand(self, slice_entry, f'new slice "{slice_entry.name}"')
        )

    # -- view --------------------------------------------------------------
    def _on_view_change(self, *_args) -> None:
        if self._doc is not None:
            self._refresh_view()

    def _render_arrangement(
        self,
        pixel_bytes: bytes,
        engine,  # noqa: ANN001 — a pixel-interpret plugin
        params,  # noqa: ANN001 — the preset's engine params
        layout: BlockLayout,
        two_dimensional: bool,
        max_rows: int | None,
    ):
        """Decode a pixel-byte buffer through the arrangement into a rendered image.

        The shared core of the live view and the decompression overlay, so blocks
        and 2D behave identically in both: 2D reflow → decode → block layout →
        render. ``pixel_bytes`` begins at the view origin — a window of the doc's
        bytes for the live view, a decompressed scratch for the overlay.
        ``max_rows`` caps the composed height (the live view's fixed window);
        ``None`` sizes to the data (the overlay shows the whole structure). Returns
        ``(QImage, real tile count)`` — the count excludes any 2D reflow padding, so
        the canvas can background the rest.
        """
        assert self._doc is not None
        cols = layout.columns
        tile_bytes = engine.bytes_per_tile(params)
        _tw, tile_h = engine.tile_size(params)
        filled = ceil_div(len(pixel_bytes), tile_bytes) if tile_bytes else 0
        buffer = (
            reflow_2d(pixel_bytes, tile_bytes, tile_h, cols)
            if two_dimensional
            else pixel_bytes
        )
        # Zero-pad the trailing partial tile so a short structure still decodes.
        if tile_bytes and len(buffer) % tile_bytes:
            buffer = buffer + bytes(-len(buffer) % tile_bytes)
        tiles = engine.decode(buffer, params, PipelineContext()) if buffer else []
        # Rows the tiles occupy under this layout (plain: ceil; blocked: the tallest
        # cell). Capped to the fixed window for the live view; uncapped for overlay.
        need_rows = (
            1 + max(layout.slot_to_cell(s)[1] for s in range(len(tiles)))
            if tiles
            else 1
        )
        canvas_rows = (
            need_rows if max_rows is None else max(1, min(max_rows, need_rows))
        )
        if layout.is_plain:
            # Narrow a single partial row to its tiles; a taller window keeps full
            # width and lets the canvas background the trailing slots.
            shown_cols = cols if need_rows > 1 else min(cols, max(1, len(tiles)))
            grid = compose_window(tiles, shown_cols, 0, canvas_rows)
        else:
            grid = compose_window(tiles, cols, 0, canvas_rows, layout)
        base = self._doc.view.subpalette_row * self._index_space()
        return render_bridge.render(grid, self._doc.palette, base), filled

    def _refresh_view(self) -> None:
        assert self._doc is not None
        cols = self._columns.value()
        # Rows is a free display-window height (bounded only by the spin's own 256
        # cap), not by the data. Asking for more rows than the file fills just
        # leaves the neutral background showing past the last tile row (see
        # shown_rows below) instead of clamping the input — so the height survives
        # switching to a format whose larger tiles leave far fewer rows of data.
        # Re-clamp the offset next: a smaller file, or a bigger window (cols/rows),
        # can push the previous offset past the last page.
        self._offset = self._doc.clamp_offset(
            self._offset, cols, self._rows.value(), self._nudge
        )
        rows = self._rows.value()
        # Clamp the subpalette row to the rows the loaded palette actually has —
        # switching to a smaller palette (e.g. Offset's 16 rows back to Default's
        # one) must not leave the view pointing past it. Signals blocked: this
        # is a correction, not a user change, and must not re-enter here.
        group = self._index_space()  # the subpalette row size
        max_row = max(0, len(self._doc.palette) - 1) // group
        if self._subpalette.value() > max_row:
            self._subpalette.blockSignals(True)
            self._subpalette.setValue(max_row)
            self._subpalette.blockSignals(False)
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
        # A reload can recolour (or drop) the selected entry under the same index.
        self._update_color_details()
        self._sync_nav()
        self._refresh_overlay()
        self._refresh_hex()

    def _refresh_hex(self) -> None:
        """Feed the hex panel a dump of the file bytes at the current offset.

        Cheap no-op while the dock is hidden (its usual state). The dump starts
        at the row holding the current view origin — so the offset's row is
        always the top line — and highlights the currently selected tile(s),
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

    def _selection_byte_range(self) -> tuple[int, int] | None:
        """The selected tile run as a ``(start, length)`` byte range in the
        document, or None with nothing selected — the hex panel's highlight.

        Same tile→byte mapping as New Slice from Selection: tiles are laid out
        linearly at ``bytes_per_tile`` each, shifted by the grid's byte nudge.
        """
        assert self._doc is not None
        if self._selected_tile is None:
            return None
        tb = self._doc.bytes_per_tile
        last = (
            self._selected_last
            if self._selected_last is not None
            else (self._selected_tile)
        )
        start = self._nudge + self._selected_tile * tb
        return start, (last - self._selected_tile + 1) * tb

    def _refresh_overlay(self) -> None:
        """Feed the floating decompression preview, or hide it.

        A parallel, view-only run of the pipeline over the current window's raw
        bytes: Decompress (best-effort — the window may cut a structure short)
        then the same pixel-interpret and palette paths the main view uses, so
        the overlay always reflects the active preset, palette row, and zoom.
        The main document is untouched; failure to decompress means "no
        structure starts at this offset" and simply hides the preview.
        """
        assert self._doc is not None
        decompress_id = self._compression.currentData() or "decompress.none"
        active = decompress_id != "decompress.none"
        self._scan_button.setEnabled(active and not self._scanning)
        self._next_structure = None
        self._structure_extent = None
        try:
            self._present_overlay(decompress_id if active else None)
        finally:
            # Jump is armed only while a whole structure (known end) is in view;
            # promote also needs the view's positions to map to file offsets.
            self._jump_next.setEnabled(self._next_structure is not None)
            self._promote_button.setEnabled(
                self._structure_extent is not None
                and self._doc.pixel_config.decompress_id == "decompress.none"
            )

    def _present_overlay(self, decompress_id: str | None) -> None:
        """The overlay body of :meth:`_refresh_overlay` (which owns the button
        state around every early exit here)."""
        assert self._doc is not None
        view = self._doc.view
        window = self._doc.window_bytes(
            view.tile_offset, view.columns * view.rows, view.byte_nudge
        )
        if decompress_id is None or not window:
            self._overlay.hide_overlay()
            return
        ctx = PipelineContext()
        ctx.set(KEY_DECOMPRESS_PARTIAL, True)
        engine, preset = self._registry.engine_for(self._pixel_preset_id())
        layout = BlockLayout(
            view.columns, view.block_columns, view.block_rows, view.block_order
        )
        try:
            plugin = self._registry.plugin(Stage.DECOMPRESS, decompress_id)
            raw = plugin.decompress(bytes(window), ctx)
            if not raw:  # nothing decompressed: not a structure
                self._overlay.hide_overlay()
                return
            # Same arrangement path as the live view (2D reflow / block layout),
            # but sized to the whole decompressed structure (max_rows=None).
            image, _ = self._render_arrangement(
                raw, engine, preset.params, layout, view.two_dimensional, max_rows=None
            )
        except Exception:  # noqa: BLE001 — any failure means "not a structure"
            self._overlay.hide_overlay()
            return

        parts = [f"{len(raw):#x} B raw from {len(window):#x} B window"]
        consumed = ctx.get(KEY_COMPRESSED_SIZE)
        if consumed and ctx.get(KEY_DECOMPRESS_COMPLETE):
            # The structure's own end was inside the window: report its true
            # extent, and arm Jump-to-Next at the byte right after it.
            parts.append(f"structure {consumed:#x} B")
            self._structure_extent = (self._byte_position(), consumed)
            after = self._byte_position() + consumed
            if after < len(self._doc.pixel_data):
                self._next_structure = after
        rows16 = ctx.get(KEY_LZ16_ROWS)
        if rows16 is not None:
            parts.append(f"{rows16} tile row(s)")
        self._overlay.show_result(
            image,
            engine.tile_size(preset.params),
            view.zoom,
            view.show_grid,
            f"Decompressed — {plugin.info.name}",
            ", ".join(parts),
        )

    def _on_jump_next(self) -> None:
        if self._doc is None or self._next_structure is None:
            return
        self._set_byte_position(self._next_structure)

    def _on_scan(self) -> None:
        """Scan forward for the next decodable structure (re-entered by Stop).

        Runs inline, pumping the event loop between batches so Stop stays
        clickable; everything else is frozen by :meth:`_set_scan_ui`. A hit is
        a *complete*, non-empty structure under a strict decode — a
        best-effort partial decode "succeeds" on almost any bytes, so it can't
        be the criterion (which also means non-self-delimiting schemes like
        LZ16 are effectively unscannable — there is nothing in the stream to
        recognise).
        """
        if self._scanning:
            self._scan_stop = True
            return
        if self._doc is None:
            return
        decompress_id = self._compression.currentData() or "decompress.none"
        if decompress_id == "decompress.none":
            return
        plugin = self._registry.plugin(Stage.DECOMPRESS, decompress_id)
        data = self._doc.pixel_data
        window_len = max(
            1,
            self._columns.value() * self._rows.value() * self._doc.bytes_per_tile,
        )
        self._scanning = True
        self._scan_stop = False
        self._set_scan_ui(True)
        try:
            result = pipeline.find_next_structure(
                data,
                plugin,
                window_len,
                self._byte_position() + 1,
                on_tick=self._scan_tick,
            )
        finally:
            self._scanning = False
            self._set_scan_ui(False)
        # Land where the scan ended — the hit, or wherever Stop/EOF left it.
        landing = result.found if result.found is not None else result.end
        self._set_byte_position(min(landing, len(data) - 1))
        if result.found is not None:
            self.statusBar().showMessage(
                f"Structure found at {self._format_offset(result.found)}."
            )
        elif result.stopped:
            self.statusBar().showMessage("Scan stopped.")
        else:
            self.statusBar().showMessage("Scan reached the end without a match.")

    def _scan_tick(self, pos: int) -> bool:
        """Progress callback for :func:`~celpix.pipeline.find_next_structure`:
        report the position, pump the event loop so Stop stays clickable, and
        return whether Stop was pressed (which aborts the scan)."""
        self.statusBar().showMessage(f"Scanning… {self._format_offset(pos)}")
        QApplication.processEvents()
        return self._scan_stop

    def _set_scan_ui(self, active: bool) -> None:
        """Swap Scan⇄Stop and freeze the rest of the UI while a scan runs."""
        self._scan_button.setText("Stop" if active else "Scan")
        for widget in (
            self.menuBar(),
            self.centralWidget(),
            self._palette_dock,
            self._files_dock,
            self._arrange_toolbar,
            self._view_toolbar,
            self._pixel_preset,
            self._palette_preset,
            self._compression,
            self._headered,
            self._header_len,
            self._jump_next,
            self._promote_button,
        ):
            widget.setEnabled(not active)
        # The blanket re-enable above must not resurrect controls that the
        # current entry keeps off (header skip is a whole-file setting; the block
        # controls stay locked unless the Pattern picker is on Custom).
        current = self._workspace.current
        if not active and current is not None:
            is_file = current.kind is EntryKind.FILE
            self._headered.setEnabled(is_file)
            self._header_len.setEnabled(is_file)
            self._apply_pattern_lock()

    def _refresh_selection(self, window_tiles: int) -> None:
        """Re-derive the canvas highlight after the window moved or resized.

        Scrolling away hides the highlight but keeps the selection, so scrolling
        back restores it; a range half in view paints just its visible part. A
        selection starting past the file's end (file shrank) is dropped, one
        merely running past it is trimmed.
        """
        assert self._doc is not None
        span: tuple[int, int] | None = None
        if self._selected_tile is not None:
            if self._selected_tile >= self._doc.tile_count:
                self._clear_selection()
                return
            last_abs = (
                self._selected_last
                if self._selected_last is not None
                else self._selected_tile
            )
            self._selected_last = min(last_abs, self._doc.tile_count - 1)
            first = max(self._selected_tile - self._offset, 0)
            last = min(self._selected_last - self._offset, window_tiles - 1)
            if first <= last:
                span = (first, last)
        self._canvas.set_selection(span)

    def _update_color_details(self) -> None:
        """Render the panel's selected colour into the details readout.

        The position reads as subpalette + colour-within-it (the pixel format's
        index space sizes the subpalette), matching how tiles actually reference
        the entry — not as a flat palette index.
        """
        index = self._palette_panel.selected_index()
        if self._doc is None or index is None:
            text = "No colour selected"
        else:
            subpal, color = divmod(index, self._index_space())
            argb = self._doc.palette.color(index)
            a = (argb >> 24) & 0xFF
            r = (argb >> 16) & 0xFF
            g = (argb >> 8) & 0xFF
            b = argb & 0xFF
            text = (
                f"Subpal {subpal} · Colour {color} (${color:X}) · #{argb:08X}\n"
                f"R {r}  G {g}  B {b}  A {a}"
            )
        # Runs on every view refresh (navigation included) — skip the label
        # update when nothing about the selection changed.
        if text != self._color_details.text():
            self._color_details.setText(text)

    def _fallback_palette(self) -> Palette:
        return Palette.default(self._index_space())

    def _alert(self, message: str, *, title: str = "Celpix", detail: str = "") -> None:
        """The one place errors and warnings reach the user, as a modal dialog.

        A status-bar line is easy to miss — it's silent and scrolls away — so
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
        self._alert(str(exc), title="Celpix — pipeline error")

    def _alert_plugin_issues(self) -> None:
        """Modal listing plugins that failed to load — shown at startup and
        after a refresh, and reachable again from File ▸ Open plugins folder."""
        if not self._plugin_issues:
            return
        detail = "\n".join(f"• {i.path}: {i.message}" for i in self._plugin_issues)
        self._alert(
            f"{len(self._plugin_issues)} plugin(s) failed to load. The rest of "
            "the app works normally; see the details, or File ▸ Open plugins "
            "folder.",
            title="Celpix — plugin load issues",
            detail=detail,
        )

    def _partial_tile_note(self) -> str:
        """Status-bar warning when the data ends mid-tile, or ``""`` when aligned.

        Not an error: the trailing partial tile renders zero-padded, so the file
        stays viewable — the note just explains the padded tail.
        """
        assert self._doc is not None
        short = -len(self._doc.pixel_data) % self._doc.bytes_per_tile
        if not short:
            return ""
        return (
            f"data ends {short} byte(s) short of a whole "
            f"{self._doc.bytes_per_tile}-byte tile; the last tile is zero-padded"
        )
