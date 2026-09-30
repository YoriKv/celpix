"""What every S-CG-CAD member shares: the signature, the metadata block, depths.

The family's framing is one idea applied at different offsets — a payload, and a
0x100-byte metadata block opening with the same signature — so the pieces every
member reads the same way live here: the block's two reportable rows, the
palette-base arithmetic a screen, a panel and a bank all apply, the depth byte
two of them state, and the blank file a save to an empty path starts from. The
members themselves are in :mod:`.screens`, :mod:`.objects` and :mod:`.banks`.
"""

from __future__ import annotations

from celpix.core.address import format_hex
from celpix.core.animation import read_sequences
from celpix.core.context import (
    KEY_SOURCE_OFFSET,
    PipelineContext,
)
from celpix.plugins.base import (
    ContainerField,
    ReadSource,
)

from .._container_fields import fixed_text

# The 16 bytes every file in the family opens its metadata block with. The 16
# after it are a tool version and build date, identical across the whole surveyed
# corpus but matched loosely all the same: a version bump should not stop a file
# from opening, and the bytes are preserved on write either way.
SIGNATURE = b"NAK1989 S-CG-CAD"

HEADER = 0x100  # every member's metadata block is this long

# The colour window a file was authored through: the half is 128 colours, and
# the cell picks a 4-colour group inside it — but only at 2bpp, where a row *is*
# four colours and the group is therefore one row. At 4bpp the window is the
# whole 128-colour half with no group to pick, and at 8bpp it is all 256 with no
# half either (`scgcad-formats.md` §3.3). The render counts in *rows*, so both
# divide through by n = 4 / 16 / 128 by depth.
_ROWS_PER_HALF = {2: 32, 4: 8}  # 128 colours // n
_ROWS_PER_CELL = {2: 1}  # a 4-colour group // n, and 2bpp only
# Shared by a screen's depth byte and a bank's: one encoding, two offsets.
_DEPTH_BPP = {0: 2, 1: 4, 2: 8}
_DEPTH_BYTE = {bpp: value for value, bpp in _DEPTH_BPP.items()}


def _row_base(col_half: int, col_cell: int, bpp: int) -> int:
    """The palette row this file's cells count their own row 0 from.

    **The cell is 2bpp-only**, which is the whole subtlety here and is not
    guessable from the field: at 4bpp it is stale editor state and applying it
    draws every cell one row out. The editor says so twice — its colour window is
    the whole 128-colour half at 4bpp, with no 4-colour group left to pick, and
    the cell control refuses to move outside 2bpp at all
    (`scgcad-formats.md` §3.3).

    The corpus agrees, and the check is independent of the code: a 4bpp bank with
    ``col_cell = 1`` states per-tile rows that only line up with the panels drawn
    against it when the cell is dropped — 806 of 808 against 0 of 808 — and
    fourteen banks otherwise claim a row past the end of CGRAM, which no reading
    of a real file should produce.
    """
    return (col_half & 1) * _ROWS_PER_HALF.get(bpp, 0) + (col_cell & 3) * (
        _ROWS_PER_CELL.get(bpp, 0)
    )


def _live_sequences(data: bytes, at: int, count: int, steps: int) -> int:
    """How many of an animation table's groups hold anything at all.

    The number worth reporting, since a file has room for 16 or 32 and typically
    fills a handful (``scgcad-formats.md`` §8.3).
    """
    return sum(1 for sequence in read_sequences(data, at, count, steps) if sequence)


def _metadata_fields(data: bytes, at: int) -> list[ContainerField]:
    """The two rows every member's 0x100 metadata block is worth reporting.

    The signature is what detection matched on and the 16 bytes after it are the
    tool version and build date — identical across the surveyed corpus, and the
    one thing in the block that says which build of the authoring tool wrote this
    file. Neither is interpreted further; the whole block rides through a save
    untouched.
    """
    block = data[at : at + HEADER]
    return [
        ContainerField(
            "Signature",
            f"{SIGNATURE.decode()} at {format_hex(at, 4)}"
            if block[: len(SIGNATURE)] == SIGNATURE
            else "absent",
            "The 16 bytes that identify this file's family,\n"
            "matched by detection to pick this container\n"
            "Its position is itself part of the format",
        ),
        ContainerField(
            "Tool version",
            fixed_text(block[0x10:0x20]) or "blank",
            "Version and build date of the authoring tool\n"
            "Recorded only; the whole block is preserved on save",
        ),
    ]


def _payload(source: ReadSource, ctx: PipelineContext, start: int, size: int) -> bytes:
    """``size`` bytes of ``source`` from ``start``, with the offset published.

    The container is the only thing that knows where its payload begins, so it
    publishes that as ``KEY_SOURCE_OFFSET`` — what every address display and
    slice anchor downstream resolves against. Short input yields what is there:
    a truncated file opens showing the rows it has rather than refusing.
    """
    ctx.set(KEY_SOURCE_OFFSET, start)
    return source.data[start : start + size]


def _blank(size: int, at: int = 0, *, tail: int = 0) -> bytes:
    """An empty file of this family: zeroes with the signature in place at ``at``.

    The signature's *position* is how detection tells the members apart, so a
    blank carries it where a real file of that member does. ``tail`` fills what
    follows the metadata block, for a member whose empty trailer is not zeroes (a
    screen's clear table).
    """
    out = bytearray(size)
    out[at : at + len(SIGNATURE)] = SIGNATURE
    if tail:
        out[at + HEADER :] = bytes([tail]) * (size - at - HEADER)
    return bytes(out)
