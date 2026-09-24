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
from ._lz import FlagGroup, copy_back
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


def _fail(reason: str) -> ValueError:
    return ValueError(f"corrupt Kosinski+ stream: {reason}")


class _Truncated(Exception):
    """The stream ran out mid-op — recoverable only under ``partial``."""


class _Reader:
    """Descriptor bits and payload bytes, the descriptor fetched only on demand."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self.pos = 0
        self._flags = 0
        self._left = 0

    def byte(self) -> int:
        if self.pos >= len(self._data):
            raise _Truncated
        value = self._data[self.pos]
        self.pos += 1
        return value

    def bit(self) -> int:
        if not self._left:
            self._flags = self.byte()
            self._left = 8
        self._left -= 1
        return (self._flags >> self._left) & 1


def decompress(data: bytes, *, partial: bool = False) -> tuple[bytes, int, bool]:
    """Unpack one Kosinski+ stream from ``data[0]``.

    Returns ``(plain, consumed, complete)``; ``complete`` is false only when the
    buffer ran out before the end marker, which ``partial`` downgrades from an
    error to a short result.
    """
    reader = _Reader(data)
    out = bytearray()
    complete = False
    try:
        while True:
            if reader.bit():
                out.append(reader.byte())
                continue
            if reader.bit():
                high = reader.byte()
                low = reader.byte()
                count = high & COUNT_MASK
                if count:
                    length = SHORT_BIAS - count
                else:
                    count = reader.byte()
                    if not count:
                        complete = True
                        break
                    length = count + LONG_BIAS
                distance = FULL_WINDOW - (((high & DISTANCE_HIGH) << 5) | low)
            else:
                distance = INLINE_WINDOW - reader.byte()
                length = ((reader.bit() << 1) | reader.bit()) + INLINE_MIN
            if distance > len(out):
                raise _fail(
                    f"match reaches {distance:,} bytes back "
                    f"into {len(out):,} bytes of output"
                )
            copy_back(out, distance, length)
    except _Truncated:
        if not partial:
            raise _fail(f"source ended after {len(out):,} bytes") from None

    return bytes(out), reader.pos, complete


def compress(data: bytes) -> bytes:
    """Encode ``data`` as one Kosinski+ stream, as small as the forms allow."""
    out = bytearray()
    # Raw descriptor bits rather than op selectors: a set bit is written as set.
    # The group reserves its byte where its first bit is pushed, which is exactly
    # where the lazy refill goes looking for it.
    flags = FlagGroup(out, msb_first=True, set_means_match=True)

    def bit(value: int) -> None:
        flags.select(bool(value))

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
    flags.finish()
    return bytes(out)


def decompress_moduled(
    data: bytes, *, partial: bool = False
) -> tuple[bytes, int, bool]:
    """Unpack a size header and 4 KiB Kosinski+ modules packed end to end."""
    return _moduled.decompress(
        data, decompress, padding=1, name="Kosinski+ moduled", partial=partial
    )


def compress_moduled(data: bytes) -> bytes:
    """Encode ``data`` as 4 KiB Kosinski+ modules behind a size header."""
    return _moduled.compress(data, compress, padding=1, name="Kosinski+ moduled")


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


class KosinskiPlusModuledCompression(PartialDecompression):
    info = PluginInfo(
        id="compression.kosinski-plus-moduled",
        name="Kosinski+, moduled (4 KiB streams behind a size header)",
        stage=Stage.COMPRESSION,
        self_delimiting=True,  # the header's size fixes the module count
        category="Sega",
    )

    _decode = staticmethod(decompress_moduled)
    _encode = staticmethod(compress_moduled)
