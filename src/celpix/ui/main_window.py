"""The application main window: open pixel/palette data, view it, save it back.

Deliberately basic — a File menu, a control strip (preset, palette format, columns,
zoom, subpalette row, grid) and a scrollable :class:`~celpix.ui.canvas.Canvas`. It
drives the Qt-free pipeline through the plugin registry and never interprets bytes
itself; all decode/encode goes through ``pipeline``. This is the shell the View &
Edit tools attach to next (see ``docs/design/mvp-plan.md``).
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QEvent, Qt, QUrl
from PySide6.QtGui import (
    QAction,
    QDesktopServices,
    QDragEnterEvent,
    QDropEvent,
    QKeySequence,
    QPalette,
)
from PySide6.QtWidgets import (
    QAbstractSlider,
    QAbstractSpinBox,
    QApplication,
    QCheckBox,
    QComboBox,
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
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from celpix.core.arrangement import compose_window
from celpix.core.document import Document, ViewOptions
from celpix.core.errors import PipelineError, Stage
from celpix.core.palette import Palette
from celpix.pipeline import pipeline
from celpix.pipeline.pathway import PathwayConfig
from celpix.plugins.base import FileRef
from celpix.plugins.discovery import PluginLoadIssue
from celpix.plugins.registry import Registry, default_registry
from celpix.ui import render_bridge
from celpix.ui.canvas import Canvas
from celpix.ui.widgets import CommittingLineEdit

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
        self._doc: Document | None = None
        # True once a palette file is loaded; until then we render through a
        # generated grayscale ramp and never write the palette back.
        self._has_palette_file = False
        # Top-left tile index of the view window. The scroll area no longer scrolls
        # the whole file; this offset does, and only the window is composed/rendered.
        self._offset = 0

        self._canvas = Canvas()
        # ClickFocus so clicking the view takes focus off any dropdown/spin box (which
        # would otherwise keep the arrow keys), letting navigation resume. Navigation
        # itself is window-wide via eventFilter, not tied to canvas focus.
        self._canvas.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        scroll = QScrollArea()
        scroll.setWidget(self._canvas)
        # Pin the (small) window to the top-left; the scroll area only scrolls now
        # when zoom makes the window itself larger than the viewport.
        scroll.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)

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

        self._build_menus()
        self._build_toolbar()
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
    def _build_menus(self) -> None:
        file_menu = self.menuBar().addMenu("&File")

        open_pixel = QAction("Open &pixel data…", self)
        open_pixel.setShortcut(QKeySequence.StandardKey.Open)
        open_pixel.triggered.connect(self._open_pixel)
        file_menu.addAction(open_pixel)

        open_palette = QAction("Open pa&lette…", self)
        open_palette.triggered.connect(self._open_palette)
        file_menu.addAction(open_palette)

        file_menu.addSeparator()

        self._save_action = QAction("&Save", self)
        self._save_action.setShortcut(QKeySequence.StandardKey.Save)
        self._save_action.triggered.connect(self._save)
        self._save_action.setEnabled(False)
        file_menu.addAction(self._save_action)

        file_menu.addSeparator()

        open_plugins = QAction("Open pl&ugins folder…", self)
        open_plugins.setToolTip("Drop preset .toml or code .py plugins here")
        open_plugins.triggered.connect(self._open_plugins_folder)
        open_plugins.setEnabled(self._plugin_dir is not None)
        file_menu.addAction(open_plugins)

        refresh = QAction("&Refresh plugins", self)
        refresh.setShortcut(QKeySequence.StandardKey.Refresh)  # F5
        refresh.setToolTip(
            "Reload plugins from the folder and re-run on the open file (developer aid)"
        )
        refresh.triggered.connect(self._refresh_plugins)
        refresh.setEnabled(self._reload_plugins is not None)
        file_menu.addAction(refresh)

        file_menu.addSeparator()

        quit_action = QAction("&Quit", self)
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

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
        bar = QToolBar("View")
        self.addToolBar(bar)

        self._pixel_preset = self._preset_combo(Stage.INTERPRET_PIXEL, "snes-4bpp")
        self._pixel_preset.currentIndexChanged.connect(self._reload_pixel)
        bar.addWidget(QLabel(" Pixel: "))
        bar.addWidget(self._pixel_preset)

        self._palette_preset = self._preset_combo(Stage.INTERPRET_PALETTE, "bgr555")
        self._palette_preset.currentIndexChanged.connect(self._reload_palette)
        bar.addWidget(QLabel(" Palette: "))
        bar.addWidget(self._palette_preset)

        self._columns = self._spin(1, 64, 16, self._on_view_change)
        bar.addWidget(QLabel(" Cols: "))
        bar.addWidget(self._columns)

        # How many tile-rows the window shows — the "render N rows" view setting.
        self._rows = self._spin(1, 256, 16, self._on_view_change)
        bar.addWidget(QLabel(" Rows: "))
        bar.addWidget(self._rows)

        self._zoom = self._spin(1, 16, 4, self._on_view_change)
        bar.addWidget(QLabel(" Zoom: "))
        bar.addWidget(self._zoom)

        self._subpalette = self._spin(0, 63, 0, self._on_view_change)
        bar.addWidget(QLabel(" Pal row: "))
        bar.addWidget(self._subpalette)

        self._grid = QCheckBox("Grid")
        self._grid.toggled.connect(self._on_view_change)
        bar.addWidget(self._grid)

    def _preset_combo(self, stage: Stage, default_suffix: str) -> QComboBox:
        combo = QComboBox()
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
        """The strip under the canvas: step buttons + the current hex position.

        Up/Down step one tile-row (``columns`` tiles); Left/Right step one tile;
        Home/End jump to the file's first/last page — the same actions the arrow
        keys drive (:meth:`_build_nav_keys`).
        """
        bar = QWidget()
        row = QHBoxLayout(bar)
        row.setContentsMargins(6, 2, 6, 2)

        # Editable hex file offset — leftmost. A CommittingLineEdit commits on Enter /
        # focus-out (not per keystroke) and always re-renders on commit, so an invalid
        # entry reverts and a valid one shows its tile-snapped 0x form; it keeps its
        # own arrow/Home keys, so the navigation shortcuts don't fire while focused.
        row.addWidget(QLabel("Offset "))
        self._offset_edit = CommittingLineEdit(
            self._parse_hex_offset, self._offset_text
        )
        self._offset_edit.setFixedWidth(96)
        self._offset_edit.setToolTip("File offset (hex, 0x optional) — Enter to jump")
        self._offset_edit.committed.connect(self._jump_to_offset)
        row.addWidget(self._offset_edit)
        row.addSpacing(12)

        # (label, tooltip, handler). Home/End use |◀ / ▶| — built from the arrow
        # glyphs so they render on any font (the ⏮/⏭ media glyphs often don't).
        for text, tip, handler in (
            ("|◀", "First page (Home)", self._nav_home),
            ("◀", "Back one tile (Left)", lambda: self._nav_tiles(-1)),
            ("▲", "Up one row (Up)", lambda: self._nav_rows(-1)),
            ("▼", "Down one row (Down)", lambda: self._nav_rows(1)),
            ("▶", "Forward one tile (Right)", lambda: self._nav_tiles(1)),
            ("▶|", "Last page (End)", self._nav_end),
        ):
            btn = QPushButton(text)
            btn.setToolTip(tip)
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # keep arrow keys global
            btn.setMinimumWidth(34)  # min, not fixed, so |◀ / ▶| fit
            btn.clicked.connect(handler)
            row.addWidget(btn)

        self._nav_info = QLabel()
        row.addSpacing(8)
        row.addWidget(self._nav_info)
        row.addStretch(1)
        return bar

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
    _ARROW_INPUT_TYPES = (QComboBox, QAbstractSpinBox, QLineEdit, QAbstractSlider)

    def _build_nav_keys(self) -> None:
        """Map navigation keys to handlers, applied window-wide by :meth:`eventFilter`.

        Arrow / Home / End / PageUp-Down drive the view window (scroll is locked to the
        tile offset; PageUp/Down step a whole window of rows). Shift+arrows resize the
        window instead of moving it (↕ rows, ↔ cols). Keyed by ``(key, shift_held)``.
        """
        self._nav_keys = {
            (Qt.Key.Key_Up, False): lambda: self._nav_rows(-1),
            (Qt.Key.Key_Down, False): lambda: self._nav_rows(1),
            (Qt.Key.Key_Left, False): lambda: self._nav_tiles(-1),
            (Qt.Key.Key_Right, False): lambda: self._nav_tiles(1),
            (Qt.Key.Key_PageUp, False): lambda: self._nav_rows(-self._rows.value()),
            (Qt.Key.Key_PageDown, False): lambda: self._nav_rows(self._rows.value()),
            (Qt.Key.Key_Home, False): self._nav_home,
            (Qt.Key.Key_End, False): self._nav_end,
            (Qt.Key.Key_Up, True): lambda: self._adjust_spin(self._rows, -1),
            (Qt.Key.Key_Down, True): lambda: self._adjust_spin(self._rows, 1),
            (Qt.Key.Key_Left, True): lambda: self._adjust_spin(self._columns, -1),
            (Qt.Key.Key_Right, True): lambda: self._adjust_spin(self._columns, 1),
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

        Yields (returns False) when an arrow-consuming input has focus, or the event
        carries Ctrl/Alt/Meta, so only bare / Shift-ed navigation keys ever act.
        """
        if isinstance(QApplication.focusWidget(), self._ARROW_INPUT_TYPES):
            return False
        mods = event.modifiers()
        blocked = (
            Qt.KeyboardModifier.ControlModifier
            | Qt.KeyboardModifier.AltModifier
            | Qt.KeyboardModifier.MetaModifier
        )
        if mods & blocked:
            return False
        shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)
        handler = self._nav_keys.get((event.key(), shift))
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

    def _nav_home(self) -> None:
        self._set_offset(0)

    def _nav_end(self) -> None:
        if self._doc is not None:
            self._set_offset(self._doc.tile_count)  # clamped to the last page

    def _set_offset(self, offset: int) -> None:
        """Clamp ``offset`` to a valid page and, if it moved, re-render."""
        if self._doc is None:
            return
        offset = self._doc.clamp_offset(
            offset, self._columns.value(), self._rows.value()
        )
        if offset == self._offset:
            # No move (e.g. a scrollbar drag past the end clamped to here) — still
            # snap the scrollbar/box back onto the clamped position.
            self._sync_nav()
            return
        self._offset = offset
        self._refresh_view()

    @staticmethod
    def _parse_hex_offset(text: str) -> int | None:
        """A hex file offset, accepting ``0x``/``$`` prefixes or a bare number.

        Returns None for anything that isn't valid hex (including empty), so the
        caller can leave the current offset untouched.
        """
        t = text.strip().lower().removeprefix("0x").removeprefix("$")
        if not t:
            return None
        try:
            return int(t, 16)
        except ValueError:
            return None

    def _offset_text(self) -> str:
        """The current offset as a normalised ``0x…`` hex byte-offset for the box.

        Also the offset box's ``current_text`` provider — it re-renders from this on
        every commit, so it must be safe to call with no document loaded.
        """
        if self._doc is None:
            return ""
        byte_off = (
            self._doc.pixel_config.source.offset
            + self._offset * self._doc.bytes_per_tile
        )
        return f"0x{byte_off:06X}"

    def _jump_to_offset(self, byte_off: int) -> None:
        """Jump to a file byte offset (tile-snapped) — the offset box's commit handler.

        The box re-renders itself from :meth:`_offset_text` after this, so there's no
        text handling to do here; an out-of-range value is clamped by _set_offset.
        """
        if self._doc is None:
            return
        base = self._doc.pixel_config.source.offset
        self._set_offset(max(0, byte_off - base) // self._doc.bytes_per_tile)

    def _sync_nav(self) -> None:
        """Mirror the current offset into the hex box, the info label, and the bar."""
        has_doc = self._doc is not None
        self._offset_edit.setEnabled(has_doc)
        self._offset_bar.setEnabled(has_doc)
        if not has_doc:
            self._offset_edit.clear()
            self._nav_info.setText("No file open")
            return

        cols, rows = self._columns.value(), self._rows.value()
        # Don't overwrite what the user is mid-way through typing; a commit re-renders
        # the box itself (CommittingLineEdit.commit), so this guard is safe.
        if not self._offset_edit.hasFocus():
            self._offset_edit.refresh()
        self._nav_info.setText(f"tile {self._offset:,} / {self._doc.tile_count:,}")

        # Scrollbar spans the whole file: value = offset, page = one window of tiles,
        # so the handle size reflects how much of the file is on screen.
        page = max(1, cols) * max(1, rows)
        max_off = max(0, self._doc.tile_count - page)
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
        return int(self._registry.preset(self._pixel_preset_id()).params["bpp"])

    def _pixel_tile_size(self) -> tuple[int, int]:
        # The atomic tile size is the codec's (recorded on the document at load) — not
        # a preset field (geometry is the engine's fixed unit; display grouping into
        # larger tiles is a separate view option, not yet implemented).
        if self._doc is not None:
            return self._doc.tile_width, self._doc.tile_height
        return 8, 8

    # -- drag & drop -------------------------------------------------------
    @staticmethod
    def _dropped_path(event: QDragEnterEvent | QDropEvent) -> str | None:
        """The first local-file path in a drag payload, or None if it has none."""
        mime = event.mimeData()
        if not mime.hasUrls():
            return None
        for url in mime.urls():
            path = url.toLocalFile()
            if path:
                return path
        return None

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # Qt override
        # Only offer to accept when the drag carries a local file; ignore otherwise
        # so the user gets accurate feedback (no drop cursor for non-file drags).
        if self._dropped_path(event) is not None:
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:  # Qt override
        path = self._dropped_path(event)
        if path is None:
            return
        event.acceptProposedAction()
        self._load_pixel(path)  # a dropped file opens as pixel data

    # -- actions -----------------------------------------------------------
    def _open_pixel(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Open pixel data")
        if path:
            self._load_pixel(path)

    def _load_pixel(self, path: str) -> None:
        """Decode ``path`` as pixel data under the current preset and show it.

        The shared entry point for both File ▸ Open and drag-and-drop (drop
        handlers below), so a dropped file behaves exactly like an opened one.
        """
        cfg = PathwayConfig(
            source=FileRef(path), interpret_preset_id=self._pixel_preset_id()
        )
        try:
            px = pipeline.load_pixel_data(cfg, self._registry)
        except PipelineError as exc:
            self._report(exc)
            return

        if self._doc is None:
            self._doc = Document(
                pixel_data=px.data,
                bytes_per_tile=px.bytes_per_tile,
                tile_width=px.tile_width,
                tile_height=px.tile_height,
                palette=self._grayscale(),
                pixel_config=cfg,
                # Placeholder until a palette file is opened; not written back.
                palette_config=PathwayConfig(
                    source=FileRef(""),
                    interpret_preset_id=self._palette_preset_id(),
                    write_enabled=False,
                ),
            )
        else:
            self._store_pixel_data(px, cfg)
        self._doc.pixel_ctx = px.ctx
        self._offset = 0  # a freshly opened file starts at the top
        self._save_action.setEnabled(True)
        self.setWindowTitle(f"Celpix — {path}")
        self._refresh_view()
        self._canvas.setFocus()  # arm arrow-key navigation on the freshly loaded view
        self.statusBar().showMessage(f"Loaded {self._doc.tile_count} tiles from {path}")

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
            self._doc.palette = self._grayscale()

    def _open_palette(self) -> None:
        if self._doc is None:
            self.statusBar().showMessage("Open pixel data first.")
            return
        path, _ = QFileDialog.getOpenFileName(self, "Open palette")
        if not path:
            return
        cfg = PathwayConfig(
            source=FileRef(path), interpret_preset_id=self._palette_preset_id()
        )
        try:
            colors, ctx = pipeline.load_palette(cfg, self._registry)
        except PipelineError as exc:
            self._report(exc)
            return
        self._doc.palette = colors
        self._doc.palette_config = cfg
        self._doc.palette_ctx = ctx
        self._has_palette_file = True
        self._refresh_view()
        self.statusBar().showMessage(f"Loaded {len(colors)} colours from {path}")

    def _reload_pixel(self) -> None:
        """Re-load the pixel bytes + geometry under a newly chosen preset.

        The view offset is a tile index, so it maps to a different *byte* position
        under a new bytes-per-tile. Preserve the file position across the codec
        change by converting through bytes and snapping down to the new codec's tile
        boundary (the tile containing that byte).
        """
        if self._doc is None:
            return
        byte_offset = self._offset * self._doc.bytes_per_tile
        cfg = PathwayConfig(
            source=self._doc.pixel_config.source,
            interpret_preset_id=self._pixel_preset_id(),
        )
        try:
            px = pipeline.load_pixel_data(cfg, self._registry)
        except PipelineError as exc:
            self._report(exc)
            return
        self._store_pixel_data(px, cfg)
        self._offset = byte_offset // px.bytes_per_tile  # _refresh_view clamps it
        self._refresh_view()

    def _reload_palette(self) -> None:
        """Re-decode the palette under a newly chosen colour format."""
        if self._doc is None or not self._has_palette_file:
            return
        cfg = PathwayConfig(
            source=self._doc.palette_config.source,
            interpret_preset_id=self._palette_preset_id(),
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

    def _save(self) -> None:
        if self._doc is None:
            return
        try:
            pipeline.save(self._doc, self._registry)
        except PipelineError as exc:
            self._report(exc)
            return
        wrote = "pixel + palette" if self._has_palette_file else "pixel"
        self.statusBar().showMessage(f"Saved ({wrote}).")

    # -- view --------------------------------------------------------------
    def _on_view_change(self, *_args) -> None:
        if self._doc is not None:
            self._refresh_view()

    def _refresh_view(self) -> None:
        assert self._doc is not None
        # Re-clamp first: a smaller file, or a bigger window (cols/rows), can push
        # the previous offset past the last page.
        self._offset = self._doc.clamp_offset(
            self._offset, self._columns.value(), self._rows.value()
        )
        cols, rows = self._columns.value(), self._rows.value()
        self._doc.view = ViewOptions(
            columns=cols,
            rows=rows,
            zoom=self._zoom.value(),
            show_grid=self._grid.isChecked(),
            subpalette_row=self._subpalette.value(),
            offset=self._offset,
        )
        # Deferred decode: only the visible window of tiles is decoded, then laid out.
        tiles = pipeline.decode_window(
            self._doc, self._registry, self._offset, cols * rows
        )
        image_grid = compose_window(tiles, cols, 0, rows)
        base = self._doc.view.subpalette_row * (1 << self._pixel_bpp())
        image = render_bridge.render(image_grid, self._doc.palette, base)
        tw, th = self._pixel_tile_size()
        self._canvas.set_tile_size(tw, th)
        self._canvas.set_zoom(self._doc.view.zoom)
        self._canvas.set_grid(self._doc.view.show_grid)
        self._canvas.set_image(image)
        self._sync_nav()

    def _grayscale(self) -> Palette:
        return Palette.grayscale(1 << self._pixel_bpp())

    def _report(self, exc: PipelineError) -> None:
        QMessageBox.warning(self, "Celpix — pipeline error", str(exc))
        self.statusBar().showMessage(str(exc))
