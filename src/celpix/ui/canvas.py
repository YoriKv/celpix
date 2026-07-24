"""The tile canvas: draws the rendered image at integer zoom, optional tile grid
(a two-level grid in a selectable :class:`GridStyle` — see :meth:`Canvas._draw_grid`).

Deliberately minimal for the MVP — a fixed-size widget the main window drops into
a scroll area. It owns no model; it is handed a ready :class:`QImage` by the render
bridge and only scales/paints it. Selection is expressed in **window slot indices**
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
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import Enum

from PySide6.QtCore import QPointF, QRect, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QRegion
from PySide6.QtWidgets import QWidget

from celpix.core.arrangement import BlockLayout
from celpix.ui.tools import EditMode
from celpix.ui.widgets import paint_selection_outline

# The neutral surround/backing behind the rendered pixels: a fixed mid-gray (not a
# theme color) so it never biases how the art's colors read. The scroll viewport
# paints it around the canvas; the canvas itself paints it over any past-end tiles
# in a partial last row, so the two meet seamlessly.
CANVAS_BACKGROUND = QColor(0x80, 0x80, 0x80)


class GridStyle(Enum):
    """How the tile grid is drawn (the YY-CHR style set). ``value`` is the stable
    string persisted in app settings."""

    NONE = "none"
    POINT = "point"  # a dot at every tile corner, no lines
    DOT = "dot"  # dotted lines
    DASH = "dash"  # dashed lines
    LINE = "line"  # solid lines


# Two fixed grid colors: translucent white at two opacities, so the levels stay
# distinct while tinting the art rather than overwriting it (legible over both
# light and dark pixels). A stronger line every COARSE_GRID_TILES tiles, a faint
# one on every tile in between — YY-CHR's default bank-grid ARGB values.
GRID_COARSE_COLOR = QColor(0xFF, 0xFF, 0xFF, 0x80)  # α128 — every 8 tiles
GRID_FINE_COLOR = QColor(0xFF, 0xFF, 0xFF, 0x20)  # α32 — per tile
# The coarse grid falls every N tiles — YY-CHR's 8×8 block convention.
COARSE_GRID_TILES = 8

# Line styles per drawing style; POINT/NONE are handled separately.
_GRID_PEN_STYLES = {
    GridStyle.DOT: Qt.PenStyle.DotLine,
    GridStyle.DASH: Qt.PenStyle.DashLine,
    GridStyle.LINE: Qt.PenStyle.SolidLine,
}


class Canvas(QWidget):
    # (anchor slot, current slot) — emitted on press and whenever a drag
    # reaches another slot. The anchor stays the pressed slot, so the window
    # can grow/shrink the range live; a plain click emits (slot, slot).
    tiles_selected = Signal(int, int)
    # ARGB sampled under the cursor while the eyedropper is armed. The rendered
    # image is sampled rather than the palette, so the value is right for any
    # view — indexed through a subpalette, or a direct-color codec with no
    # palette at all. ``object``, not ``int``: Qt's int is 32-bit *signed*, and
    # any ARGB with alpha >= 0x80 overflows it.
    color_picked = Signal(object)
    # Pixel-mode gestures, in **image pixel** coordinates (not tile slots). The
    # controller (PixelEditMixin) reads the button to tell left-draw from a
    # right-click eyedropper. Emitted only in EditMode.PIXEL; tile mode still
    # uses tiles_selected.
    pixel_pressed = Signal(int, int, object)  # x, y, Qt.MouseButton
    pixel_moved = Signal(int, int)  # x, y — while the left button is held
    pixel_released = Signal(int, int)  # x, y — the drag's final pixel

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._image = QImage()
        self._zoom = 4
        self._show_grid = False
        self._grid_style = GridStyle.LINE
        self._tile_w = 8
        self._tile_h = 8
        # Arrangement placement (block grouping / order). 1×1 is plain row-major,
        # so every mapping below reduces to the simple form.
        self._block_cols = 1
        self._block_rows = 1
        self._block_order = "row"
        self._selected_slots: frozenset[int] = frozenset()
        self._selection_as_block = False
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
        # Overlays the controller drives while editing pixels: a pixel-space
        # rectangle marquee, and a floating selection (a lifted image the user is
        # dragging) shown at a pixel position. Both are drawn over the base image.
        self._marquee: QRect | None = None
        self._float_image: QImage | None = None
        self._float_pos = (0, 0)
        # How many of the image's tile slots hold real data. When the stream ends
        # mid-row the trailing slots of the bottom row are padding, not tiles, so
        # they are painted as background rather than drawn (None = the whole image
        # is data).
        self._filled_tiles: int | None = None
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

    def set_zoom(self, zoom: int) -> None:
        self._zoom = max(1, zoom)
        self._update_size()

    def set_grid(self, on: bool) -> None:
        self._show_grid = on
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
        self, slots: Iterable[int] | None, *, as_block: bool = False
    ) -> None:
        """Highlight this set of window slots (``None``/empty clears it).

        ``as_block`` says the slots were picked as a cell *rectangle*, which is
        the only selection outlined as a single box. A linear run stays drawn as
        one box per row even when it happens to fill a rectangle, so the shape on
        screen always tells the user which mode made it.
        """
        self._selected_slots = frozenset(slots or ())
        self._selection_as_block = as_block
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
            self.setCursor(Qt.CursorShape.CrossCursor)
            self._drag_anchor = self._drag_slot = None
        else:
            self.unsetCursor()

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
        if mode is EditMode.PIXEL:
            self.setCursor(Qt.CursorShape.CrossCursor)
        else:
            self.unsetCursor()
            self._marquee = None
            self._float_image = None
        self.update()

    def set_marquee(self, rect: QRect | None) -> None:
        """Show a pixel-space rectangle marquee (``None`` clears it)."""
        self._marquee = rect
        self.update()

    def set_float(self, image: QImage | None, x: int = 0, y: int = 0) -> None:
        """Show a floating selection ``image`` at image-pixel ``(x, y)``.

        The lifted pixels the user is dragging, painted (nearest-neighbour, at
        the current zoom) over the base image with a selection outline, so the
        float reads as hovering above the canvas until it is stamped down.
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
        px = int(pos.x()) // self._zoom
        py = int(pos.y()) // self._zoom
        if clamp:
            px = max(0, min(px, self._image.width() - 1))
            py = max(0, min(py, self._image.height() - 1))
        elif not (0 <= px < self._image.width() and 0 <= py < self._image.height()):
            return None
        return px, py

    def _color_at(self, pos: QPointF) -> int | None:
        """ARGB of the rendered pixel under ``pos``; None outside the image."""
        img_x = int(pos.x()) // self._zoom
        img_y = int(pos.y()) // self._zoom
        if not (0 <= img_x < self._image.width() and 0 <= img_y < self._image.height()):
            return None
        return self._image.pixel(img_x, img_y) & 0xFFFFFFFF

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

    def _slot_at(self, pos: QPointF, clamp: bool = False) -> int | None:
        """The window slot under ``pos``; None when outside the image (or a
        block-grid gap cell that holds no tile).

        ``clamp`` snaps an outside position to the nearest edge slot instead —
        a drag that leaves the widget keeps extending to the boundary.
        """
        img_x = int(pos.x()) // self._zoom
        img_y = int(pos.y()) // self._zoom
        if clamp:
            img_x = max(0, min(img_x, self._image.width() - 1))
            img_y = max(0, min(img_y, self._image.height() - 1))
        elif not (
            0 <= img_x < self._image.width() and 0 <= img_y < self._image.height()
        ):
            return None
        return self._layout().cell_to_slot(img_x // self._tile_w, img_y // self._tile_h)

    def mousePressEvent(self, event) -> None:  # noqa: ANN001 — Qt override
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
                self.tiles_selected.emit(slot, slot)
        if event.button() == Qt.MouseButton.LeftButton and not self._image.isNull():
            slot = self._slot_at(event.position())
            if slot is not None:
                self._drag_anchor = self._drag_slot = slot
                self.tiles_selected.emit(slot, slot)
        # Let the default handling run too so ClickFocus keeps focusing us.
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: ANN001 — Qt override
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
                self.tiles_selected.emit(self._drag_anchor, slot)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: ANN001 — Qt override
        if self._edit_mode is EditMode.PIXEL:
            self._pixel_release(event)
            super().mouseReleaseEvent(event)
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_anchor = self._drag_slot = None
        super().mouseReleaseEvent(event)

    def _pixel_press(self, event) -> None:  # noqa: ANN001 — Qt event
        """Begin a pixel gesture: report the pressed pixel and its button.

        A left press starts a drag (the pen/shape/marquee tools track it); a
        right press is a one-shot the controller reads as the eyedropper. A press
        outside the image is ignored, as the tile click always was.
        """
        pixel = self._pixel_at(event.position())
        if pixel is None:
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self._pixel_dragging = True
            self._last_pixel = pixel
        self.pixel_pressed.emit(pixel[0], pixel[1], event.button())

    def _pixel_move(self, event) -> None:  # noqa: ANN001 — Qt event
        if not (self._pixel_dragging and event.buttons() & Qt.MouseButton.LeftButton):
            return
        pixel = self._pixel_at(event.position(), clamp=True)
        if pixel is not None and pixel != self._last_pixel:
            self._last_pixel = pixel
            self.pixel_moved.emit(pixel[0], pixel[1])

    def _pixel_release(self, event) -> None:  # noqa: ANN001 — Qt event
        if event.button() != Qt.MouseButton.LeftButton or not self._pixel_dragging:
            return
        self._pixel_dragging = False
        pixel = self._pixel_at(event.position(), clamp=True) or self._last_pixel
        self._last_pixel = None
        if pixel is not None:
            self.pixel_released.emit(pixel[0], pixel[1])

    def _update_size(self) -> None:
        self.setFixedSize(
            self._image.width() * self._zoom, self._image.height() * self._zoom
        )
        self.update()

    def _cell_rect(self, tile_x: int, tile_y: int) -> QRect:
        """The device-coord rect of one canvas cell."""
        z = self._zoom
        return QRect(
            tile_x * self._tile_w * z,
            tile_y * self._tile_h * z,
            self._tile_w * z,
            self._tile_h * z,
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
            z = self._zoom
            return QRegion(
                QRect(
                    remainder * self._tile_w * z,
                    row * self._tile_h * z,
                    (cols - remainder) * self._tile_w * z,
                    self._tile_h * z,
                )
            )
        region = QRegion()
        for tile_y in range(rows):
            for tile_x in range(cols):
                slot = layout.cell_to_slot(tile_x, tile_y)
                if slot is None or slot >= self._filled_tiles:
                    region = region.united(QRegion(self._cell_rect(tile_x, tile_y)))
        return region if not region.isEmpty() else None

    def paintEvent(self, event) -> None:  # noqa: ARG002 — Qt supplies the event
        if self._image.isNull():
            return
        painter = QPainter(self)
        # Nearest-neighbour: pixels must stay crisp when magnified.
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
        z = self._zoom
        # Past-end slots in a partial last row are backing, not data: fill them
        # with the neutral color and clip them out of the image/grid draw so
        # nothing (not even a grid line) suggests a tile is there. Clip is set
        # under the identity transform, so it stays in device coordinates while
        # the scale below only affects what's drawn.
        background = self._background_region()
        if background is not None:
            painter.setClipRegion(background)
            painter.fillRect(self.rect(), CANVAS_BACKGROUND)
            painter.setClipRegion(QRegion(self.rect()).subtracted(background))
        painter.scale(z, z)
        painter.drawImage(0, 0, self._image)

        painter.resetTransform()
        # The grid is a viewing aid, not part of the art: drawn in device pixels
        # (after resetTransform) so its lines stay 1px crisp at any zoom, and only
        # once a tile is at least 2px so it never swamps the pixels themselves.
        if self._show_grid and self._grid_style is not GridStyle.NONE and z >= 2:
            self._draw_grid(painter, z)
        self._paint_selection(painter)
        self._paint_pixel_overlays(painter)
        painter.end()

    def _paint_pixel_overlays(self, painter: QPainter) -> None:
        """Draw the floating selection and the pixel marquee (pixel mode only).

        The float goes down first (a lifted image the user is dragging), then its
        outline, then the marquee — a pixel-space rectangle. Both scale by the
        zoom and are drawn in device coordinates, over the base image, so they
        track the pixels beneath them. Nothing is painted in tile mode, where
        both stay ``None``.
        """
        z = self._zoom
        if self._float_image is not None:
            fx, fy = self._float_pos
            rect = QRect(
                fx * z,
                fy * z,
                self._float_image.width() * z,
                self._float_image.height() * z,
            )
            painter.drawImage(rect, self._float_image)
            paint_selection_outline(painter, rect)
        if self._marquee is not None and not self._marquee.isNull():
            m = self._marquee
            rect = QRect(m.x() * z, m.y() * z, m.width() * z, m.height() * z)
            paint_selection_outline(painter, rect)

    def _draw_grid(self, painter: QPainter, z: int) -> None:
        """Draw the two-level tile grid in the current style (device coords).

        POINT dots the tile corners in the coarse color; the line styles draw a
        fine grid on every tile (grey) with a coarse grid every
        :data:`COARSE_GRID_TILES` tiles (white) laid over it, so block boundaries
        stand out from the tile lattice.
        """
        img_w, img_h = self._image.width(), self._image.height()
        w, h = img_w * z, img_h * z
        if self._grid_style is GridStyle.POINT:
            painter.setPen(GRID_COARSE_COLOR)
            for gx in range(self._tile_w, img_w, self._tile_w):
                for gy in range(self._tile_h, img_h, self._tile_h):
                    painter.drawPoint(gx * z, gy * z)
            return
        pen_style = _GRID_PEN_STYLES[self._grid_style]
        # Fine first, then coarse over it: shared ×N boundaries read as coarse.
        levels = ((1, GRID_FINE_COLOR), (COARSE_GRID_TILES, GRID_COARSE_COLOR))
        for step_tiles, color in levels:
            pen = QPen(color)
            pen.setStyle(pen_style)
            painter.setPen(pen)
            step_x, step_y = self._tile_w * step_tiles, self._tile_h * step_tiles
            for gx in range(step_x, img_w, step_x):
                painter.drawLine(gx * z, 0, gx * z, h)
            for gy in range(step_y, img_h, step_y):
                painter.drawLine(0, gy * z, w, gy * z)

    def _paint_selection(self, painter: QPainter) -> None:
        if not self._selected_slots:
            return
        layout = self._layout()
        cols, rows = self._columns(), self._rows()
        z = self._zoom
        # Map each selected slot to its cell. A rectangle selection whose cells
        # fill their bounding box is outlined once, so it reads as the one shape
        # it is; everything else falls back to per-row contiguous runs - a linear
        # run is a run through storage, and drawing it as a box would claim a
        # rectangle the user never picked.
        cells_by_row: dict[int, list[int]] = {}
        for slot in self._selected_slots:
            tile_x, tile_y = layout.slot_to_cell(slot)
            if 0 <= tile_x < cols and 0 <= tile_y < rows:
                cells_by_row.setdefault(tile_y, []).append(tile_x)
        block = self._solid_block(cells_by_row) if self._selection_as_block else None
        if block is not None:
            x0, y0, width, height = block
            rect = QRect(
                x0 * self._tile_w * z,
                y0 * self._tile_h * z,
                width * self._tile_w * z,
                height * self._tile_h * z,
            )
            if rect.intersects(self.rect()):
                paint_selection_outline(painter, rect)
            return
        for tile_y, xs in cells_by_row.items():
            xs.sort()
            run_start = prev = xs[0]
            for x in xs[1:] + [-1]:  # -1 sentinel flushes the final run
                if x == prev + 1:
                    prev = x
                    continue
                rect = QRect(
                    run_start * self._tile_w * z,
                    tile_y * self._tile_h * z,
                    (prev - run_start + 1) * self._tile_w * z,
                    self._tile_h * z,
                )
                if rect.intersects(self.rect()):
                    paint_selection_outline(painter, rect)
                run_start = prev = x

    @staticmethod
    def _solid_block(
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
