"""Wide / odd-tile pixel codecs (16-wide tiles with bespoke intra-tile layouts).

These tiles are 16 pixels wide and/or a non-power-of-two height, so each row is two
8-pixel halves with format-specific byte placement — no shared 8×8 kernel expresses
them (``docs/graphics-formats-reference/implementation-guide.md`` §2, odd/wide-tile
formats). Every tile's bytes are still **contiguous**, so they slot into the deferred
windowed view like any other codec; only the intra-tile walk is custom.

Covered: 1bpp 16×16 / 16×12 (FF5) / 16×11 (FF6); PCE 2bpp 16×16; PCE SG 4bpp 16×16.
"""

from __future__ import annotations

from typing import Any

from celpix.core.context import PipelineContext
from celpix.core.errors import Stage
from celpix.core.index_grid import IndexGrid
from celpix.plugins.base import PluginInfo
from celpix.plugins.builtins._tile import check_tile_size, require_whole_tiles


def _bit(byte: int, x: int) -> int:
    """1bpp planar bit for pixel x (0=leftmost uses MSB)."""
    return (byte >> (7 - x)) & 1


def _pack8(pixels, plane: int) -> int:
    """Collapse 8 pixels' bit `plane` into one byte (MSB = leftmost)."""
    b = 0
    for x in range(8):
        b |= ((pixels[x] >> plane) & 1) << (7 - x)
    return b


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
        tiles: list[IndexGrid] = []
        for addr in range(0, len(data), tile_bytes):
            grid = IndexGrid(16, height)
            buf = grid.data
            for y in range(height):
                left = data[addr + lb + ls * y]
                right = data[addr + rb + rs * y]
                row = y * 16
                for x in range(8):
                    buf[row + x] = _bit(left, x)
                    buf[row + 8 + x] = _bit(right, x)
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
                out[base + lb + ls * y] = _pack8(buf[row : row + 8], 0)
                out[base + rb + rs * y] = _pack8(buf[row + 8 : row + 16], 0)
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
                lp0, lp1 = data[addr + 2 * y], data[addr + 2 * y + 32]
                rp0, rp1 = data[addr + 2 * y + 1], data[addr + 2 * y + 33]
                row = y * 16
                for x in range(8):
                    buf[row + x] = _bit(lp0, x) | (_bit(lp1, x) << 1)
                    buf[row + 8 + x] = _bit(rp0, x) | (_bit(rp1, x) << 1)
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
                out[base + 2 * y] = _pack8(left, 0)
                out[base + 2 * y + 32] = _pack8(left, 1)
                out[base + 2 * y + 1] = _pack8(right, 0)
                out[base + 2 * y + 33] = _pack8(right, 1)
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
                for x in range(8):
                    left = right = 0
                    for p in range(4):
                        block = addr + p * 32
                        left |= _bit(data[block + 2 * y + 1], x) << p  # odd byte = left
                        right |= _bit(data[block + 2 * y], x) << p  # even byte = right
                    buf[row + x] = left
                    buf[row + 8 + x] = right
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
                    out[block + 2 * y + 1] = _pack8(left, p)
                    out[block + 2 * y] = _pack8(right, p)
        return bytes(out)
