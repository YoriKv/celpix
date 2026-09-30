"""ZX0 — Einar Saukas's optimal-LZ bit stream (the v2 format), both directions.

The small-decoder LZ of choice across the Z80 machines — Spectrum, MSX, Master
System, Game Gear — and on the Game Boy through GBDK-2020, whose runtime unpacks
it on both CPUs. The decoder is under 70 bytes of Z80. This is the **v2** stream
(the one current compressors write by default); v1 differs only in the offset's
high part not being inverted, and is not read.

Bits are consumed **MSB first** from bytes fetched at the first bit needed from
each, so a bit byte sits after the operand bytes of the ops before it. The stream
is a chain of three blocks, each opening with one flag bit — except the first,
which is always literals and has none::

    after a match:     0  literals      gamma(count), then count bytes
                       1  new offset
    after literals:    0  rep match     gamma(length) bytes from the last offset
                       1  new offset

    new offset         gamma_inv(high + 1)     high + 1 == 256: end of stream
                       byte  ppppppp l         offset = high * 128
                                                        + (127 - ppppppp) + 1
                       length - 1 as gamma, its first continue bit being ``l``

    gamma(v)           v >= 1, leading 1 implied; each further bit is preceded
                       by a 0, and a 1 ends it: 1 = "1", 2 = "00 1", 3 = "01 1",
                       4 = "00 00 1" ...
    gamma_inv          the same with its value bits inverted (the v2 change)

The last offset starts at 1, so a rep match straight after the first literals
repeats the byte before it. Offsets reach 32640 (``high`` stops at 254, the next
value being the end marker), lengths and counts 65535.

Three things about that are easy to get wrong, and all three are silent:

- **Rep matches exist only right after literals.** After a match the 0 flag
  means literals, so two matches in a row are both new offsets.
- **The length's first control bit lives in the offset byte.** Its bit 0 is a 1
  when the length is exactly 2 (``length - 1`` has no further bits) and a 0
  when the gamma code carries on in the bit stream.
- **The offset's low part is stored inverted** (``127 - ``), independently of
  the high part's inversion.

**Copies are byte at a time, forward**, so a match may overlap its own output.

The encoder is a **forward shortest-path parse over both decoder states**:
every position keeps its cheapest cost for "the last block was literals" and for
"the last block was a match", each with the last offset its path left behind,
which is what prices the rep match. A literal block's cost depends on its
length only through the gamma code's width, so for each width the cheapest
start is a sliding-window minimum, kept per width in a monotonic queue. Keeping
one path per state is what makes the rep offset a heuristic rather than exact —
the reference compressor searches every offset — so this can be a few percent
larger than it; round-tripping is the contract, not byte-identity.
"""

from __future__ import annotations

from collections import deque

from celpix.core.errors import Stage
from celpix.plugins.base import PartialDecompression, PluginInfo
from celpix.plugins.builtins._lz import BitGroup, MatchFinder, copy_back

MAX_OFFSET = 0x7F80
# Where the offset's high part is 0 and so costs a single bit.
NEAR_OFFSET = 128
MAX_VALUE = 0xFFFF
END_HIGH = 256
# What a decode refuses to grow past: gamma counts are unbounded in the stream,
# so a corrupt one can ask for anything. Far past any cartridge asset.
OUTPUT_CAP = 16 * 1024 * 1024
# A literal block's count and every length the encoder writes are gamma values,
# so at most MAX_VALUE, which is 16 gamma widths. The stream could spell more,
# but the decoders it ships with (Z80, SM83) build a gamma value in a 16-bit
# register pair and wrap past it — and their whole address space is 64 KiB — so
# a longer count is a stream the target unpacks wrong. The encoder refuses the
# payload instead.
_WIDTHS = MAX_VALUE.bit_length()

_INF = float("inf")
_LITERALS, _REP, _NEW = range(3)


def _fail(reason: str) -> ValueError:
    return ValueError(f"corrupt ZX0 stream: {reason}")


class _Truncated(Exception):
    """The stream ran out mid-op — recoverable only under ``partial``."""


class _Reader:
    """Bits and bytes over one buffer, sharing a position (see the docstring)."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self.pos = 0
        self._bits = 0
        self._left = 0

    def byte(self) -> int:
        if self.pos >= len(self._data):
            raise _Truncated
        value = self._data[self.pos]
        self.pos += 1
        return value

    def bit(self) -> int:
        if not self._left:
            self._bits = self.byte()
            self._left = 8
        self._left -= 1
        return (self._bits >> self._left) & 1

    def gamma(self, value: int = 1, invert: int = 0) -> int:
        while not self.bit():
            value = (value << 1) | (self.bit() ^ invert)
            if value > OUTPUT_CAP:
                raise _fail("gamma code runs past any plausible value")
        return value


def decompress(data: bytes, *, partial: bool = False) -> tuple[bytes, int, bool]:
    """Unpack one stream from ``data[0]``; returns ``(plain, consumed, complete)``.

    ``complete`` is false only when the buffer ran out before the end marker,
    which ``partial`` downgrades from an error to a short result.
    """
    reader = _Reader(data)
    out = bytearray()
    offset = 1
    complete = False
    new_offset = 0  # the first block is literals, with no flag bit
    try:
        while True:
            if not new_offset:
                count = reader.gamma()
                if len(out) + count > OUTPUT_CAP:
                    raise _fail(f"output would exceed {OUTPUT_CAP:,} bytes")
                start = reader.pos
                if start + count > len(data):
                    raise _Truncated
                out += data[start : start + count]
                reader.pos += count
                new_offset = reader.bit()
                if not new_offset:
                    length = reader.gamma()
                    _copy(out, offset, length)
                    new_offset = reader.bit()
                    continue
            high = reader.gamma(invert=1)
            if high == END_HIGH:
                complete = True
                break
            low = reader.byte()
            offset = ((high - 1) << 7) + (127 - (low >> 1)) + 1
            length = 1 if low & 1 else reader.gamma((1 << 1) | reader.bit())
            _copy(out, offset, length + 1)
            new_offset = reader.bit()
    except _Truncated:
        if not partial:
            raise _fail(f"source ended after {len(out):,} bytes") from None
    return bytes(out), reader.pos, complete


def _copy(out: bytearray, offset: int, length: int) -> None:
    if offset > len(out):
        raise _fail(f"match reaches {offset:,} bytes back into {len(out):,} bytes")
    if len(out) + length > OUTPUT_CAP:
        raise _fail(f"output would exceed {OUTPUT_CAP:,} bytes")
    copy_back(out, offset, length)


# -- compression ------------------------------------------------------------


def _gamma_cost(value: int) -> int:
    return 2 * value.bit_length() - 1


def _new_cost(offset: int, length: int) -> int:
    """A new-offset match's bits: flag, the high part, the byte, the length's
    remaining gamma bits (its first continue bit rides in the byte)."""
    return (
        1
        + _gamma_cost(((offset - 1) >> 7) + 1)
        + 8
        + 2 * ((length - 1).bit_length() - 1)
    )


def _lengths(longest: int, shortest: int, bias: int) -> list[int]:
    """The lengths worth pricing for a match of up to ``longest``.

    The gamma code prices ``length - bias`` by its bit width, so within one width
    a longer match costs nothing more: only the longest of each width can be the
    best, plus the shortest few individually, where the remainder is cheap to
    price and matters most.
    """
    if longest < shortest:
        return []
    lengths = list(range(shortest, min(longest, shortest + 3) + 1))
    width = 1
    while True:
        top = (1 << width) - 1 + bias
        if top >= longest:
            break
        if top > lengths[-1]:
            lengths.append(top)
        width += 1
    if lengths[-1] != longest:
        lengths.append(longest)
    return lengths


def _put_gamma(bits: BitGroup, value: int, invert: int = 0) -> None:
    for shift in range(value.bit_length() - 2, -1, -1):
        bits.bit(0)
        bits.bit(((value >> shift) & 1) ^ invert)
    bits.bit(1)


def compress(data: bytes) -> bytes:
    """Encode ``data`` as one ZX0 v2 stream (see the module docstring's parse).

    An empty payload has no encoding — the first block is literals, of at least
    one byte — and is refused, as is one no path can spell: a stretch of more
    than 65535 bytes with no repeat in reach, which a literal block cannot hold.
    """
    n = len(data)
    if n == 0:
        raise ValueError("ZX0 has no encoding for an empty payload")

    near_len, near_at = MatchFinder(data, min_match=2, window=NEAR_OFFSET).all_longest(
        MAX_VALUE
    )
    far_len, far_at = MatchFinder(data, min_match=2, window=MAX_OFFSET).all_longest(
        MAX_VALUE
    )

    # Per state, per position: the cheapest cost in bits, the last offset its
    # path leaves, and how it got there - (op, from, length, offset).
    lit_cost = [_INF] * (n + 1)
    lit_offset = [1] * (n + 1)
    lit_from = [0] * (n + 1)
    match_cost = [_INF] * (n + 1)
    match_offset = [1] * (n + 1)
    match_via: list[tuple[int, int, int, int]] = [(_NEW, 0, 0, 0)] * (n + 1)
    # The start behaves as a match state whose literal flag costs nothing.
    match_cost[0] = -1

    # windows[w] holds, for literal blocks whose count has gamma width w + 1,
    # the candidate starts in cost order: count in [2**w, 2**(w+1) - 1].
    windows: list[deque[int]] = [deque() for _ in range(_WIDTHS)]
    # The last match measured at each rep offset, as (where, length) - measured
    # to the first mismatch, so every position inside it knows its own length
    # without measuring again. That is what keeps a long fill linear.
    rep_runs: dict[int, tuple[int, int]] = {}

    def rep_length(at: int, offset: int, limit: int) -> int:
        seen = rep_runs.get(offset)
        if seen is None or not seen[0] <= at < seen[0] + seen[1]:
            if offset > at:
                return 0
            length = 0
            while at + length < n and data[at + length] == data[at + length - offset]:
                length += 1
            seen = rep_runs[offset] = (at, length)
        return min(seen[1] - (at - seen[0]), limit)

    def relax(at: int, cost: float, via: tuple[int, int, int, int], offset: int):
        if cost < match_cost[at]:
            match_cost[at] = cost
            match_via[at] = via
            match_offset[at] = offset

    for j in range(n + 1):
        # A literal block ending at j: count k from a match state at j - k.
        for w, window in enumerate(windows):
            enter = j - (1 << w)
            if enter < 0:
                break
            key = match_cost[enter] - 8 * enter
            if key != _INF:
                while window and match_cost[window[-1]] - 8 * window[-1] >= key:
                    window.pop()
                window.append(enter)
            oldest = j - (1 << (w + 1)) + 1
            while window and window[0] < oldest:
                window.popleft()
            if window:
                start = window[0]
                cost = match_cost[start] + 8 * (j - start) + 1 + 2 * w + 1
                if cost < lit_cost[j]:
                    lit_cost[j] = cost
                    lit_from[j] = start
                    lit_offset[j] = match_offset[start]
        if j == n:
            break

        room = min(n - j, MAX_VALUE)
        if lit_cost[j] != _INF:
            offset = lit_offset[j]
            longest = rep_length(j, offset, room)
            for length in _lengths(longest, 1, 0):
                relax(
                    j + length,
                    lit_cost[j] + 1 + _gamma_cost(length),
                    (_REP, j, length, offset),
                    offset,
                )
        # A new offset costs the same from either state; start from the cheaper.
        base = min(lit_cost[j], match_cost[j] if j else _INF)
        if base == _INF:
            continue
        for longest, at in ((near_len[j], near_at[j]), (far_len[j], far_at[j])):
            if not longest:
                continue
            offset = j - at
            for length in _lengths(min(longest, room), 2, 1):
                relax(
                    j + length,
                    base + _new_cost(offset, length),
                    (_NEW, j, length, offset),
                    offset,
                )

    ops: list[tuple[int, int, int]] = []
    at = n
    in_literals = lit_cost[n] < match_cost[n]
    if min(lit_cost[n], match_cost[n]) == _INF:
        raise ValueError(
            "ZX0 cannot encode this payload: a stretch with no repeat in reach "
            "is longer than one literal block holds"
        )
    while at:
        if in_literals:
            start = lit_from[at]
            ops.append((_LITERALS, at - start, 0))
            at = start
            in_literals = False
        else:
            op, start, length, offset = match_via[at]
            ops.append((op, length, offset))
            at = start
            # A match from a literal state was preceded by that block; a new
            # offset from a match state was not.
            in_literals = op == _REP or (
                bool(start) and lit_cost[start] <= match_cost[start]
            )
    ops.reverse()

    out = bytearray()
    bits = BitGroup(out, msb_first=True)
    at = 0
    first = True
    for op, length, offset in ops:
        if not first:
            bits.bit(0 if op in (_LITERALS, _REP) else 1)
        first = False
        if op == _LITERALS:
            _put_gamma(bits, length)
            out += data[at : at + length]
        elif op == _REP:
            _put_gamma(bits, length)
        else:
            _put_gamma(bits, ((offset - 1) >> 7) + 1, invert=1)
            low = (127 - ((offset - 1) & 127)) << 1
            if length == 2:
                out.append(low | 1)
            else:
                out.append(low)
                value = length - 1
                top = value.bit_length() - 2
                bits.bit((value >> top) & 1)
                for shift in range(top - 1, -1, -1):
                    bits.bit(0)
                    bits.bit((value >> shift) & 1)
                bits.bit(1)
        at += length
    bits.bit(1)
    _put_gamma(bits, END_HIGH, invert=1)
    bits.finish()
    return bytes(out)


class Zx0Compression(PartialDecompression):
    info = PluginInfo(
        id="compression.zx0",
        name="ZX0",
        stage=Stage.COMPRESSION,
        self_delimiting=True,
        category="Generic",
    )

    _decode = staticmethod(decompress)
    _encode = staticmethod(compress)
