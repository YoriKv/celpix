"""Data-driven mask-based colour codec — native palette entry ⇄ ARGB.

A colour codec converts one native palette entry to/from ``0xAARRGGBB`` using
component **masks** (see
``docs/graphics-formats-reference/implementation-guide.md`` §4). Each entry is
``bytes_per_entry`` bytes read with ``byte_order`` into an integer, then each
component is sliced by its (contiguous) mask and scaled to 8 bits. As with the
planar codec, a new colour format — BGR555, RGB888, RGB565, … — is a data file,
not code.

Round-trip is exact: a field of ``w`` bits decodes to 8 bits by replicating its
high bits (``raw << (8-w) | raw >> (2w-8)``) and re-encodes by ``comp >> (8-w)``,
which recovers the original field precisely, and unused bits stay 0.
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
    """Generic mask-based colour codec; behaviour comes from ``params``."""

    info = PluginInfo(
        id="codec.color-mask",
        name="Mask-based colour codec",
        stage=Stage.INTERPRET_PALETTE,
    )

    @staticmethod
    def _config(params: dict[str, Any]) -> tuple[int, str, dict[str, int]]:
        size = int(params["bytes_per_entry"])
        order = params.get("byte_order", "little")
        return size, order, parse_masks(params["masks"])

    def decode(
        self, data: bytes, params: dict[str, Any], ctx: PipelineContext
    ) -> Palette:
        size, order, masks = self._config(params)
        if size <= 0:
            raise ValueError("bytes_per_entry must be positive")
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
        return int(params["bytes_per_entry"])
