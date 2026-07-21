"""Bespoke linear-packed pixel codecs — 3bpp (LN98) and 6bpp (LN99).

These pack indices whose bit-depth doesn't divide 8, so the fields straddle byte
boundaries in a fixed, format-specific pattern (no shared kernel expresses them —
``docs/graphics-formats-reference/implementation-guide.md`` §2, "Packed / linear").
The exact bit maps are transcribed from Tile Molester's ``_3BPPLinearTileCodec`` /
``_6BPPLinearTileCodec``; 6bpp additionally reads each row's bytes in reverse order.
Fixed 8×8.
"""

from __future__ import annotations

from typing import Any

from celpix.core.context import PipelineContext
from celpix.core.errors import Stage
from celpix.core.index_grid import IndexGrid
from celpix.plugins.base import PluginInfo
from celpix.plugins.builtins._tile import check_tile_size, require_whole_tiles


class LinearBespokeCodec:
    """3bpp/6bpp linear codec; ``bpp`` selects the fixed packing."""

    info = PluginInfo(
        id="codec.linear-bespoke",
        name="Bespoke linear codec (3bpp/6bpp)",
        stage=Stage.INTERPRET_PIXEL,
    )

    TILE = 8

    @classmethod
    def _bpp(cls, params: dict[str, Any]) -> int:
        bpp = int(params["bpp"])
        if bpp not in (3, 6):
            raise ValueError(f"linear-bespoke codec supports bpp 3 or 6, got {bpp}")
        return bpp

    def bytes_per_tile(self, params: dict[str, Any]) -> int:
        return self.TILE * self.TILE * self._bpp(params) // 8

    def tile_size(self, params: dict[str, Any]) -> tuple[int, int]:
        return self.TILE, self.TILE

    @staticmethod
    def _decode_row3(b1: int, b2: int, b3: int) -> list[int]:
        return [
            (b1 >> 5) & 7,
            (b1 >> 2) & 7,
            ((b1 & 3) << 1) | ((b2 >> 7) & 1),
            (b2 >> 4) & 7,
            (b2 >> 1) & 7,
            ((b2 & 1) << 2) | ((b3 >> 6) & 3),
            (b3 >> 3) & 7,
            b3 & 7,
        ]

    @staticmethod
    def _encode_row3(p: list[int]) -> list[int]:
        b1 = (p[0] & 7) << 5 | (p[1] & 7) << 2 | (p[2] >> 1) & 3
        b2 = (p[2] & 1) << 7 | (p[3] & 7) << 4 | (p[4] & 7) << 1 | (p[5] >> 2) & 1
        b3 = (p[5] & 3) << 6 | (p[6] & 7) << 3 | (p[7] & 7)
        return [b1, b2, b3]

    @staticmethod
    def _decode_row6(b1: int, b2: int, b3: int, b4: int, b5: int, b6: int) -> list[int]:
        # The caller reads the row's bytes in reverse (byte0 -> b6 … byte5 -> b1).
        return [
            (b1 >> 2) & 63,
            ((b1 & 3) << 4) | ((b2 >> 4) & 15),
            ((b2 & 15) << 2) | ((b3 >> 6) & 3),
            b3 & 63,
            (b4 >> 2) & 63,
            ((b4 & 3) << 4) | ((b5 >> 4) & 15),
            ((b5 & 15) << 2) | ((b6 >> 6) & 3),
            b6 & 63,
        ]

    @staticmethod
    def _encode_row6(p: list[int]) -> list[int]:
        b1 = (p[0] & 63) << 2 | (p[1] & 48) >> 4
        b2 = (p[1] & 15) << 4 | (p[2] & 60) >> 2
        b3 = (p[2] & 3) << 6 | (p[3] & 63)
        b4 = (p[4] & 63) << 2 | (p[5] & 48) >> 4
        b5 = (p[5] & 15) << 4 | (p[6] & 60) >> 2
        b6 = (p[6] & 3) << 6 | (p[7] & 63)
        return [b6, b5, b4, b3, b2, b1]  # written in reverse byte order

    def decode(
        self, data: bytes, params: dict[str, Any], ctx: PipelineContext
    ) -> list[IndexGrid]:
        bpp = self._bpp(params)
        tile = self.TILE
        row_bytes = bpp
        tile_bytes = tile * row_bytes
        require_whole_tiles(len(data), tile_bytes)
        tiles: list[IndexGrid] = []
        for addr in range(0, len(data), tile_bytes):
            grid = IndexGrid(tile, tile)
            buf = grid.data
            for y in range(tile):
                row = addr + y * row_bytes
                rb = list(data[row : row + row_bytes])
                pixels = (
                    self._decode_row3(*rb)
                    if bpp == 3
                    else self._decode_row6(*reversed(rb))
                )
                buf[y * tile : y * tile + tile] = bytes(pixels)
            tiles.append(grid)
        return tiles

    def encode(
        self, tiles: list[IndexGrid], params: dict[str, Any], ctx: PipelineContext
    ) -> bytes:
        bpp = self._bpp(params)
        tile = self.TILE
        out = bytearray()
        for t, grid in enumerate(tiles):
            check_tile_size(grid, tile, tile, t)
            buf = grid.data
            for y in range(tile):
                p = list(buf[y * tile : y * tile + tile])
                out += bytes(self._encode_row3(p) if bpp == 3 else self._encode_row6(p))
        return bytes(out)
