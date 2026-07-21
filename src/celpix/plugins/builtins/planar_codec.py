"""Data-driven planar pixel codec — one kernel, every planar format is parameters.

In a planar format each bit of a pixel's index comes from a *separate byte*
("plane"). For an 8-pixel row, pixel ``x`` (0 = leftmost) uses bit ``7 - x`` of
each plane (MSB = leftmost). The universal kernel is
(``docs/graphics-formats-reference/implementation-guide.md`` §1):

    decode:  index[x] = Σ_k ((plane[k] >> (7 - x)) & 1) << k
    encode:  plane[k] |= ((index[x] >> k) & 1) << (7 - x)

The **only** thing that varies between planar formats is which byte each plane is
read from on a given row. A preset supplies that as a per-plane linear rule
``offset(k, y) = base[k] + stride[k] * y`` — which expresses every planar layout in
the reference catalogue (GB, SNES, NES, SMS, …). So a new planar format is a data
file, not code.

This engine handles the 8-pixel-wide case (the ``7 - x`` bit rule is specific to
8-wide rows); wider/odd planar tiles are a later sub-step with their own kernel.
"""

from __future__ import annotations

from typing import Any

from celpix.core.context import PipelineContext
from celpix.core.errors import Stage
from celpix.core.index_grid import IndexGrid
from celpix.plugins.base import PluginInfo


class PlanarCodec:
    """Generic planar tile codec; behaviour comes entirely from ``params``."""

    info = PluginInfo(
        id="codec.planar",
        name="Planar (bitplane) codec",
        stage=Stage.INTERPRET_PIXEL,
    )

    # The planar kernel's "bit 7-x = pixel x" rule is specific to 8-pixel rows, and
    # every planar format this kernel expresses is 8x8 (wider/odd tiles need a
    # bespoke code plugin). So the atomic tile is the engine's *fixed unit*, not a
    # preset field — a preset is only (bpp, plane offsets). Displaying tiles grouped
    # into larger units is a *view* option, not a decode parameter, because the same
    # codec is reused across games with different groupings (docs/design/overview.md
    # §4, decode axes vs display axes).
    TILE = 8

    @classmethod
    def _geometry(cls, params: dict[str, Any]) -> tuple[int, list[dict], int]:
        bpp = int(params["bpp"])
        planes = params["planes"]
        if len(planes) != bpp:
            raise ValueError(
                f"planar preset needs one plane per bit: bpp={bpp}, got {len(planes)}"
            )
        tile_bytes = cls.TILE * cls.TILE * bpp // 8
        return bpp, planes, tile_bytes

    def bytes_per_tile(self, params: dict[str, Any]) -> int:
        _, _, tile_bytes = self._geometry(params)
        return tile_bytes

    def tile_size(self, params: dict[str, Any]) -> tuple[int, int]:
        return self.TILE, self.TILE

    def decode(
        self, data: bytes, params: dict[str, Any], ctx: PipelineContext
    ) -> list[IndexGrid]:
        bpp, planes, tile_bytes = self._geometry(params)
        tile = self.TILE
        if len(data) % tile_bytes != 0:
            raise ValueError(
                f"data length {len(data)} is not a multiple of tile size {tile_bytes}"
            )

        tiles: list[IndexGrid] = []
        for addr in range(0, len(data), tile_bytes):
            grid = IndexGrid(tile, tile)
            buf = grid.data
            for y in range(tile):
                plane_bytes = [data[addr + p["base"] + p["stride"] * y] for p in planes]
                row = y * tile
                for x in range(tile):
                    shift = 7 - x
                    idx = 0
                    for k in range(bpp):
                        idx |= ((plane_bytes[k] >> shift) & 1) << k
                    buf[row + x] = idx
            tiles.append(grid)
        return tiles

    def encode(
        self, tiles: list[IndexGrid], params: dict[str, Any], ctx: PipelineContext
    ) -> bytes:
        bpp, planes, tile_bytes = self._geometry(params)
        tile = self.TILE
        out = bytearray(len(tiles) * tile_bytes)
        for t, grid in enumerate(tiles):
            if grid.width != tile or grid.height != tile:
                raise ValueError(
                    f"tile {t} is {grid.width}x{grid.height}, expected {tile}x{tile}"
                )
            addr = t * tile_bytes
            buf = grid.data
            for y in range(tile):
                row = y * tile
                for x in range(tile):
                    shift = 7 - x
                    idx = buf[row + x]
                    for k in range(bpp):
                        if (idx >> k) & 1:
                            out[addr + planes[k]["base"] + planes[k]["stride"] * y] |= (
                                1 << shift
                            )
        return bytes(out)
