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

_COMPONENTS = ("a", "r", "g", "b")
_ARGB_SHIFT = {"a": 24, "r": 16, "g": 8, "b": 0}


def _mask_shift_width(mask: int) -> tuple[int, int]:
    """Low-bit position and bit width of a contiguous mask."""
    if mask == 0:
        return 0, 0
    shift = (mask & -mask).bit_length() - 1
    width = bin(mask).count("1")
    return shift, width


def _scale_up(raw: int, width: int) -> int:
    """Scale a ``width``-bit field up to 8 bits by replicating its high bits."""
    if width >= 8:
        return (raw >> (width - 8)) & 0xFF
    return ((raw << (8 - width)) | (raw >> max(0, 2 * width - 8))) & 0xFF


def _scale_down(comp8: int, width: int) -> int:
    """Inverse of :func:`_scale_up`: 8-bit component down to a ``width``-bit field."""
    if width >= 8:
        return comp8 << (width - 8)
    return comp8 >> (8 - width)


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
        # Masks are hex integers in TOML (0x7C00); a component is left out by
        # omitting its key. Strings ("0x7C00") are also accepted for robustness.
        raw_masks: dict[str, Any] = params["masks"]
        masks = {}
        for comp in _COMPONENTS:
            value = raw_masks.get(comp)
            if not value:
                continue
            masks[comp] = int(value, 0) if isinstance(value, str) else int(value)
        return size, order, masks

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
        sw = {comp: _mask_shift_width(m) for comp, m in masks.items()}

        colors: list[int] = []
        for off in range(0, len(data), size):
            value = int.from_bytes(data[off : off + size], order)  # noqa: E203
            argb = 0
            for comp in _COMPONENTS:
                if comp in masks:
                    shift, width = sw[comp]
                    field = (value & masks[comp]) >> shift
                    comp8 = _scale_up(field, width)
                elif comp == "a":
                    comp8 = 0xFF  # no alpha field → opaque
                else:
                    comp8 = 0
                argb |= comp8 << _ARGB_SHIFT[comp]
            colors.append(argb)
        return Palette(colors)

    def encode(
        self, palette: Palette, params: dict[str, Any], ctx: PipelineContext
    ) -> bytes:
        size, order, masks = self._config(params)
        sw = {comp: _mask_shift_width(m) for comp, m in masks.items()}

        out = bytearray()
        for argb in palette.colors:
            value = 0
            for comp, mask in masks.items():
                shift, width = sw[comp]
                comp8 = (argb >> _ARGB_SHIFT[comp]) & 0xFF
                value |= (_scale_down(comp8, width) << shift) & mask
            out += value.to_bytes(size, order)
        return bytes(out)
