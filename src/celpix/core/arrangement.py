"""Tile arrangement: composing a list of tiles into one viewable image.

The pixel codec decodes to a flat list of tiles; an arrangement lays them out into
a single :class:`IndexGrid`. The MVP ships the simplest — **linear**: tiles fill
left-to-right, top-to-bottom, ``columns`` tiles wide. 2D/wide-bitmap and ADF
remaps are later sub-steps that produce a different composition from the same
tiles.

Large files are viewed through a **window** (:func:`compose_window`): only a fixed
band of rows starting at a tile offset is composed, so the cost of laying out and
rendering is bounded by the window, not the file. The full tile list stays the
model — decode and save are unaffected; only what reaches the canvas is windowed.
"""

from __future__ import annotations

from celpix.core.index_grid import IndexGrid


def compose_linear(tiles: list, columns: int):
    """Lay ``tiles`` into a ``columns``-wide grid image (row-major).

    Returns a grid of the same type as the input tiles (index or direct-colour).
    """
    if not tiles:
        return IndexGrid(0, 0)
    cols = max(1, columns)
    tw, th = tiles[0].width, tiles[0].height
    rows = (len(tiles) + cols - 1) // cols
    return _compose(tiles, cols, tw, th, first_tile=0, rows=rows)


def compose_window(tiles: list, columns: int, first_tile: int, rows: int):
    """Lay out ``rows`` rows of ``columns`` tiles starting at tile ``first_tile``.

    The image is always ``columns`` × ``rows`` tiles so the canvas size stays stable
    while navigating; slots outside ``tiles`` (a partial window at the file end, or a
    negative ``first_tile``) are left blank. Returns a grid of the same type as the
    input tiles. Composing only the visible band is what keeps viewing large files
    cheap — see the module docstring.
    """
    if not tiles:
        return IndexGrid(0, 0)
    cols = max(1, columns)
    rows = max(1, rows)
    tw, th = tiles[0].width, tiles[0].height
    return _compose(tiles, cols, tw, th, first_tile=first_tile, rows=rows)


def _compose(
    tiles: list,
    cols: int,
    tw: int,
    th: int,
    *,
    first_tile: int,
    rows: int,
):
    """Blit ``cols`` × ``rows`` tiles from ``first_tile`` into one grid, row-major.

    Slots whose tile index falls outside ``tiles`` stay blank, so both a full layout
    and a partial window at the file's ends share one code path. Works for either
    grid type — index (1 byte/pixel) or direct-colour ARGB (4 bytes/pixel) — by
    blitting in units of the tiles' ``bytes_per_pixel`` and building the output grid
    of the same type.
    """
    bpx = tiles[0].bytes_per_pixel
    image = type(tiles[0])(cols * tw, rows * th)
    dst = image.data
    dst_stride = cols * tw * bpx
    src_stride = tw * bpx
    row_bytes = tw * bpx
    for slot in range(cols * rows):
        idx = first_tile + slot
        if idx < 0 or idx >= len(tiles):
            continue
        base_x = (slot % cols) * tw
        base_y = (slot // cols) * th
        src = tiles[idx].data
        for y in range(th):
            d0 = (base_y + y) * dst_stride + base_x * bpx
            s0 = y * src_stride
            dst[d0 : d0 + row_bytes] = src[s0 : s0 + row_bytes]
    return image
