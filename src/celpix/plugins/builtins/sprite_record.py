"""A sprite record described field by field — the table-driven sprite engine.

Every sprite object celPix reads is the same idea — a run of records, each a
rectangle of tiles at a signed pixel offset — and every console, and every
compiled engine on it, lays that record out its own way: which field comes
first, how wide the offsets are, whether the size is a nibble or two counts,
where the flips sit in the attribute word. The other sprite engines are one
codec per layout. This one is a **layout stated as data**, so a new record is a
preset:

.. code-block:: toml

    engine_id = "codec.tilemap.sprite-record"
    [params]
    layout = "sprite"
    subsprite_size = "stated"
    column_major = true
    record = [
      { name = "x",        type = "s16" },
      { name = "x_mirror", type = "s16" },          # carried: nothing reads it
      { name = "y",        type = "s16" },
      { name = "pad",      type = "u8" },
      { name = "size",     type = "u8",  bits = "....ccrr" },
      { name = "attr",     type = "u16", bits = "oppv hiii iiii iiii" },
    ]
    frame_header = { bytes = 6, count_at = 4, count_type = "u16" }

**Parallel arrays.** ``arrays = [["y"], ["x", "tile"]]`` stores the record's
fields as groups, each ``count`` elements long, one after the other inside the
frame — the Master System sprite table's own shape, all the Ys and then the
(X, tile) pairs. The groups list the record's fields once each, in order, so a
piece is still exactly the record it would be back to back; ``frame_header`` is
required, since its count is where one array ends.

**Fields.** ``type`` is ``u8``, ``s8``, ``u16``, ``s16``, ``u32`` or ``s32``,
multi-byte fields in the preset's ``endian`` (big unless it says little). A field
named ``x`` or ``y`` is the piece's offset from the object's origin. A field with
``bits`` is parcelled out by a bit layout written as its documentation writes it
(:mod:`~celpix.plugins.builtins._fields`), in this engine's letters:

====  ==================================================================
i     tile index (split runs are one field, most significant first)
p     palette row
o     priority
h     horizontal flip
v     vertical flip
c     the piece's width in tiles, minus one
r     the piece's height in tiles, minus one
====  ==================================================================

A preset's ``legend`` may rename them, as for a tilemap cell. Every other field —
and every bit of a ``bits`` field no letter claims — is **carried**: kept byte
for byte and written back unchanged, so a field celPix has no meaning for costs
nothing on a save. A record with no ``c``/``r`` bits draws 1x1 pieces.

**Frames.** Without ``frame_header`` the whole run is one frame, as for
``md-sprite``. With one, the run is frames back to back, each ``bytes`` of header
holding its piece count at ``count_at`` (``count_type``, default ``u16``); the
header's other bytes are carried with the frame's first piece, and the count is
rewritten from the frame on a save. ``column_major`` says a piece's tiles run
down each column before the next, as a Mega Drive sprite's do.

Sprite objects are drawn read-only by the canvas, so what an encode meets is
the cells decode produced; it is the exact inverse all the same.
"""

from __future__ import annotations

from typing import Any

from celpix.core.address import format_hex
from celpix.core.context import PipelineContext
from celpix.core.errors import Stage
from celpix.core.sprite import Frame, Subsprite
from celpix.core.tilemap import Cell, CellOp
from celpix.plugins.base import PluginInfo
from celpix.plugins.builtins._fields import parse_layout, resolve_legend
from celpix.plugins.builtins._mask import gather, scatter

SPRITE_RECORD_ENGINE = "codec.tilemap.sprite-record"

_TYPES = {"u8": 1, "s8": 1, "u16": 2, "s16": 2, "u32": 4, "s32": 4}
_LEGEND = {
    "i": "index",
    "p": "palette",
    "o": "priority",
    "h": "flip_h",
    "v": "flip_v",
    "c": "columns",
    "r": "rows",
}
# A frame's first piece carries its header above the record, marked by this bit.
_HEADER_SHIFT = 8 * 64


class _Layout:
    """A preset's ``record`` resolved once: offsets, widths, placements."""

    def __init__(self, params: dict[str, Any]) -> None:
        spec = params.get("record")
        if not isinstance(spec, list) or not spec:
            raise ValueError("a sprite record needs a `record` list of fields")
        self.order = "little" if params.get("endian") == "little" else "big"
        legend = resolve_legend(
            _LEGEND, params.get("legend"), frozenset(_LEGEND.values())
        )
        self.fields: list[tuple[str, int, int, bool, dict]] = []
        at = 0
        seen: set[str] = set()
        for field in spec:
            name, kind = str(field.get("name", "")), str(field.get("type", ""))
            if kind not in _TYPES:
                raise ValueError(
                    f"field {name!r}: type must be one of {', '.join(_TYPES)}"
                )
            width = _TYPES[kind]
            bits = field.get("bits")
            placed = (
                parse_layout(bits, legend, width * 8) if isinstance(bits, str) else {}
            )
            for part in placed:
                if part in seen:
                    raise ValueError(f"{part!r} is placed by two fields of the record")
                seen.add(part)
            self.fields.append((name, at, width, kind.startswith("s"), placed))
            at += width
        if at * 8 > _HEADER_SHIFT:
            raise ValueError(
                f"a record is at most {_HEADER_SHIFT // 8} bytes, not {at}"
            )
        self.size = at
        self.has_rows = "palette" in seen
        self.index_bits = (
            sum(w for _s, w in self._placement("index")[1]) if "index" in seen else 0
        )
        self.row_bits = (
            sum(w for _s, w in self._placement("palette")[1]) if self.has_rows else 0
        )
        header = params.get("frame_header")
        if header is None:
            self.header = None
        else:
            length = int(header.get("bytes", 0))
            count_at = int(header.get("count_at", 0))
            count_type = str(header.get("count_type", "u16"))
            if count_type not in _TYPES or count_at + _TYPES[count_type] > length:
                raise ValueError("frame_header's count must sit inside its bytes")
            self.header = (length, count_at, _TYPES[count_type])
        self.column_major = bool(params.get("column_major", False))
        self.arrays = self._arrays(params.get("arrays"))

    def _arrays(self, spec: Any) -> tuple[tuple[int, int], ...] | None:
        """``arrays``: the record's fields as parallel arrays, each group stored
        as ``count`` consecutive elements — ``(offset in the record, width)`` per
        group. The groups must list the record's fields once each, in order, so
        a piece still reads as exactly the record it would be back to back."""
        if spec is None:
            return None
        if self.header is None:
            raise ValueError(
                "`arrays` needs a `frame_header`: its count is what says where "
                "one array ends and the next begins"
            )
        names = [name for name, _at, _w, _s, _p in self.fields]
        if not isinstance(spec, list) or [n for g in spec for n in g] != names:
            raise ValueError(
                f"`arrays` must group the record's fields {names} in order, "
                f"each once; got {spec}"
            )
        width = {name: w for name, _at, w, _s, _p in self.fields}
        at = {name: a for name, a, _w, _s, _p in self.fields}
        return tuple(
            (at[group[0]], sum(width[n] for n in group)) for group in spec if group
        )

    def _placement(self, part: str):  # noqa: ANN202
        for _name, _at, _width, _signed, placed in self.fields:
            if part in placed:
                return placed[part]
        raise KeyError(part)

    def value(self, record: bytes, name: str) -> int | None:
        for field, at, width, signed, _placed in self.fields:
            if field == name:
                return int.from_bytes(
                    record[at : at + width], self.order, signed=signed
                )
        return None

    def part(self, record: bytes, part: str, default: int = 0) -> int:
        for _name, at, width, _signed, placed in self.fields:
            if part in placed:
                chunks, sw = placed[part]
                return gather(
                    int.from_bytes(record[at : at + width], self.order), chunks, sw
                )
        return default

    def with_parts(self, record: bytes, parts: dict[str, int]) -> bytes:
        """``record`` with the named bit fields replaced, every other bit kept."""
        out = bytearray(record)
        for _name, at, width, _signed, placed in self.fields:
            touched = [p for p in placed if p in parts]
            if not touched:
                continue
            word = int.from_bytes(out[at : at + width], self.order)
            for p in touched:
                chunks, sw = placed[p]
                mask = 0
                for chunk in chunks:
                    mask |= chunk
                word = (word & ~mask) | scatter(parts[p], chunks, sw)
            out[at : at + width] = word.to_bytes(width, self.order)
        return bytes(out)


def _cell(layout: _Layout, record: bytes, header: bytes | None) -> Cell:
    flags = int.from_bytes(record, "big")
    if header is not None:
        flags |= (1 | int.from_bytes(header, "big") << 1) << _HEADER_SHIFT
    return Cell(
        index=layout.part(record, "index"),
        palette_row=layout.part(record, "palette"),
        priority=layout.part(record, "priority"),
        flip_h=bool(layout.part(record, "flip_h")),
        flip_v=bool(layout.part(record, "flip_v")),
        flags=flags,
    )


def _record(layout: _Layout, cell: Cell) -> bytes:
    """The cell's record: its carried bytes with the Cell's own fields laid back in."""
    stored = (cell.flags & ((1 << _HEADER_SHIFT) - 1)).to_bytes(layout.size, "big")
    parts = {
        "index": cell.index,
        "palette": cell.palette_row,
        "priority": cell.priority,
        "flip_h": int(cell.flip_h),
        "flip_v": int(cell.flip_v),
    }
    placed = {p for _n, _a, _w, _s, pl in layout.fields for p in pl}
    return layout.with_parts(stored, {p: v for p, v in parts.items() if p in placed})


def _piece(layout: _Layout, data: bytes, first: int, count: int, i: int) -> bytes:
    """Piece ``i`` of the frame whose pieces start at ``first``: a record back to
    back, or its slice of every parallel array joined back into one."""
    if layout.arrays is None:
        at = first + i * layout.size
        return data[at : at + layout.size]
    out, base = bytearray(), first
    for _start, width in layout.arrays:
        out += data[base + i * width : base + (i + 1) * width]
        base += count * width
    return bytes(out)


def _group(cells: list[Cell], framed: bool) -> list[list[Cell]]:
    if not framed:
        return [cells] if cells else []
    frames: list[list[Cell]] = []
    for cell in cells:
        if cell.flags >> _HEADER_SHIFT & 1 or not frames:
            frames.append([])
        frames[-1].append(cell)
    return frames


class SpriteRecordCodec:
    """The table-driven sprite record, on the tilemap codec surface."""

    info = PluginInfo(
        id=SPRITE_RECORD_ENGINE,
        name="Sprite record (fields stated by the preset)",
        stage=Stage.INTERPRET_TILEMAP,
    )

    def decode(
        self, data: bytes, params: dict[str, Any], ctx: PipelineContext
    ) -> list[Cell]:
        layout = _Layout(params)
        cells: list[Cell] = []
        at, size = 0, layout.size
        if layout.header is None:
            for at in range(0, len(data) - size + 1, size):
                cells.append(_cell(layout, data[at : at + size], None))
            return cells
        length, count_at, count_width = layout.header
        while at + length <= len(data):
            header = data[at : at + length]
            count = int.from_bytes(
                header[count_at : count_at + count_width], layout.order
            )
            end = at + length + count * size
            if end > len(data):
                raise ValueError(
                    f"the frame at {format_hex(at, None)} holds {count} pieces, "
                    "running "
                    f"{end - len(data)} bytes past the data"
                )
            for i in range(count):
                cells.append(
                    _cell(
                        layout,
                        _piece(layout, data, at + length, count, i),
                        header if i == 0 else None,
                    )
                )
            if not count:
                raise ValueError(
                    f"the frame at {format_hex(at, None)} holds no pieces, "
                    "which a run cannot carry"
                )
            at = end
        return cells

    def encode(
        self, cells: list[Cell], params: dict[str, Any], ctx: PipelineContext
    ) -> bytes:
        layout = _Layout(params)
        out = bytearray()
        for frame in _group(cells, layout.header is not None):
            if layout.header is not None:
                length, count_at, count_width = layout.header
                stored = frame[0].flags >> _HEADER_SHIFT >> 1
                header = bytearray(stored.to_bytes(length, "big"))
                header[count_at : count_at + count_width] = len(frame).to_bytes(
                    count_width, layout.order
                )
                out += header
            records = [_record(layout, cell) for cell in frame]
            if layout.arrays is None:
                out += b"".join(records)
            else:
                for start, width in layout.arrays:
                    out += b"".join(r[start : start + width] for r in records)
        return bytes(out)

    def bytes_per_cell(self, params: dict[str, Any]) -> int:
        return _Layout(params).size

    def cell_tiles(self, params: dict[str, Any]) -> tuple[int, int]:
        # A piece may be any rectangle, so there is no one answer; the frame
        # renderer asks each subsprite instead.
        return (1, 1)

    def frames(
        self, cells: list[Cell], params: dict[str, Any], ctx: PipelineContext
    ) -> list[Frame]:
        layout = _Layout(params)
        out = []
        for frame in _group(cells, layout.header is not None):
            pieces = []
            for cell in frame:
                record = _record(layout, cell)
                pieces.append(
                    Subsprite(
                        x=layout.value(record, "x") or 0,
                        y=layout.value(record, "y") or 0,
                        index=cell.index,
                        palette_row=cell.palette_row,
                        priority=cell.priority,
                        flip_h=cell.flip_h,
                        flip_v=cell.flip_v,
                        across=layout.part(record, "columns") + 1,
                        down=layout.part(record, "rows") + 1,
                        column_major=layout.column_major,
                    )
                )
            out.append(tuple(pieces))
        return out

    def has_palette_rows(self, params: dict[str, Any]) -> bool:
        return _Layout(params).has_rows

    def index_limit(self, params: dict[str, Any]) -> int | None:
        bits = _Layout(params).index_bits
        return (1 << bits) - 1 if bits else None

    def palette_row_limit(self, params: dict[str, Any]) -> int:
        bits = _Layout(params).row_bits
        return (1 << bits) - 1 if bits else 0

    def transform_cell(
        self, cell: Cell, op: CellOp, params: dict[str, Any]
    ) -> Cell | None:
        """Both mirrors where the record has the bits, and no turn."""
        layout = _Layout(params)
        placed = {p for _n, _a, _w, _s, pl in layout.fields for p in pl}
        if op is CellOp.FLIP_H and "flip_h" in placed:
            return cell.flipped_h()
        if op is CellOp.FLIP_V and "flip_v" in placed:
            return cell.flipped_v()
        return None
