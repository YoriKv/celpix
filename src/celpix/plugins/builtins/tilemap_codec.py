"""The generic tilemap cell codec: a packed word of bit fields, both directions.

Nearly every hardware tilemap cell is one little- or big-endian integer with the
tile number in the low bits and a few attribute bits above it. The SNES BG entry
is ``vhopppcc tttttttt``; a Game Boy map entry is a bare byte with no attributes
at all; the panel format shares the SNES field layout and stores its words the
other way round
(``docs/graphics-formats-reference/scgcad-formats.md`` §1). One parameterised
engine covers all of them, so a new tilemap format is a preset rather than code
— the same data-first tier the planar pixel engine provides
(``docs/design/plugin-system.md``).

Params:

- ``bytes`` — cell width in bytes (default 2).
- ``endian`` — ``"little"`` or ``"big"`` (default little). Per *format*, not per
  family: SCR and PNL come from one authoring tool and disagree.
- ``index`` / ``palette`` / ``priority`` / ``flip_h`` / ``flip_v`` — each an
  ``{ shift, bits }`` table naming where that field sits in the word. A field
  omitted from the preset does not exist in the format: it decodes as zero and
  is dropped on encode, which is how a plain index-only map is described.
- ``flags`` — the same, for bits the format has and celPix has no meaning for.
  Carried through untouched so a write stays byte-exact; see :class:`Cell`.
- ``cell_tiles`` — ``[across, down]``, how many tiles one cell covers
  (default ``[1, 1]``; a panel cell is ``[2, 2]``).
- ``page_columns`` / ``page_rows`` / ``page_counts`` — the page geometry a
  *format* fixes, for hardware that cuts a map into fixed screens and lays the
  pieces out into one picture. Stated together or not at all, and only applied
  when the file holds one of ``page_counts`` whole pages, so a run of cells the
  hardware never produced keeps a width the user owns
  (:meth:`TilemapCodec.decode`).

Encoding is deliberately lossy in exactly one direction: a value too wide for
its field is masked rather than raising. A cell can arrive from a format with a
10-bit index on its way into one with 8, and refusing the write would make the
document unsaveable over a single cell; masking loses that cell's high bits and
saves the rest. Nothing else is dropped silently — a field the format has is
always round-tripped.
"""

from __future__ import annotations

from typing import Any

from celpix.core.context import (
    KEY_TILEMAP_COLUMNS,
    KEY_TILEMAP_PAGE_ROWS,
    PipelineContext,
)
from celpix.core.errors import Stage
from celpix.core.tilemap import Cell, CellOp
from celpix.plugins.base import PluginInfo

TILEMAP_ENGINE = "codec.tilemap-packed"

# Where each Cell attribute is read from and written to. Named once so decode
# and encode cannot drift apart, and so an unknown key in a preset is inert
# rather than half-applied.
_FIELDS = ("index", "palette", "priority", "flip_h", "flip_v", "flags")

# The transforms this engine can express, and the bit each one lives in. The op's
# own name *is* the preset field, which is what lets "does this format support
# it" be answered by looking the field up rather than by a second table that
# could disagree with the first.
_MIRRORS = {
    CellOp.FLIP_H: Cell.flipped_h,
    CellOp.FLIP_V: Cell.flipped_v,
}


def _field(params: dict[str, Any], name: str) -> tuple[int, int] | None:
    """``(shift, mask)`` for field ``name``, or None when the format lacks it."""
    spec = params.get(name)
    if not spec:
        return None
    shift = int(spec.get("shift", 0))
    bits = int(spec.get("bits", 0))
    if bits <= 0:
        return None
    return shift, (1 << bits) - 1


def _layout(params: dict[str, Any]) -> dict[str, tuple[int, int] | None]:
    return {name: _field(params, name) for name in _FIELDS}


def _cell_bytes(params: dict[str, Any]) -> int:
    size = int(params.get("bytes", 2))
    if size < 1:
        raise ValueError(f"cell size must be at least one byte, got {size}")
    return size


def _publish_pages(cells: int, params: dict[str, Any], ctx: PipelineContext) -> None:
    """State the page geometry the format fixes, where this file has that shape.

    The pixel side's ``bitmap_width`` problem one level up: a format can cut a map
    into fixed screens that only mean anything assembled, and read back to back
    those screens stack in a column no hardware ever drew. A *container* states
    the shape when it has a header to read it from
    (:data:`~celpix.core.context.KEY_TILEMAP_PAGE_ROWS`); this is the same claim
    made by the **entry format** instead, which is what covers a bare payload —
    a tilemap lifted out of a ROM, or a screen file whose header was stripped —
    since those carry no container to speak for them.

    Only claimed at a page count the format actually comes in (``page_counts``).
    The geometry drives a *locked* width, so claiming one wrongly is worse than
    claiming none: a cell run some game laid out its own way would be pinned to a
    shape it has not got, where saying nothing leaves the width the user's.

    The container wins where it spoke, and by construction rather than by
    precedence — a header is the better authority, and it has already run.
    """
    columns = int(params.get("page_columns", 0) or 0)
    rows = int(params.get("page_rows", 0) or 0)
    counts = params.get("page_counts") or ()
    if columns <= 0 or rows <= 0 or ctx.get(KEY_TILEMAP_PAGE_ROWS):
        return
    per_page = columns * rows
    if cells % per_page or cells // per_page not in {int(n) for n in counts}:
        return
    # Both halves or neither: a page with no stated width has no shape, and
    # ``Document.page_size`` reads the width off the map width above.
    if not ctx.get(KEY_TILEMAP_COLUMNS):
        ctx.set(KEY_TILEMAP_COLUMNS, columns)
    ctx.set(KEY_TILEMAP_PAGE_ROWS, rows)


def _endian(params: dict[str, Any]) -> str:
    order = str(params.get("endian", "little"))
    if order not in ("little", "big"):
        raise ValueError(f"endian must be 'little' or 'big', got {order!r}")
    return order


def _get(word: int, field: tuple[int, int] | None) -> int:
    shift, mask = field if field else (0, 0)
    return (word >> shift) & mask if field else 0


def _put(word: int, field: tuple[int, int] | None, value: int) -> int:
    if field is None:
        return word
    shift, mask = field
    # Masked, not checked: see the module docstring on why a too-wide value
    # costs its high bits rather than the whole save.
    return word | ((value & mask) << shift)


class TilemapCodec:
    info = PluginInfo(
        id=TILEMAP_ENGINE,
        name="Packed tilemap cell",
        stage=Stage.INTERPRET_TILEMAP,
    )

    def decode(
        self, data: bytes, params: dict[str, Any], ctx: PipelineContext
    ) -> list[Cell]:
        size = _cell_bytes(params)
        order = _endian(params)
        fields = _layout(params)
        cells: list[Cell] = []
        # A trailing partial cell is dropped rather than zero-padded: unlike a
        # partial *tile*, which still draws as something, half a cell has no
        # meaningful index at all and would render as a spurious tile 0.
        for at in range(0, len(data) - size + 1, size):
            word = int.from_bytes(data[at : at + size], order)
            cells.append(
                Cell(
                    index=_get(word, fields["index"]),
                    palette_row=_get(word, fields["palette"]),
                    priority=_get(word, fields["priority"]),
                    flip_h=bool(_get(word, fields["flip_h"])),
                    flip_v=bool(_get(word, fields["flip_v"])),
                    flags=_get(word, fields["flags"]),
                )
            )
        _publish_pages(len(cells), params, ctx)
        return cells

    def encode(
        self, cells: list[Cell], params: dict[str, Any], ctx: PipelineContext
    ) -> bytes:
        size = _cell_bytes(params)
        order = _endian(params)
        fields = _layout(params)
        out = bytearray()
        for cell in cells:
            word = 0
            word = _put(word, fields["index"], cell.index)
            word = _put(word, fields["palette"], cell.palette_row)
            word = _put(word, fields["priority"], cell.priority)
            word = _put(word, fields["flip_h"], int(cell.flip_h))
            word = _put(word, fields["flip_v"], int(cell.flip_v))
            word = _put(word, fields["flags"], cell.flags)
            out += word.to_bytes(size, order)
        return bytes(out)

    def bytes_per_cell(self, params: dict[str, Any]) -> int:
        return _cell_bytes(params)

    def cell_tiles(self, params: dict[str, Any]) -> tuple[int, int]:
        across, down = params.get("cell_tiles", (1, 1))
        if int(across) < 1 or int(down) < 1:
            raise ValueError(f"cell_tiles must be positive, got {across}x{down}")
        return int(across), int(down)

    def index_limit(self, params: dict[str, Any]) -> int | None:
        """How high a cell reference can go — the ``index`` field's own width.

        The same trick :meth:`has_palette_rows` and :meth:`transform_cell` use: the
        field table already knows, so the answer comes out of the one place the
        layout is stated rather than a second that could disagree. A preset with no
        ``index`` describes a format whose cells reference nothing settable.
        """
        field = _field(params, "index")
        return field[1] if field else None  # the mask *is* the highest value

    def has_palette_rows(self, params: dict[str, Any]) -> bool:
        """Whether the preset places a palette field — the field table answers.

        The same trick :meth:`transform_cell` uses, and for the same reason: a
        preset that declares no ``palette`` describes a format with nowhere to
        put a row, so the answer falls out of the one table rather than a second
        one that could disagree with it. An index-only map (a Game Boy BG entry,
        a converted screen) is exactly a preset without the field.
        """
        return _field(params, "palette") is not None

    def transform_cell(
        self, cell: Cell, op: CellOp, params: dict[str, Any]
    ) -> Cell | None:
        """A mirror, when the preset gives the format a bit to say it with.

        The field table already answers this: a preset that declares no
        ``flip_h`` describes a format with nowhere to put one, so the flip is
        refused rather than set in the model and dropped again by
        :meth:`encode` — which is what a Game Boy map, or a stamp layout's
        coordinate word, would otherwise do to every flip the user pressed.

        Rotations are refused by every format this engine reads: none of them has
        a rotation bit, and a :class:`Cell` has no field for one to live in.
        """
        toggle = _MIRRORS.get(op)
        if toggle is None or _field(params, op.value) is None:
            return None
        return toggle(cell)
