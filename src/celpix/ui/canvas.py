"""The tile canvas: draws the rendered image at integer zoom, optional grid
(two levels at a selectable :class:`~celpix.core.document.GridMode` scale, in a
selectable :class:`GridStyle` — see :meth:`Canvas._draw_grid`).

A fixed-size widget the main window drops into a scroll area. It owns no model;
it is handed a ready :class:`QImage` by the render bridge and only scales/paints
it, plus the overlays a gesture needs (marquee, float, pen preview, rearrange
drop target). Selection is expressed in **window slot indices**
(0 .. visible tiles - 1): the canvas reports the pressed and dragged-to slots and
paints whatever *set* of slots it is told to highlight, while the main window owns
which absolute tiles are selected and what shape the two gesture slots describe —
a linear run of slots or a rectangle of cells (`Selection Shape`). Keeping the
canvas shape-agnostic is why the highlight is a slot set rather than a span: a
rectangle of cells is not a contiguous slot run.

In **pixel mode** (:meth:`Canvas.set_edit_mode`) the same widget becomes a paint
surface: the mouse reports **image-pixel** coordinates through the ``pixel_*``
signals instead of tile slots, and it paints two controller-driven overlays — a
floating selection and a pixel-space marquee. It still owns no model; what a
gesture *does* is the pixel-edit controller's job on the window side.

The **rearrange tool** (:meth:`Canvas.set_rearranging`) is armed over tile mode and
never together with pixel mode, but it is still a modal flag the mouse handlers
check ahead of the mode split, joining the pan and the eyedropper. While armed a
left drag reports slots through the ``rearrange_*`` signals, and the canvas paints
the dragged tile floating under the cursor over an outlined drop target — whether
that drop is *allowed* is the controller's call, since it depends on the
rearrangement.
The **right** drag takes over tile selection there: picking the block to carry is
what the left button would otherwise be for, and the tool has no context menu to
displace.

The **stamp tool** (:meth:`Canvas.set_stamping`) sits beside it, the same modal
flag checked ahead of the same split, and is a tilemap's tool as the rearrange
one is a pixel document's. It claims **both** buttons through the ``stamp_*``
signals — left lays a tile into the cell under the cursor and keeps laying it
through a drag, right picks the one already there — so unlike the rearrange tool
it leaves no button for selection. It paints no overlay of its own: what a stamp
changes is the picture itself, and the controller re-renders it.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import Enum

from PySide6.QtCore import QPoint, QPointF, QRect, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPixmap, QRegion
from PySide6.QtWidgets import QWidget

from celpix.core.arrangement import BlockLayout
from celpix.core.document import GridMode
from celpix.ui.tools import EditMode
from celpix.ui.widgets import ZOOM_LEVELS, PanZoomSurface, paint_selection_outline

# The neutral surround/backing behind the rendered pixels: a fixed mid-gray (not a
# theme color) so it never biases how the art's colors read. The scroll viewport
# paints it around the canvas; the canvas itself paints it over any past-end tiles
# in a partial last row, so the two meet seamlessly.
CANVAS_BACKGROUND = QColor(0x80, 0x80, 0x80)


class GridStyle(Enum):
    """How the grid is drawn — the four conventional line styles. ``value`` is
    the stable
    string persisted in app settings.

    Orthogonal to :class:`~celpix.core.document.GridMode`, which says what the
    lattice *counts*: the style is one app-wide look, the mode is the project's.
    """

    NONE = "none"
    POINT = "point"  # a dot at every tile corner, no lines
    DOT = "dot"  # dotted lines
    DASH = "dash"  # dashed lines
    LINE = "line"  # solid lines


# The lattice's two colors, following the convention modern pixel editors settled
# on (analysed in `docs/design-reference/editing-features.md`). They go by *role*,
# which is what makes the grid readable without being told which mode it is in:
# a neutral light grey for the **fine** level — the unit being worked in, pixels
# or tiles — and a saturated blue for the **structural** one above it, the tile,
# block or 8-tile square that unit sits inside.
#
# Hue rather than two opacities of white is what makes a grid line separable from
# the art at a glance — white lines vanish into white pixels, which is most of
# what a light sprite is, while nothing in a retro palette reads as this blue at
# this opacity.
GRID_FINE_COLOR = QColor(0xC8, 0xC8, 0xC8)
GRID_STRUCTURE_COLOR = QColor(0x00, 0x00, 0xFF)
# The opacity a level is drawn at once it is fully faded in, and the stronger one
# the coarse step gets so the two levels stay distinct where both are solid.
GRID_ALPHA = 160
GRID_COARSE_ALPHA = 255
# Below this alpha a level is not worth drawing at all — it would be a smudge on
# the art rather than a lattice. This is what retires a level, in place of a hard
# zoom cutoff: a level *fades* out as its cells shrink and simply stops.
GRID_MIN_ALPHA = 8
# Below this many device pixels a cell has no room for a digit, and a number
# spilling across its neighbours would say less than nothing.
_ROW_LABEL_MIN = 12
# Auto-opacity. A level whose cells are smaller than this many device pixels is
# drawn proportionally fainter, so a dense lattice tints the art instead of
# burying it and thins away smoothly as the view zooms out — no popping.
GRID_FADE_PX = 32
# The pixel level has no cell size to measure (its cell *is* the zoom), so it
# fades on the zoom directly: invisible at 2x, full at this zoom and beyond.
GRID_PIXEL_FULL_ZOOM = 16
# The tile grid's coarse step, in tiles — the conventional 8×8, and what
# Workspace.block_grid replaces with the arrangement's own block size.
GRID_COARSE_TILES = 8

# Outline around the one-pixel paint preview. Translucent white reads against the
# art without hiding the previewed colour, and matches the grid's idiom of tinting
# rather than overwriting.
PREVIEW_OUTLINE_COLOR = QColor(0xFF, 0xFF, 0xFF, 0xC0)

# Where a rearrange drag would land. Opaque and thicker than the grid, because
# this marks a destination rather than tinting the art: it has to be findable
# under the floating tile and over any colors. Red is the refusal (a drop that
# would overlap its own source), the one thing the canvas says no to.
DROP_TARGET_COLOR = QColor(0x40, 0xC0, 0xFF)
DROP_REFUSED_COLOR = QColor(0xFF, 0x50, 0x50)
DROP_TARGET_WIDTH = 2

# Where a fontmap's line ends (:meth:`Canvas.set_line_ends`). Amber for the same
# reason the structural grid is blue: it has to be separable at a glance from
# both levels of the lattice and from the art, and on a fontmap the coarse level
# is already a blue line at every cell boundary — a marker in that colour would
# be a lattice line among lattice lines. Opaque and solid, because unlike the
# grid this is *content*: it says where a string breaks, which nothing else on
# the picture does.
LINE_END_COLOR = QColor(0xFF, 0xA5, 0x28)
LINE_END_WIDTH = 2
# Below this many device pixels across, a cell has no room for a bar that is not
# most of it. The marker thins to a hairline rather than disappearing: the line
# structure is still the thing being read at that size.
LINE_END_MIN_CELL = 8


def _tile_id_text(value: int) -> str:
    """A cell's tile number as the tilemap controls spell one: ``$1c4``.

    One definition because the overlay measures the widest label before drawing
    any, and a fit tested against a different spelling than the one drawn would
    either clip or hide labels that fit.
    """
    return f"${value:x}"


def _tinted(color: QColor, alpha: int) -> QColor:
    """``color`` at ``alpha``, clamped to a drawable opacity."""
    tinted = QColor(color)
    tinted.setAlpha(max(0, min(255, alpha)))
    return tinted


# Line styles per drawing style; POINT/NONE are handled separately.
_GRID_PEN_STYLES = {
    GridStyle.DOT: Qt.PenStyle.DotLine,
    GridStyle.DASH: Qt.PenStyle.DashLine,
    GridStyle.LINE: Qt.PenStyle.SolidLine,
}

# The grid is periodic, so it is painted as a repeating pixmap rather than line by
# line. That is not a micro-optimisation: Qt's raster engine strokes a *translucent*
# one-pixel line at roughly two hundred times the cost per pixel of blitting one, so
# a full repaint of a large window at high zoom would spend longer on the lattice
# than on the art. Blitting a prepared cell composites identically — source-over is
# associative, so blending the crossings into the cell first and the cell onto the
# canvas after lands on the same colours.
#
# The cell spans one coarse block, which is the mapping's true period. Past this
# many pixels a side it is not worth holding, and a cell that large means so few
# lines that stroking them is cheap anyway — which is what the line path below is
# for. The two go together: a cell only exceeds the cap at a high zoom or on a
# big block, and both of those are exactly when the lines are far enough apart to
# be sparse in the viewport the stroking is clipped to. The dense lattice the
# cache exists for sits well inside it at any zoom.
#
# Solid lines and corner dots come out pixel-identical this way. The dotted and
# dashed styles restart their dash phase at each repeat rather than carrying it
# the length of the line: a one- or two-pixel shift in the rhythm, once every
# coarse block, landing on the coarse line that crosses there.
# The phase is texture rather than information, so it is not worth a cell sized to
# the lowest common multiple of the two periods — that is up to nine times the
# area, which would push every ordinary zoom back onto the slow path.
GRID_PATTERN_MAX = 1024


class _GridPattern:
    """One period of the lattice, ready to tile, keyed by what shapes it.

    ``top``/``left`` are the canvas's own first row and column, which the tiling
    cannot supply: the lattice is periodic, so a repeat that paints the coarse
    line at *x = period* paints one at *x = 0* too — and the image's left edge
    carries no grid line. So those two lines are held separately, each the cell's
    edge with the origin line taken out, and laid down after the tiling. They are
    what keeps every line's first pixel where a single stroke would put it.
    """

    __slots__ = ("key", "pixmap", "top", "left")

    def __init__(
        self,
        key: tuple,
        pixmap: QPixmap,
        top: QPixmap | None = None,
        left: QPixmap | None = None,
    ) -> None:
        self.key = key
        self.pixmap = pixmap
        self.top = top
        self.left = left


class Canvas(PanZoomSurface, QWidget):
    # (anchor slot, current slot) — emitted on press and whenever a drag
    # reaches another slot. The anchor stays the pressed slot, so the window
    # can grow/shrink the range live; a plain click emits (slot, slot).
    slots_selected = Signal(int, int)
    # Where a **tile-mode** left press landed, in image pixels — the same press
    # ``slots_selected`` reports as a slot, said again more precisely. What needs
    # it is the sprite object, whose subsprites sit at signed pixel offsets and
    # overlap: one 8x8 square of the sheet routinely holds pieces of three of
    # them, so the slot cannot say which was clicked and only the pixel can
    # (:func:`~celpix.pipeline.pipeline.subsprite_at`). Press only, not the drag:
    # it identifies a thing rather than sweeping a range.
    pixel_picked = Signal(int, int)  # x, y — image pixels
    # ARGB sampled under the cursor while the eyedropper is armed. The rendered
    # image is sampled rather than the palette, so the value is right for any
    # view — indexed through a subpalette, or a direct-color codec with no
    # palette at all. ``object``, not ``int``: Qt's int is 32-bit *signed*, and
    # any ARGB with alpha >= 0x80 overflows it.
    color_picked = Signal(object)
    # Pixel-mode gestures, in **image pixel** coordinates (not tile slots). The
    # controller (PixelEditMixin) reads the button to tell left-draw from a
    # right-click eyedropper. Emitted only in EditMode.PIXEL; tile mode still
    # uses slots_selected.
    pixel_pressed = Signal(int, int, object)  # x, y, Qt.MouseButton
    pixel_moved = Signal(int, int)  # x, y — while the left button is held
    pixel_released = Signal(int, int)  # x, y — the drag's final pixel
    # A left double-click in pixel mode, at the pixel under the cursor. The
    # controller decides what it means (the Select tool takes the whole tile).
    pixel_double_clicked = Signal(int, int)  # x, y
    # Rearrange-tool gestures, in window slots. Emitted only while the tool is
    # armed (:meth:`set_rearranging`) — dragging a tile to a new *display*
    # position is neither a tile selection nor a paint stroke, so it gets its own
    # gesture rather than overloading one of theirs. ``dropped``
    # carries the slot released on; ``cancelled`` means the gesture ended with no
    # destination (Esc, a right press mid-drag, or a release off the image).
    rearrange_started = Signal(int)  # slot pressed
    rearrange_moved = Signal(int)  # slot dragged over
    rearrange_dropped = Signal(int)  # slot released on
    rearrange_cancelled = Signal()
    # Stamp-tool gestures, in window slots. Emitted only while the tool is armed
    # (:meth:`set_stamping`) — laying a tile into a cell is neither a selection
    # nor a paint stroke, so like the rearrange tool it gets its own gesture
    # rather than overloading one of theirs. ``stamp_pressed`` carries the button,
    # because the two do opposite things: left lays the held tile down, right
    # picks the one already there. A left drag keeps emitting ``stamp_moved`` so
    # the tool paints like a pencil, and ``stamp_finished`` closes the stroke —
    # the whole drag is one undoable step, not one per cell crossed.
    stamp_pressed = Signal(int, object)  # slot, Qt.MouseButton
    stamp_moved = Signal(int)  # slot dragged over, left button held
    stamp_finished = Signal()  # the left drag ended
    # A space-drag pan step, in device pixels: how far to shift the view. The
    # window feeds it to the scroll bars, which clamp it so the image can't be
    # dragged off screen. Emitted in either edit mode.
    pan_requested = Signal(int, int)  # dx, dy
    # A wheel-zoom request: a signed zoom step and the cursor's device position on
    # the canvas. The window steps the zoom control and re-anchors the view so the
    # pixel under the cursor stays put. Emitted in either edit mode.
    zoom_requested = Signal(int, object)  # steps, QPointF cursor pos (device)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._image = QImage()
        # Device pixels per image pixel. A float only because of the one
        # reducing level (:data:`~celpix.ui.widgets.ZOOM_LEVELS`); every geometry
        # built from it is rounded to whole device pixels at the point of use, so
        # nothing downstream carries a fraction.
        #
        # Nothing below reads it directly: what reaches the screen is this times
        # the pixel aspect, per axis, and :attr:`~celpix.ui.widgets.
        # PanZoomSurface._zoom_x` is where the two meet. They are equal on a
        # square pixel, which is what leaves an ordinary view drawing as it did.
        self._zoom: float = 4.0
        self._show_grid = False
        self._grid_mode = GridMode.TILE
        self._block_grid = False
        self._grid_style = GridStyle.LINE
        # One subpalette row per visible slot, or None when the labels are
        # off. Set by the render cycle from the same rows the pinned-colour
        # biases are built from, so the number and the recolour can never
        # disagree about which row a tile is on.
        self._palette_rows: list[int | None] | None = None
        # The tile each tilemap cell names, by slot, or None when the labels are
        # off (:meth:`set_tile_ids`). Only a cell's first slot carries a number,
        # so a metatile is labelled once.
        self._tile_ids: list[int | None] | None = None
        # The largest of them, kept with the list because the fit test reads it
        # per repaint and the list is tens of thousands long (:meth:`set_tile_ids`).
        self._widest_tile_id = 0
        # A fontmap's line ends, by the slot each one's cell starts at
        # (:meth:`set_line_ends`). Empty for every other kind of document.
        self._line_ends: frozenset[int] = frozenset()
        self._tile_w = 8
        self._tile_h = 8
        # Arrangement placement (block grouping / order). 1×1 is plain row-major,
        # so every mapping below reduces to the simple form.
        self._block_cols = 1
        self._block_rows = 1
        self._block_order = "row"
        self._selected_slots: frozenset[int] = frozenset()
        self._selection_as_rect = False
        self._drag_anchor: int | None = None
        self._drag_slot: int | None = None  # last emitted, to skip no-op moves
        # Eyedropper: while armed, a press samples a color instead of selecting
        # tiles (see :meth:`set_eyedropper`).
        self._eyedropper = False
        # Pixel-editing mode: while set to PIXEL the mouse paints pixels (via the
        # pixel_* signals) instead of selecting tiles, and the marquee/float
        # overlays below are painted. Tile mode is the default and unchanged.
        self._edit_mode = EditMode.TILE
        self._pixel_dragging = False
        self._last_pixel: tuple[int, int] | None = None  # skip no-op drag emits
        # Right-button eyedropper drag: while a right press is held it keeps
        # sampling the color under the cursor (a re-emitted right press per new
        # pixel), so the picker can be swept rather than clicked pixel by pixel.
        self._sampling = False
        self._last_sample: tuple[int, int] | None = None
        # Overlays the controller drives while editing pixels: a pixel-space
        # rectangle marquee, and a floating selection (a lifted image the user is
        # dragging) shown at a pixel position. Both are drawn over the base image.
        self._marquee: QRect | None = None
        self._float_image: QImage | None = None
        self._float_pos = (0, 0)
        # The picked subsprite's box, in image pixels (:meth:`set_pick_outline`).
        # A pixel-space overlay drawn in **tile** mode, which is the one thing
        # separating it from the marquee above: what it outlines is not on the
        # slot grid the tile selection is made of.
        self._pick_outline: QRect | None = None
        # Rearrange tool: like the eyedropper and the pan it is a modal flag the
        # mouse handlers check before the mode split (it is armed over tile mode,
        # never together with pixel mode). ``_rearrange_slot`` is the last slot
        # reported during a drag, so crossing within one tile emits nothing.
        self._rearranging = False
        self._rearrange_drag = False
        self._rearrange_slot: int | None = None
        # Stamp tool: the same shape as the rearrange flags above — a modal flag
        # checked before the mode split, a drag in progress, and the last slot
        # reported so crossing within one cell emits nothing. Armed over tile
        # mode and never together with either of the other two.
        self._stamping = False
        self._stamp_drag = False
        self._stamp_slot: int | None = None
        # Where a rearrange drag would land, and whether it may: the controller
        # decides (a drop that would overlap its own source is refused), the
        # canvas only draws it.
        self._drop_slots: frozenset[int] = frozenset()
        self._drop_valid = True
        # How many of the image's tile slots hold real data. When the stream ends
        # mid-row the trailing slots of the bottom row are padding, not tiles, so
        # they are painted as background rather than drawn (None = the whole image
        # is data).
        self._filled_tiles: int | None = None
        # Paint preview: the pen color a drawing tool would lay down, shown as a
        # single pixel under the pointer so the target is visible at any zoom (the
        # cursor hotspot alone doesn't say *which* pixel). ``None`` while no
        # drawing tool is armed; the hovered pixel is tracked only while it is set.
        self._preview_color: QColor | None = None
        self._hover_pixel: tuple[int, int] | None = None
        # The tiled lattice cell, rebuilt whenever the style/zoom/tile size that
        # shapes it changes (see :data:`GRID_PATTERN_MAX`).
        self._grid_pattern: _GridPattern | None = None
        # Hover needs move events with no button held.
        self.setMouseTracking(True)
        self._update_size()

    def set_image(self, image: QImage) -> None:
        self._image = image
        self._update_size()

    def set_filled_tiles(self, count: int | None) -> None:
        """Mark how many leading tile slots of the image are real data.

        The rest — a contiguous run at the end of the bottom row, since tiles are
        a linear stream — render as empty canvas so they don't imply data past the
        file's end.
        """
        self._filled_tiles = count
        self.update()

    def set_zoom(self, zoom: float) -> None:
        self._zoom = max(ZOOM_LEVELS[0], float(zoom))
        self._update_size()

    def set_grid(
        self, show: bool, mode: GridMode = GridMode.TILE, block_grid: bool = False
    ) -> None:
        """Set whether a lattice is drawn, at what scale, and on what structure.

        ``mode`` and ``block_grid`` are kept even while ``show`` is off, so the
        switch restores the grid the user last had rather than a default one.
        ``block_grid`` claims the structural (blue) level for the arrangement's
        block size (:meth:`set_arrangement`), in either mode — displacing the
        8×8-tile square the tile grid uses without it, and the tile the pixel
        grid does.
        """
        self._show_grid = show
        self._grid_mode = mode
        self._block_grid = block_grid
        self.update()

    def set_palette_rows(self, rows: list[int | None] | None) -> None:
        """Label each slot with the subpalette row it **names**, or stop
        labelling.

        Indexed by slot, like the selection: slot 0 is the window's first tile. A
        slot that names no row of its own is ``None`` and carries no number — on
        a pixel document the overlay exists to pick the pinned few out of a
        window, so numbering the rest says nothing. That has to be stated by the
        caller and cannot be inferred here: a row is a row, and "pinned to the
        row the view is already on" is a pin like any other
        (``docs/design/palette-editing.md``).

        A tilemap fills it from its **cells**, which name a row each; one number
        per cell, so a metatile's other slots are ``None`` for the reason
        :meth:`set_tile_ids`' are.
        """
        self._palette_rows = rows
        self.update()

    def set_tile_ids(self, ids: list[int | None] | None) -> None:
        """Label each cell with the tile it names, or stop labelling.

        Indexed by **slot** like :meth:`set_palette_rows`, so the two overlays
        address the same space — but a tilemap cell can cover several tiles, and
        one number per cell is the point. The entries for a cell's other slots
        are ``None``, and only its first carries the id.

        ``None`` for the whole list turns the overlay off, and is what every
        document that has no named tiles passes: a pixel tile *is* its position,
        which the position bar already says.

        The **widest** id is measured here rather than at paint time. It decides
        whether the numbers fit in a cell at all (:meth:`_paint_tile_ids`), and
        it is a fact about the list: scanning the whole map for it on every
        repaint would put back the per-slot cost the paint loop just shed — and
        answering it from the exposed band instead would let the overlay come and
        go as the widest number scrolled past.
        """
        self._tile_ids = ids
        self._widest_tile_id = max(
            (value for value in ids or () if value is not None), default=0
        )
        self.update()

    def set_line_ends(self, slots: frozenset[int]) -> None:
        """Mark the cells a fontmap's lines end on, or clear the marks.

        Indexed by the **slot the cell starts at**, like :meth:`set_tile_ids`'
        numbers, and drawn down that cell's trailing edge — where the line stops.

        A fontmap's line structure is content: the cart's own strings end where
        these say, and the canvas otherwise draws a run of glyphs with nothing to
        show for it. That is why this has no switch of its own, unlike the two
        label overlays: it is never on where there is nothing to say, and where
        there is, it is the picture's only account of the thing the entry exists
        to edit.
        """
        self._line_ends = slots
        self.update()

    def set_grid_style(self, style: GridStyle) -> None:
        self._grid_style = style
        self.update()

    def set_tile_size(self, width: int, height: int) -> None:
        self._tile_w = max(1, width)
        self._tile_h = max(1, height)
        self.update()

    def set_arrangement(
        self, block_columns: int, block_rows: int, block_order: str
    ) -> None:
        """Set how linear tile slots map to canvas cells (block grouping).

        Click-mapping, selection, and past-end backgrounding all follow this so a
        blocked view stays interactive; a 1×1 block is the plain row-major default.
        """
        self._block_cols = max(1, block_columns)
        self._block_rows = max(1, block_rows)
        self._block_order = block_order
        self.update()

    def set_selection(
        self, slots: Iterable[int] | None, *, as_rect: bool = False
    ) -> None:
        """Highlight this set of window slots (``None``/empty clears it).

        ``as_rect`` says the slots were picked as a cell *rectangle*, which is
        the only selection outlined as a single box. A linear run stays drawn as
        one box per row even when it happens to fill a rectangle, so the shape on
        screen always tells the user which mode made it.
        """
        self._selected_slots = frozenset(slots or ())
        self._selection_as_rect = as_rect
        self.update()

    def set_eyedropper(self, on: bool) -> None:
        """Arm/disarm color sampling; while armed, clicks don't select tiles.

        Suppressing selection matters: the eyedropper is driven from the color
        editor, and moving the tile selection underneath it would reload the
        palette in Offset mode — changing the very colors being edited.
        """
        if self._eyedropper == on:
            return
        self._eyedropper = on
        if on:
            self._drag_anchor = self._drag_slot = None
        self._apply_cursor()

    def set_edit_mode(self, mode: EditMode) -> None:
        """Switch between tile selection and pixel painting.

        Leaving pixel mode drops any transient drag and the overlays, so a
        half-made stroke or floating selection can't linger under tile editing.
        The cross cursor marks the paint surface; tile mode restores the default.
        """
        if self._edit_mode == mode:
            return
        self._edit_mode = mode
        self._pixel_dragging = False
        self._last_pixel = None
        self._sampling = False
        self._last_sample = None
        if mode is not EditMode.PIXEL:
            self._marquee = None
            self._float_image = None
            self._preview_color = None  # nothing paints in tile mode
            self._hover_pixel = None
        self._apply_cursor()
        self.update()

    def set_rearranging(self, on: bool) -> None:
        """Arm/disarm the rearrange tool.

        Modal over the mouse while armed, like the pan and the eyedropper: a left
        press picks a tile up to move where it is *shown*, so it must not also
        select tiles or paint — selecting moves to the right button instead.
        Toggling abandons any drag in progress — the controller is told, so a
        half-made move can't leave a float behind — and drops the selection drag
        with it, since the button it belongs to changes either way.
        """
        if self._rearranging == on:
            return
        self._rearranging = on
        if not on and self._rearrange_drag:
            self._end_rearrange_drag()  # clears the drag and its hovered slot
            self.rearrange_cancelled.emit()
        self._drag_anchor = self._drag_slot = None
        self._apply_cursor()
        self.update()

    def set_stamping(self, on: bool) -> None:
        """Arm/disarm the stamp tool.

        Modal over the mouse while armed, like the rearrange tool: a left press
        lays a tile into the cell under it and a right press picks the one
        already there, so neither button is left to select tiles with. Toggling
        ends any stroke in progress — the controller is told, so a half-made
        drag is committed rather than stranded — and drops the selection drag
        with it, both buttons having changed meaning.
        """
        if self._stamping == on:
            return
        self._stamping = on
        if not on and self._stamp_drag:
            self._end_stamp_drag()
            self.stamp_finished.emit()
        self._drag_anchor = self._drag_slot = None
        self._apply_cursor()
        self.update()

    def _end_stamp_drag(self) -> None:
        self._stamp_drag = False
        self._stamp_slot = None

    def set_drop_target(
        self, slots: Iterable[int] | None, *, valid: bool = True
    ) -> None:
        """Outline where a rearrange drag would land; ``valid`` says it may.

        Driven entirely by the controller — whether a drop is legal depends on
        the rearrangement, which the canvas has no business knowing.
        """
        self._drop_slots = frozenset(slots or ())
        self._drop_valid = valid
        self.update()

    def _end_rearrange_drag(self) -> None:
        self._rearrange_drag = False
        self._rearrange_slot = None
        self._float_image = None
        self._drop_slots = frozenset()

    def _pan_cursor(self) -> Qt.CursorShape | None:
        """The cursor with no pan armed: cross on the paint/eyedrop surface, the
        move arrows while a tile is carried, the widget's own otherwise."""
        if self._rearranging:
            return Qt.CursorShape.SizeAllCursor
        if self._stamping:
            # The cross the paint surface uses: a stamp is a pencil over cells,
            # and what it needs marked is which cell the pointer is on.
            return Qt.CursorShape.CrossCursor
        if self._edit_mode is EditMode.PIXEL or self._eyedropper:
            return Qt.CursorShape.CrossCursor
        return None

    def _has_content(self) -> bool:
        return not self._image.isNull()

    def set_paint_preview(self, color: QColor | None) -> None:
        """Arm the one-pixel paint preview in ``color`` (``None`` disarms it).

        The controller passes the pen's colour whenever a drawing tool is armed, so
        the canvas need not know which tool is active or how a pen resolves — only
        what colour to show under the pointer.
        """
        if self._preview_color == color:
            return
        self._preview_color = color
        if color is None:
            self._hover_pixel = None
        self.update()

    def leaveEvent(self, event) -> None:  # noqa: ANN001 — Qt override
        # The pointer left the canvas: no pixel is targeted any more.
        super().leaveEvent(event)
        if self._hover_pixel is not None:
            self._hover_pixel = None
            self.update()

    def _track_hover(self, pos: QPointF) -> None:
        """Follow the pixel under the pointer while the preview is armed."""
        pixel = None if self._preview_color is None else self._pixel_at(pos)
        if pixel != self._hover_pixel:
            self._hover_pixel = pixel
            self.update()

    def set_marquee(self, rect: QRect | None) -> None:
        """Show a pixel-space rectangle marquee (``None`` clears it)."""
        self._marquee = rect
        self.update()

    def set_pick_outline(self, rect: QRect | None) -> None:
        """Outline a pixel-space ``rect`` that is not a slot (``None`` clears it).

        The picked subsprite, and only that: what is drawn on a sprite object
        does not sit on the slot grid, so the tile selection's own highlight
        cannot describe it. Drawn in the **structural** colour rather than the
        selection's white, the same distinction the tile source panel's two rings
        make — the white square is where the user clicked, this is what the click
        resolved to.
        """
        self._pick_outline = rect
        self.update()

    def set_float(self, image: QImage | None, x: int = 0, y: int = 0) -> None:
        """Show a floating selection ``image`` at image-pixel ``(x, y)``.

        The lifted pixels the user is dragging, painted (nearest-neighbour, at
        the current zoom) over the base image with a selection outline, so the
        float reads as hovering above the canvas until it is set down.
        """
        self._float_image = None if (image is None or image.isNull()) else image
        self._float_pos = (x, y)
        self.update()

    def _pixel_at(self, pos: QPointF, clamp: bool = False) -> tuple[int, int] | None:
        """The image pixel under ``pos``; None outside (unless ``clamp``).

        ``clamp`` snaps an outside position to the nearest edge pixel — a drag
        that leaves the widget keeps painting to the boundary, like the tile
        selection's own clamp.
        """
        if self._image.isNull():
            return None
        # Floor division, not int(): a position just outside the top-left has to
        # come out negative so the caller can reject it (or clamp it), where
        # truncation would report pixel 0 and paint on the edge column.
        px, py = self._image_pixel(pos)
        if clamp:
            px = max(0, min(px, self._image.width() - 1))
            py = max(0, min(py, self._image.height() - 1))
        elif not (0 <= px < self._image.width() and 0 <= py < self._image.height()):
            return None
        return px, py

    def _color_at(self, pos: QPointF) -> int | None:
        """ARGB of the rendered pixel under ``pos``; None outside the image."""
        pixel = self._pixel_at(pos)
        return None if pixel is None else self._image.pixel(*pixel) & 0xFFFFFFFF

    def _columns(self) -> int:
        # The composed image is exactly columns * tile_w wide, so the count is
        # recoverable without the canvas holding view state.
        return max(1, self._image.width() // self._tile_w)

    def _rows(self) -> int:
        return max(1, self._image.height() // self._tile_h)

    def _layout(self) -> BlockLayout:
        return BlockLayout(
            self._columns(), self._block_cols, self._block_rows, self._block_order
        )

    def _exposed_slots(self, exposed: QRect) -> range:
        """The slots whose **block row** ``exposed`` touches, in slot order.

        What the per-cell overlays loop over instead of the whole window. A slot
        does not sit where its number says under a block arrangement, but the
        mapping is periodic: one block row is
        :attr:`~celpix.core.arrangement.BlockLayout.slots_per_block_row` slots
        wide however its cells are ordered inside it, so a band of rows is still
        a contiguous band of slots. That is what keeps a repaint the cost of what
        is on screen — a metatile map is tens of thousands of slots, and touching
        each one to find out it is off screen took an eighth of a second per
        repaint (:meth:`~celpix.ui.widgets.PanZoomSurface._exposed_rows`).
        """
        layout = self._layout()
        first, stop = self._exposed_rows(
            exposed, self._tile_h * max(1, self._block_rows)
        )
        period = layout.slots_per_block_row
        return range(first * period, stop * period)

    def _slot_at(self, pos: QPointF, clamp: bool = False) -> int | None:
        """The window slot under ``pos``; None when outside the image (or a
        block-grid gap cell that holds no tile).

        ``clamp`` snaps an outside position to the nearest edge slot instead —
        a drag that leaves the widget keeps extending to the boundary.
        """
        pixel = self._pixel_at(pos, clamp)
        if pixel is None:
            return None
        return self._layout().pos_to_slot(
            pixel[0] // self._tile_w, pixel[1] // self._tile_h
        )

    def mousePressEvent(self, event) -> None:  # noqa: ANN001 — Qt override
        # Space-drag panning is modal: while armed a left press grabs the view and
        # neither selects nor paints. Checked first so it wins over every gesture.
        if self._pan_press(event):
            return
        # The color-editor eyedropper (armed from outside) samples a rendered
        # ARGB in either mode and swallows the press — it must reach the canvas
        # even while pixel editing, so it is handled before the mode split.
        if (
            self._eyedropper
            and event.button() == Qt.MouseButton.LeftButton
            and not self._image.isNull()
        ):
            argb = self._color_at(event.position())
            if argb is not None:
                self.color_picked.emit(argb)
            event.accept()
            return
        # The rearrange tool owns the mouse while it is armed, so it sits above
        # the mode split alongside the pan/eyedropper.
        if self._rearranging and not self._image.isNull():
            self._rearrange_press(event)
            super().mousePressEvent(event)
            return
        # The stamp tool owns the mouse the same way, and sits beside it: both
        # are armed over tile mode and neither can be armed with the other.
        if self._stamping and not self._image.isNull():
            self._stamp_press(event)
            super().mousePressEvent(event)
            return
        if self._edit_mode is EditMode.PIXEL:
            self._pixel_press(event)
            super().mousePressEvent(event)
            return
        if event.button() == Qt.MouseButton.RightButton and not self._image.isNull():
            # The context menu acts on the selection, so a right-click outside
            # it moves the selection there first (the usual file-manager rule);
            # inside it, the existing range is kept so a multi-tile selection
            # survives being right-clicked.
            slot = self._slot_at(event.position())
            if slot is not None and slot not in self._selected_slots:
                self.slots_selected.emit(slot, slot)
        if event.button() == Qt.MouseButton.LeftButton and not self._image.isNull():
            slot = self._slot_at(event.position())
            if slot is not None:
                self._drag_anchor = self._drag_slot = slot
                self.slots_selected.emit(slot, slot)
                pixel = self._pixel_at(event.position())
                if pixel is not None:
                    self.pixel_picked.emit(*pixel)
        # Let the default handling run too so ClickFocus keeps focusing us.
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: ANN001 — Qt override
        """Report a left double-click as a pixel, in pixel mode only.

        Qt delivers press → release → *double-click* → release, so a drag is
        already under way by the time this arrives. It is ended here: the
        double-click replaces that gesture, and leaving the drag live would let
        any jitter before the final release resize what the double-click picked.
        """
        if (
            self._edit_mode is EditMode.PIXEL
            and event.button() == Qt.MouseButton.LeftButton
            and not self._pan_active
            and not self._eyedropper
        ):
            pixel = self._pixel_at(event.position())
            if pixel is not None:
                self._pixel_dragging = False
                self._last_pixel = None
                self.pixel_double_clicked.emit(pixel[0], pixel[1])
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: ANN001 — Qt override
        if self._pan_move(event):
            return
        self._track_hover(event.position())
        if self._rearranging:
            self._rearrange_move(event)
            super().mouseMoveEvent(event)
            return
        if self._stamping:
            self._stamp_move(event)
            super().mouseMoveEvent(event)
            return
        if self._edit_mode is EditMode.PIXEL:
            self._pixel_move(event)
            super().mouseMoveEvent(event)
            return
        if (
            self._drag_anchor is not None
            and event.buttons() & Qt.MouseButton.LeftButton
        ):
            slot = self._slot_at(event.position(), clamp=True)
            if slot is not None and slot != self._drag_slot:
                self._drag_slot = slot
                self.slots_selected.emit(self._drag_anchor, slot)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: ANN001 — Qt override
        if self._pan_release(event):
            return
        if self._rearranging:
            self._rearrange_release(event)
            super().mouseReleaseEvent(event)
            return
        if self._stamping:
            self._stamp_release(event)
            super().mouseReleaseEvent(event)
            return
        if self._edit_mode is EditMode.PIXEL:
            self._pixel_release(event)
            super().mouseReleaseEvent(event)
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_anchor = self._drag_slot = None
        super().mouseReleaseEvent(event)

    def _rearrange_press(self, event) -> None:  # noqa: ANN001 — Qt event
        """Pick a tile up (left), or start selecting tiles (right).

        The right button inherits the tile-selection drag while the tool is armed:
        the left one is picking tiles *up*, and a block has to be selected before
        it can be carried as one. It never samples a color and never opens the
        context menu here — the window suppresses that for the same reason.

        Mid-drag it still abandons the move instead, the standard "get me out of
        this drag" escape, since selecting under a tile in the air would leave the
        gesture pinned to a block that is no longer the one selected.
        """
        if event.button() == Qt.MouseButton.RightButton:
            if self._rearrange_drag:
                self._end_rearrange_drag()
                self.rearrange_cancelled.emit()
                self.update()
                return
            slot = self._slot_at(event.position())
            if slot is not None:
                self._drag_anchor = self._drag_slot = slot
                self.slots_selected.emit(slot, slot)
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return
        slot = self._slot_at(event.position())
        if slot is None:
            return
        self._rearrange_drag = True
        self._rearrange_slot = slot
        self.rearrange_started.emit(slot)

    def _rearrange_move(self, event) -> None:  # noqa: ANN001 — Qt event
        buttons = event.buttons()
        if self._drag_anchor is not None and buttons & Qt.MouseButton.RightButton:
            # The borrowed selection drag: same growing range as tile mode's, on
            # the button the tool left free.
            slot = self._slot_at(event.position(), clamp=True)
            if slot is not None and slot != self._drag_slot:
                self._drag_slot = slot
                self.slots_selected.emit(self._drag_anchor, slot)
            return
        if not (self._rearrange_drag and buttons & Qt.MouseButton.LeftButton):
            return
        # Clamped, like the tile-selection drag: sliding off the edge keeps
        # aiming at the boundary tile rather than dropping the gesture.
        slot = self._slot_at(event.position(), clamp=True)
        if slot is not None and slot != self._rearrange_slot:
            self._rearrange_slot = slot
            self.rearrange_moved.emit(slot)

    def _rearrange_release(self, event) -> None:  # noqa: ANN001 — Qt event
        if event.button() == Qt.MouseButton.RightButton:
            self._drag_anchor = self._drag_slot = None
            return
        if event.button() != Qt.MouseButton.LeftButton or not self._rearrange_drag:
            return
        slot = self._slot_at(event.position(), clamp=True)
        self._end_rearrange_drag()
        if slot is None:
            self.rearrange_cancelled.emit()
        else:
            self.rearrange_dropped.emit(slot)
        self.update()

    def _stamp_press(self, event) -> None:  # noqa: ANN001 — Qt event
        """Lay a tile down (left), or pick the one under the cursor (right).

        Both are reported as one signal carrying the button, because they are
        one gesture on one target and splitting them would let the two disagree
        about which cell that is. Only the left one opens a drag: picking is a
        discrete act, and sweeping it would spray the tile source panel with every
        tile crossed — the reading the palette grid's eyedropper already takes.
        """
        slot = self._slot_at(event.position())
        if slot is None:
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self._stamp_drag = True
            self._stamp_slot = slot
        elif event.button() != Qt.MouseButton.RightButton:
            return
        self.stamp_pressed.emit(slot, event.button())

    def _stamp_move(self, event) -> None:  # noqa: ANN001 — Qt event
        if not (self._stamp_drag and event.buttons() & Qt.MouseButton.LeftButton):
            return
        # Clamped, like every other drag here: sliding off the edge keeps aiming
        # at the boundary cell rather than dropping the stroke.
        slot = self._slot_at(event.position(), clamp=True)
        if slot is not None and slot != self._stamp_slot:
            self._stamp_slot = slot
            self.stamp_moved.emit(slot)

    def _stamp_release(self, event) -> None:  # noqa: ANN001 — Qt event
        if event.button() != Qt.MouseButton.LeftButton or not self._stamp_drag:
            return
        self._end_stamp_drag()
        self.stamp_finished.emit()

    def _pixel_press(self, event) -> None:  # noqa: ANN001 — Qt event
        """Begin a pixel gesture: report the pressed pixel and its button.

        A left press starts a drag (the pen/shape/marquee tools track it); a
        right press begins an eyedropper sweep the controller reads as sampling.
        A press outside the image is ignored, as the tile click always was.
        """
        pixel = self._pixel_at(event.position())
        if pixel is None:
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self._pixel_dragging = True
            self._last_pixel = pixel
        elif event.button() == Qt.MouseButton.RightButton:
            self._sampling = True
            self._last_sample = pixel
        self.pixel_pressed.emit(pixel[0], pixel[1], event.button())

    def _pixel_move(self, event) -> None:  # noqa: ANN001 — Qt event
        buttons = event.buttons()
        if self._pixel_dragging and buttons & Qt.MouseButton.LeftButton:
            pixel = self._pixel_at(event.position(), clamp=True)
            if pixel is not None and pixel != self._last_pixel:
                self._last_pixel = pixel
                self.pixel_moved.emit(pixel[0], pixel[1])
        elif self._sampling and buttons & Qt.MouseButton.RightButton:
            # Continuous eyedropper: re-emit a right press for each new pixel the
            # sweep reaches so the controller samples it. Not clamped — a sweep
            # off the image samples nothing rather than the edge color.
            pixel = self._pixel_at(event.position())
            if pixel is not None and pixel != self._last_sample:
                self._last_sample = pixel
                self.pixel_pressed.emit(pixel[0], pixel[1], Qt.MouseButton.RightButton)

    def _pixel_release(self, event) -> None:  # noqa: ANN001 — Qt event
        if event.button() == Qt.MouseButton.RightButton:
            self._sampling = False
            self._last_sample = None
            return
        if event.button() != Qt.MouseButton.LeftButton or not self._pixel_dragging:
            return
        self._pixel_dragging = False
        pixel = self._pixel_at(event.position(), clamp=True) or self._last_pixel
        self._last_pixel = None
        if pixel is not None:
            self.pixel_released.emit(pixel[0], pixel[1])

    def _update_size(self) -> None:
        self.setFixedSize(*self._scaled_size(self._image.width(), self._image.height()))
        self.update()

    def _slot_rect(self, tile_x: int, tile_y: int) -> QRect:
        """The device-coord rect of one canvas slot."""
        return self._cell_rect(tile_x, tile_y, 1, 1)

    def _cell_rect(self, tile_x: int, tile_y: int, across: int, down: int) -> QRect:
        """The device-coord rect of ``across`` x ``down`` slots at ``tile_x/y``.

        The one place a run of slots becomes a rectangle, so a wide run is scaled
        from its own far edge rather than from a scaled single cell multiplied up
        — under a fractional aspect the second drifts a device pixel every few
        cells, and the labels, the backing and the line-end marks would each drift
        differently.
        """
        return self._scaled_rect(
            tile_x * self._tile_w,
            tile_y * self._tile_h,
            across * self._tile_w,
            down * self._tile_h,
        )

    def _background_region(self) -> QRegion | None:
        """Device-coord region of cells that are backing, not data, or None.

        Cells past the filled tile count (a partial last window) — and, under a
        block layout, any block-grid gap cell that holds no tile — are painted as
        the neutral surround so nothing implies a tile is there. Plain row-major
        keeps the fast path: the padding is one contiguous tail of the last data
        row (tiles are a linear stream).
        """
        if self._filled_tiles is None or self._image.isNull():
            return None
        layout = self._layout()
        cols, rows = self._columns(), self._rows()
        if layout.is_plain:
            remainder = self._filled_tiles % cols
            row = self._filled_tiles // cols
            if remainder == 0 or row >= rows:
                return None
            return QRegion(self._cell_rect(remainder, row, cols - remainder, 1))
        # Backing cells are unioned a horizontal *run* at a time rather than one
        # by one: a region union costs the same for a wide rect as a narrow one,
        # and a window can hold thousands of cells.
        region = QRegion()
        filled = self._filled_tiles
        for tile_y in range(rows):
            start = None
            for tile_x in range(cols + 1):  # one past the end flushes the last run
                slot = None if tile_x == cols else layout.pos_to_slot(tile_x, tile_y)
                backing = tile_x < cols and (slot is None or slot >= filled)
                if backing:
                    if start is None:
                        start = tile_x
                    continue
                if start is not None:
                    run = self._cell_rect(start, tile_y, tile_x - start, 1)
                    region = region.united(QRegion(run))
                    start = None
        return region if not region.isEmpty() else None

    def paintEvent(self, event) -> None:  # noqa: ANN001 — Qt override
        if self._image.isNull():
            return
        # Everything below is confined to the exposed rect. Qt clips to it anyway,
        # but the overlays are drawn per line / per cell — issuing the ones that
        # fall outside costs as much as the ones that don't, and a scrolled view
        # exposes a sliver of a canvas that can be thousands of pixels across.
        exposed = event.rect()
        painter = QPainter(self)
        # Nearest-neighbour: pixels must stay crisp when magnified.
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
        zx, zy = self._zoom_x, self._zoom_y
        # Past-end slots in a partial last row are backing, not data: fill them
        # with the neutral color and clip them out of the image/grid draw so
        # nothing (not even a grid line) suggests a tile is there. Clip is set
        # under the identity transform, so it stays in device coordinates while
        # the scale below only affects what's drawn.
        # Laid down under everything, so a transparent pixel has something
        # defined behind it rather than whatever the widget was last painted
        # with. The image is opaque unless Transparent 0 is on, and then the
        # backdrop showing through *is* the point — the same neutral colour a
        # past-end slot gets, which is already this canvas's way of saying
        # nothing is here.
        painter.fillRect(exposed, CANVAS_BACKGROUND)
        background = self._background_region()
        if background is not None:
            painter.setClipRegion(QRegion(self.rect()).subtracted(background))
        painter.scale(zx, zy)
        painter.drawImage(0, 0, self._image)

        painter.resetTransform()
        # The grid is a viewing aid, not part of the art: drawn in device pixels
        # (after resetTransform) so its lines stay 1px crisp at any zoom, and only
        # once a tile is at least 2px *each way* so it never swamps the pixels
        # themselves. The narrower axis decides, since it is the one that would be
        # swamped — under a 1:2 pixel at the reducing level the wide axis is
        # comfortably clear of it and the tall one is not.
        if (
            self._show_grid
            and self._grid_style is not GridStyle.NONE
            and min(zx, zy) >= 2
        ):
            self._draw_grid(painter, zx, zy, exposed)
        # Over the lattice and under the labels: it is read as structure, and a
        # cell that carries both a number and a line end must not hide either.
        self._paint_line_ends(painter, exposed)
        self._paint_palette_rows(painter, exposed)
        self._paint_tile_ids(painter, exposed)
        self._paint_selection(painter, exposed)
        self._paint_overlays(painter, exposed)
        painter.end()

    def _paint_overlays(self, painter: QPainter, exposed: QRect) -> None:
        """Draw the float, and whatever else the armed interaction wants.

        The float goes down first (a lifted image the user is dragging), then its
        outline. Both pixel editing and the rearrange tool put something in the
        air, so the float is painted for either; the marquee and pen preview
        belong to pixel editing alone, and the drop target to the rearrange.

        Gated on the mode rather than trusting the overlays to be ``None`` there:
        undo steps through pixel-mode selections wherever the history is walked,
        tile mode included, and it restores them by driving these same setters. A
        pixel rectangle drawn over the tile view is then a stray outline the user
        has no way to explain or dismiss.

        The **pick outline** is the exception, and ungated for the reason the
        others are gated: nothing but the window sets it, it is cleared the moment
        what is on screen stops being a sprite object, and it says which subsprite
        is selected — which is as true while its pixels are being edited as while
        they are being looked at.
        """
        if self._pick_outline is not None:
            paint_selection_outline(
                painter,
                self._scaled_rect(*self._pick_outline.getRect()),
                color=GRID_STRUCTURE_COLOR,
            )
        pixel_mode = self._edit_mode is EditMode.PIXEL
        if not (pixel_mode or self._rearranging):
            return
        if self._rearranging:
            self._paint_drop_target(painter, exposed)
        if self._float_image is not None:
            fx, fy = self._float_pos
            rect = self._scaled_rect(
                fx, fy, self._float_image.width(), self._float_image.height()
            )
            painter.drawImage(rect, self._float_image)
            paint_selection_outline(painter, rect)
        if not pixel_mode:
            return
        if self._marquee is not None and not self._marquee.isNull():
            paint_selection_outline(
                painter, self._scaled_rect(*self._marquee.getRect())
            )
        self._paint_pen_preview(painter)

    def _paint_drop_target(self, painter: QPainter, exposed: QRect) -> None:
        """Outline the cells a rearrange drag would land on.

        Under the float, so the tile being carried stays readable over its
        destination. The invalid color is the only place the canvas says *no* to
        a gesture, and it has to read as a refusal at a glance — the swap would
        otherwise look like it simply didn't take.
        """
        if not self._drop_slots:
            return
        layout = self._layout()
        color = DROP_TARGET_COLOR if self._drop_valid else DROP_REFUSED_COLOR
        pen = QPen(color)
        pen.setWidth(DROP_TARGET_WIDTH)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        cols, rows = self._columns(), self._rows()
        for slot in self._drop_slots:
            tile_x, tile_y = layout.slot_to_pos(slot)
            if 0 <= tile_x < cols and 0 <= tile_y < rows:
                # A pen straddles the path, so inset by half its width to keep
                # the whole outline inside the cell it marks.
                inset = DROP_TARGET_WIDTH // 2
                rect = self._slot_rect(tile_x, tile_y).adjusted(
                    inset, inset, -inset - 1, -inset - 1
                )
                if rect.intersects(exposed):
                    painter.drawRect(rect)

    def _paint_pen_preview(self, painter: QPainter) -> None:
        """Tint the pixel the pen is aimed at, in the colour it would write.

        Drawn last so it sits above the art and the selection overlays, and while
        panning it is suppressed — the hand is moving the view, not painting. The
        pen colour can be indistinguishable from what is already there, so a thin
        contrasting outline keeps the target visible either way.
        """
        if self._preview_color is None or self._hover_pixel is None or self._panning:
            return
        x, y = self._hover_pixel
        rect = self._scaled_rect(x, y, 1, 1)
        # At the reducing level a pixel is half a device pixel; the preview still
        # has to mark *something*, so neither side shrinks below one.
        rect.setWidth(max(1, rect.width()))
        rect.setHeight(max(1, rect.height()))
        painter.fillRect(rect, self._preview_color)
        pen = QPen(PREVIEW_OUTLINE_COLOR)
        pen.setWidth(1)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        # adjusted(): a 1px pen straddles the path, so inset to keep it inside.
        painter.drawRect(rect.adjusted(0, 0, -1, -1))

    def _grid_levels(
        self, zx: float, zy: float
    ) -> list[tuple[tuple[int, int], QColor]]:
        """The lattice at this scale: each level's step, in **image pixels**, and
        the color to stroke it in — fine level first, empty when nothing is drawn.

        What each level counts is the mode's whole job
        (:class:`~celpix.core.document.GridMode`), and the two questions are
        independent. The **fine** (grey) level is the unit being worked in — a
        pixel in pixel mode, a tile in tile mode. The **structural** (blue) level
        is what that unit sits inside: the arrangement's **block** whenever
        ``block_grid`` is on, and otherwise the step that reads as structure
        without one — the tile in pixel mode, the 8-tile square in tile mode.
        Everything below works from this list alone, so neither the drawing nor
        the cached cell has to know which mode produced it.

        Each level's opacity follows its cell size (:data:`GRID_FADE_PX`), and a
        level faded past :data:`GRID_MIN_ALPHA` is dropped rather than drawn as a
        smudge — which is how the pixel level bows out as the view zooms away
        from it, leaving its coarse level holding the lattice alone. Whether the
        grid is shown at all is the caller's question, not this one's.
        """
        tile = (self._tile_w, self._tile_h)
        if self._grid_mode is GridMode.PIXEL:
            fine = (1, 1)
            # On the zoom rather than the cell size, since for a one-pixel cell
            # they are the same number and the zoom curve is the gentler of the
            # two — the pixel level is the one being zoomed *in* to see. The
            # narrower axis, for the reason the grid's own gate takes it: that is
            # the direction the lattice crowds the art in.
            zoom = min(zx, zy)
            fine_alpha = int(GRID_ALPHA * (zoom - 2) // (GRID_PIXEL_FULL_ZOOM - 2))
            unblocked = tile
        else:
            fine = tile
            fine_alpha = self._faded(fine, zx, zy, GRID_ALPHA)
            unblocked = (tile[0] * GRID_COARSE_TILES, tile[1] * GRID_COARSE_TILES)
        # A block grid on the default 1×1 arrangement lands the structural step on
        # every tile — right, if degenerate: there every tile *is* a block.
        coarse = (
            (tile[0] * self._block_cols, tile[1] * self._block_rows)
            if self._block_grid
            else unblocked
        )
        levels = [
            (fine, _tinted(GRID_FINE_COLOR, min(GRID_ALPHA, fine_alpha))),
            (
                coarse,
                _tinted(
                    GRID_STRUCTURE_COLOR, self._faded(coarse, zx, zy, GRID_COARSE_ALPHA)
                ),
            ),
        ]
        return [
            (step, color) for step, color in levels if color.alpha() > GRID_MIN_ALPHA
        ]

    @staticmethod
    def _faded(step: tuple[int, int], zx: float, zy: float, full: int) -> int:
        """``full`` opacity, scaled down while ``step``'s cell is small on screen.

        The mean of the cell's two sides *as drawn*, which is what makes a
        non-square pixel fade on what is actually on screen rather than on what
        the image measures: a 16x8 tile under a 1:2 pixel is a 16x16 square there,
        and fading it as though it were half as tall would drop the tile lattice
        off a view it comfortably fits.
        """
        cell = (step[0] * zx + step[1] * zy) / 2
        return min(full, int(full * cell / GRID_FADE_PX))

    def _draw_grid(
        self, painter: QPainter, zx: float, zy: float, exposed: QRect
    ) -> None:
        """Draw the two-level grid in the current style (device coords).

        POINT dots the finest level's corners in the fine color; the line
        styles draw the fine grid (grey) with the coarse one (white) laid over
        it, so the bigger boundaries stand out from the lattice between them.

        Both are one repeating cell, so the whole lattice is a tiled blit of that
        cell (:data:`GRID_PATTERN_MAX`) — falling back to stroking the lines when
        the cell is too big to be worth holding, which is also when there are few
        enough of them for it not to matter.
        """
        levels = self._grid_levels(zx, zy)
        if not levels:
            return
        pattern = self._grid_pattern_for(zx, zy)
        if pattern is not None:
            self._tile_grid(painter, pattern, exposed)
            return
        img_w, img_h = self._image.width(), self._image.height()
        # Clamp the lines to the exposed band and skip the ones outside it: the
        # lattice spans the whole canvas, which is mostly off screen.
        top, bottom = exposed.top(), exposed.bottom()
        left, right = exposed.left(), exposed.right()
        if self._grid_style is GridStyle.POINT:
            step_x, step_y = levels[0][0]
            # Always the fine color, at full strength: dots mark one level only,
            # so there is no second one for the structural color to distinguish
            # them from — and one pixel of a faded color is nothing at all.
            painter.setPen(_tinted(GRID_FINE_COLOR, GRID_COARSE_ALPHA))
            for gx in range(step_x, img_w, step_x):
                at_x = round(gx * zx)
                if not left <= at_x <= right:
                    continue
                for gy in range(step_y, img_h, step_y):
                    at_y = round(gy * zy)
                    if top <= at_y <= bottom:
                        painter.drawPoint(at_x, at_y)
            return
        pen_style = _GRID_PEN_STYLES[self._grid_style]
        # Fine first, then coarse over it: shared boundaries read as coarse.
        for (step_x, step_y), color in levels:
            pen = QPen(color)
            pen.setStyle(pen_style)
            painter.setPen(pen)
            for gx in range(step_x, img_w, step_x):
                at_x = round(gx * zx)
                if left <= at_x <= right:
                    painter.drawLine(at_x, top, at_x, bottom)
            for gy in range(step_y, img_h, step_y):
                at_y = round(gy * zy)
                if top <= at_y <= bottom:
                    painter.drawLine(left, at_y, right, at_y)

    def _tile_grid(
        self, painter: QPainter, pattern: _GridPattern, exposed: QRect
    ) -> None:
        """Repeat the lattice cell over the exposed area, anchored on the canvas.

        The tiled area starts one pixel in from the top-left, since the cell's own
        origin is a coarse boundary and the image's border is not; the two edge
        strips put back the line pixels that leaves out. Every offset is taken
        modulo the period against the *canvas* origin, so what lands where does
        not depend on which band of the canvas happens to be exposed.
        """
        cell = pattern.pixmap
        width, height = cell.width(), cell.height()
        target = exposed.intersected(
            QRect(1, 1, max(0, self.width() - 1), max(0, self.height() - 1))
        )
        if not target.isEmpty():
            painter.drawTiledPixmap(
                target, cell, QPoint(target.x() % width, target.y() % height)
            )
        if pattern.top is not None and exposed.top() == 0 and exposed.width() > 1:
            strip = QRect(max(1, exposed.left()), 0, 0, 1)
            strip.setRight(exposed.right())
            painter.drawTiledPixmap(strip, pattern.top, QPoint(strip.x() % width, 0))
        if pattern.left is not None and exposed.left() == 0 and exposed.height() > 1:
            strip = QRect(0, max(1, exposed.top()), 1, 0)
            strip.setBottom(exposed.bottom())
            painter.drawTiledPixmap(strip, pattern.left, QPoint(0, strip.y() % height))

    def _grid_pattern_for(self, zx: float, zy: float) -> _GridPattern | None:
        """The cached lattice cell for the current style/scale/mode/steps.

        ``None`` when one period is larger than :data:`GRID_PATTERN_MAX` a side,
        which sends :meth:`_draw_grid` down the line-stroking path instead — the
        block grid on a tall block is the usual way there.

        And ``None`` for a **fractional** scale, which only a non-square pixel
        whose sides are not multiples of each other produces (8:7 and its like).
        A repeating cell can only express a lattice with a whole-device-pixel
        period; there the lines land on an uneven grid, one rounded position at a
        time, and there is no cell to repeat. The stroking path draws exactly that
        and is what the fall-through reaches.
        """
        levels = self._grid_levels(zx, zy)
        if not levels:
            return None
        if zx != int(zx) or zy != int(zy):
            return None
        zx, zy = int(zx), int(zy)
        style = self._grid_style
        # POINT's period is the level it dots — it marks corners, with no second
        # level; the line styles repeat over one coarse cell.
        period = levels[0][0] if style is GridStyle.POINT else levels[-1][0]
        width, height = period[0] * zx, period[1] * zy
        if width > GRID_PATTERN_MAX or height > GRID_PATTERN_MAX:
            return None
        # The colors are part of what shapes the cell, not just the steps: both
        # fade with the zoom, so the same lattice at two zooms is two cells.
        key = (style, zx, zy, tuple((step, color.rgba()) for step, color in levels))
        cached = self._grid_pattern
        if cached is not None and cached.key == key:
            return cached
        if style is GridStyle.POINT:
            # Corner dots never fall on the image's first row or column, so this
            # style needs no edge strips.
            pattern = _GridPattern(
                key, self._grid_square(zx, zy, width, height, levels)
            )
        else:
            pattern = _GridPattern(
                key,
                self._grid_square(zx, zy, width, height, levels),
                self._grid_square(zx, zy, width, height, levels, only="vertical").copy(
                    0, 0, width, 1
                ),
                self._grid_square(
                    zx, zy, width, height, levels, only="horizontal"
                ).copy(0, 0, 1, height),
            )
        self._grid_pattern = pattern
        return pattern

    def _grid_square(
        self,
        zx: int,
        zy: int,
        width: int,
        height: int,
        levels: list[tuple[tuple[int, int], QColor]],
        only: str | None = None,
    ) -> QPixmap:
        """Draw one period of the lattice into a transparent pixmap.

        ``only`` keeps just the vertical or just the horizontal lines, which is
        what the two edge strips are cut from: canvas row 0 shows the verticals
        crossing it and nothing else, and column 0 the horizontals.
        """
        pixmap = QPixmap(width, height)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        if self._grid_style is GridStyle.POINT:
            painter.setPen(_tinted(GRID_FINE_COLOR, GRID_COARSE_ALPHA))
            painter.drawPoint(0, 0)
            painter.end()
            return pixmap
        pen_style = _GRID_PEN_STYLES[self._grid_style]
        # The square's own origin is the coarse boundary; the steps inside it are
        # the fine ones. Fine first, so a crossing reads coarse — and from 0 rather
        # than the first step inside, because a coarse boundary is a fine boundary
        # too and the fine line under it is what gives the coarse line its
        # brightness. The last level *is* the square, so it needs only its origin
        # line.
        drawn = [
            (color, (range(0, width, step[0] * zx), range(0, height, step[1] * zy)))
            for step, color in levels[:-1]
        ]
        drawn.append((levels[-1][1], (range(1), range(1))))
        for color, positions in drawn:
            pen = QPen(color)
            pen.setStyle(pen_style)
            painter.setPen(pen)
            if only != "horizontal":
                for gx in positions[0]:
                    painter.drawLine(gx, 0, gx, height)
            if only != "vertical":
                for gy in positions[1]:
                    painter.drawLine(0, gy, width, gy)
        painter.end()
        return pixmap

    def _paint_palette_rows(self, painter: QPainter, exposed: QRect) -> None:
        """Number each labelled tile with its subpalette row, bottom-left corner.

        In the grid's own colour, because it is the same kind of thing: an
        annotation laid over the art rather than part of it, and one the eye
        should be able to ignore. Skipped entirely below a zoom where a digit
        would not fit inside a tile — a number spilling across its neighbours
        would say less than nothing.

        The **bottom** left, because the two overlays can now be on together: a
        tilemap's cells name both a tile and a row, so the id keeps the top-left
        corner it has always had and the row sits under it. Per *tile* rather
        than per cell (:meth:`_paint_tile_ids` widens to the cell): the labels
        arrive per slot on a pixel document, where a block arrangement would
        otherwise stack a block's worth of numbers in one place.
        """
        rows = self._palette_rows
        if not rows:
            return
        cell_w = self._tile_w * self._zoom_x
        cell_h = self._tile_h * self._zoom_y
        if cell_w < _ROW_LABEL_MIN or cell_h < _ROW_LABEL_MIN:
            return
        layout = self._layout()
        cols, canvas_rows = self._columns(), self._rows()
        font = painter.font()
        font.setPixelSize(max(_ROW_LABEL_MIN - 2, min(int(cell_h // 3), 14)))
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(_tinted(GRID_FINE_COLOR, GRID_COARSE_ALPHA))
        band = self._exposed_slots(exposed)
        # A slot band is whole rows of the window, which can be hundreds of tiles
        # across where the exposed strip is twenty: the columns bound what is
        # worth turning into a rectangle. Both are read off the same exposed
        # rectangle the drawing is clipped to, so what they take out is only ever
        # what would have been clipped away.
        left, right = self._exposed_columns(exposed, self._tile_w)
        right = min(cols, right)
        for slot in range(band.start, min(band.stop, len(rows))):
            row = rows[slot]
            if row is None:
                continue  # this slot names no row; row 0 named is still a row
            tile_x, tile_y = layout.slot_to_pos(slot)
            if not (left <= tile_x < right and 0 <= tile_y < canvas_rows):
                continue
            rect = self._slot_rect(tile_x, tile_y)
            if not exposed.intersects(rect):
                continue
            painter.drawText(
                rect.adjusted(1, 0, 0, 0),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom,
                str(row),
            )

    def _paint_tile_ids(self, painter: QPainter, exposed: QRect) -> None:
        """Number each tilemap cell with the tile it names, top-left corner.

        The palette-row overlay's twin, in the same colour and the corner above
        it: a tilemap cell names a tile *and* a row, so the two can be on
        together and the id keeps the top-left it has always had.

        Two differences the number itself forces. It is drawn into the whole
        **cell** rather than one tile, because a metatile's label would not fit
        inside its top-left eighth; and the fit is tested against the widest label
        actually present rather than a fixed minimum, since ``$3FF`` needs four
        times the room a palette row's single digit does. That widest label is
        measured when the list arrives (:meth:`set_tile_ids`) and the fit tested
        once here — both off the per-cell path, which a screen of thousands of
        cells would otherwise show.

        Hex with the ``$`` the Base tile spin uses: a bare ``10`` over a tile
        cannot say whether it means sixteen, and this is the number you carry to
        that spin, a hex editor or a bank listing.
        """
        ids = self._tile_ids
        if not ids:
            return
        across, down = max(1, self._block_cols), max(1, self._block_rows)
        cell_w = self._tile_w * self._zoom_x * across
        cell_h = self._tile_h * self._zoom_y * down
        if cell_h < _ROW_LABEL_MIN:
            return
        font = painter.font()
        font.setPixelSize(max(_ROW_LABEL_MIN - 2, min(int(cell_h // 3), 14)))
        font.setBold(True)
        painter.setFont(font)
        metrics = painter.fontMetrics()
        if metrics.horizontalAdvance(_tile_id_text(self._widest_tile_id)) + 2 > cell_w:
            return
        layout = self._layout()
        cols, canvas_rows = self._columns(), self._rows()
        painter.setPen(_tinted(GRID_FINE_COLOR, GRID_COARSE_ALPHA))
        band = self._exposed_slots(exposed)
        # The columns of the band worth drawing — :meth:`_paint_palette_rows`,
        # less one cell's width, since a cell's number is written from its left
        # edge and the cell before it may start outside the strip.
        left, right = self._exposed_columns(exposed, self._tile_w)
        left, right = max(0, left - across), min(cols, right)
        for slot in range(band.start, min(band.stop, len(ids))):
            value = ids[slot]
            if value is None:
                continue
            tile_x, tile_y = layout.slot_to_pos(slot)
            if not (left <= tile_x < right and 0 <= tile_y < canvas_rows):
                continue
            rect = self._cell_rect(tile_x, tile_y, across, down)
            if not exposed.intersects(rect):
                continue
            painter.drawText(
                rect.adjusted(1, 0, 0, 0),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                _tile_id_text(value),
            )

    def _paint_line_ends(self, painter: QPainter, exposed: QRect) -> None:
        """Rule the trailing edge of every cell a fontmap's line ends on.

        Inside the cell rather than on the boundary between it and the next, for
        two reasons that point the same way. A line that ends in the **last
        column** — which is every line of a fixed-width message box — would
        otherwise fall on the canvas's own edge and be half of it or none of it;
        and the mark belongs to *that* cell, since it is that character carrying
        the terminator bit or being the break code. Drawn inside, it reads as the
        stop it is and is always fully on screen.

        Filled rather than stroked: a rectangle blits, where a translucent
        one-pixel line is the slow path Qt's raster engine takes
        (:data:`GRID_PATTERN_MAX`'s comment) — and a text region is thousands of
        cells with a mark on every eighteenth.
        """
        if not self._line_ends:
            return
        across, down = max(1, self._block_cols), max(1, self._block_rows)
        cell_w = self._tile_w * self._zoom_x * across
        width = LINE_END_WIDTH if cell_w >= LINE_END_MIN_CELL else 1
        layout = self._layout()
        cols, rows = self._columns(), self._rows()
        # Walked as the *set* rather than as the band, this one being sparse — a
        # mark every line rather than one per cell. The band is what it is tested
        # against, which is a lookup where a rectangle per mark was not.
        band = self._exposed_slots(exposed)
        for slot in self._line_ends:
            if slot not in band:
                continue
            tile_x, tile_y = layout.slot_to_pos(slot)
            if not (0 <= tile_x < cols and 0 <= tile_y < rows):
                continue
            cell = self._cell_rect(tile_x, tile_y, across, down)
            bar = QRect(cell.right() + 1 - width, cell.y(), width, cell.height())
            if not exposed.intersects(bar):
                continue
            painter.fillRect(bar, LINE_END_COLOR)

    def _paint_selection(self, painter: QPainter, exposed: QRect) -> None:
        if not self._selected_slots:
            return
        layout = self._layout()
        cols, rows = self._columns(), self._rows()
        # Map each selected slot to its cell. A rectangle selection whose cells
        # fill their bounding box is outlined once, so it reads as the one shape
        # it is; everything else falls back to per-row contiguous runs - a linear
        # run is a run through storage, and drawing it as a box would claim a
        # rectangle the user never picked.
        cells_by_row: dict[int, list[int]] = {}
        for slot in self._selected_slots:
            tile_x, tile_y = layout.slot_to_pos(slot)
            if 0 <= tile_x < cols and 0 <= tile_y < rows:
                cells_by_row.setdefault(tile_y, []).append(tile_x)
        solid = self._solid_rect(cells_by_row) if self._selection_as_rect else None
        if solid is not None:
            x0, y0, width, height = solid
            rect = self._cell_rect(x0, y0, width, height)
            if rect.intersects(exposed):
                paint_selection_outline(painter, rect)
            return
        for tile_y, xs in cells_by_row.items():
            xs.sort()
            run_start = prev = xs[0]
            for x in xs[1:] + [-1]:  # -1 sentinel flushes the final run
                if x == prev + 1:
                    prev = x
                    continue
                rect = self._cell_rect(run_start, tile_y, prev - run_start + 1, 1)
                if rect.intersects(exposed):
                    paint_selection_outline(painter, rect)
                run_start = prev = x

    @staticmethod
    def _solid_rect(
        cells_by_row: dict[int, list[int]],
    ) -> tuple[int, int, int, int] | None:
        """``(x, y, columns, rows)`` when the cells fill their bounding box.

        The visible test for "this selection is one rectangle": every row present,
        each holding exactly the same contiguous span. ``None`` for a ragged set,
        which has no single box to draw — a rectangle scrolled half out of view
        included, so the visible part still outlines row by row.
        """
        if not cells_by_row:
            return None
        rows = sorted(cells_by_row)
        if rows[-1] - rows[0] + 1 != len(rows):
            return None
        span = None
        for row in rows:
            xs = sorted(cells_by_row[row])
            if xs[-1] - xs[0] + 1 != len(xs):
                return None  # a gap in this row
            if span is None:
                span = (xs[0], xs[-1])
            elif span != (xs[0], xs[-1]):
                return None  # rows don't line up
        return span[0], rows[0], span[1] - span[0] + 1, len(rows)

    def sizeHint(self):  # noqa: ANN201 — Qt override
        return self.size()
