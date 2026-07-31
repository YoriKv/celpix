"""Data-driven mask-based color codec — native palette entry ⇄ ARGB.

A color codec converts one native palette entry to and from ``0xAARRGGBB`` using
component **masks** (``docs/graphics-formats-reference/implementation-guide.md``
§4). Each entry is ``bytes_per_entry`` bytes read with ``byte_order`` into an
integer, then each component is sliced by its contiguous mask and scaled to 8
bits. As with the planar codec, a new color format — BGR555, RGB888, RGB565, … —
is a data file.

The round trip is exact: a ``w``-bit field decodes to 8 bits by replicating its
high bits (``raw << (8-w) | raw >> (2w-8)``) and re-encodes by ``comp >> (8-w)``,
recovering the original field, and unused bits stay 0.
"""

from __future__ import annotations

from typing import Any

from celpix.core.context import PipelineContext
from celpix.core.errors import Stage
from celpix.core.palette import Palette
from celpix.plugins.base import PluginInfo
from celpix.plugins.builtins._mask import (
    argb_to_value,
    parse_masks,
    shift_widths,
    value_to_argb,
)


class ColorCodec:
    """Generic mask-based color codec; behaviour comes from ``params``."""

    info = PluginInfo(
        id="codec.palette.mask",
        name="Mask-based color codec",
        stage=Stage.INTERPRET_PALETTE,
    )

    @staticmethod
    def _config(params: dict[str, Any]) -> tuple[int, str, dict[str, int]]:
        # Validated here rather than per entry point, so encode and
        # bytes_per_entry reject an unusable preset with decode's message.
        size = int(params["bytes_per_entry"])
        if size <= 0:
            raise ValueError("bytes_per_entry must be positive")
        order = params.get("byte_order", "little")
        return size, order, parse_masks(params["masks"])

    def decode(
        self, data: bytes, params: dict[str, Any], ctx: PipelineContext
    ) -> Palette:
        size, order, masks = self._config(params)
        if len(data) % size != 0:
            raise ValueError(
                f"palette length {len(data)} is not a multiple of entry size {size}"
            )
        sw = shift_widths(masks)
        return Palette(
            [
                value_to_argb(int.from_bytes(data[off : off + size], order), masks, sw)
                for off in range(0, len(data), size)
            ]
        )

    def encode(
        self, palette: Palette, params: dict[str, Any], ctx: PipelineContext
    ) -> bytes:
        size, order, masks = self._config(params)
        sw = shift_widths(masks)
        out = bytearray()
        for argb in palette.colors:
            out += argb_to_value(argb, masks, sw).to_bytes(size, order)
        return bytes(out)

    def bytes_per_entry(self, params: dict[str, Any]) -> int:
        return self._config(params)[0]
