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

- ``fields`` — the cell's **bit layout**: one letter per bit, most significant
  first, the way the format's own notes draw it. The SNES entry above is
  ``vhop ppii iiii iiii``; whitespace groups it for counting and means nothing.
  Read by :mod:`~celpix.plugins.builtins._fields`, which is also where the
  letters and a preset's own ``legend`` are explained.

  - ``i`` the tile number, or the coordinate an ``indirect`` map names
  - ``p`` palette row
  - ``o`` priority — carried, never rendered: celPix has no layers
  - ``h`` / ``v`` horizontal and vertical flip
  - ``d`` drawn, set where the position IS drawn (see :data:`_FIELDS`)
  - ``e`` ends the line, for the text formats described below
  - ``f`` bits the format has and celPix has no meaning for, carried through
    untouched so a write stays byte-exact (:class:`Cell`)
  - ``.`` a bit no field claims, and one a write leaves clear

  A letter the layout never uses is a field the format does not have: it decodes
  as zero and is dropped on encode, which is how a plain index-only map
  (``iiii iiii``) is described.

  **A field need not be contiguous.** Hardware that grew a tile number past the
  room left for it parks the extra bits wherever there was space: a Game Boy
  Color map entry holds bits 0-7 of the index in its first byte and bit 8 alone
  up in the attribute byte, and the WonderSwan does the same with bit 9. Written
  ``ovh. ippp iiii iiii`` the two runs of ``i`` are one field, in the order they
  are read — the same split the colour masks take, through the same kernel
  (:mod:`~celpix.plugins.builtins._mask`).

  ``e`` is for a **text** format that ends a line by setting a bit on its last
  character rather than by spending a code on a terminator
  (``docs/graphics-formats-reference/text-formats.md`` §4.4). It is the only
  field here that changes no pixel: it lands on
  :attr:`~celpix.core.tilemap.Cell.ends_line`, which the fontmap reading turns
  into a newline. Placing it is also what takes the bit **out of the index**, so
  ``eiii iiii`` is a one-byte text run whose last character draws the letter the
  hardware draws rather than a tile past the end of the sheet.
- ``bytes`` — cell width in bytes. Optional: the layout already states it, and a
  preset giving both is cross-checked.
- ``endian`` — ``"little"`` or ``"big"`` (default little). Per *format*, not per
  family: SCR and PNL come from one authoring tool and disagree. It orders the
  cell's **bytes in the file**; the layout is always the word itself, high bit
  first, so the two are independent.
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

from collections.abc import Callable
from typing import Any

from celpix.core.context import (
    KEY_TILEMAP_COLUMNS,
    KEY_TILEMAP_PAGE_ROWS,
    PipelineContext,
)
from celpix.core.errors import Stage
from celpix.core.tilemap import Cell, CellOp
from celpix.plugins.base import PluginInfo
from celpix.plugins.builtins._fields import (
    Field,
    bit_width,
    parse_layout,
    resolve_legend,
)
from celpix.plugins.builtins._mask import gather, scatter

TILEMAP_ENGINE = "codec.tilemap.packed"

# Where each Cell attribute is read from and written to. Named once so decode
# and encode cannot drift apart, and so an unknown key in a preset is inert
# rather than half-applied.
# ``drawn`` is set where the position IS drawn, which is how the one format that
# has it stores the bit; a preset placing no ``drawn`` describes a format whose
# every position is drawn, and every other format in hand is that.
_FIELDS = (
    "index",
    "palette",
    "priority",
    "flip_h",
    "flip_v",
    "drawn",
    "terminator",
    "flags",
)

# The transforms this engine can express, and the bit each one lives in. The op's
# own name *is* the preset field, which is what lets "does this format support
# it" be answered by looking the field up rather than by a second table that
# could disagree with the first.
_MIRRORS = {
    CellOp.FLIP_H: Cell.flipped_h,
    CellOp.FLIP_V: Cell.flipped_v,
}


# One field's placement in the word — the chunk masks and (shift, width) pairs
# ``_mask.gather`` / ``_mask.scatter`` take (:mod:`~celpix.plugins.builtins._fields`).
_Field = Field

# Which letter of a cell layout names which field. Overridable per preset, so a
# layout can keep the mnemonics of the note it was copied from.
_LEGEND = {
    "i": "index",
    "p": "palette",
    "o": "priority",
    "h": "flip_h",
    "v": "flip_v",
    "d": "drawn",
    "e": "terminator",
    "f": "flags",
}


def _placements(params: dict[str, Any]) -> dict[str, _Field] | None:
    """Everything the preset's ``fields`` layout places, or None where it has none."""
    text = params.get("fields")
    if not isinstance(text, str):
        return None
    legend = resolve_legend(_LEGEND, params.get("legend"), frozenset(_FIELDS))
    return parse_layout(text, legend, _cell_bytes(params) * 8)


def _field(params: dict[str, Any], name: str) -> _Field | None:
    """Field ``name``'s chunks, or None when the format lacks it."""
    placed = _placements(params)
    if placed is not None:
        return placed.get(name)
    spec = params.get(name)
    if not spec:
        return None
    chunks: list[int] = []
    sw: list[tuple[int, int]] = []
    seen = 0
    for part in spec if isinstance(spec, (list, tuple)) else (spec,):
        shift = int(part.get("shift", 0))
        bits = int(part.get("bits", 0))
        if bits <= 0:
            continue
        mask = ((1 << bits) - 1) << shift
        if mask & seen:
            raise ValueError(f"tilemap field {name!r}: chunks overlap")
        seen |= mask
        chunks.append(mask)
        sw.append((shift, bits))
    if not chunks:
        return None
    return tuple(chunks), tuple(sw)


def _limit(field: _Field | None) -> int | None:
    """The highest value a field can hold — all its chunks' bits set."""
    if field is None:
        return None
    return (1 << sum(width for _, width in field[1])) - 1


def _layout(params: dict[str, Any]) -> dict[str, _Field | None]:
    return {name: _field(params, name) for name in _FIELDS}


def _cell_bytes(params: dict[str, Any]) -> int:
    """How wide one cell is — the layout's own answer where it has one.

    ``bytes`` stays accepted so a preset can state the width plainly, but the
    layout is what decides it: a cell whose two statements disagree is a preset
    to fix rather than one to guess at.
    """
    stated = None if params.get("bytes") is None else int(params["bytes"])
    text = params.get("fields")
    if isinstance(text, str):
        width = bit_width(text)
        if width % 8:
            raise ValueError(
                f"a cell has to be a whole number of bytes, and the layout "
                f"describes {width} bits"
            )
        size = width // 8
        if stated is not None and stated != size:
            raise ValueError(
                f"the layout describes a {size}-byte cell and bytes says {stated}"
            )
    else:
        size = stated if stated is not None else 2
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


def _get(word: int, field: _Field | None) -> int:
    return gather(word, *field) if field else 0


# Every field a cell can state, with what reads it off one — the writer's half of
# :meth:`TilemapCodec.decode`, in the order the word is assembled. A table rather
# than eight lines of code so the encode loop can be built from the fields a
# format declares instead of asking after all of them per cell.
_CELL_FIELDS: tuple[tuple[str, Callable[[Cell], int]], ...] = (
    ("index", lambda cell: cell.index),
    ("palette", lambda cell: cell.palette_row),
    ("priority", lambda cell: cell.priority),
    ("flip_h", lambda cell: int(cell.flip_h)),
    ("flip_v", lambda cell: int(cell.flip_v)),
    ("drawn", lambda cell: int(cell.visible)),
    ("terminator", lambda cell: int(cell.ends_line)),
    ("flags", lambda cell: cell.flags),
)


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
                    # Absent field reads 0, which for every other Cell attribute
                    # is the right default and for this one is its opposite.
                    visible=fields["drawn"] is None
                    or bool(_get(word, fields["drawn"])),
                    ends_line=bool(_get(word, fields["terminator"])),
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
        # The fields this format actually has, paired with what reads each off a
        # cell. Settled once rather than probed per cell: a text cell is one byte
        # carrying an index and nothing else, so seven of the eight probes below
        # would be a call into a function whose whole answer is "no such field" —
        # and this runs over every cell of the map on every committed edit.
        present = [
            (field, read)
            for name, read in _CELL_FIELDS
            if (field := fields[name]) is not None
        ]
        out = bytearray()
        for cell in cells:
            word = 0
            for field, read in present:
                # Masked, not checked: see the module docstring on why a too-wide
                # value costs its high bits rather than the whole save.
                word |= scatter(read(cell), *field)
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
        return _limit(_field(params, "index"))

    def palette_row_limit(self, params: dict[str, Any]) -> int | None:
        """How high a cell's palette row can go — the ``palette`` field's width.

        :meth:`index_limit` for the colour field, off the same table: three bits
        on a console BG entry, so rows 0-7, and nothing at all on a format that
        places no ``palette``.
        """
        return _limit(_field(params, "palette"))

    def has_line_flag(self, params: dict[str, Any]) -> bool:
        """Whether the format ends a line on a **bit** rather than on a code.

        :meth:`has_palette_rows` for the terminator: the layout already says, so
        the answer comes off the one table rather than a second that could
        disagree. The alphabet has to know before a newline can be typed into
        such a stream (:attr:`~celpix.core.font.FontAlphabet.flag_break`), and it
        has to know from the *format*, since this is the stream's punctuation
        and not the font's (``docs/design/fontmap-entry.md`` §4).
        """
        return _field(params, "terminator") is not None

    def has_palette_rows(self, params: dict[str, Any]) -> bool:
        """Whether the preset places a palette field — the field table answers.

        The same trick :meth:`transform_cell` uses, and for the same reason: a
        preset that declares no ``palette`` describes a format with nowhere to
        put a row, so the answer falls out of the one table rather than a second
        one that could disagree with it. An index-only map (a Game Boy BG entry,
        a converted screen) is exactly a preset without the field.
        """
        return _field(params, "palette") is not None

    def has_visibility(self, params: dict[str, Any]) -> bool:
        """Whether the preset places a ``drawn`` bit — the field table answers.

        :meth:`has_palette_rows` for the visibility flag, off the same table: a
        preset placing no ``drawn`` describes a format whose every position is
        drawn, and clearing a cell there has no "nothing here" to write.
        """
        return _field(params, "drawn") is not None

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
