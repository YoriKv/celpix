"""GBDK LZ — the Game Boy byte/word-run + back-reference packer, both directions.

The default scheme of GBDK-2020's packer, and the one its ``gb_decompress``
family unpacks — straight into VRAM tiles, if the game asks, writing only while
the LCD leaves VRAM open. The format predates GBDK: it is the one the 1990s Game
Boy Tile Designer and Map Builder saved their compressed exports in, so Game Boy
homebrew of every era carries it. The decoder also ships for the Master System /
Game Gear port.

A run of commands, each a one-byte token ``TTLLLLLL`` followed by its operands;
``LLLLLL + 1`` is the count, 1..64::

    00  value         byte run:   the value, count times
    01  a b           word run:   the pair a b, count times (2 * count bytes)
    10  lo hi         back-ref:   count bytes from (hi << 8 | lo) - 0x10000
                                  bytes back — a negative 16-bit offset,
                                  little-endian, so 1..65535 back
    11  bytes...      literal:    count bytes verbatim
    0x00              end of stream

Two things about that are easy to get wrong:

- **A one-byte run *is* the end marker.** Token ``0x00`` would be a byte run of
  count 1, so no stream can hold one; the encoder writes byte runs of 2 or more.
- **Copies are byte at a time, forward**, on the console and in the reference
  decoder alike, so a back-reference may overlap the bytes it is writing. The
  reference packer never writes one that does; this encoder does, since every
  decoder of the format reads it as the repeat it spells.

The word run is there for 2bpp tiles, whose two plane bytes repeat together
down a solid or striped row.

The encoder is a shortest-path parse by byte cost. Every command's cost is flat
over its count, so at each position the best count of each kind is the one that
leaves the cheapest remainder — a minimum over a slice of the costs already
priced, which runs in C rather than a loop per count. The reference packer is
greedy; this is never larger than it. Round-tripping is the contract, not
byte-identity.
"""

from __future__ import annotations

from celpix.core.errors import Stage
from celpix.plugins.base import PartialDecompression, PluginInfo
from celpix.plugins.builtins._lz import MatchFinder, copy_back

END = 0x00
BYTE_RUN, WORD_RUN, BACK_REF, LITERAL = 0x00, 0x40, 0x80, 0xC0
KIND_MASK, COUNT_MASK = 0xC0, 0x3F
MAX_COUNT = 64
MAX_DISTANCE = 0xFFFF

# Command sizes in bytes, token included; a literal adds its count.
BYTE_RUN_COST = 2
WORD_RUN_COST = 3
BACK_REF_COST = 3
LITERAL_COST = 1


def _fail(reason: str) -> ValueError:
    return ValueError(f"corrupt GBDK LZ stream: {reason}")


def decompress(data: bytes, *, partial: bool = False) -> tuple[bytes, int, bool]:
    """Unpack one stream from ``data[0]``; returns ``(plain, consumed, complete)``.

    ``complete`` is false only when the buffer ran out before the end byte, which
    ``partial`` downgrades from an error to the commands that did arrive.
    """
    out = bytearray()
    i, n = 0, len(data)
    while i < n:
        token = data[i]
        if token == END:
            return bytes(out), i + 1, True
        count = (token & COUNT_MASK) + 1
        kind = token & KIND_MASK
        if kind == BYTE_RUN:
            if i + 2 > n:
                break
            out += bytes((data[i + 1],)) * count
            i += 2
        elif kind == WORD_RUN:
            if i + 3 > n:
                break
            out += data[i + 1 : i + 3] * count
            i += 3
        elif kind == BACK_REF:
            if i + 3 > n:
                break
            distance = (0x10000 - (data[i + 1] | data[i + 2] << 8)) & 0xFFFF
            if distance == 0 or distance > len(out):
                raise _fail(
                    f"back-reference reaches {distance or 0x10000:,} bytes back "
                    f"into {len(out):,} bytes of output"
                )
            copy_back(out, distance, count)
            i += 3
        else:
            if i + 1 + count > n:
                break
            out += data[i + 1 : i + 1 + count]
            i += 1 + count
    if not partial:
        raise _fail(f"source ended after {len(out):,} bytes with no end byte")
    return bytes(out), n, False


def _repeat_lengths(data: bytes, period: int) -> list[int]:
    """How far the data from each position repeats with ``period``, capped.

    The byte run is period 1 and the word run period 2; either is only worth
    knowing up to the longest command, :data:`MAX_COUNT` units.
    """
    n = len(data)
    cap = MAX_COUNT * period
    lengths = [0] * (n + 1)
    for i in range(n - 1, -1, -1):
        if i + period < n and data[i] == data[i + period]:
            lengths[i] = min(lengths[i + 1] + 1, cap)
        else:
            lengths[i] = min(n - i, period)
    return lengths


def compress(data: bytes) -> bytes:
    """Encode ``data`` as one stream, as small as the commands allow."""
    n = len(data)
    run = _repeat_lengths(data, 1)
    pair = _repeat_lengths(data, 2)
    ref_len, ref_at = MatchFinder(
        data, min_match=BACK_REF_COST, window=MAX_DISTANCE
    ).all_longest(MAX_COUNT)

    # cost[i]: bytes to encode data[i:], the end byte aside. `ahead[j]` is
    # cost[j] + j, which turns a literal's 1 + k + cost[i + k] into a minimum
    # over one slice.
    cost = [0] * (n + 1)
    ahead = [0] * (n + 1)
    ahead[n] = n
    choice: list[tuple[int, int]] = [(LITERAL, 1)] * n

    for i in range(n - 1, -1, -1):
        top = min(n, i + MAX_COUNT) + 1
        tail = min(ahead[i + 1 : top])
        best = LITERAL_COST + tail - i
        pick = (LITERAL, ahead.index(tail, i + 1, top) - i)

        if run[i] >= 2:
            span = cost[i + 2 : i + run[i] + 1]
            tail = min(span)
            if BYTE_RUN_COST + tail < best:
                best = BYTE_RUN_COST + tail
                pick = (BYTE_RUN, span.index(tail) + 2)
        words = pair[i] // 2
        if words >= 2:
            span = cost[i + 4 : i + 2 * words + 1 : 2]
            tail = min(span)
            if WORD_RUN_COST + tail < best:
                best = WORD_RUN_COST + tail
                pick = (WORD_RUN, 2 * (span.index(tail) + 2))
        if ref_len[i]:
            span = cost[i + BACK_REF_COST : i + ref_len[i] + 1]
            tail = min(span)
            if BACK_REF_COST + tail < best:
                best = BACK_REF_COST + tail
                pick = (BACK_REF, span.index(tail) + BACK_REF_COST)

        cost[i] = best
        ahead[i] = best + i
        choice[i] = pick

    out = bytearray()
    at = 0
    while at < n:
        kind, length = choice[at]
        if kind == LITERAL:
            out.append(LITERAL | (length - 1))
            out += data[at : at + length]
        elif kind == BYTE_RUN:
            out += bytes((BYTE_RUN | (length - 1), data[at]))
        elif kind == WORD_RUN:
            out.append(WORD_RUN | (length // 2 - 1))
            out += data[at : at + 2]
        else:
            offset = (0x10000 - (at - ref_at[at])) & 0xFFFF
            out.append(BACK_REF | (length - 1))
            out += offset.to_bytes(2, "little")
        at += length
    out.append(END)
    return bytes(out)


class GbdkLzCompression(PartialDecompression):
    info = PluginInfo(
        id="compression.gbdk-lz",
        name="GBDK LZ",
        stage=Stage.COMPRESSION,
        self_delimiting=True,
        category="Nintendo",
    )

    _decode = staticmethod(decompress)
    _encode = staticmethod(compress)
