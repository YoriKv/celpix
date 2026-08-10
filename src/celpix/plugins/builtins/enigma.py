"""Enigma — the Mega Drive's plane-map RLE, both directions.

A *tilemap* compression, unlike everything else in this module: it unpacks to
plain 16-bit VDP cells, which ``preset.tilemap.md-bg`` then reads. Sega shipped it
alongside Nemesis in the same toolchain, and it turns up wherever a game stores a
whole static screen — menus, title cards, special-stage layouts, block mappings.

What makes it small is that it knows what a screen built from freshly uploaded
art looks like. Most cells either count up through the tile bank in the order the
screen consumes it, or repeat one background cell, and both get a six-bit token
with no operand at all.

Stream shape::

    byte     index width, in bits (1..11)
    byte     000PCCVH - which of a cell's top five bits this stream stores
             inline, P priority, CC palette line, V/H flip
    u16 be   incrementing word } both biased by the base cell the caller passes
    u16 be   repeated word     } in, so the stream is position-independent

    then, MSB first, until the terminator:
      0 0 nnnn    the incrementing word, n+1 times, +1 to it after each
      0 1 nnnn    the repeated word, n+1 times
      1 00 nnnn   one stored cell, n+1 times
      1 01 nnnn   one stored cell, n+1 times, +1 after each
      1 10 nnnn   one stored cell, n+1 times, -1 after each
      1 11 1111   end of stream
      1 11 nnnn   n+1 freshly stored cells

      a stored cell = the flag bits the header's mask names, most significant
                      first, then the index; OR-ed together, not added

Four things about that cost real work to get wrong:

- **The token is 6 or 7 bits**, and which it is comes from the first bit alone.
  The two operand-free ops spend a bit less than the four that carry a cell,
  which is most of what the format saves.
- **The incrementing word is state, not an operand.** It starts where the header
  says and only ever moves forward, one per cell its own op emits. Read a stream
  twice from different points and it decodes differently.
- **The flag field spells only the bits the mask names**, most significant first,
  so its width varies per stream and a screen that never flips a tile pays
  nothing anywhere. A decoder that assumes five bits, or reads them low-first,
  produces plausible cells with the wrong colours.
- **Everything is relative to the base cell the caller supplies.** celPix decodes
  with a base of zero, which is what makes the result an ordinary nametable: the
  palette row and the tile bank's start then live on the entry
  (``palette_row_base``, the tile source's base index) where they can be seen and
  changed, instead of being baked into every cell.

Format detail and provenance are in
``docs/graphics-formats-reference/implementation-guide.md`` §7.
"""

from __future__ import annotations

from collections import Counter

from celpix.core.context import (
    KEY_COMPRESSED_SIZE,
    KEY_DECOMPRESS_COMPLETE,
    KEY_DECOMPRESS_PARTIAL,
    PipelineContext,
)
from celpix.core.errors import Stage
from celpix.plugins.base import PluginInfo

HEADER_SIZE = 6
CELL_BYTES = 2

COUNT_BITS = 4
COUNT_MASK = 0x0F
MAX_RUN = COUNT_MASK + 1
END_COUNT = 0x0F  # the literal op with a full count nibble is the terminator

MODE_REPEAT = 0
MODE_UP = 1
MODE_DOWN = 2
MODE_LITERALS = 3
DELTA = (0, 1, -1)

# A VDP cell is PCCVHAAA AAAAAAAA: five flag bits over an 11-bit tile index.
FLAG_SHIFT = 11
FLAG_BITS = 5
INDEX_MASK = (1 << FLAG_SHIFT) - 1
FLAG_MASK = 0xFFFF ^ INDEX_MASK
MAX_INDEX_BITS = FLAG_SHIFT

# How many of the commonest cells to try as the repeated word when packing, and
# the cell count past which the sweep is narrowed. The sweep costs a full pack
# per candidate, which is nothing on a screen and adds up on a whole level's
# block mappings.
FILLER_CANDIDATES = 6
NARROW_FILLERS = 2
NARROW_ABOVE = 4096


def _fail(reason: str) -> ValueError:
    return ValueError(f"corrupt Enigma stream: {reason}")


def _flag_bits(mask: int) -> tuple[int, ...]:
    """Which word bits the inline flag field carries, in the order it stores them.

    Most significant first, which is the one ordering not to guess: a stream whose
    mask names priority and both flips spells them P, V, H, and reading them the
    other way round decodes a screen with its colours and flips shuffled rather
    than with anything that looks like an error.
    """
    return tuple(
        FLAG_SHIFT + bit for bit in range(FLAG_BITS - 1, -1, -1) if mask >> bit & 1
    )


class _BitReader:
    """MSB-first, with zero padding past the end so a bounded slice still ends.

    ``consumed`` reproduces what the original unpacker leaves behind: the byte the
    last real bit came from, rounded up to a word, which is where the next
    structure starts. The stream is read a *word* at a time by the 68000 routine,
    so a stream that ends mid-word still owns the rest of it.
    """

    def __init__(self, data: bytes, pos: int) -> None:
        self._data = data
        self._pos = pos
        self._pad_bits = 0
        self._acc = 0
        self._bits = 0

    @property
    def exhausted(self) -> bool:
        return self._bits - self._pad_bits <= 0 and self._pos >= len(self._data)

    @property
    def consumed(self) -> int:
        real = max(self._bits - self._pad_bits, 0)
        return (self._pos - real // 8 + 1) & ~1

    def take(self, count: int) -> int:
        while self._bits < count:
            if self._pos < len(self._data):
                self._acc = (self._acc << 8) | self._data[self._pos]
                self._pos += 1
            else:
                self._acc <<= 8
                self._pad_bits += 8
            self._bits += 8
        value = (self._acc >> (self._bits - count)) & ((1 << count) - 1)
        self._bits -= count
        self._pad_bits = min(self._pad_bits, self._bits)
        self._acc &= (1 << self._bits) - 1
        return value


def decompress(data: bytes, *, partial: bool = False) -> tuple[bytes, int, bool]:
    """Unpack a token stream into 16-bit big-endian nametable cells.

    Returns ``(cells, consumed, complete)``; ``complete`` is false only when the
    buffer ran out before the terminator, which ``partial`` downgrades from an
    error to a short result.
    """
    if len(data) < HEADER_SIZE:
        raise _fail(f"shorter than the {HEADER_SIZE}-byte header")
    width = data[0]
    mask = data[1]
    if not 1 <= width <= MAX_INDEX_BITS:
        raise _fail(f"index width {width} is outside 1..{MAX_INDEX_BITS}")
    if mask >> FLAG_BITS:
        raise _fail(f"flag mask {mask:#04x} sets bits above the cell's top five")
    cursor = int.from_bytes(data[2:4], "big")
    filler = int.from_bytes(data[4:HEADER_SIZE], "big")

    flag_bits = _flag_bits(mask)
    reader = _BitReader(data, HEADER_SIZE)

    def stored_cell() -> int:
        cell = 0
        for bit in flag_bits:
            if reader.take(1):
                cell |= 1 << bit
        # OR, not add: the index is at most 11 bits and the flags sit above it,
        # so there is nothing to carry and a wide index must not be allowed to
        # look like a flag.
        return cell | reader.take(width)

    out = bytearray()
    complete = False
    while not reader.exhausted:
        if not reader.take(1):
            repeated = reader.take(1)
            count = reader.take(COUNT_BITS) + 1
            if repeated:
                out += filler.to_bytes(CELL_BYTES, "big") * count
            else:
                for _ in range(count):
                    out += (cursor & 0xFFFF).to_bytes(CELL_BYTES, "big")
                    cursor += 1
            continue

        mode = reader.take(2)
        count = reader.take(COUNT_BITS)
        if mode == MODE_LITERALS:
            if count == END_COUNT:
                complete = True
                break
            for _ in range(count + 1):
                out += stored_cell().to_bytes(CELL_BYTES, "big")
            continue

        cell = stored_cell()
        step = DELTA[mode]
        for _ in range(count + 1):
            out += (cell & 0xFFFF).to_bytes(CELL_BYTES, "big")
            cell += step

    if not complete and not partial:
        raise _fail(f"source ended after {len(out) // CELL_BYTES:,} cells")
    return bytes(out), reader.consumed, complete


# -- compression ------------------------------------------------------------


class _BitWriter:
    def __init__(self) -> None:
        self.out = bytearray()
        self._acc = 0
        self._bits = 0

    def push(self, value: int, width: int) -> None:
        self._acc = (self._acc << width) | value
        self._bits += width
        while self._bits >= 8:
            self._bits -= 8
            self.out.append((self._acc >> self._bits) & 0xFF)
        self._acc &= (1 << self._bits) - 1

    def finish(self) -> bytes:
        if self._bits:
            self.push(0, 8 - self._bits)
        if len(self.out) & 1:  # the reader rounds its extent up to a word
            self.out.append(0)
        return bytes(self.out)


def _run_length(cells: list[int], at: int, step: int) -> int:
    """How far ``cells`` keeps stepping by ``step`` from ``at``, capped.

    Full-word arithmetic, not index-only: the decoder adds the step to the whole
    cell, so a run that walks off the top of the index field carries into the
    flip bit, and an encoder measuring only the index would write a run the
    decoder then reads differently.
    """
    length = 1
    while (
        length < MAX_RUN
        and at + length < len(cells)
        and cells[at + length] == (cells[at] + step * length) & 0xFFFF
    ):
        length += 1
    return length


def compress(data: bytes) -> bytes:
    """Pack 16-bit big-endian nametable cells into a token stream.

    The token walk itself is greedy, which costs little: every token is six or
    seven bits plus at most one stored cell, so taking the longest run available
    is almost always right. What the walk cannot decide for itself is the two
    header choices it runs *under* — which cell is the repeated word, and where
    the incrementing word starts — because both only pay off across the whole
    screen. So a handful of candidates are packed and the smallest kept, which is
    cheap and lands under the original encoder on real screens.
    """
    if len(data) % CELL_BYTES:
        raise ValueError(
            f"a nametable is whole {CELL_BYTES}-byte cells: {len(data):,} bytes is not"
        )
    cells = [
        int.from_bytes(data[at : at + CELL_BYTES], "big")
        for at in range(0, len(data), CELL_BYTES)
    ]
    if not cells:
        raise ValueError("nothing to pack")

    # The mask is the OR of every cell's flags: a bit no cell sets is one the
    # stream never has to spell, anywhere.
    mask = 0
    for cell in cells:
        mask |= cell
    flag_bits = _flag_bits(mask >> FLAG_SHIFT)
    width = max(1, (mask & INDEX_MASK).bit_length())

    fillers = [
        cell
        for cell, _ in Counter(cells).most_common(
            NARROW_FILLERS if len(cells) > NARROW_ABOVE else FILLER_CANDIDATES
        )
    ]
    # The incrementing word exists to walk a freshly uploaded tile bank in order,
    # so it starts at the lowest plain (unflagged) cell the screen uses - or at
    # the first cell, for a screen laid out from wherever it happens to begin.
    #
    # **Whole cells, flags and all.** The incrementing word is compared against
    # the cell as a unit, so a screen drawn entirely from flipped tiles - a
    # mirrored half, a whole layer at high priority - has an ascending run the
    # operand-free token can walk only if a candidate carries those flags too.
    # Offering just the masked value costs such a screen every one of these
    # tokens, and it falls back to a stored cell per run.
    plain = [cell for cell in cells if not cell & FLAG_MASK]
    starts = {
        min(plain) if plain else 0,
        min(cells),
        cells[0],
        cells[0] & INDEX_MASK,
        0,
    }
    return min(
        (
            _pack(cells, width, flag_bits, start, filler, cursor_first)
            for filler in fillers
            for start in starts
            # Whether a cell that is *both* the next incrementing value and the
            # repeated one should spend the incrementing word is the one choice
            # with a lasting cost: spending it advances past a tile the screen
            # still needs later, and every run after that misses.
            for cursor_first in (True, False)
        ),
        key=len,
    )


def _pack(
    cells: list[int],
    width: int,
    flag_bits: tuple[int, ...],
    cursor_start: int,
    filler: int,
    cursor_first: bool,
) -> bytes:
    writer = _BitWriter()

    def put_short(repeated: bool, count: int) -> None:
        writer.push((int(repeated) << COUNT_BITS) | (count - 1), 1 + 1 + COUNT_BITS)

    def put_long(mode: int, count: int) -> None:
        writer.push(
            (1 << (2 + COUNT_BITS)) | (mode << COUNT_BITS) | (count - 1),
            1 + 2 + COUNT_BITS,
        )

    def put_cell(cell: int) -> None:
        for bit in flag_bits:
            writer.push((cell >> bit) & 1, 1)
        writer.push(cell & INDEX_MASK, width)

    cursor = cursor_start
    at = 0
    pending: list[int] = []

    def flush() -> None:
        while pending:
            # Fifteen, not sixteen: a literal token's count nibble reaches 0xF
            # only as the terminator, so a full batch would end the stream.
            batch, pending[:] = pending[: MAX_RUN - 1], pending[MAX_RUN - 1 :]
            put_long(MODE_LITERALS, len(batch))
            for cell in batch:
                put_cell(cell)

    def reach() -> int:
        """How far a non-incrementing token may run before the next cursor cell.

        Nothing else may swallow the cell the incrementing word is waiting for.
        It is the one operand-free way to name a tile and it only advances when it
        is spent, so a run that steps over its cell costs six bits *here* and
        strands every incrementing token after it — which is most of the saving
        the format exists for.
        """
        for ahead in range(1, MAX_RUN):
            if at + ahead >= len(cells) or cells[at + ahead] == cursor & 0xFFFF:
                return ahead
        return MAX_RUN

    while at < len(cells):
        on_cursor = cells[at] == cursor & 0xFFFF
        on_filler = cells[at] == filler
        if on_cursor and (cursor_first or not on_filler):
            run = 1
            while (
                run < MAX_RUN
                and at + run < len(cells)
                and cells[at + run] == (cursor + run) & 0xFFFF
            ):
                run += 1
            flush()
            put_short(False, run)
            cursor += run
            at += run
            continue
        limit = reach()
        if on_filler:
            run = 1
            while run < limit and cells[at + run] == filler:
                run += 1
            flush()
            put_short(True, run)
            at += run
            continue
        options = (
            (min(_run_length(cells, at, 0), limit), MODE_REPEAT),
            (min(_run_length(cells, at, 1), limit), MODE_UP),
            (min(_run_length(cells, at, -1), limit), MODE_DOWN),
        )
        run, mode = max(options)
        if run == 1:
            # A lone cell is cheaper batched into one literal token than given a
            # token of its own, so it waits to see how many others join it.
            pending.append(cells[at])
            at += 1
            continue
        flush()
        put_long(mode, run)
        put_cell(cells[at])
        at += run

    flush()
    put_long(MODE_LITERALS, END_COUNT + 1)

    flag_mask = 0
    for bit in flag_bits:
        flag_mask |= 1 << (bit - FLAG_SHIFT)
    return (
        bytes([width, flag_mask])
        + (cursor_start & 0xFFFF).to_bytes(2, "big")
        + (filler & 0xFFFF).to_bytes(2, "big")
        + writer.finish()
    )


class EnigmaCompression:
    info = PluginInfo(
        id="compression.enigma",
        name="Enigma (Mega Drive plane map RLE)",
        stage=Stage.COMPRESSION,
        # The terminator token bounds the stream, which is what lets a ROM chain
        # a screen's map and its art end to end with no gap between them.
        self_delimiting=True,
        category="Sega",
    )

    def decompress(self, data: bytes, ctx: PipelineContext) -> bytes:
        out, consumed, complete = decompress(
            data, partial=bool(ctx.get(KEY_DECOMPRESS_PARTIAL))
        )
        ctx.set(KEY_COMPRESSED_SIZE, consumed)
        ctx.set(KEY_DECOMPRESS_COMPLETE, complete)
        return out

    def compress(self, data: bytes, ctx: PipelineContext) -> bytes:
        return compress(data)
