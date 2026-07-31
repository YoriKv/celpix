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

Two params exist for the handheld grayscale palettes, which are the same kernel
seen through different-shaped holes:

- ``bits_per_entry`` replaces ``bytes_per_entry`` where an entry is **smaller
  than a byte**. A Game Boy's ``BGP`` register is four two-bit shades in one
  byte and a WonderSwan's palette word is four nibbles in two; both are a
  register, not an array, so nothing on disk is one-entry-wide to point at. Sub-
  byte entries fill each **unit** from the low bits up, which is the order both
  hardware families use. A value of 8 or more is just ``bytes_per_entry`` spelt
  in bits, and the two params are mutually exclusive.
- ``invert`` marks a format whose fields count **darkness** rather than
  brightness — 0 is white on all three handhelds
  (:func:`~celpix.plugins.builtins._mask._flip`).
- ``gray`` marks a format that stores **one shade**, not three channels. The
  preset then declares a single mask and it drives R, G and B together; writing
  the same mask three times would say the wrong thing, because a pure red would
  encode through the OR of the three as something the format cannot mean. Going
  the other way an arbitrary colour is reduced by luma, which is what the
  hardware's own converters do.

Where an entry is a whole number of bytes and holds a colour (every other
preset) all four params are absent, so the path below is the one it always was.
"""

from __future__ import annotations

from dataclasses import dataclass
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


@dataclass(frozen=True)
class _Config:
    """A preset's colour layout, resolved once per call."""

    unit_bytes: int  # bytes one read unit spans
    per_unit: int  # entries packed into that unit (1 for every byte-sized entry)
    entry_bits: int  # width of one entry's slot within the unit
    order: str
    invert: bool
    gray: bool
    masks: dict[str, tuple[int, ...]]


# Rec. 601 luma, the weighting the handhelds' own art tools reduced with. Scaled
# to 1/1000 so the reduction is integer arithmetic and identical everywhere.
_LUMA = ((299, 16), (587, 8), (114, 0))


def _to_gray(argb: int) -> int:
    """``argb`` with R, G and B all set to its luma; alpha untouched."""
    level = sum(w * ((argb >> at) & 0xFF) for w, at in _LUMA) // 1000
    return (argb & 0xFF000000) | (level << 16) | (level << 8) | level


class ColorCodec:
    """Generic mask-based color codec; behaviour comes from ``params``."""

    info = PluginInfo(
        id="codec.palette.mask",
        name="Mask-based color codec",
        stage=Stage.INTERPRET_PALETTE,
    )

    @staticmethod
    def _config(params: dict[str, Any]) -> _Config:
        # Validated here rather than per entry point, so encode and
        # bytes_per_entry reject an unusable preset with decode's message.
        bits = params.get("bits_per_entry")
        if bits is not None and "bytes_per_entry" in params:
            raise ValueError(
                "give bits_per_entry or bytes_per_entry, not both - "
                "an entry has one width"
            )
        if bits is None:
            size = int(params["bytes_per_entry"])
            if size <= 0:
                raise ValueError("bytes_per_entry must be positive")
            entry_bits, unit_bytes, per_unit = size * 8, size, 1
        else:
            entry_bits = int(bits)
            if entry_bits <= 0:
                raise ValueError("bits_per_entry must be positive")
            if entry_bits >= 8:
                # A whole number of bytes: the same thing bytes_per_entry says.
                if entry_bits % 8:
                    raise ValueError(
                        f"bits_per_entry {entry_bits} is neither a whole number of "
                        "bytes nor a divisor of 8"
                    )
                unit_bytes, per_unit = entry_bits // 8, 1
            else:
                # Sub-byte entries must tile a byte exactly, or a unit would
                # straddle a byte boundary and the packing order stop being a
                # property of the format.
                if 8 % entry_bits:
                    raise ValueError(
                        f"bits_per_entry {entry_bits} does not divide 8 - a "
                        "sub-byte entry has to tile a byte exactly"
                    )
                unit_bytes, per_unit = 1, 8 // entry_bits
        masks = parse_masks(params["masks"])
        gray = bool(params.get("gray", False))
        if gray:
            if len(masks) != 1:
                raise ValueError(
                    f"gray takes exactly one mask - the shade's own field - got "
                    f"{sorted(masks) or 'none'}"
                )
            # Fanned out here so decode needs no special case: three components
            # reading one field *is* a grey. Encode grays the colour first, which
            # makes the three writes agree and the OR exact.
            shade = next(iter(masks.values()))
            masks = {"r": shade, "g": shade, "b": shade}
        return _Config(
            unit_bytes=unit_bytes,
            per_unit=per_unit,
            entry_bits=entry_bits,
            order=params.get("byte_order", "little"),
            invert=bool(params.get("invert", False)),
            gray=gray,
            masks=masks,
        )

    def decode(
        self, data: bytes, params: dict[str, Any], ctx: PipelineContext
    ) -> Palette:
        cfg = self._config(params)
        if len(data) % cfg.unit_bytes != 0:
            raise ValueError(
                f"palette length {len(data)} is not a multiple of entry size "
                f"{cfg.unit_bytes}"
            )
        sw = shift_widths(cfg.masks)
        slot = (1 << cfg.entry_bits) - 1
        colors = []
        for off in range(0, len(data), cfg.unit_bytes):
            unit = int.from_bytes(data[off : off + cfg.unit_bytes], cfg.order)
            for at in range(cfg.per_unit):
                value = (unit >> (at * cfg.entry_bits)) & slot
                colors.append(value_to_argb(value, cfg.masks, sw, cfg.invert))
        return Palette(colors)

    def encode(
        self, palette: Palette, params: dict[str, Any], ctx: PipelineContext
    ) -> bytes:
        cfg = self._config(params)
        sw = shift_widths(cfg.masks)
        out = bytearray()
        # Stepped by unit, so a palette that does not fill its last one leaves
        # the unused slots zero rather than dropping the entries that came
        # before them - the same "write what we have" rule the pixel side keeps
        # for a partial trailing tile.
        for base in range(0, len(palette.colors), cfg.per_unit):
            unit = 0
            for at, argb in enumerate(palette.colors[base : base + cfg.per_unit]):
                if cfg.gray:
                    argb = _to_gray(argb)
                unit |= argb_to_value(argb, cfg.masks, sw, cfg.invert) << (
                    at * cfg.entry_bits
                )
            out += unit.to_bytes(cfg.unit_bytes, cfg.order)
        return bytes(out)

    def bytes_per_entry(self, params: dict[str, Any]) -> int:
        """Bytes one **read unit** spans — the entry itself, unless it is packed.

        The host reads and splices palettes a unit at a time, so this stays the
        stride even for the sub-byte formats, where :meth:`entries_per_unit`
        says how many entries that stride actually covers.
        """
        return self._config(params).unit_bytes

    def entries_per_unit(self, params: dict[str, Any]) -> int:
        """Entries packed into one ``bytes_per_entry``-sized unit.

        1 for every format whose entry is a whole number of bytes, which is all
        of them but the handheld grayscale registers. Optional on the codec
        surface — the host reaches it with ``getattr`` and assumes 1 — so a
        colour codec written before packed entries existed is not missing
        anything.
        """
        return self._config(params).per_unit
