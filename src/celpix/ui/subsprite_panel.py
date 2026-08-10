"""The subsprite sheet: a sprite map's records, one to a square.

The tile source panel's twin, one level along. A tilemap cell *names* a tile that
lives in another entry and the picture says nothing about which, so that panel
shows the tiles themselves; a sprite map's frame is a heap of overlapping pieces
at signed pixel offsets, and the front ones hide the back ones, so the picture
says nothing about what the frame is **made of**. This shows the pieces
themselves, one per record (``docs/design/tilemap-entry.md`` §6).

A dumb view, like :class:`~celpix.ui.tile_source_panel.TileSourcePanel`: it is
handed a composed sheet and the record list it covers, and resolves nothing. The
sheet comes from :func:`~celpix.pipeline.pipeline.subsprite_sheet`, which draws a
piece through the same blit that draws it on the canvas, so a subsprite here and
the same subsprite there are the same pixels.

**Addressed in records, not tiles.** A square holds a ``(frame, subsprite)``
pair — both indices into what is drawn, which is the numbering the canvas's own
pick speaks (:mod:`celpix.ui.main_window.sprite_select`). That is the whole
reason this is not the tile source panel with different pictures in it: a
subsprite is not a tile and has no ID, and two records drawing the same tile are
two different things to point at. Under the sheet's *inventory* reading a square
is the **first** of the several records drawing one piece, which changes nothing
here — a square still holds one pair, and which pair a pick belongs to is settled
before it arrives (:mod:`celpix.ui.main_window.subsprites`).

**One ring, and it is not a selection.** The canvas is what picks a subsprite
here; a square is a thing to *find*, not a thing to choose, so nothing in this
panel selects. The ring marks the record the canvas picked, and it is drawn in
the grid's structural blue for the reason the canvas outline and the tile source
panel's marker are — white is where the user pointed, blue is what that resolved
to.

It rings the **piece**, not the square it was laid in. The two are the same
rectangle on an object whose subsprites are all one size, and on one that mixes
them the square is the largest of them: ringing that would say the gutter around
a small piece is part of the record, which is precisely the misreading the
window's *sizes* badge exists to warn about. The rectangles come composed with
the sheet (:attr:`~celpix.pipeline.render.SubspriteSheet.boxes`) rather than
being worked out again here, so the ring cannot drift from the blit.

**A lattice on every square**, unlike the tile source panel's every sixteen. What
that panel rules off is the *ID space*, whose tiles are already separate
pictures; what this rules off is where one record ends and the next begins, which
on an object whose pieces are all one size is otherwise an unbroken band of art.
It is drawn in the canvas's fine **grey**, not the structural blue that panel
rules in, for the reason above: the blue is the ring's here, and a blue ring
inside a blue lattice is a ring nobody can find.

**Ctrl+wheel zooms and space-drag pans**, the canvas's gestures with the canvas's
meanings. Both are reported rather than acted on: the level lives in the window's
Zoom spin and the scrolling in its scroll area, and neither is this widget's.
"""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import QRect, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QWidget

from celpix.core import ceil_div
from celpix.ui.canvas import (
    CANVAS_BACKGROUND,
    GRID_COARSE_ALPHA,
    GRID_FINE_COLOR,
    GRID_STRUCTURE_COLOR,
)
from celpix.ui.tile_source_panel import LABEL_COLOR, LABEL_MIN_PX, LABEL_PLATE
from celpix.ui.widgets import PanZoomSurface, paint_selection_outline

#: One record, as the sheet addresses it: ``(frame, subsprite)``.
Record = tuple[int, int]

#: Where one record's art sits in the sheet: ``(x, y, w, h)`` in sheet pixels.
Box = tuple[int, int, int, int]


class SubspritePanel(PanZoomSurface, QWidget):
    """The composed subsprite sheet, drawn at this window's zoom."""

    zoom_requested = Signal(int, object)  # steps, QPointF cursor pos (widget)
    pan_requested = Signal(int, int)  # dx, dy

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._sheet = QImage()
        self._records: Sequence[Record] = ()
        self._boxes: Sequence[Box] = ()
        self._cell_px = (8, 8)  # one square's size in image pixels
        self._columns = 16
        self._zoom = 2
        self._marked: Record | None = None
        self._captions = True
        # Focusable although nothing here is picked with the keyboard: the window
        # puts the focus on the sheet when it opens, and its Cols keys yield to a
        # focused spin box — so a sheet that could not hold focus would leave the
        # header holding it and the keys answering nowhere
        # (:meth:`~celpix.ui.subsprite_window.SubspriteWindow._columns_key`).
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._update_size()

    # -- what to show --------------------------------------------------------
    def set_sheet(
        self,
        sheet: QImage,
        records: Sequence[Record],
        boxes: Sequence[Box],
        cell_px: tuple[int, int],
        columns: int,
    ) -> None:
        """Show ``sheet``, whose squares hold ``records`` laid ``columns`` across.

        ``cell_px`` is one square's size in the sheet's own pixels — the largest
        subsprite of the object, in whole tiles — which is what lays the lattice
        and the captions out. ``boxes`` is where each record's own art landed
        inside those squares, and is what turns a record into a rectangle to
        ring; a slot with no box falls back to its square, which is the same
        rectangle whenever the object's pieces are all one size.

        A mark on a record the new sheet does not hold is dropped rather than
        moved: a record is named by its position in the file, and the nearest
        surviving one is a different piece of a different frame.
        """
        self._sheet = sheet
        self._records = list(records)
        self._boxes = list(boxes)
        self._cell_px = (max(1, cell_px[0]), max(1, cell_px[1]))
        self._columns = max(1, columns)
        if self._marked is not None and self._marked not in self._records:
            self._marked = None
        self._update_size()

    def records(self) -> Sequence[Record]:
        """The records on show, one per square, in slot order."""
        return self._records

    def set_zoom(self, zoom: int) -> None:
        if zoom != self._zoom:
            self._zoom = max(1, zoom)
            self._update_size()

    def set_captions(self, on: bool) -> None:
        """Whether each square is captioned with the record it holds."""
        if on != self._captions:
            self._captions = on
            self.update()

    def set_marked(self, record: Record | None) -> None:
        """Ring the record the canvas picked, or clear the ring."""
        if record is not None and record not in self._records:
            record = None
        if record != self._marked:
            self._marked = record
            self.update()

    def clear(self) -> None:
        """Show nothing — no document, or one that is not a sprite map."""
        self.set_sheet(QImage(), (), (), self._cell_px, self._columns)

    def _has_content(self) -> bool:
        return not self._sheet.isNull()

    # -- geometry ------------------------------------------------------------
    def _rows(self) -> int:
        return max(1, ceil_div(len(self._records), self._columns))

    def _update_size(self) -> None:
        cw, ch = self._cell_px
        self.setFixedSize(
            self._columns * cw * self._zoom, self._rows() * ch * self._zoom
        )
        self.update()

    def _cell_rect(self, slot: int) -> QRect:
        """Where the ``slot``-th square sits — the grid geometry, in one place."""
        cw, ch = self._cell_px[0] * self._zoom, self._cell_px[1] * self._zoom
        return QRect((slot % self._columns) * cw, (slot // self._columns) * ch, cw, ch)

    def _piece_rect(self, slot: int) -> QRect:
        """Where the ``slot``-th record's *art* sits — what the ring goes round.

        The composed box scaled by the zoom, so it is whole widget pixels for
        whole sheet ones and the ring lands on the boundary of the art rather
        than through the outermost row of it. A slot the sheet gave no box for
        falls back to its square (see :meth:`set_sheet`).
        """
        if slot >= len(self._boxes):
            return self._cell_rect(slot)
        x, y, w, h = self._boxes[slot]
        z = self._zoom
        return QRect(x * z, y * z, w * z, h * z)

    # -- interaction ---------------------------------------------------------
    def mousePressEvent(self, event) -> None:  # noqa: ANN001 — Qt override
        if self._pan_press(event):
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: ANN001 — Qt override
        if self._pan_move(event):
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: ANN001 — Qt override
        if self._pan_release(event):
            return
        super().mouseReleaseEvent(event)

    # -- painting ------------------------------------------------------------
    def paintEvent(self, event) -> None:  # noqa: ANN001 — Qt override
        painter = QPainter(self)
        # The trailing squares of a partial last row are backing, not records:
        # the neutral canvas colour says so, the same answer the canvas gives
        # past the end of a file. Painted under the sheet rather than over it, so
        # a full grid costs one fill and no clipping.
        painter.fillRect(event.rect(), CANVAS_BACKGROUND)
        if not self._sheet.isNull():
            # Nearest-neighbour: pixel art must stay crisp when magnified.
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
            painter.scale(self._zoom, self._zoom)
            painter.drawImage(0, 0, self._sheet)
            painter.resetTransform()
            self._paint_grid(painter, event.rect())
            self._paint_captions(painter, event.rect())
        if self._marked is not None:
            paint_selection_outline(
                painter,
                self._piece_rect(self._records.index(self._marked)),
                color=GRID_STRUCTURE_COLOR,
            )
        painter.end()

    def _paint_grid(self, painter: QPainter, exposed: QRect) -> None:
        """Rule the sheet on every square boundary, both ways.

        The canvas's **fine** grey rather than its structural blue, and that is
        the whole of the reason: the blue is spoken for here. It is what the
        canvas's pick outline and the tile source panel's marker are drawn in, so
        it is what the ring over the picked record is drawn in too — and a blue
        ring inside a blue lattice is a ring nobody can find. The lattice is the
        lesser of the two marks, so it takes the lesser colour.

        Drawn over the art and under the ring, so a marked square on a boundary
        still reads as marked.

        **Interior lines only** — a line at 0 would be a border around the widget
        rather than a division of it, the rule the canvas's lattice follows too.
        Lines outside the exposed band are skipped: a long object read at 8x is
        mostly off screen.
        """
        cw, ch = self._cell_px[0] * self._zoom, self._cell_px[1] * self._zoom
        if cw <= 0 or ch <= 0:
            return
        color = QColor(GRID_FINE_COLOR)
        color.setAlpha(GRID_COARSE_ALPHA)
        painter.setPen(color)
        for x in range(cw, self.width(), cw):
            if exposed.left() <= x <= exposed.right():
                painter.drawLine(x, exposed.top(), x, exposed.bottom())
        for y in range(ch, self.height(), ch):
            if exposed.top() <= y <= exposed.bottom():
                painter.drawLine(exposed.left(), y, exposed.right(), y)

    def _paint_captions(self, painter: QPainter, exposed: QRect) -> None:
        """Write ``frame:subsprite`` across the bottom of each square.

        The one thing the picture cannot show. A sheet in reading order runs
        through the frames without a break, and which frame a piece came from —
        the number an animation step names, and the number the status line says —
        is nowhere in the art. That is the same reason the tile source panel
        writes IDs over its squares, so it is drawn the same way: white on a
        near-opaque plate, because a sprite is as often light on dark as dark on
        light and a single ink colour vanishes into half of them.

        **Skipped entirely below** :data:`~celpix.ui.tile_source_panel.
        LABEL_MIN_PX`, because a caption that does not fit is worse than none:
        overflowing text spills onto the squares either side and claims to
        describe them.
        """
        if not self._captions:
            return
        cw, ch = self._cell_px[0] * self._zoom, self._cell_px[1] * self._zoom
        if min(cw, ch) < LABEL_MIN_PX:
            return
        font = painter.font()
        font.setPixelSize(max(7, min(ch // 4, cw // 4)))
        painter.setFont(font)
        height = painter.fontMetrics().height()
        for slot, (at, index) in enumerate(self._records):
            square = self._cell_rect(slot)
            if not square.intersects(exposed):
                continue
            strip = QRect(
                square.left(), square.bottom() - height, square.width(), height
            )
            painter.fillRect(strip, LABEL_PLATE)
            painter.setPen(LABEL_COLOR)
            painter.drawText(strip, Qt.AlignmentFlag.AlignCenter, f"{at}:{index}")

    def sizeHint(self):  # noqa: ANN201 — Qt override
        return self.size()
