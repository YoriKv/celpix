"""The application main window: open pixel/palette data, view it, save it back.

Deliberately basic — File, Palette and View menus, a control strip (preset,
palette format, columns, rows, zoom, subpalette row, grid), a scrollable
:class:`~celpix.ui.canvas.Canvas` showing a windowed view with single-tile
selection, a right-side palette dock, and a navigation bar.
It drives the Qt-free pipeline through the plugin registry and never interprets
bytes itself; all decode/encode goes through
``pipeline``. This is the shell the View & Edit tools attach to next (see
``docs/design/architecture.md`` §5).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QEvent, Qt, QUrl
from PySide6.QtGui import (
    QAction,
    QDesktopServices,
    QDragEnterEvent,
    QDropEvent,
    QImage,
    QKeySequence,
    QPalette,
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

from celpix.core.address import (
    BANK_PRESETS,
    BankLayout,
    BankPreset,
    SplitBankLayout,
    format_hex,
    parse_hex,
)
from celpix.core.arrangement import compose_window
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
    Workspace,
    backfill_slice_length,
    default_slice_name,
    pixel_config_for,
)
from celpix.ui import render_bridge
from celpix.ui.canvas import CANVAS_BACKGROUND, Canvas
from celpix.ui.decompress_overlay import DecompressOverlay
from celpix.ui.file_list_panel import FileListPanel
from celpix.ui.palette_panel import PalettePanel
from celpix.ui.slice_dialog import SliceDialog
from celpix.ui.widgets import CommittingLineEdit, CompactComboBox

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
        # The .celpix file this session was loaded from / last saved to, so
        # File ▸ Save Project can rewrite it without re-asking for a path.
        self._project_path: str | None = None
        # Where the palette comes from: "default" (the generated fallback),
        # "file" (Open palette…), or "offset" (read from the pixel file). The
        # dock's mode dropdown is a view of this member.
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
        # loses focus (see _reload_pixel / the focus_lost hookup).
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
        if self._plugin_issues:
            self.statusBar().showMessage(
                f"Open pixel data to begin. "
                f"({len(self._plugin_issues)} plugin(s) failed to load — see File ▸ "
                f"Open plugins folder.)"
            )
        else:
            self.statusBar().showMessage("Open pixel data to begin.")

    # -- construction ------------------------------------------------------
    @property
    def _has_palette_file(self) -> bool:
        """Whether real palette data is loaded (vs the generated fallback).

        Derived from the load mode so the two can never diverge; the historical
        name is kept so the pixel/palette reload guards read naturally.
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
        self._files_panel.edit_slice_requested.connect(self._edit_slice)
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

    def _on_entry_added(self, entry: Entry) -> None:
        # The panel nests a slice under its parent file's item when it's open.
        self._files_panel.add_entry(entry, self._workspace.parent_of(entry))

    def _on_entry_dirty_changed(self, entry: Entry) -> None:
        self._files_panel.refresh_entry(entry)
        self._write_all_action.setEnabled(bool(self._workspace.dirty_entries()))

    def _rename_entry(self, entry: Entry, name: str) -> None:
        entry.name = name
        self._files_panel.refresh_entry(entry)
        if entry is self._workspace.current:
            self.setWindowTitle(f"Celpix — {name}")

    # -- entry switching -----------------------------------------------------
    def _activate_entry(self, entry: Entry) -> None:
        """Switch the view to ``entry`` — every activation path funnels here."""
        if entry is None or entry is self._workspace.current:
            return
        fresh = entry.doc is None
        if fresh and not self._load_entry(entry):
            # Load failed (bad codec/vanished file): stay put, and snap the
            # list highlight back onto the entry actually shown.
            self._files_panel.set_current(self._workspace.current)
            return
        self._capture_session()
        self._workspace.set_current(entry)  # -> _on_current_entry_changed
        self._canvas.setFocus()  # arm arrow-key navigation on the fresh view
        if fresh:
            message = f"Loaded {entry.doc.tile_count} tiles from {entry.name}"
            note = self._partial_tile_note()
            self.statusBar().showMessage(f"{message} — {note}" if note else message)

    def _on_current_entry_changed(self, entry: Entry | None) -> None:
        self._files_panel.set_current(entry)
        if entry is None:
            self._show_empty()
            return
        # Already loaded on the _activate_entry path; a close() repointing
        # current to a never-activated (or invalidated) neighbour lands here.
        if entry.doc is None and not self._load_entry(entry):
            self._show_empty()
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
        palette with a status note — a project load never fails on it.
        """
        doc, session = entry.doc, entry.session
        assert doc is not None and session is not None
        if entry.pending_view is not None:
            doc.view = entry.pending_view
            entry.pending_view = None
        source, entry.pending_palette = entry.pending_palette, None
        if source is None:
            return
        try:
            if source.colors is not None:
                doc.palette = Palette(source.colors)
                return
            if source.path is not None:  # an external palette file
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
        except (PipelineError, OSError) as exc:
            session.palette_mode = "default"
            self.statusBar().showMessage(f"{entry.name}: palette not restored ({exc})")

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
        if entry is None:
            return
        if entry.doc is not None:
            entry.doc.view.offset = self._offset
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
        for combo, data in (
            (self._pixel_preset, session.pixel_preset_id),
            (self._palette_preset, session.palette_preset_id),
            (self._compression, session.compression_id),
        ):
            combo.blockSignals(True)
            index = combo.findData(data)
            if index >= 0:  # a plugin refresh may have dropped the preset
                combo.setCurrentIndex(index)
            combo.blockSignals(False)
        for spin, value in (
            (self._columns, view.columns),
            (self._rows, view.rows),
            (self._zoom, view.zoom),
            (self._subpalette, view.subpalette_row),
            (self._header_len, session.header_length),
        ):
            spin.blockSignals(True)
            spin.setValue(value)
            spin.blockSignals(False)
        for check, value in (
            (self._grid, view.show_grid),
            (self._headered, session.headered),
        ):
            check.blockSignals(True)
            check.setChecked(value)
            check.blockSignals(False)
        # Header skip is FILE display state; a slice's offsets are absolute
        # file offsets and must not shift under it.
        is_file = entry.kind is EntryKind.FILE
        self._headered.setEnabled(is_file)
        self._header_len.setEnabled(is_file)
        self._offset, self._nudge = view.offset, view.byte_nudge
        self._selected_tile = session.selected_tile
        self._selected_last = (
            session.selected_last
            if session.selected_last is not None
            else session.selected_tile
        )
        self._update_selection_actions()
        self._set_palette_mode(session.palette_mode)
        self._write_action.setEnabled(entry.doc.pixel_config.write_enabled)
        # Slices are carved out of raw byte views; a decompressed stream's
        # positions don't map back to file offsets, so it can't spawn them.
        can_slice = entry.doc.pixel_config.decompress_id == "decompress.none"
        self._new_slice_action.setEnabled(can_slice)
        self._new_slice_from_view_action.setEnabled(can_slice)
        self.setWindowTitle(f"Celpix — {entry.name}")

    def _show_empty(self) -> None:
        """Nothing open: clear the canvas, disable everything document-bound."""
        self._doc = None
        self._selected_tile = None
        self._selected_last = None
        self._canvas.set_selection(None)
        self._update_selection_actions()
        self._canvas.set_image(QImage())
        self._overlay.hide_overlay()
        self._write_action.setEnabled(False)
        self._new_slice_action.setEnabled(False)
        self._new_slice_from_view_action.setEnabled(False)
        self.setWindowTitle("Celpix")
        self._sync_nav()
        self._announce_ready()

    def _remove_entry(self, entry: Entry) -> None:
        """Remove ``entry`` from the list (a file takes its slices with it),
        always confirming first — Remove is also on the Delete key, and a slip
        there costs the entry's whole session setup."""
        victims = [entry, *self._workspace.slices_of(entry)]
        dirty = [e.name for e in victims if e.dirty]
        message = f"Remove {entry.name}?"
        parts = []
        if len(victims) > 1:
            parts.append(f"removes its {len(victims) - 1} slice(s)")
        if dirty:
            parts.append(f"discards unsaved changes ({', '.join(dirty)})")
        if parts:
            message = f"Remove {entry.name}? This also " + " and ".join(parts) + "."
        answer = QMessageBox.question(self, "Celpix — remove", message)
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._workspace.close(entry)  # current repoints via _on_current_entry_changed

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
            "file, or an offset into the open pixel file"
        )
        for name in ("Default", "File", "Offset"):
            self._palette_mode_combo.addItem(name, name.lower())
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

        self._build_view_menu()
        self._build_palette_menu()
        self._build_panels_menu()

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
        """View ▸ the navigation actions — the menu home for every nav key.

        Some of these also have navbar buttons; the rest (first/last page, page
        steps, window resizing) live only here and on the keyboard, so the menu
        doubles as the discoverable list of navigation shortcuts.
        """
        menu = self.menuBar().addMenu("View")
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
        """Panels ▸ show/hide the dockable panels (Files, Palette)."""
        menu = self.menuBar().addMenu("Panels")
        files_toggle = self._files_dock.toggleViewAction()
        files_toggle.setText("Files Panel")
        menu.addAction(files_toggle)
        palette_toggle = self._palette_dock.toggleViewAction()
        palette_toggle.setText("Palette Panel")
        menu.addAction(palette_toggle)

    def _open_plugins_folder(self) -> None:
        if self._plugin_dir is None:
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(self._plugin_dir))
        if self._plugin_issues:
            detail = "\n".join(f"• {i.path}: {i.message}" for i in self._plugin_issues)
            QMessageBox.warning(
                self,
                "Celpix — plugin load issues",
                "Some plugins failed to load:\n\n" + detail,
            )

    def _build_toolbar(self) -> None:
        # Two stacked rows: the codec selects (what the bytes *are*) on top, the
        # view settings (how they're shown) below. The break keeps the second
        # bar on its own row instead of flowing after the first.
        codecs = QToolBar("Codecs")
        self.addToolBar(codecs)
        self.addToolBarBreak()
        view = QToolBar("View")
        self.addToolBar(view)
        self._view_toolbar = view  # frozen wholesale during a scan
        for bar in (codecs, view):
            bar.layout().setSpacing(10)

        self._pixel_preset = self._preset_combo(Stage.INTERPRET_PIXEL, "snes-4bpp")
        self._pixel_preset.currentIndexChanged.connect(self._reload_pixel)
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
            "Scan forward byte-by-byte for the next complete compressed "
            "structure; turns into Stop while running."
        )
        self._scan_button.setEnabled(False)
        self._scan_button.clicked.connect(self._on_scan)
        codecs.addWidget(self._scan_button)
        # One click promotes the complete structure in view into a decompressed
        # slice entry in the files list — the overlay preview made editable.
        self._promote_button = QPushButton("To Slice")
        self._promote_button.setToolTip(
            "Add the structure in view to the file list as a decompressed slice"
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

        self._grid = QCheckBox("Grid")
        self._grid.toggled.connect(self._on_view_change)
        view.addWidget(self._grid)

    def _header_offset(self) -> int:
        """File bytes to skip before data begins (0 while 'Header' is unchecked)."""
        return self._header_len.value() if self._headered.isChecked() else 0

    def _on_header_change(self, *_args) -> None:
        if self._doc is None:
            return
        # A length edit only matters while the skip is armed; unchecking must
        # reload too, to drop a previously applied skip.
        if self._headered.isChecked() or self._doc.pixel_config.source.offset:
            self._reload_pixel()

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
        self._addr_format = QComboBox()
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
            # Not navigation, but the same routing need: a bare letter key that
            # must yield to focused text inputs (Palette ▸ Load from Selection).
            (Qt.Key.Key_P, *no_mod): self._load_palette_from_selection,
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
        if self._doc is None:
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
        self._offset, self._nudge = offset, nudge
        self._refresh_view()

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
        another format's index space (e.g. the outgoing preset in _reload_pixel).
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
        already open activates its existing entry — identity is the path.
        """
        self._activate_entry(self._workspace.open_file(path))

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
            QMessageBox.warning(self, "Celpix — project", str(exc))
            return
        if loaded.version > projectfile.PROJECT_VERSION:
            QMessageBox.warning(
                self,
                "Celpix — project",
                "This project was saved by a newer Celpix. It opens with what "
                "this version understands, but saving will rewrite it at "
                f"version {projectfile.PROJECT_VERSION}, dropping the rest.",
            )
        self._workspace.replace(loaded.entries, loaded.current)
        self._project_path = path
        self.statusBar().showMessage(
            f"Loaded project {Path(path).name} ({len(loaded.entries)} entries)."
        )

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
            QMessageBox.warning(self, "Celpix — project", f"Cannot write {path}: {exc}")
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
        if not self._has_palette_file:
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
        self._doc.palette = colors
        self._doc.palette_config = cfg
        self._doc.palette_ctx = ctx
        self._set_palette_mode("file")
        self._refresh_view()
        self.statusBar().showMessage(f"Loaded {len(colors)} colours from {path}")
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
        combo = self._palette_mode_combo
        combo.blockSignals(True)
        combo.setCurrentIndex(combo.findData(mode))
        combo.blockSignals(False)
        self._palette_offset_edit.setEnabled(mode == "offset")
        # Mid-commit the box refreshes itself afterwards; don't fight it.
        if not self._palette_offset_edit.hasFocus():
            self._palette_offset_edit.refresh()

    def _on_palette_mode_change(self) -> None:
        """Act on a user pick in the mode dropdown; revert the combo on failure.

        self._palette_mode still holds the OLD mode here (it is only updated by
        _set_palette_mode on success), so reverting is just re-syncing to it.
        """
        mode = self._palette_mode_combo.currentData()
        if mode == self._palette_mode:
            return
        if self._doc is None:
            self.statusBar().showMessage("Open pixel data first.")
            self._set_palette_mode(self._palette_mode)
            return
        if mode == "default":
            self._apply_default_palette()
        elif mode == "file":
            if not self._open_palette():
                self._set_palette_mode(self._palette_mode)
        elif mode == "offset":
            if not self._load_palette_at_offset(self._initial_palette_offset()):
                self._set_palette_mode(self._palette_mode)

    def _apply_default_palette(self) -> None:
        """Back to the generated default palette (mode "default")."""
        assert self._doc is not None
        self._doc.palette = self._fallback_palette()
        self._doc.palette_config = self._placeholder_palette_config()
        self._doc.palette_ctx = PipelineContext()
        self._set_palette_mode("default")
        self._refresh_view()
        self.statusBar().showMessage("Using the default palette.")

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

    def _update_selection_actions(self) -> None:
        """Converge everything gated on 'a selection exists' with the state."""
        has = self._selected_tile is not None
        self._load_selection_action.setEnabled(has)
        can_slice = (
            self._doc is not None
            and self._doc.pixel_config.decompress_id == "decompress.none"
        )
        self._new_slice_from_selection_action.setEnabled(has and can_slice)
        self._files_panel.set_has_selection(has)

    def _selection_palette_source(
        self, path: str, byte_off: int, preset_id: str | None = None
    ) -> FileRef | None:
        """A read window for up to 256 palette entries at ``byte_off``.

        Floored to whole entries — the colour codecs reject a partial trailing
        entry, so clamping at EOF alone is not enough. ``None`` when not even one
        entry fits. ``preset_id`` overrides the combo when sizing entries for a
        non-current entry's palette format (project restore).
        """
        bpe = pipeline.palette_entry_size(
            preset_id or self._palette_preset_id(), self._registry
        )
        avail = Path(path).stat().st_size - byte_off
        entries = min(256, max(0, avail) // bpe)
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
            self.statusBar().showMessage(f"Cannot read {src.path}: {exc}")
            return False
        if ref is None:
            self.statusBar().showMessage(
                "Not enough data at that offset for a palette entry."
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
        self._doc.palette = colors
        self._doc.palette_config = cfg
        self._doc.palette_ctx = ctx
        # Mode "offset" keeps pixel reloads from restoring the default palette.
        self._set_palette_mode("offset")
        self._refresh_view()
        self.statusBar().showMessage(
            f"Loaded {len(colors)} colours from "
            f"{self._format_offset(byte_off)} (view-only)"
        )
        return True

    def _load_palette_from_selection(self) -> None:
        """Palette ▸ Load from Selection: Offset mode at the selected tile."""
        if self._doc is None or self._selected_tile is None:
            return
        self._load_palette_at_offset(self._tile_byte_offset(self._selected_tile))

    def _reload_pixel(self) -> None:
        """Re-load the pixel bytes + geometry under a newly chosen preset.

        The view offset is a tile index, so it maps to a different *byte* position
        under a new bytes-per-tile. Preserve the exact byte position across the
        codec change by converting through bytes; whatever doesn't divide into the
        new codec's tiles becomes the byte nudge. The subpalette row is likewise
        re-anchored: the same row index means a different palette base under the
        new colour count, so it is recomputed from the selected colour (or the
        old base) to keep pointing at the same palette entries.
        """
        entry = self._workspace.current
        if self._doc is None or entry is None:
            return
        # Anchor on the target from the first switch of this run, if one is live,
        # so a series of switches all measure from the same intended position
        # instead of from wherever the previous format's clamping happened to
        # land. The first switch has none yet, so it seeds it from the live view.
        if self._pixel_switch_target is None:
            self._pixel_switch_target = self._byte_position()
        byte_offset = self._pixel_switch_target
        old_group = self._index_space(self._doc.pixel_config.interpret_preset_id)
        # Rebuild from the entry, not the old config: a slice keeps its bounds
        # and codec ids, and a file re-derives the header skip — a header
        # toggle re-lands here via _on_header_change, so it can't be copied.
        cfg = pixel_config_for(
            entry, self._pixel_preset_id(), self._header_offset(), self._registry
        )
        try:
            px = pipeline.load_pixel_data(cfg, self._registry)
        except PipelineError as exc:
            self._report(exc)
            return
        self._store_pixel_data(px, cfg)
        # _refresh_view clamps the offset; the nudge stays < the new tile size.
        self._offset, self._nudge = divmod(byte_offset, px.bytes_per_tile)
        anchor = self._palette_panel.selected_index()
        if anchor is None:
            anchor = self._subpalette.value() * old_group
        # Signals blocked: _refresh_view below re-renders (and re-clamps) once.
        self._subpalette.blockSignals(True)
        self._subpalette.setValue(anchor // self._index_space())
        self._subpalette.blockSignals(False)
        self._clear_selection()  # the same tile index covers different bytes now
        self._refresh_view()
        note = self._partial_tile_note()
        if note:
            self.statusBar().showMessage(f"Preset changed — {note}")

    def _end_pixel_switch_run(self) -> None:
        """Drop the scratch target when the pixel dropdown loses focus.

        The target only spans one uninterrupted bout of format-cycling; once the
        user moves on, the current view *is* the position, so the next switch
        should re-anchor there rather than resurrect a stale byte offset.
        """
        self._pixel_switch_target = None

    def _reload_palette(self) -> None:
        """Re-decode the palette under a newly chosen colour format.

        ``write_enabled`` must carry over from the old config: a from-selection
        palette reads out of the pixel file, and re-arming Write here would make
        Save splice palette bytes into it. Such a config's read window is also
        re-floored, since the new format's entry size may not divide the old
        window's length.
        """
        if self._doc is None or not self._has_palette_file:
            return
        old = self._doc.palette_config
        source = old.source
        if not old.write_enabled and source.length is not None:
            try:
                source = self._selection_palette_source(source.path, source.offset)
            except PipelineError as exc:
                self._report(exc)
                return
            except OSError as exc:
                self.statusBar().showMessage(f"Cannot read {old.source.path}: {exc}")
                return
            if source is None:
                self.statusBar().showMessage(
                    "Not enough data at the palette offset for this format."
                )
                return
        cfg = PathwayConfig(
            source=source,
            interpret_preset_id=self._palette_preset_id(),
            write_enabled=old.write_enabled,
        )
        try:
            colors, ctx = pipeline.load_palette(cfg, self._registry)
        except PipelineError as exc:
            self._report(exc)
            return
        self._doc.palette = colors
        self._doc.palette_config = cfg
        self._doc.palette_ctx = ctx
        self._refresh_view()

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
            # These re-decode the open file's sources through the new registry.
            self._reload_pixel()
            self._reload_palette()

        parts = ["Plugins refreshed"]
        if self._doc is not None:
            parts.append("re-ran on current file")
        if self._plugin_issues:
            parts.append(f"{len(self._plugin_issues)} issue(s) — see File ▸ plugins")
        self.statusBar().showMessage("; ".join(parts) + ".")

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
            self.statusBar().showMessage(
                f"{entry.name} is view-only (its compression has no compressor)."
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
    def _slice_prefill_offset(self) -> int:
        """The view position as an absolute file offset (raw sources only)."""
        assert self._doc is not None
        return self._doc.pixel_config.source.offset + self._byte_position()

    def _new_slice_current(self) -> None:
        """File ▸ New Slice… on the current entry's file."""
        entry = self._workspace.current
        if entry is not None:
            self._new_slice_for(entry)

    def _new_slice_for(self, entry: Entry) -> None:
        """Open the slice dialog for ``entry``'s file (dock context menu)."""
        offset = 0
        # Prefill from the view only when the dialog targets what's on screen
        # and the on-screen stream actually maps to file offsets.
        if (
            entry is self._workspace.current
            and self._doc is not None
            and self._doc.pixel_config.decompress_id == "decompress.none"
        ):
            offset = self._slice_prefill_offset()
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
        entry, doc = self._workspace.current, self._doc
        if (
            entry is None
            or doc is None
            or doc.pixel_config.decompress_id != "decompress.none"
        ):
            return
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
        entry, doc = self._workspace.current, self._doc
        if (
            entry is None
            or doc is None
            or self._selected_tile is None
            or doc.pixel_config.decompress_id != "decompress.none"
        ):
            return
        tb = doc.bytes_per_tile
        # Selection tiles live on the nudged grid; the trailing (possibly
        # partial) tile is clamped to the bytes that exist.
        start = self._nudge + self._selected_tile * tb
        end = min(len(doc.pixel_data), self._nudge + (self._selected_last + 1) * tb)
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
        entry.name = params.name
        entry.slice_offset = params.offset
        entry.slice_length = params.length
        entry.decompress_id = params.decompress_id
        self._workspace.set_dirty(entry, False)  # edits die with the old region
        entry.doc = None
        self._files_panel.refresh_entry(entry)
        if entry is self._workspace.current:
            self._on_current_entry_changed(entry)  # reload the new region now

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
        entry = self._workspace.add_slice(
            path, params.name, params.offset, params.length, params.decompress_id
        )
        self._activate_entry(entry)

    def _on_promote_structure(self) -> None:
        """One click: the complete structure in view becomes a slice entry."""
        entry, doc = self._workspace.current, self._doc
        if (
            entry is None
            or doc is None
            or self._structure_extent is None
            or doc.pixel_config.decompress_id != "decompress.none"
        ):
            return
        start, consumed = self._structure_extent
        abs_off = doc.pixel_config.source.offset + start
        decompress_id = self._compression.currentData() or "decompress.none"
        slice_entry = self._workspace.add_slice(
            entry.path,
            default_slice_name(abs_off, consumed, decompress_id),
            abs_off,
            consumed,
            decompress_id,
        )
        self._activate_entry(slice_entry)

    # -- view --------------------------------------------------------------
    def _on_view_change(self, *_args) -> None:
        if self._doc is not None:
            self._refresh_view()

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
            offset=self._offset,
            byte_nudge=self._nudge,
        )
        # Deferred decode: only the visible window of tiles is decoded, then laid
        # out. Reads back through doc.view (like zoom/grid below) so the freshly
        # stored ViewOptions is genuinely the render input, not a dead mirror.
        view = self._doc.view
        tiles = pipeline.decode_window(
            self._doc, self._registry, view.offset, cols * rows, view.byte_nudge
        )
        # Compose only what the data fills. Fully-empty rows past the end are
        # dropped from the image (shown_rows); a single partial row also narrows
        # to its actual tiles (shown_cols). A partial last row of a multi-row
        # window keeps a rectangular image, and the canvas paints its trailing
        # past-end slots as background (set_filled_tiles). (The zero-padded
        # trailing partial *tile* is real data and stays.)
        shown_cols = min(cols, max(1, len(tiles)))
        shown_rows = min(rows, -(-len(tiles) // max(1, cols)))
        image_grid = compose_window(tiles, shown_cols, 0, shown_rows)
        base = view.subpalette_row * group
        image = render_bridge.render(image_grid, self._doc.palette, base)
        tw, th = self._pixel_tile_size()
        self._canvas.set_tile_size(tw, th)
        self._canvas.set_zoom(view.zoom)
        self._canvas.set_grid(view.show_grid)
        self._canvas.set_filled_tiles(len(tiles))
        self._canvas.set_image(image)
        self._refresh_selection(cols * rows)
        self._palette_panel.set_palette(self._doc.palette.colors)
        self._palette_panel.set_active_range(base, group)
        # A reload can recolour (or drop) the selected entry under the same index.
        self._update_color_details()
        self._sync_nav()
        self._refresh_overlay()

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
            view.offset, view.columns * view.rows, view.byte_nudge
        )
        if decompress_id is None or not window:
            self._overlay.hide_overlay()
            return
        ctx = PipelineContext()
        ctx.set(KEY_DECOMPRESS_PARTIAL, True)
        preset = self._registry.preset(self._pixel_preset_id())
        try:
            plugin = self._registry.plugin(Stage.DECOMPRESS, decompress_id)
            raw = plugin.decompress(bytes(window), ctx)
            engine = self._registry.plugin(Stage.INTERPRET_PIXEL, preset.engine_id)
            tile_bytes = engine.bytes_per_tile(preset.params)
            # Zero-pad the trailing partial tile, as the main view's
            # window slicing does, so short structures still decode.
            data = raw + bytes(-len(raw) % tile_bytes)
            tiles = engine.decode(data, preset.params, ctx)
        except Exception:  # noqa: BLE001 — any failure means "not a structure"
            self._overlay.hide_overlay()
            return
        if not tiles:
            self._overlay.hide_overlay()
            return
        cols = view.columns
        grid = compose_window(tiles, cols, 0, (len(tiles) + cols - 1) // cols)
        base = view.subpalette_row * self._index_space()
        image = render_bridge.render(grid, self._doc.palette, base)

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
        pos = self._byte_position() + 1
        found: int | None = None
        self._scanning = True
        self._scan_stop = False
        self._set_scan_ui(True)
        try:
            while pos < len(data):
                try:
                    if plugin.decompress(
                        data[pos : pos + window_len], PipelineContext()
                    ):
                        found = pos
                        break
                except Exception:  # noqa: BLE001 — not a structure; keep walking
                    pass
                pos += 1
                if pos % 64 == 0:
                    self.statusBar().showMessage(
                        f"Scanning… {self._format_offset(pos)}"
                    )
                    QApplication.processEvents()
                    if self._scan_stop:
                        break
        finally:
            self._scanning = False
            self._set_scan_ui(False)
        # Land where the scan ended — the hit, or wherever Stop/EOF left it.
        self._set_byte_position(found if found is not None else min(pos, len(data) - 1))
        if found is not None:
            self.statusBar().showMessage(
                f"Structure found at {self._format_offset(found)}."
            )
        elif self._scan_stop:
            self.statusBar().showMessage("Scan stopped.")
        else:
            self.statusBar().showMessage("Scan reached the end without a match.")

    def _set_scan_ui(self, active: bool) -> None:
        """Swap Scan⇄Stop and freeze the rest of the UI while a scan runs."""
        self._scan_button.setText("Stop" if active else "Scan")
        for widget in (
            self.menuBar(),
            self.centralWidget(),
            self._palette_dock,
            self._files_dock,
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
        # current entry keeps off (header skip is a whole-file setting).
        current = self._workspace.current
        if not active and current is not None:
            is_file = current.kind is EntryKind.FILE
            self._headered.setEnabled(is_file)
            self._header_len.setEnabled(is_file)

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

    def _report(self, exc: PipelineError) -> None:
        QMessageBox.warning(self, "Celpix — pipeline error", str(exc))
        self.statusBar().showMessage(str(exc))

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
