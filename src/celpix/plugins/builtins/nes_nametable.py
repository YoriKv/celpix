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
format publishes its pages. Unlike a paged format's, it is published **over**
whatever a container left on the context rather than behind it: this width is
what :meth:`~NesNametableFormat.encode` packs the plane at, so it is not a hint
to be improved on (:func:`_publish_geometry`).

**Four cells share one stored row**, which no other format here does. The cells
still each carry the row they are drawn in — :class:`~celpix.core.tilemap.Cell`
is unchanged and the renderer asks nothing — but only one of the four can be
written. That is what
:meth:`~celpix.plugins.base.TilemapCodecPlugin.palette_row_granularity` exists to
say, and the host writes whole quadrants because this says ``(2, 2)``
(``docs/design/tilemap-entry.md`` §4). Should cells reach :meth:`encode`
disagreeing, the **quadrant's top-left cell wins** — the same rule
:meth:`~celpix.core.document.Document.snapped_palette_rows` applies. It is
restated here because a codec is handed a flat buffer and no document, and it has
to answer for itself; but the host settles the list before handing it over
(:attr:`~celpix.core.document.Document.settled_cells`), so in practice this pick
never sees a quadrant that disagrees. Two statements of one rule, only one of
which can be exercised, which is what keeps them from drifting apart.

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
Those bits are real bytes in the file, and they belong to the **page** rather
than to any cell — which is why they are not in
:attr:`~celpix.core.tilemap.Cell.flags`, whose contract is bits of *that cell's
own record* (``docs/design/tilemap-entry.md`` §6, "Attribute bit 0 is carried,
never recomputed"). A cell travels: eyedrop the carrier and stamp it somewhere
else and its bits would land on a block that has no room for them, over live
colour fields, while the position they came from would save as zero. Both
failures are silent, because the model agrees with itself either way.

So they are preserved **by position**: :meth:`~NesNametableFormat.decode` leaves
each page's attribute plane on the context and
:meth:`~NesNametableFormat.encode` puts back, out of that plane, exactly the bits
no cell of the page reaches — whatever the cells above them have become. An
encode against a context that never read this page has nothing to put back, the
same way one has no byte order to honour there
(:data:`~celpix.core.context.KEY_TILEMAP_ENDIAN`); the save path passes the
document's own (:func:`~celpix.pipeline.pipeline.encode_cells`).
"""

from __future__ import annotations

from celpix.core import ceil_div
from celpix.core.context import (
    KEY_TILEMAP_COLUMNS,
    KEY_TILEMAP_PAGE_ROWS,
    KEY_TILEMAP_PAGES_ACROSS,
    PipelineContext,
)
from celpix.core.tilemap import Cell, CellOp
from celpix.plugins.formats import FormatInfo

NES_NAMETABLE_FORMAT = "format.tilemap.nes-nametable"

# tuple[bytes, ...]: each decoded page's attribute plane, as the file had it.
# Where decode leaves the bits encode has to put back positionally, since the
# cells cannot carry them (module docstring). Namespaced to this format and kept
# here rather than in :mod:`celpix.core.context`, because it is not a hint a
# container states about a file for whoever is interested — it is one half of one
# codec remembering what its other half read, and nothing else produces or reads
# it.
KEY_NES_ATTRIBUTE_PLANES = "tilemap.nes-nametable.attribute-planes"

# The console's numbers, in the order each follows from the one above it. The PPU
# reads one attribute byte per 4x4-tile block and splits it into four 2x2-tile
# quadrants of two bits each; a page is 32 cells across and 30 down, which is why
# it is 960 bytes of index and not 1024.
_BLOCK = 4
_QUADRANT = 2
_ROW_MASK = 0b11
_COLUMNS = 32
_ROWS = 30


# Derived rather than written out, so the arithmetic that produces them is the
# documentation: the attribute plane's size *is* a function of the cell grid, and
# a bare `8` and `64` here would be two numbers to check against a rule stated
# nowhere. The plane covers 32 rows where the page has 30, which is the loose end
# `_spare_mask` exists for.
_CELLS = _COLUMNS * _ROWS  # 960
_ATTR_STRIDE = ceil_div(_COLUMNS, _BLOCK)  # 8
_ATTR_BYTES = _ATTR_STRIDE * ceil_div(_ROWS, _BLOCK)  # 64
_PAGE = _CELLS + _ATTR_BYTES  # 1024


def _attr_at(x: int, y: int) -> tuple[int, int]:
    """``(byte index, shift)`` of the field colouring the cell at ``x, y``."""
    at = (y // _BLOCK) * _ATTR_STRIDE + (x // _BLOCK)
    shift = (2 if x & _QUADRANT else 0) + (4 if y & _QUADRANT else 0)
    return at, shift


def _spare_mask(x: int, y: int) -> int:
    """Which bits of the attribute byte covering the block at ``x, y`` reach no cell.

    Non-zero only where the page's rows stop inside the 4x4 block the byte
    covers — the last attribute row of a 30-row page, whose upper half addresses
    rows 30 and 31. Those are bytes in the file, so something has to put them
    back; the alternative is a save that clears bits it never asked about.

    Stated as a mask rather than as the bits themselves because it is what
    :meth:`~NesNametableFormat.encode` needs: the guarantee that what it ORs in
    cannot be a field some cell of the page owns is this mask, and it holds
    whatever the plane it draws from turns out to hold.
    """
    spare = 0
    for dy in (0, _QUADRANT):
        for dx in (0, _QUADRANT):
            if x + dx < _COLUMNS and y + dy < _ROWS:
                continue
            shift = (2 if dx else 0) + (4 if dy else 0)
            spare |= _ROW_MASK << shift
    return spare


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

        The planes are left on ``ctx`` as they were read, which is how the bits
        no cell reaches survive a save (module docstring). Set on every decode
        rather than only where something is there to keep, so a re-read replaces
        what the read before it left instead of writing an older page's bits into
        a newer one.
        """
        cells: list[Cell] = []
        planes: list[bytes] = []
        for page in range(len(data) // _PAGE):
            base = page * _PAGE
            attrs = data[base + _CELLS : base + _PAGE]
            planes.append(attrs)
            for at in range(_CELLS):
                x, y = at % _COLUMNS, at // _COLUMNS
                byte, shift = _attr_at(x, y)
                cells.append(
                    Cell(
                        index=data[base + at],
                        palette_row=(attrs[byte] >> shift) & _ROW_MASK,
                    )
                )
        ctx.set(KEY_NES_ATTRIBUTE_PLANES, tuple(planes))
        _publish_geometry(len(cells), ctx)
        return cells

    def encode(self, cells: list[Cell], ctx: PipelineContext) -> bytes:
        """Cells back to whole pages — indices, then the plane they colour.

        The quadrant's **top-left cell** decides its two bits. Cells inside one
        could disagree — nothing in a flat buffer stops them — and picking is the
        same answer :meth:`~...tilemap_codec.TilemapCodec.encode` gives a too-wide
        index: refusing would make a file unsaveable over one cell, where picking
        loses what the format never had room for and saves the rest. In practice
        the host settles the groups on both sides of the edit, the way in and the
        way out (:attr:`~celpix.core.document.Document.settled_cells`), so this is
        a backstop that never fires rather than the usual path — which is exactly
        what stops the two statements of the rule mattering separately.

        The bits no cell reaches come back from the plane ``ctx`` was left,
        **masked to the positions that have them** — so a page whose cells have
        been rearranged, overwritten or pasted over still writes those bytes as
        the file had them, and nothing can put a stray bit on top of a colour
        field some cell does own.
        """
        planes = tuple(ctx.get(KEY_NES_ATTRIBUTE_PLANES) or ())
        out = bytearray()
        for page in range(ceil_div(len(cells), _CELLS)):
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
            # The bits no quadrant of this page reaches, put back from the plane
            # this page was read as. The mask is what makes the OR safe — it
            # leaves only the fields that address rows the page does not have, so
            # the two never overlap however the cells have been edited. Keyed by
            # the block's position and not by a cell, because that is whose bits
            # they are.
            spare = planes[page] if page < len(planes) else b""
            for y in range(0, _ROWS, _BLOCK):
                for x in range(0, _COLUMNS, _BLOCK):
                    byte, _ = _attr_at(x, y)
                    if byte < len(spare):
                        attrs[byte] |= spare[byte] & _spare_mask(x, y)
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
    than failing — with one difference, and it is what makes this the one
    geometry a container may not override.

    Everywhere else the width is **advisory**, and a header is the better
    authority precisely because a wrong width only lays the same bytes out badly:
    a packed cell holds its own attributes wherever it sits, so the picture
    shears and the file still saves byte for byte. Here the width is how the
    *attribute plane is addressed*. It decides which quadrant of which byte a
    palette row lands in, and :meth:`~NesNametableFormat.encode` packs that plane
    at :data:`_COLUMNS` and nothing else — so a width from anywhere else is not a
    better answer to the same question, it is a different question. Left standing
    it puts the host's row groups
    (:meth:`~celpix.core.document.Document.palette_row_group`) on a grid the
    codec does not write, and the edit on screen stops being what a reload shows.

    So this is stated rather than offered, and on every whole page rather than
    only at the page counts a preset lists: a nametable read at any width but its
    own is not a nametable. A variant that genuinely carried its own geometry —
    the arbitrary-size nametable one authoring tool wrote (module docstring) —
    would have to be read *and written* at that width before publishing it could
    be right, which is a codec that consults the context, not a publication that
    steps aside for one.

    **The geometry is stated whole**, all three keys of it, and the third is why:
    a container that framed these bytes for its *own* format may have published
    an assembly, and taking the width and the page height over while leaving that
    standing is a page height from here beside an arrangement from somewhere
    else. A screen file is the live case — 0x2000 of payload is eight whole
    nametable pages, and its container states 2 across — so picking this format
    for one would pin the picture to a shape nothing in these bytes claims, with
    the spin locked against saying otherwise.
    """
    if not cells or cells % _CELLS:
        return
    ctx.set(KEY_TILEMAP_COLUMNS, _COLUMNS)
    ctx.set(KEY_TILEMAP_PAGE_ROWS, _ROWS)
    # And **no** assembly, which is this format's answer rather than its silence:
    # how a run of pages goes together is the cartridge's mirroring, not anything
    # in the bytes, so the arrangement stays the user's. Zero is what every reader
    # already takes for "none stated"
    # (:data:`~celpix.core.context.KEY_TILEMAP_PAGES_ACROSS`), so saying it needs
    # no way to unsay a key — which the bag deliberately has not got.
    ctx.set(KEY_TILEMAP_PAGES_ACROSS, 0)
