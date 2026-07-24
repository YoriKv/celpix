"""The palette dock: the swatch grid's surroundings, and per-mode visibility.

:class:`~celpix.ui.palette_panel.PalettePanel` is a dumb swatch grid; everything
around it is built here - the load-mode dropdown, the offset field and its step
buttons, the source-file label, the color-format row, the details readout and
the export button.

The header is **per-mode**, and :meth:`_set_palette_mode` is the single place
that converges the mode member, the dropdown and which of those widgets are
showing. What each mode wants is not re-listed here: it is asked of the mode
itself (``decodes_raw_bytes``, ``has_external_file``, ``is_exportable``).
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import (
    QAction,
    QKeySequence,
)
from PySide6.QtWidgets import (
    QDockWidget,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QStyle,
    QVBoxLayout,
    QWidget,
)

from celpix.core.errors import Stage
from celpix.project.workspace import (
    PaletteMode,
)
from celpix.ui.palette_panel import PalettePanel
from celpix.ui.widgets import (
    CommittingLineEdit,
    CompactComboBox,
    select_combo_data,
)

# Floor for the header's mode-specific slot (file name, or offset field plus
# step arrows). Chosen so the dock's minimum width matches what a file name
# alone used to ask for - narrow enough that no mode can ratchet the dock
# wider, wide enough that the offset field is still usable at that width.
_SOURCE_SLOT_MIN_WIDTH = 58


class PaletteDockMixin:
    """The palette dock's header, format row and readout - and per-mode visibility.

    A slice of :class:`~celpix.ui.main_window.window.MainWindow`, not a
    standalone object: it reads and writes the window's own widgets and its
    single live ``_doc``. See the module docstring for what it owns, and the
    package docstring for why these are mixins.
    """

    def _build_palette_dock(self) -> None:
        """The palette dock: a load-mode header and a per-mode format row over
        the swatch grid, in the left column under Files.

        Built after _build_navbar, whose address-format machinery the offset
        field here shares (_parse_address / _palette_offset_text), after
        _build_files_dock, whose dock it splits, and before _build_toolbar - the
        palette format combo is created here, not on the codecs toolbar.
        """
        self._palette_panel = PalettePanel()
        # A scroll area guards against a pathologically large opened palette;
        # a typical 256-color grid is small and never scrolls.
        holder = QScrollArea()
        holder.setWidget(self._palette_panel)
        holder.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)

        # Same compact treatment as the pixel dropdown, at half its natural
        # width - the four mode labels don't need the full width the longest
        # ("Emulator State") reserves.
        self._palette_mode_combo = CompactComboBox(0.7)
        self._palette_mode_combo.setToolTip("Where the palette comes from")
        for label, mode in (
            ("Default", PaletteMode.DEFAULT),
            ("File", PaletteMode.FILE),
            ("Offset", PaletteMode.OFFSET),
            ("Emulator State", PaletteMode.EMULATOR),
            ("Custom", PaletteMode.CUSTOM),
        ):
            self._palette_mode_combo.addItem(label, mode)
        # Connected after population so the addItem calls don't fire it. Qt only
        # emits on index *change*, so re-selecting the current "File" entry
        # doesn't re-prompt - re-opening a different palette file goes through
        # the Palette menu.
        self._palette_mode_combo.currentIndexChanged.connect(
            self._on_palette_mode_change
        )

        # Same parse + rendering conventions as the navbar offset box (shared
        # address-format dropdown and bank settings). The header is per-mode:
        # this field shows only in Offset mode, the file label only in the
        # file-backed modes - both managed by _set_palette_mode.
        self._palette_offset_edit = CommittingLineEdit(
            self._parse_address, self._palette_offset_text
        )
        self._palette_offset_edit.setFixedWidth(104)
        self._palette_offset_edit.setToolTip(
            "Palette offset in the pixel file - Enter to load"
        )
        self._palette_offset_edit.hide()
        self._palette_offset_edit.committed.connect(self._on_palette_offset_committed)

        # Step the palette offset one tile at a time (the tile-molester idiom):
        # nudging the source window by a whole tile is how you hunt for a
        # palette that sits a few tiles off the graphics. Shown with the offset
        # field, in Offset mode only. The same style standard-icon arrows the
        # navbar's tile steps use - triangle glyphs render inconsistently (see
        # _build_navbar).
        sp = QStyle.StandardPixmap
        self._palette_offset_prev = QPushButton()
        self._palette_offset_prev.setIcon(self.style().standardIcon(sp.SP_ArrowLeft))
        self._palette_offset_prev.setToolTip("Palette offset back one tile")
        self._palette_offset_prev.setFixedWidth(28)
        self._palette_offset_prev.clicked.connect(lambda: self._step_palette_offset(-1))
        self._palette_offset_prev.hide()
        self._palette_offset_next = QPushButton()
        self._palette_offset_next.setIcon(self.style().standardIcon(sp.SP_ArrowRight))
        self._palette_offset_next.setToolTip("Palette offset forward one tile")
        self._palette_offset_next.setFixedWidth(28)
        self._palette_offset_next.clicked.connect(lambda: self._step_palette_offset(1))
        self._palette_offset_next.hide()

        # Which external file the palette comes from (File/Emulator modes).
        self._palette_file_label = QLabel()
        self._palette_file_label.hide()

        # The palette color format, below the mode it qualifies. Shown for every
        # real palette - File/Offset/Emulator/Custom, not the generated Default;
        # live where raw bytes are decoded (File/Offset/Emulator, so the picker
        # reinterprets them) and read-only for Custom, which stores its own ARGB
        # and only *carries* a format (visibility and enabled state managed by
        # _set_palette_mode). Hidden widgets still hold state - the session
        # capture/restore and undo paths read and set them as before.
        self._palette_preset = self._preset_combo(Stage.INTERPRET_PALETTE, "bgr555")
        self._palette_preset.setToolTip("How palette bytes decode to colors")
        self._palette_preset.currentIndexChanged.connect(self._reload_palette)
        self._palette_preset.hide()
        self._palette_format_label = QLabel("Format:")
        self._palette_format_label.setToolTip(self._palette_preset.toolTip())
        self._palette_format_label.setBuddy(self._palette_preset)
        self._palette_format_label.hide()

        # The per-mode widgets share one slot whose *minimum* width is fixed and
        # mode-independent. Without it the header's minimum jumps by ~110px when
        # Offset mode swaps a file name for the offset field and its two
        # arrows - and QMainWindow, which must honour a dock's minimum, widens
        # the dock to suit and never gives the width back. The slot's size hint
        # is still the natural one, so at any comfortable dock width nothing is
        # squeezed; drag the dock narrower than the slot and its contents clip
        # (children are clipped to the slot) rather than pushing back.
        source_slot = QWidget()
        source_row = QHBoxLayout(source_slot)
        source_row.setContentsMargins(0, 0, 0, 0)
        source_row.addWidget(self._palette_file_label)
        source_row.addWidget(self._palette_offset_edit)
        source_row.addWidget(self._palette_offset_prev)
        source_row.addWidget(self._palette_offset_next)
        source_row.addStretch(1)
        source_slot.setMinimumWidth(_SOURCE_SLOT_MIN_WIDTH)

        header = QHBoxLayout()
        header.setContentsMargins(4, 4, 4, 2)
        header.addWidget(self._palette_mode_combo)
        header.addWidget(source_slot)
        header.addStretch(1)

        # Custom only: snap the stored ARGB colors onto the values the selected
        # format can hold. A Custom palette keeps colors verbatim, so this is the
        # explicit, one-shot conversion - the dropdown alone only relabels.
        self._quantize_palette_action = QPushButton("Quantize")
        self._quantize_palette_action.setToolTip(
            "Snap colors to the nearest the format can store"
        )
        self._quantize_palette_action.clicked.connect(self._quantize_custom_palette)
        self._quantize_palette_action.hide()

        format_row = QHBoxLayout()
        format_row.setContentsMargins(4, 0, 4, 2)
        format_row.addWidget(self._palette_format_label)
        format_row.addWidget(self._palette_preset)
        format_row.addStretch(1)

        # Its own row under Format: it acts *on* the picked format rather than
        # being part of choosing it, and only Custom shows it at all.
        quantize_row = QHBoxLayout()
        quantize_row.setContentsMargins(4, 0, 4, 2)
        quantize_row.addWidget(self._quantize_palette_action)
        quantize_row.addStretch(1)

        # Details readout for the panel's selected color. Selectable text so
        # values can be copied out.
        self._color_details = QLabel("No color selected")
        self._color_details.setContentsMargins(4, 0, 4, 4)
        self._color_details.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self._palette_panel.color_selected.connect(self._on_palette_color_selected)
        # Double-click a swatch to edit it; the eyedropper can sample from the
        # grid as well as the canvas.
        self._palette_panel.edit_requested.connect(self._open_color_editor)
        self._palette_panel.color_picked.connect(self._on_color_picked)
        # Copy/paste the selected color (Ctrl+C/V) or the active subpalette
        # (Ctrl+Shift+C/V) while the grid has focus, or from its right-click menu.
        self._palette_panel.copy_requested.connect(self._copy_palette_color)
        self._palette_panel.paste_requested.connect(self._paste_palette_color)
        self._palette_panel.copy_subpalette_requested.connect(self._copy_subpalette)
        self._palette_panel.paste_subpalette_requested.connect(self._paste_subpalette)
        self._palette_panel.customContextMenuRequested.connect(self._show_palette_menu)

        # Get the colors on screen out as a file of their own. Armed only in the
        # modes where they exist nowhere else as a palette - see
        # _sync_palette_export_action.
        self._export_palette_action = QPushButton("Export to File…")
        self._export_palette_action.setToolTip("Write these colors to a .pal file")
        self._export_palette_action.clicked.connect(self._export_palette_file)
        export_row = QHBoxLayout()
        export_row.setContentsMargins(4, 0, 4, 4)
        export_row.addWidget(self._export_palette_action)
        export_row.addStretch(1)

        container = QWidget()
        column = QVBoxLayout(container)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(2)
        column.addLayout(header)
        column.addLayout(format_row)
        column.addLayout(quantize_row)
        column.addWidget(holder, 1)
        column.addWidget(self._color_details)
        column.addLayout(export_row)

        self._palette_dock = QDockWidget("Palette", self)
        self._palette_dock.setObjectName("palette-dock")  # keeps saveState usable
        self._palette_dock.setWidget(container)
        # Stacked under the Files dock in the same left column (built first, so it
        # is there to split), and given an even share of that column's height -
        # both are browsing panes, and neither earns the space by default.
        self.splitDockWidget(
            self._files_dock, self._palette_dock, Qt.Orientation.Vertical
        )
        self.resizeDocks(
            [self._files_dock, self._palette_dock], [1, 1], Qt.Orientation.Vertical
        )
        # Sharing a column means one width serves both, and left alone Qt settles
        # on the palette's *minimum* - which is the width its header can't go
        # below, not one it reads well at. Ask for its natural width instead; the
        # canvas still nets space, since the palette no longer holds a column of
        # its own on the right.
        self.resizeDocks(
            [self._palette_dock],
            [self._palette_dock.sizeHint().width()],
            Qt.Orientation.Horizontal,
        )

    def _build_palette_menu(self) -> None:
        """Palette ▸ everything palette-flavoured: palette-from-selection,
        panel."""
        menu = self.menuBar().addMenu("Palette")

        self._palette_from_selection_action = QAction("Palette from Selection", self)
        self._palette_from_selection_action.setToolTip(
            "Read a palette from the selected tile's offset"
        )
        self._palette_from_selection_action.triggered.connect(
            self._load_palette_from_selection
        )
        # Needs a doc + a selection.
        self._palette_from_selection_action.setEnabled(False)
        # Display-only shortcut, like the View menu's: the bare key is routed by
        # the app-wide event filter (_handle_nav_key), which yields to focused
        # text inputs - a live shortcut here would steal "p" from them.
        self._palette_from_selection_action.setShortcut(QKeySequence("P"))
        self._palette_from_selection_action.setShortcutContext(
            Qt.ShortcutContext.WidgetShortcut
        )
        menu.addAction(self._palette_from_selection_action)

    def _set_palette_mode(self, mode: PaletteMode) -> None:
        """Converge mode member, dropdown, and the per-mode header widgets
        (the success path).

        The dock shows only what the mode uses: the offset field in Offset
        mode, the source file's name in the file-backed modes, and the format
        combo for every real palette (all but the generated Default). The combo
        is live for every real mode: File/Offset/Emulator re-decode their raw
        bytes under it, and Custom - which has no bytes to reinterpret - rebases
        its stored ARGB colors into the picked format instead. Signals are
        blocked while syncing the combo so programmatic updates never re-enter
        _on_palette_mode_change.
        """
        self._palette_mode = mode
        select_combo_data(self._palette_mode_combo, mode)
        is_offset = mode is PaletteMode.OFFSET
        self._palette_offset_edit.setVisible(is_offset)
        self._palette_offset_prev.setVisible(is_offset)
        self._palette_offset_next.setVisible(is_offset)
        # Mid-commit the box refreshes itself afterwards; don't fight it.
        if not self._palette_offset_edit.hasFocus():
            self._palette_offset_edit.refresh()
        self._palette_format_label.setVisible(mode.is_real)
        self._palette_preset.setVisible(mode.is_real)
        # Live for every real palette: the raw-bytes modes re-decode under it,
        # Custom only relabels (its colors are stored verbatim). Only the
        # generated Default has no format to pick, and it hides the row outright.
        self._palette_format_label.setEnabled(mode.is_real)
        self._palette_preset.setEnabled(mode.is_real)
        # Quantize applies only to Custom's verbatim colors; the raw-bytes modes
        # already hold values their format can store, so it would be a no-op.
        self._quantize_palette_action.setVisible(mode is PaletteMode.CUSTOM)
        self._refresh_palette_file_label()
        self._sync_palette_export_action()

    def _sync_palette_export_action(self) -> None:
        """Arm the dock's Export to File button iff there is a palette to write."""
        self._export_palette_action.setEnabled(
            self._doc is not None and self._palette_mode.is_exportable
        )

    def _refresh_palette_file_label(self) -> None:
        """Point the dock's file label at the palette's external source.

        Only the file/emulator modes have one. A degraded source (mode kept,
        file gone - see ``Entry.missing_palette``) still names its intended
        file, marked missing; otherwise the path is read off the live config.
        """
        path, missing = None, False
        if self._doc is not None and self._palette_mode.has_external_file:
            path = self._doc.palette_config.source.path or None
            entry = self._workspace.current
            if path is None and entry is not None and entry.missing_palette:
                path, missing = entry.missing_palette.path, True
        if path is None:
            self._palette_file_label.hide()
            return
        name = Path(path).name + (" (missing)" if missing else "")
        # Elide long names by hand (QLabel has no elide mode) - the full path
        # lives in the tooltip, and the dock must not widen to fit the text.
        metrics = self._palette_file_label.fontMetrics()
        self._palette_file_label.setText(
            metrics.elidedText(name, Qt.TextElideMode.ElideMiddle, 150)
        )
        self._palette_file_label.setToolTip(path)
        self._palette_file_label.show()
