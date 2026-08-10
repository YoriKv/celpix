"""The palette dock: the swatch grid's surroundings, and per-mode visibility.

:class:`~celpix.ui.palette_panel.PalettePanel` is a dumb swatch grid; everything
around it is built here - the load-mode dropdown, the offset field and its step
buttons, the source-file label, the color-format row, the base palette row, the
details readout and the export button.

The header is **per-mode**, and :meth:`_set_palette_mode` is the single place
that converges the mode member, the dropdown and which of those widgets are
showing. What each mode wants is not re-listed here: it is asked of the mode
itself (``decodes_raw_bytes``, ``has_external_file``, ``is_exportable``).

**Base Palette Row** is the one control here that is per *entry* rather than per
palette, and it is here because it is the question of where the two meet: a file
numbers its rows from wherever its own palette sat in CGRAM, and the base is the
distance from there to the palette in this dock. Every kind of entry has named
rows to count - a map's cells, a sprite's parts, a bank's pinned rows - so it is
gated on the document rather than on the mode (:meth:`_sync_row_base`).
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
    Entry,
    PaletteMode,
)
from celpix.ui.palette_panel import PalettePanel
from celpix.ui.undo_commands import PaletteRowBaseCommand
from celpix.ui.widgets import (
    CommittingLineEdit,
    CompactComboBox,
    add_labelled,
    select_combo_data,
    signals_blocked,
    value_spin,
)

# Floor for the header's mode-specific slot (file name, or offset field plus
# step arrows). One floor for every mode, so switching modes can't ratchet the
# dock wider - narrow enough that the widest slot still fits the width a file
# name alone asks for, wide enough that the offset field stays usable there.
_SOURCE_SLOT_MIN_WIDTH = 58

# The dock's rows carry no vertical margin of their own: every gap between them,
# and the one above the first and below the last, is the column's own spacing and
# margin. One number for all of it, so a row added or moved can't land on a
# different rhythm than its neighbours.
_ROW_GAP = 4
# Horizontal inset for each row, matching the swatch grid's own left edge.
_ROW_MARGIN = 4


def _dock_row(*widgets: QWidget) -> QHBoxLayout:
    """One row of the dock's column: the shared inset, then a trailing stretch.

    Every row here is a short run of controls pinned to the left edge, and the
    inset is what lines them up with the swatch grid below (:data:`_ROW_MARGIN`).
    Passing the widgets is the whole of the common case; a row that has a caption
    to add through :func:`~celpix.ui.widgets.add_labelled` takes none and adds its
    own stretch last, since a stretch has to stay the final item.
    """
    row = QHBoxLayout()
    row.setContentsMargins(_ROW_MARGIN, 0, _ROW_MARGIN, 0)
    for widget in widgets:
        row.addWidget(widget)
    if widgets:
        row.addStretch(1)
    return row


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
        # Room for a full 16x16 grid however short the palette actually loaded
        # is - selection outlines included, since the panel draws them inside
        # the swatch. As a *minimum* it also floors the dock, so neither a drag
        # nor a restored layout can squeeze the grid to where it scrolls; only
        # a palette longer than 256 colors still falls back on the scroll area.
        full = self._palette_panel.full_grid_size()
        frame = 2 * holder.frameWidth()
        holder.setMinimumSize(full.width() + frame, full.height() + frame)

        # Same compact treatment as the pixel dropdown, and narrower than it: this
        # is a fixed list of five short mode labels rather than a registry of
        # format names, so it needs about the width of "Emulator State" and no
        # room for a plugin to widen it later.
        self._palette_mode_combo = CompactComboBox(120)
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
            "Palette offset in the pixel file\nEnter to load"
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

        header = _dock_row(self._palette_mode_combo, source_slot)

        # Custom only: snap the stored ARGB colors onto the values the selected
        # format can hold. A Custom palette keeps colors verbatim, so this is the
        # explicit, one-shot conversion - the dropdown alone only relabels.
        self._quantize_palette_action = QPushButton("Quantize")
        self._quantize_palette_action.setToolTip(
            "Snap colors to the nearest the format can store"
        )
        self._quantize_palette_action.clicked.connect(self._quantize_custom_palette)
        self._quantize_palette_action.hide()

        format_row = _dock_row()
        self._palette_format_label = add_labelled(
            format_row, "Format:", self._palette_preset, self._palette_preset.toolTip()
        )
        self._palette_format_label.hide()
        format_row.addStretch(1)

        # Where a *named* row - a cell's, a subsprite's, a pinned region's -
        # meets the palette that actually got loaded. It belongs under Format
        # because it is about these colours rather than about the picture: the
        # file numbers its rows from wherever its own palette sat in CGRAM, and
        # this is the distance between there and here. Decimal, and signed, since
        # the loaded palette is as often the upper half as the whole of it.
        self._row_base = value_spin(-255, 255, 0, self._on_row_base_change)
        row_base_row = _dock_row()
        self._row_base_label = add_labelled(
            row_base_row,
            "Base (Offset) Palette Row:",
            self._row_base,
            "Which palette row a named row 0 draws through\n"
            "A map's cells, a sprite's parts and a bank's pinned\n"
            "rows all count from here; the file says where it can\n"
            "Palette > Wrap Palette Rows decides what happens\n"
            "to a row this pushes off the end",
        )
        row_base_row.addStretch(1)

        # Its own row under Format: it acts *on* the picked format rather than
        # being part of choosing it, and only Custom shows it at all.
        quantize_row = _dock_row(self._quantize_palette_action)

        # Details readout for the panel's selected color. Selectable text so
        # values can be copied out.
        self._color_details = QLabel("No color selected")
        self._color_details.setContentsMargins(_ROW_MARGIN, 0, _ROW_MARGIN, 0)
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
        self._export_palette_action.setToolTip(
            "Write these colors to a .pal file,\nencoded in the Format row's codec"
        )
        self._export_palette_action.clicked.connect(self._export_palette_file)

        # How the *next* palette file to come in is read - nothing more. A .pal
        # records nothing about its own encoding, so reading one is always a
        # guess; this is where the user makes it in advance, and it seeds each
        # newly registered palette entry's codec. It deliberately does not reach
        # back to whatever is already open: that palette has the Format row
        # above, which names its encoding and re-reads it. Always live, since it
        # is a reading choice rather than a property of anything on screen.
        self._palette_import_preset = self._preset_combo(
            Stage.INTERPRET_PALETTE, "bgr555"
        )
        self._palette_import_preset.setToolTip(
            "How the next palette file opened is read\n"
            "The Format row re-reads the one on screen"
        )
        # A row each: the two are opposite directions through the same file
        # format, and sharing a line left neither enough width at the dock's
        # natural size.
        import_row = _dock_row()
        self._palette_import_label = add_labelled(
            import_row,
            "Import as:",
            self._palette_import_preset,
            self._palette_import_preset.toolTip(),
        )
        import_row.addStretch(1)

        export_row = _dock_row(self._export_palette_action)

        container = QWidget()
        column = QVBoxLayout(container)
        column.setContentsMargins(0, _ROW_GAP, 0, _ROW_GAP)
        column.setSpacing(_ROW_GAP)
        column.addLayout(header)
        column.addLayout(format_row)
        column.addLayout(row_base_row)
        column.addLayout(quantize_row)
        column.addWidget(holder, 1)
        column.addWidget(self._color_details)
        column.addLayout(import_row)
        column.addLayout(export_row)

        self._palette_dock = QDockWidget("Palette", self)
        self._palette_dock.setObjectName("palette-dock")  # keeps saveState usable
        self._palette_dock.setWidget(container)
        # Stacked under the Files dock in the same left column (built first, so it
        # is there to split), and opened at its natural height - which the grid's
        # minimum above already carries a full palette's rows of swatches into,
        # on top of the header, readout and buttons. Files takes what is left,
        # and either can be dragged from there.
        self.splitDockWidget(
            self._files_dock, self._palette_dock, Qt.Orientation.Vertical
        )
        wanted = self._palette_dock.sizeHint().height()
        self.resizeDocks(
            [self._files_dock, self._palette_dock],
            [max(1, self.height() - wanted), wanted],
            Qt.Orientation.Vertical,
        )
        # Sharing a column with the Files dock means one width serves both, and
        # left alone Qt settles on the palette's *minimum* - the width its header
        # can't go below, not one it reads well at. Ask for its natural width
        # instead; it costs the canvas nothing, since the palette shares this
        # column rather than holding one of its own.
        self.resizeDocks(
            [self._palette_dock],
            [self._palette_dock.sizeHint().width()],
            Qt.Orientation.Horizontal,
        )

    def _build_palette_menu(self) -> None:
        """Palette ▸ everything palette-flavoured: palette-from-selection,
        panel."""
        menu = self.menuBar().addMenu("&Palette")

        # Mnemonic "P", the letter of its own shortcut, which the canvas menu
        # (where this action also sits) keeps clear for it.
        self._palette_from_selection_action = QAction("&Palette from Selection", self)
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

        # Pinning is the other thing a selection can become palette-wise, so it
        # belongs here rather than on a menu of its own.
        menu.addSeparator()
        menu.addAction(self._pin_palette_action)
        menu.addAction(self._unpin_palette_action)
        menu.addAction(self._unpin_all_action)
        # Whether the pins are being *shown*, under the three that make them:
        # it is the same subject, and a toggle is not a gesture on a selection.
        menu.addSeparator()
        self._build_show_regions_action()
        menu.addAction(self._show_palette_regions_action)
        menu.addAction(self._show_palette_rows_action)
        # Below them and in a group of its own: the two above say whether the pins
        # are shown, this says how the *base* behaves at the palette's ends — which
        # a map's cells and a sprite's parts obey as much as a pin does.
        menu.addSeparator()
        menu.addAction(self._wrap_palette_rows_action)

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
        # Shown for every real palette, and live whenever it is shown: the
        # raw-bytes modes re-decode under it, Custom only relabels (its colors
        # are stored verbatim). The generated Default has no format to pick at
        # all, so the row goes away rather than greying out.
        # Quantize applies only to Custom's verbatim colors; the raw-bytes modes
        # already hold values their format can store, so it would be a no-op.
        self._quantize_palette_action.setVisible(mode is PaletteMode.CUSTOM)
        self._refresh_palette_file_label()
        self._sync_palette_export_action()
        self._sync_palette_mode_items()
        # A file palette is Ctrl+W's second target, so loading or dropping one
        # changes what Write has to offer.
        self._sync_write_action()

    # -- the base palette row ------------------------------------------------
    def _sync_row_base(self) -> None:
        """Show the palette row base in force, where anything counts from it.

        The value is read off the **document**, which carries the base in force —
        what the file said until this control overrode it
        (:meth:`~...session.SessionMixin._row_base_for`) — so the spin opens on 8
        for a sprite, or on a bank's own stated row, rather than on a 0 that
        would misdescribe what is drawn.

        Hidden on a tilemap whose format gives a cell no palette row: nothing
        there names a row for a base to shift, and the colours are the view's
        Subpal to choose instead — the two are never both live
        (:meth:`~...rendering.RenderingMixin._sync_subpalette`). Shown on every
        pixel entry, whose pinned rows count from it whether or not any are
        pinned yet; hidden with no document at all, a palette shown on its own
        having nothing to be a base *for*.
        """
        doc = self._doc
        shown = doc is not None and (not doc.is_tilemap or doc.cells_carry_palette_rows)
        self._row_base_label.setVisible(shown)
        self._row_base.setVisible(shown)
        if doc is None:
            return
        with signals_blocked(self._row_base):
            self._row_base.setValue(doc.palette_row_base)

    def _on_row_base_change(self, value: int) -> None:
        """Redraw through a different palette row base — never a re-read.

        The base reaches the screen as an index shift at render time, so nothing
        about which bytes were read has moved and a refresh is the whole of
        applying it. That matters most for a *pixel* entry, whose document holds
        edits the file does not: re-reading to change a number would cost the
        user their work.
        """
        entry = self._workspace.current
        doc = self._doc
        if entry is None or doc is None or self._applying_undo:
            return
        before = (entry.palette_row_base, doc.palette_row_base)
        if before == (value, value):
            return  # the entry already chose this: not a history step
        self._push_command(
            PaletteRowBaseCommand(
                self,
                entry,
                f"set base palette row to {value}",
                before,
                (value, value),
            )
        )

    def _set_palette_row_base(
        self, entry: Entry, chosen: int | None, in_force: int
    ) -> None:
        """Land a base — :class:`PaletteRowBaseCommand`'s apply, both directions.

        Both halves land: ``chosen`` is what the project keeps (``None`` where
        the user has not overridden the file), ``in_force`` the resolved number
        every render reads.
        """
        entry.palette_row_base = chosen
        if entry.doc is not None:
            entry.doc.palette_row_base = in_force
        if self._doc is not None:
            self._refresh_view()  # which also re-marks the project modified

    def _sync_palette_export_action(self) -> None:
        """Arm the dock's Export to File button iff there is a palette to write."""
        self._export_palette_action.setEnabled(
            self._palette_doc() is not None and self._palette_mode.is_exportable
        )

    def _sync_palette_mode_items(self) -> None:
        """Grey out the load modes that have no graphic to act on.

        Offset reads the graphic's own bytes, and Default/Custom/Emulator all
        store their colors *into* one (an edit to either of the last two forks a
        Custom palette, which lives in the graphic's project record). With
        nothing open only File means anything - open a ``.pal`` and edit it on
        its own - so the rest are disabled rather than answering "Open pixel
        data first" to a click that looked available.
        """
        graphic = self._doc is not None
        for index in range(self._palette_mode_combo.count()):
            mode = PaletteMode.parse(self._palette_mode_combo.itemData(index))
            # Default stays selectable with nothing open: it is the resting
            # state the dock shows read-only, and picking it is how you put a
            # standalone palette away again.
            enabled = graphic or mode in (PaletteMode.FILE, PaletteMode.DEFAULT)
            item = self._palette_mode_combo.model().item(index)
            if item is not None:
                item.setEnabled(enabled)

    def _refresh_palette_file_label(self) -> None:
        """Point the dock's file label at the palette's external source.

        Only the file/emulator modes have one. A degraded source (mode kept,
        file gone - see ``Entry.missing_palette``) still names its intended
        file, marked missing; otherwise the path is read off the live config -
        or off the previewed entry, which has no document behind it.
        """
        path, missing = None, False
        doc = self._palette_doc()
        if doc is not None and self._palette_mode.has_external_file:
            path = doc.palette_config.source.path or None
            entry = self._workspace.current
            if path is None and entry is not None and entry.missing_palette:
                path, missing = entry.missing_palette.path, True
        elif doc is None and self._preview_palette is not None:
            path = self._preview_palette.path
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
