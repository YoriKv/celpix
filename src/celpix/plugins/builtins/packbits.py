"""PackBits — Apple's byte-oriented RLE, as used by TIFF, IFF/ILBM and MacPaint.

The simplest scheme in the compression layer: a stream of packets, each a
one-byte control header read as a *signed* value, followed by its data.

| control ``c`` | meaning |
|---|---|
| ``0x00``–``0x7F`` (0..127)   | **literal**: copy the next ``c + 1`` bytes verbatim |
| ``0x81``–``0xFF`` (-127..-1) | **run**: repeat the next byte ``257 - c`` times |
| ``0x80`` (-128)              | **no-op**: skip, the next byte is another control |

Both packet kinds therefore carry 1..128 output bytes; a run needs at least 2
(``c`` can't reach -128 as a count, which is what frees ``0x80``), so a lone
byte is always a literal. Worst case — no two adjacent bytes equal — costs one
control byte per 128, so the encoding expands by at most ~0.8%.

**There is no terminator, and no length field.** The end of a PackBits structure
is knowable only from outside the stream (the TIFF strip byte count, the ILBM
chunk size, a game's pointer table), and *any* byte sequence decodes as valid
PackBits. Two consequences the rest of celPix has to live with:

- We never report ``KEY_DECOMPRESS_COMPLETE``. The structure's own end is not in
  the data, so a decode that runs to the end of the buffer means "the buffer ran
  out", not "the structure ended" — claiming otherwise would let a slice created
  without a length backfill its extent as *the whole rest of the file*. Give a
  PackBits slice an explicit length; that bound is the only real one.
- The structure **scan** can't find PackBits streams, since its criterion is a
  complete decode and nothing here ever fails to decode.

:data:`_MAX_OUT` bounds that openness in memory: an unbounded read (offset to
end-of-file) would otherwise expand to 128× the file's tail. Unlike the LZ
family's cap — where overshooting means the stream is corrupt, so it raises —
hitting this one just stops the decode at the last packet boundary, because a
PackBits stream has no end to be inconsistent with in the first place.

**Dialects we do not ship.** TGA's RLE repeats a whole *pixel* (1–4 bytes)
rather than a byte and flips the sense of the sign bit; Apple's ICNS variant
reads ``0x80``–``0xFF`` as a run of ``c - 125`` (3..130). Both are the same idea
with different arithmetic, and both belong to container formats celPix doesn't
read — so this module implements the canonical byte-oriented scheme only.

The encoder emits a run for every stretch of 3 or more equal bytes and packs
everything else into literal packets, both capped at 128 bytes. Byte-identity
with another packer's output is a non-goal; round-tripping is the contract.
"""

from __future__ import annotations

from celpix.core.context import (
    KEY_COMPRESSED_SIZE,
    KEY_DECOMPRESS_COMPLETE,
    PipelineContext,
)
from celpix.core.errors import Stage
from celpix.plugins.base import PluginInfo

# Output bytes one packet can carry, literal or run.
_MAX_PACKET = 128
# The no-op control byte (-128). Skipped on decode, never emitted on encode.
_NOP = 0x80
# Shortest run worth its own packet. A run costs 2 bytes; the same bytes appended
# to an *open* literal packet cost one each with no new control byte, so a 2-run
# only pays for itself when no literal packet is open to absorb it (handled
# below) — from 3 up, the run always wins.
_MIN_RUN = 3
# Memory guard for unbounded reads, not a format limit (see the module
# docstring): 1 MB is far past any tile bank a retro structure holds, while
# capping the 128× worst-case expansion of arbitrary bytes.
_MAX_OUT = 0x100000


def decompress(data: bytes) -> tuple[bytes, int]:
    """Decode a PackBits stream; returns ``(output, consumed)``.

    Decoding runs until the buffer is exhausted, the output cap is reached, or a
    packet is cut short by the end of the buffer — a truncated literal still
    contributes the bytes that *are* present, since a bounded view window
    routinely slices mid-packet. ``consumed`` is the end of the last *complete*
    packet, i.e. the offset a following structure could start at. It is not the
    structure's true extent: PackBits has no terminator to find one with.
    """
    out = bytearray()
    i, n = 0, len(data)
    consumed = 0
    while i < n and len(out) < _MAX_OUT:
        control = data[i]
        if control == _NOP:
            i += 1
        elif control < _NOP:
            count = control + 1
            chunk = data[i + 1 : i + 1 + count]
            out += chunk
            i += 1 + count
            if len(chunk) < count:  # buffer ended inside the literal
                break
        else:
            if i + 1 >= n:  # buffer ended before the run's value byte
                break
            out += bytes([data[i + 1]]) * (257 - control)
            i += 2
        consumed = i
    return bytes(out), consumed


def compress(data: bytes) -> bytes:
    """Encode raw bytes as a PackBits stream."""
    out = bytearray()
    literals = bytearray()

    def flush_literals() -> None:
        start = 0
        while start < len(literals):
            take = min(len(literals) - start, _MAX_PACKET)
            out.append(take - 1)
            out.extend(literals[start : start + take])
            start += take
        literals.clear()

    def emit_run(value: int, count: int) -> None:
        out.append(257 - count)
        out.append(value)

    i, n = 0, len(data)
    while i < n:
        value = data[i]
        run = 1
        while i + run < n and data[i + run] == value:
            run += 1
        i += run

        if run >= _MIN_RUN:
            flush_literals()
            while run >= _MIN_RUN:
                take = min(run, _MAX_PACKET)
                emit_run(value, take)
                run -= take
        # 0-2 bytes may remain, either a short run or a long one's tail. With a
        # literal packet already open they ride along for one byte each; with
        # none open, opening one to hold 2 bytes costs 3 where a run costs 2.
        if run == 2 and not literals:
            emit_run(value, 2)
        else:
            literals += bytes([value]) * run

    flush_literals()
    return bytes(out)


class PackBitsCompression:
    info = PluginInfo(
        id="compression.packbits",
        name="PackBits (TIFF / ILBM / MacPaint)",
        stage=Stage.COMPRESSION,
        # A pure byte stream: it ends where its container says it does, never
        # where the data does. The overlay phrases its status accordingly.
        self_delimiting=False,
    )

    def decompress(self, data: bytes, ctx: PipelineContext) -> bytes:
        out, consumed = decompress(data)
        ctx.set(KEY_COMPRESSED_SIZE, consumed)
        # Never complete: the format carries no end marker, so "we decoded to
        # here" is not "the structure ends here" (see the module docstring).
        ctx.set(KEY_DECOMPRESS_COMPLETE, False)
        return out

    def compress(self, data: bytes, ctx: PipelineContext) -> bytes:
        return compress(data)
