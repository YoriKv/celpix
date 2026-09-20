"""Bytes stored as differences — each one the step from the byte before it.

A loader that wants a picture of consecutive tile numbers to pack well stores the
*steps* instead of the numbers: ``$40 $41 $42 $43`` becomes ``$40 $01 $01 $01``,
a flat area becomes zeroes, and any run-length or LZ scheme then packs the result
to almost nothing. The game undoes it with a running sum, mod 256, three
instructions on a Z80::

    XOR A
    loop: ADD A,(HL) / LD (HL+),A      ; out[i] = out[i-1] + stored[i]

``reshape`` is that sum and ``unshape`` the first difference, so the pair is
byte-exact in both directions for every input. The symptom of needing it is a
decoded map of mostly ``$00`` and ``$01`` that draws as noise
(``docs/rom-mapping/console-game-boy.md``).

**It is a reshape rather than a compression scheme** because it has every property
the stage is defined by and none of a structure's: length-preserving, bijective,
no extent and no end marker. In the Compression slot the structure scan would
report a hit at every offset, since a sum "decodes" anywhere
(``docs/design/reshape-stage.md`` §1). It moves no byte, which puts it beside the
value substitutions rather than the permutations — but no lookup table expresses
it, each output depending on every byte before it.

Where the differences sit under a compressed stream — the usual case, since
packing well is the reason to store them — the sum has to run *after* the
decompressor, which is what a Compress & Reshape plugin pairs it with one for
(:mod:`celpix.plugins.compress_reshape`).
"""

from __future__ import annotations

from itertools import accumulate

from celpix.core.context import PipelineContext
from celpix.core.errors import Stage
from celpix.plugins.base import PluginInfo


def _sum(data: bytes) -> bytes:
    # `accumulate` keeps the loop in C; the totals are left to grow and masked
    # once on the way out, which is the same value as wrapping at every step.
    return bytes(total & 0xFF for total in accumulate(data))


def _difference(data: bytes) -> bytes:
    # Each byte against the one before it, the first against zero. The shifted
    # copy is one byte longer on purpose, so the pairing is not strict.
    return bytes((b - a) & 0xFF for a, b in zip(b"\x00" + data, data, strict=False))


class RunningSumReshape:
    """Sum on load, first difference on save; exact inverses, mod 256."""

    info = PluginInfo(
        id="reshape.running-sum",
        name="Running sum (bytes stored as differences)",
        stage=Stage.RESHAPE,
        category="Generic",
    )

    def reshape(self, data: bytes, ctx: PipelineContext) -> bytes:
        return _sum(data)

    def unshape(self, data: bytes, ctx: PipelineContext) -> bytes:
        return _difference(data)
