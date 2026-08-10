"""Shared mask-based color kernel: a native value ⇄ ``0xAARRGGBB``.

One integer's R/G/B/A fields are sliced by component **mask**, scaled to 8 bits by
high-bit replication (so the round trip is exact at the field's precision), and
packed into ARGB. A palette entry and a direct-color pixel are the same problem,
so both
:class:`~celpix.plugins.builtins.color_codec.ColorCodec` and
:class:`~celpix.plugins.builtins.direct_color_codec.DirectColorCodec` use this
(``docs/graphics-formats-reference/implementation-guide.md`` §4).

**A field need not be contiguous.** Some boards park a channel's low bit away
from the rest of the channel, to keep the high nibbles byte-aligned — the
``RRRRGGGGBBBBRGBx`` word many 68000 arcade boards use is three 5-bit channels
laid out that way. A mask is therefore a **tuple of contiguous chunks, most
significant first** (a plain mask parses to a one-chunk tuple), and the order is
carried rather than inferred: which chunk holds the high bits is a property of
the format, and boards exist that put the stray bit above the run as well as
below. :func:`gather` / :func:`scatter` are that split-field kernel on its own,
and a tilemap cell's fields are the same problem once more — a Game Boy Color
index is bits 0-7 plus bit 8 stranded in the attribute byte — so
:mod:`~celpix.plugins.builtins.tilemap_codec` places its own fields through
them too.

A preset states where the components sit as a **bit layout**: the letter-per-bit
diagram its own documentation draws, ``0BBBBBGGGGGRRRRR`` or
``RRRRGGGGBBBBRGBx``, read by :mod:`~celpix.plugins.builtins._fields`. That is
where the split above comes from — the two runs of ``R`` in the second are one
channel, in the order they are written — and it is what makes the unused bit at
the bottom of the first a thing the preset says rather than a thing it forgot.

:func:`value_to_argb` / :func:`argb_to_value` are the per-value form a palette
wants: a handful of entries, one at a time. A direct-color *image* needs the same
conversion a million times over and goes through :func:`decode_tables` /
:func:`encode_tables` — the identical kernel precomputed as one 256-entry byte
table per (source byte, component) pair.

That factorisation is exact. Both scaling functions are built from shifts and a
mask, and shifting distributes over OR, so a component assembled from several
source bytes is the OR of what each contributes on its own and the tables apply
byte-plane at a time over a whole buffer.
"""

from __future__ import annotations

from functools import cache
from typing import Any

from celpix.plugins.builtins._fields import parse_layout, resolve_legend

# Where each component sits in the little-endian ARGB pixel buffer both grids use
# (bytes B, G, R, A) — the byte plane a table reads or writes.
ARGB_BYTE_LAYOUT = ("b", "g", "r", "a")

COMPONENTS = ("a", "r", "g", "b")
_ARGB_SHIFT = {"a": 24, "r": 16, "g": 8, "b": 0}

# The shade field of a format that stores brightness rather than three channels:
# the handheld greys, and the intensity textures. It is not a component — it
# *drives* R, G and B together — so it is kept out of COMPONENTS and each codec
# spends it its own way. Spelt ``v`` because that is what it holds: HSV's
# **value**, a brightness with neither a hue nor a saturation beside it.
GRAY = "v"

# Letters a colour layout is written with, and the components they name. The
# first four are what every note in circulation uses; a preset overrides them
# where its own hardware documentation spells a channel some other way.
COLOR_FIELDS = frozenset(COMPONENTS) | {GRAY}
COLOR_LEGEND = {name: name for name in COLOR_FIELDS}


def chunk_shift_width(chunk: int) -> tuple[int, int]:
    """Low-bit position and bit width of one contiguous mask chunk."""
    if chunk == 0:
        return 0, 0
    shift = (chunk & -chunk).bit_length() - 1
    width = bin(chunk).count("1")
    if chunk >> shift != (1 << width) - 1:
        raise ValueError(
            f"mask chunk {chunk:#x} has a gap in it - write a split field as a "
            "list of contiguous chunks, most significant first"
        )
    return shift, width


def _full(chunks: tuple[int, ...]) -> int:
    """The whole field's mask, for "does this byte carry any of it" tests."""
    whole = 0
    for chunk in chunks:
        whole |= chunk
    return whole


def _width(sw: tuple[tuple[int, int], ...]) -> int:
    return sum(width for _, width in sw)


def gather(value: int, chunks: tuple[int, ...], sw: tuple[tuple[int, int], ...]) -> int:
    """Pack ``value``'s masked bits down into one field, chunks high-to-low.

    Linear in ``value``: each chunk lands at a fixed field position, so gathering
    two values separately and OR-ing the results is gathering their OR — which is
    what lets :func:`decode_tables` build the field one source byte at a time.
    """
    raw = 0
    for chunk, (shift, width) in zip(chunks, sw):
        raw = (raw << width) | ((value & chunk) >> shift)
    return raw


def scatter(raw: int, chunks: tuple[int, ...], sw: tuple[tuple[int, int], ...]) -> int:
    """Inverse of :func:`gather`: spread a field back over its chunks."""
    value = 0
    rest = _width(sw)
    for chunk, (shift, width) in zip(chunks, sw):
        rest -= width
        value |= ((raw >> rest) << shift) & chunk
    return value


def _scale_up(raw: int, width: int) -> int:
    """Scale a ``width``-bit field up to 8 bits by replicating its high bits.

    The pattern is repeated until it fills eight bits, not copied once: at four
    bits and up one copy already reaches the bottom, but a 3-bit ``7`` tiled once
    is ``0xE7`` and the format plainly means white. Genesis and PC Engine palettes
    are 3 bits a channel and the handheld greys are 2 and 3, so the difference is
    the whole top of their range.

    The first copy lands in the top ``width`` bits, which is what keeps
    :func:`_scale_down` an exact inverse however many follow it.
    """
    if width >= 8:
        return (raw >> (width - 8)) & 0xFF
    out, filled = 0, 0
    while filled < 8:
        out = (out << width) | raw
        filled += width
    return (out >> (filled - 8)) & 0xFF


def _scale_down(comp8: int, width: int) -> int:
    """Inverse of :func:`_scale_up`: 8-bit component down to a ``width``-bit field."""
    if width >= 8:
        return comp8 << (width - 8)
    return comp8 >> (8 - width)


def parse_masks(raw_masks: dict[str, Any]) -> dict[str, tuple[int, ...]]:
    """Read the ``masks`` param into ``{component: chunks}``, skipping absent ones.

    Masks are hex integers in TOML (``0x7C00``); strings (``"0x7C00"``) are also
    accepted, as is a **list** of them for a split field, most significant chunk
    first (``[0xF000, 0x0008]``). Either way the result is a tuple, so the rest of
    the kernel has one shape to handle. A component is left out by omitting its
    key.
    """
    masks: dict[str, tuple[int, ...]] = {}
    for comp in COMPONENTS:
        value = raw_masks.get(comp)
        if not value:
            continue
        raw = value if isinstance(value, (list, tuple)) else (value,)
        chunks = tuple(int(c, 0) if isinstance(c, str) else int(c) for c in raw)
        seen = 0
        for chunk in chunks:
            if not chunk:
                raise ValueError(f"mask {comp!r}: a chunk may not be empty")
            if chunk & seen:
                raise ValueError(f"mask {comp!r}: chunks overlap")
            chunk_shift_width(chunk)  # rejects a gap inside one chunk
            seen |= chunk
        masks[comp] = chunks
    return masks


def color_masks(params: dict[str, Any], width: int) -> dict[str, tuple[int, ...]]:
    """Where each component sits, read off the preset's ``fields`` layout.

    ``width`` is how many bits one value really holds — the entry or the pixel,
    not the unit it is packed into — so a diagram that leaves bits unspoken for
    fails at load rather than quietly writing them as zero.

    :data:`GRAY` may come back among the components. It is not one, and each
    codec decides what a single shade means for it: a palette entry reduces a
    colour to its luma on the way in, where a truecolor pixel carries the field
    to R, G and B and lets them meet again on the way out.
    """
    text = params.get("fields")
    if isinstance(text, str):
        legend = resolve_legend(COLOR_LEGEND, params.get("legend"), COLOR_FIELDS)
        return {
            name: chunks
            for name, (chunks, _sw) in parse_layout(text, legend, width).items()
        }
    if "masks" in params:
        return parse_masks(params["masks"])
    raise ValueError(
        "the preset does not say where the colour components sit - give "
        "`fields`, one letter per bit, most significant first"
    )


def shift_widths(
    masks: dict[str, tuple[int, ...]],
) -> dict[str, tuple[tuple[int, int], ...]]:
    return {
        comp: tuple(chunk_shift_width(chunk) for chunk in chunks)
        for comp, chunks in masks.items()
    }


def _flip(raw: int, width: int, invert: bool) -> int:
    """Complement a field within its own width, for formats that count darkness.

    A grayscale LCD stores *how dark* a shade is, not how bright: 0 is white on a
    Game Boy, a WonderSwan and a Neo Geo Pocket alike. That is one xor away from
    an ordinary brightness field, so those palettes stay data
    (``docs/graphics-formats-reference/tile-converter-formats.md`` §3.3) instead of
    each needing its own engine. Its own inverse, so the round trip is unaffected.
    """
    return raw ^ ((1 << width) - 1) if invert else raw


def value_to_argb(
    value: int,
    masks: dict[str, tuple[int, ...]],
    sw: dict[str, tuple[tuple[int, int], ...]],
    invert: bool = False,
) -> int:
    argb = 0
    for comp in COMPONENTS:
        if comp in masks:
            width = _width(sw[comp])
            raw = _flip(gather(value, masks[comp], sw[comp]), width, invert)
            comp8 = _scale_up(raw, width)
        elif comp == "a":
            comp8 = 0xFF  # no alpha field → opaque
        else:
            comp8 = 0
        argb |= comp8 << _ARGB_SHIFT[comp]
    return argb


def argb_to_value(
    argb: int,
    masks: dict[str, tuple[int, ...]],
    sw: dict[str, tuple[tuple[int, int], ...]],
    invert: bool = False,
) -> int:
    value = 0
    for comp, chunks in masks.items():
        comp8 = (argb >> _ARGB_SHIFT[comp]) & 0xFF
        width = _width(sw[comp])
        raw = _flip(_scale_down(comp8, width), width, invert)
        value |= scatter(raw, chunks, sw[comp])
    return value


def _byte_shift(index: int, count: int, order: str) -> int:
    """Bit position of stream byte ``index`` within a ``count``-byte value."""
    return 8 * (index if order == "little" else count - 1 - index)


@cache
def decode_tables(
    masks: tuple[tuple[str, tuple[int, ...]], ...], count: int, order: str
) -> tuple[tuple[bytes | None, ...], ...]:
    """Per-component tables turning source bytes into ARGB byte planes.

    Indexed ``[component][source byte]`` — component in :data:`ARGB_BYTE_LAYOUT`,
    source byte in stream order — each entry a 256-byte ``bytes.translate`` table,
    or ``None`` where that byte holds none of that component's mask.

    ``masks`` arrives as sorted items rather than a dict so the result can be
    cached across the many decodes one view refresh makes.
    """
    mask_map = dict(masks)
    sw = shift_widths(mask_map)
    tables: list[tuple[bytes | None, ...]] = []
    for comp in ARGB_BYTE_LAYOUT:
        chunks = mask_map.get(comp)
        if chunks is None:
            tables.append((None,) * count)
            continue
        whole, width = _full(chunks), _width(sw[comp])
        per_byte: list[bytes | None] = []
        for index in range(count):
            up = _byte_shift(index, count, order)
            if not (whole >> up) & 0xFF:
                per_byte.append(None)  # this byte carries none of the field
                continue
            per_byte.append(
                bytes(
                    _scale_up(gather(value << up, chunks, sw[comp]), width)
                    for value in range(256)
                )
            )
        tables.append(tuple(per_byte))
    return tuple(tables)


@cache
def encode_tables(
    masks: tuple[tuple[str, tuple[int, ...]], ...], count: int, order: str
) -> tuple[tuple[bytes | None, ...], ...]:
    """The inverse of :func:`decode_tables`: ARGB byte planes to source bytes.

    Indexed ``[source byte][component]``, the transpose of the decode side:
    encoding builds one source byte out of every component reaching into it, where
    decoding built one component out of every source byte.
    """
    mask_map = dict(masks)
    sw = shift_widths(mask_map)
    tables: list[tuple[bytes | None, ...]] = []
    for index in range(count):
        up = _byte_shift(index, count, order)
        per_comp: list[bytes | None] = []
        for comp in ARGB_BYTE_LAYOUT:
            chunks = mask_map.get(comp)
            if chunks is None or not (_full(chunks) >> up) & 0xFF:
                per_comp.append(None)
                continue
            width = _width(sw[comp])
            per_comp.append(
                bytes(
                    (scatter(_scale_down(value, width), chunks, sw[comp]) >> up) & 0xFF
                    for value in range(256)
                )
            )
        tables.append(tuple(per_comp))
    return tuple(tables)
