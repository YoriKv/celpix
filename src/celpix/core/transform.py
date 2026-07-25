"""Geometric transforms of a decoded tile: flip, 90° rotation, transpose.

These are **content edits** — they rewrite the interpreted pixels, unlike the
byte-nudge that only realigns where tiles start (a display option). Each function
takes an :class:`~celpix.core.index_grid.IndexGrid` or
:class:`~celpix.core.argb_grid.ArgbGrid` and returns a **new** grid of the same
class, leaving the input untouched (callers snapshot then overwrite).

Both grids expose the same shape — a ``(width, height)`` constructor and
``get(x, y)`` / ``set(x, y, value)`` — so one implementation serves both with no
special-casing; ``type(grid)(w, h)`` builds the empty result of the right kind.
Qt-free, like the rest of ``core``. Tiles are small (typically 8×8), so plain
loops are more than fast enough.
"""

from __future__ import annotations

from typing import TypeVar

# Any grid with the IndexGrid/ArgbGrid interface; the concrete class round-trips
# through type(grid), so the return matches the input exactly.
Grid = TypeVar("Grid")


def flip_horizontal(grid: Grid) -> Grid:
    """Mirror left↔right: column ``x`` becomes column ``w-1-x``."""
    w, h = grid.width, grid.height
    out = type(grid)(w, h)
    for y in range(h):
        for x in range(w):
            out.set(x, y, grid.get(w - 1 - x, y))
    return out


def flip_vertical(grid: Grid) -> Grid:
    """Mirror top↔bottom: row ``y`` becomes row ``h-1-y``."""
    w, h = grid.width, grid.height
    out = type(grid)(w, h)
    for y in range(h):
        for x in range(w):
            out.set(x, y, grid.get(x, h - 1 - y))
    return out


def rotate_cw(grid: Grid) -> Grid:
    """Rotate 90° clockwise. The result is ``h×w`` (dimensions swap)."""
    w, h = grid.width, grid.height
    out = type(grid)(h, w)
    for y in range(h):
        for x in range(w):
            # (x, y) → (h-1-y, x): the top row becomes the right column.
            out.set(h - 1 - y, x, grid.get(x, y))
    return out


def rotate_ccw(grid: Grid) -> Grid:
    """Rotate 90° counter-clockwise. The result is ``h×w`` (dimensions swap)."""
    w, h = grid.width, grid.height
    out = type(grid)(h, w)
    for y in range(h):
        for x in range(w):
            # (x, y) → (y, w-1-x): the top row becomes the left column.
            out.set(y, w - 1 - x, grid.get(x, y))
    return out


def transpose(grid: Grid) -> Grid:
    """Reflect across the main diagonal: ``(x, y)`` → ``(y, x)``, so ``h×w`` out.

    No toolbar button performs this one. It is here because it is the *axis swap*
    the two rotations are built from — a quarter turn is a transpose plus a mirror
    — which is what lets the display orientations of
    :mod:`celpix.core.tilemap` be eight combinations of three independent bits
    rather than a table.
    """
    w, h = grid.width, grid.height
    out = type(grid)(h, w)
    for y in range(h):
        for x in range(w):
            out.set(y, x, grid.get(x, y))
    return out
