"""Direct-color (truecolor) tile codec — pixels are colors, no palette.

The tile-side analogue of the mask-based color codec: each pixel is
``bytes_per_pixel`` bytes decoded to ``0xAARRGGBB`` via component masks/shifts
(``docs/graphics-formats-reference/implementation-guide.md`` §3). It decodes to an
:class:`~celpix.core.argb_grid.ArgbGrid` and skips the palette entirely. The
mask→ARGB kernel is shared with :mod:`celpix.plugins.builtins.color_codec`.

Faithful to Tile Molester's default: the value is read at the configured
``byte_order`` (little by default) and the catalogue masks are applied as-is.

The tile is 8×8 unless ``tile_width``/``tile_height`` say otherwise. Nothing
here is tied to 8 — a pixel is a whole number of bytes, so any tile size walks
the buffer correctly — and truecolor data is exactly what arrives as a bitmap of
some arbitrary width (a TIFF strip, an ILBM row) rather than as console tiles.
That is what lets the view's bitmap width re-cut the grid to a size the width
divides by (``ViewOptions.bitmap_width``). Bit-packed codecs can't follow: a
planar or sub-byte-packed row *is* eight pixels.
"""

from __future__ import annotations

from typing import Any

from celpix.core.argb_grid import ArgbGrid
from celpix.core.context import PipelineContext
from celpix.core.errors import Stage
from celpix.plugins.base import PluginInfo
from celpix.plugins.builtins._bits import merge_planes
from celpix.plugins.builtins._mask import (
    ARGB_BYTE_ORDER,
    decode_tables,
    encode_tables,
    parse_masks,
)
from celpix.plugins.builtins._tile import check_tile_size, require_whole_tiles


class DirectColorCodec:
    """Truecolor tile codec; component masks come from ``params``."""

    info = PluginInfo(
        id="codec.direct-color",
        name="Direct-color (truecolor) tile codec",
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

    @classmethod
    def _tile(cls, params: dict[str, Any]) -> tuple[int, int]:
        width = int(params.get("tile_width", cls.TILE))
        height = int(params.get("tile_height", cls.TILE))
        if width <= 0 or height <= 0:
            raise ValueError(f"tile size must be positive, got {width}x{height}")
        return width, height

    def bytes_per_tile(self, params: dict[str, Any]) -> int:
        w, h = self._tile(params)
        return w * h * self._config(params)[0]

    def tile_size(self, params: dict[str, Any]) -> tuple[int, int]:
        return self._tile(params)

    def decode(
        self, data: bytes, params: dict[str, Any], ctx: PipelineContext
    ) -> list[ArgbGrid]:
        """Whole buffer at a time: one byte plane per component, then split.

        A tile's pixels are contiguous and a pixel's conversion depends on
        nothing but its own bytes, so the whole window converts as four strided
        byte planes (``_mask.decode_tables``) and only the final split into tiles
        happens per tile. A truecolor window is hundreds of thousands of pixels —
        a per-pixel Python loop here is what makes a wide-bitmap view stall.
        """
        bpx, order, masks = self._config(params)
        width, height = self._tile(params)
        tile_bytes = width * height * bpx
        require_whole_tiles(len(data), tile_bytes)
        if not data:
            return []
        pixels = len(data) // bpx
        out = bytearray(pixels * 4)
        tables = decode_tables(tuple(sorted(masks.items())), bpx, order)
        for slot, comp in enumerate(ARGB_BYTE_ORDER):
            planes = [
                data[index::bpx].translate(table)
                for index, table in enumerate(tables[slot])
                if table is not None
            ]
            if planes:
                out[slot::4] = merge_planes(planes)
            elif comp == "a":
                out[slot::4] = b"\xff" * pixels  # no alpha field → opaque
        stride = width * height * 4
        return [
            ArgbGrid(width, height, out[base : base + stride])
            for base in range(0, len(out), stride)
        ]

    def encode(
        self, tiles: list[ArgbGrid], params: dict[str, Any], ctx: PipelineContext
    ) -> bytes:
        """The inverse plane walk: every tile's pixels at once, then interleaved."""
        bpx, order, masks = self._config(params)
        width, height = self._tile(params)
        for t, grid in enumerate(tiles):
            check_tile_size(grid, width, height, t)
        if not tiles:
            return b""
        argb = b"".join(bytes(grid.data) for grid in tiles)
        out = bytearray(len(argb) // 4 * bpx)
        tables = encode_tables(tuple(sorted(masks.items())), bpx, order)
        for index in range(bpx):
            planes = [
                argb[slot::4].translate(table)
                for slot, table in enumerate(tables[index])
                if table is not None
            ]
            if planes:
                out[index::bpx] = merge_planes(planes)
        return bytes(out)
