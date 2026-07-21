"""Chunky 8bpp pixel codec — one byte per pixel, row-major (a straight copy).

The simplest pixel layout: each byte *is* a palette index, tiles are row-major, so a
tile is a contiguous ``width × height`` byte block
(``docs/graphics-formats-reference/implementation-guide.md`` §2, "Chunky"). Covers
SNES Mode 7, Nintendo DS 2D, generic 8bpp (8×8), and the SNES whole-bank 8bpp view
(a single 128×128 "tile") — the tile size is the only parameter.

Higher-bpp chunky is *direct colour* (truecolor, no palette): a separate concern, not
this index-producing engine.
"""

from __future__ import annotations

from typing import Any

from celpix.core.context import PipelineContext
from celpix.core.errors import Stage
from celpix.core.index_grid import IndexGrid
from celpix.plugins.base import PluginInfo
from celpix.plugins.builtins._tile import check_tile_size, require_whole_tiles


class ChunkyCodec:
    """8bpp chunky codec; the tile's pixel size comes from ``params``."""

    info = PluginInfo(
        id="codec.chunky",
        name="Chunky (8bpp) codec",
        stage=Stage.INTERPRET_PIXEL,
    )

    @staticmethod
    def _geometry(params: dict[str, Any]) -> tuple[int, int]:
        # 8bpp only: one byte per pixel = one index. width/height default to 8×8.
        bpp = int(params.get("bpp", 8))
        if bpp != 8:
            raise ValueError(f"chunky codec is 8bpp (index-per-byte); got {bpp}")
        width = int(params.get("tile_width", 8))
        height = int(params.get("tile_height", 8))
        if width <= 0 or height <= 0:
            raise ValueError(f"chunky tile size must be positive: {width}x{height}")
        return width, height

    def bytes_per_tile(self, params: dict[str, Any]) -> int:
        w, h = self._geometry(params)
        return w * h

    def tile_size(self, params: dict[str, Any]) -> tuple[int, int]:
        return self._geometry(params)

    def decode(
        self, data: bytes, params: dict[str, Any], ctx: PipelineContext
    ) -> list[IndexGrid]:
        w, h = self._geometry(params)
        tile_bytes = w * h
        require_whole_tiles(len(data), tile_bytes)
        # Row-major byte-per-pixel: the tile's bytes are already the index grid.
        return [
            IndexGrid(w, h, data[addr : addr + tile_bytes])
            for addr in range(0, len(data), tile_bytes)
        ]

    def encode(
        self, tiles: list[IndexGrid], params: dict[str, Any], ctx: PipelineContext
    ) -> bytes:
        w, h = self._geometry(params)
        out = bytearray()
        for t, grid in enumerate(tiles):
            check_tile_size(grid, w, h, t)
            out += grid.data
        return bytes(out)
