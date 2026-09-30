"""Text of one- and two-byte codes, for the packed tilemap engine.

Most Japanese games — and any stream written in Shift-JIS — keep their text as a
mix of widths: a code is one byte, except that certain byte values are **lead
bytes** that join the byte after them into a single character. Read at one
constant stride, such a stream splits every kanji into two cells, a lead the font
has no glyph for and an index the alphabet cannot spell. Read here, it is one
cell per character, which is what the picture draws and what the text window
types (``docs/graphics-formats-reference/text-formats.md`` §2).

Four parameters on :mod:`~celpix.plugins.builtins.tilemap_codec`'s preset, all
optional and all read by :func:`lead_scheme`:

- ``lead_codes`` — the lead bytes: a list whose elements are one code or an
  inclusive ``[first, last]`` range, so a Shift-JIS band is one element. A lead
  followed by another byte reads as **one** cell, ``(lead - lead_bias) << 8 |
  trail``; a lead with nothing after it (the region's, or the record's, last
  byte) is an ordinary one-byte cell.
- ``lead_bias`` — subtracted from a lead before it becomes the letter's high
  byte, default 0. Final Fantasy VI's leads ``$1C-$1F`` with bias ``$1B`` give
  letters ``$100-$4FF``, the glyph numbers its own renderer uses. A bias that
  would put any letter at or below ``$FF`` is refused: that letter would share a
  number with a one-byte code, and encode could not tell which to write.
- ``operands`` — a table from an operand count to the codes that take it,
  ``{ 1 = [0x11, 0x14], 2 = [[0x80, 0x83]] }``. Such a command swallows the
  bytes after it **whatever their value**, each as its own one-byte cell and
  never as a lead — a pause whose duration byte happens to be ``$1C`` is a pause
  and a number, not a pause and half a kanji. A command too near the end to have
  all its operands takes the bytes that are there (none, at the very end).
  Only meaningful beside ``lead_codes``, and refused without it.
- ``line_bytes`` — the stream is fixed-length records of this many bytes, each
  one line: a name table. Each record is read on its own, so a lead never pairs
  across a boundary, and its last cell ends the line
  (:attr:`~celpix.core.tilemap.Cell.ends_line`). A trailing partial record is
  dropped, as a partial cell is. Usable without ``lead_codes`` too, for a name
  table of plain one-byte letters.

All four need a **one-byte cell that is all index** (``iiii iiii``). A composed
letter is a number, not a word with fields in it, so nothing else a layout
could place — a palette bit, a flag bit, an ``e`` line-end bit — would have
anywhere to live on a two-byte letter; and ``line_bytes`` already ends each line
by position, which an ``e`` bit would say a second time. The side array is
refused for the same reason: its bits sit above a cell word these cells do not
have.

Encode is decode's exact inverse, and **refuses** rather than write bytes that
read back as different cells: a one-byte cell holding a lead where decode would
pair it with the next byte, an operand past ``$FF``, a letter above ``$FF``
whose lead is not a lead code, and a ``line_bytes`` record that does not come
back to its length. Every refusal names the cell. That is stricter than the
engine's usual masking (``tilemap_codec``'s docstring), deliberately: a masked
field loses one cell's high bits, where a mis-paired lead shifts every cell after
it.

The cell count is not the byte count here, and the host reads, draws, types and
saves a text region through its cells, so all of that carries over.
:meth:`~celpix.plugins.builtins.tilemap_codec.TilemapCodec.bytes_per_cell` stays
1 — the width of a blank cell, which is what creating or resizing a file asks.
Where the bytes matter the host asks :meth:`LeadScheme.widths` instead, through
the codec's ``cell_widths`` and ``line_bytes``: the text window keeps the region
full in bytes rather than in cells, its budget counts bytes, and the hex dump
shades a selection where its cells actually start
(``docs/design/fontmap-entry.md`` §5).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from celpix.core.tilemap import Cell

#: The preset parameters this module reads. Any of them present turns it on.
LEAD_CODES = "lead_codes"
LEAD_BIAS = "lead_bias"
OPERANDS = "operands"
LINE_BYTES = "line_bytes"
PARAMS = (LEAD_CODES, LEAD_BIAS, OPERANDS, LINE_BYTES)

# One shared cell per byte value. A cell is frozen, and a text region is
# thousands of them over a few dozen distinct codes, so the decode that runs on
# every load and every re-read builds none of its one-byte cells.
_ONE_BYTE = tuple(Cell(index=b) for b in range(0x100))


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _byte_set(name: str, value: object) -> frozenset[int]:
    """A list of codes and ``[first, last]`` ranges, as the set of bytes it names."""
    if not isinstance(value, list | tuple) or not value:
        raise ValueError(
            f"{name} must be a non-empty list of codes and [first, last] ranges, "
            f"got {value!r}"
        )
    out: set[int] = set()
    for item in value:
        if _is_int(item):
            first = last = item
        elif (
            isinstance(item, list | tuple)
            and len(item) == 2
            and all(_is_int(end) for end in item)
        ):
            first, last = item
        else:
            raise ValueError(
                f"{name}: {item!r} is neither a code nor a [first, last] range"
            )
        if not 0 <= first <= last <= 0xFF:
            raise ValueError(
                f"{name}: {item!r} is not a byte or an ascending range of bytes "
                "($00-$FF)"
            )
        out.update(range(first, last + 1))
    return frozenset(out)


def _operand_counts(value: object, leads: frozenset[int]) -> dict[int, int]:
    """``operands`` as code → how many bytes it swallows."""
    if not isinstance(value, Mapping):
        raise ValueError(
            f"operands must be a table of count = [codes], like "
            f"{{ 1 = [0x11] }}, got {value!r}"
        )
    counts: dict[int, int] = {}
    for key, codes in value.items():
        # TOML table keys are always strings; a preset built in Python may use ints.
        count = key if _is_int(key) else None
        if isinstance(key, str) and key.strip().isdigit():
            count = int(key)
        if count is None or count < 1:
            raise ValueError(
                f"operands: {key!r} is not an operand count - a key is how many "
                "bytes the codes under it swallow, 1 or more"
            )
        for code in sorted(_byte_set(f"operands.{key}", codes)):
            if code in counts:
                raise ValueError(
                    f"operands: ${code:02X} is listed under both {counts[code]} "
                    f"and {count}"
                )
            if code in leads:
                # Decode would have to choose between reading the byte after it
                # as an operand and as a trail, and encode could not say which.
                raise ValueError(
                    f"operands: ${code:02X} is also a lead code - a byte is one or "
                    "the other"
                )
            counts[code] = count
    return counts


@dataclass(frozen=True, slots=True)
class LeadScheme:
    """One preset's reading of a mixed-width stream, validated."""

    leads: frozenset[int]
    bias: int
    operands: Mapping[int, int]
    #: Record length for a name table; 0 for one continuous stream.
    line_bytes: int
    #: The highest cell index there is: the last two-byte letter, or ``$FF``.
    top: int

    def decode(self, data: bytes) -> list[Cell]:
        size = self.line_bytes
        if not size:
            return self._tokens(data)
        cells: list[Cell] = []
        for at in range(0, len(data) - size + 1, size):
            record = self._tokens(data[at : at + size])
            record[-1] = Cell(index=record[-1].index, ends_line=True)
            cells += record
        return cells

    def encode(self, cells: list[Cell]) -> bytes:
        size = self.line_bytes
        if not size:
            return self._bytes(cells, 0)
        out = bytearray()
        start = 0
        for k, cell in enumerate(cells):
            if not cell.ends_line:
                continue
            raw = self._bytes(cells[start : k + 1], start)
            if len(raw) != size:
                raise ValueError(
                    f"cells {start}-{k}: record {len(out) // size} comes to "
                    f"{len(raw)} bytes, and every record is {size}"
                )
            out += raw
            start = k + 1
        if start < len(cells):
            raise ValueError(
                f"cells {start}-{len(cells) - 1}: the last record has no line end, "
                "and every record is one line"
            )
        return bytes(out)

    def widths(self, cells: list[Cell]) -> list[int]:
        """How many bytes each cell stands on — what a read of :meth:`encode`'s
        output would take it to be.

        A letter above ``$FF`` is two and everything else one, with one exception:
        a one-byte cell holding a **lead**, anywhere a byte follows it, is two.
        Encode refuses to write one there, and it is two because that is what a
        read would make of it — the lead and the byte after it, joined. Answering
        1 would tell the host it can fill a region with such a code, and the save
        would then refuse every cell of the fill. An operand is one byte whatever
        it holds, and a record's last cell ends its stream, as in :meth:`_bytes`.

        A scan and no more, since it is asked per keystroke of a text region.
        """
        leads, operands, records = self.leads, self.operands, bool(self.line_bytes)
        last = len(cells) - 1
        out: list[int] = []
        pending = 0
        for k, cell in enumerate(cells):
            code = cell.index
            end = k == last or (records and cell.ends_line)
            if pending:
                out.append(1)
                pending = 0 if end else pending - 1
                continue
            if code > 0xFF or (code in leads and not end):
                out.append(2)
                continue
            out.append(1)
            if not end:
                pending = operands.get(code, 0)
        return out

    def _tokens(self, data: bytes) -> list[Cell]:
        """One stream's cells: a letter each, an operand as its own byte."""
        leads, bias, operands = self.leads, self.bias, self.operands
        one = _ONE_BYTE
        out: list[Cell] = []
        i, n = 0, len(data)
        while i < n:
            b = data[i]
            if i + 1 < n:
                count = operands.get(b)
                if count:
                    # Taken whatever they hold: an operand is never a lead.
                    stop = min(n, i + 1 + count)
                    out += [one[x] for x in data[i:stop]]
                    i = stop
                    continue
                if b in leads:
                    out.append(Cell(index=((b - bias) << 8) | data[i + 1]))
                    i += 2
                    continue
            out.append(one[b])
            i += 1
        return out

    def _bytes(self, cells: list[Cell], base: int) -> bytes:
        """The inverse of :meth:`_tokens`; ``base`` numbers the cells in errors."""
        leads, bias, operands, top = self.leads, self.bias, self.operands, self.top
        out = bytearray()
        pending = 0
        last = len(cells) - 1
        for k, cell in enumerate(cells):
            code = cell.index
            if pending:
                if not 0 <= code <= 0xFF:
                    raise ValueError(
                        f"cell {base + k}: an operand is one byte, not {code:#x}"
                    )
                out.append(code)
                pending -= 1
                continue
            if code > 0xFF:
                if code > top:
                    raise ValueError(
                        f"cell {base + k}: {code:#x} is past the last two-byte "
                        f"letter, {top:#x}"
                    )
                lead = (code >> 8) + bias
                if lead not in leads:
                    raise ValueError(
                        f"cell {base + k}: {code:#x} would need ${lead:02X} as its "
                        "lead byte, which is not a lead code"
                    )
                out.append(lead)
                out.append(code & 0xFF)
                continue
            if code < 0:
                raise ValueError(f"cell {base + k}: {code} is not a code")
            # A lead is only a lead where a byte follows it, so the last cell may
            # hold one; anywhere else it would pair with its neighbour on read.
            if code in leads and k != last:
                raise ValueError(
                    f"cell {base + k}: a lone ${code:02X} would read back as a lead "
                    "byte, joined to the cell after it"
                )
            out.append(code)
            count = operands.get(code)
            if count:
                pending = min(count, last - k)
        return bytes(out)


def wants_lead_scheme(params: Mapping[str, Any]) -> bool:
    """Whether the preset states any of this module's parameters — the fast gate.

    Asked on every decode and encode of every packed map, so it is four key
    lookups and nothing else: a preset without them pays no more than that.
    """
    return any(name in params for name in PARAMS)


def lead_scheme(
    params: Mapping[str, Any],
    cell_bytes: int,
    field_limits: Mapping[str, int],
    has_side: bool,
) -> LeadScheme:
    """The preset's :class:`LeadScheme`, or a ValueError saying what is wrong.

    ``cell_bytes`` and ``field_limits`` (field name → its highest value) are the
    packed engine's own reading of ``fields``, passed in so the layout is parsed
    in one place; ``has_side`` is whether it states ``side_fields``. Only call
    this where :func:`wants_lead_scheme` said yes.
    """
    given = ", ".join(name for name in PARAMS if name in params)
    if (
        cell_bytes != 1
        or set(field_limits) != {"index"}
        or field_limits["index"] != 0xFF
    ):
        extra = sorted(set(field_limits) - {"index"})
        why = (
            " - line_bytes ends each record's line itself, so an `e` bit would say "
            "it twice"
            if "terminator" in extra
            else ""
        )
        raise ValueError(
            f"{given} read a one-byte cell that is all index (`iiii iiii`); this "
            f"layout is {cell_bytes * 8} bits"
            + (f" and also places {', '.join(extra)}" if extra else "")
            + why
        )
    if has_side:
        raise ValueError(f"{given} cannot be combined with side_fields")

    leads: frozenset[int] = frozenset()
    if LEAD_CODES in params:
        leads = _byte_set(LEAD_CODES, params[LEAD_CODES])
    for name in (LEAD_BIAS, OPERANDS):
        if name in params and not leads:
            raise ValueError(f"{name} only means something beside lead_codes")

    bias = params.get(LEAD_BIAS, 0)
    if not _is_int(bias):
        raise ValueError(f"lead_bias must be an integer, got {bias!r}")
    if leads and min(leads) - bias < 1:
        # Letter (lead - bias) << 8 would be $00xx or below: the same number as a
        # one-byte code, so encode could not know which of the two to write.
        raise ValueError(
            f"lead_bias {bias:#x} turns lead ${min(leads):02X} into letters at or "
            "below $FF, where the one-byte codes are - the bias can be at most "
            f"{min(leads) - 1:#x}"
        )

    operands = _operand_counts(params[OPERANDS], leads) if OPERANDS in params else {}

    line_bytes = params.get(LINE_BYTES, 0)
    if LINE_BYTES in params and (not _is_int(line_bytes) or line_bytes < 1):
        raise ValueError(
            f"line_bytes must be a positive record length, got {line_bytes!r}"
        )

    top = ((max(leads) - bias) << 8) | 0xFF if leads else 0xFF
    return LeadScheme(leads, bias, operands, line_bytes, top)
