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
    """One tool's fixed description: how it's shown, and how it behaves.

    ``icon`` and ``shape`` are the panel's two ways to draw a tool button, kept
    here as plain data so this module stays Qt-free. Exactly one is set: ``icon``
    names a bundled monochrome PNG under ``resources/icons`` (tinted to the theme);
    ``shape`` names a primitive the panel paints itself for the geometry tools
    (``"line"``/``"rect"``/``"rect_filled"``/``"ellipse"``/``"ellipse_filled"``/
    ``"marquee"``), so they share one size and padding. ``label`` remains the
    accessible name and tooltip lead.
    """

    tool: Tool
    label: str
    tooltip: str
    key: str  # the bare number key that selects it (1..9)
    gesture: Gesture
    rasterize: Rasterize | None = None
    icon: str | None = None
    shape: str | None = None


# This order is the rail's top-to-bottom order *and* the 1..9 number keys, so a
# tool's key is always its position in the panel — one list rather than a display
# order and a key mapping that can drift apart. The marquee leads, then the pen
# and the two samplers, then the shape tools grouped outline-before-filled.
# Reordering here moves the buttons and renumbers the keys together; only the
# ``key`` strings need to stay 1..9 in sequence.
TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        Tool.SELECT,
        "Select",
        "Select a pixel rectangle; Shift for a square",
        "1",
        Gesture.MARQUEE,
        shape="marquee",
    ),
    ToolSpec(
        Tool.PENCIL,
        "Pencil",
        "Freehand paint",
        "2",
        Gesture.FREEHAND,
        draw.line,
        icon="pencil.png",
    ),
    ToolSpec(
        Tool.EYEDROPPER,
        "Eyedropper",
        "Pick a color; right-click does this on any tool",
        "3",
        Gesture.SAMPLE,
        icon="eyedropper.png",
    ),
    ToolSpec(
        Tool.FILL,
        "Fill",
        "Flood-fill the region under the cursor",
        "4",
        Gesture.FILL,
        icon="paint-bucket.png",
    ),
    ToolSpec(
        Tool.LINE,
        "Line",
        "Draw a line",
        "5",
        Gesture.SHAPE,
        draw.line,
        shape="line",
    ),
    ToolSpec(
        Tool.RECT,
        "Rectangle",
        "Draw a rectangle outline",
        "6",
        Gesture.SHAPE,
        draw.rect_outline,
        shape="rect",
    ),
    ToolSpec(
        Tool.RECT_FILLED,
        "Filled Rectangle",
        "Draw a filled rectangle",
        "7",
        Gesture.SHAPE,
        draw.rect_filled,
        shape="rect_filled",
    ),
    ToolSpec(
        Tool.ELLIPSE,
        "Ellipse",
        "Draw an ellipse outline",
        "8",
        Gesture.SHAPE,
        draw.ellipse_outline,
        shape="ellipse",
    ),
    ToolSpec(
        Tool.ELLIPSE_FILLED,
        "Filled Ellipse",
        "Draw a filled ellipse",
        "9",
        Gesture.SHAPE,
        draw.ellipse_filled,
        shape="ellipse_filled",
    ),
)

# By-member and by-key lookups the panel/controller use instead of re-scanning.
TOOL_SPEC: dict[Tool, ToolSpec] = {spec.tool: spec for spec in TOOL_SPECS}
TOOL_BY_KEY: dict[str, Tool] = {spec.key: spec.tool for spec in TOOL_SPECS}
