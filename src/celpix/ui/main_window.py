"""The application main window: open pixel/palette data, view it, save it back.

Deliberately basic — a File menu, a control strip (preset, palette format, columns,
zoom, subpalette row, grid) and a scrollable :class:`~celpix.ui.canvas.Canvas`. It
drives the Qt-free pipeline through the plugin registry and never interprets bytes
itself; all decode/encode goes through ``pipeline``. This is the shell the View &
Edit tools attach to next (see ``docs/design/mvp-plan.md``).
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QUrl
from PySide6.QtGui import QAction, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QScrollArea,
    QSpinBox,
    QToolBar,
    QWidget,
)

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

        self._canvas = Canvas()
        scroll = QScrollArea()
        scroll.setWidget(self._canvas)
        scroll.setAlignment(scroll.alignment())
        self.setCentralWidget(scroll)

        self._build_menus()
        self._build_toolbar()
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
        spin.valueChanged.connect(on_change)
        return spin

    # -- current selections ------------------------------------------------
    def _pixel_preset_id(self) -> str:
        return self._pixel_preset.currentData()

    def _palette_preset_id(self) -> str:
        return self._palette_preset.currentData()

    def _pixel_bpp(self) -> int:
        return int(self._registry.preset(self._pixel_preset_id()).params["bpp"])

    def _pixel_tile_size(self) -> tuple[int, int]:
        # The atomic tile size is the codec's, read off the decoded tiles — not a
        # preset field (geometry is the engine's fixed unit; display grouping into
        # larger tiles is a separate view option, not yet implemented).
        if self._doc is not None and self._doc.pixel_tiles:
            tile = self._doc.pixel_tiles[0]
            return tile.width, tile.height
        return 8, 8

    # -- actions -----------------------------------------------------------
    def _open_pixel(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Open pixel data")
        if not path:
            return
        cfg = PathwayConfig(
            source=FileRef(path), interpret_preset_id=self._pixel_preset_id()
        )
        try:
            tiles, ctx = pipeline.load_pixel(cfg, self._registry)
        except PipelineError as exc:
            self._report(exc)
            return

        if self._doc is None:
            self._doc = Document(
                pixel_tiles=tiles,
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
            self._doc.pixel_tiles = tiles
            self._doc.pixel_config = cfg
            self._doc.pixel_ctx = ctx
            if not self._has_palette_file:
                self._doc.palette = self._grayscale()
        self._doc.pixel_ctx = ctx
        self._save_action.setEnabled(True)
        self.setWindowTitle(f"Celpix — {path}")
        self._refresh_view()
        self.statusBar().showMessage(f"Loaded {len(tiles)} tiles from {path}")

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
        """Re-decode the pixel data under a newly chosen preset."""
        if self._doc is None:
            return
        cfg = PathwayConfig(
            source=self._doc.pixel_config.source,
            interpret_preset_id=self._pixel_preset_id(),
        )
        try:
            tiles, ctx = pipeline.load_pixel(cfg, self._registry)
        except PipelineError as exc:
            self._report(exc)
            return
        self._doc.pixel_tiles = tiles
        self._doc.pixel_config = cfg
        self._doc.pixel_ctx = ctx
        if not self._has_palette_file:
            self._doc.palette = self._grayscale()
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
        self._doc.view = ViewOptions(
            columns=self._columns.value(),
            zoom=self._zoom.value(),
            show_grid=self._grid.isChecked(),
            subpalette_row=self._subpalette.value(),
        )
        image_grid = self._doc.compose_image()
        base = self._doc.view.subpalette_row * (1 << self._pixel_bpp())
        image = render_bridge.render(image_grid, self._doc.palette, base)
        tw, th = self._pixel_tile_size()
        self._canvas.set_tile_size(tw, th)
        self._canvas.set_zoom(self._doc.view.zoom)
        self._canvas.set_grid(self._doc.view.show_grid)
        self._canvas.set_image(image)

    def _grayscale(self) -> Palette:
        return Palette.grayscale(1 << self._pixel_bpp())

    def _report(self, exc: PipelineError) -> None:
        QMessageBox.warning(self, "Celpix — pipeline error", str(exc))
        self.statusBar().showMessage(str(exc))
