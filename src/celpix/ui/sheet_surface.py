"""A composed sheet of squares, magnified: the shared half of the sheet panels.

The tile source panel and the subsprite sheet are the same widget around
different contents — a picture composed elsewhere, laid ``columns`` squares
across, drawn at a zoom of its own with Ctrl+wheel and space-drag reported
rather than acted on (:class:`~celpix.ui.widgets.PanZoomSurface`). What they
share is the geometry (which square is where, which ones a repaint exposes),
the paint preamble (the backing, the nearest-neighbour blit), a lattice ruled
between squares, and white captions on a dark plate. What differs is what a
square *is* — a tile ID, a sprite record — and how the panel answers a click,
which stays each panel's own.
"""

from __future__ import annotations

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QWidget

from celpix.core import ceil_div
from celpix.ui.canvas import CANVAS_BACKGROUND, GRID_COARSE_ALPHA
from celpix.ui.panzoom import PanZoomSurface

__all__ = ["LABEL_COLOR", "LABEL_MIN_PX", "LABEL_PLATE", "SheetSurface"]

# Below this many screen pixels a square, a caption is dropped rather than drawn
# (:meth:`SheetSurface._caption_height`). 24 is where a 7-pixel font stops fitting
# inside one square with the art still visible above it.
LABEL_MIN_PX = 24

# The plate a caption sits on, and the ink on it. Near-opaque black under white
# because a sheet is as often light-on-dark as dark-on-light, and a single ink
# colour vanishes into half of them.
LABEL_PLATE = QColor(0, 0, 0, 190)
LABEL_COLOR = QColor(255, 255, 255)


class SheetSurface(PanZoomSurface, QWidget):
    """A sheet of equal squares, ``columns`` across, drawn at an integer zoom.

    A subclass says how many squares there are (:meth:`_slot_count`) and paints
    its own marks over :meth:`_paint_sheet`; the geometry here is in **slots**,
    positions in the subclass's own list, which is what every square is looked
    up by. ``_zoom_range`` bounds :meth:`set_zoom` — ``None`` for no ceiling.
    """

    _zoom_range: tuple[int, int | None] = (1, None)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._sheet = QImage()
        self._cell_px = (8, 8)  # one square's size in the sheet's own pixels
        self._columns = 16
        self._zoom = 2

    def _slot_count(self) -> int:
        """How many squares the sheet holds."""
        raise NotImplementedError

    def set_zoom(self, zoom: int) -> None:
        low, high = self._zoom_range
        zoom = max(low, zoom if high is None else min(high, zoom))
        if zoom != self._zoom:
            self._zoom = zoom
            self._update_size()

    def _has_content(self) -> bool:
        return not self._sheet.isNull()

    # -- geometry ------------------------------------------------------------
    def _rows(self) -> int:
        return max(1, ceil_div(self._slot_count(), self._columns))

    def _update_size(self) -> None:
        cw, ch = self._cell_px
        self.setFixedSize(*self._scaled_size(self._columns * cw, self._rows() * ch))
        self.update()

    def _cell_rect(self, slot: int) -> QRect:
        """Where the ``slot``-th square sits — the grid geometry, in one place."""
        cw, ch = self._cell_px
        return self._scaled_rect(
            (slot % self._columns) * cw, (slot // self._columns) * ch, cw, ch
        )

    def _exposed_slots(self, exposed: QRect) -> range:
        """The squares ``exposed`` covers — whole rows of the sheet.

        What the per-square overlays loop over, instead of the whole run: a bank
        is thousands of squares and a scrolled view shows a dozen rows of them
        (:meth:`~celpix.ui.widgets.PanZoomSurface._exposed_rows`). Whole rows
        because the grid is filled left to right, so a row's slots are contiguous
        and the columns are few enough not to be worth trimming.
        """
        first, stop = self._exposed_rows(exposed, self._cell_px[1])
        count = self._slot_count()
        return range(
            min(first * self._columns, count), min(stop * self._columns, count)
        )

    # -- painting ------------------------------------------------------------
    def _paint_sheet(self, painter: QPainter, exposed: QRect) -> None:
        """The backing, then the sheet at the zoom.

        The trailing squares of a partial last row are backing, not content: the
        neutral canvas colour says so, the same answer the canvas gives past the
        end of a file. Painted under the sheet rather than over it, so a full
        grid costs one fill and no clipping. Nearest-neighbour, so the art stays
        crisp when magnified.
        """
        painter.fillRect(exposed, CANVAS_BACKGROUND)
        if self._sheet.isNull():
            return
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
        painter.scale(self._zoom_x, self._zoom_y)
        painter.drawImage(0, 0, self._sheet)
        painter.resetTransform()

    def _paint_lattice(
        self, painter: QPainter, exposed: QRect, step_cells: int, color: QColor
    ) -> None:
        """Rule the sheet every ``step_cells`` squares, both ways, in ``color``.

        **Interior lines only** — the step counts from the sheet's own top-left,
        so a line at 0 would be a border around the widget rather than a division
        of it, which is the rule the canvas's lattice follows too. Lines outside
        the exposed band are skipped: a sheet read at 8x is mostly off screen.

        Stepped in the sheet's own pixels and scaled at the point of drawing,
        which is what keeps the lines on the square boundaries under a
        non-square pixel: the device step is fractional there, and stepping by a
        rounded one would drift a pixel further from the art every few squares.
        """
        step_x = self._cell_px[0] * step_cells
        step_y = self._cell_px[1] * step_cells
        if step_x <= 0 or step_y <= 0:
            return
        img_w = self._columns * self._cell_px[0]
        img_h = self._rows() * self._cell_px[1]
        ink = QColor(color)
        ink.setAlpha(GRID_COARSE_ALPHA)
        painter.setPen(ink)
        for gx in range(step_x, img_w, step_x):
            x = round(gx * self._zoom_x)
            if exposed.left() <= x <= exposed.right():
                painter.drawLine(x, exposed.top(), x, exposed.bottom())
        for gy in range(step_y, img_h, step_y):
            y = round(gy * self._zoom_y)
            if exposed.top() <= y <= exposed.bottom():
                painter.drawLine(exposed.left(), y, exposed.right(), y)

    def _caption_height(
        self, painter: QPainter, height_share: int, width_share: int
    ) -> int | None:
        """Set the caption font for the zoom; its line height, or ``None`` to skip.

        **Skipped entirely below** :data:`LABEL_MIN_PX` a square, because a
        caption that does not fit is worse than none: overflowing text spills
        onto the squares either side and claims to describe them. The font is
        the square's height over ``height_share`` or its width over
        ``width_share``, whichever is smaller, and never under seven pixels.
        """
        cw = self._cell_px[0] * self._zoom_x
        ch = self._cell_px[1] * self._zoom_y
        if min(cw, ch) < LABEL_MIN_PX:
            return None
        font = painter.font()
        font.setPixelSize(max(7, int(min(ch // height_share, cw // width_share))))
        painter.setFont(font)
        return painter.fontMetrics().height()

    @staticmethod
    def _paint_caption(
        painter: QPainter, square: QRect, height: int, text: str
    ) -> None:
        """``text`` across the bottom of ``square``, white on a dark plate.

        Bottom-aligned so it covers the part of the art that carries the least
        of its identity, and cut to its own square so a wide caption is clipped
        rather than borrowed from the neighbour.
        """
        strip = QRect(square.left(), square.bottom() - height, square.width(), height)
        painter.fillRect(strip, LABEL_PLATE)
        painter.setPen(LABEL_COLOR)
        painter.drawText(strip, Qt.AlignmentFlag.AlignCenter, text)

    def sizeHint(self):  # noqa: ANN201 — Qt override
        return self.size()
