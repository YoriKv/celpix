"""How the bytes on screen are read: codec, container, and arrangement.

The decode axes (the pixel preset, the container that decides *which* bytes
the entry is) together with the display axes on the toolbars - block grouping,
fill order, 2D - and the plugin registry they all resolve through.

The load rule that shapes this module: switching the **preset** re-reads nothing,
because it only changes how the same buffer is interpreted, and re-running the
pathway there would pull the file's bytes back over unsaved edits. Changing the
container (or anything else feeding Read/Decompress) genuinely changes which bytes
the entry is, and must load. :func:`_same_bytes` is the test.
"""

from __future__ import annotations

from PySide6.QtGui import QPalette
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QToolBar,
    QWidget,
)

from celpix.core.arrangement import (
    ARRANGEMENT_PRESETS,
    ArrangementPreset,
    arrangement_preset_for,
)
from celpix.core.errors import PipelineError, Stage
from celpix.pipeline import pipeline
from celpix.pipeline.pathway import PathwayConfig
from celpix.plugins.base import NO_COMPRESSION, category_order
from celpix.project.workspace import (
    Entry,
    EntryKind,
    composite_config,
    pixel_config_for,
    repair_presets,
)
from celpix.ui.searchable_combo import (
    SearchableComboBox,
    fill_grouped,
    preset_rows,
)
from celpix.ui.undo_commands import (
    PaletteState,
    PixelConfigCommand,
)
from celpix.ui.widgets import (
    PRESET_COMBO_WIDTH,
    ChecklistPopupButton,
    CompactComboBox,
    ZoomSpinBox,
    add_labelled,
    funnel_icon,
    select_combo_data,
    signals_blocked,
    value_spin,
)

# The Rows control's tooltip, in its two states: it is the window height until
# View > Entire File takes that over, and a locked input has to say what locked
# it (see MainWindow._sync_entire_file).
ROWS_TIP = "Tile rows shown"
ROWS_LOCKED_TIP = "Tile rows shown\nLocked by View > Entire File"
# A tilemap is always shown entire, so unlike the line above this is not a
# setting being held — there is no window to set the height of.
ROWS_WHOLE_TIP = "Tile rows shown\nA tilemap is always shown whole"

# The Subpal control's tooltip, in its two states. It picks the range of palette
# entries the picture indexes into — except on a tilemap whose cells name a row
# each, where the file has already answered and a second answer would shift a map
# that is in the right colours (see MainWindow._sync_subpalette).
# The Tile size readout's two states. On a tilemap the unit the user points at,
# selects and edits is the cell — which may be a 2x2 metatile — so both the
# caption and the number follow it (see MainWindow._refresh_tile_size).
TILE_SIZE_TIP = "Size of one tile in pixels"
TILE_SIZE_CELL_TIP = (
    "Size of one map cell in pixels\nSelections and edits work a cell at a time"
)

SUBPAL_TIP = "Which range of palette entries tiles index into"
# On a tilemap whose cells carry rows the spin does not recolour anything - the
# file has answered that, and a view-wide row on top would shift a map already in
# its authored colours. It is still the row being *pointed at*: the palette grid
# outlines it, the tile sheet is read in it, and Set Selection's Palette Row
# writes it into the cells. So it stays live and says which of the two it is.
SUBPAL_CELLS_TIP = (
    "Which palette row the next assignment uses\n"
    "This map draws through its cells' own rows; Palette >\n"
    "Set Selection's Palette Row writes this one into them"
)

# The Cols control's tooltip, and what it reads while a paged tilemap's assembly
# owns the width (see MainWindow._settle_tilemap_width). Locked rather than
# left free because an assembly *is* a width: any other column count cuts the
# pages at the wrong place and shears the picture.
COLS_TIP = "Tiles per row"
# What a column *is* differs by document, and the number cannot say so itself.
# A tilemap is laid out in the unit it is drawn in — a cell, which may be a
# metatile of several tiles — and a sprite object in whole frames: its Cols lays
# out the strip of frames the canvas shows
# (:func:`~celpix.pipeline.render.sprite_sheet`), while the pieces inside a frame
# sit at the offsets the file gives them whatever the strip is doing.
COLS_CELLS_TIP = "Cells per row\nA cell may be a metatile of several tiles"
COLS_FRAMES_TIP = "Frames per row\nLays out the strip of frames, not the tiles in one"
# Says what has taken Cols over. There is no control to point at — a file that
# fixes its width states it itself — so these name the *file* as the authority
# rather than sending the user looking for a picker along some other row. Three
# wordings because three different things do it, and the reason is the only part
# the user can act on: one file assembles pages, one stores its colours for
# blocks of cells and has to be laid out on the grid it stores them in, and one
# draws a stamp per entry
# (:attr:`~celpix.core.document.Document.drawn_columns`).
COLS_ASSEMBLED_TIP = "Cells per row\nFixed by how this file's pages assemble"
COLS_ROW_PLANE_TIP = (
    "Cells per row\nFixed by the format: it stores one palette row\n"
    "per block of cells, counted in the file's own rows"
)
COLS_STAMPED_TIP = "Cells per row\nFixed by the stamp each of this file's entries draws"
# And the dense map whose format states no width, where Cols is live: the number
# is the user's, but it is theirs a stamp at a time, so say so before the spin
# hands back a value they did not type
# (:attr:`~celpix.core.document.Document.stamp_columns`).
COLS_STAMPS_TIP = "Cells per row\nRounded down to whole stamps"

# The Block W×H spins' width. Wide enough for the one digit every arrangement in
# hand uses, with room to read a two-digit value typed into Custom — the range
# allows 64 across and 256 down, and a box sized to the range would be a Rows-wide
# reservation for a "2".
BLOCK_SPIN_WIDTH = 42


def _group(caption: str, *widgets: QWidget, tooltip: str = "") -> QWidget:
    """A captioned run of toolbar controls as one widget, so it hides as one.

    A toolbar hides what it is *told about* — the action ``addWidget`` returns —
    so a control that has to disappear has to be one widget with one action. That
    is the whole reason these are grouped rather than added loose: a tilemap
    entry swaps a whole run of the codecs bar, and hiding four widgets while
    their action still holds the space would leave a gap where the run was.

    Spacing matches the toolbar's own so a grouped run reads no differently from
    the loose ones beside it; ``tooltip`` (when given) applies to the caption and
    the first widget, which is :func:`add_labelled`'s rule.
    """
    box = QWidget()
    row = QHBoxLayout(box)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(10)
    first, rest = widgets[0], widgets[1:]
    add_labelled(row, caption, first, tooltip or first.toolTip())
    for widget in rest:
        row.addWidget(widget)
    return box


def _same_bytes(a: PathwayConfig, b: PathwayConfig) -> bool:
    """Would both configs' Read + Decompress produce the same bytes?

    Everything downstream of Decompress - the Interpret preset - only decides how
    those bytes are *read*, so when this holds the loaded buffer is still valid
    and must not be fetched again (see :meth:`InterpretationMixin._pixel_data_for`).
    """
    return (a.source, a.container_id, a.compression_id) == (
        b.source,
        b.container_id,
        b.compression_id,
    )


class InterpretationMixin:
    """The codec, container and arrangement the bytes are read through.

    A slice of :class:`~celpix.ui.main_window.window.MainWindow`, not a
    standalone object: it reads and writes the window's own widgets and its
    single live ``_doc``. See the module docstring for what it owns, and the
    package docstring for why these are mixins.
    """

    def _pixel_config(self, entry: Entry, preset_id: str) -> PathwayConfig:
        """``entry``'s pixel pathway config, in this workspace.

        The workspace is what lets a slice of a parent with unsaved edits read
        those edits instead of the stale file (:func:`pixel_config_for`), so every
        config the window builds goes through here rather than calling the factory
        directly and silently losing that.

        It is also where a slice's parent settles any fold it owes: the config is
        what carries the parent's live buffer to the read (``_parent_view_bytes``),
        so this is the moment those bytes have to be true
        (:meth:`~...writing.WritingMixin._settle_region`).

        A **composite** settles every one of its pieces for the same reason and
        goes through :meth:`~...session.SessionMixin._composite_layout`, which is
        where that happens — and which records the run lengths the assembly
        measured, so this is also what keeps them current
        (``docs/design/composite-entry.md``).

        ``preset_id`` reaches **both** halves of that, and has to. It is what the
        buffer is read through, and it is also the tile a whole-entry piece is
        rounded up to — so handing it to one and not the other would round the
        assembly at one depth and decode it at another, leaving the run lengths
        the assembly records disagreeing with the buffer they describe.
        """
        if entry.kind is EntryKind.COMPOSITE:
            return composite_config(
                entry,
                self._registry,
                self._workspace,
                layout=self._composite_layout(entry, preset_id),
                preset_id=preset_id,
            )
        self._settle_region(entry)
        return pixel_config_for(entry, preset_id, self._registry, self._workspace)

    def _build_toolbar(self) -> None:
        # Three stacked rows: the codec selects (what the bytes *are*) on top, the
        # tile arrangement (how those tiles are grouped/addressed) directly below
        # it, and the view settings (how they're shown) at the bottom.
        #
        # Placed in the canvas column (above the transform bar) rather than with
        # ``addToolBar``: QMainWindow's toolbar area spans the whole window width,
        # which would cut across the top of the Files/Palette column. Inserting
        # them here keeps every bar over the canvas it describes and leaves the
        # docks the window's full height. Immovable for the same reason the
        # transform bar is — there is no toolbar area to drag them to.
        codecs = QToolBar("Codecs")
        self._codecs_toolbar = codecs  # greyed out wholesale for a missing entry
        arrange = QToolBar("Arrangement")
        self._arrange_toolbar = arrange  # frozen wholesale during a scan
        view = QToolBar("View")
        self._view_toolbar = view  # frozen wholesale during a scan
        for index, bar in enumerate((codecs, arrange, view)):
            bar.setMovable(False)
            bar.layout().setSpacing(10)
            self._canvas_column.insertWidget(index, bar)

        # Which pixel presets the dropdown lists lives on the workspace, so the
        # project file persists it (self._workspace.hidden_pixel_presets); empty
        # means all. It's view-only — pruning the codec picker to the formats the
        # user cares about, without touching how any file is read.
        self._pixel_preset = self._preset_combo(Stage.INTERPRET_PIXEL, "snes-4bpp")
        self._pixel_preset.currentIndexChanged.connect(self._on_pixel_preset_change)
        # End a format-cycling run when focus leaves the dropdown: the next switch
        # then re-anchors on the live position rather than the stale target.
        self._pixel_preset.focus_lost.connect(self._end_pixel_switch_run)
        self._pixel_preset.setToolTip("Tile graphics format")
        # The combo and its filter button read as one control: grouped in a tight
        # container (no toolbar gap between them), same height, the button a plain
        # funnel icon that picks up the theme's button-text color.
        self._pixel_filter = ChecklistPopupButton(
            "Filter", self._pixel_filter_items, self._apply_pixel_filter
        )
        self._bake_pixel_filter_icon()
        self._pixel_filter.setToolTip("Which formats appear in the dropdown")
        self._pixel_filter.setFixedHeight(self._pixel_preset.sizeHint().height())
        # Whole groups rather than loose widgets, because a tilemap entry swaps
        # this end of the bar wholesale: what a *cell* is replaces what a tile
        # is, and neither the pixel format nor the compression preview says
        # anything about a map (see _sync_capabilities and the note below).
        pixel_pair = QWidget()
        pair = QHBoxLayout(pixel_pair)
        pair.setContentsMargins(0, 0, 0, 0)
        pair.setSpacing(2)  # no toolbar gap: the pair is one control
        pair.addWidget(self._pixel_preset)
        pair.addWidget(self._pixel_filter)
        self._pixel_codec_action = codecs.addWidget(
            _group("Pixel:", pixel_pair, tooltip=self._pixel_preset.toolTip())
        )
        # The palette format combo lives in the palette dock's header, next to
        # the mode it qualifies (_build_palette_dock).

        # A tilemap's own format picker, in the pixel one's place: the entry's
        # bytes are cells, and how they are read - field layout and byte order -
        # is the same kind of choice one row up from tiles. Its pathway has no
        # compression stage of its own either, so both go and this arrives.
        self._tilemap_preset = SearchableComboBox(PRESET_COMBO_WIDTH)
        self._tilemap_preset.setToolTip(
            "How a cell's bytes are read: field layout and byte order\n"
            "[T] grid map · [S] sprite map · [F] fontmap"
        )
        self._tilemap_preset.activated.connect(self._on_tilemap_preset_change)
        self._tilemap_codec_action = codecs.addWidget(
            _group("Tilemap:", self._tilemap_preset)
        )

        # Compression preview: the main view stays raw; the chosen Decompress
        # plugin runs over the current window and shows in the floating overlay.
        self._compression = SearchableComboBox(PRESET_COMBO_WIDTH)
        self._populate_compression()
        self._compression.currentIndexChanged.connect(self._on_view_change)
        # Structure navigation for contiguously packed compressed data: hop
        # past the structure in view, or walk forward looking for the next one.
        self._jump_next = QPushButton("Jump to Next")
        self._jump_next.setToolTip("Jump past the structure in view")
        self._jump_next.setEnabled(False)
        self._jump_next.clicked.connect(self._on_jump_next)
        self._scan_button = QPushButton("Scan")
        self._scan_button.setToolTip(
            "Scan for the next compressed structure; click again to stop"
        )
        self._scan_button.setEnabled(False)
        self._scan_button.clicked.connect(self._on_scan)
        # One click promotes the complete structure in view into a decompressed
        # slice entry in the files list - the overlay preview made editable.
        self._promote_button = QPushButton("To Slice")
        self._promote_button.setToolTip(
            "Add the structure in view as a decompressed slice"
        )
        self._promote_button.setEnabled(False)
        self._promote_button.clicked.connect(self._on_promote_structure)
        # The picker and the three buttons it drives travel together: they are
        # one feature, and the buttons with no codec to run would be furniture.
        self._compression_action = codecs.addWidget(
            _group(
                "Compression:",
                self._compression,
                self._jump_next,
                self._scan_button,
                self._promote_button,
                tooltip="Preview the window decompressed with this codec",
            )
        )

        # Sits immediately left of Cols because the two are read together: a
        # bitmap width re-cuts the codec's tiles and Cols is then derived from
        # whatever size that landed on, so the number it derives *from* has to
        # be on screen. Read-only - the size is the codec's, not a setting.
        self._tile_size = QLabel()
        # The caption is kept because a tilemap relabels the pair: what the user
        # points at, selects and edits there is a *cell*, which may be several
        # tiles across (see :meth:`_refresh_tile_size`).
        self._tile_size_label = add_labelled(
            view,
            "Tile:",
            self._tile_size,
            TILE_SIZE_TIP,
        )
        # Pinned wide enough for the sizes a re-cut reaches, so Cols doesn't
        # slide sideways each time the format or the width changes.
        self._tile_size.setMinimumWidth(
            self._tile_size.fontMetrics().horizontalAdvance("64\u00d764")
        )

        # Ranged well past a screenful of 8-px tiles because a bitmap width
        # derives this: a 4096-px bitmap of 8-px tiles is 512 columns.
        self._columns = value_spin(1, 512, 16, self._on_view_change)
        # The caption is kept for the same reason Rows' and Subpal's are: an
        # tilemap that fixes its own width locks the pair and has to say what
        # locked it (:meth:`~...rendering.RenderingMixin._settle_tilemap_width`).
        self._columns_label = add_labelled(view, "Cols:", self._columns, COLS_TIP)

        # A paged assembly and a dense stamp each take Cols over and have **no
        # control at all**: every format that does either states its own layout, so
        # there was never a choice to offer. That is why the caption above has to
        # say what locked it (:data:`COLS_ASSEMBLED_TIP`,
        # :data:`COLS_STAMPED_TIP`) — there is nothing on screen to infer it from.

        # How many tile-rows the window shows - the "render N rows" view setting.
        # Kept on self with its caption because View > Entire File locks the pair
        # (see MainWindow._sync_entire_file), which retooltips and greys both.
        self._rows = value_spin(1, 256, 16, self._on_view_change)
        self._rows_label = add_labelled(view, "Rows:", self._rows, ROWS_TIP)
        # Cols maxes at 2 digits, rows at 3, so their hints differ - pin both
        # to the rows hint so the pair reads as a matched set.
        rows_width = self._rows.sizeHint().width()
        self._columns.setFixedWidth(rows_width)
        self._rows.setFixedWidth(rows_width)

        # The one view spin that isn't ``_spin``: its levels include a fractional
        # one, so it steps through a list rather than counting (ZoomSpinBox).
        self._zoom = ZoomSpinBox()
        self._zoom.valueChanged.connect(self._on_view_change)
        add_labelled(
            view,
            "Zoom:",
            self._zoom,
            "Screen pixels per image pixel\n0.5 halves it, to read a whole map at once",
        )

        # Range 255: enough rows for a 512-entry palette under a 2-color (1bpp)
        # index space; the view refresh clamps to the loaded palette anyway.
        self._subpalette = value_spin(0, 255, 0, self._on_view_change)
        # The caption is kept because _sync_subpalette retooltips the pair
        # together on a tilemap whose cells carry their own rows, where the spin
        # picks a row to *assign* rather than one to draw through - and a label
        # is as likely to be hovered as the input
        # (:func:`~celpix.ui.widgets.add_labelled`).
        self._subpalette_label = add_labelled(
            view,
            "Subpal:",
            self._subpalette,
            SUBPAL_TIP,
        )

        # Whether these tiles are *letters* — the declaration a fontmap bound to
        # this sheet reads its codes through (``docs/design/fontmap-entry.md``).
        #
        # On this row and not on the fontmap's own bar because it is a fact about
        # the **art**: it is settled when the sheet is drawn, and every string in
        # the game that uses the sheet is bound by it. Last on the row and past
        # the spins, because it is the only control here that is not a view
        # setting — everything to its left changes what the canvas shows without
        # touching the file, and this one is a property of the entry. It is also
        # the only one that comes and goes (:meth:`_sync_use_as_font`), so a
        # position at the end is the one that does not shuffle its neighbours.
        #
        # A declaration and not an inference from the table being filled in, for
        # the reason `layout = "text"` on the map is one: it has to answer before
        # there is a table to look at, and it is what puts the editor within
        # reach of a sheet nobody has typed a letter into yet.
        self._use_as_font = QCheckBox("Use as Font")
        self._use_as_font.setToolTip(
            "Treat these tiles as letters, so a fontmap bound to\n"
            "this sheet reads its codes as words\n"
            "What they spell is typed in View > Font Alphabet\n"
            "Unticking keeps the table, but stops it being read"
        )
        self._use_as_font.toggled.connect(self._on_use_as_font_change)
        # The **action**, not the widget, is what :meth:`_sync_use_as_font` hides
        # by: a toolbar wraps a widget it is handed in a QWidgetAction and shows
        # that widget again on its next re-layout, so a `setVisible(False)` on
        # the checkbox comes undone the first time the window is resized
        # (``docs/py-qt-reference/pyside6-pitfalls.md``).
        self._use_as_font_action = view.addWidget(self._use_as_font)

        # The Selection Shape picker (what a canvas drag selects) lives on the
        # canvas transform toolbar - see :mod:`celpix.ui.main_window.transform` -
        # because it gates that bar's block transforms.

        # Arrangement (display-only placement/addressing, so these re-render like
        # zoom/grid - not undoable). Block W×H groups tiles into blocks; Order sets
        # how each block fills; 2D reads the source as one wide bitmap Cols across.
        # These share the codecs bar's second row (see _build_toolbar) rather than
        # the view row.
        #
        # Pattern names documented block/order/2D combinations and, like the Offset
        # format picker, fills + locks the individual controls when a preset is
        # chosen; "Custom" unlocks them so they can be hand-edited.
        self._pattern = CompactComboBox(PRESET_COMBO_WIDTH)
        for preset in ARRANGEMENT_PRESETS:
            self._pattern.addItem(preset.name, preset)
        self._pattern.addItem("Custom", "custom")
        self._pattern.setToolTip(
            "Arrangement preset; pick Custom to edit these yourself"
        )
        self._pattern.currentIndexChanged.connect(self._on_pattern_change)
        add_labelled(arrange, "Pattern:", self._pattern, self._pattern.toolTip())

        self._block_cols = value_spin(1, 64, 1, self._on_view_change)
        self._block_rows = value_spin(1, 256, 1, self._on_view_change)
        # Narrower than the three digits Cols and Rows are pinned to: a block is a
        # handful of tiles \u2014 every shipped arrangement is 1 or 2 a side \u2014 so
        # sizing these to their *range* reserved a Rows-wide box for a "2".
        self._block_cols.setFixedWidth(BLOCK_SPIN_WIDTH)
        self._block_rows.setFixedWidth(BLOCK_SPIN_WIDTH)
        self._block_cols.setToolTip("Tiles per block, across")
        self._block_rows.setToolTip("Tiles per block, down")
        # The "x" between the pair belongs to both, so it carries the whole
        # control's sense rather than either side's half.
        times = QLabel("\u00d7")
        times.setToolTip("Block size, in tiles")
        # The three read as one control, so they go in a tight container instead
        # of taking the toolbar's own gap between each \u2014 the treatment the pixel
        # format and its filter button already get. "2 x 4" with a full toolbar
        # gap either side of the x reads as three controls that happen to be
        # adjacent.
        block_pair = QWidget()
        pair = QHBoxLayout(block_pair)
        pair.setContentsMargins(0, 0, 0, 0)
        pair.setSpacing(2)
        pair.addWidget(self._block_cols)
        pair.addWidget(times)
        pair.addWidget(self._block_rows)
        # Buddied to the first spin: the container cannot take focus, so the
        # caption's mnemonic needs the input it actually names.
        add_labelled(
            arrange,
            "Block:",
            block_pair,
            "Tiles per block, across",
            buddy=self._block_cols,
        )
        self._block_order = QComboBox()
        self._block_order.setToolTip(
            "How each block fills:\n"
            "• Row - left to right, then down\n"
            "• Column - top to bottom, then right\n"
            "• Row-interleave - a tile-row across every block"
        )
        for label, data in (
            ("Row", "row"),
            ("Column", "column"),
            ("Row-interleave", "row-interleave"),
        ):
            self._block_order.addItem(label, data)
        self._block_order.currentIndexChanged.connect(self._on_view_change)
        add_labelled(arrange, "Order:", self._block_order, self._block_order.toolTip())
        self._two_d = QCheckBox("2D")
        self._two_d.setToolTip("Read as one wide bitmap, not back-to-back tiles")
        self._two_d.toggled.connect(self._on_two_d_change)
        arrange.addWidget(self._two_d)

        # The width the wide-bitmap read is *of* — so it belongs to 2D and is
        # live only with 2D on. A bitmap only lines up when whole tiles span its
        # width, which an 8-px tile can't do for (say) 306. Deliberately outside
        # _arrangement_controls: a Pattern preset picks the arrangement, not the
        # width of one particular asset, so it stays editable under a preset.
        self._bitmap_width = value_spin(0, 8192, 0, self._recut_tile_geometry)
        self._bitmap_width.setSuffix(" px")
        add_labelled(
            arrange,
            "Bitmap W:",
            self._bitmap_width,
            "Width of the 2D bitmap in pixels (needs 2D)\n"
            "0 keeps the codec's own tile size\n"
            "Any other width re-cuts tiles to the largest size\n"
            "that divides it (306 gives 6x6) and spans it in Cols\n"
            "Codecs with a fixed tile size are unaffected",
        )
        # The default view is Linear (the first preset), so start with the block
        # controls locked until Custom is picked.
        self._apply_pattern_lock()

    @property
    def _arrangement_controls(self) -> tuple[QWidget, ...]:
        """The individual block/order/2D widgets a Pattern preset drives.

        Exactly the four axes a preset states — the bitmap width is not one of
        them (no preset carries a width), so it is not locked away with these;
        :meth:`_settle_bitmap_width_and_columns` gates it on its own condition instead.
        """
        return (
            self._block_cols,
            self._block_rows,
            self._block_order,
            self._two_d,
        )

    def _apply_pattern_lock(self) -> None:
        """Enable the individual arrangement controls only under Custom; a named
        preset owns them, so they're read-only while one is selected.

        Locked is not inert — every one of these still drives the view while a
        preset holds it; the lock only says the preset is the thing choosing.
        The width is gated separately (see
        :meth:`_settle_bitmap_width_and_columns`), so it is settled afterwards
        rather than by the blanket rule.
        """
        custom = self._pattern.currentData() == "custom"
        for widget in self._arrangement_controls:
            widget.setEnabled(custom)
        self._settle_bitmap_width_and_columns()

    def _set_arrangement(
        self, block_columns: int, block_rows: int, block_order: str, two_d: bool
    ) -> None:
        """Push the four arrangement values onto their widgets with signals
        blocked - a preset fill (or a session restore) is one coherent change the
        caller re-renders once, not four cascading _on_view_change calls."""
        with signals_blocked(self._block_cols, self._block_rows, self._two_d):
            self._block_cols.setValue(block_columns)
            self._block_rows.setValue(block_rows)
            self._two_d.setChecked(two_d)
        select_combo_data(self._block_order, block_order)

    def _on_pattern_change(self) -> None:
        """Apply a chosen Pattern: a preset fills + locks the block/order/2D
        controls; Custom just unlocks them (leaving the current values as the
        starting point). Either way, re-render.

        Picking a Pattern also **clears the bitmap width**. A width is an
        override of the codec's own geometry chosen for one particular asset, not
        a standing preference, so it does not follow the user to a different
        arrangement - and leaving it set would be worse than untidy: it stays in
        force invisibly (the spin greys out under a preset) and springs back the
        moment the new arrangement is 2D. Clearing it hands the tile size and
        Cols back at the same time (:meth:`_settle_bitmap_width_and_columns`).
        """
        data = self._pattern.currentData()
        applied = self._effective_bitmap_width() > 0
        with signals_blocked(self._bitmap_width):
            self._bitmap_width.setValue(0)
        if isinstance(data, ArrangementPreset):
            self._set_arrangement(
                data.block_columns,
                data.block_rows,
                data.block_order,
                data.two_dimensional,
            )
        self._apply_pattern_lock()
        # A width that *was* in force re-cut the codec's tiles, so withdrawing it
        # is a geometry change and takes the re-interpretation path; otherwise
        # this is an ordinary re-render.
        if applied:
            self._recut_tile_geometry()
        else:
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

    def _preset_combo(self, stage: Stage, default_suffix: str) -> SearchableComboBox:
        # Compact: preset names run past 50 characters and the combo shares a row
        # with other controls, so the closed button takes the format pickers'
        # shared width; the popup stays full, and carries the search field and
        # the category headings a hundred-entry list needs.
        combo = SearchableComboBox(PRESET_COMBO_WIDTH)
        rows = preset_rows(self._registry.presets(stage))
        ids = [preset_id for _, _, preset_id in rows]
        default = next((i for i in ids if i.endswith(default_suffix)), None)
        fill_grouped(combo, rows, default)
        return combo

    # -- pixel-format filter ----------------------------------------------
    def _all_pixel_presets(self) -> list:
        """Every pixel preset the registry offers, in the dropdown's own order —
        by category, then by name — which is also the filter list's order, so the
        two read the same way down the page."""
        return sorted(
            self._registry.presets(Stage.INTERPRET_PIXEL),
            key=lambda p: (category_order(p.category), p.name),
        )

    def _fill_pixel_combo(self, select_id: str) -> None:
        """Repopulate the pixel dropdown with the un-hidden presets, always
        keeping ``select_id`` present and selected.

        The selected format is force-included even when the filter hides it: you
        can't hide the format actually interpreting the file, and every apply
        path (a switch, an undo, a session restore) lands here so that invariant
        holds. Signals stay blocked — the caller owns any reinterpretation.
        """
        hidden = self._workspace.hidden_pixel_presets
        visible = [
            preset
            for preset in self._registry.presets(Stage.INTERPRET_PIXEL)
            if preset.id not in hidden or preset.id == select_id
        ]
        with signals_blocked(self._pixel_preset):
            fill_grouped(self._pixel_preset, preset_rows(visible), select_id)

    def _bake_pixel_filter_icon(self) -> None:
        """Paint the filter button's funnel in the theme's button-text color.

        A pixmap, so it is baked and not styled: re-run when the theme or the
        device scale changes (``_rebake_icons``).
        """
        self._pixel_filter.setIcon(
            funnel_icon(
                self.palette().color(QPalette.ColorRole.ButtonText),
                ratio=self.devicePixelRatioF(),
            )
        )

    def _pixel_filter_items(self) -> list[tuple[str, str, bool]]:
        """``(id, name, checked)`` for every pixel preset — the filter popup's
        model. The format in force always reads as checked (it can't be hidden)."""
        current = self._pixel_preset_id()
        hidden = self._workspace.hidden_pixel_presets
        return [
            (
                preset.id,
                preset.name,
                preset.id not in hidden or preset.id == current,
            )
            for preset in self._all_pixel_presets()
        ]

    def _apply_pixel_filter(self, desired: set[str]) -> set[str]:
        """Set which pixel presets the dropdown lists; return the set in force.

        ``desired`` is the ids left checked. The list can never be emptied, so an
        empty request keeps the current format; unchecking the *current* format
        switches the view to the first remaining one. When that switch can't take
        (no document, or the bytes don't fit the new codec) nothing changes. The
        returned set is what actually ended up checked, so the popup can spring a
        clamped request back.
        """
        all_ids = {preset.id for preset in self._all_pixel_presets()}
        current = self._pixel_preset_id()
        desired = (set(desired) & all_ids) or {current}
        if current not in desired:
            target = next(
                (p.id for p in self._all_pixel_presets() if p.id in desired), None
            )
            if target is None or not self._try_switch_pixel(target):
                return all_ids - self._workspace.hidden_pixel_presets  # unchanged
        self._workspace.hidden_pixel_presets = all_ids - desired
        self._fill_pixel_combo(self._pixel_preset_id())
        # The filter is project state now, so changing it can dirty the project.
        self._refresh_project_modified()
        return desired

    def _try_switch_pixel(self, target: str) -> bool:
        """Move the dropdown to ``target`` and reinterpret through it, as an
        ordinary undoable switch. With no document open there is nothing to read,
        so it just moves the default selection and reports success."""
        self._fill_pixel_combo(target)
        if self._doc is None:
            return True
        return self._on_pixel_preset_change()

    # -- current selections ------------------------------------------------
    def _pixel_preset_id(self) -> str:
        return self._pixel_preset.currentData()

    def _palette_preset_id(self) -> str:
        return self._palette_preset.currentData()

    def _palette_import_preset_id(self) -> str:
        """The format a palette *file* is read with (the dock's Import as…).

        Separate from :meth:`_palette_preset_id`, which names the format of the
        palette currently on screen whatever its source.
        """
        return self._palette_import_preset.currentData()

    def _compression_id(self) -> str:
        """The compression-preview combo's plugin id, pass-through by default.

        The fallback matters before the combo is populated (session seeding runs
        during construction) and after a plugin refresh drops the selected
        scheme, both of which leave ``currentData()`` empty.
        """
        return self._compression.currentData() or NO_COMPRESSION

    def _pixel_bpp(self) -> int:
        return pipeline.pixel_bpp(self._pixel_preset_id(), self._registry)

    def _palette_base(self) -> int:
        """The first palette index the active subpalette row addresses.

        A tile stores an index into a window of the palette, so every render of
        decoded pixels needs this to turn those indices into colors. One
        definition, because the row and the window size are separate controls.
        """
        return self._subpalette.value() * self._index_space()

    def _index_space(self, preset_id: str | None = None) -> int:
        """The pixel format's color count - the subpalette row size.

        Capped at 256: a direct-color preset's bpp can be up to 32, and both
        the palette maths and the fallback palette top out at 256 entries. The
        bpp comes from the resolved codec's geometry (:func:`pipeline.pixel_bpp`),
        so a preset with no ``bpp`` param - a wide/odd-tile codec, a code format -
        is sized correctly rather than crashing on a missing key.

        Defaults to the currently selected preset; pass ``preset_id`` to size
        another format's index space (e.g. _apply_pixel_config's outgoing preset).
        A stale id (preset removed by a plugin refresh) falls back to the
        current preset rather than failing the reload.

        **On a tilemap the combo is not the answer.** A tilemap has no pixel
        format of its own — the picker is hidden there — and the one the combo
        holds is whatever the toolbar happened to show when the map was opened.
        Its tiles are read under the *bound* entry's format, and that is the
        space a cell's palette row steps in
        (:func:`~celpix.pipeline.pipeline.tilemap_tiles`), so a row would
        otherwise be sized against a format nothing on screen is using.
        """
        doc = self._doc
        if preset_id is None and doc is not None and doc.is_tilemap:
            preset_id = doc.pixel_config.interpret_preset_id
        if preset_id is not None:
            try:
                bpp = pipeline.pixel_bpp(preset_id, self._registry)
                return min(256, 1 << bpp)
            except (KeyError, PipelineError):
                pass
        return min(256, 1 << self._pixel_bpp())

    def _effective_bitmap_width(self) -> int:
        """The bitmap width actually in force — 0 unless the 2D walk is on.

        The width describes a *wide-bitmap* read, so it means nothing to the
        back-to-back tile walk; gating it here is what keeps the greyed-out
        spin from still quietly driving the codec's geometry.
        """
        return self._bitmap_width.value() if self._two_d.isChecked() else 0

    def _on_two_d_change(self) -> None:
        """2D toggled: re-render, or re-cut the geometry if a width is waiting.

        With a bitmap width set, switching the walk on or off changes the tile
        size itself (it comes into force, or reverts), so this has to take the
        geometry path rather than merely repaint.
        """
        if self._bitmap_width.value() > 0:
            self._recut_tile_geometry()
        else:
            self._on_view_change()

    def _recut_tile_geometry(self) -> None:
        """Re-cut the codec's tiles to the new bitmap width.

        Alone among the view controls this changes the document's *geometry* —
        bytes per tile, and therefore what a tile index means — so it takes the
        same re-interpretation path a format switch does, which re-lands the
        view on the byte position it was showing instead of on a tile index that
        now points somewhere else. Still display state, so nothing is pushed
        onto the undo stack.
        """
        if self._doc is None:
            return
        if self._apply_pixel_config(self._pixel_preset_id(), self._byte_position()):
            self.statusBar().showMessage(self._bitmap_width_note())

    def _bitmap_width_note(self) -> str:
        """What the bitmap width did to the tile size, for the status footer.

        The effect is invisible in the picture — a re-cut grid looks like any
        other grid — and a codec whose tile size is fixed silently ignores the
        whole setting, so the footer is where those two outcomes are told apart.
        """
        width = self._effective_bitmap_width()
        tile_w, tile_h = self._pixel_tile_size()
        if width <= 0:
            if self._bitmap_width.value() > 0:  # set, but the walk is off
                return f"Bitmap width needs 2D - {tile_w}x{tile_h} tiles"
            return f"Bitmap width off - {tile_w}x{tile_h} tiles"
        if width % tile_w:
            return (
                f"Bitmap width {width} px - no effect: "
                f"{self._pixel_preset.currentText()} has a fixed "
                f"{tile_w}x{tile_h} tile"
            )
        return (
            f"Bitmap width {width} px - {tile_w}x{tile_h} tiles, "
            f"{width // tile_w} columns"
        )

    def _settle_bitmap_width_and_columns(self) -> None:
        """Gate the width, and point Cols at it while it is in force.

        Editable wherever it means anything, which is exactly: the 2D walk is on
        (a back-to-back tile read has no bitmap width). Deliberately *not* also
        gated on Custom, unlike the block controls: no Pattern preset carries a
        width, so a preset has nothing to say about it and locking it under one
        would leave a width that is still in force with no way to change it —
        which is what a session restore lands on, since the Pattern reads back as
        whichever preset the four axes match.

        With a bitmap width the column count stops being a free choice: it is
        however many tiles span that width, and any other value would show the
        bitmap at the wrong stride. Cols is left alone when the tiles don't
        divide the width — a codec that ignored the override keeps its own tile
        size, and no column count spans the width with it.

        Runs from the render path, so every route into a new arrangement — the
        checkbox, a Pattern preset, a session restore — lands here without each
        having to remember to.
        """
        self._bitmap_width.setEnabled(self._two_d.isChecked())
        self._refresh_tile_size()
        width = self._effective_bitmap_width()
        tile_w = self._pixel_tile_size()[0]
        spans = width > 0 and tile_w > 0 and width % tile_w == 0
        self._columns.setEnabled(not spans)
        if spans:
            # Remembered on the take-over only, so repeated refreshes under the
            # same width don't record the derived count as if it were a choice.
            if self._columns_before_bitmap is None:
                self._columns_before_bitmap = self._columns.value()
            if width // tile_w != self._columns.value():
                with signals_blocked(self._columns):
                    self._columns.setValue(width // tile_w)
        elif self._columns_before_bitmap is not None:
            # The width stopped applying (cleared, 2D off, a codec that ignores
            # it): hand Cols back at the value it had before, since the derived
            # one described a bitmap that is no longer being read.
            with signals_blocked(self._columns):
                self._columns.setValue(self._columns_before_bitmap)
            self._columns_before_bitmap = None

    def _refresh_tile_size(self) -> None:
        """Show the size of the unit the picture on screen is made of.

        Reads the document's own geometry rather than the preset's, so both a
        bitmap width that re-cut the codec's tiles and a fixed-size codec that
        ignored the width read true. With nothing open there is no geometry to
        report - the 8x8 fallback would be a guess about the next file.

        On a **tilemap** that unit is the map's own cell, not the tile it is built
        from: a screen of 16x16 cells is read, selected and edited a cell at a
        time (:meth:`~...selection.SelectionMixin._cell_unit`), so reporting the
        bound bank's 8x8 would name a thing no gesture in that view acts on. The
        caption follows the number, since "Tile: 16x16" over cells made of four
        8x8 tiles is the one reading that is wrong either way round.
        """
        cell = self._grid_tilemap() is not None
        self._tile_size_label.setText("Cell:" if cell else "Tile:")
        tip = TILE_SIZE_CELL_TIP if cell else TILE_SIZE_TIP
        for widget in (self._tile_size, self._tile_size_label):
            widget.setToolTip(tip)
        if self._doc is None:
            self._tile_size.setText("\u2014")
            return
        tile_w, tile_h = self._pixel_tile_size()
        across, down = self._cell_unit()
        self._tile_size.setText(f"{tile_w * across}\u00d7{tile_h * down}")

    def _pixel_tile_size(self) -> tuple[int, int]:
        # The atomic tile size is the codec's (recorded on the document at load) - not
        # a preset field (geometry is the engine's fixed unit; display grouping into
        # larger tiles is a separate view option, not yet implemented).
        if self._doc is not None:
            return self._doc.tile_width, self._doc.tile_height
        return 8, 8

    def _adopt_pixel_data(self, px: pipeline.PixelData, cfg: PathwayConfig) -> None:
        """Update the open document's pixel bytes + geometry from a fresh load."""
        assert self._doc is not None
        self._doc.pixel_data = px.data
        self._doc.bytes_per_tile = px.bytes_per_tile
        self._doc.tile_width = px.tile_width
        self._doc.tile_height = px.tile_height
        self._doc.pixel_config = cfg
        self._doc.pixel_ctx = px.ctx
        # A re-read can produce a different set of notices than the one that
        # opened the entry - a container change is exactly that - so the row
        # follows the bytes rather than only the entry switch.
        self._refresh_current_entry_row()
        if not self._palette_mode.is_real:
            self._doc.palette = self._fallback_palette()

    def _on_pixel_preset_change(self) -> bool:
        """The pixel combo changed: validate the new interpretation, then push
        one undoable command whose first redo applies the pre-validated load.

        Anchor on the target from the first switch of this run, if one is live,
        so a series of switches all measure from the same intended position
        instead of from wherever the previous format's clamping happened to
        land. The first switch has none yet, so it seeds it from the live view.

        Returns whether the switch went through — False on an early bail (no
        document) or a load failure (already reported, combo reverted), which
        the filter uses to leave its own state untouched when a switch it drove
        can't take.
        """
        entry = self._workspace.current
        if self._doc is None or entry is None or self._applying_undo:
            return False
        if self._pixel_switch_target is None:
            self._pixel_switch_target = self._byte_position()
        # The doc still holds the outgoing interpretation here (only the combo
        # has moved), so the undo state reads straight off it.
        old_preset = self._doc.pixel_config.interpret_preset_id
        before = (old_preset, self._byte_position())
        preset_id = self._pixel_preset_id()
        # Rebuild from the entry, not the old config: a slice keeps its bounds
        # and codec ids, and a file re-derives its container.
        cfg = self._pixel_config(entry, preset_id)
        try:
            px = self._pixel_data_for(cfg)
        except PipelineError as exc:
            self._report(exc)
            # The doc never switched - snap the combo back onto its preset.
            select_combo_data(self._pixel_preset, old_preset)
            return False
        self._push_command(
            PixelConfigCommand(
                self,
                entry,
                f"switch pixel format to {self._pixel_preset.currentText()}",
                before=before,
                after=(preset_id, self._pixel_switch_target),
                preloaded=px,
            )
        )
        note = self._partial_tile_note()
        if note:
            self.statusBar().showMessage(f"Preset changed - {note}")
        return True

    def _pixel_data_for(
        self, cfg: PathwayConfig, *, reload: bool = False
    ) -> pipeline.PixelData:
        """``cfg``'s pixel bytes + geometry, going to disk only when it must.

        A pixel-format switch changes how the same bytes are *read as* tiles, not
        which bytes they are - so the live buffer is reinterpreted in place.
        Re-running the pathway there would pull the file's own bytes back over
        unsaved edits, silently undoing them. A container change (or any other
        change to the source, Read or Decompress ids) genuinely moves which bytes
        the entry is, and has to load; ``reload`` forces that for a plugin
        refresh, whose whole point is to re-run the reloaded plugins.
        """
        live = self._doc
        # The bitmap width re-cuts the codec's tile geometry, so it is an input
        # to every geometry resolution, not only to the one that set it.
        bitmap_width = self._effective_bitmap_width()
        if not reload and live is not None and _same_bytes(live.pixel_config, cfg):
            return pipeline.reinterpret_pixel_data(
                live.pixel_data, live.pixel_ctx, cfg, self._registry, bitmap_width
            )
        return pipeline.load_pixel_data(cfg, self._registry, bitmap_width)

    def _apply_pixel_config(
        self,
        preset_id: str,
        byte_position: int,
        preloaded: pipeline.PixelData | None = None,
        *,
        reload: bool = False,
    ) -> bool:
        """Re-interpret the current entry's bytes and land on ``byte_position``.

        The one application path for preset switches, container changes, plugin
        refreshes and their undos: syncs the codec widgets (signals blocked,
        the _restore_session pattern) and never pushes a command. ``preloaded``
        carries a push site's already-validated result; without it the pathway
        re-runs here (through :meth:`_pixel_data_for`, so a mere reinterpretation
        keeps unsaved edits), and a failure (reported) leaves the view untouched.

        The view offset is a tile index, so it maps to a different *byte*
        position under a new bytes-per-tile - ``byte_position`` re-lands the
        view exactly, with the sub-tile remainder becoming the byte nudge. The
        subpalette row is likewise re-anchored: the same row index means a
        different palette base under the new color count, so it is recomputed
        from the selected color (or the old base) to keep pointing at the
        same palette entries.
        """
        entry = self._workspace.current
        if self._doc is None or entry is None:
            return False
        old_group = self._index_space(self._doc.pixel_config.interpret_preset_id)
        cfg = self._pixel_config(entry, preset_id)
        if preloaded is not None:
            px = preloaded
        else:
            try:
                px = self._pixel_data_for(cfg, reload=reload)
            except PipelineError as exc:
                self._report(exc)
                return False
        # Rebuild rather than a plain select: the applied format may be one the
        # filter hides, and you can never hide the format actually in force.
        self._fill_pixel_combo(preset_id)
        self._adopt_pixel_data(px, cfg)
        # _refresh_view clamps the offset; the nudge stays < the new tile size.
        self._offset, self._nudge = divmod(byte_position, px.bytes_per_tile)
        anchor = self._palette_panel.selected_index()
        if anchor is None:
            anchor = self._subpalette.value() * old_group
        # Signals blocked: _refresh_view below re-renders (and re-clamps) once.
        with signals_blocked(self._subpalette):
            self._subpalette.setValue(anchor // self._index_space())
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

    def _refresh_plugins(self) -> None:
        """Developer aid: reload plugins from disk and re-run on the open file.

        Rebuilds the registry from both plugin roots - the user's folder and the
        open project's own (:meth:`_load_project_plugins`) - picking up
        added/changed/removed presets and code plugins (a changed code plugin
        passes the trust gate; one you approved this run reloads without a
        prompt), refreshes the preset menus, and re-decodes the currently open
        pixel/palette through the reloaded plugins.

        The pixel re-run goes back to disk so a reloaded Read/Decompress plugin
        is exercised too - except on an entry with unsaved edits, which live only
        in the loaded bytes and a re-read would throw away. There the refresh
        reinterprets what is in memory, so a changed *codec* still takes effect
        and the edits survive; a re-read happens on the entry's next load.

        **A tilemap is re-read whole instead** (:meth:`~...tilemap_bar.
        TilemapBarMixin._reload_tilemap`), because its pixel half is not its own:
        the tiles come from the entry it is bound to, and running the pixel
        pathway over *this* entry's bytes would decode the map's own cells as
        tiles and draw noise over a picture that was right. That path re-reads
        both halves under the binding, so the reloaded plugins are still
        exercised, and it carries the same unsaved-edit rule.
        """
        if self._reload_plugins is None:
            return
        entry = self._workspace.current
        # With the open project's path, so the scan covers its own plugins/
        # folder too - F5 is the way to pick up an edit there, exactly as it is
        # for the user's folder.
        self._registry, self._plugin_issues = self._reload_plugins(self._project_path)
        # A refresh can *remove* a format as easily as add one — a deleted preset
        # file, a plugin that no longer passes the trust gate — so the open
        # entries are put back in step with the registry before anything decodes
        # through it. Reported at the end, with the load issues.
        missing_presets = repair_presets(self._workspace.entries, self._registry)
        self._repopulate_presets()
        if self._doc is not None and self._doc.is_tilemap and entry is not None:
            # Cells, bound tiles and palette in one read, under the binding.
            self._reload_tilemap(entry)
        elif self._doc is not None:
            # Re-decode the open file's sources through the new registry - via
            # the application paths, never commands: a plugin refresh isn't an
            # edit and must not pollute the undo history.
            self._apply_pixel_config(
                self._pixel_preset_id(),
                self._byte_position(),
                reload=entry is None or not entry.pixel_dirty,
            )
            # Only a palette with an external source can be re-decoded; a
            # generated default or a project-stored custom palette has no bytes
            # to re-read (its config points at an empty path).
            if self._palette_mode.has_source:
                result = self._reinterpret_palette()
                if result is not None:
                    loaded, cfg = result
                    self._apply_palette_state(
                        PaletteState(
                            cfg.interpret_preset_id,
                            self._palette_mode,
                            loaded.palette,
                            cfg,
                            loaded.ctx,
                            base_bytes=loaded.data,
                        )
                    )

        parts = ["Plugins refreshed"]
        if self._doc is not None:
            parts.append("re-ran on current file")
        self.statusBar().showMessage("; ".join(parts) + ".")
        # Any plugin that failed the reload is a warning, surfaced modally.
        self._alert_plugin_issues()
        self._alert_missing_presets(missing_presets)

    def _load_project_plugins(self, project_path: str | None) -> None:
        """Rebuild the registry for the project at ``project_path``.

        The ``plugins/`` folder beside a project file belongs to the project, so
        it is scanned as the project opens and dropped again when it closes -
        both ends go through here, and the registry an entry decodes through is
        never one from the project before. Called *before* the workspace is
        replaced: a restored entry may name a preset only the project provides,
        and showing it decodes immediately.

        Its code plugins pass the same trust gate as the user's own, so opening
        a project that carries one asks first (:mod:`celpix.plugins.trust`).
        """
        if self._reload_plugins is None:
            return
        self._registry, self._plugin_issues = self._reload_plugins(project_path)
        # The one widget that keeps a reference of its own rather than reading
        # the window's live: its rows name each entry's container and which of
        # the three tilemap layouts it holds, both off the registry, and this is
        # a *different object* from the one it was built with. Left behind, every
        # row whose format the project itself provides reads as having none.
        self._files_panel.set_registry(self._registry)
        self._repopulate_presets()
        self._alert_plugin_issues()

    def _repopulate_presets(self) -> None:
        """Rebuild the preset combos from the (reloaded) registry, keeping the
        current selection when it still exists."""
        # The pixel combo goes through the filter (a refresh keeps hidden formats
        # hidden and lets newly added ones through); the selection is preserved.
        self._fill_pixel_combo(self._pixel_preset.currentData())
        current = self._palette_preset.currentData()
        # Block signals so repopulating doesn't fire a reload per item; the
        # refresh does one explicit reload afterwards.
        with signals_blocked(self._palette_preset):
            fill_grouped(
                self._palette_preset,
                preset_rows(self._registry.presets(Stage.INTERPRET_PALETTE)),
                current,
            )
        # The compression combo lists Decompress *plugins*, not presets, but
        # refreshes the same way (keep the selection when it survives the reload).
        with signals_blocked(self._compression):
            self._populate_compression(self._compression.currentData())

    def _partial_tile_note(self) -> str:
        """Status-bar warning when the data ends mid-tile, or ``""`` when aligned.

        Not an error: the trailing partial tile renders zero-padded, so the file
        stays viewable - the note just explains the padded tail.
        """
        assert self._doc is not None
        short = -len(self._doc.pixel_data) % self._doc.bytes_per_tile
        if not short:
            return ""
        return (
            f"data ends {short} byte(s) short of a whole "
            f"{self._doc.bytes_per_tile}-byte tile; the last tile is zero-padded"
        )
