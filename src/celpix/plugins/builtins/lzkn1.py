"""LZKN1 — Konami's Mega Drive LZSS.

The Mega Drive cousin of the arcade 1 KiB ring in
:mod:`~celpix.plugins.builtins.konami_lz` §1: the same three op forms behind the
same flag byte, with every field moved. Konami's own Mega Drive titles pack art
and maps this way — Contra: Hard Corps is the one it was reverse-engineered
from::

    +0  u16 big-endian   decoded size
    then, repeating:
      flag byte          8 selectors, LSB first; 0 = literal, 1 = an op byte
      op 0x1F            end of stream
      op 0x00..0x7F      long match, one more byte b
                           distance = ((op & 0x60) << 3) | b    (1..1023)
                           length   = (op & 0x1F) + 3           (3..33)
      op 0x80..0xBF      short match, no operand
                           distance = op & 0x0F                 (1..15)
                           length   = (op >> 4) - 6             (2..5)
      op 0xC0..0xFF      literal block: copy op - 0xB8 bytes    (8..71)

Beside the arcade scheme the differences are all silent — each reads the other's
streams to plausible garbage, never an error:

- **The long form keeps its length in the low five bits**, with the distance's
  top two bits above them; the arcade form is the other way round.
- **The short form's distance is not biased**, so it reaches 1..15, not 1..16,
  and a zero there names no byte at all.
- **``0xFF`` is a 71-byte literal block**, not the terminator: this one ends on
  ``0x1F``, which as a long op would be a 34-byte match within 256 bytes — so
  the encoder stops at 33, the one length the end op takes away.
- **The window is a plain 1023 bytes back** with nothing behind the first output
  byte — no zero-filled ring to reach into.

The size header is not what the decoder stops on; the end op is. It is checked
against the output afterwards, which is what makes a scan candidate at a wrong
offset fail loudly instead of decoding a few bytes of noise.

The moduled variant (:class:`Lzkn1ModuledCompression`) packs 4 KiB modules end
to end behind the size header :mod:`~celpix.plugins.builtins._moduled`
describes — each module a whole stream, its own size header included.

Format detail and provenance are in
``docs/graphics-formats-reference/konami-lz-formats.md``.
"""

from __future__ import annotations

from collections import deque

from celpix.core.errors import Stage
from celpix.plugins.base import PartialDecompression, PluginInfo

from . import _moduled
from ._lz import FlagGroup, MatchFinder, copy_back

HEADER_BYTES = 2
SIZE_LIMIT = 0xFFFF

END_OP = 0x1F
SHORT_OP = 0x80
BLOCK_OP = 0xC0

LONG_WINDOW = 0x3FF
LONG_MIN, LONG_MAX = 3, 33  # 34 within 256 bytes would spell the end op
SHORT_WINDOW = 0x0F
SHORT_MIN, SHORT_MAX = 2, 5
BLOCK_MIN, BLOCK_MAX = 8, 71
BLOCK_BIAS = BLOCK_OP - BLOCK_MIN

# Bits each form costs, its flag included; the parse is a shortest path over
# them. A block's cost grows with its length, eight bits a byte.
LITERAL_COST = 1 + 8
SHORT_COST = 1 + 8
LONG_COST = 1 + 16
BLOCK_COST = 1 + 8
END_COST = 1 + 8

OP_LITERAL, OP_SHORT, OP_LONG, OP_BLOCK = range(4)


def _fail(reason: str) -> ValueError:
    return ValueError(f"corrupt LZKN1 stream: {reason}")


class _Truncated(Exception):
    """The stream ran out mid-op — recoverable only under ``partial``."""


class _Reader:
    """Flag bits and bytes over one buffer, the flag byte fetched on demand."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self.pos = HEADER_BYTES
        self._flags = 1  # spent, so the first bit opens a byte

    def byte(self) -> int:
        if self.pos >= len(self._data):
            raise _Truncated
        value = self._data[self.pos]
        self.pos += 1
        return value

    def flag(self) -> int:
        if self._flags == 1:
            self._flags = 0x100 | self.byte()
        bit = self._flags & 1
        self._flags >>= 1
        return bit


def decompress(data: bytes, *, partial: bool = False) -> tuple[bytes, int, bool]:
    """Unpack one LZKN1 stream from ``data[0]``.

    Returns ``(plain, consumed, complete)``; ``complete`` is false only when the
    buffer ran out before the end op, which ``partial`` downgrades from an error
    to a short result.
    """
    if len(data) < HEADER_BYTES:
        raise _fail(f"shorter than the {HEADER_BYTES}-byte size header")
    declared = int.from_bytes(data[:HEADER_BYTES], "big")
    reader = _Reader(data)
    out = bytearray()
    complete = False
    try:
        while True:
            if not reader.flag():
                out.append(reader.byte())
            else:
                op = reader.byte()
                if op == END_OP:
                    complete = True
                    break
                if op >= BLOCK_OP:
                    for _ in range(op - BLOCK_BIAS):
                        out.append(reader.byte())
                else:
                    if op >= SHORT_OP:
                        distance, length = op & 0x0F, (op >> 4) - 6
                    else:
                        distance = ((op & 0x60) << 3) | reader.byte()
                        length = (op & 0x1F) + LONG_MIN
                    if not 0 < distance <= len(out):
                        raise _fail(
                            f"match reaches {distance:,} bytes back "
                            f"into {len(out):,} bytes of output"
                        )
                    copy_back(out, distance, length)
            if len(out) > declared:
                raise _fail(f"output passes the declared {declared:,} bytes")
    except _Truncated:
        if not partial:
            raise _fail(f"source ended after {len(out):,} bytes") from None

    if complete and len(out) != declared:
        raise _fail(f"decoded {len(out):,} bytes where the header says {declared:,}")
    return bytes(out), reader.pos, complete


def _parse(data: bytes) -> list[tuple[int, int, int]]:
    """The cheapest op sequence for ``data``, as ``(kind, length, distance)``.

    A shortest path over bit costs, solved right to left as Kosinski's is. The
    literal block is what makes it more than that: its cost depends on its
    length, so pricing every length 8..71 at every position would be 64 probes
    a byte. Its cost from ``i`` over ``n`` bytes is ``cost[i + n] + 8n`` plus a
    constant — ``cost[j] + 8j`` less ``8i`` — so the cheapest block is the least
    ``cost[j] + 8j`` over ``j`` in a window sliding left with ``i``, which a
    monotonic queue holds in constant time a step.
    """
    n = len(data)
    near_len, near_off = MatchFinder(
        data, min_match=SHORT_MIN, window=SHORT_WINDOW
    ).all_longest(SHORT_MAX)
    far_len, far_off = MatchFinder(
        data, min_match=LONG_MIN, window=LONG_WINDOW
    ).all_longest(LONG_MAX)

    cost = [0] * (n + 1)
    choice: list[tuple[int, int, int]] = [(OP_LITERAL, 1, 0)] * (n + 1)
    cost[n] = END_COST
    # Candidate block ends j, with cost[j] + 8j increasing front to back.
    ends: deque[int] = deque()

    for i in range(n - 1, -1, -1):
        j = i + BLOCK_MIN
        if j <= n:
            key = cost[j] + 8 * j
            while ends and cost[ends[-1]] + 8 * ends[-1] >= key:
                ends.pop()
            ends.append(j)
        while ends and ends[0] > i + BLOCK_MAX:
            ends.popleft()

        best = cost[i + 1] + LITERAL_COST
        pick = (OP_LITERAL, 1, 0)
        if near_len[i]:
            distance = i - near_off[i]
            for length in range(SHORT_MIN, min(near_len[i], SHORT_MAX) + 1):
                value = cost[i + length] + SHORT_COST
                if value < best:
                    best, pick = value, (OP_SHORT, length, distance)
        if far_len[i]:
            distance = i - far_off[i]
            # One cost whatever the length, but a shorter match can leave a tail
            # a cheaper form covers, so each is priced.
            for length in range(LONG_MIN, far_len[i] + 1):
                value = cost[i + length] + LONG_COST
                if value < best:
                    best, pick = value, (OP_LONG, length, distance)
        if ends:
            j = ends[0]
            value = cost[j] + 8 * (j - i) + BLOCK_COST
            if value < best:
                best, pick = value, (OP_BLOCK, j - i, 0)
        cost[i], choice[i] = best, pick

    ops = []
    at = 0
    while at < n:
        ops.append(choice[at])
        at += choice[at][1]
    return ops


def compress(data: bytes) -> bytes:
    """Encode ``data`` as one LZKN1 stream, as small as the forms allow."""
    if len(data) > SIZE_LIMIT:
        raise ValueError(
            f"input is {len(data):,} bytes; the LZKN1 size header holds {SIZE_LIMIT:,}"
        )
    out = bytearray(len(data).to_bytes(HEADER_BYTES, "big"))
    flags = FlagGroup(out, msb_first=False, set_means_match=True)
    at = 0
    for op, length, distance in _parse(data):
        if op == OP_LITERAL:
            flags.select(is_match=False)
            out.append(data[at])
        elif op == OP_SHORT:
            flags.select(is_match=True)
            out.append(((length + 6) << 4) | distance)
        elif op == OP_LONG:
            flags.select(is_match=True)
            out.append(((distance >> 3) & 0x60) | (length - LONG_MIN))
            out.append(distance & 0xFF)
        else:
            flags.select(is_match=True)
            out.append(length + BLOCK_BIAS)
            out += data[at : at + length]
        at += length
    flags.select(is_match=True)
    out.append(END_OP)
    flags.finish()
    return bytes(out)


class Lzkn1Compression(PartialDecompression):
    info = PluginInfo(
        id="compression.lzkn1",
        name="LZKN1 (Konami Mega Drive LZSS)",
        stage=Stage.COMPRESSION,
        self_delimiting=True,  # the 0x1F op
        category="Sega",
    )

    _decode = staticmethod(decompress)
    _encode = staticmethod(compress)


def decompress_moduled(
    data: bytes, *, partial: bool = False
) -> tuple[bytes, int, bool]:
    """Unpack a size header and 4 KiB LZKN1 modules packed end to end."""
    return _moduled.decompress(
        data, decompress, padding=1, name="LZKN1 moduled", partial=partial
    )


def compress_moduled(data: bytes) -> bytes:
    """Encode ``data`` as 4 KiB LZKN1 modules behind a size header."""
    return _moduled.compress(data, compress, padding=1, name="LZKN1 moduled")


class Lzkn1ModuledCompression(PartialDecompression):
    info = PluginInfo(
        id="compression.lzkn1-moduled",
        name="LZKN1, moduled (4 KiB streams behind a size header)",
        stage=Stage.COMPRESSION,
        self_delimiting=True,  # the header's size fixes the module count
        category="Sega",
    )

    _decode = staticmethod(decompress_moduled)
    _encode = staticmethod(compress_moduled)
