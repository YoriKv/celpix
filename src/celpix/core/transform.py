"""Geometric transforms of a decoded grid: flip, 90° rotation, transpose.

These are **content edits** — they rewrite the interpreted pixels, unlike the
byte-nudge that only realigns where tiles start (a display option). Each function
takes a :class:`~celpix.core.grid.PixelGrid` and returns a **new** grid of the
same class, leaving the input untouched (callers snapshot then overwrite).

Everything works on ``grid.data`` in whole pixels of ``bytes_per_pixel``, so one
implementation serves both grid types and the cost is a handful of buffer slices
per row or column rather than a Python call per pixel. That matters because the
callers are not only 8×8 tiles: a marquee transform covers the whole window, and
rendering a rearranged tile map re-orients every visible tile on each repaint.
Qt-free, like the rest of ``core``.
"""

from __future__ import annotations

from typing import TypeVar

from celpix.core.grid import PixelGrid

# The concrete class round-trips through type(grid), so the return matches the
# input exactly.
Grid = TypeVar("Grid", bound=PixelGrid)


def flip_horizontal(grid: Grid) -> Grid:
    """Mirror left↔right: column ``x`` becomes column ``w-1-x``."""
    w, h = grid.width, grid.height
    bpx = grid.bytes_per_pixel
    stride = w * bpx
    src = grid.data
    out = bytearray(len(src))
    for y in range(h):
        row = src[y * stride : (y + 1) * stride]
        if bpx == 1:
            out[y * stride : (y + 1) * stride] = row[::-1]
        else:
            # Reverse pixel order without reversing the bytes *within* a pixel:
            # take each byte lane of the pixel separately and reverse that.
            rev = bytearray(stride)
            for b in range(bpx):
                rev[b::bpx] = row[b::bpx][::-1]
            out[y * stride : (y + 1) * stride] = rev
    return type(grid)(w, h, out)


def flip_vertical(grid: Grid) -> Grid:
    """Mirror top↔bottom: row ``y`` becomes row ``h-1-y``."""
    w, h = grid.width, grid.height
    stride = w * grid.bytes_per_pixel
    src = grid.data
    out = bytearray(len(src))
    for y in range(h):
        d = (h - 1 - y) * stride
        out[d : d + stride] = src[y * stride : (y + 1) * stride]
    return type(grid)(w, h, out)


def transpose(grid: Grid) -> Grid:
    """Reflect across the main diagonal: ``(x, y)`` → ``(y, x)``, so ``h×w`` out.

    No toolbar button performs this one. It is here because it is the *axis swap*
    the two rotations are built from — a quarter turn is a transpose plus a mirror
    — which is what lets the display orientations of
    :mod:`celpix.core.tilemap` be eight combinations of three independent bits
    rather than a table.
    """
    w, h = grid.width, grid.height
    bpx = grid.bytes_per_pixel
    src = grid.data
    out = bytearray(len(src))
    # Source column x is destination row x: gather it with one strided slice per
    # byte lane, stepping a whole source row at a time.
    for x in range(w):
        d0 = x * h * bpx
        for b in range(bpx):
            out[d0 + b : d0 + h * bpx : bpx] = src[x * bpx + b :: w * bpx]
    return type(grid)(h, w, out)


def rotate_cw(grid: Grid) -> Grid:
    """Rotate 90° clockwise. The result is ``h×w`` (dimensions swap)."""
    # Mirror-then-transpose, the same composition tilemap's TILE_ROTATE_CW flag
    # combination stands for, so the two can never disagree about turn direction.
    return transpose(flip_vertical(grid))


def rotate_ccw(grid: Grid) -> Grid:
    """Rotate 90° counter-clockwise. The result is ``h×w`` (dimensions swap)."""
    return transpose(flip_horizontal(grid))
