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
- :func:`flood_fill` reads a grid (it returns the region's coordinates; the
  caller does the painting) and the region helpers copy between two, but both do
  so only through the :class:`~celpix.core.grid.PixelGrid` shape, so one
  implementation covers index and direct-color grids alike.
"""

from __future__ import annotations

from typing import TypeVar

from celpix.core.grid import PixelGrid

Coord = tuple[int, int]
# Regions round-trip through type(grid), so a returned grid matches the input's
# kind exactly.
Grid = TypeVar("Grid", bound=PixelGrid)


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


def flood_fill(
    grid: Grid, x: int, y: int, bounds: tuple[int, int, int, int] | None = None
) -> list[Coord]:
    """Every pixel of the 4-connected region of ``grid`` that matches ``(x, y)``.

    A scanline seed fill (span-based, not per-pixel recursion, so a large flat
    region can't blow the stack): it reads the value under the seed and returns
    the contiguous run of equal-valued pixels reachable from it. It does not
    mutate — the caller paints the returned pixels with the pen, which is what
    lets a fill be one undoable edit and lets a fill with the same color no-op.

    ``bounds`` (inclusive ``(x0, y0, x1, y1)``, clamped to the grid) confines the
    spread — what a selection does to a fill. The region is what is reachable
    *within* the box, so a shape that leaves it and comes back doesn't drag the
    part outside in, and a seed outside it fills nothing at all.
    """
    w, h = grid.width, grid.height
    x0, y0, x1, y1 = bounds if bounds is not None else (0, 0, w - 1, h - 1)
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(w - 1, x1), min(h - 1, y1)
    if not (x0 <= x <= x1 and y0 <= y <= y1):
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
            left > x0
            and not visited[sy * w + left - 1]
            and grid.get(left - 1, sy) == target
        ):
            left -= 1
        right = sx
        while (
            right < x1
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
                    y0 <= ny <= y1
                    and not visited[ny * w + px]
                    and grid.get(px, ny) == target
                ):
                    stack.append((px, ny))
    return pixels


def copy_rect(
    dst: Grid, dx: int, dy: int, src: Grid, sx: int, sy: int, w: int, h: int
) -> None:
    """Copy a ``w × h`` block from ``src`` at ``(sx, sy)`` to ``dst`` at ``(dx, dy)``.

    Clipped against both grids, then copied a whole row at a time — a lifted
    selection can be the entire window, and every rearrange or marquee drag
    re-lifts and re-stamps it as the cursor moves, so the per-row slice store
    rather than a per-pixel loop is what keeps a drag smooth.
    """
    left = max(0, -dx, -sx)
    right = min(w, dst.width - dx, src.width - sx)
    top = max(0, -dy, -sy)
    bottom = min(h, dst.height - dy, src.height - sy)
    if right <= left or bottom <= top:
        return
    bpx = dst.bytes_per_pixel
    src_buf, dst_buf = src.data, dst.data
    span = (right - left) * bpx
    for row in range(top, bottom):
        s0 = ((sy + row) * src.width + sx + left) * bpx
        d0 = ((dy + row) * dst.width + dx + left) * bpx
        dst_buf[d0 : d0 + span] = src_buf[s0 : s0 + span]


def extract_region(grid: Grid, x: int, y: int, w: int, h: int) -> Grid:
    """Copy the ``w × h`` block at ``(x, y)`` into a fresh grid of the same kind.

    Pixels of the block that fall outside ``grid`` come back as 0 (the empty
    default), so a marquee dragged partly off the edge still lifts a full-size
    rectangle. The source is left untouched — the floating selection owns the
    copy.
    """
    out = type(grid)(max(0, w), max(0, h))
    copy_rect(out, 0, 0, grid, x, y, w, h)
    return out


def clear_region(grid: Grid, x: int, y: int, w: int, h: int) -> None:
    """Zero the ``w × h`` block at ``(x, y)``, clipped to ``grid``.

    Zero is empty for both grid kinds — index 0, and transparent black — so
    lifting a selection out of the picture is a blit from a fresh grid rather
    than a per-pixel walk. That matters because a *move* float re-blanks its
    source rectangle on every repaint while it is in the air.
    """
    copy_rect(grid, x, y, type(grid)(max(0, w), max(0, h)), 0, 0, w, h)


def blit_region(dst: Grid, src: Grid, x: int, y: int) -> None:
    """Paste ``src`` into ``dst`` at ``(x, y)``, clipped to ``dst``'s bounds.

    In place — how a floating selection stamps down.
    """
    copy_rect(dst, x, y, src, 0, 0, src.width, src.height)
