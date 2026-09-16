"""aPLib — Jørgen Ibsen's LZ bit-stream packer, both directions.

The small-decoder LZ that homebrew and late-commercial developers reach for
across the 8- and 16-bit machines: a 68000 unpacker fits in under 170 bytes,
and it is one of the two schemes SGDK's resource compiler packs Mega Drive
assets with (the other is :mod:`~celpix.plugins.builtins.lz4w`). This is the
**raw** stream, with no size header in front of it — what a ROM carries and what
those unpackers read.

The stream opens with the first byte verbatim and is then a sequence of ops
selected by tag bits. The bits live in **tag bytes interleaved into the byte
stream**, eight per byte, consumed **MSB first**, each byte fetched only at the
moment its first bit is needed — so a tag byte sits *after* the operand bytes
of the ops before it and *before* those of the ops it describes. Ops, in the
order their bits are read::

    0             literal: one byte follows
    10  g g       block:   g1 = gamma; if g1 == 2 and the last op was not a
                             block: distance = the previous block's, length
                             = gamma (the *repeat* form)
                           else: distance = ((g1 - 3 or 2) << 8) | byte,
                             length = gamma, plus 2 if distance < 128,
                             1 if >= 1280, another 1 if >= 32000
    110 byte      short:   distance = byte >> 1, length = 2 + (byte & 1)
                           distance 0 is the **end of stream**
    111 dddd      tiny:    one byte from `dddd` back; dddd = 0 writes a 0x00

    gamma         Elias-gamma with the leading 1 implied, read two bits at a
                  time - a value bit, then a continue bit - so 2 is "00",
                  3 "10", 4 "0100", 5 "0110" ...; values are 2 and up

Three things about that are easy to get wrong, and all three are silent:

- **The block form's distance bias depends on the previous op.** After a
  literal, a tiny or a short (the "LWM" of the reference decoder) the high
  byte is ``gamma - 3``, because ``gamma == 2`` then means the repeat form;
  after a block or a short it is ``gamma - 2``. Fix the bias at either value
  and the first block after a literal decodes to a wrong distance rather than
  failing.
- **The length adjustments are cumulative.** A distance of 32000 or more
  earns both the ``>= 1280`` and the ``>= 32000`` increments; ``< 128`` earns
  two of its own. The reference 68000 unpacker chains its compares to the same
  effect.
- **The repeat form is legal only right after a non-block.** ``gamma == 2``
  after a block is an ordinary block whose distance high byte is zero.

**Copies are byte at a time, forward.** A match may be longer than its
distance, re-reading bytes it has just written, which is what gives the format
run-length encoding for free (the tiny form at distance 1 and the block form at
distance 1 are the RLE ops).

The encoder is a **shortest-path parse over both decoder states**: ``cost[i]``
is priced twice, once for "the op before ``i`` was a block" and once for "it
was not", because the block form's own cost differs between the two and the
tiny/short/literal forms all reset the state. The repeat form is not planned —
its cost depends on which distance the *path* last used, which is not a
property of a position — but is taken whenever the planned block happens to
reuse the previous one's distance, since the state after it is the same.
Round-tripping is the contract, not byte-identity with any particular packer.

Format provenance and the cross-check against SGDK's own packer are in
``docs/graphics-formats-reference/implementation-guide.md`` §7.
"""

from __future__ import annotations

from celpix.core.errors import Stage
from celpix.plugins.base import PartialDecompression, PluginInfo
from celpix.plugins.builtins._lz import MatchFinder, copy_back

# Where the block form's length adjustments switch, and the tiny/short reaches.
FAR_DISTANCE = 32000
MID_DISTANCE = 1280
NEAR_DISTANCE = 128
TINY_MAX_DISTANCE = 15
SHORT_MAX_DISTANCE = 127
SHORT_MIN, SHORT_MAX = 2, 3

# The longest match the encoder writes. The gamma code is unbounded, so this is
# a search bound rather than a format limit; a run past it costs one more block.
MAX_MATCH = 0x1_0000
# What a decode refuses to grow past: the gamma code that names a length is
# unbounded too, so a corrupt stream can ask for terabytes. 16 MiB is far past
# any cartridge asset.
OUTPUT_CAP = 16 * 1024 * 1024

# Op costs in bits, tag bits included, for the parse.
LITERAL_COST = 1 + 8
TINY_COST = 3 + 4
SHORT_COST = 3 + 8
BLOCK_HEAD_COST = 2 + 8  # the two tag bits and the distance's low byte

OP_LITERAL, OP_TINY, OP_SHORT, OP_BLOCK = range(4)


def _fail(reason: str) -> ValueError:
    return ValueError(f"corrupt aPLib stream: {reason}")


class _Truncated(Exception):
    """The stream ran out mid-op — recoverable only under ``partial``."""


def _length_delta(distance: int) -> int:
    """What the block form adds to its gamma-coded length at ``distance``."""
    if distance < NEAR_DISTANCE:
        return 2
    if distance >= FAR_DISTANCE:
        return 2
    if distance >= MID_DISTANCE:
        return 1
    return 0


def _gamma_bits(value: int) -> int:
    """How many tag bits the gamma code of ``value`` (>= 2) takes."""
    return 2 * (value.bit_length() - 1)


class _Reader:
    """Tag bits and payload bytes over one buffer, sharing a position.

    They have to share it: the tag byte is fetched lazily, at the first bit
    read after the previous one is spent, so where it lands relative to the
    operand bytes around it is decided by the order of reads.
    """

    def __init__(self, data: bytes) -> None:
        self._data = data
        self.pos = 0
        self._tag = 0
        self._left = 0

    def byte(self) -> int:
        if self.pos >= len(self._data):
            raise _Truncated
        value = self._data[self.pos]
        self.pos += 1
        return value

    def bit(self) -> int:
        if not self._left:
            self._tag = self.byte()
            self._left = 8
        self._left -= 1
        return (self._tag >> self._left) & 1

    def gamma(self) -> int:
        value = 1
        while True:
            value = (value << 1) | self.bit()
            if not self.bit():
                return value
            if value > OUTPUT_CAP:
                raise _fail("gamma code runs past any plausible value")


def decompress(data: bytes, *, partial: bool = False) -> tuple[bytes, int, bool]:
    """Unpack one stream from ``data[0]``.

    Returns ``(plain, consumed, complete)``; ``complete`` is false only when the
    buffer ran out before the end marker, which ``partial`` downgrades from an
    error to a short result.
    """
    if not data:
        raise _fail("empty source")
    reader = _Reader(data)
    out = bytearray((reader.byte(),))
    after_block = False  # the reference decoder's LWM, inverted
    last_distance = 0
    complete = False

    def copy(distance: int, length: int) -> None:
        if distance < 1 or distance > len(out):
            raise _fail(
                f"match reaches {distance:,} bytes back "
                f"into {len(out):,} bytes of output"
            )
        if len(out) + length > OUTPUT_CAP:
            raise _fail(f"output would exceed {OUTPUT_CAP:,} bytes")
        copy_back(out, distance, length)

    try:
        while True:
            if not reader.bit():
                out.append(reader.byte())
                after_block = False
            elif not reader.bit():
                value = reader.gamma()
                if not after_block and value == 2:
                    distance = last_distance
                    length = reader.gamma()
                else:
                    high = value - (2 if after_block else 3)
                    distance = (high << 8) | reader.byte()
                    length = reader.gamma() + _length_delta(distance)
                    last_distance = distance
                copy(distance, length)
                after_block = True
            elif not reader.bit():
                value = reader.byte()
                distance = value >> 1
                if distance == 0:
                    complete = True
                    break
                copy(distance, SHORT_MIN + (value & 1))
                last_distance = distance
                after_block = True
            else:
                distance = 0
                for _ in range(4):
                    distance = (distance << 1) | reader.bit()
                if distance:
                    copy(distance, 1)
                else:
                    out.append(0)
                after_block = False
    except _Truncated:
        if not partial:
            raise _fail(f"source ended after {len(out):,} bytes") from None

    return bytes(out), reader.pos, complete


# -- compression ------------------------------------------------------------


class _Writer:
    """Tag bytes allocated lazily, exactly as the reader fetches them.

    A tag byte is reserved the moment the first bit of a new group is written,
    and operand bytes written while the group is open land *after* it — which
    is the order the reader meets them, since it fetches a tag only when it
    needs a bit from it.
    """

    def __init__(self) -> None:
        self._out = bytearray()
        self._tag_at = -1
        self._left = 0

    def bit(self, value: int) -> None:
        if not self._left:
            self._tag_at = len(self._out)
            self._out.append(0)
            self._left = 8
        self._left -= 1
        if value:
            self._out[self._tag_at] |= 1 << self._left

    def bits(self, value: int, count: int) -> None:
        for shift in range(count - 1, -1, -1):
            self.bit((value >> shift) & 1)

    def byte(self, value: int) -> None:
        self._out.append(value & 0xFF)

    def gamma(self, value: int) -> None:
        # The leading 1 is implied; each remaining bit is followed by whether
        # another follows it.
        top = value.bit_length() - 2
        for shift in range(top, -1, -1):
            self.bit((value >> shift) & 1)
            self.bit(1 if shift else 0)

    def finish(self) -> bytes:
        return bytes(self._out)


def _block_lengths(longest: int, delta: int) -> list[int]:
    """The lengths worth pricing for a block of up to ``longest``.

    The gamma code prices a length by its bit width, so between two widths a
    longer match is free: only the longest of each width can be the best
    choice, plus the shortest few individually, where the tail a match leaves
    is cheap to price and matters most. ``delta`` is the distance's length
    adjustment, which sets the shortest length the form can spell.
    """
    shortest = SHORT_MIN + delta
    if longest < shortest:
        return []
    lengths = list(range(shortest, min(longest, shortest + 3) + 1))
    width = 2
    while True:
        top = (1 << width) - 1 + delta
        if top >= longest:
            break
        if top > lengths[-1]:
            lengths.append(top)
        width += 1
    if lengths[-1] != longest:
        lengths.append(longest)
    return lengths


def compress(data: bytes) -> bytes:
    """Encode ``data`` as one aPLib stream, as small as the forms allow.

    See the module docstring for the parse. An empty payload has no encoding —
    the first byte is always stored verbatim — and is refused.
    """
    n = len(data)
    if n == 0:
        raise ValueError("aPLib has no encoding for an empty payload")

    # Two searches: the nearest match inside the short/tiny reach is routinely a
    # different one from the longest overall, and the cheap forms need it.
    near_len, near_off = MatchFinder(
        data, min_match=SHORT_MIN, window=SHORT_MAX_DISTANCE
    ).all_longest(MAX_MATCH)
    far_len, far_off = MatchFinder(data, min_match=SHORT_MIN).all_longest(MAX_MATCH)

    inf = float("inf")
    # Indexed [after_block][position]: the state the op at `position` starts in.
    cost: list[list[float]] = [[inf] * (n + 1), [inf] * (n + 1)]
    choice: list[list[tuple[int, int, int]]] = [
        [(OP_LITERAL, 1, 0)] * (n + 1),
        [(OP_LITERAL, 1, 0)] * (n + 1),
    ]
    cost[0][n] = cost[1][n] = 0
    cost_after_block = cost[1]  # every match form leaves the decoder in that state

    for i in range(n - 1, 0, -1):
        byte = data[i]
        # The forms that reset the state cost the same from either state, so
        # price them once.
        if byte == 0:
            # The zero-fill spelling of the tiny form: cheaper than a literal.
            reset_best, reset_pick = cost[0][i + 1] + TINY_COST, (OP_TINY, 1, 0)
        else:
            reset_best, reset_pick = cost[0][i + 1] + LITERAL_COST, (OP_LITERAL, 1, 0)
            for distance in range(1, min(TINY_MAX_DISTANCE, i) + 1):
                if data[i - distance] == byte:
                    reset_best = cost[0][i + 1] + TINY_COST
                    reset_pick = (OP_TINY, 1, distance)
                    break
        candidates: list[tuple[int, int]] = []
        if near_len[i]:
            candidates.append((near_len[i], i - near_off[i]))
        if far_len[i] and (not near_len[i] or far_off[i] != near_off[i]):
            candidates.append((far_len[i], i - far_off[i]))
        for after_block in (0, 1):
            best, pick = reset_best, reset_pick
            for longest, distance in candidates:
                if distance <= SHORT_MAX_DISTANCE:
                    for length in range(SHORT_MIN, min(longest, SHORT_MAX) + 1):
                        value = cost_after_block[i + length] + SHORT_COST
                        if value < best:
                            best, pick = value, (OP_SHORT, length, distance)
                delta = _length_delta(distance)
                # The head is the two tag bits, the gamma-coded high byte with
                # the state's bias, and the low byte; only the length varies.
                head = BLOCK_HEAD_COST + _gamma_bits(
                    (distance >> 8) + (2 if after_block else 3)
                )
                for length in _block_lengths(longest, delta):
                    value = (
                        cost_after_block[i + length]
                        + head
                        + 2 * ((length - delta).bit_length() - 1)
                    )
                    if value < best:
                        best, pick = value, (OP_BLOCK, length, distance)
            cost[after_block][i], choice[after_block][i] = best, pick

    writer = _Writer()
    writer.byte(data[0])
    at = 1
    after_block = 0
    last_distance = 0
    while at < n:
        op, length, distance = choice[after_block][at]
        if op == OP_LITERAL:
            writer.bit(0)
            writer.byte(data[at])
            after_block = 0
        elif op == OP_TINY:
            writer.bits(0b111, 3)
            writer.bits(distance, 4)
            after_block = 0
        elif op == OP_SHORT:
            writer.bits(0b110, 3)
            writer.byte((distance << 1) | (length - SHORT_MIN))
            last_distance = distance
            after_block = 1
        else:
            writer.bits(0b10, 2)
            if not after_block and distance == last_distance:
                writer.gamma(2)
                writer.gamma(length)
            else:
                writer.gamma((distance >> 8) + (2 if after_block else 3))
                writer.byte(distance & 0xFF)
                writer.gamma(length - _length_delta(distance))
            last_distance = distance
            after_block = 1
        at += length

    writer.bits(0b110, 3)
    writer.byte(0)
    return writer.finish()


class AplibCompression(PartialDecompression):
    info = PluginInfo(
        id="compression.aplib",
        name="aPLib (LZ + gamma-coded bit stream)",
        stage=Stage.COMPRESSION,
        # The end marker bounds the stream, so a ROM can chain these back to back.
        self_delimiting=True,
        category="Generic",
    )

    _decode = staticmethod(decompress)
    _encode = staticmethod(compress)
