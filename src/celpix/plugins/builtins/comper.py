"""Comper and ComperX — word-granular LZSS built for decode speed.

Neither is a format any cartridge shipped with. Both were designed in the Sonic
hacking community for art that has to decompress fast on the 68000, which is
why everything is a 16-bit word: literals are words, distances and lengths
count words, and the flag field is itself a word::

    descriptor  u16 big-endian, bits consumed HIGH bit first, fetched only when
                a bit is wanted and none remain

    0           one literal WORD follows (2 bytes, copied as they are)
    1           two bytes d, l follow
                  l == 0     end of stream
                  otherwise  copy words from d and l, below, one at a time

The two differ only in how ``d`` and ``l`` are read:

===========  ================================  ================================
             Comper                            ComperX
===========  ================================  ================================
distance     ``0x100 - d`` words (d = 0: 256)  ``((-d) & 0xFF) + 1`` words
length       ``l + 1`` words (2..256)          ``0x100 - 2 * (l & 0x7F)
                                               + (l >> 7)`` words (2..255)
end marker   ``00 00``                         ``FF 00``
===========  ================================  ================================

ComperX's length is laid out so its decoder can jump straight into an unrolled
copy loop; the price is a longest match of 255 words rather than 256, since
``l = 0`` has to stay the terminator.

- **The payload must be a whole number of words.** An odd-length input has no
  encoding; the known packer pads it with a zero byte, which then decodes back
  as part of the data. celPix refuses it instead, so a round trip is exact.
- **The window is 256 words back and nothing before the first output word.** A
  reference past the start is corruption, not a read of zero fill.
- **Every form costs seventeen bits**, flag included, and a stream's remaining
  cost never grows as the position advances. So the longest match is always the
  cheapest choice, and the greedy parse is already the shortest path.

The moduled variants cut the payload into 4 KiB modules packed end to end
behind the size header :mod:`~celpix.plugins.builtins._moduled` describes.

Format detail and provenance are in
``docs/graphics-formats-reference/implementation-guide.md`` §7.
"""

from __future__ import annotations

from celpix.core.errors import Stage
from celpix.plugins.base import PartialDecompression, PluginInfo

from . import _moduled
from ._lz import FlagGroup, MatchFinder, copy_back

WINDOW_WORDS = 0x100
MIN_WORDS = 2
MAX_WORDS = {False: 0x100, True: 0xFF}  # keyed by `comperx`


def _name(comperx: bool) -> str:
    return "ComperX" if comperx else "Comper"


def _fail(comperx: bool, reason: str) -> ValueError:
    return ValueError(f"corrupt {_name(comperx)} stream: {reason}")


class _Truncated(Exception):
    """The stream ran out mid-op — recoverable only under ``partial``."""


def decompress(
    data: bytes, *, partial: bool = False, comperx: bool = False
) -> tuple[bytes, int, bool]:
    """Unpack one Comper (or, with ``comperx``, ComperX) stream from ``data[0]``.

    Returns ``(plain, consumed, complete)``; ``complete`` is false only when the
    buffer ran out before the end marker, which ``partial`` downgrades from an
    error to a short result.
    """
    out = bytearray()
    pos = 0
    flags = left = 0
    complete = False
    n = len(data)
    try:
        while True:
            if not left:
                if pos + 2 > n:
                    raise _Truncated
                flags, left = (data[pos] << 8) | data[pos + 1], 16
                pos += 2
            left -= 1
            if pos + 2 > n:
                raise _Truncated
            first, second = data[pos], data[pos + 1]
            pos += 2
            if not (flags >> left) & 1:
                out += bytes((first, second))
                continue
            if not second:
                complete = True
                break
            if comperx:
                distance = ((-first) & 0xFF) + 1
                length = 0x100 - ((second & 0x7F) << 1) + (second >> 7)
            else:
                distance = 0x100 - first
                length = second + 1
            if distance * 2 > len(out):
                raise _fail(
                    comperx,
                    f"match reaches {distance:,} words back "
                    f"into {len(out) // 2:,} words of output",
                )
            # Word-at-a-time copying at an even byte distance is the same
            # period-`distance` repetition as copying bytes.
            copy_back(out, distance * 2, length * 2)
    except _Truncated:
        if not partial:
            raise _fail(comperx, f"source ended after {len(out):,} bytes") from None

    return bytes(out), pos, complete


def compress(data: bytes, *, comperx: bool = False) -> bytes:
    """Encode ``data`` as one Comper (or ComperX) stream, longest match first."""
    if len(data) % 2:
        raise ValueError(
            f"{_name(comperx)} packs 16-bit words, and the input is "
            f"{len(data):,} bytes — an odd length has no encoding"
        )
    words = tuple((data[i] << 8) | data[i + 1] for i in range(0, len(data), 2))
    lengths, offsets = MatchFinder(
        words, min_match=MIN_WORDS, window=WINDOW_WORDS
    ).all_longest(MAX_WORDS[comperx])

    out = bytearray()
    flags = FlagGroup(out, msb_first=True, set_means_match=True, width=16)
    at = 0
    while at < len(words):
        length = lengths[at]
        if not length:
            flags.select(is_match=False)
            out += data[2 * at : 2 * at + 2]
            at += 1
            continue
        distance = at - offsets[at]
        flags.select(is_match=True)
        if comperx:
            out.append((1 - distance) & 0xFF)
            out.append((0x7F - ((length - 2) >> 1)) | ((length & 1) << 7))
        else:
            out.append((-distance) & 0xFF)
            out.append(length - 1)
        at += length

    flags.select(is_match=True)
    out += b"\xff\x00" if comperx else b"\x00\x00"
    flags.finish()
    return bytes(out)


class _ComperBase(PartialDecompression):
    _comperx = False

    def _decode(self, data: bytes, *, partial: bool) -> tuple[bytes, int, bool]:
        return decompress(data, partial=partial, comperx=self._comperx)

    def _encode(self, data: bytes) -> bytes:
        return compress(data, comperx=self._comperx)


class _ComperModuledBase(_ComperBase):
    def _decode(self, data: bytes, *, partial: bool) -> tuple[bytes, int, bool]:
        return _moduled.decompress(
            data,
            super()._decode,
            padding=1,
            name=f"{_name(self._comperx)} moduled",
            partial=partial,
        )

    def _encode(self, data: bytes) -> bytes:
        return _moduled.compress(
            data,
            super()._encode,
            padding=1,
            name=f"{_name(self._comperx)} moduled",
        )


class ComperCompression(_ComperBase):
    info = PluginInfo(
        id="compression.comper",
        name="Comper (word LZSS, hacks and homebrew)",
        stage=Stage.COMPRESSION,
        self_delimiting=True,  # the 00 00 end marker
        category="Sega",
    )


class ComperXCompression(_ComperBase):
    _comperx = True
    info = PluginInfo(
        id="compression.comperx",
        name="ComperX (word LZSS, hacks and homebrew)",
        stage=Stage.COMPRESSION,
        self_delimiting=True,  # the FF 00 end marker
        category="Sega",
    )


class ComperModuledCompression(_ComperModuledBase):
    info = PluginInfo(
        id="compression.comper-moduled",
        name="Comper, moduled (4 KiB streams behind a size header)",
        stage=Stage.COMPRESSION,
        self_delimiting=True,  # the header's size fixes the module count
        category="Sega",
    )


class ComperXModuledCompression(_ComperModuledBase):
    _comperx = True
    info = PluginInfo(
        id="compression.comperx-moduled",
        name="ComperX, moduled (4 KiB streams behind a size header)",
        stage=Stage.COMPRESSION,
        self_delimiting=True,  # the header's size fixes the module count
        category="Sega",
    )
