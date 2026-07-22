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
    QColor,
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
from celpix.core.errors import PipelineError, Stage
from celpix.core.palette import Palette
from celpix.pipeline import pipeline
from celpix.pipeline.pathway import PathwayConfig
from celpix.plugins.base import FileRef
from celpix.plugins.builtins.lz16 import KEY_LZ16_ROWS
from celpix.plugins.discovery import PluginLoadIssue
from celpix.plugins.registry import Registry, default_registry
from celpix.ui import render_bridge
from celpix.ui.canvas import Canvas
from celpix.ui.decompress_overlay import DecompressOverlay
from celpix.ui.palette_panel import PalettePanel
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
        self._doc: Document | None = None
        # Where the palette comes from: "custom" (the generated default),
        # "file" (Open palette…), or "offset" (read from the pixel file). The
        # dock's mode dropdown is a view of this member.
        self._palette_mode = "custom"
        # Top-left tile index of the view window. The scroll area no longer scrolls
        # the whole file; this offset does, and only the window is composed/rendered.
        self._offset = 0
        # Sub-tile byte shift of the whole tile grid (0 <= nudge < bytes_per_tile),
        # for aligning graphics that don't start on a tile boundary. Byte steps
        # (+B/−B) move it; tile/row/page steps leave it alone.
        self._nudge = 0
        # The clicked tile, as an absolute tile index (survives scrolling; the
        # canvas only paints the highlight while it is inside the window).
        self._selected_tile: int | None = None
        # Compression navigation: byte position right after the structure in
        # view (the Jump-to-Next target, None = end unknown/invalid), and the
        # scan interlock (the Scan button doubles as Stop while one runs).
        self._next_structure: int | None = None
        self._scanning = False
        self._scan_stop = False

        self._canvas = Canvas()
        self._overlay = DecompressOverlay(self)
        self._canvas.tile_clicked.connect(self._on_tile_clicked)
        # ClickFocus so clicking the view takes focus off any dropdown/spin box (which
        # would otherwise keep the arrow keys), letting navigation resume. Navigation
        # itself is window-wide via eventFilter, not tied to canvas focus.
        self._canvas.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        scroll = QScrollArea()
        scroll.setWidget(self._canvas)
        # Pin the (small) window to the top-left; the scroll area only scrolls now
        # when zoom makes the window itself larger than the viewport.
        scroll.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        # Neutral gray around/behind the rendered pixels: a fixed mid-gray (not a
        # theme colour) so it never biases how the art's colours read.
        viewport = scroll.viewport()
        viewport_palette = viewport.palette()
        viewport_palette.setColor(QPalette.ColorRole.Window, QColor(0x80, 0x80, 0x80))
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

        self._build_palette_dock()  # before _build_menus: its toggle goes in a menu
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
        return self._palette_mode != "custom"

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
        for name in ("Custom", "File", "Offset"):
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

        self._save_action = QAction("Save", self)
        self._save_action.setShortcut(QKeySequence.StandardKey.Save)
        self._save_action.triggered.connect(self._save)
        self._save_action.setEnabled(False)
        file_menu.addAction(self._save_action)

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

        self._build_palette_menu()
        self._build_view_menu()

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

        menu.addSeparator()

        toggle = self._palette_dock.toggleViewAction()
        toggle.setText("Palette Panel")
        menu.addAction(toggle)

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
        codecs.addWidget(QLabel("Pixel:"))
        codecs.addWidget(self._pixel_preset)

        self._palette_preset = self._preset_combo(Stage.INTERPRET_PALETTE, "bgr555")
        self._palette_preset.currentIndexChanged.connect(self._reload_palette)
        codecs.addWidget(QLabel("Palette:"))
        codecs.addWidget(self._palette_preset)

        # Compression preview: the main view stays raw; the chosen Decompress
        # plugin runs over the current window and shows in the floating overlay.
        self._compression = CompactComboBox(0.75)
        self._populate_compression()
        self._compression.currentIndexChanged.connect(self._on_view_change)
        codecs.addWidget(QLabel("Comp:"))
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

        Two rows — the address row (offset box, format dropdown, bank settings,
        tile indicator at the right edge) and below it the step-button row — so
        the bank settings don't push the buttons off-screen at narrow widths.

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
        self._nav_info = QLabel()  # "tile N / M" — right-aligned on the address row
        row.addWidget(self._nav_info)

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
    _ARROW_INPUT_TYPES = (
        QComboBox,
        QAbstractSpinBox,
        QLineEdit,
        QAbstractSlider,
        PalettePanel,
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

    def _tile_byte_offset(self, tile: int) -> int:
        """The file byte offset of ``tile`` on the current (nudged) grid."""
        assert self._doc is not None
        return (
            self._doc.pixel_config.source.offset
            + self._nudge
            + tile * self._doc.bytes_per_tile
        )

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
        base = self._doc.pixel_config.source.offset
        self._set_byte_position(byte_off - base)

    def _sync_nav(self) -> None:
        """Mirror the current offset into the hex box, the info label, and the bar."""
        has_doc = self._doc is not None
        self._offset_edit.setEnabled(has_doc)
        self._offset_bar.setEnabled(has_doc)
        if not has_doc:
            self._offset_edit.clear()
            self._nav_info.setText("No file open")
            self._nudge_info.clear()
            return

        cols, rows = self._columns.value(), self._rows.value()
        # Don't overwrite what the user is mid-way through typing; a commit re-renders
        # the box itself (CommittingLineEdit.commit), so this guard is safe.
        if not self._offset_edit.hasFocus():
            self._offset_edit.refresh()
        self._nav_info.setText(f"tile {self._offset:,} / {self._doc.tile_count:,}")
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
        return int(self._registry.preset(self._pixel_preset_id()).params["bpp"])

    def _index_space(self, preset_id: str | None = None) -> int:
        """The pixel format's colour count — the subpalette row size.

        Capped at 256: a direct-colour preset's bpp can be up to 32, and both
        the palette maths and the fallback palette top out at 256 entries.
        Defaults to the currently selected preset; pass ``preset_id`` to size
        another format's index space (e.g. the outgoing preset in _reload_pixel).
        A stale id (preset removed by a plugin refresh) falls back to the
        current preset rather than failing the reload.
        """
        if preset_id is not None:
            try:
                bpp = int(self._registry.preset(preset_id).params["bpp"])
                return min(256, 1 << bpp)
            except KeyError:
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
            source=FileRef(path, offset=self._header_offset()),
            interpret_preset_id=self._pixel_preset_id(),
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
                palette=self._fallback_palette(),
                pixel_config=cfg,
                palette_config=self._placeholder_palette_config(),
            )
        else:
            self._store_pixel_data(px, cfg)
        self._doc.pixel_ctx = px.ctx
        self._offset = 0  # a freshly opened file starts at the top
        self._nudge = 0  # alignment belongs to the previous file's graphics
        self._clear_selection()  # a tile index from another file means nothing here
        self._save_action.setEnabled(True)
        self.setWindowTitle(f"Celpix — {path}")
        self._refresh_view()
        self._canvas.setFocus()  # arm arrow-key navigation on the freshly loaded view
        message = f"Loaded {self._doc.tile_count} tiles from {path}"
        note = self._partial_tile_note()
        self.statusBar().showMessage(f"{message} — {note}" if note else message)

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
    def _placeholder_palette_config(self) -> PathwayConfig:
        """The no-palette-loaded config: empty source, never written back."""
        return PathwayConfig(
            source=FileRef(""),
            interpret_preset_id=self._palette_preset_id(),
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
        if mode == "custom":
            self._apply_custom_palette()
        elif mode == "file":
            if not self._open_palette():
                self._set_palette_mode(self._palette_mode)
        elif mode == "offset":
            if not self._load_palette_at_offset(self._initial_palette_offset()):
                self._set_palette_mode(self._palette_mode)

    def _apply_custom_palette(self) -> None:
        """Back to the generated default palette (mode "custom")."""
        assert self._doc is not None
        self._doc.palette = self._fallback_palette()
        self._doc.palette_config = self._placeholder_palette_config()
        self._doc.palette_ctx = PipelineContext()
        self._set_palette_mode("custom")
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
    def _on_tile_clicked(self, slot: int) -> None:
        """Select the clicked window slot (ignoring blank padding past the file)."""
        if self._doc is None:
            return
        absolute = self._offset + slot
        if absolute >= self._doc.tile_count:
            return
        self._selected_tile = absolute
        self._canvas.set_selection(slot)
        self._load_selection_action.setEnabled(True)
        self.statusBar().showMessage(
            f"Selected tile {absolute:,} at "
            f"{self._format_offset(self._tile_byte_offset(absolute))}"
        )

    def _clear_selection(self) -> None:
        self._selected_tile = None
        self._canvas.set_selection(None)
        self._load_selection_action.setEnabled(False)

    def _selection_palette_source(self, path: str, byte_off: int) -> FileRef | None:
        """A read window for up to 256 palette entries at ``byte_off``.

        Floored to whole entries — the colour codecs reject a partial trailing
        entry, so clamping at EOF alone is not enough. ``None`` when not even one
        entry fits.
        """
        bpe = pipeline.palette_entry_size(self._palette_preset_id(), self._registry)
        avail = Path(path).stat().st_size - byte_off
        entries = min(256, max(0, avail) // bpe)
        if entries == 0:
            return None
        return FileRef(path, offset=byte_off, length=entries * bpe)

    def _load_palette_at_offset(self, byte_off: int) -> bool:
        """Load palette data from the open pixel file at ``byte_off`` (Offset mode).

        The offset is in the pixel *source's* coordinate space (the same numbers
        the offset box shows — i.e. after any header skip, which is re-added for
        the file read), and the palette pathway re-reads the raw file — for
        container/compressed pixel sources the bytes at that offset differ from the
        decoded pixel data. Accepted for now; it mirrors the offset box semantics.
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
        if self._doc is None:
            return
        byte_offset = self._byte_position()
        old_group = self._index_space(self._doc.pixel_config.interpret_preset_id)
        cfg = PathwayConfig(
            # Same file, current header skip — a toggle re-lands here via
            # _on_header_change, so the offset must be re-derived, not copied.
            source=FileRef(
                self._doc.pixel_config.source.path, offset=self._header_offset()
            ),
            interpret_preset_id=self._pixel_preset_id(),
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

    def _save(self) -> None:
        if self._doc is None:
            return
        try:
            pipeline.save(self._doc, self._registry)
        except PipelineError as exc:
            self._report(exc)
            return
        # A from-selection palette is view-only — don't claim it was written.
        wrote = "pixel + palette" if self._doc.palette_config.write_enabled else "pixel"
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
            self._offset, self._columns.value(), self._rows.value(), self._nudge
        )
        cols, rows = self._columns.value(), self._rows.value()
        # Clamp the subpalette row to the rows the loaded palette actually has —
        # switching to a smaller palette (e.g. Offset's 16 rows back to Custom's
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
        image_grid = compose_window(tiles, cols, 0, rows)
        base = view.subpalette_row * group
        image = render_bridge.render(image_grid, self._doc.palette, base)
        tw, th = self._pixel_tile_size()
        self._canvas.set_tile_size(tw, th)
        self._canvas.set_zoom(view.zoom)
        self._canvas.set_grid(view.show_grid)
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
        try:
            self._present_overlay(decompress_id if active else None)
        finally:
            # Jump is armed only while a whole structure (known end) is in view.
            self._jump_next.setEnabled(self._next_structure is not None)

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
            self._view_toolbar,
            self._pixel_preset,
            self._palette_preset,
            self._compression,
            self._headered,
            self._header_len,
            self._jump_next,
        ):
            widget.setEnabled(not active)

    def _refresh_selection(self, window_tiles: int) -> None:
        """Re-derive the canvas highlight after the window moved or resized.

        Scrolling away hides the highlight but keeps the selection, so scrolling
        back restores it; a selection past the file's end (file shrank) is dropped.
        """
        assert self._doc is not None
        if self._selected_tile is not None and (
            self._selected_tile >= self._doc.tile_count
        ):
            self._clear_selection()
            return
        slot: int | None = None
        if self._selected_tile is not None:
            candidate = self._selected_tile - self._offset
            if 0 <= candidate < window_tiles:
                slot = candidate
        self._canvas.set_selection(slot)

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
