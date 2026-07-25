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

Both directions run **plane at a time over the whole buffer** rather than pixel by
pixel. A plane's byte for one tile row sits at a fixed offset inside every tile, so
a single strided slice collects it from every tile at once, and the kernel above is
a 256-entry table (:mod:`celpix.plugins.builtins._bits`) applied to that slice.
"""

from __future__ import annotations

from typing import Any

from celpix.core.context import PipelineContext
from celpix.core.errors import Stage
from celpix.core.index_grid import IndexGrid
from celpix.plugins.base import PluginInfo
from celpix.plugins.builtins._bits import (
    bit_expansion,
    bit_packing,
    merge_planes,
    or_bytes,
)
from celpix.plugins.builtins._tile import check_tile_size, require_whole_tiles


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
    def _geometry(cls, params: dict[str, Any]) -> tuple[int, list[list[int]], int]:
        """``(bpp, per-plane row offsets, bytes per tile)``.

        The plane rules are resolved to the eight byte offsets each plane occupies
        inside a tile, since that is what both directions actually index by. Every
        offset has to land inside the tile: the walks below address a plane's byte
        across all tiles with one strided slice, which only describes the format
        while each tile's bytes stay its own.
        """
        bpp = int(params["bpp"])
        planes = params["planes"]
        if len(planes) != bpp:
            raise ValueError(
                f"planar preset needs one plane per bit: bpp={bpp}, got {len(planes)}"
            )
        tile_bytes = cls.TILE * cls.TILE * bpp // 8
        offsets = [
            [p["base"] + p["stride"] * y for y in range(cls.TILE)] for p in planes
        ]
        for plane, rows in enumerate(offsets):
            for y, off in enumerate(rows):
                if not 0 <= off < tile_bytes:
                    raise ValueError(
                        f"planar plane {plane} row {y} reads byte {off}, "
                        f"outside the {tile_bytes}-byte tile"
                    )
        return bpp, offsets, tile_bytes

    def bytes_per_tile(self, params: dict[str, Any]) -> int:
        _, _, tile_bytes = self._geometry(params)
        return tile_bytes

    def tile_size(self, params: dict[str, Any]) -> tuple[int, int]:
        return self.TILE, self.TILE

    def decode(
        self, data: bytes, params: dict[str, Any], ctx: PipelineContext
    ) -> list[IndexGrid]:
        """Expand every tile's plane bytes at once, one pixel row at a time."""
        bpp, offsets, tile_bytes = self._geometry(params)
        tile = self.TILE
        require_whole_tiles(len(data), tile_bytes)
        if not data:
            return []
        # rows[y] holds pixel row y of every tile, back to back: the OR of one
        # table lookup per plane, each applied to that plane's byte in all tiles.
        rows = [
            merge_planes(
                [
                    b"".join(
                        map(bit_expansion(k).__getitem__, data[offs[y] :: tile_bytes])
                    )
                    for k, offs in enumerate(offsets)
                ]
            )
            for y in range(tile)
        ]
        return [
            IndexGrid(
                tile,
                tile,
                b"".join(row[base : base + tile] for row in rows),
            )
            for base in range(0, len(data) // tile_bytes * tile, tile)
        ]

    def encode(
        self, tiles: list[IndexGrid], params: dict[str, Any], ctx: PipelineContext
    ) -> bytes:
        """The inverse: pack each plane across every tile, then scatter it home."""
        bpp, offsets, tile_bytes = self._geometry(params)
        tile = self.TILE
        for t, grid in enumerate(tiles):
            check_tile_size(grid, tile, tile, t)
        out = bytearray(len(tiles) * tile_bytes)
        if not tiles:
            return bytes(out)
        pixels = b"".join(bytes(grid.data) for grid in tiles)
        for k, offs in enumerate(offsets):
            # One byte per (tile, row): the OR of the eight columns' contributions.
            packed = merge_planes(
                [pixels[x::tile].translate(bit_packing(k, x)) for x in range(tile)]
            )
            for y in range(tile):
                # OR into place rather than assign, so two planes naming one byte
                # both land there exactly as the per-pixel form had them.
                column = out[offs[y] :: tile_bytes]
                out[offs[y] :: tile_bytes] = or_bytes(bytes(column), packed[y::tile])
        return bytes(out)
