"""Software rasterization for pixel editing: shapes, flood fill, region copy.

The pixel-mode drawing tools need to turn a mouse gesture into a set of pixels to
paint. That geometry is pure integer math with no palette and no Qt, so it lives
here in ``core`` — testable headless, and reused by the UI's tool controller,
which supplies the color.

Two conventions keep this palette-agnostic:

- The shape functions (:func:`line`, :func:`rect_outline`/:func:`rect_filled`,
  :func:`ellipse_outline`/:func:`ellipse_filled`) return a **list of ``(x, y)``
  coordinates** rather than touching a grid. The caller paints them with whatever
  value the pen holds (an index, or an ARGB for a direct-color view) and clips to
  the grid — so the same routine serves both grid kinds. Coordinates may fall
  outside any particular grid; that is the caller's clip to make.
- :func:`flood_fill` and the region helpers *do* read/write a grid, but only
  through the ``width``/``height``/``get``/``set``/``type(grid)(w, h)`` shape both
  :class:`~celpix.core.index_grid.IndexGrid` and
  :class:`~celpix.core.argb_grid.ArgbGrid` expose, so one implementation covers
  both (the same trick :mod:`celpix.core.transform` uses).
"""

from __future__ import annotations

from typing import TypeVar

Coord = tuple[int, int]
# A grid with the IndexGrid/ArgbGrid interface; regions round-trip through
# type(grid), so a returned grid matches the input's kind exactly.
Grid = TypeVar("Grid")


def line(x0: int, y0: int, x1: int, y1: int) -> list[Coord]:
    """Every pixel on the segment from ``(x0, y0)`` to ``(x1, y1)``, inclusive.

    Bresenham's integer line — the connective tissue of the freehand pen (each
    mouse move draws a line from the last sample so a fast stroke leaves no gaps)
    and the Line tool itself.
    """
    dx = abs(x1 - x0)
    dy = -abs(y1 - y0)
    sx = 1 if x0 < x1 else -1
    sy = 1 if y0 < y1 else -1
    err = dx + dy
    pixels: list[Coord] = []
    while True:
        pixels.append((x0, y0))
        if x0 == x1 and y0 == y1:
            return pixels
        e2 = 2 * err
        if e2 >= dy:
            err += dy
            x0 += sx
        if e2 <= dx:
            err += dx
            y0 += sy


def _bounds(x0: int, y0: int, x1: int, y1: int) -> tuple[int, int, int, int]:
    """Normalise a drag's two corners to ``(left, top, right, bottom)``."""
    if x0 > x1:
        x0, x1 = x1, x0
    if y0 > y1:
        y0, y1 = y1, y0
    return x0, y0, x1, y1


def rect_outline(x0: int, y0: int, x1: int, y1: int) -> list[Coord]:
    """The one-pixel border of the rectangle the drag spans (corners inclusive)."""
    x0, y0, x1, y1 = _bounds(x0, y0, x1, y1)
    pixels: list[Coord] = []
    for x in range(x0, x1 + 1):
        pixels.append((x, y0))
        if y1 != y0:
            pixels.append((x, y1))
    for y in range(y0 + 1, y1):  # sides, corners already covered above
        pixels.append((x0, y))
        if x1 != x0:
            pixels.append((x1, y))
    return pixels


def rect_filled(x0: int, y0: int, x1: int, y1: int) -> list[Coord]:
    """Every pixel inside (and on) the rectangle the drag spans."""
    x0, y0, x1, y1 = _bounds(x0, y0, x1, y1)
    return [(x, y) for y in range(y0, y1 + 1) for x in range(x0, x1 + 1)]


def ellipse_outline(x0: int, y0: int, x1: int, y1: int) -> list[Coord]:
    """The ellipse inscribed in the drag's bounding box, one pixel thick.

    Zingl's integer bounding-box ellipse (a rearranged midpoint algorithm): it
    takes the box corners directly, so it handles even *and* odd extents and
    degenerate thin boxes without a fractional centre. No floats, four-way
    symmetric.
    """
    x0, y0, x1, y1 = _bounds(x0, y0, x1, y1)
    a = x1 - x0
    b = y1 - y0
    b1 = b & 1
    dx = 4 * (1 - a) * b * b
    dy = 4 * (b1 + 1) * a * a
    err = dx + dy + b1 * a * a
    y0 += (b + 1) // 2
    y1 = y0 - b1
    a8 = 8 * a * a
    b8 = 8 * b * b
    pixels: list[Coord] = []
    while x0 <= x1:
        pixels.append((x1, y0))  # quadrant I
        pixels.append((x0, y0))  # quadrant II
        pixels.append((x0, y1))  # quadrant III
        pixels.append((x1, y1))  # quadrant IV
        e2 = 2 * err
        if e2 <= dy:
            y0 += 1
            y1 -= 1
            dy += a8
            err += dy
        if e2 >= dx or 2 * err > dy:
            x0 += 1
            x1 -= 1
            dx += b8
            err += dx
    # Flat ellipses (a≈1) stop the loop early; walk the remaining tips.
    while y0 - y1 <= b:
        pixels.append((x0 - 1, y0))
        pixels.append((x1 + 1, y0))
        y0 += 1
        pixels.append((x0 - 1, y1))
        pixels.append((x1 + 1, y1))
        y1 -= 1
    return pixels


def ellipse_filled(x0: int, y0: int, x1: int, y1: int) -> list[Coord]:
    """The filled ellipse: the outline plus every pixel between its two sides.

    Derived from :func:`ellipse_outline` so the fill lands exactly inside the
    same curve — each scanline runs from the leftmost to the rightmost outline
    pixel on that row.
    """
    spans: dict[int, tuple[int, int]] = {}
    for x, y in ellipse_outline(x0, y0, x1, y1):
        lo, hi = spans.get(y, (x, x))
        spans[y] = (min(lo, x), max(hi, x))
    pixels: list[Coord] = []
    for y, (lo, hi) in spans.items():
        pixels.extend((x, y) for x in range(lo, hi + 1))
    return pixels


def flood_fill(grid: Grid, x: int, y: int) -> list[Coord]:
    """Every pixel of the 4-connected region of ``grid`` that matches ``(x, y)``.

    A scanline seed fill (span-based, not per-pixel recursion, so a large flat
    region can't blow the stack): it reads the value under the seed and returns
    the contiguous run of equal-valued pixels reachable from it. It does not
    mutate — the caller paints the returned pixels with the pen, which is what
    lets a fill be one undoable edit and lets a fill with the same color no-op.
    """
    w, h = grid.width, grid.height
    if not (0 <= x < w and 0 <= y < h):
        return []
    target = grid.get(x, y)
    visited = bytearray(w * h)
    pixels: list[Coord] = []
    stack: list[Coord] = [(x, y)]
    while stack:
        sx, sy = stack.pop()
        if visited[sy * w + sx]:
            continue
        left = sx
        while (
            left > 0
            and not visited[sy * w + left - 1]
            and grid.get(left - 1, sy) == target
        ):
            left -= 1
        right = sx
        while (
            right < w - 1
            and not visited[sy * w + right + 1]
            and grid.get(right + 1, sy) == target
        ):
            right += 1
        for px in range(left, right + 1):
            visited[sy * w + px] = 1
            pixels.append((px, sy))
        # Seed the rows above and below across the whole span just filled.
        for px in range(left, right + 1):
            for ny in (sy - 1, sy + 1):
                if (
                    0 <= ny < h
                    and not visited[ny * w + px]
                    and grid.get(px, ny) == target
                ):
                    stack.append((px, ny))
    return pixels


def extract_region(grid: Grid, x: int, y: int, w: int, h: int) -> Grid:
    """Copy the ``w × h`` block at ``(x, y)`` into a fresh grid of the same kind.

    Pixels of the block that fall outside ``grid`` come back as 0 (the empty
    default), so a marquee dragged partly off the edge still lifts a full-size
    rectangle. The source is left untouched — the floating selection owns the
    copy.
    """
    out = type(grid)(max(0, w), max(0, h))
    for yy in range(h):
        sy = y + yy
        if not (0 <= sy < grid.height):
            continue
        for xx in range(w):
            sx = x + xx
            if 0 <= sx < grid.width:
                out.set(xx, yy, grid.get(sx, sy))
    return out


def blit_region(
    dst: Grid, src: Grid, x: int, y: int, *, transparent: int | None = None
) -> None:
    """Paste ``src`` into ``dst`` at ``(x, y)``, clipped to ``dst``'s bounds.

    In place — how a floating selection stamps down. ``transparent``, when given,
    is a source value to skip (leaving whatever ``dst`` already held), so a
    non-rectangular stamp can preserve the pixels around it; ``None`` copies every
    pixel verbatim.
    """
    for yy in range(src.height):
        dy = y + yy
        if not (0 <= dy < dst.height):
            continue
        for xx in range(src.width):
            dx = x + xx
            if not (0 <= dx < dst.width):
                continue
            value = src.get(xx, yy)
            if transparent is None or value != transparent:
                dst.set(dx, dy, value)
