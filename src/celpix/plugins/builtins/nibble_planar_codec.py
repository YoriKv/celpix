"""Nibble-planar pixel codec — a byte holds two bitplanes of four pixels.

Neither planar nor packed. A planar format gives each plane its own byte and a
packed one gives each pixel its own field; this family splits the difference:
**one byte carries four pixels, its high nibble holding one bitplane of those four
and its low nibble the next**, bit 3 of a nibble being the leftmost pixel. A byte
is 8 bits of picture either way, but a pixel's two bits sit four bits apart in one
byte rather than in two different bytes.

    index[i] = ((byte >> (7 - i)) & 1) << hi | ((byte >> (3 - i)) & 1) << lo

Depths past 2bpp add more bytes per group of four pixels, most significant plane
pair first: at 4bpp the first byte of a group carries index bits 3 and 2, the
second bits 1 and 0. That is the Atari System 2 encoding, used at three geometries
across its library (``docs/graphics-formats-reference/mame-formats.md`` §3), and
the same shape recurs on Atari's other raster boards.

**The deeper formats need their region joined first.** On the hardware a 4bpp
tile's two plane pairs come from opposite halves of the graphics region rather
than from adjacent bytes, so a 4bpp preset expects a buffer
``reshape.split-planes-2`` has already interleaved
(:mod:`celpix.plugins.builtins.split_planes`). After that a group's bytes *are*
adjacent and this codec stays buffer-relative, keeping windowed decoding of a
large file working. 2bpp formats carry both planes in one byte and need no such
step.

Any even depth from 2 to 8 works, and any tile width that is a whole number of
four-pixel groups: nothing in the kernel is tied to a size, unlike the planar and
packed engines whose per-row layout assumes eight pixels. Both directions run
**group byte at a time over the whole buffer** — a given byte of a given group
sits at a fixed offset inside every tile, so one strided slice collects it from
all of them and a 256-entry table (:mod:`celpix.plugins.builtins._bits`) does the
shuffle in C.
"""

from __future__ import annotations

from typing import Any

from celpix.core.context import PipelineContext
from celpix.core.errors import Stage
from celpix.core.index_grid import IndexGrid
from celpix.plugins._byteops import or_all
from celpix.plugins.base import PluginInfo
from celpix.plugins.builtins._bits import (
    nibble_plane_expansion,
    nibble_plane_packing,
)
from celpix.plugins.builtins._tile import (
    flatten_tiles,
    require_whole_tiles,
    tiles_from_rows,
)

# Four pixels share a byte, two bitplanes to a nibble. Both are properties of the
# encoding rather than of any one format, so they are constants, not parameters.
PIXELS_PER_GROUP = 4
PLANES_PER_BYTE = 2


class NibblePlanarGeometry:
    """Resolved geometry of one nibble-planar preset."""

    __slots__ = ("bpp", "group_bytes", "groups_per_row", "height", "width")

    def __init__(self, params: dict[str, Any]) -> None:
        self.bpp = int(params["bpp"])
        self.width = int(params.get("tile_width", 8))
        self.height = int(params.get("tile_height", 8))
        if self.bpp <= 0 or self.bpp % PLANES_PER_BYTE != 0 or self.bpp > 8:
            raise ValueError(
                f"nibble-planar bpp must be an even 2..8 (two planes per byte): "
                f"got {self.bpp}"
            )
        if self.width <= 0 or self.width % PIXELS_PER_GROUP != 0:
            raise ValueError(
                f"nibble-planar tile_width must be a multiple of {PIXELS_PER_GROUP}: "
                f"got {self.width}"
            )
        if self.height <= 0:
            raise ValueError(
                f"nibble-planar tile_height must be positive: {self.height}"
            )
        self.group_bytes = self.bpp // PLANES_PER_BYTE
        self.groups_per_row = self.width // PIXELS_PER_GROUP

    @property
    def row_bytes(self) -> int:
        return self.groups_per_row * self.group_bytes

    @property
    def tile_bytes(self) -> int:
        return self.row_bytes * self.height

    def offset(self, y: int, group: int, group_byte: int) -> int:
        """Byte offset inside a tile of ``group_byte`` of ``group`` on row ``y``.

        Groups run left to right along the row and each group's bytes are
        adjacent, which the split-planes join arranges for the deeper depths.
        """
        return y * self.row_bytes + group * self.group_bytes + group_byte


class NibblePlanarCodec:
    """Nibble-planar tile codec; geometry and depth come entirely from ``params``."""

    info = PluginInfo(
        id="codec.pixel.nibble-planar",
        name="Nibble-planar codec (two planes per byte)",
        stage=Stage.INTERPRET_PIXEL,
    )

    def bytes_per_tile(self, params: dict[str, Any]) -> int:
        return NibblePlanarGeometry(params).tile_bytes

    def tile_size(self, params: dict[str, Any]) -> tuple[int, int]:
        geo = NibblePlanarGeometry(params)
        return geo.width, geo.height

    def decode(
        self, data: bytes, params: dict[str, Any], ctx: PipelineContext
    ) -> list[IndexGrid]:
        geo = NibblePlanarGeometry(params)
        require_whole_tiles(len(data), geo.tile_bytes)
        if not data:
            return []
        count = len(data) // geo.tile_bytes
        rows = [self._decode_row(data, geo, y, count) for y in range(geo.height)]
        return tiles_from_rows(rows, geo.width, geo.height, count)

    @staticmethod
    def _decode_row(
        data: bytes, geo: NibblePlanarGeometry, y: int, count: int
    ) -> bytes:
        """Pixel row ``y`` of every tile, back to back."""
        row = bytearray(count * geo.width)
        for group in range(geo.groups_per_row):
            # A group's four pixels are the OR of one table lookup per group byte,
            # each applied to that byte in all tiles at once.
            pixels = or_all(
                [
                    b"".join(
                        map(
                            nibble_plane_expansion(geo.bpp, gb).__getitem__,
                            data[geo.offset(y, group, gb) :: geo.tile_bytes],
                        )
                    )
                    for gb in range(geo.group_bytes)
                ]
            )
            # Four bytes per tile; scatter them into the group's columns.
            base = group * PIXELS_PER_GROUP
            for pos in range(PIXELS_PER_GROUP):
                row[base + pos :: geo.width] = pixels[pos::PIXELS_PER_GROUP]
        return bytes(row)

    def encode(
        self, tiles: list[IndexGrid], params: dict[str, Any], ctx: PipelineContext
    ) -> bytes:
        geo = NibblePlanarGeometry(params)
        pixels = flatten_tiles(tiles, geo.width, geo.height)
        out = bytearray(len(tiles) * geo.tile_bytes)
        if not tiles:
            return bytes(out)
        stride = geo.width * geo.height  # one tile's worth of pixels
        for y in range(geo.height):
            for group in range(geo.groups_per_row):
                base = y * geo.width + group * PIXELS_PER_GROUP
                for gb in range(geo.group_bytes):
                    # One byte per tile: the OR of the four pixels' contributions.
                    packed = or_all(
                        [
                            pixels[base + pos :: stride].translate(
                                nibble_plane_packing(geo.bpp, gb, pos)
                            )
                            for pos in range(PIXELS_PER_GROUP)
                        ]
                    )
                    out[geo.offset(y, group, gb) :: geo.tile_bytes] = packed
        return bytes(out)
