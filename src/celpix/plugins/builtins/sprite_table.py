"""A sprite mapping table: word offsets naming frames that live in a region.

How most Mega Drive games hold an object's frames. The table is a run of offset
words, one per frame, each counted from the table's own first byte; a frame is a
count followed by that many pieces::

    table  = offset.w per frame, from the table's first byte
    frame  = header (holding the piece count), then count pieces
    piece  = a sprite record, as `codec.tilemap.sprite-record` states one

**The entry is the table**, one cell per offset word, so a save writes back
exactly the table. The frames arrive through the ``frames`` region input: the
bytes from the table's first byte onward, far enough to hold every frame it
names. That is structure rather than tidiness. Tables do not own their frames —
several objects' tables may point into one shared pool laid down after them —
and no table stores its length, so no slice could hold "the table and its
frames" without claiming bytes another entry owns
(``docs/design/sprite-map.md`` §3).

**The frame and its pieces are sprite-record's parameters, unchanged**:
``record``, ``frame_header`` (required here — a frame's count is what ends it),
``arrays``, ``column_major``, ``subsprite_tiles``, ``legend``, ``endian``. So a
Mega Drive dialect is a preset, and three ship: ``sega-mappings-5`` (a count
byte and five-byte pieces — Sonic 1's, Sonic CD's and Phantasy Star II's),
``sonic2-mappings`` (a count word and eight-byte pieces
carrying the two-player tile word) and ``sonic3k-mappings`` (a count word and
six-byte pieces). ``endian`` also orders the offset words.

The frames are every **distinct** offset, in table order — a table naming one
frame for several animation steps draws it once. A frame that runs outside the
region is kept as an empty frame rather than dropped, so frame *k* of the entry
is still the table's *k*-th distinct frame, and a notice says how many.

**Grid frames** (``grid_frames = true``) are Phantasy Star II's second frame
kind, the one its large battle enemies are drawn from: where the count byte is 0
or has bit 7 set, the frame is instead a grid of nametable words the game copies
into a plane::

    grid   = x.w  y.w  columns-1.b  rows-1.b  columns x rows cells
    cell   = a Mega Drive nametable word; 0 draws nothing

drawn as 8x8 pieces on that grid, its corner at ``x, y``. It needs the count to be the
frame's first byte, a ``u8``, and is off by default for a reason: in the Sonic 1
layout a count of 0 is an ordinary empty frame, and reading it as a grid would
invent pieces out of the next frame's bytes.

Sprite objects are drawn read-only by the canvas; what a save meets is the offset
words decode produced.
"""

from __future__ import annotations

from typing import Any, Literal

from celpix.core.context import KEY_INPUTS, PipelineContext
from celpix.core.errors import Stage
from celpix.core.notices import warn
from celpix.core.sprite import Frame, Subsprite
from celpix.core.tilemap import Cell
from celpix.plugins._params import flag
from celpix.plugins.base import InputKind, InputSpec, PluginInfo
from celpix.plugins.builtins.sprite_record import RecordLayout, piece_bytes, subsprite

SPRITE_TABLE_ENGINE = "codec.tilemap.sprite-table"
INPUT_FRAMES = "frames"

_Order = Literal["little", "big"]

OFFSET_BYTES = 2
GRID_HEADER = 6  # x.w y.w columns-1.b rows-1.b
_GRID_FLAG = 0x80

# The nametable word a grid cell holds: the Mega Drive's, whatever the record's
# own attribute field says, since the game copies it into a plane as it stands.
_INDEX = 0x7FF
_FLIP_H = 0x0800
_FLIP_V = 0x1000
_ROW_SHIFT = 13
_PRIORITY = 0x8000


class TableLayout:
    """The record layout, plus what the table adds to it."""

    def __init__(self, params: dict[str, Any]) -> None:
        self.record = RecordLayout(params)
        if self.record.header is None:
            raise ValueError(
                "a sprite table needs a `frame_header`: its count is what ends "
                "a frame, since the frames sit back to back in the region"
            )
        self.grid = flag(params, "grid_frames")
        if self.grid and self.record.header[1:] != (0, 1):
            raise ValueError(
                "`grid_frames` tells a grid by the frame's first byte, so the "
                "frame header's count must be a u8 at count_at = 0"
            )


def _word(data: bytes, at: int, order: _Order) -> int:
    return int.from_bytes(data[at : at + 2], order)


def _grid(region: bytes, at: int, order: _Order) -> Frame | None:
    if at + GRID_HEADER > len(region):
        return None
    x = int.from_bytes(region[at : at + 2], order, signed=True)
    y = int.from_bytes(region[at + 2 : at + 4], order, signed=True)
    columns, rows = region[at + 4] + 1, region[at + 5] + 1
    cells = at + GRID_HEADER
    if cells + columns * rows * 2 > len(region):
        return None
    out = []
    for r in range(rows):
        for c in range(columns):
            word = _word(region, cells + 2 * (r * columns + c), order)
            if word:
                out.append(
                    Subsprite(
                        x=x + 8 * c,
                        y=y + 8 * r,
                        index=word & _INDEX,
                        palette_row=(word >> _ROW_SHIFT) & 3,
                        priority=1 if word & _PRIORITY else 0,
                        flip_h=bool(word & _FLIP_H),
                        flip_v=bool(word & _FLIP_V),
                    )
                )
    return tuple(out)


def read_frame(table: TableLayout, region: bytes, at: int) -> Frame | None:
    """The frame at ``region[at]``, or ``None`` when it runs past the region."""
    layout = table.record
    length, count_at, count_width = layout.header
    if at + length > len(region):
        return None
    if table.grid and (region[at] == 0 or region[at] & _GRID_FLAG):
        return _grid(region, at, layout.order)
    count = int.from_bytes(
        region[at + count_at : at + count_at + count_width], layout.order
    )
    first = at + length
    if first + count * layout.size > len(region):
        return None
    return tuple(
        subsprite(layout, piece_bytes(layout, region, first, count, i))
        for i in range(count)
    )


def distinct(offsets: list[int]) -> list[int]:
    """``offsets`` with repeats dropped, in first-seen order."""
    return list(dict.fromkeys(offsets))


class SpriteTableCodec:
    """An offset table as the entry, its frames read out of a region input."""

    info = PluginInfo(
        id=SPRITE_TABLE_ENGINE,
        name="Sprite mapping table (offsets to frames in a region)",
        stage=Stage.INTERPRET_TILEMAP,
        inputs=(
            InputSpec(
                INPUT_FRAMES,
                "Frames",
                InputKind.REGION,
                tooltip=(
                    "The bytes from this table's first byte onward,\n"
                    "far enough to hold every frame it names:\n"
                    "the offsets count from the table's start."
                ),
            ),
        ),
    )

    def decode(
        self, data: bytes, params: dict[str, Any], ctx: PipelineContext
    ) -> list[Cell]:
        order = TableLayout(params).record.order
        return [
            Cell(index=0, flags=_word(data, at, order))
            for at in range(0, len(data) - 1, OFFSET_BYTES)
        ]

    def encode(
        self, cells: list[Cell], params: dict[str, Any], ctx: PipelineContext
    ) -> bytes:
        order = TableLayout(params).record.order
        return b"".join((c.flags & 0xFFFF).to_bytes(2, order) for c in cells)

    def bytes_per_cell(self, params: dict[str, Any]) -> int:
        return OFFSET_BYTES

    def cell_tiles(self, params: dict[str, Any]) -> tuple[int, int]:
        # A cell is an offset, not a tile; the frame renderer asks each piece.
        return (1, 1)

    def frames(
        self, cells: list[Cell], params: dict[str, Any], ctx: PipelineContext
    ) -> list[Frame]:
        table = TableLayout(params)
        region = (ctx.get(KEY_INPUTS) or {}).get(INPUT_FRAMES)
        if not isinstance(region, bytes) or not region:
            return []
        out: list[Frame] = []
        outside = 0
        for offset in distinct([c.flags for c in cells]):
            frame = read_frame(table, region, offset)
            if frame is None:
                outside += 1
                frame = ()
            out.append(frame)
        if outside:
            warn(
                ctx,
                f"{outside} of this table's frames run past its Frames region",
                "They draw empty. Lengthen the Frames region until it holds\n"
                "every frame the table names, or check that the table\n"
                "is not longer than the game's.",
                source=SPRITE_TABLE_ENGINE,
            )
        return out
