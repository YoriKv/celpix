"""GBA/NDS BIOS Huffman — the handheld HuffUnComp ROM call, both directions.

The BIOS's Huffman decoder (`SWI 0x13`; on the DS the read-by-callback variant),
for 4-bit or 8-bit symbols. Stream shape::

    byte0        0x24 / 0x28  high nibble 2 = Huffman; low nibble = symbol size in
                              bits (4 or 8)
    bytes 1..3   uint24 le    decompressed size, in bytes
    byte 4       tree size    ts: the tree table (this byte included) is (ts+1)*2
                              bytes, so the bitstream starts at 4 + (ts+1)*2
    bytes 5..    tree nodes, root first (the root is at byte 5)
    then         bitstream, in 32-bit little-endian words, each read from
                 bit 31 down to bit 0

    internal node:  bits 0-5  offset
                    bit 7     the child reached by a 0 bit ("left", node0) is data
                    bit 6     the child reached by a 1 bit ("right", node1) is data
                    node0 is at (node_addr & ~1) + offset*2 + 2, node1 right after it
    data node:      the symbol (upper bits zero for 4-bit symbols)

Decoding walks from the root, one bit per step, and emits the data node it lands
on, then restarts at the root. 4-bit symbols fill each output byte **low nibble
first** (the BIOS ORs symbol *k* of a 32-bit block in at bit ``k * 4``).

Traps, all silent:

- **Node addresses are pair-aligned** (``node_addr & ~1``): children always sit
  as a pair at an even address, and the root is the odd byte of the pair that
  starts with the tree-size byte. Computing the child from the node's own
  address instead of its pair's is off by one for every odd node.
- **The flag bits are crossed**: bit 7 belongs to node0 (the 0 branch) and bit 6
  to node1. Swap them and a stream still decodes, to noise.
- **The bitstream is 32-bit words, MSB first, stored little-endian.** Reading
  bytes MSB-first instead scrambles every word.
- **Output is written in 32-bit units**, so the BIOS keeps decoding until the
  output reaches the declared size *rounded up to a multiple of 4 bytes*, and
  reads the padding symbols' bits too. This decoder returns exactly the
  declared size but ``consumed`` covers the words the BIOS reads, padding
  included (bounded by the buffer); the encoder writes those padding symbols
  (the most frequent one, repeated) so the BIOS never reads past the stream.
- **The offset is 6 bits**, so a node's children must be at most 64 pairs after
  the node's own pair. A breadth-first tree layout overflows that for 8-bit
  data with many distinct symbols (a complete 256-leaf tree needs offsets up to
  ~127). The encoder lays the tree out in a deadline-driven order instead (see
  :func:`_layout`).

**The symbol width is the stream's own choice, not the call's.** The one BIOS
entry point reads it from the header, so a 4-bit and an 8-bit encoding of the
same bytes are equally valid wherever either is. The plugin's save therefore
writes whichever of the two comes out smaller — the slot a save-back must fit is
the only thing the choice affects. :func:`compress` still takes the width, for a
caller that wants one in particular.

Sources: GBATEK, "BIOS Decompression Functions" (HuffUnComp) for the header,
tree-size, node and bitstream fields; mGBA's HLE BIOS (``src/gba/bios.c``,
``_unHuffman``) for the pair-aligned child address, the 32-bit output blocks
(low nibble first) and the rounded-up output extent. The format's write-up is in
``docs/graphics-formats-reference/implementation-guide.md`` §7.
"""

from __future__ import annotations

import heapq
from collections import Counter

from celpix.core.errors import Stage
from celpix.plugins.base import PartialDecompression, PluginInfo
from celpix.plugins.builtins.gba_lz77 import ALIGNMENT, HEADER_SIZE, MAX_DECOMPRESSED

HUFFMAN_TYPE = 0x20  # high nibble 2; the low nibble is the symbol width
SYMBOL_BITS = (4, 8)
ROOT = HEADER_SIZE + 1  # the root node's byte, relative to the stream start
MAX_OFFSET = 0x3F
MAX_TABLE = 512  # the size byte counts pairs: (0xFF + 1) * 2


def _fail(reason: str) -> ValueError:
    return ValueError(f"corrupt GBA Huffman stream: {reason}")


def decompress(data: bytes, *, partial: bool = False) -> tuple[bytes, int, bool]:
    """Decode a BIOS Huffman stream (4- or 8-bit symbols, from the header).

    Returns ``(output, consumed, complete)``. ``complete`` means the declared
    size was produced. With ``partial`` a buffer that ends inside the bitstream
    yields the bytes decoded so far instead of raising; a truncated tree, a node
    pointing outside the tree, or a bad header still raise.
    """
    if len(data) < HEADER_SIZE + 1:
        raise _fail("shorter than the header and tree-size byte")
    bits = data[0] & 0x0F
    if data[0] & 0xF0 != HUFFMAN_TYPE or bits not in SYMBOL_BITS:
        raise _fail(f"type byte {data[0]:#04x} is not a Huffman header (0x24 or 0x28)")
    target = int.from_bytes(data[1:HEADER_SIZE], "little")
    if target == 0:
        raise _fail("declared decompressed size is zero")
    stream_at = HEADER_SIZE + (data[HEADER_SIZE] + 1) * 2
    if stream_at > len(data):
        raise _fail(f"tree table runs to byte {stream_at:,}, past the end of the data")

    mask = (1 << bits) - 1
    per_byte = 8 // bits
    want = target * per_byte  # symbols that make up the declared size
    if not partial and want > (len(data) - stream_at) * 8:
        # Every symbol costs at least one bit: the buffer cannot hold the stream.
        # Checked up front because a bit-at-a-time decode of a noise header's
        # multi-megabyte size is what makes a scan slow.
        raise _fail(
            f"{target:,} bytes cannot fit in the "
            f"{len(data) - stream_at:,}-byte bitstream"
        )
    bios_want = -(-target // 4) * 4 * per_byte  # the BIOS decodes whole words

    symbols: list[int] = []
    src = stream_at
    node_at = ROOT
    node = data[node_at]
    n = len(data)
    while len(symbols) < bios_want and src + 4 <= n:
        word = int.from_bytes(data[src : src + 4], "little")
        src += 4
        for shift in range(31, -1, -1):
            # Pair-aligned: from the node's pair, not the node's own address.
            child = (node_at & ~1) + (node & MAX_OFFSET) * 2 + 2
            if word >> shift & 1:
                child += 1
                is_data = node & 0x40
            else:
                is_data = node & 0x80
            if child >= stream_at:
                raise _fail(
                    f"tree node at byte {node_at} points outside the tree "
                    f"(byte {child})"
                )
            if is_data:
                symbols.append(data[child] & mask)
                node_at = ROOT
                if len(symbols) >= bios_want:
                    break
            else:
                node_at = child
            node = data[node_at]

    complete = len(symbols) >= want
    if not complete and not partial:
        raise _fail(
            f"source ended after {len(symbols) // per_byte:,} of {target:,} bytes"
        )
    symbols = symbols[:want]
    if bits == 8:
        out = bytes(symbols)
    else:
        # Low nibble first; a partial decode's odd trailing nibble is dropped.
        out = bytes(
            symbols[i] | (symbols[i + 1] << 4) for i in range(0, len(symbols) - 1, 2)
        )
    return out, src, complete


# -- compression ------------------------------------------------------------


class _Node:
    __slots__ = ("left", "right", "symbol")

    def __init__(
        self, symbol: int = -1, left: _Node | None = None, right: _Node | None = None
    ):
        self.symbol, self.left, self.right = symbol, left, right

    @property
    def leaf(self) -> bool:
        return self.left is None


def _build_tree(counts: Counter[int]) -> _Node:
    heap: list[tuple[int, int, _Node]] = []
    for order, (symbol, weight) in enumerate(sorted(counts.items())):
        heap.append((weight, order, _Node(symbol)))
    heapq.heapify(heap)
    if len(heap) == 1:
        # A tree needs two children at the root; one symbol takes both (a 1-bit
        # code).
        only = heap[0][2]
        return _Node(left=only, right=only)
    order = len(heap)
    while len(heap) > 1:
        wa, _, a = heapq.heappop(heap)
        wb, _, b = heapq.heappop(heap)
        heapq.heappush(heap, (wa + wb, order, _Node(left=a, right=b)))
        order += 1
    return heap[0][2]


def _layout(root: _Node) -> bytes:
    """Serialise the tree (size byte included), keeping every offset in 6 bits.

    The table is a sequence of pairs; pair 0 is the size byte plus the root, and
    each internal node's two children form one later pair within 64 pairs of its
    own. Placing children is a unit-time scheduling problem: each placed pair
    creates up to two new jobs, each due 64 pairs later. Earliest-deadline-first
    (breadth-first) lets the pending set grow past 64 on wide trees; depth-first
    starves old siblings. So: take the earliest deadline whenever the pending
    set's slack is nearly used up, otherwise the job that creates the fewest new
    jobs (latest first), which keeps the pending set small.
    """
    table = bytearray([0, 0])  # size byte (filled in last), root
    # (deadline, sequence, node, its byte index); sequence breaks ties LIFO.
    pending: list[tuple[int, int, _Node, int]] = [(MAX_OFFSET + 1, 0, root, 1)]
    seq = 0
    pair = 1
    while pending:
        pending.sort(key=lambda job: job[0])
        urgent = any(job[0] - pair <= rank + 1 for rank, job in enumerate(pending))
        if urgent:
            pick = 0
        else:
            pick = min(
                range(len(pending)),
                key=lambda i: (
                    (not pending[i][2].left.leaf) + (not pending[i][2].right.leaf),
                    -pending[i][1],
                ),
            )
        deadline, _, node, at = pending.pop(pick)
        if pair > deadline:
            raise ValueError(
                "Huffman tree cannot be laid out within the 6-bit node offsets"
            )
        offset = pair - at // 2 - 1
        flags = 0
        table += b"\x00\x00"
        for side, child in enumerate((node.left, node.right)):
            index = 2 * pair + side
            if child.leaf:
                flags |= 0x80 >> side
                table[index] = child.symbol
            else:
                seq += 1
                pending.append((pair + MAX_OFFSET + 1, seq, child, index))
        table[at] = flags | offset
        pair += 1
    if len(table) % 4:
        table += b"\x00\x00"  # keep the bitstream word-aligned
    if len(table) > MAX_TABLE:
        raise ValueError("Huffman tree table exceeds the 8-bit size field")
    table[0] = len(table) // 2 - 1
    return bytes(table)


def _codes(root: _Node) -> dict[int, tuple[int, int]]:
    codes: dict[int, tuple[int, int]] = {}
    stack = [(root, 0, 0)]
    while stack:
        node, code, length = stack.pop()
        if node.leaf:
            codes.setdefault(node.symbol, (code, length))
            continue
        stack.append((node.right, (code << 1) | 1, length + 1))
        stack.append((node.left, code << 1, length + 1))
    return codes


def compress(data: bytes, *, bits: int = 8) -> bytes:
    """Encode raw bytes as a BIOS Huffman stream of ``bits``-bit symbols (4 or 8)."""
    if bits not in SYMBOL_BITS:
        raise ValueError(f"symbol size must be 4 or 8 bits, not {bits}")
    n = len(data)
    if n == 0:
        # A zero size is what decompress refuses, so writing one would save a
        # stream this plugin cannot open again.
        raise ValueError("GBA BIOS Huffman has no encoding for an empty payload")
    if n > MAX_DECOMPRESSED:
        raise ValueError(
            f"input is {n:,} bytes; the 24-bit size field holds {MAX_DECOMPRESSED:,}"
        )
    if bits == 8:
        symbols = list(data)
    else:
        symbols = [s for byte in data for s in (byte & 0x0F, byte >> 4)]
    counts = Counter(symbols)
    # The BIOS decodes whole 32-bit output blocks; write the padding symbols so it
    # never reads bits past the stream. The most frequent symbol is the cheapest.
    per_byte = 8 // bits
    pad = (-(-n // 4) * 4 - n) * per_byte
    symbols += [counts.most_common(1)[0][0]] * pad

    root = _build_tree(counts)
    table = _layout(root)
    codes = _codes(root)

    out = bytearray([HUFFMAN_TYPE | bits])
    out += n.to_bytes(3, "little")
    out += table
    acc = 0
    filled = 0
    for symbol in symbols:
        code, length = codes[symbol]
        acc = (acc << length) | code
        filled += length
        while filled >= 32:
            filled -= 32
            out += ((acc >> filled) & 0xFFFFFFFF).to_bytes(4, "little")
            acc &= (1 << filled) - 1
    if filled:
        out += ((acc << (32 - filled)) & 0xFFFFFFFF).to_bytes(4, "little")
    return bytes(out)


def compress_smallest(data: bytes) -> bytes:
    """Whichever of the 4- and 8-bit encodings is shorter; 8-bit on a tie.

    Either decodes under the one BIOS call, which reads the width from the
    header, so the shorter is simply the better stream (see the module
    docstring).
    """
    return min((compress(data, bits=8), compress(data, bits=4)), key=len)


class GbaHuffmanCompression(PartialDecompression):
    info = PluginInfo(
        id="compression.gba-huffman",
        name="GBA/NDS BIOS Huffman (SWI 0x13)",
        stage=Stage.COMPRESSION,
        # No end marker, but the header's 24-bit size bounds the structure.
        self_delimiting=True,
        alignment=ALIGNMENT,
        category="Nintendo",
    )

    _decode = staticmethod(decompress)
    _encode = staticmethod(compress_smallest)
