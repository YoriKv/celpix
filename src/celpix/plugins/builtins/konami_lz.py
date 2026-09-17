"""Two Konami LZ schemes that share a name and nothing else.

Both are house LZSS variants from Konami's arcade lineage, both are called some
form of "Bemani LZ" in the wild, and **neither can read the other's streams**.
They are here together because that is the trap: told apart, each is ordinary.

===============  ==========================  ==========================
                 1 KiB ring (§1)             4 KiB ring (§2)
===============  ==========================  ==========================
history          1024 bytes                  4096 bytes
flag bit ``1``   an **opcode byte** follows  a **literal**
flag bit ``0``   a literal                   a back-reference
operand byte     read after **every** flag   only for a back-reference
match forms      three                       one
end of stream    opcode ``0xFF``             a back-reference at
                                             distance 0 (``00 00``)
===============  ==========================  ==========================

What they do share is the flag byte: eight selectors, **least-significant bit
first**, written in front of the ops it describes, refilled through the usual
sentinel trick (a high bit shifted down alongside the flags, so the byte is
spent after exactly eight). That idiom is common to half the LZSS dialects ever
written and identifies nothing.

Both keep a **zero-filled ring with its cursor at 0**, so a reference reaching
back past the first output byte reads zeros rather than failing. As in the ring
LZSS (:mod:`~celpix.plugins.builtins.lzss_ring`) the decoders here hold that as a
zero prefix in front of the output instead of a live ring — the same window seen
from the other side, and no modulo per byte.

---

**§1 — the 1 KiB ring.** Konami's older arcade scheme, in their data since at
least the mid-1990s. Every flag bit is followed by a byte; a set flag makes that
byte an opcode::

    op 0x00..0x7F   long match, one operand byte b
                      distance = b | ((op & 3) << 8)      (0..1023; 0 means 1024)
                      length   = (op >> 2) + 3            (3..34)
    op 0x80..0xBF   short match, no operand
                      distance = (op & 0x0F) + 1          (1..16)
                      length   = (op >> 4) - 6            (2..5)
    op 0xC0..0xFE   literal block: copy op - 0xB8 bytes   (8..70)
    op 0xFF         end of stream

The **literal block** is what sets this one apart from every other LZSS here: it
amortises one flag bit over up to seventy raw bytes, which is what makes
incompressible data cost 8.1 bits a byte instead of 9. It pays from ten bytes up,
and the encoder here uses it exactly there.

The **long form's distance of 0 means 1024, not 0** — it addresses the ring
position the cursor is sitting on, which after a full wrap is the byte 1024 back.
Read it as "no distance" and a stream decodes to garbage from that op on.

---

**§2 — the 4 KiB ring.** Konami's later scheme, used both over the wire for XML
and inside a good many of their file formats. One match form, two bytes::

    hi, lo          distance = (hi << 4) | (lo >> 4)      (12 bits)
                    length   = (lo & 0x0F) + 3            (3..18)
                    distance 0 ends the stream

That is the classic 4 KiB / 3..18 LZSS shape, and its length field is bit for bit
the ring LZSS's. Three things are not, and all three are silent:

- the 12-bit field is a **distance back from the cursor**, where the ring LZSS
  makes it an absolute ring position seeded at ``0xFEE``;
- it is packed **high byte first**, where the ring LZSS puts the low byte first;
- the stream **ends on distance 0** and carries no size prefix at all.

So distance 0 is not addressable, and the reach is 1..4095 rather than 1..4096.

Format detail and the comparison that separated these two is in
``docs/graphics-formats-reference/konami-lz-formats.md``.
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

# A corrupt stream of either scheme can spell matches until it runs out of
# buffer, so both decoders stop at a size no cartridge or arcade asset reaches.
OUTPUT_CAP = 16 * 1024 * 1024

# -- §1, the 1 KiB ring ------------------------------------------------------

RING_1K = 0x400
LONG_MIN, LONG_MAX = 3, 34  # (op >> 2) + 3
SHORT_MIN, SHORT_MAX = 2, 5  # (op >> 4) - 6
SHORT_MAX_DISTANCE = 16  # (op & 0x0F) + 1
BLOCK_MAX = 70  # op - 0xB8, so the form spans 8..70
BLOCK_BIAS = 0xB8
# A block costs a flag bit, its opcode and the bytes; individual literals cost a
# flag bit each. So it pays from ten bytes up, and ties at nine.
BLOCK_WORTH = 10
END_OP = 0xFF


def _fail(reason: str) -> ValueError:
    return ValueError(f"corrupt Konami LZ stream: {reason}")


class _Truncated(Exception):
    """The stream ran out mid-op — recoverable only under ``partial``."""


class _Reader:
    """Flag bits and bytes over one buffer, with the shared refill sentinel."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self.pos = 0
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


def decompress_1k(data: bytes, *, partial: bool = False) -> tuple[bytes, int, bool]:
    """Unpack one 1 KiB-ring stream from ``data[0]``.

    Returns ``(plain, consumed, complete)``; ``complete`` is false only when the
    buffer ran out before the ``0xFF`` opcode, which ``partial`` downgrades from
    an error to a short result.
    """
    reader = _Reader(data)
    # The prefix is the ring's width, so a reach can never fall before the
    # buffer: one past the first output byte lands in the zero fill instead.
    buf = bytearray(RING_1K)
    complete = False

    try:
        while True:
            is_op = reader.flag()
            op = reader.byte()  # read either way: for a literal it *is* the byte
            if not is_op:
                buf.append(op)
            elif op < 0x80:  # long match
                field = reader.byte() | ((op & 3) << 8)
                # The field addresses the ring position the cursor sits on, which
                # after a wrap is the byte a whole ring back — not no distance.
                distance = field if field else RING_1K
                copy_back(buf, distance, (op >> 2) + LONG_MIN)
            elif op < 0xC0:  # short match
                copy_back(buf, (op & 0x0F) + 1, (op >> 4) - 6)
            elif op == END_OP:
                complete = True
                break
            else:  # literal block
                for _ in range(op - BLOCK_BIAS):
                    buf.append(reader.byte())
            if len(buf) - RING_1K > OUTPUT_CAP:
                raise _fail(f"output would exceed {OUTPUT_CAP:,} bytes")
    except _Truncated:
        if not partial:
            raise _fail(f"source ended after {len(buf) - RING_1K:,} bytes") from None

    return bytes(buf[RING_1K:]), reader.pos, complete


def compress_1k(data: bytes) -> bytes:
    """Encode ``data`` as one 1 KiB-ring stream.

    A greedy parse, then one pass that gathers each run of literals into the
    block form wherever it pays. Two-byte matches only fit the short form, so one
    reaching further than sixteen bytes back is spelled as literals instead.
    """
    out = bytearray()
    # The set bit selects the opcode byte here, which the literal block uses too.
    flags = FlagGroup(out, msb_first=False, set_means_match=True)
    finder = MatchFinder(data, min_match=SHORT_MIN, window=RING_1K)

    ops: list[tuple[int, int, int]] = []  # (position, distance, length)
    for pos, length, candidate in parse_greedy(
        data, finder, min_match=SHORT_MIN, max_match=LONG_MAX
    ):
        distance = pos - candidate
        if length == SHORT_MIN and distance > SHORT_MAX_DISTANCE:
            ops += [(pos, 0, 0), (pos + 1, 0, 0)]  # unwritable: two literals
        elif length:
            ops.append((pos, distance, length))
        else:
            ops.append((pos, 0, 0))

    index = 0
    while index < len(ops):
        pos, distance, length = ops[index]
        if length:
            flags.select(is_match=True)
            if length <= SHORT_MAX and distance <= SHORT_MAX_DISTANCE:
                out.append(((length + 6) << 4) | (distance - 1))
            else:
                # The field holds 0..1023, and a full-ring reach wraps to zero.
                field = distance & (RING_1K - 1)
                out.append(((length - LONG_MIN) << 2) | ((field >> 8) & 3))
                out.append(field & 0xFF)
            index += 1
            continue

        run = index
        while run < len(ops) and not ops[run][2]:
            run += 1
        left = run - index
        while left >= BLOCK_WORTH:
            take = min(left, BLOCK_MAX)
            flags.select(is_match=True)
            out.append(take + BLOCK_BIAS)
            out += data[pos : pos + take]
            pos += take
            left -= take
        for byte in data[pos : pos + left]:
            flags.select(is_match=False)
            out.append(byte)
        index = run

    flags.select(is_match=True)
    flags.finish()
    out.append(END_OP)
    return bytes(out)


# -- §2, the 4 KiB ring ------------------------------------------------------

RING_4K = 0x1000
MATCH_MIN, MATCH_MAX = 3, 18  # (lo & 0x0F) + 3
# Distance 0 ends the stream, so it is not a distance anything can reach.
MAX_DISTANCE = RING_4K - 1


def decompress_4k(data: bytes, *, partial: bool = False) -> tuple[bytes, int, bool]:
    """Unpack one 4 KiB-ring stream from ``data[0]``.

    Returns ``(plain, consumed, complete)``; ``complete`` is false only when the
    buffer ran out before a zero-distance back-reference, which ``partial``
    downgrades from an error to a short result.
    """
    reader = _Reader(data)
    # The prefix is the ring's width, so a reach can never fall before the
    # buffer: one past the first output byte lands in the zero fill instead.
    buf = bytearray(RING_4K)
    complete = False

    try:
        while True:
            if reader.flag():
                buf.append(reader.byte())
            else:
                # One 16-bit field, high byte first: twelve bits of distance
                # then four of length. The two halves of `lo` split between them.
                hi, lo = reader.byte(), reader.byte()
                distance = (hi << 4) | (lo >> 4)
                if not distance:
                    complete = True
                    break
                copy_back(buf, distance, (lo & 0x0F) + MATCH_MIN)
            if len(buf) - RING_4K > OUTPUT_CAP:
                raise _fail(f"output would exceed {OUTPUT_CAP:,} bytes")
    except _Truncated:
        if not partial:
            raise _fail(f"source ended after {len(buf) - RING_4K:,} bytes") from None

    return bytes(buf[RING_4K:]), reader.pos, complete


def compress_4k(data: bytes) -> bytes:
    """Encode ``data`` as one 4 KiB-ring stream, greedily parsed."""
    out = bytearray()
    flags = FlagGroup(out, msb_first=False, set_means_match=False)
    finder = MatchFinder(data, min_match=MATCH_MIN, window=MAX_DISTANCE)

    for pos, length, candidate in parse_greedy(
        data, finder, min_match=MATCH_MIN, max_match=MATCH_MAX
    ):
        if not length:
            flags.select(is_match=False)
            out.append(data[pos])
            continue
        distance = pos - candidate
        flags.select(is_match=True)
        out.append((distance >> 4) & 0xFF)
        out.append(((distance & 0x0F) << 4) | (length - MATCH_MIN))

    flags.select(is_match=True)  # the end marker is a back-reference
    flags.finish()
    out += b"\x00\x00"
    return bytes(out)


class KonamiLz1kCompression(PartialDecompression):
    info = PluginInfo(
        id="compression.konami-lz-1k",
        name="Konami LZ (1 KiB ring, three op forms)",
        stage=Stage.COMPRESSION,
        self_delimiting=True,  # the 0xFF opcode
        category="Arcade",
    )

    _decode = staticmethod(decompress_1k)
    _encode = staticmethod(compress_1k)


class KonamiLz4kCompression(PartialDecompression):
    info = PluginInfo(
        id="compression.konami-lz-4k",
        name="Konami LZ (4 KiB ring, distance-addressed)",
        stage=Stage.COMPRESSION,
        self_delimiting=True,  # a back-reference at distance 0
        category="Arcade",
    )

    _decode = staticmethod(decompress_4k)
    _encode = staticmethod(compress_4k)
