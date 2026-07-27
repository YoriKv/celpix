"""Data-driven planar pixel codec — one kernel, every planar format is parameters.

In a planar format each bit of a pixel's index comes from a *separate byte*
("plane"). For an 8-pixel row, pixel ``x`` (0 = leftmost) uses bit ``7 - x`` of
each plane (MSB = leftmost). The universal kernel is
(``docs/graphics-formats-reference/implementation-guide.md`` §1):

    decode:  index[x] = Σ_k ((plane[k] >> (7 - x)) & 1) << k
    encode:  plane[k] |= ((index[x] >> k) & 1) << (7 - x)

The **only** thing that varies between planar formats is which byte each plane is
read from on a given row. A preset supplies that as a per-plane linear rule
``offset(k, y) = base[k] + stride[k] * y``, which expresses every planar layout we
document (GB, SNES, NES, SMS, …), so a new planar format is a data file.

This engine handles the 8-pixel-wide case, the ``7 - x`` bit rule being specific
to 8-wide rows; wider and odd planar tiles go through
:mod:`celpix.plugins.builtins.wide_codecs`.

Both directions run **plane at a time over the whole buffer** rather than pixel by
pixel. A plane's byte for one tile row sits at a fixed offset inside every tile, so
one strided slice collects it from every tile at once and the kernel above is a
256-entry table (:mod:`celpix.plugins.builtins._bits`) applied to that slice.
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
    or_all,
    or_bytes,
)
from celpix.plugins.builtins._tile import (
    flatten_tiles,
    require_whole_tiles,
    tiles_from_rows,
)


class PlanarCodec:
    """Generic planar tile codec; behaviour comes entirely from ``params``."""

    info = PluginInfo(
        id="codec.planar",
        name="Planar (bitplane) codec",
        stage=Stage.INTERPRET_PIXEL,
    )

    # The "bit 7-x = pixel x" rule is specific to 8-pixel rows and every planar
    # format this kernel expresses is 8x8, so the atomic tile is the engine's
    # fixed unit rather than a preset field — a preset is only (bpp, plane
    # offsets). Displaying tiles grouped into larger units is a *view* option, not
    # a decode parameter: the same codec serves games with different groupings
    # (docs/design/overview.md §4, decode axes vs display axes).
    TILE = 8

    @classmethod
    def _geometry(cls, params: dict[str, Any]) -> tuple[int, list[list[int]], int]:
        """``(bpp, per-plane row offsets, bytes per tile)``.

        The plane rules resolve to the eight byte offsets each plane occupies
        inside a tile, which is what both directions index by. Every offset has to
        land inside the tile: the walks below address a plane's byte across all
        tiles with one strided slice, which only describes the format while each
        tile's bytes stay its own.
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
            or_all(
                [
                    b"".join(
                        map(bit_expansion(k).__getitem__, data[offs[y] :: tile_bytes])
                    )
                    for k, offs in enumerate(offsets)
                ]
            )
            for y in range(tile)
        ]
        return tiles_from_rows(rows, tile, tile, len(data) // tile_bytes)

    def encode(
        self, tiles: list[IndexGrid], params: dict[str, Any], ctx: PipelineContext
    ) -> bytes:
        """The inverse: pack each plane across every tile, then scatter it home."""
        bpp, offsets, tile_bytes = self._geometry(params)
        tile = self.TILE
        pixels = flatten_tiles(tiles, tile, tile)
        out = bytearray(len(tiles) * tile_bytes)
        if not tiles:
            return bytes(out)
        for k, offs in enumerate(offsets):
            # One byte per (tile, row): the OR of the eight columns' contributions.
            packed = or_all(
                [pixels[x::tile].translate(bit_packing(k, x)) for x in range(tile)]
            )
            for y in range(tile):
                # OR into place rather than assign, so two planes naming one byte
                # both land there as the per-pixel form would have them.
                column = out[offs[y] :: tile_bytes]
                out[offs[y] :: tile_bytes] = or_bytes(bytes(column), packed[y::tile])
        return bytes(out)
