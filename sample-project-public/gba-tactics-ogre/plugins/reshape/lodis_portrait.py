"""Knight of Lodis portrait layout, ``reshape.lodis-portrait``.

Reorders each 960-byte portrait (30 4bpp tiles) from sprite-piece order to a
5x6 tile grid; a trailing partial portrait is left unchanged. Stored order::

    tiles  0-15   32x32, 4x4 tiles
    tiles 16-19    8x32, 1x4 tiles, right of the 32x32
    tiles 20-27   32x16, 4x2 tiles, below the 32x32
    tiles 28-29    8x16, 1x2 tiles, bottom right
"""

from __future__ import annotations

from celpix.core.context import PipelineContext
from celpix.core.errors import Stage
from celpix.plugins.base import PluginInfo

TILE = 32  # bytes per 4bpp tile
ACROSS = 5  # grid width in tiles
# (first stored tile, width, height, grid column, grid row) of each sprite piece.
_PIECES = ((0, 4, 4, 0, 0), (16, 1, 4, 4, 0), (20, 4, 2, 0, 4), (28, 1, 2, 4, 4))
# _GRID[k]: stored tile at grid cell k (row-major, five across).
_GRID = [0] * 30
# Place each piece's tiles, row-major within the piece, at its grid position.
for _first, _w, _h, _col, _row in _PIECES:
    for _k in range(_w * _h):
        _GRID[(_row + _k // _w) * ACROSS + _col + _k % _w] = _first + _k
PORTRAIT = len(_GRID) * TILE  # 960 bytes


def _permute(data: bytes, order: list[int], *, forward: bool) -> bytes:
    out = bytearray(data)  # a trailing partial portrait stays as is
    # Each whole portrait in turn.
    for base in range(0, len(data) - PORTRAIT + 1, PORTRAIT):
        # Move each tile: stored -> grid cell (forward), or back.
        for cell, stored in enumerate(order):
            src, dst = (stored, cell) if forward else (cell, stored)
            out[base + dst * TILE : base + (dst + 1) * TILE] = data[
                base + src * TILE : base + (src + 1) * TILE
            ]
    return bytes(out)


class LodisPortrait:
    info = PluginInfo(
        id="reshape.lodis-portrait",
        name="Knight of Lodis portrait (sprite pieces to 5x6 tiles)",
        stage=Stage.RESHAPE,
    )

    def reshape(self, data: bytes, ctx: PipelineContext) -> bytes:
        # Load: sprite-piece order -> 5x6 grid.
        return _permute(data, _GRID, forward=True)

    def unshape(self, data: bytes, ctx: PipelineContext) -> bytes:
        # Save: 5x6 grid -> sprite-piece order.
        return _permute(data, _GRID, forward=False)


def register(registry):
    registry.register(LodisPortrait())
