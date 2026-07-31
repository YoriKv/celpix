"""Data-driven planar pixel codec — one kernel, every planar format is parameters.

In a planar format each bit of a pixel's index comes from a *separate byte*
("plane"). For an 8-pixel row, pixel ``x`` (0 = leftmost) uses bit ``7 - x`` of
each plane (MSB = leftmost). The universal kernel is
(``docs/graphics-formats-reference/implementation-guide.md`` §1):

    decode:  index[x] = Σ_k ((plane[k] >> (7 - x)) & 1) << k
    encode:  plane[k] |= ((index[x] >> k) & 1) << (7 - x)

The **only** thing that varies between planar formats is which byte each plane is
read from. A preset supplies that as a per-plane linear rule

    offset(k, y, g) = base[k] + stride[k] * y + group_stride[k] * g

where *g* indexes the eight-pixel **group** across the row (``g = x // 8``). The
``7 - x`` bit rule is what fixes the group at eight pixels wide, but nothing fixes
how many groups a row has, so ``tile_width``/``tile_height`` are parameters and a
row is ``width / 8`` groups. At the 8×8 default there is one group, ``group_stride``
never multiplies anything, and the rule collapses to ``base[k] + stride[k] * y``.

Between them those three terms express every planar layout we document — GB, SNES,
NES, SMS, the 16-wide arcade tiles, and the odd sizes whose halves sit at a
format-specific distance (``group_stride`` may be negative, which is how a tile
storing its right half *first* is written). So a new planar format is a data file.

Both directions run **plane at a time over the whole buffer** rather than pixel by
pixel. A plane's byte for one tile row and group sits at a fixed offset inside every
tile, so one strided slice collects it from every tile at once and the kernel above
is a 256-entry table (:mod:`celpix.plugins.builtins._bits`) applied to that slice.
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
        id="codec.pixel.planar",
        name="Planar (bitplane) codec",
        stage=Stage.INTERPRET_PIXEL,
    )

    # The "bit 7-x = pixel x" rule is what makes eight pixels the engine's atomic
    # unit; the *tile* is a whole number of those groups and is a preset field.
    # Displaying tiles grouped into larger units stays a *view* option rather than
    # a decode parameter — the same codec serves games with different groupings
    # (docs/design/overview.md §4, decode axes vs display axes). A 16-wide tile is
    # not that: its halves interleave inside the tile's own bytes.
    GROUP = 8

    @classmethod
    def _geometry(
        cls, params: dict[str, Any]
    ) -> tuple[int, list[list[list[int]]], int, int, int, int]:
        """``(bpp, offsets[plane][row][group], tile bytes, width, height, groups)``.

        The plane rules resolve to the byte offsets each plane occupies inside a
        tile, which is what both directions index by. Every offset has to land
        inside the tile: the walks below address a plane's byte across all tiles
        with one strided slice, which only describes the format while each tile's
        bytes stay its own.
        """
        bpp = int(params["bpp"])
        planes = params["planes"]
        width = int(params.get("tile_width", cls.GROUP))
        height = int(params.get("tile_height", cls.GROUP))
        if len(planes) != bpp:
            raise ValueError(
                f"planar preset needs one plane per bit: bpp={bpp}, got {len(planes)}"
            )
        if width <= 0 or height <= 0:
            raise ValueError(f"planar tile size must be positive: {width}x{height}")
        if width % cls.GROUP:
            raise ValueError(
                f"planar tile_width must be a multiple of {cls.GROUP} "
                f"(a pixel is one bit of one byte): got {width}"
            )
        groups = width // cls.GROUP
        tile_bytes = width * height * bpp // 8
        offsets = [
            [
                [
                    p["base"] + p["stride"] * y + p.get("group_stride", 0) * g
                    for g in range(groups)
                ]
                for y in range(height)
            ]
            for p in planes
        ]
        for plane, rows in enumerate(offsets):
            for y, row in enumerate(rows):
                for g, off in enumerate(row):
                    if not 0 <= off < tile_bytes:
                        raise ValueError(
                            f"planar plane {plane} row {y} group {g} reads byte "
                            f"{off}, outside the {tile_bytes}-byte tile"
                        )
        return bpp, offsets, tile_bytes, width, height, groups

    def bytes_per_tile(self, params: dict[str, Any]) -> int:
        return self._geometry(params)[2]

    def tile_size(self, params: dict[str, Any]) -> tuple[int, int]:
        _bpp, _offs, _tb, width, height, _g = self._geometry(params)
        return width, height

    def decode(
        self, data: bytes, params: dict[str, Any], ctx: PipelineContext
    ) -> list[IndexGrid]:
        """Expand every tile's plane bytes at once, one pixel row at a time."""
        bpp, offsets, tile_bytes, width, height, groups = self._geometry(params)
        require_whole_tiles(len(data), tile_bytes)
        if not data:
            return []
        count = len(data) // tile_bytes
        rows = []
        for y in range(height):
            # One group's eight pixels, for every tile: the OR of one table lookup
            # per plane, each applied to that plane's byte in all tiles at once.
            chunks = [
                or_all(
                    [
                        b"".join(
                            map(
                                bit_expansion(k).__getitem__,
                                data[offsets[k][y][g] :: tile_bytes],
                            )
                        )
                        for k in range(bpp)
                    ]
                )
                for g in range(groups)
            ]
            if groups == 1:
                # The whole row already, in tile order — the common 8-wide case,
                # kept free of the interleave below.
                rows.append(chunks[0])
                continue
            # Groups arrive tile-major; the strided writes lace them into pixel
            # order so rows[y] is again row y of every tile back to back.
            row_buf = bytearray(count * width)
            for g, chunk in enumerate(chunks):
                for i in range(self.GROUP):
                    row_buf[g * self.GROUP + i :: width] = chunk[i :: self.GROUP]
            rows.append(bytes(row_buf))
        return tiles_from_rows(rows, width, height, count)

    def encode(
        self, tiles: list[IndexGrid], params: dict[str, Any], ctx: PipelineContext
    ) -> bytes:
        """The inverse: pack each plane across every tile, then scatter it home."""
        bpp, offsets, tile_bytes, width, height, groups = self._geometry(params)
        pixels = flatten_tiles(tiles, width, height)
        out = bytearray(len(tiles) * tile_bytes)
        if not tiles:
            return bytes(out)
        group = self.GROUP
        for k in range(bpp):
            for g in range(groups):
                # One byte per (tile, row): the OR of this group's eight columns.
                packed = or_all(
                    [
                        pixels[g * group + i :: width].translate(bit_packing(k, i))
                        for i in range(group)
                    ]
                )
                for y in range(height):
                    # OR into place rather than assign, so two planes naming one
                    # byte both land there as the per-pixel form would have them.
                    off = offsets[k][y][g]
                    column = out[off::tile_bytes]
                    out[off::tile_bytes] = or_bytes(bytes(column), packed[y::height])
        return bytes(out)
