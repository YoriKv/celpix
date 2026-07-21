"""Data-driven packed (linear) pixel codec — one kernel, order flags are parameters.

In a packed format each pixel index is a sub-byte **field** stored directly (no
planes): an 8-pixel row is `8 / pixels_per_byte` bytes, each byte holding
`pixels_per_byte` adjacent pixels. The universal kernel walks pixels left-to-right;
two per-format knobs place each field
(``docs/graphics-formats-reference/implementation-guide.md`` §2, "Packed / linear"):

- **``msb_first``** — is pixel 0 of a byte in its **high** field or its low field?
  High → GBA's opposite (Genesis/MSX 4bpp high-nibble-left, NGP 2bpp high-bits-first);
  low → GBA 4bpp (low-nibble-left), Virtual Boy 2bpp (low-bits-first).
- **``reverse_bytes``** — read the row's bytes right-to-left. Covers the YY-CHR
  Neo Geo Pocket byte-swap (odd byte drives the left pixels).

So GBA, Genesis/X68000/MSX 4bpp, Virtual Boy, and both Neo Geo Pocket orderings are
each a two-flag parameter set, not code.

Like the planar engine this handles the 8-pixel-wide case (fixed 8×8 tile); wider or
odd-width packed tiles are a later bespoke codec with their own walk.
"""

from __future__ import annotations

from typing import Any

from celpix.core.context import PipelineContext
from celpix.core.errors import Stage
from celpix.core.index_grid import IndexGrid
from celpix.plugins.base import PluginInfo
from celpix.plugins.builtins._tile import check_tile_size, require_whole_tiles


class PackedCodec:
    """Generic packed tile codec; behaviour comes entirely from ``params``."""

    info = PluginInfo(
        id="codec.packed",
        name="Packed (linear) codec",
        stage=Stage.INTERPRET_PIXEL,
    )

    TILE = 8  # the kernel's per-row layout is specific to 8-pixel rows (fixed 8×8)

    @classmethod
    def _geometry(cls, params: dict[str, Any]) -> tuple[int, bool, bool, int, int]:
        bpp = int(params["bpp"])
        if bpp <= 0 or cls.TILE % bpp != 0:
            raise ValueError(f"packed bpp must divide {cls.TILE}: got {bpp}")
        msb_first = bool(params.get("msb_first", False))
        reverse = bool(params.get("reverse_bytes", False))
        pixels_per_byte = cls.TILE // bpp
        tile_bytes = cls.TILE * cls.TILE * bpp // 8
        return bpp, msb_first, reverse, pixels_per_byte, tile_bytes

    def bytes_per_tile(self, params: dict[str, Any]) -> int:
        return self._geometry(params)[4]

    def tile_size(self, params: dict[str, Any]) -> tuple[int, int]:
        return self.TILE, self.TILE

    def _shift(self, pos: int, pixels_per_byte: int, bpp: int, msb_first: bool) -> int:
        # Field position of pixel `pos` (0-based within its byte) → bit shift.
        slot = (pixels_per_byte - 1 - pos) if msb_first else pos
        return slot * bpp

    def decode(
        self, data: bytes, params: dict[str, Any], ctx: PipelineContext
    ) -> list[IndexGrid]:
        bpp, msb_first, reverse, ppb, tile_bytes = self._geometry(params)
        tile = self.TILE
        bytes_per_row = tile // ppb
        mask = (1 << bpp) - 1
        require_whole_tiles(len(data), tile_bytes)

        tiles: list[IndexGrid] = []
        for addr in range(0, len(data), tile_bytes):
            grid = IndexGrid(tile, tile)
            buf = grid.data
            for y in range(tile):
                row = addr + y * bytes_per_row
                out = y * tile
                for x in range(tile):
                    bi = x // ppb
                    if reverse:
                        bi = bytes_per_row - 1 - bi
                    shift = self._shift(x % ppb, ppb, bpp, msb_first)
                    buf[out + x] = (data[row + bi] >> shift) & mask
            tiles.append(grid)
        return tiles

    def encode(
        self, tiles: list[IndexGrid], params: dict[str, Any], ctx: PipelineContext
    ) -> bytes:
        bpp, msb_first, reverse, ppb, tile_bytes = self._geometry(params)
        tile = self.TILE
        bytes_per_row = tile // ppb
        out = bytearray(len(tiles) * tile_bytes)
        for t, grid in enumerate(tiles):
            check_tile_size(grid, tile, tile, t)
            buf = grid.data
            for y in range(tile):
                row = t * tile_bytes + y * bytes_per_row
                src = y * tile
                for x in range(tile):
                    bi = x // ppb
                    if reverse:
                        bi = bytes_per_row - 1 - bi
                    shift = self._shift(x % ppb, ppb, bpp, msb_first)
                    out[row + bi] |= (buf[src + x] << shift) & 0xFF
        return bytes(out)
