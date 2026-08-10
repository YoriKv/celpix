"""Nemesis — the Mega Drive's nibble-run Huffman art codec, both directions.

The compression Sega shipped most of its Mega Drive tile art behind: a canonical
68000 decompressor that games call with a source pointer and a VDP write address,
so it turns up across first-party titles of the whole generation. It is *not* an
LZ: it codes 4bpp **pixel runs** directly, which is why the streams sit at nearly
8 bits of entropy per byte and every LZ probe bounces off them.

Stream shape::

    u16 be   bit 15 = XOR mode; bits 0..14 = number of 8x8 tiles
    code table, entries until a 0xFF byte:
        0x80..0x8F   set the palette index the following entries emit (& 0x0F)
        0x00..0x7F   run/length byte: run = ((b >> 4) & 7) + 1, bits = b & 0x0F
                     followed by one byte holding that many bits of prefix code
        0xFF         end of table
    bit stream, **MSB first**, until ``tiles * 8`` rows have been produced:
        111111       inline escape: 3 bits run-1, then 4 bits palette index
        <code>       a table entry: emit its run of its palette index

Four things about that layout cost real work to get wrong:

- **The symbol is a run, not a pixel.** Every entry carries both a palette index
  and how many times to emit it (1..8), so one code can fill a whole row. A
  decoder that emits one nibble per code produces a stream the right *shape* and
  the wrong length, which reads as art sliding out of alignment rather than as a
  failure.
- **The six-set-bit escape is checked before the code table**, and a table may
  legitimately contain 8-bit codes that *start* ``111111``. Matching shortest-code-
  first from length 1 without testing the escape first therefore decodes real
  streams into noise.
- **Rows are the output unit.** Nibbles accumulate into a 32-bit row (8 pixels)
  and only reach the buffer when the row is full, so ``tiles * 8`` rows is the
  terminator — there is no end marker in the bit stream.
- **XOR mode is against the previous emitted row, not the previous source row.**
  Roughly half the streams in a ROM set it; with the flag ignored the art decodes
  to a plausible-looking but wrong picture, because rows are then absolute values
  of what were meant to be deltas.

**On the encode side this deliberately does not reproduce Sega's compressor.**
Round-tripping is the contract, not byte-identity with any particular encoder
(the rule the LZ codecs next door keep). Sega's tool used Fano's method rather
than Huffman, so its exact code assignment differs; what this emits is a
different valid stream of its own, and re-encoding untouched art will not give
the bytes back.

**Which makes stream length the thing to watch.** A ROM packs these structures
back to back, so writing a re-encoded stream into the slot it came from is only
safe while it still fits: one byte longer moves every stream after it. The host's
slice bounds enforce that — a write that would overflow the slot is refused
rather than allowed to run over its neighbour — and the encoder is measured
against exactly that bar: over the 28 streams of a Mega Drive ROM it produces
22,310 bytes where the ROM stores 22,779, with no individual stream larger.

**The encode is a search, and what it spends is refinement.** A parse and a code
each depend on the other, so the encoder alternates them from a starting guess —
and where it starts decides where it lands, by several percent. Sixteen starts
are built (eight run caps × both modes) and *coded* once, which is cheap; the
expensive alternation then runs on the best four by that measure
(:data:`REFINE_SEEDS`). Refining all sixteen instead buys 22 bytes in 22,779 for
four times the work.

Format detail and the ROM-scanning notes are in
``docs/graphics-formats-reference/implementation-guide.md`` §7.
"""

from __future__ import annotations

from celpix.core.errors import Stage
from celpix.plugins.base import PartialDecompression, PluginInfo

HEADER_SIZE = 2
XOR_FLAG = 0x8000
TILE_COUNT_MASK = 0x7FFF

ROWS_PER_TILE = 8
NIBBLES_PER_ROW = 8
ROW_BYTES = 4

TABLE_END = 0xFF
PALETTE_MARK = 0x80
# The low nibble of a table byte, which carries the palette index on a marker and
# the code width on a run/length entry.
LOW_NIBBLE = 0x0F
RUN_MASK = 0x07  # the run nibble stores run - 1, so 1..8

MAX_CODE_BITS = 8
MAX_RUN = 8  # what the run nibble and the inline escape's 3 bits both hold

# Six set bits mean "the next 7 bits *are* the run and the index" — the format's
# escape for a symbol its Huffman table did not earn a code for.
INLINE_BITS = 6
INLINE_PREFIX = 0x3F
INLINE_RUN_BITS = 3
INLINE_INDEX_BITS = 4


def _fail(reason: str) -> ValueError:
    return ValueError(f"corrupt Nemesis stream: {reason}")


class _BitReader:
    """MSB-first bit reader over a stream whose end is not byte-aligned.

    Two things follow from that and neither is optional. **Zero bytes are read
    past the end on demand**: matching a symbol has to look ahead up to
    :data:`MAX_CODE_BITS` bits, so the last few real bits of a stream can only be
    decoded with something behind them — and a buffer cut to the structure's own
    length (a slice) has nothing there. Feeding zeros costs nothing, because the
    row count ends the decode before the padding can be mistaken for data.

    **``consumed`` counts only the real bytes a decode actually needed**, which is
    the byte count a slice must cover and how streams packed end-to-end in a ROM
    chain: counting the look-ahead fetch instead would push the next structure's
    start a byte or two late.
    """

    def __init__(self, data: bytes, pos: int) -> None:
        self._data = data
        self._pos = pos
        self._pad_bits = 0  # synthetic bits, always at the tail of the accumulator
        self._acc = 0
        self._bits = 0

    @property
    def _unread(self) -> int:
        """Real (non-padding) bits still sitting in the accumulator."""
        return self._bits - self._pad_bits

    @property
    def consumed(self) -> int:
        return self._pos - max(self._unread, 0) // 8

    @property
    def exhausted(self) -> bool:
        """True once no real bit is left to read, in the accumulator or behind it."""
        return self._unread <= 0 and self._pos >= len(self._data)

    def _fill(self, count: int) -> None:
        while self._bits < count:
            if self._pos < len(self._data):
                self._acc = (self._acc << 8) | self._data[self._pos]
                self._pos += 1
            else:
                self._acc <<= 8
                self._pad_bits += 8
            self._bits += 8

    def peek(self, count: int) -> int:
        self._fill(count)
        return (self._acc >> (self._bits - count)) & ((1 << count) - 1)

    def take(self, count: int) -> int:
        value = self.peek(count)
        self._bits -= count
        # Bits leave from the front, so padding is only eaten once everything
        # real ahead of it has gone.
        self._pad_bits = min(self._pad_bits, self._bits)
        self._acc &= (1 << self._bits) - 1
        return value


def _read_table(
    data: bytes, pos: int
) -> tuple[dict[tuple[int, int], tuple[int, int]], int]:
    """Parse the code table into ``(bits, code) -> (palette index, run)``."""
    table: dict[tuple[int, int], tuple[int, int]] = {}
    index = 0
    n = len(data)
    while True:
        if pos >= n:
            raise _fail("source ended inside the code table")
        entry = data[pos]
        pos += 1
        if entry == TABLE_END:
            return table, pos
        if entry & PALETTE_MARK:
            index = entry & LOW_NIBBLE
            continue
        run = ((entry >> 4) & RUN_MASK) + 1
        bits = entry & LOW_NIBBLE
        if not 1 <= bits <= MAX_CODE_BITS:
            raise _fail(f"code length {bits} is outside 1..{MAX_CODE_BITS}")
        if pos >= n:
            raise _fail("source ended before a prefix code")
        code = data[pos]
        pos += 1
        if code >> bits:
            # The code is stored right-aligned in one byte, so a value that needs
            # more than its declared width is not this format's table -- the check
            # is what stops a scan claiming arbitrary bytes as a stream.
            raise _fail(f"prefix code {code:#04x} does not fit in {bits} bits")
        if (bits, code) in table:
            raise _fail(f"prefix code {code:#04x}/{bits} appears twice")
        table[(bits, code)] = (index, run)


def decompress(data: bytes, *, partial: bool = False) -> tuple[bytes, int, bool]:
    """Decode a Nemesis stream.

    Returns ``(output, consumed, complete)``. ``complete`` is true when every
    declared tile was produced, making ``consumed`` the structure's true byte
    length — which is how the streams packed back-to-back in a ROM chain. With
    ``partial`` a buffer that ends mid-stream yields the tiles decoded so far
    instead of raising; a structurally invalid stream still raises.
    """
    if len(data) < HEADER_SIZE:
        raise _fail(f"shorter than the {HEADER_SIZE}-byte header")
    header = int.from_bytes(data[:HEADER_SIZE], "big")
    xor = bool(header & XOR_FLAG)
    tiles = header & TILE_COUNT_MASK
    if tiles == 0:
        # Accepting it would make two zero bytes -- the most common word in any
        # ROM -- a valid structure everywhere.
        raise _fail("header declares zero tiles")

    table, pos = _read_table(data, HEADER_SIZE)
    if not table:
        raise _fail("code table is empty")
    # One entry is legal and does occur: a sheet of a single colour, or any
    # sheet whose XOR deltas collapse to one repeated row, has exactly one
    # symbol to code. What keeps a scan honest is the table's own structure and
    # the body decoding to the declared tile count, not a minimum entry count.

    reader = _BitReader(data, pos)
    target_rows = tiles * ROWS_PER_TILE
    out = bytearray()
    row = 0
    filled = 0
    previous = 0

    while len(out) < target_rows * ROW_BYTES:
        if reader.exhausted:
            break
        if reader.peek(INLINE_BITS) == INLINE_PREFIX:
            reader.take(INLINE_BITS)
            run = reader.take(INLINE_RUN_BITS) + 1
            index = reader.take(INLINE_INDEX_BITS)
        else:
            for bits in range(1, MAX_CODE_BITS + 1):
                key = (bits, reader.peek(bits))
                if key in table:
                    reader.take(bits)
                    index, run = table[key]
                    break
            else:
                raise _fail(
                    f"no code table entry matches the next {MAX_CODE_BITS} bits "
                    f"at output tile {len(out) // (ROWS_PER_TILE * ROW_BYTES)}"
                )

        for _ in range(run):
            row = ((row << 4) | index) & 0xFFFFFFFF
            filled += 1
            if filled < NIBBLES_PER_ROW:
                continue
            if xor:
                row ^= previous
            previous = row
            out += row.to_bytes(ROW_BYTES, "big")
            row = 0
            filled = 0

    complete = len(out) == target_rows * ROW_BYTES
    if not complete and not partial:
        raise _fail(
            f"source ended after {len(out) // (ROWS_PER_TILE * ROW_BYTES)} "
            f"of {tiles} tiles"
        )
    return bytes(out), reader.consumed, complete


# -- compression ------------------------------------------------------------
#
# Byte-identity with Sega's own compressor is **not** the contract here, the same
# rule the other codecs keep: a re-encode has to decode back to the same pixels,
# not to the same bits. Sega's tool used Fano's method rather than Huffman, and
# reproducing its exact choices is a separate exercise from compressing well.


# What the escape costs to spell out in full: the 6-bit prefix, the run and the
# index. A symbol is only worth a code table entry if coding it beats this.
INLINE_COST = INLINE_BITS + INLINE_RUN_BITS + INLINE_INDEX_BITS
# One table entry is a run/length byte and a code byte. (The 0x80 index markers
# are counted separately: one buys every entry sharing that palette index.)
TABLE_ENTRY_BITS = 16

# The escape owns the whole `111111…` subtree, so a code may neither start with
# six set bits nor be a shorter all-ones prefix of them. Both live at the *top*
# of their length's range, which is why canonical codes assigned upward from zero
# stay clear of them for free — provided the code space they use leaves that
# subtree's worth of room.
CODE_SPACE = 1.0 - 2.0**-INLINE_BITS


Symbol = tuple[int, int]  # (palette index, run)


def _nibbles(rows: list[int]) -> list[int]:
    """The rows flattened into one pixel sequence — the thing that is coded.

    **Runs are free to cross a row boundary**, and a third of the ones in real
    Sega streams do. Nothing stops them: the decoder counts nibbles into a row
    and flushes on the eighth, entirely independently of how many nibbles the
    symbol it is unpacking has left. Parsing row by row instead — which the
    32-bit row and the 8-nibble run cap both invite — is a decode-identical
    mistake that silently costs about a sixth of the body, because every run
    landing on a boundary gets split into two symbols that each pay their own
    code.
    """
    pixels: list[int] = []
    for row in rows:
        pixels.extend((row >> shift) & 0x0F for shift in range(28, -1, -4))
    return pixels


def _greedy_parse(pixels: list[int], cap: int = MAX_RUN) -> list[Symbol]:
    """Split the pixel sequence into ``(index, run)`` symbols, longest run first.

    ``cap`` shortens the runs it will take, which is only useful as a *seed* for
    the refinement below: capping concentrates the frequency mass on fewer
    symbols, and which cap that pays off at is a property of the art nobody can
    read off it in advance.
    """
    symbols: list[Symbol] = []
    at, end = 0, len(pixels)
    while at < end:
        index = pixels[at]
        run = 1
        while run < cap and at + run < end and pixels[at + run] == index:
            run += 1
        symbols.append((index, run))
        at += run
    return symbols


def _cost_table(cost: dict[Symbol, int]) -> list[int]:
    """``cost`` as a flat ``index * MAX_RUN + run - 1`` lookup.

    The parse is the encoder's hot loop and its inner step is one cost lookup, so
    what that lookup *is* decides the encoder's speed: a symbol is a pair of small
    integers, and hashing a fresh tuple for each is several times the cost of
    indexing a list. Built once per parse, 128 entries, uncoded symbols filled
    with the inline price so the lookup never has to branch.
    """
    table = [INLINE_COST] * (16 * MAX_RUN)
    for (index, run), bits in cost.items():
        table[index * MAX_RUN + run - 1] = bits
    return table


def _cheapest_parse(pixels: list[int], cost: dict[Symbol, int]) -> list[Symbol]:
    """Re-split the pixel sequence to minimise coded bits under ``cost``.

    Longest-run-first is not the cheapest split either: a run of eight that
    earned a long code can be cheaper as two runs of four with short ones, and
    only the code lengths say which — so the parse and the code each depend on
    the other and neither can be chosen first. One shortest-path sweep settles
    the parse for a given code, over the at most eight runs starting at each
    pixel.

    Written against flat lists rather than dicts of pairs, and the locals bound
    up front, because this loop runs some four million times over one tile bank
    (every seed, every refinement pass, both modes) and is the whole of what the
    encoder spends. The **choice** is stored as the run alone — the index is
    ``pixels[at]``, which the walk back out already has — so the sweep allocates
    nothing per step.
    """
    end = len(pixels)
    table = _cost_table(cost)
    # ``best`` is bits-so-far, seeded past the end with a sentinel no real path
    # can reach, which is what lets the comparison below drop its None test.
    unreachable = INLINE_COST * (end + 1) + 1
    best = [0] + [unreachable] * end
    runs = [0] * (end + 1)
    for at in range(end):
        base = best[at]
        if base == unreachable:
            continue
        index = pixels[at]
        row = index * MAX_RUN
        limit = min(MAX_RUN, end - at)
        run = 0
        while run < limit and pixels[at + run] == index:
            run += 1
            total = base + table[row + run - 1]
            reached = at + run
            if total < best[reached]:
                best[reached] = total
                runs[reached] = run
    symbols: list[Symbol] = []
    at = end
    while at:
        run = runs[at]
        at -= run
        symbols.append((pixels[at], run))
    symbols.reverse()
    return symbols


def _package_merge(counts: dict[Symbol, int], limit: int) -> dict[Symbol, int]:
    """Optimal code lengths for ``counts``, none longer than ``limit`` bits.

    Plain Huffman is wrong here: it routinely wants codes past this format's
    8-bit ceiling, and truncating them afterwards is both invalid and lossy.
    Package-merge solves the length-limited problem directly — think of a code as
    buying, for each symbol, coins totalling 1: a length-``L`` code is a coin of
    denomination ``2**-L``, and the cheapest complete purchase *is* the optimal
    code. Coins are repeatedly paired ("packaged") into the next denomination up
    and merged with that denomination's own coins; the ``2n - 2`` cheapest items
    of the final list name each symbol as many times as its code is long.
    """
    symbols = sorted(counts, key=lambda s: (counts[s], s))
    if len(symbols) == 1:
        return {symbols[0]: 1}
    base = [(counts[symbol], (index,)) for index, symbol in enumerate(symbols)]
    items = base
    for _ in range(limit - 1):
        packaged = [
            (items[at][0] + items[at + 1][0], items[at][1] + items[at + 1][1])
            for at in range(0, len(items) - 1, 2)
        ]
        items = sorted(packaged + base)
    lengths = dict.fromkeys(symbols, 0)
    for _, members in items[: 2 * len(symbols) - 2]:
        for index in members:
            lengths[symbols[index]] += 1
    return lengths


def _fit_code_space(counts: dict[Symbol, int], lengths: dict[Symbol, int]) -> None:
    """Lengthen the rarest codes until the escape's subtree is free again.

    Package-merge spends the *whole* code space, and this format has one subtree
    already spoken for. Giving a slot back means one symbol going a bit longer;
    taking it from the rarest is what makes that cost the least.
    """
    kraft = sum(2.0**-length for length in lengths.values())
    for symbol in sorted(counts, key=lambda s: (counts[s], s)):
        if kraft <= CODE_SPACE:
            return
        while lengths[symbol] < MAX_CODE_BITS and kraft > CODE_SPACE:
            kraft -= 2.0 ** -(lengths[symbol] + 1)
            lengths[symbol] += 1
    if kraft > CODE_SPACE:  # pragma: no cover - 128 symbols at 8 bits is half of it
        raise ValueError("too many Nemesis symbols to code in 8 bits")


def _code_lengths(counts: dict[Symbol, int]) -> dict[Symbol, int]:
    lengths = _package_merge(counts, MAX_CODE_BITS)
    _fit_code_space(counts, lengths)
    return lengths


def _assign_codes(lengths: dict[Symbol, int]) -> dict[Symbol, tuple[int, int]]:
    """Canonical codes for ``lengths``, as ``symbol -> (bits, code)``."""
    ordered = sorted(lengths, key=lambda s: (lengths[s], s))
    codes: dict[Symbol, tuple[int, int]] = {}
    code = 0
    previous = lengths[ordered[0]]
    for symbol in ordered:
        code <<= lengths[symbol] - previous
        previous = lengths[symbol]
        # Assigned upward from zero and inside CODE_SPACE, so the escape's
        # subtree is never reached; the assert is what keeps that a fact rather
        # than a comment if the length fitting above is ever changed.
        assert code >> max(previous - INLINE_BITS, 0) != INLINE_PREFIX
        assert code >> previous == 0
        codes[symbol] = (previous, code)
        code += 1
    return codes


def _worth_coding(counts: dict[Symbol, int]) -> dict[Symbol, tuple[int, int]]:
    """The symbols that earn a table entry, with their codes.

    A symbol used once costs 16 table bits plus its code to encode and 13 bits to
    inline, so the tail of the frequency distribution is cheaper spelled out.
    Which symbols those are depends on the code lengths, which depend on which
    symbols are coded — settled by iterating until the set stops shrinking.
    """
    coded = dict(counts)
    while len(coded) > 2:
        lengths = _code_lengths(coded)
        dropped = {
            symbol
            for symbol, length in lengths.items()
            if coded[symbol] * (INLINE_COST - length) < TABLE_ENTRY_BITS
        }
        if not dropped or len(dropped) >= len(coded) - 2:
            break
        for symbol in dropped:
            del coded[symbol]
    if len(coded) < 2:
        # The decoder rejects a table it cannot build a prefix code from, and two
        # entries is the floor. Keeping the two commonest symbols is free.
        coded = dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:2])
    return _assign_codes(_code_lengths(coded))


# How many times to re-parse against the code the last parse produced. Each pass
# is one shortest-path sweep and one package-merge; on Mega Drive art the second
# pass is worth a few percent and the fourth is worth nothing.
_REFINE_PASSES = 4

# How many of the sixteen seeds are refined. Refinement is the encoder's whole
# cost — one pass re-parses every pixel — and it is strongly seed-sensitive, so
# the old answer was to refine all sixteen and keep the best. Ranking them by
# their *unrefined* size first turns that into a choice: the cheap coding pass
# already separates the promising starts from the hopeless ones, and refining the
# top four lands within 0.1% of refining all of them for a quarter of the work.
#
# Measured over the 28 Nemesis streams of a Mega Drive ROM: all sixteen give
# 22,288 bytes against the ROM's own 22,779, the best four give 22,310, and the
# best one alone gives 22,332 — every one of them smaller than the ROM's, which
# is the bar that matters, since a stream that grows will not fit its slot.
REFINE_SEEDS = 4

# The run caps refinement is seeded from (see :func:`_seeds`).
SEED_CAPS = range(1, MAX_RUN + 1)


def symbol_counts(symbols: list[Symbol]) -> dict[Symbol, int]:
    """How often each symbol appears, counted through a flat list.

    The same trick :func:`_cost_table` plays and for the same reason: hashing a
    pair per symbol is most of the work in counting a few hundred thousand of
    them, where an index into 128 slots is not. The dict comes back out because
    that is what :func:`_worth_coding` is written against, and it holds at most
    128 entries whatever the art.
    """
    tally = [0] * (16 * MAX_RUN)
    for index, run in symbols:
        tally[index * MAX_RUN + run - 1] += 1
    return {
        (slot // MAX_RUN, slot % MAX_RUN + 1): count
        for slot, count in enumerate(tally)
        if count
    }


def _stream(tiles: int, symbols: list[Symbol], xor: bool) -> bytes:
    """Assemble the table and bit stream for one parse.

    The emit loop is flat and the packing is inline, for the reason
    :func:`_cheapest_parse` is written the way it is: this runs once per parse —
    sixty-odd times over one tile bank — and a closure call plus a tuple hash per
    symbol is the difference between the two halves of the encoder costing the
    same and one of them costing nothing.
    """
    codes = _worth_coding(symbol_counts(symbols))

    table = bytearray()
    marked = -1
    for index, run in sorted(codes, key=lambda s: (s[0], codes[s][0], codes[s][1])):
        if index != marked:
            table.append(PALETTE_MARK | index)
            marked = index
        bits, code = codes[(index, run)]
        table.append(((run - 1) << 4) | bits)
        table.append(code)
    table.append(TABLE_END)

    # The coded symbols as two flat lists, and a width of 0 meaning "no code" —
    # which is the same test as the dict miss it replaces.
    widths = [0] * (16 * MAX_RUN)
    values = [0] * (16 * MAX_RUN)
    for (index, run), (bits, code) in codes.items():
        slot = index * MAX_RUN + run - 1
        widths[slot], values[slot] = bits, code

    body = bytearray()
    append = body.append
    acc = held = 0
    for index, run in symbols:
        slot = index * MAX_RUN + run - 1
        width = widths[slot]
        if width:
            value = values[slot]
        else:
            # The inline escape, as one 13-bit value rather than three pushes:
            # six set bits, then run-1 in three, then the index in four.
            width = INLINE_COST
            value = (
                (INLINE_PREFIX << (INLINE_RUN_BITS + INLINE_INDEX_BITS))
                | ((run - 1) << INLINE_INDEX_BITS)
                | index
            )
        acc = (acc << width) | value
        held += width
        while held >= 8:
            held -= 8
            append((acc >> held) & 0xFF)
        acc &= (1 << held) - 1
    if held:  # the stream ends mid-byte; the tile count is what stops the decode
        append((acc << (8 - held)) & 0xFF)

    header = (XOR_FLAG if xor else 0) | tiles
    return header.to_bytes(HEADER_SIZE, "big") + bytes(table) + bytes(body)


def _refine(
    tiles: int, pixels: list[int], symbols: list[Symbol], xor: bool, seeded: bytes
) -> bytes:
    """Alternate parse and code from one seed, keeping the smallest stream.

    The two chase each other: a cheaper parse changes the frequencies, which
    changes the code, which changes what the cheapest parse is. It settles within
    two or three passes, and the smallest seen is kept rather than the last —
    a pass is not guaranteed to improve on the one before it.

    ``seeded`` is the seed's own stream, which the caller has already built to
    rank the seeds with (:func:`compress`) — it is the first candidate, and
    re-deriving it here would be one of these passes' worth of work thrown away.
    """
    best = seeded
    for _ in range(_REFINE_PASSES):
        coded = _worth_coding(symbol_counts(symbols))
        cost = {symbol: bits for symbol, (bits, _) in coded.items()}
        reparsed = _cheapest_parse(pixels, cost)
        if reparsed == symbols:
            break
        symbols = reparsed
        candidate = _stream(tiles, symbols, xor)
        if len(candidate) < len(best):
            best = candidate
    return best


def _seeds(rows: list[int]) -> list[tuple[int, list[int], list[Symbol], bool]]:
    """Every starting point, cheapest-looking first: ``(size, pixels, parse, xor)``.

    One per run cap per mode — sixteen — each coded once and *not* refined. Both
    modes are seeds rather than a separate outer choice because they compete on
    the same terms: XOR mode wins on art with vertical coherence and loses on art
    without it, which is the same kind of "where does this start" question a run
    cap asks, and ranking them together is what lets the budget below be spent on
    whichever sixteen look best rather than eight of each.
    """
    tiles = len(rows) // ROWS_PER_TILE
    out = []
    for xor in (False, True):
        source = rows
        if xor:
            source, previous = [], 0
            for row in rows:
                source.append(row ^ previous)
                previous = row
        pixels = _nibbles(source)
        for cap in SEED_CAPS:
            parse = _greedy_parse(pixels, cap)
            out.append((len(_stream(tiles, parse, xor)), pixels, parse, xor))
    out.sort(key=lambda seed: seed[0])
    return out


def compress(data: bytes) -> bytes:
    """Encode 4bpp Mega Drive tiles as a Nemesis stream.

    Both modes are built and the smaller kept: XOR mode wins on art with vertical
    coherence and loses on art without it, the choice is one header bit, and the
    encoder is cheap enough that guessing would only ever cost bytes.

    """
    tile_bytes = ROWS_PER_TILE * ROW_BYTES
    if not data or len(data) % tile_bytes:
        raise ValueError(
            f"Nemesis compresses whole 8x8 tiles: {len(data):,} bytes is not a "
            f"non-zero multiple of {tile_bytes}"
        )
    tiles = len(data) // tile_bytes
    if tiles > TILE_COUNT_MASK:
        raise ValueError(
            f"input is {tiles:,} tiles; the header's count field holds "
            f"{TILE_COUNT_MASK:,}"
        )
    rows = [
        int.from_bytes(data[at : at + ROW_BYTES], "big")
        for at in range(0, len(data), ROW_BYTES)
    ]
    tiles = len(rows) // ROWS_PER_TILE
    seeds = _seeds(rows)
    return min(
        (
            _refine(tiles, pixels, parse, xor, _stream(tiles, parse, xor))
            for _, pixels, parse, xor in seeds[:REFINE_SEEDS]
        ),
        key=len,
    )


class NemesisCompression(PartialDecompression):
    info = PluginInfo(
        id="compression.nemesis",
        name="Nemesis (Mega Drive tile Huffman)",
        stage=Stage.COMPRESSION,
        # The tile count in the header bounds the stream, which is what lets one
        # ROM's art chain end-to-end with no gaps between the structures.
        self_delimiting=True,
        category="Sega",
    )

    _decode = staticmethod(decompress)
    _encode = staticmethod(compress)
