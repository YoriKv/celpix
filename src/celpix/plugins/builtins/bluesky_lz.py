"""BlueSky Software's Mega Drive LZ + RLE, both directions.

The general-purpose packer in BlueSky Software's Mega Drive titles — Vectorman
and Vectorman 2, Jurassic Park and its Rampage Edition, Shadowrun, the World
Series Baseball games, Stimpy's Invention and more. An LZSS over a 2 KiB ring
whose two-byte op is either a back-reference or a byte fill, told apart by its
first byte's top bit::

    u16 be      uncompressed size MINUS ONE
    then, repeating:
      flags byte      8 op selectors, MSB first; 0 = literal, 1 = two-byte op
      literal:        1 byte, copied out and into the ring
      two-byte op:    b0, b1
        b0 & 0x80     back-reference
                        length   = (b0 & 0x0F) + 3                  (3..18)
                        distance = (b1 << 3) | ((b0 >> 4) & 7)      (11 bits;
                                   0 means 2048)
        otherwise     fill
                        length   = b0 + 3                           (3..130)
                        byte     = b1

The ring is 2048 bytes, zero-filled, with its write pointer at 0 — so unlike the
ring LZSS (:mod:`~celpix.plugins.builtins.lzss_ring`) the back-reference here is a
**distance back from the write pointer**, not an absolute position, and a
distance of zero wraps to the whole ring. A reference reaching before the first
output byte reads the zero fill.

Three things about it:

- **The size field is one less than the size.** Read it as the size and every
  stream decodes one byte short, which a scan reports as a stream that never
  completes rather than as a byte-order slip.
- **The size field is the terminator**, and the decode stops the moment it is
  reached, even part-way through an op or a flags byte. A match or fill that runs
  past it is cut short, as the reference decoder does. The encoder here never
  writes one.
- **There is no magic and nothing to cross-check**, so almost any bytes decode:
  a structure scan for this format finds candidates everywhere, and only the
  ones whose output makes sense are real. What the decoder does refuse is a
  buffer that ends before the declared size. The field's own encoding leaves no
  room for an empty payload, so :func:`compress` refuses one.

The 11-bit distance field and its subtraction from the ring pointer are the
parts of the decoder that match the 68000 routine these games carry; the header,
flag order and fill form come from the published description of the format and
have not been checked against cartridge data here.

The compressor is a shortest-path parse. A back-reference and a fill cost the
same two bytes, so what the path decides is only where literals are cheaper than
either. Byte-identity with the original packer is a non-goal.
"""

from __future__ import annotations

from celpix.core.errors import Stage
from celpix.plugins.base import PartialDecompression, PluginInfo

from ._lz import FlagGroup, MatchFinder, copy_from

SIZE_BYTES = 2
MAX_SIZE = 0x10000  # the size field stores size - 1

RING_SIZE = 0x800
MATCH_FLAG = 0x80
MIN_MATCH, MAX_MATCH = 3, 18
MIN_FILL, MAX_FILL = 3, 0x7F + 3

LITERAL_BITS = 1 + 8
OP_BITS = 1 + 16


def _fail(reason: str) -> ValueError:
    return ValueError(f"corrupt BlueSky LZ stream: {reason}")


def decompress(data: bytes, *, partial: bool = False) -> tuple[bytes, int, bool]:
    """Decode one stream at ``data[0]``.

    Returns ``(output, consumed, complete)``. ``complete`` is true when the full
    declared size was produced, making ``consumed`` the structure's length. With
    ``partial`` a buffer that ends first yields the prefix decoded so far.
    """
    if len(data) < SIZE_BYTES:
        raise _fail(f"shorter than the {SIZE_BYTES}-byte size field")
    target = int.from_bytes(data[:SIZE_BYTES], "big") + 1

    # The output preceded by the ring's zero fill: a distance reaching before the
    # first output byte reads zeros, exactly as the ring would.
    win = bytearray(RING_SIZE)
    end = RING_SIZE + target
    src = SIZE_BYTES
    n = len(data)

    while len(win) < end and src < n:
        flags = data[src]
        src += 1
        for bit in range(7, -1, -1):
            if len(win) >= end:
                break
            if not (flags >> bit) & 1:
                if src >= n:
                    break
                win.append(data[src])
                src += 1
                continue
            if src + 1 >= n:
                src = n
                break
            b0, b1 = data[src], data[src + 1]
            src += 2
            if b0 & MATCH_FLAG:
                length = (b0 & 0x0F) + MIN_MATCH
                distance = ((b1 << 3) | ((b0 >> 4) & 7)) or RING_SIZE
                copy_from(win, len(win) - distance, min(length, end - len(win)))
            else:
                win += bytes((b1,)) * min(b0 + MIN_FILL, end - len(win))

    out = bytes(win[RING_SIZE:])
    complete = len(out) == target
    if not complete and not partial:
        raise _fail(f"source ended after {len(out):,} of {target:,} bytes")
    return out, min(src, n), complete


# -- compression ------------------------------------------------------------


def compress(data: bytes) -> bytes:
    """Encode ``data`` (1..65536 bytes) as one stream."""
    n = len(data)
    if not n:
        raise ValueError("an empty payload has no encoding: the size field is size - 1")
    if n > MAX_SIZE:
        raise ValueError(f"input is {n:,} bytes; the size field holds {MAX_SIZE:,}")

    match_len, match_at = MatchFinder(
        data, min_match=MIN_MATCH, window=RING_SIZE
    ).all_longest(MAX_MATCH)
    fill = [1] * (n + 1)
    for i in range(n - 2, -1, -1):
        if data[i] == data[i + 1]:
            fill[i] = fill[i + 1] + 1

    # (kind, length): 0 literal, 1 back-reference, 2 fill.
    cost = [0] * (n + 1)
    choice: list[tuple[int, int]] = [(0, 1)] * n
    for i in range(n - 1, -1, -1):
        best, pick = cost[i + 1] + LITERAL_BITS, (0, 1)
        # Every length is priced, not just the longest: a shorter op can leave a
        # tail that a later, longer one covers better.
        for length in range(MIN_MATCH, match_len[i] + 1):
            if cost[i + length] + OP_BITS < best:
                best, pick = cost[i + length] + OP_BITS, (1, length)
        run = min(fill[i], MAX_FILL)
        if run >= MIN_FILL:
            for length in (*range(MIN_FILL, min(run, MAX_MATCH) + 1), run):
                if cost[i + length] + OP_BITS < best:
                    best, pick = cost[i + length] + OP_BITS, (2, length)
        cost[i], choice[i] = best, pick

    out = bytearray((n - 1).to_bytes(SIZE_BYTES, "big"))
    group = FlagGroup(out, msb_first=True, set_means_match=True)
    at = 0
    while at < n:
        kind, length = choice[at]
        group.select(kind != 0)
        if kind == 0:
            out.append(data[at])
        elif kind == 1:
            distance = (at - match_at[at]) & (RING_SIZE - 1)
            out.append(MATCH_FLAG | ((distance & 7) << 4) | (length - MIN_MATCH))
            out.append(distance >> 3)
        else:
            out.append(length - MIN_FILL)
            out.append(data[at])
        at += length
    group.finish()
    return bytes(out)


class BlueSkyLzCompression(PartialDecompression):
    info = PluginInfo(
        id="compression.bluesky-lz",
        name="BlueSky LZ + RLE (Mega Drive)",
        stage=Stage.COMPRESSION,
        # No end marker, but the size field bounds the stream.
        self_delimiting=True,
        category="Sega",
    )

    _decode = staticmethod(decompress)
    _encode = staticmethod(compress)
