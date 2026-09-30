"""The S-CG-CAD tilemap members: screen, panel, stamp layout, converted screen.

SCR, PNL and MAP are what the authoring tool reopens; STD is what its converter
made out of a screen. All four hand a tilemap payload on to an ordinary cell
codec, and differ in where their header sits, how their cells are sized and
what shape the payload is assembled into (the package docstring,
``scgcad-formats.md`` §2-§4, §10).
"""

from __future__ import annotations

from celpix.core.address import format_hex
from celpix.core.context import (
    KEY_TILEMAP_CELL_TILES,
    KEY_TILEMAP_COLUMNS,
    KEY_TILEMAP_PAGE_ROWS,
    KEY_TILEMAP_PAGES_ACROSS,
    KEY_TILEMAP_PALETTE_ROW_BASE,
    KEY_TILEMAP_STAMP_CELLS,
    PipelineContext,
)
from celpix.core.errors import Stage
from celpix.plugins.base import (
    ContainerField,
    PluginInfo,
    ReadSource,
    WriteTarget,
    format_size,
    splice,
)

from ._common import (
    _DEPTH_BPP,
    HEADER,
    SIGNATURE,
    _blank,
    _metadata_fields,
    _payload,
    _row_base,
)

SCR_SIZE = 0x2300
SCR_PAYLOAD = 0x2000  # four 32x32 screens of 0x800 each
SCR_HEADER_AT = 0x2000

PNL_SIZE = 0x10100
PNL_TABLE = 0x8000  # tile table; an equal-sized registration table follows it

MAP_SIZE = 0x2100
MAP_PAYLOAD = 0x2000  # 0x1000 entries, laid out 64 wide

# A converted screen: the low byte of each of a screen's four quadrants, laid out
# 2x2 into one 64x64 grid of bare tile numbers. Headerless and fixed-size.
STD_SIZE = 0x1000
STD_COLUMNS = 64


# Where each member's header sits relative to its metadata block, and what the
# fields we read mean. Only the ones the view can act on are read; the rest of
# the block is carried through untouched (`scgcad-formats.md` §2, §3).
SCR_TILE_SIZE = 0x42  # screen cell size = 8 * (value + 1): 0 is 8x8, 1 is 16x16
# Read as the **writer** writes it, not as the tool's own loader reads it back.
# `save_scr` stores the editor's mode variable unmasked and the renderer switches
# on it (`case 1` halves both loop bounds), so 1 means 16x16 and nothing else
# does — but `load_scr` reads `header[0x42] & 2`, turning that 1 into a 0. That
# masking is a bug in the tool, and it is the reading anyone tracing the loader
# arrives at; against a byte the corpus only ever sets to 0 or 1 it would make
# every screen 8x8. The corpus says otherwise — the value-1 screens are 76%
# metatile-aligned against 30% for value-0 (`scgcad-formats.md` §2.3).

# A panel's header carries the same-looking byte at 0x62 and two exponents at
# 0x69/0x6A, and **none of the three is this file's cell size**. A panel word is
# always one 8x8 tile: a 16x16 unit is stored as four adjacent words, which is
# measurable — across the whole corpus, in panels flagged either way, the four
# tiles a metatile would expand to are already present as the four words of a 2x2
# group (`scgcad-formats.md` §3.1). Reading any of the three as a cell size draws
# the panel at four times its content.
#
# The pair at 0x69/0x6A is a real field all the same, and what it sizes is the
# **stamp**: how many panel cells one stamp-layout coordinate names
# (PNL_STAMP_EXPONENTS below). That is a fact about the panel's *callers*, not
# about its own cells, which is why it is published for a bound layout to read and
# never applied to the panel itself.
PNL_STAMP_EXPONENTS = (0x69, 0x6A)  # log2 of the stamp's width and height, in cells
# The tool's own size menu offers 1 to 32 cells, so the exponent it stores is 0-5.
# Anything wider is a corrupt byte and reads as no stamp at all: a panel divided
# into stamps bigger than itself has no division a layout could index.
PNL_STAMP_MAX_EXPONENT = 5

# The word at screen +0x47 / panel +0x67 is deliberately NOT read. It reads like
# a base tile index and is not one: it is non-zero in 83% of screens (most
# often 0x03EE), and adding it to every cell index sends the whole screen off the
# end of a 1024-tile bank. Neither candidate meaning survives the corpus — as a
# "clear character number" (the format's own term for a blank tile) it matches
# the screen's actual background tile 24% of the time, and its byte order is not
# even consistent between files (`scgcad-formats.md` §2, "Header fields"). The
# one independent implementation of these formats adds it to nothing either. A
# tile base is the user's to set, so the word is only reported.
SCR_BASE_WORD = 0x47

# Where a screen and a panel each state the palette base their cells count from.
# Header-relative: a screen's header sits at 0x2000, a panel's at 0.
SCR_DEPTH = 0x40  # 0 = 2bpp, 1 = 4bpp, 2 = 8bpp
SCR_COL_HALF, SCR_COL_CELL = 0x45, 0x46
PNL_COL_HALF, PNL_COL_CELL = 0x65, 0x66


PANEL_COLUMNS = 32  # a panel is 32 cells wide; its 0x4000 cells make 512 rows
# A stamp layout is 64 x 64 tile positions, which is a screen's shape and not a
# coincidence: a layout is *generated* from a screen, one entry per stamp of it.
# The editor's own renderer and its converter both index the entry table at row
# stride 64 (`scgcad-formats.md` §4), so the 0x1000 entries are 64 wide.
MAP_COLUMNS = 64
SCREEN_COLUMNS = 32  # one 32x32 quadrant — four of them make up a screen file
SCREEN_ROWS = 32  # and this is what makes each one a *page* rather than a band
# Two across, two down: a screen is one 64x64 tilemap in four quadrants,
# which is the editor's own `load_scr` (`scgcad-asset-pipeline.md` §2.7) and not
# an arrangement to be picked between.
SCREEN_PAGES_ACROSS = 2


def _cell_tiles(data: bytes, at: int) -> tuple[int, int]:
    """How many tiles one cell draws, from a ``8 * (value + 1)`` tile-size byte.

    Two values occur and only two: 0 is an 8x8 cell, 1 a 16x16 one, whose four
    tiles are ``N``, ``N+1``, ``N+0x10``, ``N+0x11`` — the console's own 16x16 BG
    arithmetic. Anything else is a corrupt byte and reads as 8x8: a file that
    unreasonable is better read small than not at all.
    """
    return (2, 2) if at < len(data) and data[at] == 1 else (1, 1)


class ScrContainer:
    """Screen file: payload first, 0x100 header at 0x2000, 0x200 clear codes.

    The four 0x800 quadrants are handed on as one buffer, with their shape and their
    **assembly** published (``KEY_TILEMAP_PAGE_ROWS``, ``KEY_TILEMAP_PAGES_ACROSS``).
    A screen is not four screens that might go together somehow: it is **one
    64x64 tilemap stored as four quadrants**, and the editor's own loader says so
    — ``load_scr`` writes the four quadrants into a single array at row stride 64,
    offsets 0, 0x80, 0x2000, 0x2080, which is top-left, top-right, bottom-left,
    bottom-right (``scgcad-asset-pipeline.md`` §2.7).

    Sized by its magic offset rather than an exact length, which is also what
    reads the rarer 0x4100 variant correctly: that file is this layout with a
    wider clear-code table, so the payload it hands on is the same 0x2000.
    """

    info = PluginInfo(
        id="container.scgcad-scr",
        name="S-CG-CAD screen (SCR)",
        stage=Stage.CONTAINER,
        extensions=(".scr",),
        magic=((SCR_HEADER_AT, SIGNATURE),),
        short_name="SCR",
        category="S-CG-CAD",
        preserves_offsets=True,
    )
    # Declaring this is what says "the payload is a tilemap, not pixels" — the
    # host reads it to set an opened file's content kind and to pick its first
    # cell codec (:func:`~celpix.plugins.detect.tilemap_preset_for`). A plain
    # attribute rather than a PluginInfo field so a third-party container can
    # do the same without the descriptor growing a case for it.
    default_tilemap_preset = "preset.tilemap.snes-bg"

    def read(self, source: ReadSource, ctx: PipelineContext) -> bytes:
        ctx.set(KEY_TILEMAP_COLUMNS, SCREEN_COLUMNS)
        # Four quadrants in one file, not one map four times as tall. The rows are
        # the page's; the columns above are its width.
        ctx.set(KEY_TILEMAP_PAGE_ROWS, SCREEN_ROWS)
        # ...and 2x2 is the format's own answer rather than a reading of it, so
        # the view is told outright instead of being left to offer a choice
        # nothing in the corpus supports (`scgcad-asset-pipeline.md` §2.7).
        ctx.set(KEY_TILEMAP_PAGES_ACROSS, SCREEN_PAGES_ACROSS)
        # A screen states its own cell size, and 949 of the 1,622 surveyed set
        # this byte. That they mean it is measurable: of the cells those screens
        # actually draw, 76.1% carry a metatile-aligned index against 30.0% for
        # the 8x8 ones — and 25% is what chance alone gives, so the 8x8 group is
        # flat noise and this one is not (`scgcad-formats.md` §2.3). Read small,
        # a 16x16 screen draws one quarter of every cell and drops the rest.
        header = source.data[SCR_HEADER_AT : SCR_HEADER_AT + HEADER]
        ctx.set(KEY_TILEMAP_CELL_TILES, _cell_tiles(header, SCR_TILE_SIZE))
        # A screen states its own depth, so the half is convertible to rows here.
        if len(header) > SCR_COL_CELL:
            bpp = _DEPTH_BPP.get(header[SCR_DEPTH] & 3, 4)
            ctx.set(
                KEY_TILEMAP_PALETTE_ROW_BASE,
                _row_base(header[SCR_COL_HALF], header[SCR_COL_CELL], bpp),
            )
        return _payload(source, ctx, 0, SCR_PAYLOAD)

    def write(self, data: bytes, dest: WriteTarget, ctx: PipelineContext) -> bytes:
        # Splicing over the existing file keeps the header and clear codes exactly
        # as they were read. A file that does not exist yet gets an all-0xFF clear
        # table — every cell visible, which is both what the tool itself writes on
        # import and the commonest state in the wild.
        existing = dest.existing or _blank(SCR_SIZE, SCR_HEADER_AT, tail=0xFF)
        return splice(existing, 0, data[:SCR_PAYLOAD])

    def describe(
        self, source: ReadSource, ctx: PipelineContext
    ) -> tuple[ContainerField, ...]:
        data = source.data
        header = data[SCR_HEADER_AT : SCR_HEADER_AT + HEADER]
        size = _cell_tiles(header, SCR_TILE_SIZE)
        raw_byte = header[SCR_TILE_SIZE] if SCR_TILE_SIZE < len(header) else 0
        base_word = (
            int.from_bytes(header[SCR_BASE_WORD : SCR_BASE_WORD + 2], "little")
            if len(header) > SCR_BASE_WORD + 1
            else 0
        )
        return (
            *_metadata_fields(data, SCR_HEADER_AT),
            ContainerField(
                "Payload",
                f"{format_size(SCR_PAYLOAD)} at 0x000000 - four 32x32 quadrants",
                "One 64x64 tilemap stored as four quadrants\n"
                "Passed on as one buffer with the 2x2 layout published,\n"
                "so the view assembles it as the editor did",
            ),
            ContainerField(
                "Cell size byte",
                f"0x{raw_byte:02X} at {format_hex(SCR_HEADER_AT + SCR_TILE_SIZE, 4)}"
                f" - {size[0]}x{size[1]} tiles per cell",
                "8 * (value + 1) pixels; published as the view's cell size\n"
                "Read as 8x8, a 16x16 screen draws one quarter of each cell",
            ),
            ContainerField(
                "Base tile word",
                f"0x{base_word:04X} at {format_hex(SCR_HEADER_AT + SCR_BASE_WORD, 4)}"
                " - not applied",
                "Reads like a base tile index and is not one\n"
                "Added to every cell it sends the screen off the bank\n"
                "Not applied; the tile base is set by the user",
            ),
            ContainerField(
                "Clear codes",
                f"{format_size(max(0, len(data) - SCR_HEADER_AT - HEADER))}"
                f" at {format_hex(SCR_HEADER_AT + HEADER, 4)}, preserved",
                "Per-cell draw/don't-draw table set by the artist\n"
                "Outside the payload: every cell is drawn here,\n"
                "and the table is preserved on save",
            ),
        )


def _stamp_cells(header: bytes) -> tuple[int, int]:
    """A panel's stamp size in cells, from its two header exponents.

    ``(1, 1)`` — no stamping — for a header that does not have them or states one
    too wide to mean anything, which is the reading that changes nothing: a
    layout bound to such a panel resolves one coordinate to one cell, as it did
    before the pair was read at all.
    """
    size = []
    for at in PNL_STAMP_EXPONENTS:
        exponent = header[at] if at < len(header) else 0
        size.append(1 << exponent if exponent <= PNL_STAMP_MAX_EXPONENT else 1)
    return size[0], size[1]


class PnlContainer:
    """Panel file: 0x100 header, 0x8000 tile table, 0x8000 registration table.

    Only the tile table is the payload. The second table is the panel allocator's
    bookkeeping — bit 15 of each word is the "this cell belongs to a registered
    panel" flag the tool sets as it hands those rectangles out — and it doubles
    as the editor's own draw test: a cell whose bit is clear renders as
    background, in the panel view and through a stamp layout alike
    (``scgcad-formats.md`` §3.2).
    That it marks only 8% of populated cells is the point rather than a puzzle,
    the rest of the 16,384-cell grid being unregistered scratch.

    It still rides through untouched, not read into ``Cell.visible``: it sits
    outside the payload the cell codec is handed, and keeping it true across an
    edit would mean re-running the allocator. So a
    panel here draws cells the tool would have left blank; what a save writes is
    what it read.

    What the container *does* read is the stamp size at 0x69/0x6A, which is not
    about this file's own cells at all — it is how many of them one stamp of a
    bound layout covers (:data:`~celpix.core.context.KEY_TILEMAP_STAMP_CELLS`).
    """

    info = PluginInfo(
        id="container.scgcad-pnl",
        name="S-CG-CAD panel (PNL)",
        stage=Stage.CONTAINER,
        extensions=(".pnl",),
        magic=((0, SIGNATURE),),
        # A panel and a stamp layout carry the same signature at the same offset,
        # so length is the only thing that tells them apart.
        exact_size=PNL_SIZE,
        short_name="PNL",
        category="S-CG-CAD",
        preserves_offsets=True,
    )
    default_tilemap_preset = "preset.tilemap.scgcad-panel"

    def read(self, source: ReadSource, ctx: PipelineContext) -> bytes:
        ctx.set(KEY_TILEMAP_COLUMNS, PANEL_COLUMNS)
        # No cell size published, on purpose: a panel word is one 8x8 tile in
        # every file of the corpus, whatever its header bytes say. See the note
        # beside SCR_TILE_SIZE for the three candidates and why none is read.
        #
        # The stamp size is a different claim and does get published: it says
        # nothing about how *this* file is drawn, only how a layout bound to it
        # carves it up, so it cannot draw the panel at four times its content the
        # way reading one of those three as a cell size would.
        ctx.set(KEY_TILEMAP_STAMP_CELLS, _stamp_cells(source.data[:HEADER]))
        # The palette base is read, and it matters more here than anywhere else:
        # a panel states no depth, so its colour *half* could not be converted
        # into rows — but it does not have to be, because `col_half` is 0 in all
        # 1,080 surveyed panels and only `col_cell` is ever non-zero. The 4bpp
        # rows-per-half passed below is therefore unreachable in this corpus, and
        # passed anyway so a panel that ever does set the half is not silently
        # wrong by a whole half of CGRAM.
        data = source.data
        if len(data) > PNL_COL_CELL:
            ctx.set(
                KEY_TILEMAP_PALETTE_ROW_BASE,
                _row_base(data[PNL_COL_HALF], data[PNL_COL_CELL], 4),
            )
        return _payload(source, ctx, HEADER, PNL_TABLE)

    def write(self, data: bytes, dest: WriteTarget, ctx: PipelineContext) -> bytes:
        existing = dest.existing or _blank(PNL_SIZE)
        return splice(existing, HEADER, data[:PNL_TABLE])

    def describe(
        self, source: ReadSource, ctx: PipelineContext
    ) -> tuple[ContainerField, ...]:
        data = source.data
        across, down = _stamp_cells(data[:HEADER])
        return (
            *_metadata_fields(data, 0),
            ContainerField(
                "Tile table",
                f"{format_size(PNL_TABLE)} at {format_hex(HEADER, 4)} - the payload",
                f"0x{PNL_TABLE // 2:X} cells, {PANEL_COLUMNS} wide; "
                "each word is one 8x8 tile\n"
                "A 16x16 unit is four adjacent words, not one bigger cell",
            ),
            ContainerField(
                "Registration table",
                f"{format_size(PNL_TABLE)} at "
                f"{format_hex(HEADER + PNL_TABLE, 4)}, preserved",
                "Bit 15 marks a cell registered to a panel,\n"
                "which is the tool's own draw test\n"
                "celPix draws every cell; written back untouched",
            ),
            ContainerField(
                "Stamp size",
                f"{across}x{down} cells at "
                f"{PNL_STAMP_EXPONENTS[0]:#04x}/{PNL_STAMP_EXPONENTS[1]:#04x}",
                "Cells one stamp covers, as two exponents\n"
                "Published for a stamp layout bound to this panel\n"
                "Not this file's cell size, which is one 8x8 tile",
            ),
            ContainerField(
                "Cell size byte",
                f"0x{data[0x62]:02X} at {format_hex(0x62, 4)} - not read"
                if len(data) > 0x62
                else "absent",
                "Shaped like a cell size and not one: read as one,\n"
                "the panel draws at four times its content\n"
                "A panel word is always a single 8x8 tile",
            ),
        )


class MapContainer:
    """Stamp layout: 0x100 header, then 0x1000 entries naming panel cells.

    A MAP holds no tiles and no attributes of its own — each entry is a
    coordinate into a panel, and resolving one needs that panel
    (``scgcad-formats.md`` §4). The container's job stops at cutting the entry
    table out; the two-level resolution belongs to the tile binding.

    The 0x1000 entries are **64 x 64 tile positions**, a screen's shape, because a
    layout is generated from a screen one stamp at a time. How many cells a stamp
    covers comes from the panel rather than from here, and it is why most of these
    entries are not read: at the commonest 2x2 the tool writes one entry in four
    and leaves the other three at whatever the file already held.
    """

    info = PluginInfo(
        id="container.scgcad-map",
        name="S-CG-CAD stamp layout (MAP)",
        stage=Stage.CONTAINER,
        extensions=(".map",),
        magic=((0, SIGNATURE),),
        exact_size=MAP_SIZE,
        short_name="MAP",
        category="S-CG-CAD",
        preserves_offsets=True,
    )
    # A stamp layout's entry word is a panel coordinate, not a tile reference,
    # so it reads through its own preset — and resolving one into tiles needs
    # the panel it was authored against (`tilemap-entry.md` §6).
    default_tilemap_preset = "preset.tilemap.scgcad-map"

    def read(self, source: ReadSource, ctx: PipelineContext) -> bytes:
        ctx.set(KEY_TILEMAP_COLUMNS, MAP_COLUMNS)
        return _payload(source, ctx, HEADER, MAP_PAYLOAD)

    def write(self, data: bytes, dest: WriteTarget, ctx: PipelineContext) -> bytes:
        existing = dest.existing or _blank(MAP_SIZE)
        return splice(existing, HEADER, data[:MAP_PAYLOAD])

    def describe(
        self, source: ReadSource, ctx: PipelineContext
    ) -> tuple[ContainerField, ...]:
        return (
            *_metadata_fields(source.data, 0),
            ContainerField(
                "Entry table",
                f"{format_size(MAP_PAYLOAD)} at {format_hex(HEADER, 4)} - the payload",
                f"0x{MAP_PAYLOAD // 2:X} entries, {MAP_COLUMNS} wide - "
                "a screen's shape\n"
                "The width is fixed by the format and published",
            ),
            ContainerField(
                "Entry meaning",
                "a panel coordinate, not a tile",
                "A stamp layout holds no tiles or attributes of its own\n"
                "Entries resolve through the tile binding's panel",
            ),
            ContainerField(
                "Stamp size",
                "the panel's, not this file's",
                "One entry names a stamp of cells; how many comes\n"
                "from the panel this layout is bound to\n"
                "Unbound, an entry reads as the single cell it names",
            ),
        )


class StdContainer:
    """Converted screen: 0x1000 bytes, 64x64 bare tile numbers, no header.

    Not something the authoring tool writes — a 1992 converter made it out of a
    screen, keeping the **low byte** of each cell and dropping the high one, then
    laid the screen's four quadrants out 2x2 (``scgcad-formats.md`` §10). So it
    holds a screen's shape with its attributes gone, which is why it reads
    through a plain index-only cell rather than the screen's.

    A container for a headerless file, because the two things celPix would
    otherwise have to be told — that this is a tilemap and that it is 64 wide —
    are both fixed by the format, and neither is guessable from 4 KiB of bytes.
    """

    info = PluginInfo(
        id="container.scgcad-std",
        name="Converted screen (STD)",
        stage=Stage.CONTAINER,
        extensions=(".std",),
        exact_size=STD_SIZE,
        short_name="STD",
        category="S-CG-CAD",
        preserves_offsets=True,
    )
    default_tilemap_preset = "preset.tilemap.scgcad-std"

    def read(self, source: ReadSource, ctx: PipelineContext) -> bytes:
        ctx.set(KEY_TILEMAP_COLUMNS, STD_COLUMNS)
        return _payload(source, ctx, 0, STD_SIZE)

    def write(self, data: bytes, dest: WriteTarget, ctx: PipelineContext) -> bytes:
        # The whole file is the payload, so there is nothing around it to keep —
        # but splicing over what is there still leaves a short write's tail
        # alone rather than truncating the file to it.
        existing = dest.existing or bytes(STD_SIZE)
        return splice(existing, 0, data[:STD_SIZE])

    def describe(
        self, source: ReadSource, ctx: PipelineContext
    ) -> tuple[ContainerField, ...]:
        return (
            ContainerField(
                "Signature",
                "none - identified by length and suffix",
                "Headerless and fixed-size\n"
                "Made from a screen by a converter that wrote no marker",
            ),
            ContainerField(
                "Payload",
                f"{format_size(STD_SIZE)} - the whole file, {STD_COLUMNS} wide",
                "Bare tile numbers, four screen quadrants laid out 2x2\n"
                "The width is fixed by the format and published",
            ),
            ContainerField(
                "Cell contents",
                "low byte only - attributes dropped",
                "The converter kept the low byte of each screen cell\n"
                "No palette rows or flip bits remain;\n"
                "reads through an index-only cell",
            ),
        )
