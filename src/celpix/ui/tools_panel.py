"""The drawing-tools rail: an exclusive vertical column of tool buttons.

A dumb view, like :class:`~celpix.ui.palette_panel.PalettePanel`, embedded down
the canvas's right edge: it shows one checkable button per tool in
:data:`~celpix.ui.tools.TOOL_SPECS` order — which is also the 1–9 key order, so a
button's position in the column is its number key — and reports the picked tool
via :attr:`tool_selected`. It owns no editing logic and no mode state: the
pixel-edit controller decides what a tool *does* and drives :meth:`set_tool` back
when the tool changes by a number-key shortcut, so the buttons always mirror the
active tool.

Each button is a fixed square showing an icon only. The face comes from a bundled
monochrome PNG where one exists (pencil, fill bucket, eyedropper) or a shape the
panel paints for the geometry tools (line/rect/ellipse and their filled variants,
plus the selection marquee), so those share one size and padding. Both are
rasterized here and tinted to the palette's text color, so the panel tracks the
theme and the display's pixel ratio — see :meth:`_rebuild_icons`.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QIcon, QImage, QPainter, QPalette, QPen, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup,
    QGridLayout,
    QSizePolicy,
    QToolButton,
    QWidget,
)

from celpix import resources
from celpix.ui.tools import TOOL_SPECS, Tool, ToolSpec

# A single vertical column: the panel is a rail down the right edge of the canvas,
# so the nine tools read top-to-bottom like a paint program's toolbox.
_COLUMNS = 1
# The rail follows TOOL_SPECS order, which is also the 1..9 key order — so a
# button's position in the column is its number key. Reorder there, not here.

# The square button and the glyph drawn inside it, in logical pixels. The icon
# leaves a little breathing room inside the button's frame and check highlight.
_BUTTON = 30
_ICON = 20


class ToolsPanel(QWidget):
    tool_selected = Signal(object)  # the picked Tool

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._buttons: dict[Tool, QToolButton] = {}
        # The (palette, device-pixel-ratio) the current icons were baked against;
        # a change to either invalidates them (see _rebuild_icons).
        self._icon_key: tuple[int, float] | None = None
        # Exclusive group: exactly one tool is active, and clicking another
        # unchecks the last without any bookkeeping here.
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        # A rail hugs the canvas's right edge: fixed to its column width, and no
        # taller than the buttons need, so the layout pins it to the top rather
        # than stretching the gaps between tools.
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Maximum)
        grid = QGridLayout(self)
        grid.setContentsMargins(2, 2, 2, 2)
        grid.setSpacing(2)
        for i, spec in enumerate(TOOL_SPECS):
            button = QToolButton()
            # "(key)" trails the description, the form every keyed control uses.
            button.setToolTip(f"{spec.tooltip} ({spec.key})")
            # The label is gone from the face but stays the accessible name, so
            # screen readers and the tooltip still identify the tool.
            button.setText(spec.label)
            button.setAccessibleName(spec.label)
            button.setCheckable(True)
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
            button.setFixedSize(_BUTTON, _BUTTON)
            button.setIconSize(QSize(_ICON, _ICON))
            # Bind the spec's tool per-iteration so every button reports its own.
            button.clicked.connect(lambda _=False, tool=spec.tool: self._pick(tool))
            self._group.addButton(button)
            grid.addWidget(button, i // _COLUMNS, i % _COLUMNS)
            self._buttons[spec.tool] = button
        self._rebuild_icons()
        # Start on the pen without announcing it — the controller seeds its own
        # tool state from the same default, and overrides it while in tile mode.
        self._buttons[Tool.PENCIL].setChecked(True)

    def _pick(self, tool: Tool) -> None:
        self.tool_selected.emit(tool)

    def set_tool(self, tool: Tool) -> None:
        """Check ``tool``'s button without emitting — reflect an external change."""
        button = self._buttons.get(tool)
        if button is not None and not button.isChecked():
            button.setChecked(True)

    # -- icons ---------------------------------------------------------------
    def changeEvent(self, event: QEvent) -> None:  # Qt override
        # Both bakes are invalidated from outside: a theme switch swaps the
        # palette out from under the tint, and a move to a differently scaled
        # display invalidates the resolution. The second one matters more than
        # it looks: the glyphs are laid out on exact integer pixels (the
        # marquee's ants especially), so a pixmap baked at the wrong ratio is
        # *smooth-scaled* by Qt to fit - blurring the dashes and drifting them
        # off the centre they were placed on. The panel is built before the
        # window is shown, so this is also how it picks up the real ratio.
        super().changeEvent(event)
        if event.type() in (
            QEvent.Type.PaletteChange,
            QEvent.Type.DevicePixelRatioChange,
            QEvent.Type.ScreenChangeInternal,
        ):
            self._rebuild_icons()

    def _rebuild_icons(self) -> None:
        """(Re)bake every tool glyph against the current palette and pixel ratio.

        The tint and resolution are baked into each pixmap, so a plain cache kept
        across a theme switch or a drag to a differently scaled monitor would show
        yesterday's color at the wrong size. Guarding on the (palette, ratio) key
        makes the frequent PaletteChange storm on startup a no-op after the first.
        """
        color = self.palette().color(
            QPalette.ColorGroup.Active, QPalette.ColorRole.ButtonText
        )
        ratio = self.devicePixelRatioF()
        key = (self.palette().cacheKey(), ratio)
        if key == self._icon_key:
            return
        self._icon_key = key
        for spec in TOOL_SPECS:
            self._buttons[spec.tool].setIcon(self._tool_icon(spec, color, ratio))

    def _tool_icon(self, spec: ToolSpec, color, ratio: float) -> QIcon:
        """The tool's face tinted to ``color``, rasterized at ``ratio``.

        Both sources feed one recolor path: an alpha mask (the PNG's own alpha, or
        the shape painted onto a transparent square) is filled with the tint via
        SourceIn. So a bundled icon and a painted primitive land on-theme with the
        same weight. The pixmap carries its device ratio, so it still measures
        ``_ICON`` in layout units.
        """
        box = round(_ICON * ratio)
        mask = QPixmap(box, box)
        mask.fill(Qt.GlobalColor.transparent)
        painter = QPainter(mask)
        if spec.icon is not None:
            source = QImage.fromData(resources.read_bytes("icons", spec.icon))
            scaled = QPixmap.fromImage(source).scaled(
                box,
                box,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            painter.drawPixmap(
                (box - scaled.width()) // 2, (box - scaled.height()) // 2, scaled
            )
        else:
            self._paint_shape(painter, spec.shape, box)
        # Recolor whatever was drawn to the tint, preserving its alpha shape.
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
        painter.fillRect(mask.rect(), color)
        painter.end()
        mask.setDevicePixelRatio(ratio)
        return QIcon(mask)

    @staticmethod
    def _paint_shape(painter: QPainter, shape: str, box: int) -> None:
        """Draw a geometry-tool primitive centered in a ``box``-pixel square.

        One padding and one stroke width serve every shape so the line, rectangles
        and ellipses read as a set. Outlines inset by half the stroke so the pen
        stays inside the same bounds the filled variants fill — filled and outline
        occupy the identical footprint, differing only in ink.
        """
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        pad = round(box * 0.18)
        stroke = max(1, round(box * 0.11))  # thick enough to read at 20 px
        rect = QRect(pad, pad, box - 2 * pad, box - 2 * pad)
        pen = QPen(Qt.GlobalColor.black, stroke)
        pen.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        # Every stroked shape follows this path, not `rect` itself. A pen is
        # centred on its path and spreads half its width either side, so a shape
        # drawn on `rect` would ink half a stroke *outside* the bounds the filled
        # variants fill. Insetting by exactly half - in float, which is the whole
        # point: `stroke // 2` is right only for even widths and leaves an odd
        # one half a pixel proud, which rasterizes as one extra pixel on one side
        # and reads as a glyph that has slid down and right. Inset properly, the
        # ink lands on `rect` at every stroke width and every display scale.
        half = stroke / 2
        path = QRectF(
            rect.x() + half,
            rect.y() + half,
            rect.width() - stroke,
            rect.height() - stroke,
        )
        if shape == "line":
            painter.setPen(pen)
            painter.drawLine(path.bottomLeft(), path.topRight())
        elif shape == "marquee":
            # The selection marquee reads as marching ants: a dashed outline, not
            # a solid one. It carries the same stroke width as the rest of the
            # set, though - a lighter one rasterized to a single pixel at 100%
            # scaling next to everything else's two, and the rail read as if this
            # glyph had faded.
            # The ants are laid out and filled by hand rather than stroked with a
            # dashed pen. A dashed drawRect runs one phase around the whole
            # perimeter: it ends mid-pattern, so whichever corners it lands on are
            # left bare and the glyph reads lopsided even though its bounding box
            # is centred. Fractional dash lengths then rasterize to uneven runs on
            # top of that. Integer dashes placed per edge avoid both.
            thin = stroke
            length = rect.width()
            # Three dashes and two equal gaps exactly fill an edge (3a + 2g = L),
            # which puts a dash at both ends of every edge — so all four corners
            # ink and the run is a mirror of itself. g is only whole if a shares
            # L's parity, hence the nudge.
            dash = max(1, length // 6)
            if (length - dash) % 2:
                dash = dash + 1 if 3 * (dash + 1) <= length else dash - 1
            dash = max(1, dash)
            gap = (length - 3 * dash) // 2
            near, far = rect.x(), rect.x() + length - thin
            for step in range(3):
                start = rect.x() + step * (dash + gap)
                painter.fillRect(start, near, dash, thin, Qt.GlobalColor.black)
                painter.fillRect(start, far, dash, thin, Qt.GlobalColor.black)
                painter.fillRect(near, start, thin, dash, Qt.GlobalColor.black)
                painter.fillRect(far, start, thin, dash, Qt.GlobalColor.black)
        elif shape == "rect_filled":
            painter.fillRect(rect, Qt.GlobalColor.black)
        elif shape == "ellipse_filled":
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(Qt.GlobalColor.black)
            painter.drawEllipse(rect)
        else:  # "rect" / "ellipse" outlines: keep the stroke within `rect`
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            if shape == "rect":
                painter.drawRect(path)
            else:
                painter.drawEllipse(path)
