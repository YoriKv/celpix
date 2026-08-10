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


class SplitPartsReshape:
    """Join N parts into one contiguous stream, and split them back apart.

    The part count and unit size are constructor arguments rather than subclasses:
    the transform is identical for every combination and only the numbers differ,
    so the shipped variants are instances of this one class.
    """

    parts: int
    unit: int

    def __init__(self, parts: int, unit: int = 1) -> None:
        self.parts = parts
        self.unit = unit
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
        return _join(data, self.parts, self.unit)

    def unshape(self, data: bytes, ctx: PipelineContext) -> bytes:
        return _split(data, self.parts, self.unit)


def split_part_plugins() -> list[SplitPartsReshape]:
    """Every shipped variant, in registration order: the byte-wise splits, then
    the word-wise chip interleaves (``ROM_LOAD32_WORD`` / ``ROM_LOAD64_WORD``)."""
    plugins = [SplitPartsReshape(parts) for parts in PART_COUNTS]
    plugins += [SplitPartsReshape(parts, unit=2) for parts in WORD_PART_COUNTS]
    return plugins
