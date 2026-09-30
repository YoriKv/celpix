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

**The GBA/NDS BIOS "UnFilter" calls are the same sum behind a header**
(:mod:`~celpix.plugins.builtins.gba_diff`): a `$81`/`$82` type byte and a declared
size make the filtered block a *structure*, with an extent the scan can find, so
those are Compression plugins that call :func:`sum_units` and
:func:`difference_units` here rather than a second copy of the arithmetic. The
16-bit unit width exists for them — the BIOS sums little-endian halfwords, mod
65536 — and this reshape stays at bytes.
"""

from __future__ import annotations

import struct
from itertools import accumulate

from celpix.core.context import PipelineContext
from celpix.core.errors import Stage
from celpix.plugins.base import PluginInfo
from celpix.plugins.builtins._tile import require_whole

# Unit width in bytes -> its little-endian struct code. The two widths any
# caller has: bytes, and the halfwords the GBA BIOS's 16-bit filter sums.
_UNIT_CODE = {1: "B", 2: "H"}


def _unit_format(data: bytes, width: int) -> tuple[str, int]:
    """The struct format that splits ``data`` into units, and the unit's mask."""
    code = _UNIT_CODE.get(width)
    if code is None:
        raise ValueError(f"unit width must be 1 or 2 bytes, not {width}")
    require_whole(len(data), width, noun="unit")
    return f"<{len(data) // width}{code}", (1 << (8 * width)) - 1


def sum_units(data: bytes, width: int = 1) -> bytes:
    """The running total of ``width``-byte little-endian units, wrapping."""
    fmt, mask = _unit_format(data, width)
    # `accumulate` keeps the loop in C; the totals are left to grow and masked
    # once on the way out, which is the same value as wrapping at every step.
    return struct.pack(
        fmt, *(total & mask for total in accumulate(struct.unpack(fmt, data)))
    )


def difference_units(data: bytes, width: int = 1) -> bytes:
    """The inverse of :func:`sum_units`: each unit minus the one before it."""
    fmt, mask = _unit_format(data, width)
    values = struct.unpack(fmt, data)
    # Each unit against the one before it, the first against zero. The shifted
    # copy is one unit longer on purpose, so the pairing is not strict.
    return struct.pack(
        fmt, *((b - a) & mask for a, b in zip((0, *values), values, strict=False))
    )


class RunningSumReshape:
    """Sum on load, first difference on save; exact inverses, mod 256."""

    info = PluginInfo(
        id="reshape.running-sum",
        name="Running sum (bytes stored as differences)",
        stage=Stage.RESHAPE,
        category="Generic",
    )

    def reshape(self, data: bytes, ctx: PipelineContext) -> bytes:
        return sum_units(data)

    def unshape(self, data: bytes, ctx: PipelineContext) -> bytes:
        return difference_units(data)
