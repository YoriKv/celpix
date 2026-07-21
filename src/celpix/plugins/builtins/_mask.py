"""Shared mask-based colour kernel: a native value ⇄ ``0xAARRGGBB``.

One integer's contiguous R/G/B/A fields are sliced by component **mask**, scaled to
8 bits (high-bit replication, so the round-trip is exact at the field's precision),
and packed into ARGB. Used by both the palette-side
:class:`~celpix.plugins.builtins.color_codec.ColorCodec` and the tile-side
:class:`~celpix.plugins.builtins.direct_color_codec.DirectColorCodec` — the palette
entry and the direct-colour pixel are the same problem
(``docs/graphics-formats-reference/implementation-guide.md`` §4).
"""

from __future__ import annotations

from typing import Any

COMPONENTS = ("a", "r", "g", "b")
_ARGB_SHIFT = {"a": 24, "r": 16, "g": 8, "b": 0}


def mask_shift_width(mask: int) -> tuple[int, int]:
    """Low-bit position and bit width of a contiguous mask."""
    if mask == 0:
        return 0, 0
    shift = (mask & -mask).bit_length() - 1
    return shift, bin(mask).count("1")


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


def parse_masks(raw_masks: dict[str, Any]) -> dict[str, int]:
    """Read the ``masks`` param into ``{component: mask}``, skipping absent ones.

    Masks are hex integers in TOML (``0x7C00``); strings (``"0x7C00"``) are also
    accepted for robustness. A component is left out by omitting its key.
    """
    masks: dict[str, int] = {}
    for comp in COMPONENTS:
        value = raw_masks.get(comp)
        if not value:
            continue
        masks[comp] = int(value, 0) if isinstance(value, str) else int(value)
    return masks


def shift_widths(masks: dict[str, int]) -> dict[str, tuple[int, int]]:
    return {comp: mask_shift_width(m) for comp, m in masks.items()}


def value_to_argb(
    value: int, masks: dict[str, int], sw: dict[str, tuple[int, int]]
) -> int:
    argb = 0
    for comp in COMPONENTS:
        if comp in masks:
            shift, width = sw[comp]
            comp8 = _scale_up((value & masks[comp]) >> shift, width)
        elif comp == "a":
            comp8 = 0xFF  # no alpha field → opaque
        else:
            comp8 = 0
        argb |= comp8 << _ARGB_SHIFT[comp]
    return argb


def argb_to_value(
    argb: int, masks: dict[str, int], sw: dict[str, tuple[int, int]]
) -> int:
    value = 0
    for comp, mask in masks.items():
        shift, width = sw[comp]
        comp8 = (argb >> _ARGB_SHIFT[comp]) & 0xFF
        value |= (_scale_down(comp8, width) << shift) & mask
    return value
