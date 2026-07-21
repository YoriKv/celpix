"""Tile arrangement: composing a list of tiles into one viewable image.

The pixel codec decodes to a flat list of tiles; an arrangement lays them out into
a single :class:`IndexGrid`. The MVP ships the simplest — **linear**: tiles fill
left-to-right, top-to-bottom, ``columns`` tiles wide. 2D/wide-bitmap and ADF
remaps are later sub-steps that produce a different composition from the same
tiles.
"""

from __future__ import annotations

from celpix.core.index_grid import IndexGrid


def compose_linear(tiles: list[IndexGrid], columns: int) -> IndexGrid:
    """Lay ``tiles`` into a ``columns``-wide grid image (row-major)."""
    if not tiles:
        return IndexGrid(0, 0)
    cols = max(1, columns)
    tw, th = tiles[0].width, tiles[0].height
    rows = (len(tiles) + cols - 1) // cols
    image = IndexGrid(cols * tw, rows * th)
    stride = image.width
    dst = image.data
    for i, tile in enumerate(tiles):
        base_x = (i % cols) * tw
        base_y = (i // cols) * th
        src = tile.data
        for y in range(th):
            d0 = (base_y + y) * stride + base_x
            s0 = y * tw
            dst[d0 : d0 + tw] = src[s0 : s0 + tw]
    return image
