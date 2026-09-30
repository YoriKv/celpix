"""Kosinski+ — Kosinski's forms, rearranged for a faster 68000 unpacker.

Not a format any cartridge shipped with: it was designed for Sonic hacks and
homebrew that wanted Kosinski's ratio with less decode time, and it keeps every
match form Kosinski has while changing how the stream is laid out around them.
Anyone mapping a hack's ROM meets it where the original game had Kosinski::

    descriptor  one BYTE, bits consumed HIGH bit first, fetched only when a bit
                is wanted and none are left

    1            one literal byte follows
    0 0          inline match: one byte d follows, THEN two more descriptor
                 bits h l; distance 0x100 - d, length ((h<<1)|l) + 2   (2..5)
    0 1          separate match: two bytes high, low follow
                   count = high & 7
                   count != 0   length = 10 - count                    (3..9)
                   count == 0   a third byte n follows:
                                  0  end of stream
                                  n  length = n + 9                    (10..264)
                 distance = 0x2000 - (((high & 0xF8) << 5) | low)

Set beside :mod:`~celpix.plugins.builtins.kosinski`, every difference is one a
Kosinski decoder reads straight through into garbage rather than failing on:

- **The descriptor is a byte, MSB first, and refilled lazily** — so it sits
  where the first op needing it begins, with no dummy descriptor at the end.
- **The inline form reads its distance byte before its two length bits.** With
  a lazy refill that order is visible: when the descriptor runs out between the
  two, the next descriptor byte comes *after* the distance byte.
- **The separate form puts the high byte first**, and counts its short lengths
  down (``10 - count``) where Kosinski counts up.
- **There is no module-boundary no-op**: the three-byte form's ``n = 1`` is an
  ordinary ten-byte match, and the longest match is 264 rather than 256.

The forms and their costs are Kosinski's, so the encoder reuses its
shortest-path parse (:func:`~celpix.plugins.builtins.kosinski.parse`) and only
writes the result differently. The moduled variant
(:class:`KosinskiPlusModuledCompression`) packs 4 KiB modules end to end behind
the size header :mod:`~celpix.plugins.builtins._moduled` describes.

Format detail and provenance are in
``docs/graphics-formats-reference/implementation-guide.md`` §7.
"""

from __future__ import annotations

from celpix.core.errors import Stage
from celpix.plugins.base import PartialDecompression, PluginInfo

from . import _moduled
from ._lz import BitGroup, ByteSource, GroupReader, Truncated, copy_back, corrupt
from .kosinski import (
    DISTANCE_HIGH,
    FULL_WINDOW,
    INLINE_MIN,
    INLINE_WINDOW,
    OP_INLINE,
    OP_LITERAL,
    OP_SHORT,
    parse,
)

COUNT_MASK = 0x07
SHORT_BIAS = 10  # length = 10 - count
LONG_BIAS = 9  # length = n + 9
LONG_MAX = 0xFF + LONG_BIAS

END_MARKER = (0xF0, 0x00, 0x00)


_fail = corrupt("Kosinski+")


def decompress(data: bytes, *, partial: bool = False) -> tuple[bytes, int, bool]:
    """Unpack one Kosinski+ stream from ``data[0]``.

    Returns ``(plain, consumed, complete)``; ``complete`` is false only when the
    buffer ran out before the end marker, which ``partial`` downgrades from an
    error to a short result.
    """
    # Descriptor bits and payload bytes share one cursor, the descriptor byte
    # fetched only on demand.
    src = ByteSource(data)
    desc = GroupReader(src, msb_first=True)
    out = bytearray()
    complete = False
    try:
        while True:
            if desc.bit():
                out.append(src.byte())
                continue
            if desc.bit():
                high = src.byte()
                low = src.byte()
                count = high & COUNT_MASK
                if count:
                    length = SHORT_BIAS - count
                else:
                    count = src.byte()
                    if not count:
                        complete = True
                        break
                    length = count + LONG_BIAS
                distance = FULL_WINDOW - (((high & DISTANCE_HIGH) << 5) | low)
            else:
                distance = INLINE_WINDOW - src.byte()
                length = desc.bits(2) + INLINE_MIN
            if distance > len(out):
                raise _fail(
                    f"match reaches {distance:,} bytes back "
                    f"into {len(out):,} bytes of output"
                )
            copy_back(out, distance, length)
    except Truncated:
        if not partial:
            raise _fail(f"source ended after {len(out):,} bytes") from None

    return bytes(out), src.pos, complete


def compress(data: bytes) -> bytes:
    """Encode ``data`` as one Kosinski+ stream, as small as the forms allow."""
    out = bytearray()
    # The group reserves its byte where its first bit is pushed, which is exactly
    # where the lazy refill goes looking for it.
    desc = BitGroup(out, msb_first=True)
    bit = desc.bit
    at = 0
    for op, length, distance in parse(data, LONG_MAX):
        if op == OP_LITERAL:
            bit(1)
            out.append(data[at])
        elif op == OP_INLINE:
            code = length - INLINE_MIN
            bit(0)
            bit(0)
            out.append((INLINE_WINDOW - distance) & 0xFF)
            bit(code >> 1)
            bit(code & 1)
        else:
            value = FULL_WINDOW - distance
            high = (value >> 5) & DISTANCE_HIGH
            bit(0)
            bit(1)
            if op == OP_SHORT:
                out.append(high | (SHORT_BIAS - length))
                out.append(value & 0xFF)
            else:
                out.append(high)
                out.append(value & 0xFF)
                out.append(length - LONG_BIAS)
        at += length

    bit(0)
    bit(1)
    out += bytes(END_MARKER)
    desc.finish()
    return bytes(out)


class KosinskiPlusCompression(PartialDecompression):
    info = PluginInfo(
        id="compression.kosinski-plus",
        name="Kosinski+ (byte descriptors, hacks and homebrew)",
        stage=Stage.COMPRESSION,
        self_delimiting=True,  # the end marker
        category="Sega",
    )

    _decode = staticmethod(decompress)
    _encode = staticmethod(compress)


# Packed end to end, unlike Kosinski's 16-byte module boundaries.
KosinskiPlusModuledCompression, _MODULED = _moduled.moduled_plugin(
    KosinskiPlusCompression, "Kosinski+"
)
decompress_moduled = _MODULED.decompress
compress_moduled = _MODULED.compress
