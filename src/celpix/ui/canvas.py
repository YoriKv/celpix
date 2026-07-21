"""The tile canvas: draws the rendered image at integer zoom, optional tile grid.

Deliberately minimal for the MVP — a fixed-size widget the main window drops into
a scroll area. It owns no model; it is handed a ready :class:`QImage` by the render
bridge and only scales/paints it. Selection is expressed in **window slot indices**
(0 .. visible tiles - 1): the canvas reports clicks as slots and paints the slot it
is told to highlight, while the main window owns which absolute tile is selected.
Editing (mouse painting) attaches here later.
"""

from __future__ import annotations

from PySide6.QtCore import QRect, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QWidget

from celpix.ui.widgets import paint_selection_outline


class Canvas(QWidget):
    tile_clicked = Signal(int)  # slot index within the current window

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._image = QImage()
        self._zoom = 4
        self._show_grid = False
        self._tile_w = 8
        self._tile_h = 8
        self._selected_slot: int | None = None
        self._update_size()

    def set_image(self, image: QImage) -> None:
        self._image = image
        self._update_size()

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

    def set_selection(self, slot: int | None) -> None:
        """Highlight one window slot (``None`` clears the highlight)."""
        self._selected_slot = slot
        self.update()

    def _columns(self) -> int:
        # The composed image is exactly columns * tile_w wide, so the count is
        # recoverable without the canvas holding view state.
        return max(1, self._image.width() // self._tile_w)

    def mousePressEvent(self, event) -> None:  # noqa: ANN001 — Qt override
        if event.button() == Qt.MouseButton.LeftButton and not self._image.isNull():
            img_x = int(event.position().x()) // self._zoom
            img_y = int(event.position().y()) // self._zoom
            if img_x < self._image.width() and img_y < self._image.height():
                col = img_x // self._tile_w
                row = img_y // self._tile_h
                self.tile_clicked.emit(row * self._columns() + col)
        # Let the default handling run too so ClickFocus keeps focusing us.
        super().mousePressEvent(event)

    def _update_size(self) -> None:
        self.setFixedSize(
            self._image.width() * self._zoom, self._image.height() * self._zoom
        )
        self.update()

    def paintEvent(self, event) -> None:  # noqa: ARG002 — Qt supplies the event
        if self._image.isNull():
            return
        painter = QPainter(self)
        # Nearest-neighbour: pixels must stay crisp when magnified.
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
        z = self._zoom
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
        if self._selected_slot is None:
            return
        cols = self._columns()
        col = self._selected_slot % cols
        row = self._selected_slot // cols
        z = self._zoom
        rect = QRect(
            col * self._tile_w * z,
            row * self._tile_h * z,
            self._tile_w * z,
            self._tile_h * z,
        )
        if not rect.intersects(self.rect()):
            return
        paint_selection_outline(painter, self.palette(), rect)

    def sizeHint(self):  # noqa: ANN201 — Qt override
        return self.size()
