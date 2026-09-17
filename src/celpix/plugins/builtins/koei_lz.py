"""Koei's SNES LZ — LZSS whose match codes ride a second, interleaved channel.

The scheme Koei's Super Famicom strategy titles pack graphics and text with:
Romance of the Three Kingdoms III and IV, Gemfire, Genghis Khan II, Uncharted
Waters: New Horizons. There is **no header and no container** — a stream is a
bare blob at an arbitrary ROM offset, its unpacked size implicit — so finding
one is a scan, and the end marker is the only thing that says where it stops.

**Two channels share one cursor**, which is what sets this apart from every
other LZSS here::

    byte channel    flag bytes and literal bytes
    bit  channel    match codes, read MSB first out of 16-bit LITTLE-ENDIAN
                    words fetched inline, from the same cursor, at the moment
                    the reader's 16-bit window would run dry

So a stream opens with one word — priming the window before anything else is
read — and the words after it land wherever the match codes happen to exhaust
it, in among the flag and literal bytes. A reader that splits the buffer into a
byte part and a bit part cannot decode this format; both halves have to be
driven off one position.

Flag bits are consumed MSB first, eight per byte, a new byte fetched when the
last is spent: **1 emits one literal byte, 0 introduces a match** whose length
and distance are read from the bit channel.

The length code is unary-prefixed — ``z`` leading zeros pick a bucket, and its
payload is ``z`` bits wide::

    1                 2                0000001 xxxxxx    65..128
    01 x              3..4             0000000 xxxxxxx  129..255
    001 xx            5..8
    0001 xxx          9..16            0000000 1111111  end of stream
    00001 xxxx       17..32
    000001 xxxxx     33..64

The distance code is **not** that regular. Distance is the encoded value; the
byte copied is ``out[-(distance + 1)]``, so the reach is 1..4096 bytes back. The
near half is four code widths whose value ranges do not fall on power-of-two
boundaries — 24 codes in the 8-bit bucket, 96 in the 9-bit — so it is stated as
ranges, which is also how the ROM implements it (a 192-entry index table)::

    6 bits   code 0x00..0x03    distance = code             0..3
    7 bits   code 0x08..0x0B    distance = code - 0x04      4..7
    8 bits   code 0x18..0x2F    distance = code - 0x10      8..31
    9 bits   code 0x60..0xBF    distance = code - 0x40     32..127

Those tile the 9-bit prefix space 0x000..0x0BF exactly; everything above is the
far half, where the prefixes are clean::

    011 + 7      128..255      110 + 10   1024..2047
    100 + 8      256..511      111 + 11   2048..4095
    101 + 9      512..1023

**The end marker is recognised but not consumed.** Its fourteen bits are
routinely the last in the blob, and consuming them would make the reader demand
a word the stream does not contain — so it is read out of the window and the
decode stops with the cursor where it is, which is the structure's length.

Format detail, the derivation and the byte-exactness check are in
``docs/graphics-formats-reference/koei-formats.md`` §1.
"""

from __future__ import annotations

from celpix.core.errors import Stage
from celpix.plugins.base import PartialDecompression, PluginInfo
from celpix.plugins.builtins._lz import (
    FlagGroup,
    MatchFinder,
    copy_back,
    parse_greedy,
)

WINDOW = 0x1000  # the furthest a match can reach back, in bytes
MIN_MATCH = 2
MAX_MATCH = 255
# A corrupt stream can spell matches forever, having no size to run out of.
# 16 MiB is far past any cartridge asset.
OUTPUT_CAP = 16 * 1024 * 1024

# Length buckets by leading-zero count: (code bits, payload bits, first count).
# The last has seven of each rather than eight and seven, which is what caps the
# format at 255 and leaves its top payload free to end the stream.
_LENGTH_CODES = (
    (1, 0, 2),
    (2, 1, 3),
    (3, 2, 5),
    (4, 3, 9),
    (5, 4, 17),
    (6, 5, 33),
    (7, 6, 65),
    (7, 7, 129),
)
_LONG_LENGTH = len(_LENGTH_CODES) - 1  # the bucket the end marker lives in
_LENGTH_PEEK = _LENGTH_CODES[_LONG_LENGTH][0]  # bits a bucket is decided by
_END_PAYLOAD = 0x7F

# Near distances: (code bits, lowest code, highest code, bias).
_NEAR_CODES = (
    (6, 0x00, 0x03, 0x00),
    (7, 0x08, 0x0B, 0x04),
    (8, 0x18, 0x2F, 0x10),
    (9, 0x60, 0xBF, 0x40),
)
# Far distances by three-bit prefix, from 0b011 up: (payload bits, first).
_FAR_CODES = ((7, 0x80), (8, 0x100), (9, 0x200), (10, 0x400), (11, 0x800))
_FIRST_FAR_PREFIX = 0b011
_FIRST_FAR_DISTANCE = 0x80

WORD_BYTES = 2
WORD_BITS = 16
FLAG_BITS = 8


def _fail(reason: str) -> ValueError:
    return ValueError(f"corrupt Koei LZ stream: {reason}")


class _Truncated(Exception):
    """The stream ran out mid-op — recoverable only under ``partial``."""


class _Reader:
    """Both channels over one buffer, sharing :attr:`pos`.

    ``_window`` always holds the next sixteen unconsumed bits of the match
    channel, which is what lets a code be recognised before it is consumed —
    the end marker is read that way and never taken. Refilling it is what pulls
    a word out of the shared cursor, so where the words land among the flag and
    literal bytes is decided by the order of reads, not by any framing.
    """

    def __init__(self, data: bytes) -> None:
        self._data = data
        self.pos = 0
        self._flags = 0
        self._left = 0
        self._window = self._word()
        self._held = self._window  # the word the window is draining
        self._avail = 0  # bits of it not yet in the window

    def byte(self) -> int:
        if self.pos >= len(self._data):
            raise _Truncated
        value = self._data[self.pos]
        self.pos += 1
        return value

    def _word(self) -> int:
        return self.byte() | (self.byte() << 8)

    def flag(self) -> int:
        if not self._left:
            self._flags = self.byte()
            self._left = FLAG_BITS
        self._left -= 1
        return (self._flags >> self._left) & 1

    def peek(self, count: int) -> int:
        return self._window >> (WORD_BITS - count)

    def take(self, count: int) -> None:
        self._window = (self._window << count) & 0xFFFF
        if self._avail < count:
            need = count - self._avail
            self._window |= (self._held & ((1 << self._avail) - 1)) << need
            self._held = self._word()
            self._avail = WORD_BITS - need
            count = need
        else:
            self._avail -= count
        self._window |= (self._held >> self._avail) & ((1 << count) - 1)


def _read_length(reader: _Reader) -> int | None:
    """The next match's length, or ``None`` at the end marker.

    The marker's bits are deliberately left in the window: see the module
    docstring for why consuming them would demand a word that is not there.
    """
    # The bucket is the run of leading zeros, which the widest code exhausts.
    bucket = _LENGTH_PEEK - reader.peek(_LENGTH_PEEK).bit_length()
    code_bits, payload_bits, first = _LENGTH_CODES[bucket]
    payload = reader.peek(code_bits + payload_bits) & ((1 << payload_bits) - 1)
    if bucket == _LONG_LENGTH and payload == _END_PAYLOAD:
        return None
    reader.take(code_bits + payload_bits)
    return first + payload


def _read_distance(reader: _Reader) -> int:
    """The next match's distance — one less than how far back it reaches."""
    prefix = reader.peek(3)
    if prefix >= _FIRST_FAR_PREFIX:
        payload_bits, first = _FAR_CODES[prefix - _FIRST_FAR_PREFIX]
        distance = first + (reader.peek(3 + payload_bits) & ((1 << payload_bits) - 1))
        reader.take(3 + payload_bits)
        return distance
    for code_bits, lowest, highest, bias in _NEAR_CODES:
        code = reader.peek(code_bits)
        if lowest <= code <= highest:
            reader.take(code_bits)
            return code - bias
    raise _fail(f"no distance code at 0b{reader.peek(9):09b}")


def decompress(data: bytes, *, partial: bool = False) -> tuple[bytes, int, bool]:
    """Unpack one stream from ``data[0]``.

    Returns ``(plain, consumed, complete)``; ``complete`` is false only when the
    buffer ran out before the end marker, which ``partial`` downgrades from an
    error to a short result.
    """
    if len(data) < WORD_BYTES:
        raise _fail("a stream opens with a 16-bit code word")
    reader = _Reader(data)
    out = bytearray()
    complete = False

    try:
        while True:
            while reader.flag():
                out.append(reader.byte())
            length = _read_length(reader)
            if length is None:
                complete = True
                break
            distance = _read_distance(reader)
            if distance >= len(out):
                raise _fail(
                    f"match reaches {distance + 1:,} bytes back "
                    f"into {len(out):,} bytes of output"
                )
            if len(out) + length > OUTPUT_CAP:
                raise _fail(f"output would exceed {OUTPUT_CAP:,} bytes")
            copy_back(out, distance + 1, length)
    except _Truncated:
        if not partial:
            raise _fail(f"source ended after {len(out):,} bytes") from None

    return bytes(out), reader.pos, complete


# -- compression ------------------------------------------------------------


class _Words:
    """The match channel's bits, and the word slots they reserve in ``out``.

    A word cannot be written when its turn comes: its sixteen bits belong to
    codes still ahead of it. So a slot is *reserved* — two bytes appended at the
    position the reader will fetch them from — and filled in at the end.

    Which is the crux of writing this format. A slot is due exactly when the
    reader's window would run dry, and the reader's state is recoverable from
    one number: having consumed ``n`` bits it has fetched ``1 + ceil(n / 16)``
    words, the leading one being the window's priming read. So :meth:`code`
    tracks ``n`` and reserves whatever that count has grown to, which puts every
    word at the byte the reader will reach for it.
    """

    __slots__ = ("_bits", "_consumed", "_out", "_slots")

    def __init__(self, out: bytearray) -> None:
        self._out = out
        self._bits: list[int] = []
        self._consumed = 0
        self._slots: list[int] = []
        self._reserve()  # the priming word, ahead of the first flag byte

    def _reserve(self) -> None:
        self._slots.append(len(self._out))
        self._out += b"\0\0"

    def _write(self, value: int, count: int) -> None:
        self._bits += [(value >> shift) & 1 for shift in range(count - 1, -1, -1)]

    def code(self, value: int, count: int) -> None:
        """Write one code the reader consumes, reserving any word it needs."""
        self._write(value, count)
        self._consumed += count
        while len(self._slots) < 1 + -(-self._consumed // WORD_BITS):
            self._reserve()

    def marker(self, value: int, count: int) -> None:
        """Write the end marker, which the reader reads without consuming.

        It needs no slot of its own: the reader is holding sixteen bits at the
        point it reads them, and this code is fourteen.
        """
        self._write(value, count)

    def finish(self) -> None:
        """Fill every reserved slot with the bits that landed in it."""
        self._bits += [0] * (len(self._slots) * WORD_BITS - len(self._bits))
        for index, at in enumerate(self._slots):
            word = 0
            for bit in self._bits[index * WORD_BITS : (index + 1) * WORD_BITS]:
                word = (word << 1) | bit
            self._out[at] = word & 0xFF
            self._out[at + 1] = word >> 8


def _length_code(length: int) -> tuple[int, int]:
    """``(value, bits)`` for a match of ``length`` bytes."""
    bucket = (length - 1).bit_length() - 1
    code_bits, payload_bits, first = _LENGTH_CODES[bucket]
    payload = length - first
    # Every bucket but the last opens with the 1 that ends its run of zeros.
    value = payload if bucket == _LONG_LENGTH else (1 << payload_bits) | payload
    return value, code_bits + payload_bits


def _distance_code(distance: int) -> tuple[int, int]:
    """``(value, bits)`` for a match ``distance + 1`` bytes back."""
    if distance >= _FIRST_FAR_DISTANCE:
        prefix = distance.bit_length() - 8 + _FIRST_FAR_PREFIX
        payload_bits, first = _FAR_CODES[prefix - _FIRST_FAR_PREFIX]
        return (prefix << payload_bits) | (distance - first), 3 + payload_bits
    for code_bits, _lowest, highest, bias in _NEAR_CODES:
        if distance + bias <= highest:
            return distance + bias, code_bits
    raise ValueError(f"distance {distance} is past the {WINDOW}-byte window")


def compress(data: bytes) -> bytes:
    """Encode ``data`` as one stream, greedily parsed.

    A nearer match is never dearer here and a match never costs more than the
    literals it replaces, so the longest-match parse the other LZ built-ins use
    needs no cost model on top of it. Byte-identity with the original ROM data
    is not claimed — the encoder that wrote it has not been characterised — only
    that what comes out unpacks to what went in.
    """
    out = bytearray()
    words = _Words(out)
    flags = FlagGroup(out, msb_first=True, set_means_match=False)
    finder = MatchFinder(data, min_match=MIN_MATCH, window=WINDOW)

    for pos, length, candidate in parse_greedy(
        data, finder, min_match=MIN_MATCH, max_match=MAX_MATCH
    ):
        if not length:
            flags.select(is_match=False)
            out.append(data[pos])
            continue
        flags.select(is_match=True)
        words.code(*_length_code(length))
        words.code(*_distance_code(pos - candidate - 1))

    flags.select(is_match=True)  # the marker sits where a length would
    flags.finish()
    words.marker(_END_PAYLOAD, sum(_LENGTH_CODES[_LONG_LENGTH][:2]))
    words.finish()
    return bytes(out)


class KoeiLzCompression(PartialDecompression):
    info = PluginInfo(
        id="compression.koei-lz",
        name="Koei LZ (SNES)",
        stage=Stage.COMPRESSION,
        # The saturated long-length code ends the stream.
        self_delimiting=True,
        category="Nintendo",
    )

    _decode = staticmethod(decompress)
    _encode = staticmethod(compress)
