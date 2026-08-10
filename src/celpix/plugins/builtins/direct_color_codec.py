"""Direct-color (truecolor) tile codec — pixels are colors, no palette.

The tile-side analogue of the palette color codec: each pixel is
``bytes_per_pixel`` bytes decoded to ``0xAARRGGBB`` through the preset's
``fields`` bit layout, read at the configured ``byte_order`` (little by
default). ``v`` there is an **intensity** field driving R, G and B together, for
the textures that store one channel and mean grey. It decodes to an
:class:`~celpix.core.argb_grid.ArgbGrid`, skipping the palette entirely, and
shares the mask→ARGB kernel with :mod:`celpix.plugins.builtins.color_codec`
(``docs/graphics-formats-reference/implementation-guide.md`` §3).

The tile is 8×8 unless ``tile_width``/``tile_height`` say otherwise. Nothing here
is tied to 8: a pixel is a whole number of bytes, so any tile size walks the
buffer correctly, and truecolor data typically arrives as a bitmap of arbitrary
width (a TIFF strip, an ILBM row) rather than as console tiles. That is what lets
the view's bitmap width re-cut the grid to a size the width divides
(``ViewOptions.bitmap_width``). Bit-packed codecs can't follow, a planar or
sub-byte-packed row *being* eight pixels.
"""

from __future__ import annotations

from typing import Any

from celpix.core.argb_grid import ArgbGrid
from celpix.core.context import PipelineContext
from celpix.core.errors import Stage
from celpix.plugins._byteops import or_all
from celpix.plugins.base import PluginInfo
from celpix.plugins.builtins._fields import bit_width
from celpix.plugins.builtins._mask import (
    ARGB_BYTE_LAYOUT,
    GRAY,
    color_masks,
    decode_tables,
    encode_tables,
)
from celpix.plugins.builtins._tile import flatten_tiles, require_whole_tiles


class DirectColorCodec:
    """Truecolor tile codec; the component layout comes from ``params``."""

    info = PluginInfo(
        id="codec.pixel.direct-color",
        name="Direct-color (truecolor) tile codec",
        stage=Stage.INTERPRET_PIXEL,
    )

    TILE = 8

    @staticmethod
    def _config(params: dict[str, Any]) -> tuple[int, str, dict[str, tuple[int, ...]]]:
        text = params.get("fields")
        if "bytes_per_pixel" in params:
            bpx = int(params["bytes_per_pixel"])
        elif isinstance(text, str):
            # The layout already says how wide a pixel is; one that is not a
            # whole number of bytes could not be strided over the buffer.
            width = bit_width(text)
            if width % 8:
                raise ValueError(
                    f"a pixel has to be a whole number of bytes, and the layout "
                    f"describes {width} bits"
                )
            bpx = width // 8
        else:
            raise ValueError("a pixel needs a width - give bytes_per_pixel")
        if bpx <= 0:
            raise ValueError("bytes_per_pixel must be positive")
        order = params.get("byte_order", "little")
        masks = color_masks(params, bpx * 8)
        shade = masks.pop(GRAY, None)
        if shade is not None:
            # One field driving all three channels: the pixel is grey, so the
            # round trip is exact for any grey the format can hold. A colour
            # that is not grey cannot be stored and collapses to the OR of the
            # three on write, which is what the format leaves us.
            masks |= {"r": shade, "g": shade, "b": shade}
        return bpx, order, masks

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

        A tile's pixels are contiguous and a pixel's conversion depends on nothing
        but its own bytes, so the whole window converts as four strided byte
        planes (``_mask.decode_tables``) and only the split into tiles happens per
        tile. A truecolor window is hundreds of thousands of pixels, where a
        per-pixel Python loop would stall a wide-bitmap view.
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
        for slot, comp in enumerate(ARGB_BYTE_LAYOUT):
            planes = [
                data[index::bpx].translate(table)
                for index, table in enumerate(tables[slot])
                if table is not None
            ]
            if planes:
                out[slot::4] = or_all(planes)
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
        argb = flatten_tiles(tiles, width, height)
        if not tiles:
            return b""
        out = bytearray(len(argb) // 4 * bpx)
        tables = encode_tables(tuple(sorted(masks.items())), bpx, order)
        for index in range(bpx):
            planes = [
                argb[slot::4].translate(table)
                for slot, table in enumerate(tables[index])
                if table is not None
            ]
            if planes:
                out[index::bpx] = or_all(planes)
        return bytes(out)
