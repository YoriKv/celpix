"""Split bitplanes — join graphics stored as N equal parts, one per plane group.

Arcade boards routinely wire one bitplane (or one pair of them) to its own ROM
chip, so a tile's planes are not interleaved within the tile at all: the whole
graphics region is cut into *N equal parts*, and part *k* holds plane *k* of
every tile in turn. Reading such a region front to back gives N separate
single-plane images rather than one picture, and no plane-offset parameter can
fix it — a plane sitting `region_size / N` bytes away is outside the tile, and
the pixel codecs are deliberately buffer-relative so that windowed decoding of a
large file keeps working (``docs/design/overview.md`` §4).

So the join is a Decompress-stage reorder, exactly as the Mode 7 VRAM split is
(:mod:`celpix.plugins.builtins.m7_interleave`): Decompress interleaves the N
parts byte-wise, after which each tile's plane bytes are contiguous and the
ordinary planar presets read it — plane *k* of row *y* lands at tile byte
``k + N * y``, which is the ``{ base = k, stride = N }`` rule the shipped
``snes-2bpp`` / ``3bpp-planar`` / ``sms-4bpp`` / ``5bpp``…``8bpp-planar`` presets
already carry. Compress splits it back apart, so the round trip is byte-exact and
write-back returns every byte to the chip it came from.

Byte-wise is the point: interleaving one byte at a time makes the transform
**independent of tile geometry**. Part *k*'s bytes for one tile are contiguous
within that part whatever the tile size, so they stay contiguous after
interleaving — the same plugin serves an 8×8 2bpp character set and a 16×16 4bpp
sprite without knowing which it is looking at.

**Apply it to the region, not to a slice of one.** The part boundaries are
``len(data) / N``, so the transform is only correct when the bytes handed to it
are exactly the graphics region. Feed it a region plus a trailing chunk of
something else and every plane after the first is misaligned. This mirrors the
hardware description it comes from, where the split is defined as a fraction of
the region rather than as an absolute offset
(``docs/graphics-formats-reference/mame-formats.md`` §1.1). Any tail beyond a
whole number of parts is passed through untouched rather than folded into a part,
so an odd length degrades to "the last few bytes are not interpreted" instead of
shearing the whole image.
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


def _join(data: bytes, parts: int) -> bytes:
    """Interleave ``parts`` equal parts of ``data`` byte-wise; keep any tail."""
    size = len(data) // parts
    out = bytearray(size * parts)
    for k in range(parts):
        out[k::parts] = data[k * size : (k + 1) * size]
    return bytes(out) + data[size * parts :]


def _split(data: bytes, parts: int) -> bytes:
    """Inverse of :func:`_join`: gather each interleaved plane back into its part."""
    size = len(data) // parts
    body = data[: size * parts]
    return b"".join(body[k::parts] for k in range(parts)) + data[size * parts :]


class SplitPlanesDecompress:
    """Join N parts into per-tile contiguous planes. ``parts`` set by subclass."""

    parts: int

    def __init__(self, parts: int) -> None:
        self.parts = parts
        self.info = PluginInfo(
            id=f"decompress.split-planes-{parts}",
            name=f"Split bitplanes ({parts} parts, join)",
            stage=Stage.DECOMPRESS,
        )

    def decompress(self, data: bytes, ctx: PipelineContext) -> bytes:
        return _join(data, self.parts)


class SplitPlanesCompress:
    """Mirror of :class:`SplitPlanesDecompress`: split the planes back apart."""

    parts: int

    def __init__(self, parts: int) -> None:
        self.parts = parts
        self.info = PluginInfo(
            id=f"compress.split-planes-{parts}",
            name=f"Split bitplanes ({parts} parts, join)",
            stage=Stage.COMPRESS,
        )

    def compress(self, data: bytes, ctx: PipelineContext) -> bytes:
        return _split(data, self.parts)


def split_plane_plugins() -> list[SplitPlanesDecompress | SplitPlanesCompress]:
    """Every shipped part count, both directions, in registration order."""
    plugins: list[SplitPlanesDecompress | SplitPlanesCompress] = []
    for parts in PART_COUNTS:
        plugins.append(SplitPlanesDecompress(parts))
        plugins.append(SplitPlanesCompress(parts))
    return plugins
