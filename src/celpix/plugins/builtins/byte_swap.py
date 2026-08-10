"""Byte-swapped 16-bit words — read a chip dumped in the other byte order.

A 16-bit graphics ROM is a *word*-wide device, and whether a dump of it lands in
the byte order the video hardware sees depends on how the dumper wired it. Half
the arcade corpus needs the pairs swapped back before anything downstream is
meaningful: MAME spells that ``ROM_LOAD16_WORD_SWAP`` (and the ``32_``/``64_WORD_SWAP``
variants, which swap inside each 16-bit half as well), so a driver naming any of
them is telling you the file's bytes run opposite to the tiles.

``reshape`` exchanges the bytes of every 16-bit group, which is its own inverse —
``unshape`` is the same transform, and the round trip is byte-exact by
construction rather than by a second table that could drift. An odd trailing byte
cannot form a pair and passes through, the same degradation rule the split-plane
joins follow.

**It is not a bitswap.** The bitswap engine permutes *address lines*, and
``i -> i ^ 1`` is an XOR rather than a permutation of them, so no table expresses
it (``docs/design/reshape-stage.md`` §6). Nor is it a chip interleave: a
``ROM_LOAD16_BYTE`` pair is two dumps woven together (``reshape.split-planes-2``)
where this is one dump read the other way round. The failure mode is mild enough
to miss — swapping the bytes of a 4bpp row exchanges pixel pairs 0,1 with 2,3 and
4,5 with 6,7, which reads as a smeared tile rather than as garbage — so it is
worth checking the driver rather than the picture.
"""

from __future__ import annotations

from celpix.core.context import PipelineContext
from celpix.core.errors import Stage
from celpix.plugins.base import PluginInfo


def _swap(data: bytes) -> bytes:
    # Bounded to whole pairs so an odd trailing byte survives the copy untouched;
    # the two strided assignments run in C, one pass each.
    body = len(data) - len(data) % 2
    out = bytearray(data)
    out[0:body:2] = data[1:body:2]
    out[1:body:2] = data[0:body:2]
    return bytes(out)


class ByteSwapReshape:
    """Swap the two bytes of every 16-bit word; an involution."""

    info = PluginInfo(
        id="reshape.swap-bytes-2",
        name="Byteswap 16-bit words",
        stage=Stage.RESHAPE,
        category="Generic",
    )

    def reshape(self, data: bytes, ctx: PipelineContext) -> bytes:
        return _swap(data)

    def unshape(self, data: bytes, ctx: PipelineContext) -> bytes:
        return _swap(data)
