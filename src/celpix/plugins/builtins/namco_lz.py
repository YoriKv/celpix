"""Namco's Mega Drive LZSS, and the 2 KiB-ring variant the Strike games use.

The same stream body as the size-prefixed ring LZSS
(:mod:`~celpix.plugins.builtins.lzss_ring`) behind a different size prefix, and
in one variant a smaller ring. Namco's Mega Drive titles — Pac-Attack, Pac-Man 2,
Phelios, Marvel Land, Rolling Thunder 2, Klax, Burning Force and more — pack
graphics this way; Desert Strike, Jungle Strike and Urban Strike use the same
grammar over a 2048-byte ring::

    u16 be      uncompressed size, in bytes
    then, repeating:
      flags byte      8 op selectors, LSB first; 1 = literal, 0 = back-reference
      literal:        1 byte, copied out and into the ring
      back-reference: 2 bytes  b0, b1
                      ring position = b0 | ((b1 & 0xF0) << 4)   (12 bits)
                      length        = (b1 & 0x0F) + 3           (3..18)

    ring            Namco   4096 bytes, cursor starts at 0xFEE
                    Strike  2048 bytes, cursor starts at 0x7EE; the position
                            field's top bit is masked off
                    both    zero-filled

**What sets these apart from the ring LZSS is the prefix alone**: two bytes,
big-endian, where that scheme has four little-endian. Read one as the other and
the size comes out wrong by orders of magnitude, so a scan does not confuse them.

**The size prefix is the terminator**, as there: no end marker, the decode stops
on the declared count, and the source position it stopped at is the structure's
length. A zero size is rejected — the format has no magic, and accepting it would
make every pair of zero bytes in a ROM a structure — which leaves an empty
payload with no encoding, so :func:`compress` refuses one.

**The encoder reaches back 1024 bytes for Namco, not 4096.** The published
description of this format says the Namco window is 0x400 bytes while the decoder
it ships alongside keeps a 0x1000-byte ring, and no cartridge data was at hand to
settle which. A reference within 1024 bytes names the same byte under either
reading — ``(0xFEE + i) & 0x3FF`` is ``(0x3EE + i) & 0x3FF`` — so streams
written here unpack correctly whichever is true, at some cost in size. The
decoder follows the 4096-byte reading; a stream written against a 1024-byte ring
with stray top bits in its position fields would decode wrong under it, and is
the case to look for if real Namco data ever disagrees. The Strike variant's
decoder masks those bits itself, so its encoder uses the whole ring.

Format detail and provenance are in
``docs/graphics-formats-reference/implementation-guide.md`` §7.
"""

from __future__ import annotations

from celpix.core.errors import Stage
from celpix.plugins.base import PartialDecompression, PluginInfo

from . import lzss_ring

SIZE_BYTES = 2
BYTEORDER = "big"

NAMCO_RING = 0x1000
NAMCO_WINDOW = 0x400  # see the module docstring
STRIKE_RING = 0x800


def decompress(
    data: bytes, *, ring_size: int = NAMCO_RING, partial: bool = False
) -> tuple[bytes, int, bool]:
    """Decode one stream; ``(output, consumed, complete)`` as the ring LZSS."""
    return lzss_ring.decompress(
        data,
        partial=partial,
        size_bytes=SIZE_BYTES,
        byteorder=BYTEORDER,
        ring_size=ring_size,
    )


def compress(
    data: bytes, *, ring_size: int = NAMCO_RING, window: int = NAMCO_WINDOW
) -> bytes:
    """Encode ``data`` as one stream, reaching back at most ``window`` bytes."""
    if not data:
        raise ValueError("an empty payload has no encoding: a zero size is rejected")
    return lzss_ring.compress(
        data,
        size_bytes=SIZE_BYTES,
        byteorder=BYTEORDER,
        ring_size=ring_size,
        window=window,
    )


class NamcoLzCompression(PartialDecompression):
    info = PluginInfo(
        id="compression.namco-lz",
        name="Namco LZSS (Mega Drive)",
        stage=Stage.COMPRESSION,
        # The body has no end marker, but the size prefix bounds it.
        self_delimiting=True,
        category="Sega",
    )

    _decode = staticmethod(decompress)
    _encode = staticmethod(compress)


class StrikeLzCompression(PartialDecompression):
    info = PluginInfo(
        id="compression.strike-lz",
        name="LZSS, 2 KiB ring (Desert/Jungle/Urban Strike)",
        stage=Stage.COMPRESSION,
        self_delimiting=True,
        category="Sega",
    )

    def _decode(self, data: bytes, *, partial: bool) -> tuple[bytes, int, bool]:
        return decompress(data, ring_size=STRIKE_RING, partial=partial)

    def _encode(self, data: bytes) -> bytes:
        return compress(data, ring_size=STRIKE_RING, window=STRIKE_RING)
