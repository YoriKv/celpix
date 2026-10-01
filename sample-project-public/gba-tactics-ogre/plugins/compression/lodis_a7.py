"""Knight of Lodis ``A7`` archive codec, ``compression.lodis-a7``.

Archive layout (big-endian)::

    +0  "A7"
    +2  entry count               u16
    +4  dictionary offset         u32, from the archive start
    +8  entry offsets             u32 each, from the archive start
        packed entries
    dictionary: mode u8, bits u8, entry size u16, then 1024 dictionary bytes

Each entry decodes to ``entry size`` bytes (mode 0):

- ``1ccccccc``: ``c + 1`` literal bytes follow.
- ``0lllllOO OOOOOOOO`` (bits = 5): copy ``l + 3`` bytes from dictionary offset
  ``O``.

Decode: every entry, end to end. Encode: every entry re-packed (shortest parse),
laid out back to back after the table, table rewritten, zero padding up to the
dictionary; header and dictionary copied from the original archive. Refused if
the entries do not fit before the dictionary.
"""

from __future__ import annotations

from celpix.core.context import (
    KEY_COMPRESSED_SIZE,
    KEY_DECOMPRESS_COMPLETE,
    KEY_DECOMPRESS_PARTIAL,
    KEY_SURROUND,
    KEY_SURROUND_START,
    PipelineContext,
)
from celpix.core.errors import Stage
from celpix.plugins.base import PluginInfo

PLUGIN_ID = "compression.lodis-a7"

MAGIC = b"A7"
_HEADER = 8  # magic, count, dictionary offset
_DICT_HEADER = 4  # mode, bits, entry size
_LITERAL = 0x80
_MAX_LITERAL = 0x80  # a run of 1-128 literals


def _u16(data: bytes, at: int) -> int:
    return int.from_bytes(data[at : at + 2], "big")


def _u32(data: bytes, at: int) -> int:
    return int.from_bytes(data[at : at + 4], "big")


class _Dictionary:
    """Dictionary header fields and data."""

    def __init__(self, data: bytes, at: int) -> None:
        if at + _DICT_HEADER > len(data):
            raise EOFError("dictionary header past the end of the data")
        # Header: mode, length-field bits, output size per entry.
        self.mode, self.bits = data[at], data[at + 1]
        if self.mode != 0:
            raise ValueError(f"dictionary mode {self.mode} is not supported (only 0)")
        if not 1 <= self.bits <= 7:
            raise ValueError(f"dictionary length field of {self.bits} bits is invalid")
        self.out_size = _u16(data, at + 2)
        if self.out_size == 0:
            raise ValueError("dictionary header declares entries of 0 bytes")
        self.mask = 0xFFFF >> (self.bits + 1)  # offset field mask (10 bits)
        self.max_copy = (1 << self.bits) - 1 + 3  # longest copy (34)
        # Dictionary bytes: as many as an offset can address (1024).
        self.start = at + _DICT_HEADER
        self.data = data[self.start : self.start + self.mask + 1]

    @property
    def end(self) -> int:
        return self.start + len(self.data)


def _decode_entry(data: bytes, at: int, dic: _Dictionary) -> tuple[bytes, int]:
    """Decode one entry at ``data[at]``; returns (output, bytes consumed)."""
    out = bytearray()
    pos = at
    while len(out) < dic.out_size:
        if pos >= len(data):
            raise EOFError("entry runs past the end of the data")
        c = data[pos]
        if c & _LITERAL:
            # 1ccccccc: copy c + 1 literal bytes from the stream.
            n = (c & 0x7F) + 1
            if pos + 1 + n > len(data):
                raise EOFError("literal run past the end of the data")
            out += data[pos + 1 : pos + 1 + n]
            pos += 1 + n
        else:
            # 0lllllOO OOOOOOOO: copy l + 3 bytes from dictionary offset O.
            if pos + 2 > len(data):
                raise EOFError("copy past the end of the data")
            n = ((c << 1) >> (8 - dic.bits)) + 3
            src = ((c << 8) | data[pos + 1]) & dic.mask
            if src + n > len(dic.data):
                raise ValueError(f"copy of {n} at {src} runs past the dictionary")
            out += dic.data[src : src + n]
            pos += 2
    # The last command must end exactly at the entry size.
    if len(out) != dic.out_size:
        raise ValueError(f"entry overruns its {dic.out_size}-byte output")
    return bytes(out), pos - at


def _encode_entry(chunk: bytes, dic: _Dictionary) -> bytes:
    """Shortest encoding of ``chunk`` (dynamic programming over literal runs and
    dictionary copies)."""
    n = len(chunk)
    # longest[i]: longest dictionary match starting at chunk[i].
    longest = []
    for i in range(n):
        best = 0
        for length in range(3, min(dic.max_copy, n - i) + 1):
            if dic.data.find(chunk[i : i + length], 0, dic.mask + length) < 0:
                break
            best = length
        longest.append(best)
    # cost[i]: fewest bytes to encode chunk[i:], filled from the end backwards.
    inf = 1 << 30
    cost = [inf] * (n + 1)
    step = [0] * (n + 1)  # positive: a copy of that length; negative: literals
    cost[n] = 0
    for i in range(n - 1, -1, -1):
        # Option 1: a 2-byte dictionary copy of any matching length.
        for length in range(3, longest[i] + 1):
            if 2 + cost[i + length] < cost[i]:
                cost[i], step[i] = 2 + cost[i + length], length
        # Option 2: a literal run (1 command byte + the bytes).
        for run in range(1, min(_MAX_LITERAL, n - i) + 1):
            if 1 + run + cost[i + run] < cost[i]:
                cost[i], step[i] = 1 + run + cost[i + run], -run
    # Emit the chosen commands from the start.
    out = bytearray()
    i = 0
    while i < n:
        if step[i] > 0:
            length = step[i]
            at = dic.data.find(chunk[i : i + length])
            out += (((length - 3) << (15 - dic.bits)) | at).to_bytes(2, "big")
        else:
            length = -step[i]
            out.append(_LITERAL | (length - 1))
            out += chunk[i : i + length]
        i += length
    return bytes(out)


def _parse(data: bytes) -> tuple[int, list[int], _Dictionary]:
    """Archive header: entry count, entry offsets, dictionary."""
    if len(data) < _HEADER:
        raise EOFError("archive header past the end of the data")
    if data[:2] != MAGIC:
        raise ValueError("no A7 archive here: the first two bytes are not 'A7'")
    count = _u16(data, 2)  # +2: entry count
    table_end = _HEADER + 4 * count
    if count == 0 or table_end > len(data):
        raise EOFError("entry table past the end of the data")
    offsets = [_u32(data, _HEADER + 4 * k) for k in range(count)]  # +8: entry table
    dic_at = _u32(data, 4)  # +4: dictionary offset
    ordered = all(a <= b for a, b in zip(offsets, offsets[1:], strict=False))
    if not (table_end <= offsets[0] and ordered and offsets[-1] < dic_at):
        raise ValueError("entry offsets are out of order or overlap the table")
    return count, offsets, _Dictionary(data, dic_at)


class LodisA7:
    info = PluginInfo(
        id=PLUGIN_ID,
        name="Knight of Lodis A7 archive (preset dictionary)",
        stage=Stage.COMPRESSION,
        self_delimiting=True,
        alignment=4,
        category="Nintendo",
    )

    def decompress(self, data: bytes, ctx: PipelineContext) -> bytes:
        # Partial: the preview may hand a truncated window; return what decodes.
        partial = ctx.get(KEY_DECOMPRESS_PARTIAL)
        try:
            count, offsets, dic = _parse(data)
        except EOFError:
            if not partial:
                raise
            ctx.set(KEY_COMPRESSED_SIZE, len(data))
            ctx.set(KEY_DECOMPRESS_COMPLETE, False)
            return b""
        # Decode every entry where the table says it starts, end to end.
        out = bytearray()
        for start in offsets:
            try:
                chunk, _ = _decode_entry(data, start, dic)
            except EOFError:
                if not partial:
                    raise
                ctx.set(KEY_COMPRESSED_SIZE, len(data))
                ctx.set(KEY_DECOMPRESS_COMPLETE, False)
                return bytes(out)
            out += chunk
        # The archive must contain its whole dictionary.
        if len(dic.data) != dic.mask + 1:
            if not partial:
                raise EOFError("dictionary runs past the end of the data")
            ctx.set(KEY_COMPRESSED_SIZE, len(data))
            ctx.set(KEY_DECOMPRESS_COMPLETE, False)
            return bytes(out)
        # The archive ends with the dictionary.
        ctx.set(KEY_COMPRESSED_SIZE, dic.end)
        ctx.set(KEY_DECOMPRESS_COMPLETE, True)
        return bytes(out)

    def compress(self, data: bytes, ctx: PipelineContext) -> bytes:
        # Header and dictionary come from the original archive (KEY_SURROUND).
        surround = ctx.get(KEY_SURROUND)
        if surround is None:
            raise ValueError(
                "the archive's own header and dictionary are needed to re-pack it, "
                "and this save does not provide the file around the slice"
            )
        original = bytes(surround[int(ctx.get(KEY_SURROUND_START, 0) or 0) :])
        count, _, dic = _parse(original)
        if len(data) != count * dic.out_size:
            raise ValueError(
                f"{len(data)} bytes is not {count} entries of {dic.out_size}"
            )
        dic_at = dic.start - _DICT_HEADER
        # Re-pack every entry back to back after the table, recording each offset.
        body = bytearray()
        table = bytearray()
        at = _HEADER + 4 * count
        for k in range(count):
            packed = _encode_entry(data[k * dic.out_size : (k + 1) * dic.out_size], dic)
            table += (at + len(body)).to_bytes(4, "big")
            body += packed
        # The entries must fit between the table and the dictionary.
        room = dic_at - (_HEADER + 4 * count)
        if len(body) > room:
            raise ValueError(
                f"the entries pack to {len(body)} bytes but the archive holds "
                f"{room} before its dictionary: {len(body) - room} bytes over"
            )
        # Original header, new table, entries, zero padding, original dictionary.
        return (
            bytes(original[:_HEADER])
            + bytes(table)
            + bytes(body)
            + bytes(room - len(body))
            + bytes(original[dic_at : dic.end])
        )


def register(registry):
    registry.register(LodisA7())
