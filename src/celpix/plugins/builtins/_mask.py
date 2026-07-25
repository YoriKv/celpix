"""Shared mask-based color kernel: a native value ⇄ ``0xAARRGGBB``.

One integer's contiguous R/G/B/A fields are sliced by component **mask**, scaled to
8 bits (high-bit replication, so the round-trip is exact at the field's precision),
and packed into ARGB. Used by both the palette-side
:class:`~celpix.plugins.builtins.color_codec.ColorCodec` and the tile-side
:class:`~celpix.plugins.builtins.direct_color_codec.DirectColorCodec` — the palette
entry and the direct-color pixel are the same problem
(``docs/graphics-formats-reference/implementation-guide.md`` §4).

:func:`value_to_argb` / :func:`argb_to_value` are the per-value form, which is what
a palette wants: a handful of entries, one at a time. A direct-color *image* wants
the same conversion a million times over, so it goes through
:func:`decode_tables` / :func:`encode_tables` instead — the identical kernel
precomputed as one 256-entry byte table per (source byte, component) pair.

That factorisation is exact, not an approximation. Both scaling functions are
built from shifts and a mask, and shifting distributes over OR — so a component
assembled from several source bytes is the OR of what each byte contributes on
its own, and the tables can be applied byte-plane at a time over a whole buffer.
"""

from __future__ import annotations

from functools import cache
from typing import Any

# Where each component sits in the little-endian ARGB pixel buffer both grids use
# (bytes B, G, R, A) — the byte plane a table reads or writes.
ARGB_BYTE_ORDER = ("b", "g", "r", "a")

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


def _byte_shift(index: int, count: int, order: str) -> int:
    """Bit position of stream byte ``index`` within a ``count``-byte value."""
    return 8 * (index if order == "little" else count - 1 - index)


@cache
def decode_tables(
    masks: tuple[tuple[str, int], ...], count: int, order: str
) -> tuple[tuple[bytes | None, ...], ...]:
    """Per-component tables turning source bytes into ARGB byte planes.

    Indexed ``[component][source byte]`` — component in :data:`ARGB_BYTE_ORDER`,
    source byte in stream order — each entry a 256-byte table for
    ``bytes.translate``, or ``None`` where that byte holds none of that
    component's mask and so contributes nothing.

    ``masks`` comes in as sorted items rather than a dict so the result can be
    cached across the many decodes one view refresh makes.
    """
    mask_map = dict(masks)
    sw = shift_widths(mask_map)
    tables: list[tuple[bytes | None, ...]] = []
    for comp in ARGB_BYTE_ORDER:
        mask = mask_map.get(comp)
        if mask is None:
            tables.append((None,) * count)
            continue
        shift, width = sw[comp]
        per_byte: list[bytes | None] = []
        for index in range(count):
            up = _byte_shift(index, count, order)
            if not (mask >> up) & 0xFF:
                per_byte.append(None)  # this byte carries none of the field
                continue
            per_byte.append(
                bytes(
                    _scale_up(((value << up) & mask) >> shift, width)
                    for value in range(256)
                )
            )
        tables.append(tuple(per_byte))
    return tuple(tables)


@cache
def encode_tables(
    masks: tuple[tuple[str, int], ...], count: int, order: str
) -> tuple[tuple[bytes | None, ...], ...]:
    """The inverse of :func:`decode_tables`: ARGB byte planes to source bytes.

    Indexed ``[source byte][component]`` — the transpose of the decode side,
    because encoding builds one source byte out of every component that reaches
    into it, where decoding built one component out of every source byte.
    """
    mask_map = dict(masks)
    sw = shift_widths(mask_map)
    tables: list[tuple[bytes | None, ...]] = []
    for index in range(count):
        up = _byte_shift(index, count, order)
        per_comp: list[bytes | None] = []
        for comp in ARGB_BYTE_ORDER:
            mask = mask_map.get(comp)
            if mask is None or not (mask >> up) & 0xFF:
                per_comp.append(None)
                continue
            shift, width = sw[comp]
            per_comp.append(
                bytes(
                    (((_scale_down(value, width) << shift) & mask) >> up) & 0xFF
                    for value in range(256)
                )
            )
        tables.append(tuple(per_comp))
    return tuple(tables)
