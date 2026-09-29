"""GBDK RLE — the byte RLE GBDK's runtime library streams maps and tiles from.

One of the three schemes GBDK-2020 ships a decoder for (with
:mod:`~celpix.plugins.builtins.gbdk_lz` and :mod:`~celpix.plugins.builtins.zx0`),
and the only one with a decoder on every port — Game Boy, Master System / Game
Gear, MSX and NES. Its decoder is *resumable*: a game initialises it once at the
stream and then asks for a row's worth of bytes at a time, which is how a map
wider than VRAM scrolls in column by column. The ROM holds one stream all the
same, and that stream is what this reads.

A run of packets, each a one-byte control ``c`` followed by its data:

| control ``c`` | meaning |
|---|---|
| ``0x00``            | **end of stream** |
| ``0x01``–``0x7F``   | **literal**: copy the next ``c`` bytes verbatim |
| ``0x80``–``0xFF``   | **run**: repeat the next byte ``256 - c`` times |

The same shape as PackBits (:mod:`~celpix.plugins.builtins.packbits`) with the
arithmetic shifted by one: a literal states its own count rather than one less,
which is what frees ``0x00`` to terminate.

**``0x80`` is a 128-byte run on the console.** Every assembly decoder counts a
run up from ``c`` to zero, writing before it steps, so ``0x80`` writes 128
bytes; the host-side reference decoder masks the count to 7 bits and reads it as
zero. This follows the console. The encoder caps runs at 127, so the stream it
writes decodes alike under both.

The encoder emits a run for every stretch of 3 or more equal bytes, and for a
lone pair when no literal packet is open to absorb it (2 bytes, against the 3 a
new literal packet would cost). Round-tripping is the contract, not
byte-identity with the reference packer.
"""

from __future__ import annotations

from celpix.core.errors import Stage
from celpix.plugins.base import PartialDecompression, PluginInfo
from celpix.plugins.builtins._rle import pack_runs

END = 0x00
# The run/literal boundary: below it a literal count, from it a negated run.
RUN = 0x80
# The longest packet the encoder writes; see the module docstring for 0x80.
MAX_PACKET = 127
MIN_RUN = 3


def _fail(reason: str) -> ValueError:
    return ValueError(f"corrupt GBDK RLE stream: {reason}")


def decompress(data: bytes, *, partial: bool = False) -> tuple[bytes, int, bool]:
    """Unpack one stream from ``data[0]``; returns ``(plain, consumed, complete)``.

    ``complete`` is false only when the buffer ran out before the end byte, which
    ``partial`` downgrades from an error to the packets that did arrive.
    """
    out = bytearray()
    i, n = 0, len(data)
    while i < n:
        control = data[i]
        if control == END:
            return bytes(out), i + 1, True
        if control < RUN:
            if i + 1 + control > n:
                break
            out += data[i + 1 : i + 1 + control]
            i += 1 + control
        else:
            if i + 1 >= n:
                break
            out += bytes((data[i + 1],)) * (256 - control)
            i += 2
    if not partial:
        raise _fail(f"source ended after {len(out):,} bytes with no end byte")
    return bytes(out), n, False


def compress(data: bytes) -> bytes:
    out = bytearray()
    pack_runs(
        data,
        out,
        literal_header=lambda count: count,
        run_header=lambda count: 256 - count,
        max_packet=MAX_PACKET,
        min_run=MIN_RUN,
        spill_pair_as_run=True,
    )
    out.append(END)
    return bytes(out)


class GbdkRleCompression(PartialDecompression):
    info = PluginInfo(
        id="compression.gbdk-rle",
        name="GBDK RLE",
        stage=Stage.COMPRESSION,
        self_delimiting=True,
        category="Nintendo",
    )

    _decode = staticmethod(decompress)
    _encode = staticmethod(compress)
