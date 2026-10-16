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
with ``bits`` is parcelled out by a bit layout written as its documentation
writes it (:mod:`~celpix.plugins.builtins._fields`), in this engine's letters:

====  ==================================================================
i     tile index (split runs are one field, most significant first)
p     palette row
o     priority
h     horizontal flip
v     vertical flip
c     the piece's width in tiles, minus one
r     the piece's height in tiles, minus one
s     the piece's size code (see **Size**)
x     the piece's x offset from the object's origin
y     the piece's y offset from the object's origin
====  ==================================================================

A preset's ``legend`` may rename them, as for a tilemap cell. On this engine
``x`` is the offset, not the spare bit it is elsewhere. Every other field — and
every bit of a ``bits`` field no letter claims — is **carried**: kept byte for
byte and written back unchanged, so a field celPix has no meaning for costs
nothing on a save.

**Offsets.** A piece's ``x`` is its ``x`` bits where a field places them, else
the whole field named ``x``, else 0; ``y`` likewise — a field named ``x`` that
places its own ``x`` bits reads the bits. ``x`` bits in one field beside another
field named ``x`` is a load error. The field's ``type`` sign applies to its ``x``
and ``y`` bits, read as two's complement at their own width: ``sxxx xxxx`` on an
``s8`` is -64..63, on a ``u8`` 0..127. That is the only thing a field's sign
reaches inside it — a record packs an offset beside the size bit that shares its
byte, so the type has to describe the offset, and every other letter is a count
or a code.

**Size.** ``c``/``r`` bits state a piece's rectangle outright. Without them an
``s`` code picks it:

- with ``sizes = [[across, down], ...]`` (two or more), the code indexes the
  table — the shape of a game whose drawing routine looks its sizes up. A code
  past the end draws 1x1, with a notice. The preset states its sizes, so it
  must declare ``subsprite_size = "stated"``.
- without, ``s`` is one bit choosing between the two squares of the **size
  pair**, as a console's OBJ size bit does: 0 the small side, 1 the large. The
  pair is a register the game set, and it has **one owner**. A preset that
  knows it gives ``size_pair = [small, large]`` and declares
  ``subsprite_size = "stated"``, which hides the bar's Sm/Lg spins. A preset
  that does not leaves both out: the spins stay live, the pair is the entry's
  own (default ``[1, 2]``).

A record with neither draws pieces of ``subsprite_tiles`` (``[across, down]``,
default 1x1): a size the record does not hold because the routine that draws it
fixes it — a SNES OAM table whose objects are all 16x16, an 8x16 NES list.
Stated once for the whole record, it is a property of the preset rather than of
a piece, which is why it is not a field. ``s`` beside ``c``/``r``, ``sizes``
without ``s``, a wider ``s`` without ``sizes``, and ``size_pair`` on a record
with no lone ``s`` bit are load errors: each leaves a bit, or a key, whose
meaning the preset never stated. So are ``size_pair`` without ``stated`` and a
lone ``s`` with ``stated`` but no ``size_pair``: each gives the pair two owners,
or none.

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
from celpix.core.notices import warn
from celpix.core.sprite import DEFAULT_SUBSPRITE_TILES, Frame, Subsprite
from celpix.core.tilemap import Cell, CellOp
from celpix.plugins._params import byte_order, flag, integer
from celpix.plugins.base import PluginInfo
from celpix.plugins.builtins._cell_answers import mirror
from celpix.plugins.builtins._fields import parse_layout, resolve_legend
from celpix.plugins.builtins._mask import gather, scatter
from celpix.plugins.builtins.object_codec import subsprite_tiles

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
    "s": "size",
    "x": "x",
    "y": "y",
}
# The letters a field's sign reaches: the offsets, which a record packs beside
# other bits in one byte. Every other letter is a count, a code or a flag.
_SIGNED_PARTS = frozenset({"x", "y"})
# A frame's first piece carries its header above the record, marked by this bit.
_HEADER_SHIFT = 8 * 64


class RecordLayout:
    """A preset's ``record`` resolved once: offsets, widths, placements."""

    def __init__(self, params: dict[str, Any]) -> None:
        spec = params.get("record")
        if not isinstance(spec, list) or not spec:
            raise ValueError("a sprite record needs a `record` list of fields")
        self.order = byte_order(params, "endian", "big")
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
        # A field named `x` that places its own `x` bits is one statement, and
        # its bits win; a *different* field placing them is two answers.
        for axis in sorted(_SIGNED_PARTS & seen):
            if any(n == axis and axis not in p for n, _a, _w, _s, p in self.fields):
                raise ValueError(
                    f"{axis!r} is placed twice: by a field's bits and by the "
                    f"field named {axis!r} - drop one"
                )
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
            length = integer(header, "bytes", 0)
            count_at = integer(header, "count_at", 0)
            count_type = str(header.get("count_type", "u16"))
            if count_type not in _TYPES or count_at + _TYPES[count_type] > length:
                raise ValueError("frame_header's count must sit inside its bytes")
            self.header = (length, count_at, _TYPES[count_type])
        self.column_major = flag(params, "column_major")
        self.arrays = self._arrays(params.get("arrays"))
        self.tiles = self._tiles(params.get("subsprite_tiles"))
        self.sized = "columns" in seen or "rows" in seen
        self.coded = "size" in seen
        self.sizes = self._sizes(params)
        self.size_pair = self._pair(params.get("size_pair"))
        if "size_pair" in params and (not self.coded or self.sizes is not None):
            raise ValueError(
                "`size_pair` is what a lone s bit picks from, and this record "
                "has none - it sizes its pieces by c/r bits, `sizes` or "
                "`subsprite_tiles`"
            )

    def _sizes(self, params: dict[str, Any]) -> tuple[tuple[int, int], ...] | None:
        """``sizes``: the rectangles an ``s`` code indexes, checked against the
        rest of the record so no size bit is left meaning nothing."""
        spec = params.get("sizes")
        stated = params.get("subsprite_size") == "stated"
        if self.coded and self.sized:
            raise ValueError(
                "a record states its size by c/r bits or by an s code, not both"
            )
        if spec is None:
            if not self.coded:
                return None
            width = sum(w for _s, w in self._placement("size")[1])
            if width != 1:
                raise ValueError(
                    f"an s code of {width} bits needs a `sizes` table to index; "
                    "without one, s is a single bit choosing from the size pair"
                )
            # The pair has one owner. A preset that knows the game's register
            # states it, and an entry must not then override it; one that does
            # not must leave the spins live, or nothing sets the pair at all.
            if stated and "size_pair" not in params:
                raise ValueError(
                    "a lone size bit picks from the size pair, which "
                    '`subsprite_size = "stated"` hides - give the pair as '
                    "`size_pair`, or drop `stated` and let the entry set it"
                )
            if "size_pair" in params and not stated:
                raise ValueError(
                    "`size_pair` states the pair, so the preset must declare "
                    '`subsprite_size = "stated"` - otherwise the bar offers '
                    "spins that override what the preset knows"
                )
            return None
        if not self.coded:
            raise ValueError("`sizes` is indexed by s bits, and the record has none")
        ok = isinstance(spec, list) and len(spec) >= 2
        if ok:
            ok = all(
                isinstance(pair, list)
                and len(pair) == 2
                and all(type(n) is int and n >= 1 for n in pair)
                for pair in spec
            )
        if not ok:
            raise ValueError(
                "`sizes` must be two or more [across, down] pairs, each at least "
                f"1; got {spec!r}"
            )
        if not stated:
            raise ValueError(
                "`sizes` states every piece's size, so the preset must declare "
                '`subsprite_size = "stated"` - otherwise the bar offers a size '
                "pair that changes nothing"
            )
        return tuple((a, d) for a, d in spec)

    @staticmethod
    def _pair(spec: Any) -> tuple[int, int]:
        """``size_pair``: the ``[small, large]`` squares, in tiles, a lone size
        bit picks between, stated by a preset that knows the game's register."""
        if spec is None:
            return DEFAULT_SUBSPRITE_TILES
        ok = isinstance(spec, list) and len(spec) == 2
        if ok:
            ok = all(type(n) is int and n >= 1 for n in spec)
        if not ok:
            raise ValueError(
                f"`size_pair` must be [small, large], each at least 1; got {spec!r}"
            )
        return (spec[0], spec[1])

    @staticmethod
    def _tiles(spec: Any) -> tuple[int, int]:
        """``subsprite_tiles``: the ``(across, down)`` a record without size
        bits draws — a size the drawing routine fixes rather than the record."""
        if spec is None:
            return (1, 1)
        ok = isinstance(spec, list) and len(spec) == 2
        if ok:
            ok = all(type(n) is int and n >= 1 for n in spec)
        if not ok:
            raise ValueError(
                "`subsprite_tiles` must be [across, down], each at least 1; "
                f"got {spec!r}"
            )
        return (spec[0], spec[1])

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
        for _name, at, width, signed, placed in self.fields:
            if part in placed:
                chunks, sw = placed[part]
                raw = gather(
                    int.from_bytes(record[at : at + width], self.order), chunks, sw
                )
                bits = sum(w for _s, w in sw)
                if signed and part in _SIGNED_PARTS and raw >> (bits - 1):
                    raw -= 1 << bits
                return raw
        return default

    def offset(self, record: bytes, axis: str) -> int:
        """The piece's ``x`` or ``y``: its bits, else its whole field, else 0."""
        if any(axis in placed for _n, _a, _w, _s, placed in self.fields):
            return self.part(record, axis)
        return self.value(record, axis) or 0

    def shape(self, record: bytes, pair: tuple[int, int]) -> tuple[int, int]:
        """The piece's ``(across, down)`` in tiles: c/r, else s, else the fixed size."""
        if self.sized or not self.coded:
            return (
                self.part(record, "columns", self.tiles[0] - 1) + 1,
                self.part(record, "rows", self.tiles[1] - 1) + 1,
            )
        code = self.part(record, "size")
        if self.sizes is not None:
            return self.sizes[code] if code < len(self.sizes) else (1, 1)
        side = max(1, pair[1] if code else pair[0])
        return (side, side)

    def unsized(self, record: bytes) -> bool:
        """Whether the record's s code is past the end of ``sizes``."""
        return self.sizes is not None and self.part(record, "size") >= len(self.sizes)

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


def _cell(layout: RecordLayout, record: bytes, header: bytes | None) -> Cell:
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


def _record(layout: RecordLayout, cell: Cell) -> bytes:
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


def frame_pair(layout: RecordLayout, ctx: PipelineContext) -> tuple[int, int]:
    """The size pair a lone size bit resolves against: the one the host
    published (the preset's stated pair, or the entry's), else the preset's.
    Read leniently, as the host may hand on a hand-edited pair."""
    return subsprite_tiles(layout.size_pair, ctx)


def warn_unsized(
    ctx: PipelineContext, count: int, layout: RecordLayout, source: str
) -> None:
    """Say how many pieces drew 1x1 because ``sizes`` has no entry for their code."""
    if not count or layout.sizes is None:
        return
    warn(
        ctx,
        f"{count} piece{' has' if count == 1 else 's have'} a size code past "
        "this preset's `sizes`",
        f"They draw 1x1. `sizes` lists {len(layout.sizes)} entries, codes 0-"
        f"{len(layout.sizes) - 1};\nadd the rest, or check the s bits' position.",
        source=source,
    )


def subsprite(layout: RecordLayout, record: bytes, pair: tuple[int, int]) -> Subsprite:
    """The piece one record draws, a lone size bit resolved against ``pair``."""
    across, down = layout.shape(record, pair)
    return Subsprite(
        x=layout.offset(record, "x"),
        y=layout.offset(record, "y"),
        index=layout.part(record, "index"),
        palette_row=layout.part(record, "palette"),
        priority=layout.part(record, "priority"),
        flip_h=bool(layout.part(record, "flip_h")),
        flip_v=bool(layout.part(record, "flip_v")),
        across=across,
        down=down,
        column_major=layout.column_major,
    )


def piece_bytes(
    layout: RecordLayout, data: bytes, first: int, count: int, i: int
) -> bytes:
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
        layout = RecordLayout(params)
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
                        piece_bytes(layout, data, at + length, count, i),
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
        layout = RecordLayout(params)
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
        return RecordLayout(params).size

    def cell_tiles(self, params: dict[str, Any]) -> tuple[int, int]:
        # A piece may be any rectangle, so there is no one answer; the frame
        # renderer asks each subsprite instead.
        return (1, 1)

    def frames(
        self, cells: list[Cell], params: dict[str, Any], ctx: PipelineContext
    ) -> list[Frame]:
        layout = RecordLayout(params)
        pair = frame_pair(layout, ctx)
        out: list[Frame] = []
        unsized = 0
        for frame in _group(cells, layout.header is not None):
            # _record lays the cell's own fields back into its bytes, so the
            # piece read from them is the cell's, edits included
            records = [_record(layout, cell) for cell in frame]
            unsized += sum(layout.unsized(r) for r in records)
            out.append(tuple(subsprite(layout, r, pair) for r in records))
        warn_unsized(ctx, unsized, layout, SPRITE_RECORD_ENGINE)
        return out

    def size_pair(self, params: dict[str, Any]) -> tuple[int, int]:
        """The pair a lone size bit picks between: the preset's stated
        ``size_pair``, or the default where the entry's spins set it."""
        return RecordLayout(params).size_pair

    def has_palette_rows(self, params: dict[str, Any]) -> bool:
        return RecordLayout(params).has_rows

    def index_limit(self, params: dict[str, Any]) -> int | None:
        bits = RecordLayout(params).index_bits
        return (1 << bits) - 1 if bits else None

    def palette_row_limit(self, params: dict[str, Any]) -> int:
        bits = RecordLayout(params).row_bits
        return (1 << bits) - 1 if bits else 0

    def transform_cell(
        self, cell: Cell, op: CellOp, params: dict[str, Any]
    ) -> Cell | None:
        """Both mirrors where the record has the bits, and no turn."""
        layout = RecordLayout(params)
        return mirror(cell, op, {p for _n, _a, _w, _s, pl in layout.fields for p in pl})
