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
- ``index_addressing`` — ``"corner"`` (default) or ``"ordinal"``, whether the
  index names the unit it draws by its top-left element or counts the units.
  Read off the preset by the host, for every engine alike
  (:class:`~celpix.core.tilemap.IndexAddressing`); the cell holds the field as
  stored either way.
- ``page_columns`` / ``page_rows`` / ``page_counts`` — the page geometry a
  *format* fixes, for hardware that cuts a map into fixed screens and lays the
  pieces out into one picture. Stated together or not at all, and only applied
  when the file holds one of ``page_counts`` whole pages, so a run of cells the
  hardware never produced keeps a width the user owns
  (:meth:`TilemapCodec.decode`).
- ``side_fields`` / ``side_cells`` / ``side_key`` — bits of the cell kept in a
  **side array** outside it, bound as the ``side_array`` input: one side word's
  layout, how many cells a word covers, and whether a cell's word is found by
  its ``"position"`` (default) or by its ``"index"`` — a per-metatile attribute
  table. Explained under ``# -- the side array`` below.
- ``lead_codes`` / ``lead_bias`` / ``operands`` / ``line_bytes`` — **text of
  one- and two-byte codes**: lead bytes that join the byte after them into one
  cell, the commands whose operand bytes are never read as leads, and
  fixed-length records that are one line each. Only on a one-byte, index-only
  cell, and the one place a cell is not a fixed number of bytes; explained in
  :mod:`~celpix.plugins.builtins._lead_codes`.

Encoding is deliberately lossy in exactly one direction: a value too wide for
its field is masked rather than raising. A cell can arrive from a format with a
10-bit index on its way into one with 8, and refusing the write would make the
document unsaveable over a single cell; masking loses that cell's high bits and
saves the rest. Nothing else is dropped silently — a field the format has is
always round-tripped.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from celpix.core.context import (
    KEY_INPUTS,
    KEY_TILEMAP_COLUMNS,
    KEY_TILEMAP_PAGE_ROWS,
    PipelineContext,
)
from celpix.core.errors import Stage
from celpix.core.tilemap import Cell, CellOp
from celpix.plugins._params import byte_order, int_list, integer
from celpix.plugins.base import InputKind, InputSpec, PluginInfo
from celpix.plugins.builtins._cell_answers import mirror
from celpix.plugins.builtins._cell_layout import (
    _BOOL_ATTRS,
    _CELL_ATTR,
    _CELL_FIELDS,
    SIDE_FIELDS,
    SIDE_INPUT,
    _by_index,
    _cell_bytes,
    _field,
    _get,
    _layout,
    _lead_scheme,
    _placements,
    _side_cells,
    _side_masks,
    _side_only,
    _side_words,
)
from celpix.plugins.builtins._fields import limit
from celpix.plugins.builtins._mask import gather, scatter

TILEMAP_ENGINE = "codec.tilemap.packed"


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
    columns = integer(params, "page_columns", 0)
    rows = integer(params, "page_rows", 0)
    counts = int_list(params, "page_counts")
    if columns <= 0 or rows <= 0 or ctx.get(KEY_TILEMAP_PAGE_ROWS):
        return
    per_page = columns * rows
    if cells % per_page or cells // per_page not in counts:
        return
    # Both halves or neither: a page with no stated width has no shape, and
    # ``Document.page_size`` reads the width off the map width above.
    if not ctx.get(KEY_TILEMAP_COLUMNS):
        ctx.set(KEY_TILEMAP_COLUMNS, columns)
    ctx.set(KEY_TILEMAP_PAGE_ROWS, rows)


class TilemapCodec:
    info = PluginInfo(
        id=TILEMAP_ENGINE,
        name="Packed tilemap cell",
        stage=Stage.INTERPRET_TILEMAP,
        inputs=(
            InputSpec(
                SIDE_INPUT,
                "Side array",
                InputKind.REGION,
                required=False,
                unit="byte",
                # Only a preset that says where the side bits go has a use for
                # the bytes; every other packed format keeps the Inputs window
                # empty.
                when_param=SIDE_FIELDS,
                tooltip=(
                    "Array holding the bits a cell keeps elsewhere.\n"
                    "By position: one word per side_cells cells,\n"
                    "in cell order. By index: word index // side_cells,\n"
                    "a per-tile or per-metatile attribute table.\n"
                    "Read-only; edits are never written back."
                ),
            ),
        ),
    )

    def decode(
        self, data: bytes, params: dict[str, Any], ctx: PipelineContext
    ) -> list[Cell]:
        if (lead := _lead_scheme(params)) is not None:
            return lead.decode(data)  # cells of one or two bytes each
        size = _cell_bytes(params)
        order = byte_order(params, "endian", "little")
        fields = _layout(params)
        side = _side_words(params, ctx.get(KEY_INPUTS))
        per_word = _side_cells(params) if side else 1
        # Keyed by index, the side word is found from the cell's own word: the
        # index has no side bits (refused by _by_index), so it is whole there.
        keyed = _by_index(params)
        above = size * 8
        cells: list[Cell] = []
        # A trailing partial cell is dropped rather than zero-padded: unlike a
        # partial *tile*, which still draws as something, half a cell has no
        # meaningful index at all and would render as a spurious tile 0.
        for at in range(0, len(data) - size + 1, size):
            word = int.from_bytes(data[at : at + size], order)
            if side:
                key = _get(word, fields["index"]) if keyed else at // size
                unit = key // per_word
                if unit < len(side):
                    word |= side[unit] << above
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
        # The stamp and records a table offers the maps drawn through it are
        # read off the preset by the host, for every engine alike
        # (:mod:`celpix.pipeline.table_layout`).
        return cells

    def encode(
        self, cells: list[Cell], params: dict[str, Any], ctx: PipelineContext
    ) -> bytes:
        if (lead := _lead_scheme(params)) is not None:
            return lead.encode(cells)
        size = _cell_bytes(params)
        order = byte_order(params, "endian", "little")
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
        above = size * 8
        own = (1 << above) - 1
        masks = _side_masks(params)
        side = _side_words(params, ctx.get(KEY_INPUTS)) if masks else ()
        per_word = _side_cells(params) if masks else 1
        keyed = bool(masks) and _by_index(params)
        # The side bits any field places, so a side word's unplaced bits — the
        # replicated quadrants of an attribute byte — are not read as a claim.
        placed = 0
        for name in masks:
            placed |= sum(fields[name][0]) >> above  # type: ignore[index]
        out = bytearray()
        for at, cell in enumerate(cells):
            word = 0
            for field, read in present:
                # Masked, not checked: see the module docstring on why a too-wide
                # value costs its high bits rather than the whole save.
                word |= scatter(read(cell), *field)
            if masks:
                # The index as it will be stored, so the word it selects is the
                # one a reload of these bytes reads.
                unit = (_get(word, fields["index"]) if keyed else at) // per_word
                stored = side[unit] if unit < len(side) else 0
                if (word >> above) != stored & placed:
                    raise ValueError(
                        f"cell {at}: its {', '.join(sorted(masks))} come from the "
                        "side array, a read-only input - change those bytes instead"
                    )
            out += (word & own).to_bytes(size, order)
        return bytes(out)

    def settle_cells(
        self,
        cells: list[Cell],
        params: dict[str, Any],
        inputs: Mapping[str, Any] | None = None,
    ) -> list[Cell]:
        """``cells`` with every side-array bit back in step with the array.

        Only a preset with ``side_fields`` has anything to settle: those bits are
        the bound array's, an input that is never written, so a cell pasted or
        painted in with another palette row would draw in one row and save as
        the other. Re-derived here, after every edit, the way the indirect-record
        engine re-derives a grouped row — so what is on screen is what a reload
        gives back. Bits of a field that live in the cell's own word (an index's
        low bits beside a side-byte bank bit) are left as the user set them.

        Keyed by index (``side_key``), the word is the one the cell's *current*
        index selects, so a metatile moved or retyped takes its colour from the
        table the way the hardware does.

        Cheap on the common case: one lookup per distinct side word, a compare per
        side field, and the same list back when nothing differs.
        """
        masks = _side_masks(params)
        if not masks:
            return cells
        fields = _placements(params)
        side = _side_words(params, inputs)
        per_word = _side_cells(params)
        above = _cell_bytes(params) * 8
        # The index as encode will store it — masked to its field, as a value
        # too wide for it is — so the word picked here is the one a save and a
        # reload pick.
        keyed = _by_index(params)
        index_mask = limit(fields.get("index")) or 0
        wanted: dict[int, dict[str, int]] = {}
        out: list[Cell] | None = None
        for at, cell in enumerate(cells):
            unit = ((cell.index & index_mask) if keyed else at) // per_word
            stored = side[unit] if unit < len(side) else 0
            want = wanted.get(stored)
            if want is None:
                want = {name: gather(stored << above, *fields[name]) for name in masks}
                wanted[stored] = want
            changes: dict[str, int | bool] = {}
            for name, mask in masks.items():
                attr = _CELL_ATTR[name]
                have = int(getattr(cell, attr))
                settled = (have & ~mask) | want[name]
                if settled != have:
                    changes[attr] = bool(settled) if attr in _BOOL_ATTRS else settled
            if changes:
                if out is None:
                    out = list(cells)
                out[at] = replace(cell, **changes)
        return cells if out is None else out

    def bytes_per_cell(self, params: dict[str, Any]) -> int:
        return _cell_bytes(params)

    def cell_widths(
        self, cells: list[Cell], params: dict[str, Any]
    ) -> list[int] | None:
        """Each cell's bytes where lead codes make them differ, else None."""
        lead = _lead_scheme(params)
        return None if lead is None else lead.widths(cells)

    def line_bytes(self, params: dict[str, Any]) -> int:
        """The record a ``line_bytes`` name table stores each line in, else 0."""
        lead = _lead_scheme(params)
        return 0 if lead is None else lead.line_bytes

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
        A two-byte letter is past the one-byte field, so lead codes answer with
        the last of those instead.
        """
        if (lead := _lead_scheme(params)) is not None:
            return lead.top
        return limit(_field(params, "index"))

    def palette_row_limit(self, params: dict[str, Any]) -> int | None:
        """How high a cell's palette row can go — the ``palette`` field's width.

        :meth:`index_limit` for the colour field, off the same table: three bits
        on a console BG entry, so rows 0-7, and nothing at all on a format that
        places no ``palette`` — or keeps it whole in the side array, where the
        row is the array's and there is no field an assigned row could land in.
        """
        if "palette" in _side_only(params):
            return None
        return limit(_field(params, "palette"))

    def has_line_flag(self, params: dict[str, Any]) -> bool:
        """Whether the format ends a line on a **bit** rather than on a code.

        :meth:`has_palette_rows` for the terminator: the layout already says, so
        the answer comes off the one table rather than a second that could
        disagree. The alphabet has to know before a newline can be typed into
        such a stream (:attr:`~celpix.core.font.FontAlphabet.flag_break`), and it
        has to know from the *format*, since this is the stream's punctuation
        and not the font's (``docs/design/fontmap-entry.md`` §4).

        ``line_bytes`` records answer yes too: their line end is not a bit but a
        record boundary, and a newline typed there costs no cell either.
        """
        if (lead := _lead_scheme(params)) is not None:
            return bool(lead.line_bytes)
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

    def cell_fields(self, params: dict[str, Any]) -> dict[str, int]:
        """Every field the preset places, named as the Cell spells it.

        The whole-table answer the per-field probes each give a row of, off the
        same table for the same reason — a preset cannot disagree with itself.
        Keys are :class:`Cell` attribute names via :data:`_CELL_ATTR`; values
        are each field's :func:`~celpix.plugins.builtins._fields.limit`.

        Lead codes answer with the index alone, up to the last two-byte letter.
        A ``line_bytes`` record's line end is where the record fills rather than
        a bit, so it gets no control: set by hand it could only be refused.
        """
        if (lead := _lead_scheme(params)) is not None:
            return {"index": lead.top}
        side_only = _side_only(params)
        return {
            _CELL_ATTR[name]: top
            for name, field in _layout(params).items()
            if (top := limit(field)) is not None and name not in side_only
        }

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
        if op.value in _side_only(params):
            return None  # the array's bit, which a save cannot write
        return mirror(cell, op, _placements(params))
