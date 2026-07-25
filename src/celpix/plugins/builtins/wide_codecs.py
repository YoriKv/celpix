"""Wide / odd-tile pixel codecs (16-wide tiles with bespoke intra-tile layouts).

These tiles are 16 pixels wide and/or a non-power-of-two height, so each row is two
8-pixel halves with format-specific byte placement — no *preset* over the 8×8 planar
engine expresses that walk (``docs/graphics-formats-reference/implementation-guide.md``
§2, odd/wide-tile formats). Every tile's bytes are still **contiguous**, so they slot
into the deferred windowed view like any other codec; only the intra-tile walk is
custom, and each half-row still goes through the shared planar kernel
(:func:`~celpix.plugins.builtins._bits.expand_row` / :func:`.pack_row`) rather than
its own bit loop.

That kernel is applied a row at a time here, not over the whole buffer as the 8×8
engines do: a plane's bytes sit at offsets these formats each choose differently,
so there is no single stride to gather every tile's copy of one byte along.

Covered: 1bpp 16×16 / 16×12 (FF5) / 16×11 (FF6); PCE 2bpp 16×16; PCE SG 4bpp 16×16.
"""

from __future__ import annotations

from typing import Any

from celpix.core.context import PipelineContext
from celpix.core.errors import Stage
from celpix.core.index_grid import IndexGrid
from celpix.plugins.base import PluginInfo
from celpix.plugins.builtins._bits import bit_expansion, expand_row, pack_row
from celpix.plugins.builtins._tile import check_tile_size, require_whole_tiles


class Wide1bppCodec:
    """1bpp 16-wide tiles; ``mode`` selects the row byte placement + height."""

    info = PluginInfo(
        id="codec.wide-1bpp",
        name="1bpp wide-tile codec (16xN)",
        stage=Stage.INTERPRET_PIXEL,
    )

    # mode -> (height, (left_base, left_stride), (right_base, right_stride))
    _MODES = {
        "halves": (16, (0, 2), (1, 2)),  # 16x16: byte0=left, byte1=right
        "ff5": (12, (0, 1), (12, 1)),  # 16x12 (FF5): columns 12 bytes apart
        "ff6": (11, (1, 2), (0, 2)),  # 16x11 (FF6): byte-swapped pair per row
    }

    @classmethod
    def _mode(cls, params: dict[str, Any]):
        mode = params.get("mode", "halves")
        if mode not in cls._MODES:
            raise ValueError(f"unknown wide-1bpp mode {mode!r}")
        return cls._MODES[mode]

    def bytes_per_tile(self, params: dict[str, Any]) -> int:
        height = self._mode(params)[0]
        return 2 * height  # 16 wide * height * 1bpp / 8

    def tile_size(self, params: dict[str, Any]) -> tuple[int, int]:
        return 16, self._mode(params)[0]

    def decode(
        self, data: bytes, params: dict[str, Any], ctx: PipelineContext
    ) -> list[IndexGrid]:
        height, (lb, ls), (rb, rs) = self._mode(params)
        tile_bytes = 2 * height
        require_whole_tiles(len(data), tile_bytes)
        expand = bit_expansion(0)  # 1bpp: one plane, so a byte *is* eight pixels
        tiles: list[IndexGrid] = []
        for addr in range(0, len(data), tile_bytes):
            grid = IndexGrid(16, height)
            buf = grid.data
            for y in range(height):
                row = y * 16
                buf[row : row + 8] = expand[data[addr + lb + ls * y]]
                buf[row + 8 : row + 16] = expand[data[addr + rb + rs * y]]
            tiles.append(grid)
        return tiles

    def encode(
        self, tiles: list[IndexGrid], params: dict[str, Any], ctx: PipelineContext
    ) -> bytes:
        height, (lb, ls), (rb, rs) = self._mode(params)
        tile_bytes = 2 * height
        out = bytearray(len(tiles) * tile_bytes)
        for t, grid in enumerate(tiles):
            check_tile_size(grid, 16, height, t)
            buf = grid.data
            base = t * tile_bytes
            for y in range(height):
                row = y * 16
                out[base + lb + ls * y] = pack_row(buf[row : row + 8], 0)
                out[base + rb + rs * y] = pack_row(buf[row + 8 : row + 16], 0)
        return bytes(out)


class Pce2bpp16Codec:
    """PC Engine 2bpp 16×16 sprite tiles (64 bytes; two planes, halves interleaved)."""

    info = PluginInfo(
        id="codec.pce-2bpp16",
        name="PC Engine 2bpp 16x16",
        stage=Stage.INTERPRET_PIXEL,
    )

    def bytes_per_tile(self, params: dict[str, Any]) -> int:
        return 64

    def tile_size(self, params: dict[str, Any]) -> tuple[int, int]:
        return 16, 16

    def decode(
        self, data: bytes, params: dict[str, Any], ctx: PipelineContext
    ) -> list[IndexGrid]:
        require_whole_tiles(len(data), 64)
        tiles: list[IndexGrid] = []
        for addr in range(0, len(data), 64):
            grid = IndexGrid(16, 16)
            buf = grid.data
            for y in range(16):
                row = y * 16
                buf[row : row + 8] = expand_row(
                    [data[addr + 2 * y], data[addr + 2 * y + 32]]
                )
                buf[row + 8 : row + 16] = expand_row(
                    [data[addr + 2 * y + 1], data[addr + 2 * y + 33]]
                )
            tiles.append(grid)
        return tiles

    def encode(
        self, tiles: list[IndexGrid], params: dict[str, Any], ctx: PipelineContext
    ) -> bytes:
        out = bytearray(len(tiles) * 64)
        for t, grid in enumerate(tiles):
            check_tile_size(grid, 16, 16, t)
            buf = grid.data
            base = t * 64
            for y in range(16):
                left = buf[y * 16 : y * 16 + 8]
                right = buf[y * 16 + 8 : y * 16 + 16]
                out[base + 2 * y] = pack_row(left, 0)
                out[base + 2 * y + 32] = pack_row(left, 1)
                out[base + 2 * y + 1] = pack_row(right, 0)
                out[base + 2 * y + 33] = pack_row(right, 1)
        return bytes(out)


class PceSgCodec:
    """PC Engine SG 4bpp 16×16 sprite tiles (128 bytes; 4 plane blocks 32B apart)."""

    info = PluginInfo(
        id="codec.pce-sg",
        name="PC Engine SG 4bpp 16x16",
        stage=Stage.INTERPRET_PIXEL,
    )

    def bytes_per_tile(self, params: dict[str, Any]) -> int:
        return 128

    def tile_size(self, params: dict[str, Any]) -> tuple[int, int]:
        return 16, 16

    def decode(
        self, data: bytes, params: dict[str, Any], ctx: PipelineContext
    ) -> list[IndexGrid]:
        require_whole_tiles(len(data), 128)
        tiles: list[IndexGrid] = []
        for addr in range(0, len(data), 128):
            grid = IndexGrid(16, 16)
            buf = grid.data
            for y in range(16):
                row = y * 16
                # Odd byte of each plane block = left half, even byte = right.
                buf[row : row + 8] = expand_row(
                    [data[addr + p * 32 + 2 * y + 1] for p in range(4)]
                )
                buf[row + 8 : row + 16] = expand_row(
                    [data[addr + p * 32 + 2 * y] for p in range(4)]
                )
            tiles.append(grid)
        return tiles

    def encode(
        self, tiles: list[IndexGrid], params: dict[str, Any], ctx: PipelineContext
    ) -> bytes:
        out = bytearray(len(tiles) * 128)
        for t, grid in enumerate(tiles):
            check_tile_size(grid, 16, 16, t)
            buf = grid.data
            base = t * 128
            for y in range(16):
                left = buf[y * 16 : y * 16 + 8]
                right = buf[y * 16 + 8 : y * 16 + 16]
                for p in range(4):
                    block = base + p * 32
                    out[block + 2 * y + 1] = pack_row(left, p)
                    out[block + 2 * y] = pack_row(right, p)
        return bytes(out)
