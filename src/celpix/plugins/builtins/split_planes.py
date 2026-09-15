"""Split-ROM joins — reshape graphics stored as N equal parts into one stream.

Arcade boards routinely wire one bitplane — or one pair of them, or one half of
each 32-bit word — to its own ROM chip, so a tile's bytes are not contiguous in
the region: the region is cut into *N equal parts*, and part *k* holds every
tile's *k*-th unit in turn. Read front to back that gives N partial images rather
than one picture, and no plane-offset parameter can fix it, since a plane sitting
``region_size / N`` bytes away is outside the tile and the pixel codecs are
buffer-relative so windowed decoding of a large file keeps working
(``docs/design/overview.md`` §4).

So the join is a Reshape, like the Mode 7 VRAM split
(:mod:`celpix.plugins.builtins.m7_vram`): ``reshape`` interleaves the N parts
unit-wise, after which each tile's bytes are contiguous and the ordinary presets
read it, and ``unshape`` splits them back apart so write-back returns every byte
to the chip it came from.

Two unit sizes cover the shipped variants:

- **Byte-wise** (``unit=1``) is the bitplane split: plane *k* of row *y* lands at
  tile byte ``k + N * y``, the ``{ base = k, stride = N }`` rule the shipped
  ``snes-2bpp`` / ``3bpp-planar`` / ``sms-4bpp`` / ``5bpp``…``8bpp-planar``
  presets already carry. It is independent of tile geometry, so one plugin serves
  an 8×8 2bpp tile set and a 16×16 4bpp sprite alike. The same interleave is
  MAME's ``ROM_LOAD16_BYTE`` / ``32_BYTE`` / ``64_BYTE`` chip pairing, one *byte
  lane* per chip rather than one plane; the transform cannot tell the two readings
  apart, and neither changes it.
- **Word-wise** (``unit=2``) is the chip interleave of MAME's ``ROM_LOAD32_WORD``
  and ``ROM_LOAD64_WORD``: two or four chips alternating at 16-bit-word
  granularity, one per lane of a 32- or 64-bit graphics bus. After the join the
  stock ``sms-4bpp`` preset reads a board like TMNT directly
  (``docs/design/reshape-stage.md`` §7).

A third shape is not a chip split at all but a **table stored as parallel
arrays**, and it is the same join applied one level down:

- **Grouped** (``groups=G``) cuts the region into G equal groups first and joins
  N parts *within* each. A table of 2x2 stamps kept as four word arrays — every
  stamp's top-left cell, then every top-right, bottom-left, bottom-right — is
  G=2, N=2: the join turns ``TL‖TR‖BL‖BR`` into ``TL0 TR0 TL1 TR1…`` followed by
  ``BL0 BR0 BL1 BR1…``, so laid out at twice the table's length each stamp's four
  cells are a 2x2 rectangle, which is what a chained binding's stamp walk
  (``docs/design/tilemap-entry.md`` §3.1) reads. A plain four-part join would
  weave all four into one row instead, and the two-part join over the whole
  region pairs TL with BL — the same four cells, transposed.

Which a board wants is **not visible in the shapes**. For a two-chip pair the
byte-wise and word-wise joins differ only by a swap of bitplanes 1 and 2, so both
render the same picture and only the colours tell them apart — the byte-wise join
of a ``ROM_LOAD32_WORD`` pair looks like a palette problem.

**Apply it to the region, not to a slice of one.** The part boundaries are
``len(data) / N``, so the transform is correct only when the bytes handed to it
are exactly the graphics region; a region plus a trailing chunk of something else
misaligns every part after the first. That mirrors the hardware description, where
the split is a fraction of the region rather than an absolute offset
(``docs/graphics-formats-reference/mame-formats.md`` §2). A tail beyond a whole
number of unit-aligned parts passes through untouched, so an odd length degrades
to "the last few bytes are not interpreted" instead of shearing the image.
"""

from __future__ import annotations

from celpix.core.context import PipelineContext
from celpix.core.errors import Stage
from celpix.plugins.base import PluginInfo

# The byte-wise part counts that occur in hardware. Two through six are the plane
# splits — one chip per plane, plus the plane-*pair* split of the Atari System 2
# family (two parts, two planes each) — pairing with the shipped ``snes-2bpp``…
# ``6bpp-planar`` presets. Eight is not a plane split but MAME's
# ``ROM_LOAD64_BYTE``: eight chips feeding one byte lane each of a 64-bit bus, the
# same interleave with a larger N. Seven is absent because no driver splits a
# region into sevenths.
PART_COUNTS = (2, 3, 4, 5, 6, 8)

# The word-wise counts follow bus width: a 32-bit graphics bus takes two chips
# (``ROM_LOAD32_WORD``), a 64-bit one four (``ROM_LOAD64_WORD``). Buses come in
# powers of two, so this set is closed rather than merely what has been needed.
WORD_PART_COUNTS = (2, 4)

# ``(parts, groups)`` for the grouped word joins. Unlike the bus widths above this
# is a list of what has been needed, not a closed set: 2x2 is a table of 2x2
# stamps stored as four corner arrays (Sesame Street: Counting Cafe, Mega Drive).
GROUPED_WORD_PARTS = ((2, 2),)


def _join(data: bytes, parts: int, unit: int) -> bytes:
    """Interleave ``parts`` equal parts of ``data`` ``unit`` bytes at a time;
    keep any tail past a whole number of unit-aligned parts."""
    size = (len(data) // (parts * unit)) * unit
    out = bytearray(size * parts)
    step = parts * unit
    for k in range(parts):
        for b in range(unit):
            out[k * unit + b :: step] = data[k * size + b : (k + 1) * size : unit]
    return bytes(out) + data[size * parts :]


def _split(data: bytes, parts: int, unit: int) -> bytes:
    """Inverse of :func:`_join`: gather each interleaved unit back into its part."""
    size = (len(data) // (parts * unit)) * unit
    body = data[: size * parts]
    step = parts * unit
    out = bytearray(size * parts)
    for k in range(parts):
        for b in range(unit):
            out[k * size + b : (k + 1) * size : unit] = body[k * unit + b :: step]
    return bytes(out) + data[size * parts :]


def _grouped(data: bytes, groups: int, parts: int, unit: int, step) -> bytes:
    """Apply ``step`` (:func:`_join` or :func:`_split`) to each of ``groups`` equal
    groups; keep any tail past a whole number of unit-aligned parts per group.

    Each group is cut to a whole number of parts before ``step`` sees it, so the
    per-group call never has a tail of its own and the one tail is the region's.
    """
    if groups == 1:
        return step(data, parts, unit)
    size = (len(data) // (groups * parts * unit)) * parts * unit
    body = b"".join(
        step(data[g * size : (g + 1) * size], parts, unit) for g in range(groups)
    )
    return body + data[size * groups :]


class SplitPartsReshape:
    """Join N parts into one contiguous stream, and split them back apart.

    The part count, unit size and group count are constructor arguments rather
    than subclasses: the transform is identical for every combination and only the
    numbers differ, so the shipped variants are instances of this one class.
    """

    parts: int
    unit: int
    groups: int

    def __init__(self, parts: int, unit: int = 1, groups: int = 1) -> None:
        self.parts = parts
        self.unit = unit
        self.groups = groups
        if groups > 1:
            # Named for what is on disk — tables side by side — since no chip
            # wiring is involved, and filed apart from the arcade joins.
            plugin_id = f"reshape.split-words-{parts}x{groups}"
            name = (
                f"Split word tables ({groups} groups of {parts}, "
                f"{unit * 8}-bit words, join each group)"
            )
            self.info = PluginInfo(
                id=plugin_id, name=name, stage=Stage.RESHAPE, category="Generic"
            )
            return
        if unit == 1:
            plugin_id = f"reshape.split-planes-{parts}"
            name = f"Split bitplanes ({parts} ROMs, join)"
        else:
            # "chips" rather than "pair", the same transform serving the two-chip
            # 32-bit bus and the four-chip 64-bit one.
            plugin_id = f"reshape.split-words-{parts}"
            name = f"Split ROM chips ({parts} chips, {unit * 8}-bit words, join)"
        self.info = PluginInfo(
            id=plugin_id, name=name, stage=Stage.RESHAPE, category="Arcade"
        )

    def reshape(self, data: bytes, ctx: PipelineContext) -> bytes:
        return _grouped(data, self.groups, self.parts, self.unit, _join)

    def unshape(self, data: bytes, ctx: PipelineContext) -> bytes:
        return _grouped(data, self.groups, self.parts, self.unit, _split)


def split_part_plugins() -> list[SplitPartsReshape]:
    """Every shipped variant, in registration order: the byte-wise splits, the
    word-wise chip interleaves (``ROM_LOAD32_WORD`` / ``ROM_LOAD64_WORD``), then
    the grouped word-table joins."""
    plugins = [SplitPartsReshape(parts) for parts in PART_COUNTS]
    plugins += [SplitPartsReshape(parts, unit=2) for parts in WORD_PART_COUNTS]
    plugins += [
        SplitPartsReshape(parts, unit=2, groups=groups)
        for parts, groups in GROUPED_WORD_PARTS
    ]
    return plugins
