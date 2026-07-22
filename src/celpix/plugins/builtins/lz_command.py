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

The one difference between the two family members is the backreference offset's
byte order: **LZ1 is little-endian, LZ2 big-endian**. Everything else is shared,
so both plugins parameterize the same engine. Encoding details and provenance:
``docs/graphics-formats-reference/implementation-guide.md`` §6.

The compressor is a greedy parse by benefit (bytes saved vs. literals) with a
one-step lazy deferral, using a 3-byte-prefix index for backreference search.
It only ever emits backreference command ``100`` — the decoder treats 5/6/7 as
aliases, and command 7 in long form can collide with the ``0xFF`` terminator
(``111 111 11`` = 0xFF), so avoiding them keeps every emitted header
unambiguous. Any stream that round-trips is valid; matching another
compressor's exact output is a non-goal.
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

# Compressor tuning: a run/backref shorter than 3 never beats literals; the
# candidate cap only bounds pathological inputs (real tile data has few
# same-prefix positions per chain).
_MIN_MATCH = 3
_MAX_CHAIN = 64


def _fail(reason: str) -> ValueError:
    return ValueError(f"corrupt LZ stream: {reason}")


def decompress(
    data: bytes, *, big_endian_offsets: bool, allow_partial: bool = False
) -> tuple[bytes, int]:
    """Decode one compressed structure from the start of ``data``.

    Returns ``(output, consumed)`` — ``consumed`` counts the compressed bytes
    through the terminator, so callers handing in an over-read buffer (offset to
    end-of-file) learn the structure's true extent. Trailing bytes after the
    terminator are never touched.

    With ``allow_partial`` (a *bounded* buffer that may cut the structure
    short), running out of source is not an error: the prefix decoded so far is
    returned, finishing as much of the current command as the buffer allows.
    Structural corruption — a backreference into unwritten output, output past
    the 64 KB cap — still raises either way; that distinction is what lets a
    window preview tell "a structure continues past the window" from "this is
    not a structure at all".
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
            # Byte-at-a-time so an overlapping copy re-reads bytes this same
            # command just produced (the format's run-extension idiom).
            for k in range(length):
                out.append(out[off + k])


def _header_cost(length: int) -> int:
    return 1 if length <= _MAX_SHORT else 2


def _emit_header(out: bytearray, op: int, length: int) -> None:
    if length <= _MAX_SHORT:
        out.append(op | (length - 1))
    else:
        encoded = length - 1  # 0..1023
        out.append(0xE0 | ((op >> 3) & 0x1C) | (encoded >> 8))
        out.append(encoded & 0xFF)


def compress(data: bytes, *, big_endian_offsets: bool) -> bytes:
    """Encode ``data`` (≤ 64 KB) as one compressed structure."""
    n = len(data)
    if n > _MAX_OUT:
        raise ValueError(
            f"data is {n:#x} bytes; LZ structures cap at {_MAX_OUT:#x} (one 64 KB bank)"
        )
    out = bytearray()

    # 3-byte-prefix index for backreference search: prefix -> positions, most
    # recent last. Bounding the scan to the newest _MAX_CHAIN keeps worst-case
    # inputs (a single repeated byte) from going quadratic.
    prefixes: dict[bytes, list[int]] = {}

    def insert(pos: int) -> None:
        if pos + 3 <= n:
            prefixes.setdefault(data[pos : pos + 3], []).append(pos)

    def find_backref(d: int) -> tuple[int, int] | None:
        if d < 1 or d + 3 > n:
            return None
        positions = prefixes.get(data[d : d + 3])
        if not positions:
            return None
        best_len = 0
        best_off = 0
        max_len = min(n - d, _MAX_LONG)
        for p in reversed(positions[-_MAX_CHAIN:]):
            period = d - p  # < period ⇒ overlap: the copy repeats with this period
            length = 0
            while length < max_len and data[d + length] == data[p + length % period]:
                length += 1
            if length > best_len:
                best_len, best_off = length, p
                if length >= max_len:
                    break
        if best_len < _MIN_MATCH:
            return None
        return best_len, best_off

    def best_command_at(d: int) -> tuple[int, int, int, int] | None:
        """Best non-literal command at ``d``: ``(op, length, cost, off)``.

        Maximum benefit (covered − cost), ties to the cheaper command; ``None``
        when literals are no worse.
        """
        best: tuple[int, int, int, int] | None = None
        best_benefit = 0

        def consider(op: int, length: int, cost: int, off: int = 0) -> None:
            nonlocal best, best_benefit
            benefit = length - cost
            if benefit < 1:
                return
            if (
                best is None
                or benefit > best_benefit
                or (benefit == best_benefit and cost < best[2])
            ):
                best = (op, length, cost, off)
                best_benefit = benefit

        first = data[d]
        # byte fill
        length = 1
        while d + length < n and data[d + length] == first and length < _MAX_LONG:
            length += 1
        if length >= _MIN_MATCH:
            consider(_OP_FILL, length, _header_cost(length) + 1)
        # increasing fill
        length = 1
        while (
            d + length < n
            and data[d + length] == (first + length) & 0xFF
            and length < _MAX_LONG
        ):
            length += 1
        if length >= _MIN_MATCH:
            consider(_OP_INCREASING, length, _header_cost(length) + 1)
        # word fill (equal bytes are the plain fill's job)
        if d + 1 < n and first != data[d + 1]:
            pair = (first, data[d + 1])
            length = 1
            while (
                d + length < n
                and data[d + length] == pair[length & 1]
                and length < _MAX_LONG
            ):
                length += 1
            if length >= 4:
                consider(_OP_WORD_FILL, length, _header_cost(length) + 2)
        # backreference
        match = find_backref(d)
        if match is not None and match[1] <= 0xFFFF:
            length, off = match
            consider(_OP_BACKREF, length, _header_cost(length) + 2, off)
        return best

    # Pending literal run [lit_start, d); flushed before any command and at EOF.
    lit_start = -1

    def flush_literals(end: int) -> None:
        nonlocal lit_start
        if lit_start < 0:
            return
        i = lit_start
        while i < end:
            chunk = min(end - i, _MAX_LONG)
            _emit_header(out, _OP_LITERAL, chunk)
            out.extend(data[i : i + chunk])
            i += chunk
        lit_start = -1

    d = 0
    while d < n:
        cmd = best_command_at(d)
        if cmd is not None:
            # Lazy step: when deferring one byte exposes a strictly longer
            # command, take the literal now and the better command next.
            nxt = best_command_at(d + 1) if d + 1 < n else None
            if nxt is not None and nxt[1] > cmd[1]:
                if lit_start < 0:
                    lit_start = d
                insert(d)
                d += 1
                continue
            flush_literals(d)
            op, length, _, off = cmd
            _emit_header(out, op, length)
            if op == _OP_FILL or op == _OP_INCREASING:
                out.append(data[d])
            elif op == _OP_WORD_FILL:
                out += data[d : d + 2]
            elif op == _OP_BACKREF:
                if big_endian_offsets:
                    out += bytes(((off >> 8) & 0xFF, off & 0xFF))
                else:
                    out += bytes((off & 0xFF, (off >> 8) & 0xFF))
            for k in range(length):
                insert(d + k)
            d += length
        else:
            if lit_start < 0:
                lit_start = d
            insert(d)
            d += 1
    flush_literals(n)
    out.append(_TERMINATOR)
    return bytes(out)


class _LzDecompressBase:
    _big_endian: bool

    def decompress(self, data: bytes, ctx: PipelineContext) -> bytes:
        # Strict first: reaching the terminator means the structure's true end
        # is known. Only fall back to a best-effort partial decode when the
        # caller said the buffer may cut the structure short.
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


class _LzCompressBase:
    _big_endian: bool

    def compress(self, data: bytes, ctx: PipelineContext) -> bytes:
        return compress(data, big_endian_offsets=self._big_endian)


class Lz1Decompress(_LzDecompressBase):
    _big_endian = False
    info = PluginInfo(id="decompress.lz1", name="LZ1 (Zelda 3)", stage=Stage.DECOMPRESS)


class Lz1Compress(_LzCompressBase):
    _big_endian = False
    info = PluginInfo(id="compress.lz1", name="LZ1 (Zelda 3)", stage=Stage.COMPRESS)


class Lz2Decompress(_LzDecompressBase):
    _big_endian = True
    info = PluginInfo(
        id="decompress.lz2",
        name="LZ2 (SMW, Yoshi's Island)",
        stage=Stage.DECOMPRESS,
    )


class Lz2Compress(_LzCompressBase):
    _big_endian = True
    info = PluginInfo(
        id="compress.lz2", name="LZ2 (SMW, Yoshi's Island)", stage=Stage.COMPRESS
    )
