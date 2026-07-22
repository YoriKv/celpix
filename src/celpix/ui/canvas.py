"""The tile canvas: draws the rendered image at integer zoom, optional tile grid.

Deliberately minimal for the MVP — a fixed-size widget the main window drops into
a scroll area. It owns no model; it is handed a ready :class:`QImage` by the render
bridge and only scales/paints it. Selection is expressed in **window slot indices**
(0 .. visible tiles - 1): the canvas reports pressed/dragged slots and paints the
span it is told to highlight, while the main window owns which absolute tiles are
selected. A click selects one tile; dragging extends the selection to the linear
slot range between the press and the pointer (tiles are a linear byte stream, so
a range is a run of slots, not a rectangle). Editing (mouse painting) attaches
here later.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRect, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QRegion
from PySide6.QtWidgets import QWidget

from celpix.ui.widgets import paint_selection_outline

# The neutral surround/backing behind the rendered pixels: a fixed mid-gray (not a
# theme colour) so it never biases how the art's colours read. The scroll viewport
# paints it around the canvas; the canvas itself paints it over any past-end tiles
# in a partial last row, so the two meet seamlessly.
CANVAS_BACKGROUND = QColor(0x80, 0x80, 0x80)


class Canvas(QWidget):
    # (anchor slot, current slot) — emitted on press and whenever a drag
    # reaches another slot. The anchor stays the pressed slot, so the window
    # can grow/shrink the range live; a plain click emits (slot, slot).
    tiles_selected = Signal(int, int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._image = QImage()
        self._zoom = 4
        self._show_grid = False
        self._tile_w = 8
        self._tile_h = 8
        self._selected_span: tuple[int, int] | None = None
        self._drag_anchor: int | None = None
        self._drag_slot: int | None = None  # last emitted, to skip no-op moves
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

    def set_tile_size(self, width: int, height: int) -> None:
        self._tile_w = max(1, width)
        self._tile_h = max(1, height)
        self.update()

    def set_selection(self, span: tuple[int, int] | None) -> None:
        """Highlight an inclusive slot range (``None`` clears the highlight)."""
        self._selected_span = span
        self.update()

    def _columns(self) -> int:
        # The composed image is exactly columns * tile_w wide, so the count is
        # recoverable without the canvas holding view state.
        return max(1, self._image.width() // self._tile_w)

    def _slot_at(self, pos: QPointF, clamp: bool = False) -> int | None:
        """The window slot under ``pos``; None when outside the image.

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
        return (img_y // self._tile_h) * self._columns() + (img_x // self._tile_w)

    def mousePressEvent(self, event) -> None:  # noqa: ANN001 — Qt override
        if event.button() == Qt.MouseButton.LeftButton and not self._image.isNull():
            slot = self._slot_at(event.position())
            if slot is not None:
                self._drag_anchor = self._drag_slot = slot
                self.tiles_selected.emit(slot, slot)
        # Let the default handling run too so ClickFocus keeps focusing us.
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: ANN001 — Qt override
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
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_anchor = self._drag_slot = None
        super().mouseReleaseEvent(event)

    def _update_size(self) -> None:
        self.setFixedSize(
            self._image.width() * self._zoom, self._image.height() * self._zoom
        )
        self.update()

    def _past_end_rect(self) -> QRect | None:
        """Device-coord rect of the bottom row's padding slots, or None.

        When the data ends mid-row the missing tiles are one contiguous block at
        the end of the last row (tiles are a linear stream). None when every slot
        is data or the last row happens to be exactly full.
        """
        if self._filled_tiles is None or self._image.isNull():
            return None
        cols = self._columns()
        remainder = self._filled_tiles % cols
        rows = max(1, self._image.height() // self._tile_h)
        row = self._filled_tiles // cols
        if remainder == 0 or row >= rows:
            return None
        z = self._zoom
        return QRect(
            remainder * self._tile_w * z,
            row * self._tile_h * z,
            (cols - remainder) * self._tile_w * z,
            self._tile_h * z,
        )

    def paintEvent(self, event) -> None:  # noqa: ARG002 — Qt supplies the event
        if self._image.isNull():
            return
        painter = QPainter(self)
        # Nearest-neighbour: pixels must stay crisp when magnified.
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
        z = self._zoom
        # Past-end slots in a partial last row are backing, not data: fill them
        # with the neutral colour and clip them out of the image/grid draw so
        # nothing (not even a grid line) suggests a tile is there. Clip is set
        # under the identity transform, so it stays in device coordinates while
        # the scale below only affects what's drawn.
        past_end = self._past_end_rect()
        if past_end is not None:
            painter.fillRect(past_end, CANVAS_BACKGROUND)
            painter.setClipRegion(QRegion(self.rect()).subtracted(QRegion(past_end)))
        painter.scale(z, z)
        painter.drawImage(0, 0, self._image)

        painter.resetTransform()
        if self._show_grid and z >= 2:
            pen = painter.pen()
            pen.setColor(QColor(0, 0, 0, 96))
            painter.setPen(pen)
            w = self._image.width() * z
            h = self._image.height() * z
            for gx in range(0, self._image.width() + 1, self._tile_w):
                painter.drawLine(gx * z, 0, gx * z, h)
            for gy in range(0, self._image.height() + 1, self._tile_h):
                painter.drawLine(0, gy * z, w, gy * z)
        self._paint_selection(painter)
        painter.end()

    def _paint_selection(self, painter: QPainter) -> None:
        if self._selected_span is None:
            return
        first, last = self._selected_span
        cols = self._columns()
        z = self._zoom
        # A linear slot run isn't a rectangle once it wraps rows — outline each
        # row's contiguous segment so the shape reads as one selection.
        for row in range(first // cols, last // cols + 1):
            seg_first = max(first, row * cols)
            seg_last = min(last, row * cols + cols - 1)
            rect = QRect(
                (seg_first % cols) * self._tile_w * z,
                row * self._tile_h * z,
                (seg_last - seg_first + 1) * self._tile_w * z,
                self._tile_h * z,
            )
            if rect.intersects(self.rect()):
                paint_selection_outline(painter, self.palette(), rect)

    def sizeHint(self):  # noqa: ANN201 — Qt override
        return self.size()
