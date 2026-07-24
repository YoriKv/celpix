"""The pixel-editing interaction model: the edit mode and the drawing tools.

This is the single source of truth that the tools panel (the buttons), the canvas
(how it reads the mouse), and the pixel-edit controller (what a gesture does) all
read, so a new tool is added in one place. It is UI-layer but Qt-free — pure data
plus a reference to the Qt-free rasterizer in :mod:`celpix.core.draw` — so the
controller can drive a tool without going through a widget.

``EditMode`` is the top-level switch: ``TILE`` is Celpix's original
tile-granular editing (selection, clipboard, transforms over whole tiles);
``PIXEL`` turns the canvas into a paint surface where these tools act on
individual pixels.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from celpix.core import draw


class EditMode(Enum):
    """Tile-granular editing (the default) vs. pixel painting. ``value`` is the
    stable string persisted in app settings, like :class:`SelectionShape`."""

    TILE = "tile"
    PIXEL = "pixel"


class Gesture(Enum):
    """How a tool reads a mouse gesture — which drives the canvas/controller.

    - ``FREEHAND`` — paint under the pointer, connecting samples (the pen).
    - ``SHAPE`` — anchor on press, rubber-band a shape to the pointer with a live
      preview, commit on release (line, rectangles, ellipses).
    - ``FILL`` — a single click floods the region under it.
    - ``SAMPLE`` — a click picks the color under it (the eyedropper); no edit.
    - ``MARQUEE`` — drag a pixel rectangle for the floating selection.
    """

    FREEHAND = "freehand"
    SHAPE = "shape"
    FILL = "fill"
    SAMPLE = "sample"
    MARQUEE = "marquee"


class Tool(Enum):
    """The drawing tools available in pixel mode. ``value`` is the settings key."""

    PENCIL = "pencil"
    LINE = "line"
    RECT = "rect"
    RECT_FILLED = "rect_filled"
    ELLIPSE = "ellipse"
    ELLIPSE_FILLED = "ellipse_filled"
    FILL = "fill"
    EYEDROPPER = "eyedropper"
    SELECT = "select"


# The pixel rasterizer a SHAPE (or freehand) tool uses: two corners → the pixels
# to paint. ``None`` for tools that don't rasterize a shape (fill/sample/marquee).
Rasterize = Callable[[int, int, int, int], list[tuple[int, int]]]


@dataclass(frozen=True)
class ToolSpec:
    """One tool's fixed description: how it's shown, and how it behaves."""

    tool: Tool
    label: str
    tooltip: str
    key: str  # the bare number key that selects it (1..9)
    gesture: Gesture
    rasterize: Rasterize | None = None


# Registration order = display order in the panel and the number-key mapping
# (1..9). The pen and eyedropper come first (the two most-used), then the shape
# tools grouped outline-before-filled, then fill and the selection marquee.
TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        Tool.PENCIL,
        "Pencil",
        "Freehand: left-drag paints the active color, connecting fast strokes",
        "1",
        Gesture.FREEHAND,
        draw.line,
    ),
    ToolSpec(
        Tool.LINE,
        "Line",
        "Drag a straight line from press to release",
        "2",
        Gesture.SHAPE,
        draw.line,
    ),
    ToolSpec(
        Tool.RECT,
        "Rectangle",
        "Drag a rectangle outline",
        "3",
        Gesture.SHAPE,
        draw.rect_outline,
    ),
    ToolSpec(
        Tool.RECT_FILLED,
        "Filled Rectangle",
        "Drag a filled rectangle",
        "4",
        Gesture.SHAPE,
        draw.rect_filled,
    ),
    ToolSpec(
        Tool.ELLIPSE,
        "Ellipse",
        "Drag an ellipse outline, inscribed in the box you drag",
        "5",
        Gesture.SHAPE,
        draw.ellipse_outline,
    ),
    ToolSpec(
        Tool.ELLIPSE_FILLED,
        "Filled Ellipse",
        "Drag a filled ellipse",
        "6",
        Gesture.SHAPE,
        draw.ellipse_filled,
    ),
    ToolSpec(
        Tool.FILL,
        "Fill",
        "Flood-fill the contiguous same-color region under the click",
        "7",
        Gesture.FILL,
    ),
    ToolSpec(
        Tool.EYEDROPPER,
        "Eyedropper",
        "Pick the color under the click (also right-click on any tool)",
        "8",
        Gesture.SAMPLE,
    ),
    ToolSpec(
        Tool.SELECT,
        "Select",
        "Drag a pixel rectangle to lift a floating selection (drag it, Esc to drop)",
        "9",
        Gesture.MARQUEE,
    ),
)

# By-member and by-key lookups the panel/controller use instead of re-scanning.
TOOL_SPEC: dict[Tool, ToolSpec] = {spec.tool: spec for spec in TOOL_SPECS}
TOOL_BY_KEY: dict[str, Tool] = {spec.key: spec.tool for spec in TOOL_SPECS}
