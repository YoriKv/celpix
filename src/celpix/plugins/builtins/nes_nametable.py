"""The NES nametable page: a plane of tile indices, then a plane of palette rows.

The first cell format celPix reads whose **colour is not in the cell**. Every
other tilemap here is a packed word — the tile number in the low bits, the
attributes above it — which is what :mod:`~celpix.plugins.builtins.tilemap_codec`
covers with a ``fields`` string and no code. The PPU splits the two instead: a
page is 960 bare index bytes followed by a 64-byte **attribute plane**, and one
two-bit field of that plane colours a **2x2 square of cells**.

Two consequences shape everything below.

**The plane is addressed in rows, so the codec has to know the width**, and it
is the one thing about a tilemap that is normally the view's to choose. A
nametable takes it back: the page is 32x30 and nothing else — a run of these
bytes read at any other width is not a nametable
(``docs/rom-mapping/console-nes.md`` §4) — so the geometry is stated here as the
console's own constant and published for the layout to use, the way a paged
format publishes its pages.

**Four cells share one stored row**, which no other format here does. The cells
still each carry the row they are drawn in — :class:`~celpix.core.tilemap.Cell`
is unchanged and the renderer asks nothing — but only one of the four can be
written. That is what
:meth:`~celpix.plugins.base.TilemapCodecPlugin.palette_row_granularity` exists to
say, and the host writes whole quadrants because this says ``(2, 2)``
(``docs/design/tilemap-entry.md`` §4). Where cells still reach :meth:`encode`
disagreeing — a paste carries the rows it was cut with — the **quadrant's
top-left cell wins**, which is the same rule
:meth:`~celpix.core.document.Document.snapped_palette_rows` applies on the way
in, stated in two places because they must not drift.

**Nothing here is a parameter, which is why this is a *format* and not an
engine.** Every number below is the console's, and a page shaped differently
would be a different format rather than this one configured. An engine exists to
be parameterised — a preset supplies the numbers and a second preset supplies
different ones — and there is no second set of numbers here, so the two-file
engine-plus-preset shape would be a config file with nothing in it and an example
teaching knobs that have one legal setting each. A
:class:`~celpix.plugins.formats.TilemapFormat` says the same thing in one class:
no ``params`` in any signature, and
:func:`~celpix.plugins.formats.adapt_format` puts it in the picker beside every
preset (``docs/design/plugin-system.md``).

The nametable of *arbitrary* size that one authoring tool wrote is not the
counter-example it looks like: it carries its geometry in its own trailer, so it
would arrive through a container and the pipeline context — never through a
preset — and would still not make this a parameterised engine.

**Whole pages only.** ``decode`` reads ``len(data) // 1024`` pages and ignores
any remainder, and ``encode`` writes whole pages back, so the two are exact
inverses over the only length this format has. A **960-byte** nametable saved
without its attribute plane is not this format at all — it is a bare index run,
which ``preset.tilemap.gb-bg`` already reads and writes byte for byte, and
reading it here would decode 960 cells that encode could only put back as 1024
bytes.

The one thing a page holds that the cells cannot: for a 30-row page the last
attribute row's upper half addresses tile rows 30 and 31, which do not exist.
Those bits are real bytes in the file, so they ride in the ``flags`` of the
attribute byte's own top-left cell and are written back untouched — the
:attr:`~celpix.core.tilemap.Cell.flags` contract exactly
(``docs/design/tilemap-entry.md`` §6, "Attribute bit 0 is carried, never
recomputed").
"""

from __future__ import annotations

from celpix.core.context import (
    KEY_TILEMAP_COLUMNS,
    KEY_TILEMAP_PAGE_ROWS,
    PipelineContext,
)
from celpix.core.tilemap import Cell, CellOp
from celpix.plugins.formats import FormatInfo

NES_NAMETABLE_FORMAT = "format.tilemap.nes-nametable"

# The console's numbers, in the order each follows from the one above it. The PPU
# reads one attribute byte per 4x4-tile block and splits it into four 2x2-tile
# quadrants of two bits each; a page is 32 cells across and 30 down, which is why
# it is 960 bytes of index and not 1024.
_BLOCK = 4
_QUADRANT = 2
_ROW_MASK = 0b11
_COLUMNS = 32
_ROWS = 30


def _ceil_div(value: int, by: int) -> int:
    return -(-value // by)


# Derived rather than written out, so the arithmetic that produces them is the
# documentation: the attribute plane's size *is* a function of the cell grid, and
# a bare `8` and `64` here would be two numbers to check against a rule stated
# nowhere. The plane covers 32 rows where the page has 30, which is the loose end
# `_spare_bits` exists for.
_CELLS = _COLUMNS * _ROWS  # 960
_ATTR_STRIDE = _ceil_div(_COLUMNS, _BLOCK)  # 8
_ATTR_BYTES = _ATTR_STRIDE * _ceil_div(_ROWS, _BLOCK)  # 64
_PAGE = _CELLS + _ATTR_BYTES  # 1024


def _attr_at(x: int, y: int) -> tuple[int, int]:
    """``(byte index, shift)`` of the field colouring the cell at ``x, y``."""
    at = (y // _BLOCK) * _ATTR_STRIDE + (x // _BLOCK)
    shift = (2 if x & _QUADRANT else 0) + (4 if y & _QUADRANT else 0)
    return at, shift


def _spare_bits(attrs: bytes, x: int, y: int) -> int:
    """The bits of ``x, y``'s attribute byte that no cell of the page reaches.

    Non-zero only on the byte's own top-left cell, and only where the page's rows
    stop inside the 4x4 block it covers — the last attribute row of a 30-row
    page, whose upper half addresses rows 30 and 31. Those are bytes in the file,
    so something has to carry them; the alternative is a save that clears bits it
    never asked about.
    """
    if x % _BLOCK or y % _BLOCK:
        return 0
    byte, _ = _attr_at(x, y)
    spare = 0
    for dy in (0, _QUADRANT):
        for dx in (0, _QUADRANT):
            if x + dx < _COLUMNS and y + dy < _ROWS:
                continue
            shift = (2 if dx else 0) + (4 if dy else 0)
            spare |= _ROW_MASK << shift
    return attrs[byte] & spare


class NesNametableFormat:
    """The page, on the params-free :class:`~celpix.plugins.formats.TilemapFormat`
    surface — see the module docstring for why it is a format and not an engine."""

    info = FormatInfo(
        id=NES_NAMETABLE_FORMAT,
        name="NES nametable page (960 + 64 attributes)",
        category="Nintendo",
    )

    def decode(self, data: bytes, ctx: PipelineContext) -> list[Cell]:
        """Whole pages to cells, each carrying the row its quadrant states.

        A page's two planes are read together because that is the only way either
        of them means anything: the indices alone are a grey picture, and the
        attribute plane alone is 64 bytes of nothing.
        """
        cells: list[Cell] = []
        for page in range(len(data) // _PAGE):
            base = page * _PAGE
            attrs = data[base + _CELLS : base + _PAGE]
            for at in range(_CELLS):
                x, y = at % _COLUMNS, at // _COLUMNS
                byte, shift = _attr_at(x, y)
                cells.append(
                    Cell(
                        index=data[base + at],
                        palette_row=(attrs[byte] >> shift) & _ROW_MASK,
                        # The bits of this cell's attribute byte that no cell of
                        # the page reaches — the last row's upper half. Kept on
                        # the byte's own top-left cell, so there is exactly one
                        # carrier and encode knows which.
                        flags=_spare_bits(attrs, x, y),
                    )
                )
        _publish_geometry(len(cells), ctx)
        return cells

    def encode(self, cells: list[Cell], ctx: PipelineContext) -> bytes:
        """Cells back to whole pages — indices, then the plane they colour.

        The quadrant's **top-left cell** decides its two bits. Cells inside one
        can disagree (a paste brings the rows it was cut with), and picking is
        the same answer :meth:`~...tilemap_codec.TilemapCodec.encode` gives a
        too-wide index: refusing would make a file unsaveable over one cell,
        where picking loses what the format never had room for and saves the
        rest. The host settles the same groups the same way before the edit
        lands, so this is the backstop rather than the usual path.
        """
        out = bytearray()
        for page in range(_ceil_div(len(cells), _CELLS)):
            window = cells[page * _CELLS : (page + 1) * _CELLS]
            plane = bytearray(_CELLS)
            attrs = bytearray(_ATTR_BYTES)
            for at, cell in enumerate(window):
                plane[at] = cell.index & 0xFF
            for y in range(0, _ROWS, _QUADRANT):
                for x in range(0, _COLUMNS, _QUADRANT):
                    at = y * _COLUMNS + x
                    if at >= len(window):
                        continue
                    byte, shift = _attr_at(x, y)
                    attrs[byte] |= (window[at].palette_row & _ROW_MASK) << shift
            # The bits no quadrant of this page reaches, put back from wherever
            # decode parked them. OR'd in after the modelled fields so a carried
            # bit can never land on top of a row the user set.
            for y in range(0, _ROWS, _BLOCK):
                for x in range(0, _COLUMNS, _BLOCK):
                    at = y * _COLUMNS + x
                    if at < len(window):
                        byte, _ = _attr_at(x, y)
                        attrs[byte] |= window[at].flags & 0xFF
            out += plane
            out += attrs
        return bytes(out)

    def bytes_per_cell(self) -> int:
        """One byte — the index plane's stride, which is what a cell is stored in.

        Exact for the single page this format almost always is. Across a
        **multi-page** file the mapping drifts by the 64 attribute bytes each page
        ends with, so the hex dump's cell-to-byte arrow is off by that much from
        page two onwards. Stated here rather than worked around: the number is
        what the host slices byte windows with, and there is no second one that
        is right for both jobs.
        """
        return 1

    def cell_tiles(self) -> tuple[int, int]:
        return (1, 1)

    def index_limit(self) -> int:
        """255 — the whole byte, since the index plane is nothing else.

        Which half of the 8 KB CHR the number lands in is the *bank's* answer,
        set by the binding's base tile, and not a bit of this file.
        """
        return 0xFF

    def has_palette_rows(self) -> bool:
        return True

    def palette_row_limit(self) -> int:
        return _ROW_MASK

    def palette_row_granularity(self) -> tuple[int, int]:
        """``(2, 2)`` — one two-bit field colours a 2x2 square of cells."""
        return (_QUADRANT, _QUADRANT)

    def transform_cell(self, cell: Cell, op: CellOp) -> Cell | None:
        """Nothing: a nametable byte is a tile number and has no room for a bit.

        The PPU mirrors *sprites*, through OAM, and a background tile not at all
        — flipped background art is drawn as more tiles. So every transform is
        refused rather than set in the model and dropped again by :meth:`encode`.
        """
        return None


def _publish_geometry(cells: int, ctx: PipelineContext) -> None:
    """State the width and page height this format fixes, for the layout to use.

    The same claim :func:`~...tilemap_codec._publish_pages` makes and for the same
    reason — a page read at the wrong width shears into diagonal stripes rather
    than failing — with one difference: here the width is not only how the
    picture is laid out but how the *attribute plane is addressed*, so the group
    a palette-row assignment covers is resolved against it
    (:meth:`~celpix.core.document.Document.palette_row_group`). A nametable read
    at any width but its own is not a nametable, so this is claimed on every
    whole page rather than only at the page counts a preset lists.

    The container still wins where it spoke, by construction: it has already run.
    """
    if not cells or cells % _CELLS:
        return
    if not ctx.get(KEY_TILEMAP_COLUMNS):
        ctx.set(KEY_TILEMAP_COLUMNS, _COLUMNS)
    if not ctx.get(KEY_TILEMAP_PAGE_ROWS):
        ctx.set(KEY_TILEMAP_PAGE_ROWS, _ROWS)
