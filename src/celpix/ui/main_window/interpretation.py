"""How the bytes on screen are read: the toolbar, the codec and the container.

The decode axes (the pixel preset, the container that decides *which* bytes
the entry is), the pixel-format filter, the preview and palette-view pickers,
and the toolbar that carries them together with the display axes. That toolbar
builds the arrangement controls too, whose push/apply cycle is
:mod:`~celpix.ui.main_window.arrangement`'s; the registry every picker lists is
reloaded by :mod:`~celpix.ui.main_window.plugins`.

The load rule that shapes this module: switching the **preset** re-reads nothing,
because it only changes how the same buffer is interpreted, and re-running the
pathway there would pull the file's bytes back over unsaved edits. Changing the
container (or anything else feeding Read/Decompress) genuinely changes which bytes
the entry is, and must load. :func:`_same_bytes` is the test.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtWidgets import (
    QApplication,
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
)
from celpix.core.capabilities import Capability
from celpix.core.errors import PipelineError, Stage
from celpix.pipeline import pipeline
from celpix.pipeline.pathway import PathwayConfig
from celpix.plugins.base import (
    NO_COMPRESSION,
    PALETTE_SWATCH_ENGINE,
    STAGE_DEFAULT_PRESET,
    category_order,
)
from celpix.project.workspace import (
    Entry,
    EntryKind,
    composite_config,
    pixel_config_for,
)
from celpix.ui.icon_font import icon_qicon
from celpix.ui.icons import Icon
from celpix.ui.searchable_combo import (
    SearchableComboBox,
    fill_grouped,
    preset_rows,
)
from celpix.ui.undo_commands import (
    PaletteViewFormatCommand,
    PixelConfigCommand,
    PixelFilterCommand,
    PreviewCompressionCommand,
)
from celpix.ui.widgets import (
    PRESET_COMBO_WIDTH,
    ChecklistPopupButton,
    CompactComboBox,
    ToolBarOverflow,
    ZoomSpinBox,
    add_labelled,
    load_float_setting,
    save_float_setting,
    select_combo_data,
    signals_blocked,
    value_spin,
    zoom_level_after,
)

# How close the user is standing, kept **app-wide** rather than per entry: it is
# not a fact about any one file, and a list of entries that each sprang back to
# their own magnification would make switching between them a jolt. Stored in
# QSettings beside the grid (``view_menu.GRID_SHOWN_KEY``), which is app-wide for
# the same reason, and deliberately not in the project file
# (``docs/design/project-format.md`` §7).
ZOOM_KEY = "view/zoom"

# The Rows control's tooltip, in its two states: it is the window height until
# View > Entire File takes that over, and a locked input has to say what locked
# it (see MainWindow._sync_entire_file).
ROWS_TIP = "Tile rows shown"
ROWS_LOCKED_TIP = "Tile rows shown\nLocked by View > Entire File"
# A tilemap is always shown entire, so unlike the line above this is not a
# setting being held — there is no window to set the height of.
ROWS_WHOLE_TIP = "Tile rows shown\nUnused: a tilemap is always shown whole"

# The Palette Row control's tooltip, in its two states. It picks the range of palette
# entries the picture indexes into — except on a tilemap whose cells name a row
# each, where the file has already answered and a second answer would shift a map
# that is in the right colours (see MainWindow._sync_palette_row).
# The Tile size readout's two states. On a tilemap the unit the user points at,
# selects and edits is the cell — which may be a 2x2 metatile — so both the
# caption and the number follow it (see MainWindow._refresh_tile_size).
TILE_SIZE_TIP = "Tile size in pixels"
TILE_SIZE_CELL_TIP = "Cell size in pixels\nSelections and edits work per cell"
TILE_SIZE_STAMP_TIP = "Stamp size in pixels\nSelections and edits work per stamp"

PALETTE_ROW_TIP = "Palette row the tiles index into"
# On a tilemap whose cells carry rows the spin does not recolour anything - the
# file has answered that, and a view-wide row on top would shift a map already in
# its authored colours. It is still the row being *pointed at*: the palette grid
# outlines it, the tile sheet is read in it, and Set Selection's Palette Row
# writes it into the cells. So it stays live and says which of the two it is.
PALETTE_ROW_CELLS_TIP = (
    "Palette row written by Palette > Set Selection's Palette Row\n"
    "This map draws through the rows its cells store"
)
# And under a direct-colour format, where pixels are colours and no row is read.
PALETTE_ROW_DIRECT_TIP = (
    "Palette row the tiles index into\n"
    "Unused: this pixel format stores colors, not indices"
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
COLS_CELLS_TIP = "Cells per row"
COLS_FRAMES_TIP = "Frames per row"
# Says what has taken Cols over. There is no control to point at — a file that
# fixes its width states it itself — so these name the *file* as the authority
# rather than sending the user looking for a picker along some other row. Three
# wordings because three different things do it, and the reason is the only part
# the user can act on: one file assembles pages, one stores its colours for
# groups of cells and has to be laid out on the grid it stores them in, and one
# draws a stamp per entry
# (:attr:`~celpix.core.document.Document.drawn_columns`).
COLS_ASSEMBLED_TIP = "Cells per row\nFixed by the file's page assembly"
COLS_ROW_PLANE_TIP = (
    "Cells per row\nFixed by the format: palette rows are stored\n"
    "per group of cells in the file's own row width"
)
COLS_STAMPED_TIP = "Cells per row\nFixed by the stamp each entry draws"
# And the dense map whose format states no width, where Cols is live: the number
# is the user's, but it is theirs a stamp at a time, so say so before the spin
# hands back a value they did not type
# (:attr:`~celpix.core.document.Document.stamp_columns`).
COLS_STAMPS_TIP = "Cells per row\nRounded down to whole stamps"
# A table of records — metatiles, sprite frames — drawn each as a rectangle: the
# width is the user's, in whole records (:attr:`~celpix.core.document.Document.
# records_across`).
COLS_RECORDS_TIP = "Cells per row\nRounded down to whole records"
# And the paged map whose format does not say how its pages assemble, where Cols
# is the control that does (:attr:`~celpix.core.document.Document.
# assembly_choices`). It has to say the unit, because the spin will not take the
# number typed: only arrangements that show every page are widths here.
COLS_PAGES_TIP = (
    "Cells per row, in whole pages\n"
    "The file does not state its page layout\n"
    "Snaps to widths that use every page"
)

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
    return (
        a.source,
        a.container_id,
        a.compression_id,
        a.inputs.get(Stage.COMPRESSION),
    ) == (b.source, b.container_id, b.compression_id, b.inputs.get(Stage.COMPRESSION))


class InterpretationMixin:
    """The toolbar, and the codec and container the bytes are read through.

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
        goes through :meth:`~...bindings.BindingsMixin._composite_layout`, which is
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
            ToolBarOverflow(bar)  # the » that shows what a narrow window cuts off
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
        self._pixel_preset.setToolTip("Pixel format")
        # The combo and its filter button read as one control: grouped in a tight
        # container (no toolbar gap between them), same height, the button a plain
        # funnel icon that picks up the theme's button-text color.
        self._pixel_filter = ChecklistPopupButton(
            "Filter", self._pixel_filter_items, self._apply_pixel_filter
        )
        self._bake_pixel_filter_icon()
        self._pixel_filter.setToolTip("Filter the formats listed")
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
            "Cell format: field layout and byte order\n"
            "[T] grid map · [S] sprite map · [F] fontmap"
        )
        self._tilemap_preset.activated.connect(self._on_tilemap_preset_change)
        # The badge beside the picker: present only when the cell engine
        # declares inputs — a mapping's parallel arrays — wearing whether they
        # resolve, and opening the Inputs window (``main_window/inputs.py``).
        self._tilemap_inputs_badge = self._make_inputs_badge(
            "Inputs this cell format reads from elsewhere in the file"
        )
        self._tilemap_inputs_badge.clicked.connect(self._inputs_current)
        self._tilemap_codec_action = codecs.addWidget(
            _group("Tilemap:", self._tilemap_preset, self._tilemap_inputs_badge)
        )

        # Compression preview: the main view stays raw; the chosen Decompress
        # plugin runs over the current window and shows in the floating overlay.
        self._compression = SearchableComboBox(PRESET_COMBO_WIDTH)
        self._populate_compression()
        self._compression.currentIndexChanged.connect(
            self._on_preview_compression_change
        )
        # Structure navigation for contiguously packed compressed data: hop
        # past the structure in view, or walk forward looking for the next one.
        self._jump_next = QPushButton("Jump to Next")
        self._jump_next.setToolTip("Jump to the end of the structure in view")
        self._jump_next.setEnabled(False)
        self._jump_next.clicked.connect(self._on_jump_next)
        self._scan_button = QPushButton("Scan")
        self._scan_button.setToolTip(
            "Scan forward for the next compressed structure\nClick again to stop"
        )
        self._scan_button.setEnabled(False)
        self._scan_button.clicked.connect(self._on_scan)
        # Scan again, but only stop on a structure that plausibly holds tiles
        # of the current pixel format (pipeline.looks_like_graphics). Making
        # the structure in view a slice is File > New Slice from View.
        self._smart_scan_button = QPushButton("Smart Scan")
        self._smart_scan_button.setToolTip(
            "Scan forward for the next compressed structure that\n"
            "decodes to plausible tiles in the current pixel format\n"
            "Click again to stop"
        )
        self._smart_scan_button.setEnabled(False)
        self._smart_scan_button.clicked.connect(self._on_smart_scan)
        # The compression badge speaks for the *preview* codec, bound on the
        # file on screen: what the overlay decodes with, what Scan hunts with,
        # and what a slice carved under that codec inherits.
        self._compression_inputs_badge = self._make_inputs_badge(
            "Inputs this codec reads from elsewhere in the file\n"
            "Used by the preview and by slices cut under it"
        )
        self._compression_inputs_badge.clicked.connect(self._inputs_current)
        # The picker and the three buttons it drives travel together: they are
        # one feature, and the buttons with no codec to run would be furniture.
        self._compression_action = codecs.addWidget(
            _group(
                "Compression:",
                self._compression,
                self._compression_inputs_badge,
                self._jump_next,
                self._scan_button,
                self._smart_scan_button,
                tooltip="Preview the view decompressed with this codec",
            )
        )
        # The compression group's stand-in while the pixel picker reads the
        # bytes as palette colors: there is nothing to decompress on a color
        # table, and what the view needs instead is which color format the
        # entries are in. Same slot, so the bar keeps its shape — the swap is
        # made in _sync_palette_view_bar, one level under the kind's gate.
        self._palette_view_preset = self._preset_combo(
            Stage.INTERPRET_PALETTE, "bgr555"
        )
        self._palette_view_preset.setToolTip(
            "Palette color format, when the view reads bytes as colors"
        )
        self._palette_view_preset.currentIndexChanged.connect(
            self._on_palette_view_change
        )
        self._palette_view_action = codecs.addWidget(
            _group("Palette:", self._palette_view_preset)
        )
        self._palette_view_action.setVisible(False)
        self._bake_inputs_badges()

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
        # derives this: a 4096-px bitmap of 8-px tiles is 512 columns. The range
        # runs further still because a **tilemap's** width is not a preference
        # the user would ever type: a map bound to a stamp table strides the
        # source by the width its cells are laid at
        # (``session._chain_source_columns``), and a table of four corner arrays
        # is laid at twice its own length — 1,070 cells for a 535-stamp table.
        # Clamped, that entry's stored width silently becomes the wrong stride
        # and every stamp's lower half resolves from the wrong row, which draws
        # a picture rather than failing.
        self._columns = value_spin(1, 8192, 16, self._axis_slot("columns"))
        # The caption is kept for the same reason Rows' and Palette Row's are: an
        # tilemap that fixes its own width locks the pair and has to say what
        # locked it (:meth:`~...rendering.RenderingMixin._settle_tilemap_width`).
        self._columns_label = add_labelled(view, "Cols:", self._columns, COLS_TIP)

        # A stated page assembly and a stated stamp each take Cols over and have
        # **no control at all**: the file says its own layout, so there is no
        # choice to offer. That is why the caption above has to say what locked it
        # (:data:`COLS_ASSEMBLED_TIP`, :data:`COLS_STAMPED_TIP`) — there is nothing
        # on screen to infer it from. Pages the file does *not* lay out are the
        # one case Cols stays live over, as the pages-across control
        # (:data:`COLS_PAGES_TIP`).

        # How many tile-rows the window shows - the "render N rows" view setting.
        # Kept on self with its caption because View > Entire File locks the pair
        # (see MainWindow._sync_entire_file), which retooltips and greys both.
        self._rows = value_spin(1, 256, 16, self._axis_slot("rows"))
        self._rows_label = add_labelled(view, "Rows:", self._rows, ROWS_TIP)
        # Cols maxes at 2 digits, rows at 3, so their hints differ - pin both
        # to the rows hint so the pair reads as a matched set.
        rows_width = self._rows.sizeHint().width()
        self._columns.setFixedWidth(rows_width)
        self._rows.setFixedWidth(rows_width)

        # The one view spin that isn't ``_spin``: its levels include a fractional
        # one, so it steps through a list rather than counting (ZoomSpinBox).
        # Seeded from the app-wide preference, and snapped on the way in: a level
        # this build has no member for - an older settings file, or one edited by
        # hand - would otherwise sit in the box as a value the arrows step away
        # from and can never return to.
        self._zoom = ZoomSpinBox(zoom_level_after(load_float_setting(ZOOM_KEY, 4.0), 0))
        self._zoom.valueChanged.connect(self._on_zoom_change)
        add_labelled(
            view,
            "Zoom:",
            self._zoom,
            "Screen pixels per image pixel\n"
            "Shared by every entry; remembered between sessions",
        )

        # Range 255: enough rows for a 512-entry palette under a 2-color (1bpp)
        # index space; the view refresh clamps to the loaded palette anyway.
        self._palette_row = value_spin(0, 255, 0, self._axis_slot("palette row"))
        # The caption is kept because _sync_palette_row retooltips the pair
        # together on a tilemap whose cells carry their own rows, where the spin
        # picks a row to *assign* rather than one to draw through - and a label
        # is as likely to be hovered as the input
        # (:func:`~celpix.ui.widgets.add_labelled`).
        self._palette_row_label = add_labelled(
            view,
            "Palette Row:",
            self._palette_row,
            PALETTE_ROW_TIP,
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
            "Treat these tiles as a font, so a bound fontmap reads\n"
            "its codes as text. Characters are set in View > Font Alphabet\n"
            "Unticking deletes that alphabet"
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

        # Arrangement: display-only placement/addressing, and undoable all the
        # same - the project file keeps all five, and a Pattern preset moves four
        # of them at once (:class:`~celpix.ui.undo_commands.ArrangementCommand`).
        # Block W×H groups tiles into blocks; Order sets
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
            "Tile arrangement preset\nCustom unlocks the fields to the right"
        )
        # What the picker settled on last, which the view does not record and a
        # ``currentIndexChanged`` handler can no longer read (the combo has
        # already moved) - see
        # :meth:`~...arrangement.ArrangementMixin._stored_arrangement`.
        self._pattern_choice = self._pattern.currentData()
        self._pattern.currentIndexChanged.connect(self._on_pattern_change)
        add_labelled(arrange, "Pattern:", self._pattern, self._pattern.toolTip())

        self._block_cols = value_spin(1, 64, 1, self._arrangement_slot("block size"))
        self._block_rows = value_spin(1, 256, 1, self._arrangement_slot("block size"))
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
        times.setToolTip("Block size in tiles")
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
            "Order tiles fill a block:\n"
            "• Row - left to right, then down\n"
            "• Column - top to bottom, then right\n"
            "• Row-interleave - one tile row across every block"
        )
        for label, data in (
            ("Row", "row"),
            ("Column", "column"),
            ("Row-interleave", "row-interleave"),
        ):
            self._block_order.addItem(label, data)
        self._block_order.currentIndexChanged.connect(
            self._arrangement_slot("block order")
        )
        add_labelled(arrange, "Order:", self._block_order, self._block_order.toolTip())
        self._two_d = QCheckBox("2D")
        self._two_d.setToolTip("Read as one wide bitmap instead of consecutive tiles")
        self._two_d.toggled.connect(self._arrangement_slot("2D"))
        arrange.addWidget(self._two_d)

        # The width the wide-bitmap read is *of* — so it belongs to 2D and is
        # live only with 2D on. A bitmap only lines up when whole tiles span its
        # width, which an 8-px tile can't do for (say) 306. Deliberately outside
        # _arrangement_controls: a Pattern preset picks the arrangement, not the
        # width of one particular asset, so it stays editable under a preset.
        self._bitmap_width = value_spin(
            0, 8192, 0, self._arrangement_slot("bitmap width")
        )
        self._bitmap_width.setSuffix(" px")
        add_labelled(
            arrange,
            "Bitmap W:",
            self._bitmap_width,
            "Bitmap width in pixels (2D only)\n"
            "0 keeps the codec's tile size; any other width re-cuts\n"
            "tiles to the largest size that divides it and sets Cols\n"
            "No effect on codecs with a fixed tile size",
        )
        # The default view is Linear (the first preset), so start with the block
        # controls locked until Custom is picked.
        self._apply_pattern_lock()

    def _on_zoom_change(self, value: float) -> None:
        """The Zoom spin moved: remember it app-wide, then re-render.

        Written on every change rather than at shutdown, so a session that ends
        badly does not take the zoom with it — the store is the same one the grid
        writes to on each toggle, for the same reason. It is *not* a project
        change: the zoom is not in the project file, so moving it must leave a
        saved session reading clean (:meth:`MainWindow._project_is_dirty`).
        """
        save_float_setting(ZOOM_KEY, value)
        self._on_view_change()

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
        """Stamp the filter button's funnel in the theme's button-text color.

        A pixmap, so it is baked and not styled: re-run when the theme or the
        device scale changes (``_rebake_icons``).
        """
        self._pixel_filter.setIcon(
            icon_qicon(
                Icon.FUNNEL, QApplication.palette(), ratio=self.devicePixelRatioF()
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

        One popup visit is **one** undo step even when it is two commands: the
        forced switch is an ordinary ``PixelConfigCommand`` and the list is a
        :class:`~celpix.ui.undo_commands.PixelFilterCommand`, and a macro over
        the pair is what stops Ctrl+Z restoring thirty hidden formats while
        leaving the view on a codec the user never chose.
        """
        all_ids = {preset.id for preset in self._all_pixel_presets()}
        current = self._pixel_preset_id()
        desired = (set(desired) & all_ids) or {current}
        before = frozenset(self._workspace.hidden_pixel_presets)
        after = frozenset(all_ids - desired)
        if after == before:
            return desired
        switching = current not in desired
        switch = None
        if switching:
            target = next(
                (p.id for p in self._all_pixel_presets() if p.id in desired), None
            )
            if target is None:
                return all_ids - before  # unchanged
            # Validated before the macro opens: a macro cannot be abandoned once
            # begun, so a doomed switch inside one would still leave an empty
            # step on the stack (and cut off the redo history on its way).
            # With no document there is nothing to read, so the moved selection
            # is the whole switch.
            self._fill_pixel_combo(target)
            if self._doc is not None:
                switch = self._prepare_pixel_switch()
                if switch is None:
                    return all_ids - before  # unchanged
            self._undo_stack.beginMacro("filter pixel formats")
        try:
            if switch is not None:
                self._push_pixel_switch(switch)
            self._push_command(
                PixelFilterCommand(self, "filter pixel formats", before, after)
            )
        finally:
            if switching:
                self._undo_stack.endMacro()
        return desired

    def _apply_pixel_filter_set(self, hidden: set[str]) -> None:
        """Land a hidden-id set on the workspace and refill the dropdown.

        The application path for the filter popup, its undo and its redo. The
        combo is rebuilt rather than reselected because the format in force is
        force-shown whatever the list says - you can never hide what you are
        looking at - so which items exist depends on both.
        """
        self._workspace.hidden_pixel_presets = set(hidden)
        self._fill_pixel_combo(self._pixel_preset_id())
        # The filter is project state, so changing it can dirty the project.
        self._refresh_project_modified()

    # -- current selections ------------------------------------------------
    def _pixel_preset_id(self) -> str:
        return self._pixel_preset.currentData()

    def _palette_preset_id(self) -> str:
        return self._palette_preset.currentData()

    def _palette_view_preset_id(self) -> str:
        """The color format the palette-swatch view reads through — the toolbar
        picker's, falling back to the stage default before it is populated."""
        return (
            self._palette_view_preset.currentData()
            or STAGE_DEFAULT_PRESET[Stage.INTERPRET_PALETTE]
        )

    def _palette_view_active(self) -> bool:
        """Whether the pixel picker names a preset over the palette-swatch engine.

        Off the **picker** rather than the document, because the bar it decides
        configures the next open as much as the current one: with nothing loaded
        the picker still says how bytes will be read, and the palette-format
        group belongs beside it then too. Decided by the engine, not the shipped
        preset's id, so a user's own preset over the same engine gets the group.
        """
        preset_id = self._pixel_preset_id()
        if not preset_id or not self._registry.has_preset(preset_id):
            return False
        return self._registry.preset(preset_id).engine_id == PALETTE_SWATCH_ENGINE

    def _sync_palette_view_bar(self) -> None:
        """Put the palette-format group in the compression group's place, or the
        reverse — the one swap the capability table cannot express.

        Runs from the tail of :meth:`~...capability_sync.CapabilitySyncMixin.
        _sync_capabilities`, after the kind's own gate on the compression group
        has had its say: a tilemap shows neither, whatever the pixel picker holds.
        """
        allowed = self._can(Capability.COMPRESSION_SCAN)
        swatches = allowed and self._palette_view_active()
        self._compression_action.setVisible(allowed and not swatches)
        self._palette_view_action.setVisible(swatches)

    def _on_palette_view_change(self, *_args) -> None:
        """Push one move of the palette-format picker.

        The pixel-preset switch's shape rather than the compression picker's:
        the format decides how many bytes an entry is, so the bytes are
        reinterpreted — validated once here, then landed by the command's first
        redo. The before state comes off the session, which the apply keeps
        true, since a session is otherwise captured only on the way out.
        """
        self._push_session_pick(
            "palette_view_preset_id",
            self._palette_view_preset_id(),
            PaletteViewFormatCommand,
            f"read palette as {self._palette_view_preset.currentText()}",
            validate=self._palette_view_readable,
        )

    def _palette_view_readable(self, entry: Entry, before: str, after: str) -> bool:
        """Whether the bytes read under color format ``after``; reported if not.

        Validated before anything is committed, and only where the swatch view is
        showing — under any other pixel format the pick is only kept. The config
        reads the session, so the session is moved for the probe and put back
        either way; on failure the picker goes back to ``before`` too.
        """
        if not self._palette_view_active():
            return True
        entry.session.palette_view_preset_id = after
        cfg = self._pixel_config(entry, self._pixel_preset_id())
        try:
            self._pixel_data_for(cfg)
        except PipelineError as exc:
            entry.session.palette_view_preset_id = before
            self._report(exc)
            select_combo_data(self._palette_view_preset, before)
            return False
        entry.session.palette_view_preset_id = before
        return True

    def _push_session_pick(
        self,
        field: str,
        after: str,
        command: type,
        text: str,
        *,
        validate: Callable[[Entry, str, str], bool] | None = None,
    ) -> None:
        """Push one move of a picker whose pick lives in the entry's session.

        The compression preview and the palette-swatch format are one shape: the
        pick is kept in :class:`~celpix.project.workspace.EntrySession` field
        ``field`` and written to the project file, so a move is an undo step
        rather than a glance. The session field is the before state and the
        apply (:meth:`_land_session_pick`) is what keeps it true, so the pair
        never drifts from the combo — a session is otherwise captured only on
        the way out of an entry. With nothing open there is no session to
        carry it, and the move only re-runs the view.

        ``validate(entry, before, after)`` runs between the guard and the push,
        and refuses the step by answering False.
        """
        if self._applying_undo:
            return
        entry = self._workspace.current
        if self._doc is None or entry is None or entry.session is None:
            self._on_view_change()
            return
        before = getattr(entry.session, field)
        if before == after:
            return
        if validate is not None and not validate(entry, before, after):
            return
        self._push_command(command(self, entry, text, before, after))

    def _land_session_pick(self, combo, field: str, preset_id: str) -> Entry | None:  # noqa: ANN001
        """Show ``preset_id`` on ``combo`` and write it to the session's ``field``.

        The apply half of :meth:`_push_session_pick`: the session is written as
        well as the combo so the next gesture's before state is the one actually
        showing. Returns the current entry, for the caller's own follow-up.
        """
        select_combo_data(combo, preset_id)
        entry = self._workspace.current
        if entry is not None and entry.session is not None:
            setattr(entry.session, field, preset_id)
        return entry

    def _apply_palette_view_format(self, preset_id: str) -> None:
        """Land a color format on the picker and the session, and re-read the
        bytes through it where the swatch view is showing.

        Written to the session as well as the combo for the reason
        :meth:`_land_session_pick` gives — and because the session is
        what the pixel config is built from, so the reinterpretation below reads
        the format that was just picked rather than the one before it. Under any
        other pixel format the pick is only kept, for the next time the swatch
        view is chosen.
        """
        entry = self._land_session_pick(
            self._palette_view_preset, "palette_view_preset_id", preset_id
        )
        if self._doc is None or not self._palette_view_active():
            return
        self._apply_pixel_config(
            self._pixel_preset_id(), self._recorded_byte_position()
        )
        if entry is not None and entry.kind is EntryKind.PALETTE:
            # On a palette file the swatch format *is* the palette's format —
            # one fact, two pickers — so the colours the dock shows and every
            # graphic mirrors follow it, and the entry records it as the codec
            # its double-click decodes with.
            self._adopt_palette_entry_format(entry, preset_id)

    def _palette_import_preset_id(self) -> str:
        """The format a palette *file* is read with (the dock's Import as…).

        Separate from :meth:`_palette_preset_id`, which names the format of the
        palette currently on screen whatever its source.
        """
        return self._palette_import_preset.currentData()

    def _on_preview_compression_change(self, *_args) -> None:
        """Push one move of the compression-preview picker.

        The pick is a finding about the region rather than a glance at it, so it
        is an undo step (:meth:`_push_session_pick`).
        """
        self._push_session_pick(
            "preview_compression_id",
            self._compression_id(),
            PreviewCompressionCommand,
            f"preview {self._compression.currentText()}",
        )

    def _apply_preview_compression(self, preset_id: str) -> None:
        """Land a preview scheme on the picker and the session, and re-run the
        overlay."""
        self._land_session_pick(self._compression, "preview_compression_id", preset_id)
        self._on_view_change()

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
        """The first palette index the active palette row addresses.

        A tile stores an index into a window of the palette, so every render of
        decoded pixels needs this to turn those indices into colors. One
        definition, because the row and the window size are separate controls.
        """
        return self._palette_row.value() * self._index_space()

    def _index_space(self, preset_id: str | None = None) -> int:
        """The pixel format's color count - the palette row size.

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
                return pipeline.palette_row_size(preset_id, self._registry)
            except (KeyError, PipelineError):
                pass
        return pipeline.palette_row_size(self._pixel_preset_id(), self._registry)

    def _pixel_values(self) -> int:
        """How many values one pixel can store: ``1 << bpp``, capped at 256.

        Usually the same as :meth:`_index_space`, and different exactly where a
        format's palette rows are wider than its pixels (``palette_bpp``): a 3bpp
        SNES sheet sits in 16-colour rows but still stores only 0-7. Whatever puts
        a value *into* a pixel - the pen, a pasted image - is bounded by this, so
        a colour from the upper half of the row is not silently wrapped.
        """
        doc = self._doc
        preset_id = self._pixel_preset_id()
        if doc is not None and doc.is_tilemap:
            preset_id = doc.pixel_config.interpret_preset_id
        try:
            return min(256, 1 << pipeline.pixel_bpp(preset_id, self._registry))
        except (KeyError, PipelineError):
            return min(256, 1 << self._pixel_bpp())

    def _refresh_tile_size(self) -> None:
        """Show the size of the unit the picture on screen is made of.

        Reads the document's own geometry rather than the preset's, so both a
        bitmap width that re-cut the codec's tiles and a fixed-size codec that
        ignored the width read true. With nothing open there is no geometry to
        report - the 8x8 fallback would be a guess about the next file.

        On a **tilemap** that unit is the map's own cell, not the tile it is built
        from: a screen of 16x16 cells is read, selected and edited a cell at a
        time (:meth:`~...selection.SelectionMixin._selection_unit`), so reporting
        the bound bank's 8x8 would name a thing no gesture in that view acts on.
        The caption follows the number, since "Tile: 16x16" over cells made of
        four 8x8 tiles is the one reading that is wrong either way round. A
        **stamp-resolved chain** is the same story one size further up: its
        gestures act a stamp at a time, and the caption says which reserved noun
        the number measures (``docs/design/terminology.md``).
        """
        doc = self._grid_tilemap()
        stamped = doc is not None and doc.stamp_cells != (1, 1)
        cell = doc is not None
        self._tile_size_label.setText(
            "Stamp:" if stamped else "Cell:" if cell else "Tile:"
        )
        tip = (
            TILE_SIZE_STAMP_TIP
            if stamped
            else TILE_SIZE_CELL_TIP
            if cell
            else TILE_SIZE_TIP
        )
        for widget in (self._tile_size, self._tile_size_label):
            widget.setToolTip(tip)
        if self._doc is None:
            self._tile_size.setText("\u2014")
            return
        tile_w, tile_h = self._pixel_tile_size()
        across, down = self._selection_unit()
        self._tile_size.setText(f"{tile_w * across}\u00d7{tile_h * down}")

    def _pixel_tile_size(self) -> tuple[int, int]:
        # The atomic tile size is the codec's (recorded on the document at load) - not
        # a preset field: geometry is the engine's fixed unit. The arrangement's
        # Block W×H only lays these tiles out in groups; it changes neither the
        # tile nor the unit a pixel-view gesture acts on.
        if self._doc is not None:
            return self._doc.tile_width, self._doc.tile_height
        return 8, 8

    def _adopt_pixel_data(self, px: pipeline.PixelData, cfg: PathwayConfig) -> None:
        """Update the open document's pixel bytes + geometry from a fresh load."""
        assert self._doc is not None
        # A load read the file, so its bytes are the new baseline; a
        # reinterpretation kept the live buffer, edits and all, and the baseline
        # those edits are measured against stays what it was.
        if px.data is not self._doc.pixel_data:
            self._doc.pixel_base_bytes = px.data
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
        document) or a load failure (already reported, combo reverted).
        """
        switch = self._prepare_pixel_switch()
        if switch is None:
            return False
        self._push_pixel_switch(switch)
        return True

    def _prepare_pixel_switch(self) -> PixelConfigCommand | None:
        """Validate the combo's format against the open bytes and build the
        command that lands it, without pushing it; None when it cannot take
        (already reported, combo reverted).

        Split from the push so a caller that wraps the switch in a macro - the
        filter popup - can find out it is doomed *before* opening one: a macro
        cannot be abandoned once begun, and an empty one is still a step.
        """
        entry = self._workspace.current
        if self._doc is None or entry is None or self._applying_undo:
            return None
        if self._pixel_switch_target is None:
            self._pixel_switch_target = self._recorded_byte_position()
        # The doc still holds the outgoing interpretation here (only the combo
        # has moved), so the undo state reads straight off it.
        old_preset = self._doc.pixel_config.interpret_preset_id
        before = (old_preset, self._recorded_byte_position(), self._palette_row.value())
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
            return None
        # The re-anchored row is settled once, here, and carried: re-deriving it
        # on every apply would measure from whatever row and swatch selection
        # are live by then, so an undo would land a row nobody was on.
        row = self._reanchored_palette_row(old_preset, preset_id)
        return PixelConfigCommand(
            self,
            entry,
            f"switch pixel format to {self._pixel_preset.currentText()}",
            before=before,
            after=(preset_id, self._pixel_switch_target, row),
            preloaded=px,
        )

    def _push_pixel_switch(self, switch: PixelConfigCommand) -> None:
        """Push a switch :meth:`_prepare_pixel_switch` validated."""
        self._push_command(switch)
        note = self._partial_tile_note()
        if note:
            self.statusBar().showMessage(f"Preset changed - {note}")

    def _reanchored_palette_row(self, old_preset: str, new_preset: str) -> int:
        """The Palette Row that keeps pointing at the same colors across a
        switch from ``old_preset`` to ``new_preset``.

        The same row index means a different palette base under a new color
        count, so the row is recomputed from the selected color (or the old
        base). Under an unchanged count the row already means the same colors,
        and is kept - re-anchoring there would only snap it onto a selected
        swatch in some other row.
        """
        old_group = self._index_space(old_preset)
        new_group = self._index_space(new_preset)
        row = self._palette_row.value()
        if old_group == new_group:
            return row
        anchor = self._palette_panel.selected_index()
        if anchor is None:
            anchor = row * old_group
        return anchor // new_group

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
        palette_row: int | None = None,
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
        view exactly, with the sub-tile remainder becoming the byte nudge.

        ``palette_row`` lands that row as given - what a command passes, having
        settled it once at push time. Without one the row is re-anchored here
        (:meth:`_reanchored_palette_row`), which only a direct re-application
        such as a plugin refresh wants: it has no earlier state to be true to.
        """
        entry = self._workspace.current
        if self._doc is None or entry is None:
            return False
        if palette_row is None:
            palette_row = self._reanchored_palette_row(
                self._doc.pixel_config.interpret_preset_id, preset_id
            )
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
        self._place_origin(*divmod(byte_position, px.bytes_per_tile))
        # Signals blocked: _refresh_view below re-renders (and re-clamps) once.
        with signals_blocked(self._palette_row):
            self._palette_row.setValue(palette_row)
        self._clear_selection()  # the same tile index covers different bytes now
        self._refresh_view()
        self._retile_bound_maps(entry, preset_id)
        return True

    def _retile_bound_maps(self, entry: Entry, preset_id: str) -> None:
        """Re-read the maps drawing from ``entry`` now its format is ``preset_id``.

        A map holds a decoded *copy* of its bank, read under the format the
        bank's session names (:meth:`~...bindings.BindingsMixin._tile_source_config`)
        - and a session is otherwise only written on the way out of an entry. So
        the format goes onto it here first, then every open map bound to it is
        read again: without both, a switch (or its undo) left those maps drawing
        the old format until something else happened to drop them.
        """
        if entry.session is not None:
            entry.session.pixel_preset_id = preset_id
        # The row reads the session's format too, and on a **composite** it is
        # what files the row under Pixels or under Palettes: a join of colour
        # tables read as swatches *is* one (``docs/design/palette-editing.md``).
        # So the refresh is asked for here, where the format lands, rather than
        # left to whatever happens to repaint the row next.
        self._files_panel.refresh_entry(entry)
        self._reresolve_bound_art(self._maps_drawing_from([entry]))

    def _end_pixel_switch_run(self) -> None:
        """Drop the scratch target when the pixel dropdown loses focus.

        The target only spans one uninterrupted bout of format-cycling; once the
        user moves on, the current view *is* the position, so the next switch
        should re-anchor there rather than resurrect a stale byte offset.
        """
        self._pixel_switch_target = None

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
