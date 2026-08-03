"""The SNES command-stream LZ family: LZ1 (Zelda 3) and LZ2 (SMW, YI) codecs.

A compressed stream is a sequence of commands, each a header byte (or two)
followed by its payload, terminated by a ``0xFF`` header. Header layout::

    short form:  CCCLLLLL            command = bits 7..5 (not 111),
                                     length  = bits 4..0 + 1        (1..32)
    long form:   111CCCLL LLLLLLLL   command = bits 4..2,
                                     length  = 10 bits + 1          (1..1024)

Commands (``length`` output bytes each):

    000  literal        copy ``length`` source bytes verbatim
    001  byte fill      repeat the next source byte
    010  word fill      alternate the next two source bytes (a,b,a,b,…)
    011  increasing     next source byte, then +1, +2, … (mod 256)
    1xx  backreference  copy from an **absolute** 16-bit offset into the output
                        produced so far (overlap allowed — a forward-copy RLE).
                        All four high commands decode identically.

The two family members differ only in the backreference offset's byte order:
**LZ1 is little-endian, LZ2 big-endian**. Everything else is shared, so both
plugins parameterise one engine. Encoding details and provenance:
``docs/graphics-formats-reference/implementation-guide.md`` §6.

The compressor parses by shortest path — the cheapest encoding of the whole
remaining structure, not the command that looks best where it stands — over a
3-byte-prefix index for backreference search. It emits only backreference command
``100``: the decoder treats 5/6/7 as aliases, and command 7 in long form collides
with the ``0xFF`` terminator (``111 111 11``), so avoiding them keeps every
emitted header unambiguous. Any stream that round-trips is valid; matching
another compressor's exact output is a non-goal, and no known tool reproduces the
original blobs anyway.
"""

from __future__ import annotations

from celpix.core.context import (
    KEY_COMPRESSED_SIZE,
    KEY_DECOMPRESS_COMPLETE,
    KEY_DECOMPRESS_PARTIAL,
    PipelineContext,
)
from celpix.core.errors import Stage
from celpix.plugins.base import PluginInfo
from celpix.plugins.builtins._lz import MatchFinder

# One HiROM bank — the conventional cap on an uncompressed structure, and the
# reach of the absolute 16-bit backreference offset.
_MAX_OUT = 0x10000

_TERMINATOR = 0xFF
_MAX_SHORT = 32
_MAX_LONG = 1024

_OP_LITERAL = 0x00
_OP_FILL = 0x20
_OP_WORD_FILL = 0x40
_OP_INCREASING = 0x60
_OP_BACKREF = 0x80

# Compressor tuning: a run or backref shorter than 3 never beats literals, and
# the candidate cap bounds pathological inputs only — deepening it to 256 costs
# a third more time and buys 0.3%, to 1024 another 60% for 0.1% more.
_MIN_MATCH = 3
_MAX_CHAIN = 64

# Stands in where a command cannot be written at all; its cost is infinite, so it
# never reaches the emitter.
_NO_CHOICE = (_OP_LITERAL, 0, 0)


def _fail(reason: str) -> ValueError:
    return ValueError(f"corrupt LZ stream: {reason}")


def decompress(
    data: bytes, *, big_endian_offsets: bool, allow_partial: bool = False
) -> tuple[bytes, int]:
    """Decode one compressed structure from the start of ``data``.

    Returns ``(output, consumed)``, ``consumed`` counting the compressed bytes
    through the terminator, so a caller handing in an over-read buffer learns the
    structure's true extent. Trailing bytes after the terminator are never touched.

    With ``allow_partial``, for a *bounded* buffer that may cut the structure
    short, running out of source is not an error: the prefix decoded so far comes
    back, finishing as much of the current command as the buffer allows.
    Structural corruption — a backreference into unwritten output, output past the
    64 KB cap — raises either way, which is what lets a window preview tell "a
    structure continues past the window" from "this is not a structure at all".
    """
    out = bytearray()
    n = len(data)
    i = 0

    def truncated(reason: str) -> tuple[bytes, int]:
        if allow_partial:
            return bytes(out), n
        raise _fail(reason)

    while True:
        if i >= n:
            return truncated("source exhausted before the 0xFF terminator")
        cmd = data[i]
        i += 1
        if cmd == _TERMINATOR:
            return bytes(out), i
        if (cmd & 0xE0) == 0xE0:  # long form
            if i >= n:
                return truncated("source exhausted inside a long-form header")
            length = (((cmd & 0x03) << 8) | data[i]) + 1
            i += 1
            op = (cmd << 3) & 0xE0
        else:
            length = (cmd & 0x1F) + 1
            op = cmd & 0xE0
        if len(out) + length > _MAX_OUT:
            raise _fail(f"output exceeds the {_MAX_OUT:#x}-byte cap")

        if op == _OP_LITERAL:
            if i + length > n:
                out += data[i:n]
                return truncated("source exhausted inside a literal run")
            out += data[i : i + length]
            i += length
        elif op == _OP_FILL:
            if i >= n:
                return truncated("source exhausted reading a fill byte")
            out += data[i : i + 1] * length
            i += 1
        elif op == _OP_WORD_FILL:
            if i + 2 > n:
                return truncated("source exhausted reading a word-fill pair")
            pair = data[i : i + 2]
            i += 2
            out += (pair * ((length + 1) // 2))[:length]
        elif op == _OP_INCREASING:
            if i >= n:
                return truncated("source exhausted reading an increasing-fill byte")
            v = data[i]
            i += 1
            out += bytes((v + k) & 0xFF for k in range(length))
        else:  # backreference (all four high commands)
            if i + 2 > n:
                return truncated("source exhausted reading a backreference offset")
            if big_endian_offsets:
                off = (data[i] << 8) | data[i + 1]
            else:
                off = data[i] | (data[i + 1] << 8)
            i += 2
            if off >= len(out):
                raise _fail(
                    f"backreference into unwritten output ({off:#x} >= {len(out):#x})"
                )
            # Byte-at-a-time so an overlapping copy re-reads bytes this command
            # just produced — the format's run-extension idiom.
            for k in range(length):
                out.append(out[off + k])


def _emit_header(out: bytearray, op: int, length: int) -> None:
    if length <= _MAX_SHORT:
        out.append(op | (length - 1))
    else:
        encoded = length - 1  # 0..1023
        out.append(0xE0 | ((op >> 3) & 0x1C) | (encoded >> 8))
        out.append(encoded & 0xFF)


def _run_lengths(data: bytes) -> tuple[list[int], list[int], list[int]]:
    """How far the fill, increasing and alternating patterns reach from each
    position — the three commands whose reach is a property of the bytes alone,
    so it is worth knowing everywhere at once rather than re-measuring per parse
    step."""
    n = len(data)
    fill = [1] * (n + 1)
    inc = [1] * (n + 1)
    alt = [1] * (n + 1)
    for i in range(n - 2, -1, -1):
        if data[i + 1] == data[i]:
            fill[i] = min(fill[i + 1] + 1, _MAX_LONG)
        if data[i + 1] == (data[i] + 1) & 0xFF:
            inc[i] = min(inc[i + 1] + 1, _MAX_LONG)
        # a,b,a,b,… continues exactly when data[i+2] repeats data[i]; the tail
        # from i+1 is the same shape with the pair swapped, so its length carries.
        alt[i] = (
            min(alt[i + 1] + 1, _MAX_LONG)
            if data[i + 2 : i + 3] == data[i : i + 1]
            else 2
        )
    return fill, inc, alt


def compress(data: bytes, *, big_endian_offsets: bool) -> bytes:
    """Encode ``data`` (≤ 64 KB) as one compressed structure.

    The parse is a shortest path, not a greedy walk: ``cost[i]`` is the fewest
    compressed bytes that can encode ``data[i:]``, solved right-to-left, so every
    command and every length is priced against what the rest of the structure
    then costs. A greedy parse has to guess — the command that covers the most
    bytes here can leave the next position straddling a run it would otherwise
    have coded whole — and on Yoshi's Island's blobs guessing costs ~1.4%.
    """
    n = len(data)
    if n > _MAX_OUT:
        raise ValueError(
            f"data is {n:#x} bytes; LZ structures cap at {_MAX_OUT:#x} (one 64 KB bank)"
        )
    if n == 0:
        return bytes((_TERMINATOR,))

    fill, inc, alt = _run_lengths(data)
    # No distance window: the offset is *absolute* within the structure, so every
    # earlier position is addressable. Bounding the scan to the newest
    # _MAX_CHAIN candidates keeps a worst-case input from going quadratic.
    match_len, match_off = MatchFinder(
        data, min_match=_MIN_MATCH, window=None, max_candidates=_MAX_CHAIN
    ).all_longest(_MAX_LONG)

    inf = float("inf")
    cost: list[float] = [inf] * (n + 1)
    # cost[j] + j, which is what a literal run reaching j is worth: its payload is
    # its length, so folding that in turns "cheapest literal length" into a plain
    # minimum over a window rather than a scan that has to weigh each length.
    reach: list[float] = [inf] * (n + 1)
    choice: list[tuple[int, int, int]] = [(_OP_LITERAL, 1, 0)] * (n + 1)
    cost[n] = 0
    reach[n] = n

    def priced(
        i: int, op: int, reach_len: int, payload: int, off: int
    ) -> tuple[float, tuple[int, int, int]]:
        """Cheapest way to write ``op`` at ``i``, in either header form.

        Only the longest length each form allows is priced. A shorter one can in
        principle win — ``cost`` is non-increasing apart from the odd single byte,
        where dropping a byte off the front forces an extra header — but scanning
        every length for that costs a third more time and recovers 0.03%.
        """
        if reach_len < _MIN_MATCH:
            return inf, _NO_CHOICE
        length = reach_len if reach_len < _MAX_SHORT else _MAX_SHORT
        best = cost[i + length] + 1 + payload
        pick = (op, length, off)
        if reach_len > _MAX_SHORT:
            length = reach_len if reach_len < _MAX_LONG else _MAX_LONG
            value = cost[i + length] + 2 + payload
            if value < best:
                best = value
                pick = (op, length, off)
        return best, pick

    for i in range(n - 1, -1, -1):
        # Literals first: they always apply, so they seed the minimum.
        stop = min(i + _MAX_SHORT, n)
        value = min(reach[i + 1 : stop + 1])
        best = value + 1 - i
        best_choice = (_OP_LITERAL, reach.index(value, i + 1, stop + 1) - i, 0)
        if n - i > _MAX_SHORT:
            stop = min(i + _MAX_LONG, n)
            value = min(reach[i + 33 : stop + 1])
            if value + 2 - i < best:
                best = value + 2 - i
                best_choice = (_OP_LITERAL, reach.index(value, i + 33, stop + 1) - i, 0)

        for value, pick in (
            priced(i, _OP_FILL, fill[i], 1, 0),
            priced(i, _OP_INCREASING, inc[i], 1, 0),
            # Equal bytes are the plain fill's job, never the word fill's.
            priced(
                i,
                _OP_WORD_FILL,
                alt[i] if i + 1 < n and data[i] != data[i + 1] else 0,
                2,
                0,
            ),
            priced(i, _OP_BACKREF, match_len[i], 2, match_off[i]),
        ):
            if value < best:
                best = value
                best_choice = pick

        cost[i] = best
        reach[i] = best + i
        choice[i] = best_choice

    out = bytearray()
    i = 0
    while i < n:
        op, length, off = choice[i]
        _emit_header(out, op, length)
        if op == _OP_LITERAL:
            out += data[i : i + length]
        elif op == _OP_FILL or op == _OP_INCREASING:
            out.append(data[i])
        elif op == _OP_WORD_FILL:
            out += data[i : i + 2]
        else:
            if big_endian_offsets:
                out += bytes(((off >> 8) & 0xFF, off & 0xFF))
            else:
                out += bytes((off & 0xFF, (off >> 8) & 0xFF))
        i += length
    out.append(_TERMINATOR)
    return bytes(out)


class _LzBase:
    """Both directions of one LZ variant; ``_big_endian`` is all that differs."""

    _big_endian: bool

    def decompress(self, data: bytes, ctx: PipelineContext) -> bytes:
        # Strict first: reaching the terminator means the structure's true end is
        # known. Fall back to a best-effort partial decode only when the caller
        # said the buffer may cut the structure short.
        try:
            out, consumed = decompress(data, big_endian_offsets=self._big_endian)
            complete = True
        except ValueError:
            if not ctx.get(KEY_DECOMPRESS_PARTIAL):
                raise
            out, consumed = decompress(
                data, big_endian_offsets=self._big_endian, allow_partial=True
            )
            complete = False
        ctx.set(KEY_COMPRESSED_SIZE, consumed)
        ctx.set(KEY_DECOMPRESS_COMPLETE, complete)
        return out

    def compress(self, data: bytes, ctx: PipelineContext) -> bytes:
        return compress(data, big_endian_offsets=self._big_endian)


class Lz1(_LzBase):
    _big_endian = False
    info = PluginInfo(
        id="compression.lz1", name="LZ1 (Zelda 3)", stage=Stage.COMPRESSION
    )


class Lz2(_LzBase):
    _big_endian = True
    info = PluginInfo(
        id="compression.lz2",
        name="LZ2 (SMW, Yoshi's Island)",
        stage=Stage.COMPRESSION,
    )
