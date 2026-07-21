"""Direct-colour (truecolor) tile codec — pixels are colours, no palette.

The tile-side analogue of the mask-based colour codec: each pixel is
``bytes_per_pixel`` bytes decoded to ``0xAARRGGBB`` via component masks/shifts
(``docs/graphics-formats-reference/implementation-guide.md`` §3). It decodes to an
:class:`~celpix.core.argb_grid.ArgbGrid` and skips the palette entirely. The
mask→ARGB kernel is shared with :mod:`celpix.plugins.builtins.color_codec`.

Faithful to Tile Molester's default: the value is read at the configured
``byte_order`` (little by default) and the catalogue masks are applied as-is.
"""

from __future__ import annotations

from typing import Any

from celpix.core.argb_grid import ArgbGrid
from celpix.core.context import PipelineContext
from celpix.core.errors import Stage
from celpix.plugins.base import PluginInfo
from celpix.plugins.builtins._mask import (
    argb_to_value,
    parse_masks,
    shift_widths,
    value_to_argb,
)
from celpix.plugins.builtins._tile import check_tile_size, require_whole_tiles


class DirectColorCodec:
    """Truecolor tile codec; component masks come from ``params``."""

    info = PluginInfo(
        id="codec.direct-color",
        name="Direct-colour (truecolor) tile codec",
        stage=Stage.INTERPRET_PIXEL,
    )

    TILE = 8

    @staticmethod
    def _config(params: dict[str, Any]) -> tuple[int, str, dict[str, int]]:
        bpx = int(params["bytes_per_pixel"])
        if bpx <= 0:
            raise ValueError("bytes_per_pixel must be positive")
        order = params.get("byte_order", "little")
        return bpx, order, parse_masks(params["masks"])

    def bytes_per_tile(self, params: dict[str, Any]) -> int:
        return self.TILE * self.TILE * self._config(params)[0]

    def tile_size(self, params: dict[str, Any]) -> tuple[int, int]:
        return self.TILE, self.TILE

    def decode(
        self, data: bytes, params: dict[str, Any], ctx: PipelineContext
    ) -> list[ArgbGrid]:
        bpx, order, masks = self._config(params)
        sw = shift_widths(masks)
        tile = self.TILE
        tile_bytes = tile * tile * bpx
        require_whole_tiles(len(data), tile_bytes)
        tiles: list[ArgbGrid] = []
        for addr in range(0, len(data), tile_bytes):
            grid = ArgbGrid(tile, tile)
            buf = grid.data
            pos = addr
            for i in range(tile * tile):
                value = int.from_bytes(data[pos : pos + bpx], order)
                pos += bpx
                argb = value_to_argb(value, masks, sw)
                buf[i * 4 : i * 4 + 4] = (argb & 0xFFFFFFFF).to_bytes(4, "little")
            tiles.append(grid)
        return tiles

    def encode(
        self, tiles: list[ArgbGrid], params: dict[str, Any], ctx: PipelineContext
    ) -> bytes:
        bpx, order, masks = self._config(params)
        sw = shift_widths(masks)
        tile = self.TILE
        out = bytearray()
        for t, grid in enumerate(tiles):
            check_tile_size(grid, tile, tile, t)
            buf = grid.data
            for i in range(tile * tile):
                argb = int.from_bytes(buf[i * 4 : i * 4 + 4], "little")
                out += argb_to_value(argb, masks, sw).to_bytes(bpx, order)
        return bytes(out)
