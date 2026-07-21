"""The tile canvas: draws the rendered image at integer zoom, optional tile grid.

Deliberately minimal for the MVP — a fixed-size widget the main window drops into
a scroll area. It owns no model; it is handed a ready :class:`QImage` by the render
bridge and only scales/paints it. Editing (mouse painting, selection) attaches here
later.
"""

from __future__ import annotations

from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QWidget


class Canvas(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._image = QImage()
        self._zoom = 4
        self._show_grid = False
        self._tile_w = 8
        self._tile_h = 8
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

        if self._show_grid and z >= 2:
            painter.resetTransform()
            pen = painter.pen()
            pen.setColor(QColor(0, 0, 0, 96))
            painter.setPen(pen)
            w = self._image.width() * z
            h = self._image.height() * z
            for gx in range(0, self._image.width() + 1, self._tile_w):
                painter.drawLine(gx * z, 0, gx * z, h)
            for gy in range(0, self._image.height() + 1, self._tile_h):
                painter.drawLine(0, gy * z, w, gy * z)
        painter.end()

    def sizeHint(self):  # noqa: ANN201 — Qt override
        return self.size()
