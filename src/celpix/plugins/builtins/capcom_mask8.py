"""Capcom's 8-byte mask RLE — one repeated byte per block, picked by a bitmask.

The in-house scheme six Capcom SNES titles unpack tile banks with. It has no
published name; this one describes it, because the block is the format::

    per block, repeating:
      mm dd [literals]    mm = the mask, dd = the block's repeated byte

    mask bit, most significant first, one per output byte:
      0   emit dd
      1   emit the next literal byte

Eight output bytes per block, always. The cost is ``2 + popcount(mm)``, so a
block runs from 2 bytes (all eight the same) to 10 (all eight different, and
``dd`` is then read and never used) — a worst case of 25% expansion that the
encoder below never reaches, since it always spends ``dd`` on a byte that occurs.

**It is a run coder with a fixed window, not a general RLE.** A run only pays
where it lands inside a block, and a second run in the same block cannot be coded
at all. That suits 4bpp SNES tiles, where a mostly-flat row is four plane bytes
of ``$00`` or ``$FF`` interleaved with detail, and it is why the format holds its
own on art and loses badly on anything else: real banks land between 0.41 and
0.95 bytes stored per byte unpacked, mostly around 0.8.

**No terminator, no length, and every byte sequence decodes.** A stream ends where
the game's pointer table says it does. Every loader seen turns a *byte* length into
``ceil(length / 8)`` whole blocks and decodes that many, overrunning the requested
length by up to seven bytes — which is why :func:`compress` refuses a partial
block rather than inventing a padding rule. Same framing problem as
:mod:`~celpix.plugins.builtins.packbits`, and the same three consequences:
``KEY_DECOMPRESS_COMPLETE`` is never reported, the structure scan cannot find
these streams, and an unbounded read is stopped by :data:`_MAX_OUT` rather than by
anything in the data. Give a slice an explicit length.

**Confirmed in six titles**, by matching the decompressor in the cartridge and
decoding its own pointer tables: Super Ghouls 'N Ghosts (1991), Mega Man X,
Aladdin and Goof Troop (1993), Mega Man X2 (1994) and Mega Man X3 (1995). The
routine takes two shapes — bit-serial with a counter of 8 (Goof Troop, the Mega
Man X family) and fully unrolled into eight 21-byte blocks (Super Ghouls 'N
Ghosts, Aladdin) — and the 1991 title carries the older, unrolled one. It is a
library routine passed down a house codebase, not a per-game invention.

**Finding it in a cart does not mean it holds that cart's art.** The Mega Man X
games carry an unrelated 1 KiB-window LZSS for their main tile sets and use this
scheme for a second, separate group of banks.

**The decoder decides per *bit*, not per nibble.** A 16-way dispatch on each
nibble of the mask describes the same bytes exactly, and inviting that reading
misplaces where the format's structure lies. Every cartridge routine shifts one
bit at a time; the nibbles mean nothing.

Data-side detection is hopeless, which is why no scan will find these streams:
arbitrary bytes read as this format consume ``2 + 4`` per 8 output bytes on
average, the same ~0.75 ratio real streams achieve. Find a stream from the loader
or from a pointer table.
"""

from __future__ import annotations

from celpix.core.context import (
    KEY_COMPRESSED_SIZE,
    KEY_DECOMPRESS_COMPLETE,
    PipelineContext,
)
from celpix.core.errors import Stage
from celpix.plugins.base import PluginInfo

# Output bytes one mask byte covers. The format's single structural constant:
# block size, mask width and the encoder's run window are all this number.
BLOCK = 8

# Memory guard for unbounded reads, not a format limit (see the module docstring).
# Matches PackBits' cap; the expansion here tops out at 4x rather than 128x, so
# this only ever bites a read that was never bounded in the first place.
_MAX_OUT = 0x100000


def decompress(data: bytes) -> tuple[bytes, int]:
    """Decode a Capcom 8-byte-mask stream; returns ``(output, consumed)``.

    Decoding runs until the buffer is exhausted, the output cap is reached, or a
    block is cut short by the end of the buffer. A truncated block still
    contributes the bytes it *can* produce — a view window routinely slices
    mid-block, and the preview wants them — while ``consumed`` stops at the end of
    the last whole block, the only offset a following structure could start at.
    """
    out = bytearray()
    at, size = 0, len(data)
    consumed = 0
    while at + 1 < size and len(out) < _MAX_OUT:
        mask, repeat = data[at], data[at + 1]
        pos = at + 2
        for bit in reversed(range(BLOCK)):
            if not mask >> bit & 1:
                out.append(repeat)
            elif pos < size:
                out.append(data[pos])
                pos += 1
            else:  # the buffer ended inside the block's literals
                return bytes(out), consumed
        at = consumed = pos
    return bytes(out), consumed


def compress(data: bytes) -> bytes:
    """Encode raw bytes as a Capcom 8-byte-mask stream.

    The repeated byte is the block's **mode** — a block costs ``2 + (8 - count)``,
    so the most frequent value is always the cheapest choice, and a block of eight
    distinct bytes still spends ``dd`` on the first of them rather than wasting it.

    The length must be a whole number of blocks. The format has no way to state a
    short final one: the game's loader stops on an output count it holds
    externally, so a partial block is a fact about the *slice*, not about the
    stream, and padding one here would hand back more bytes than went in.
    """
    if not data or len(data) % BLOCK:
        raise ValueError(
            f"the Capcom mask codec packs whole {BLOCK}-byte blocks: "
            f"{len(data):,} bytes is not a non-zero multiple of {BLOCK}"
        )
    out = bytearray()
    for start in range(0, len(data), BLOCK):
        block = data[start : start + BLOCK]
        repeat = max(block, key=block.count)
        mask = sum(1 << (BLOCK - 1 - i) for i, b in enumerate(block) if b != repeat)
        out.append(mask)
        out.append(repeat)
        out += bytes(b for b in block if b != repeat)
    return bytes(out)


class CapcomMask8Compression:
    info = PluginInfo(
        id="compression.capcom-mask8",
        name="Capcom 8-byte mask RLE (SNES)",
        stage=Stage.COMPRESSION,
        # A bare block stream: it ends where the game's pointer table says, not
        # where the data does. The overlay phrases its status accordingly.
        self_delimiting=False,
        category="Nintendo",
    )

    def decompress(self, data: bytes, ctx: PipelineContext) -> bytes:
        out, consumed = decompress(data)
        ctx.set(KEY_COMPRESSED_SIZE, consumed)
        # Never complete: with no end marker, "we decoded to here" is not "the
        # structure ends here" (see the module docstring).
        ctx.set(KEY_DECOMPRESS_COMPLETE, False)
        return out

    def compress(self, data: bytes, ctx: PipelineContext) -> bytes:
        return compress(data)
