"""Split-ROM joins — reshape graphics stored as N equal parts into one stream.

Arcade boards routinely wire one bitplane (or one pair of them, or one half of
each 32-bit word) to its own ROM chip, so a tile's bytes are not contiguous in
the region at all: the whole graphics region is cut into *N equal parts*, and
part *k* holds every tile's *k*-th unit in turn. Reading such a region front to
back gives N separate partial images rather than one picture, and no
plane-offset parameter can fix it — a plane sitting ``region_size / N`` bytes
away is outside the tile, and the pixel codecs are deliberately buffer-relative
so that windowed decoding of a large file keeps working
(``docs/design/overview.md`` §4).

So the join is a Reshape-stage plugin, exactly as the Mode 7 VRAM split is
(:mod:`celpix.plugins.builtins.m7_interleave`): ``reshape`` interleaves the N
parts unit-wise, after which each tile's bytes are contiguous and the ordinary
presets read it. ``unshape`` splits it back apart, so the round trip is
byte-exact and write-back returns every byte to the chip it came from.

Two unit sizes cover the shipped variants:

- **Byte-wise** (``unit=1``) is the bitplane split: plane *k* of row *y* lands
  at tile byte ``k + N * y``, which is the ``{ base = k, stride = N }`` rule the
  shipped ``snes-2bpp`` / ``3bpp-planar`` / ``sms-4bpp`` / ``5bpp``…
  ``8bpp-planar`` presets already carry — and it is independent of tile
  geometry, so the same plugin serves an 8×8 2bpp character set and a 16×16
  4bpp sprite without knowing which it is looking at.
- **Word-wise** (``unit=2``, two parts) is the ROM-pair interleave of MAME's
  ``ROM_LOAD32_WORD``: two chips alternating at 16-bit-word granularity. After
  the join the stock ``sms-4bpp`` preset reads a board like TMNT directly
  (``docs/design/reshape-stage.md`` §7).

**Apply it to the region, not to a slice of one.** The part boundaries are
``len(data) / N``, so the transform is only correct when the bytes handed to it
are exactly the graphics region. Feed it a region plus a trailing chunk of
something else and every part after the first is misaligned. This mirrors the
hardware description it comes from, where the split is defined as a fraction of
the region rather than as an absolute offset
(``docs/graphics-formats-reference/mame-formats.md`` §1.1). Any tail beyond a
whole number of unit-aligned parts is passed through untouched rather than
folded into a part, so an odd length degrades to "the last few bytes are not
interpreted" instead of shearing the whole image.
"""

from __future__ import annotations

from celpix.core.context import PipelineContext
from celpix.core.errors import Stage
from celpix.plugins.base import PluginInfo

# Two, three and four parts cover the bitplane splits that actually occur: one
# chip per plane up to 4bpp, and the plane-*pair* split of the Atari System 2
# family (two parts, two planes each). Deeper splits are the same transform with a
# larger N and can be added as data if a board ever needs one.
PART_COUNTS = (2, 3, 4)


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

    The part count and unit size are constructor arguments rather than
    subclasses because the transform is identical for every combination — only
    the numbers differ, so the shipped variants are instances of this one class.
    """

    parts: int
    unit: int

    def __init__(self, parts: int, unit: int = 1) -> None:
        self.parts = parts
        self.unit = unit
        if unit == 1:
            plugin_id = f"reshape.split-planes-{parts}"
            name = f"Split bitplanes ({parts} parts, join)"
        else:
            plugin_id = f"reshape.split-words-{parts}"
            name = f"Split ROM pair ({parts} chips, {unit * 8}-bit words, join)"
        self.info = PluginInfo(id=plugin_id, name=name, stage=Stage.RESHAPE)

    def reshape(self, data: bytes, ctx: PipelineContext) -> bytes:
        return _join(data, self.parts, self.unit)

    def unshape(self, data: bytes, ctx: PipelineContext) -> bytes:
        return _split(data, self.parts, self.unit)


def split_plane_plugins() -> list[SplitPartsReshape]:
    """Every shipped variant, in registration order: the byte-wise bitplane
    splits, then the ROM-pair word interleave (``ROM_LOAD32_WORD``)."""
    plugins = [SplitPartsReshape(parts) for parts in PART_COUNTS]
    plugins.append(SplitPartsReshape(2, unit=2))
    return plugins
