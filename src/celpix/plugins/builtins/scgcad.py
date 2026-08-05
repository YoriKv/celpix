"""S-CG-CAD containers — SCR/PNL/MAP/OBJ/OBZ references, CGX tiles, COL palettes.

One SNES-era authoring tool's file family, plus the two files its pipeline made
*out* of them (OBZ, STD). Each member is a fixed-size file: a payload, and — for
everything the tool itself reopens — a 0x100-byte metadata block carrying a
32-byte ASCII signature. Byte-exact specs, the confidence level behind each
claim, and the corpus they were verified against are in
``docs/graphics-formats-reference/scgcad-formats.md``.

What makes these containers rather than codecs is that each one **frames** its
payload: it has to be cut out of the file, and everything around it — header,
trailer, a panel's second table — has to come back unchanged on write. The
payload is then read by the ordinary codec for what it holds, which is why the
two cell byte orders in this family cost a preset each rather than a plugin each.

They do not all frame the same *kind* of thing, and the family is the reason a
container declares which it frames (``PluginInfo.content_kinds``): a COL is a
palette, a CGX is pixels, the other three are tilemaps. Without that, the palette
would be offered a screen's container and vice versa.

Four things a reader has to get right and would not guess:

- **SCR puts its header last.** The signature is at 0x2000, past the payload, so
  detection cannot assume offset 0. PNL and MAP put theirs first, and a sprite
  object puts it at whichever of two offsets says which size it is.
- **Two members carry no signature.** A transfer object and a converted screen
  were written to be *consumed* rather than reopened, so nothing in the bytes
  says what they are and detection has only a length and an extension.
- **The byte orders disagree.** An SCR cell is little-endian, the console's own
  order; PNL and MAP words are byte-swapped. One rule for the family decodes SCR
  into noise (``scgcad-formats.md`` §5.2).
- **A sprite object is not a grid.** Its records carry signed pixel offsets, so
  what the view draws is frames of freely placed subsprites rather than cells laid out
  in rows (:mod:`celpix.core.sprite`).

The trailing regions are preserved verbatim rather than regenerated, and they are
not padding. A screen's 0x200 trailer and a panel's 0x8000 second table are both
**visibility tables** — a per-cell "draw this / don't", which cannot be derived
from the tile data and would be destroyed by regenerating it. celPix has no
per-cell visibility to map them onto yet, so they ride through untouched
(``scgcad-formats.md`` §2.1, §3.2).
"""

from __future__ import annotations

from celpix.core.animation import read_sequences
from celpix.core.capabilities import ContentKind
from celpix.core.context import (
    KEY_PIXEL_PRESET,
    KEY_SOURCE_OFFSET,
    KEY_TILE_PALETTE_ROW_BASE,
    KEY_TILE_PALETTE_ROWS,
    KEY_TILEMAP_ANIMATIONS,
    KEY_TILEMAP_CELL_TILES,
    KEY_TILEMAP_COLUMNS,
    KEY_TILEMAP_ENDIAN,
    KEY_TILEMAP_PAGE_ROWS,
    KEY_TILEMAP_PAGES_ACROSS,
    KEY_TILEMAP_PALETTE_ROW_BASE,
    KEY_TILEMAP_STAMP_TILES,
    KEY_TILEMAP_SUBSPRITES_PER_FRAME,
    PipelineContext,
)
from celpix.core.errors import Stage
from celpix.plugins.base import (
    ContainerField,
    PluginInfo,
    ReadSource,
    WriteTarget,
    format_size,
    plain_read,
    splice,
)

# The 16 bytes every file in the family opens its metadata block with. The 16
# after it are a tool version and build date, identical across the whole surveyed
# corpus but matched loosely all the same: a version bump should not stop a file
# from opening, and the bytes are preserved on write either way.
SIGNATURE = b"NAK1989 S-CG-CAD"

HEADER = 0x100  # every member's metadata block is this long

SCR_SIZE = 0x2300
SCR_PAYLOAD = 0x2000  # four 32x32 screens of 0x800 each
SCR_HEADER_AT = 0x2000
SCR_SCREEN = 0x800

PNL_SIZE = 0x10100
PNL_TABLE = 0x8000  # tile table; an equal-sized registration table follows it

MAP_SIZE = 0x2100
MAP_PAYLOAD = 0x2000  # 0x1000 entries, laid out 64 wide
MAP_WIDTH = 64

COL_SIZE = 0x400
COL_PAYLOAD = 0x200  # 256 BGR555 entries; the metadata block follows them
COL_HEADER_AT = 0x200

# A sprite object's records, then its header: 32 frames of 64 six-byte subsprites
# in the ordinary form and **64 frames of 128** in the extended one. Which it is
# shows in where the signature sits, so these double as the detection offsets.
OBJ_PAYLOADS = (0x3000, 0xC000)
# How each form slots that payload. Not derivable from the size — 0xC000 is 64x128
# and 128x64 alike — so it is stated, and the pair is what the container publishes
# for the codec to frame by (``KEY_TILEMAP_SUBSPRITES_PER_FRAME``). The extended
# form's build converter declares `obj_data[64][128][6]` against the ordinary one's
# `[32][64][6]`, and the corpus agrees: over 301 extended objects the drawn records
# peak at slot 127 and fall away monotonically, with slot 63 at 6% of that — one
# period of 128, not two of 64 (``scgcad-formats.md`` §8.1).
OBJ_SUBSPRITES_PER_FRAME = {0x3000: 64, 0xC000: 128}
# One subsprite record. Named because the frame division is reported as well as
# read, and a hard-coded "64 records" divisor is what let the info panel go on
# describing an extended object as 128 frames of 64 after the codec stopped.
SUBSPRITE_RECORD = 6
# Each form's whole length, keyed by its payload: 0x100 of header and then the
# animation table, which is twice the size in the extended form for the twice the
# frames it names. The write side needs the pair, an object saved to a
# path with no file at it having to be *built* at the form its records are in —
# the ordinary one holds a quarter of an extended object's frames, so writing one
# into the other is not a shorter file but three quarters of the art gone.
OBJ_SIZES = {0x3000: 0x3500, 0xC000: 0xC900}
OBJ_SIZE = OBJ_SIZES[OBJ_PAYLOADS[0]]  # the ordinary form
# How many animation sequences each form holds, and how many steps each of those
# has room for. One group per 64 bytes of table, which is what the sizes above
# already say — stated rather than divided out, because the step count is the same
# 32 in both forms and only the group count grows with the frames
# (``scgcad-formats.md`` §8.3).
OBJ_SEQUENCES = {0x3000: 16, 0xC000: 32}
OBJ_SEQUENCE_STEPS = 32

# The transfer form of a sprite object: 64 frames of 64 subsprites, then a 0xA00 tail
# holding the animation table. The one member of the family that carries **no
# signature at all** — none of the 148 in the corpus has one anywhere — so its
# length and its extension are the whole of what identifies it.
OBZ_SIZE = 0x6A00
OBZ_PAYLOAD = 0x6000
# Its animation table is the object's read at a different shape: 16 sequences of
# 64 steps in the 0x800 that follows the records, then a 0x200 tail nothing has
# decoded (``scgcad-formats.md`` §9).
OBZ_SEQUENCES = 16
OBZ_SEQUENCE_STEPS = 64
OBZ_TABLE = (OBZ_SEQUENCES, OBZ_SEQUENCE_STEPS)  # the pair both readers take

# A converted screen: the low byte of each of a screen's four blocks, laid out
# 2x2 into one 64x64 grid of bare character numbers. Headerless and fixed-size.
STD_SIZE = 0x1000
STD_COLUMNS = 64
# How many frames the strip puts on a row to start with. A sprite object has no
# width of its own — its frames are separate pictures rather than one picture —
# so this is a legible default rather than the file's own answer.
OBJECT_COLUMNS = 8


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
# into blocks bigger than itself has no division a layout could index.
PNL_STAMP_MAX_EXPONENT = 5

# The word at screen +0x47 / panel +0x67 is deliberately NOT read. It reads like
# a base character index and is not one: it is non-zero in 83% of screens (most
# often 0x03EE), and adding it to every cell index sends the whole screen off the
# end of a 1024-tile bank. Neither candidate meaning survives the corpus — as a
# "clear character number" it matches the screen's actual background character
# 24% of the time, and its byte order is not even consistent between files
# (`scgcad-formats.md` §2, "Unresolved"). The one independent implementation of
# these formats adds it to nothing either. A tile base is the user's to set.

# Where a screen and a panel each state the palette base their cells count from.
# Header-relative: a screen's header sits at 0x2000, a panel's at 0.
SCR_DEPTH = 0x40  # 0 = 2bpp, 1 = 4bpp, 2 = 8bpp
SCR_COL_HALF, SCR_COL_CELL = 0x45, 0x46
PNL_COL_HALF, PNL_COL_CELL = 0x65, 0x66
CGX_COL_HALF, CGX_COL_CELL = 0x22, 0x23
# A bank states its depth in the same encoding a screen does, at its own offset,
# and it is the file's *second* statement of which of the three variants it is —
# the first being where the signature sits. All 1,744 surveyed banks agree with
# their own length, so either signal identifies the variant on its own
# (`scgcad-formats.md` §6).
CGX_DEPTH = 0x20

# The colour window a file was authored through: the half is 128 colours, and
# the cell picks a 4-colour group inside it — but only at 2bpp, where a row *is*
# four colours and the group is therefore one row. At 4bpp the window is the
# whole 128-colour half with no group to pick, and at 8bpp it is all 256 with no
# half either (`scgcad-formats.md` §3.3). The render counts in *rows*, so both
# divide through by n = 4 / 16 / 128 by depth.
_ROWS_PER_HALF = {2: 32, 4: 8}  # 128 colours // n
_ROWS_PER_CELL = {2: 1}  # a 4-colour group // n, and 2bpp only
# Shared by a screen's depth byte and a bank's: one encoding, two offsets.
_DEPTH_BPP = {0: 2, 1: 4, 2: 8}
_DEPTH_BYTE = {bpp: value for value, bpp in _DEPTH_BPP.items()}


def _row_base(col_half: int, col_cell: int, bpp: int) -> int:
    """The palette row this file's cells count their own row 0 from.

    **The cell is 2bpp-only**, which is the whole subtlety here and is not
    guessable from the field: at 4bpp it is stale editor state and applying it
    draws every cell one row out. The editor says so twice — its colour window is
    the whole 128-colour half at 4bpp, with no 4-colour group left to pick, and
    the cell control refuses to move outside 2bpp at all
    (`scgcad-formats.md` §3.3).

    The corpus agrees, and the check is independent of the code: a 4bpp bank with
    ``col_cell = 1`` states per-tile rows that only line up with the panels drawn
    against it when the cell is dropped — 806 of 808 against 0 of 808 — and
    fourteen banks otherwise claim a row past the end of CGRAM, which no reading
    of a real file should produce.
    """
    return (col_half & 1) * _ROWS_PER_HALF.get(bpp, 0) + (col_cell & 3) * (
        _ROWS_PER_CELL.get(bpp, 0)
    )


PANEL_COLUMNS = 32  # a panel is 32 cells wide; its 0x4000 cells make 512 rows
# A stamp layout is 64 x 64 tile positions, which is a screen's shape and not a
# coincidence: a layout is *generated* from a screen, one entry per block of it.
# The editor's own renderer and its converter both index the entry table at row
# stride 64 (`scgcad-formats.md` §4), so the 0x1000 entries are 64 wide.
MAP_COLUMNS = 64
SCREEN_COLUMNS = 32  # one 32x32 quadrant — four of them make up a screen file
SCREEN_ROWS = 32  # and this is what makes each one a *page* rather than a band
# Two across, two down: a screen is one 64x64 tilemap in four quadrant blocks,
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


def _text(raw: bytes) -> str:
    """ASCII out of a fixed-width header field: cut at the first NUL, stripped."""
    return raw.split(b"\x00")[0].decode("ascii", "replace").strip()


def _live_sequences(data: bytes, at: int, count: int, steps: int) -> int:
    """How many of an animation table's groups hold anything at all.

    The number worth reporting, since a file has room for 16 or 32 and typically
    fills a handful (``scgcad-formats.md`` §8.3).
    """
    return sum(1 for sequence in read_sequences(data, at, count, steps) if sequence)


def _metadata_fields(data: bytes, at: int) -> list[ContainerField]:
    """The two rows every member's 0x100 metadata block is worth reporting.

    The signature is what detection matched on and the 16 bytes after it are the
    tool version and build date — identical across the surveyed corpus, and the
    one thing in the block that says which build of the authoring tool wrote this
    file. Neither is interpreted further; the whole block rides through a save
    untouched.
    """
    block = data[at : at + HEADER]
    return [
        ContainerField(
            "Signature",
            f"{SIGNATURE.decode()} at {at:#06x}"
            if block[: len(SIGNATURE)] == SIGNATURE
            else "absent",
            "The 16 bytes that identify this file's family, and what\n"
            "detection matched to pick this container. Where they sit\n"
            "is itself part of the format.",
        ),
        ContainerField(
            "Tool version",
            _text(block[0x10:0x20]) or "blank",
            "The version and build date of the authoring tool that\n"
            "wrote the file. Recorded here only - celPix is not that\n"
            "tool, so a save leaves the whole block as it found it.",
        ),
    ]


def _payload(source: ReadSource, ctx: PipelineContext, start: int, size: int) -> bytes:
    """``size`` bytes of ``source`` from ``start``, with the offset published.

    The container is the only thing that knows where its payload begins, so it
    publishes that as ``KEY_SOURCE_OFFSET`` — what every address display and
    slice anchor downstream resolves against. Short input yields what is there:
    a truncated file opens showing the rows it has rather than refusing.
    """
    ctx.set(KEY_SOURCE_OFFSET, start)
    return source.data[start : start + size]


class ScrContainer:
    """Screen file: payload first, 0x100 header at 0x2000, 0x200 clear codes.

    The four 0x800 blocks are handed on as one buffer, with their shape and their
    **assembly** published (``KEY_TILEMAP_PAGE_ROWS``, ``KEY_TILEMAP_PAGES_ACROSS``).
    A screen is not four screens that might go together somehow: it is **one
    64x64 tilemap stored as four quadrants**, and the editor's own loader says so
    — ``load_scr`` writes the four blocks into a single array at row stride 64,
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
        existing = dest.existing or _blank_scr()
        return splice(existing, 0, data[:SCR_PAYLOAD])

    def describe(
        self, source: ReadSource, ctx: PipelineContext
    ) -> tuple[ContainerField, ...]:
        data = source.data
        header = data[SCR_HEADER_AT : SCR_HEADER_AT + HEADER]
        size = _cell_tiles(header, SCR_TILE_SIZE)
        raw_byte = header[SCR_TILE_SIZE] if SCR_TILE_SIZE < len(header) else 0
        base_word = (
            int.from_bytes(header[0x47:0x49], "little") if len(header) > 0x48 else 0
        )
        return (
            *_metadata_fields(data, SCR_HEADER_AT),
            ContainerField(
                "Payload",
                f"{format_size(SCR_PAYLOAD)} at 0x000000 - four 32x32 quadrants",
                "One 64x64 tilemap stored as four quadrant blocks. They\n"
                "come out as one buffer with their shape and their 2x2\n"
                "layout published, so the view assembles them as the\n"
                "editor that wrote them did.",
            ),
            ContainerField(
                "Cell size byte",
                f"0x{raw_byte:02X} at {SCR_HEADER_AT + SCR_TILE_SIZE:#06x}"
                f" - {size[0]}x{size[1]} tiles per cell",
                "8 * (value + 1) pixels. Read small, a 16x16 screen draws\n"
                "one quarter of every cell and drops the rest, so this is\n"
                "published as the view's cell size.",
            ),
            ContainerField(
                "Base character word",
                f"0x{base_word:04X} at {SCR_HEADER_AT + 0x47:#06x} - not applied",
                "It reads like a base tile index and is not one: added to\n"
                "every cell it sends the screen off the end of the bank,\n"
                "and neither candidate meaning survives the corpus. The\n"
                "tile base is yours to set.",
            ),
            ContainerField(
                "Clear codes",
                f"{format_size(max(0, len(data) - SCR_HEADER_AT - HEADER))}"
                f" at {SCR_HEADER_AT + HEADER:#06x}, preserved",
                "A per-cell draw/don't-draw the artist set. celPix has no\n"
                "per-cell visibility to map it onto, so it rides through a\n"
                "save untouched rather than being regenerated.",
            ),
        )


def _blank_scr() -> bytes:
    out = bytearray(SCR_SIZE)
    out[SCR_HEADER_AT : SCR_HEADER_AT + len(SIGNATURE)] = SIGNATURE
    trailer = SCR_HEADER_AT + HEADER
    out[trailer:] = b"\xff" * (SCR_SIZE - trailer)
    return bytes(out)


def _stamp_tiles(header: bytes) -> tuple[int, int]:
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
    panel" flag the tool sets as it hands blocks out — and it doubles as the
    editor's own draw test: a cell whose bit is clear renders as background, in
    the panel view and through a stamp layout alike (``scgcad-formats.md`` §3.2).
    That it marks only 8% of populated cells is the point rather than a puzzle,
    the rest of the 16,384-cell grid being unregistered scratch.

    It still rides through untouched, because celPix has no per-cell visibility to
    map it onto and regenerating it would mean re-running the allocator. So a
    panel here draws cells the tool would have left blank; what a save writes is
    what it read.

    What the container *does* read is the stamp size at 0x69/0x6A, which is not
    about this file's own cells at all — it is the block size a bound stamp layout
    indexes in (:data:`~celpix.core.context.KEY_TILEMAP_STAMP_TILES`).
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
        ctx.set(KEY_TILEMAP_STAMP_TILES, _stamp_tiles(source.data[:HEADER]))
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
        across, down = _stamp_tiles(data[:HEADER])
        return (
            *_metadata_fields(data, 0),
            ContainerField(
                "Tile table",
                f"{format_size(PNL_TABLE)} at {HEADER:#06x} - the payload",
                f"0x{PNL_TABLE // 2:X} cells, laid out {PANEL_COLUMNS} wide.\n"
                "Each word is one 8x8 tile; a 16x16 unit is stored as\n"
                "four adjacent words rather than as one bigger cell.",
            ),
            ContainerField(
                "Registration table",
                f"{format_size(PNL_TABLE)} at {HEADER + PNL_TABLE:#06x}, preserved",
                "The same size again, following the tiles. Bit 15 marks\n"
                "a cell the tool handed out as part of a panel, and it is\n"
                "the tool's own draw test - clear renders as background.\n"
                "celPix draws every cell and writes this back untouched.",
            ),
            ContainerField(
                "Stamp size",
                f"{across}x{down} cells at "
                f"{PNL_STAMP_EXPONENTS[0]:#04x}/{PNL_STAMP_EXPONENTS[1]:#04x}",
                "How big a block one stamp-layout coordinate names, as\n"
                "two exponents. Published for a layout bound to this\n"
                "panel; it is not this file's own cell size, which is one\n"
                "8x8 tile per word whatever the header suggests.",
            ),
            ContainerField(
                "Cell size byte",
                f"0x{data[0x62]:02X} at 0x000062 - not read"
                if len(data) > 0x62
                else "absent",
                "A header byte shaped like a cell size and not one:\n"
                "reading it as one draws the panel at four times its\n"
                "content. Measured against the corpus, a panel word is\n"
                "always a single 8x8 tile.",
            ),
        )


class MapContainer:
    """Stamp layout: 0x100 header, then 0x1000 entries naming panel cells.

    A MAP holds no tiles and no attributes of its own — each entry is a
    coordinate into a panel, and resolving one needs that panel
    (``scgcad-formats.md`` §4). The container's job stops at cutting the entry
    table out; the two-level resolution belongs to the tile binding.

    The 0x1000 entries are **64 x 64 tile positions**, a screen's shape, because a
    layout is generated from a screen one block at a time. How big a block is comes
    from the panel rather than from here, and it is why most of these entries are
    not read: at the commonest 2x2 the tool writes one entry in four and leaves the
    other three at whatever the file already held.
    """

    info = PluginInfo(
        id="container.scgcad-map",
        name="S-CG-CAD stamp layout (MAP)",
        stage=Stage.CONTAINER,
        extensions=(".map",),
        magic=((0, SIGNATURE),),
        exact_size=MAP_SIZE,
        short_name="MAP",
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
                f"{format_size(MAP_PAYLOAD)} at {HEADER:#06x} - the payload",
                f"0x{MAP_PAYLOAD // 2:X} entries, laid out {MAP_COLUMNS} wide -\n"
                "a screen's shape, which is what a layout is made from.\n"
                "The width is fixed by the format, so it is published\n"
                "rather than left as a guess.",
            ),
            ContainerField(
                "Entry meaning",
                "a panel coordinate, not a tile",
                "A stamp layout holds no tiles and no attributes of its\n"
                "own: resolving an entry needs the panel it was authored\n"
                "against, which is what the tile binding supplies.",
            ),
            ContainerField(
                "Stamp size",
                "the panel's, not this file's",
                "One entry names a block, and how big a block is comes\n"
                "from the panel this layout is bound to. Until it is\n"
                "bound, an entry reads as the single cell it names.",
            ),
        )


class ObjContainer:
    """Sprite object: subsprite records first, then the header and animation table.

    Payload-first like a screen, and the header's *offset* is what says which of
    the two sizes this is — 0x3000 for an object, 0xC000 for the extended form,
    which holds twice the frames at twice the subsprites each. Detection matches
    the signature at either.

    What follows the header is the animation table: runs of (duration, frame)
    naming frames in the payload. It is preserved and not read — celPix draws
    the frames themselves, laid out in file order, and playing them is a
    different feature (``tilemap-entry.md`` §9).

    The one thing here that is a *reading* rather than a cut: the build marker
    decides the attribute word's byte order, so it is published for the codec
    (:data:`~celpix.core.context.KEY_TILEMAP_ENDIAN`). 26 of the 1,341 objects in
    the corpus are the later build and every one of them says so.
    """

    info = PluginInfo(
        id="container.scgcad-obj",
        name="S-CG-CAD sprite object (OBJ/OBX)",
        stage=Stage.CONTAINER,
        extensions=(".obj", ".obx"),
        magic=tuple((at, SIGNATURE) for at in OBJ_PAYLOADS),
        short_name="OBJ",
    )
    default_tilemap_preset = "preset.tilemap.scgcad-object"

    def read(self, source: ReadSource, ctx: PipelineContext) -> bytes:
        payload = _obj_payload(source.data)
        ctx.set(KEY_TILEMAP_COLUMNS, OBJECT_COLUMNS)
        # The two forms hold the same payload and divide it differently — 32
        # frames of 64 slots against 64 of 128 — so the stride is not derivable
        # from the size and has to come from *which form this is*, which is the
        # question already answered above. The extended object's own converter
        # declares `obj_data[64][128][6]` where the ordinary one declares
        # `[32][64][6]` (``scgcad-formats.md`` §8.1).
        ctx.set(KEY_TILEMAP_SUBSPRITES_PER_FRAME, OBJ_SUBSPRITES_PER_FRAME[payload])
        marker = source.data[payload + 0x10 : payload + 0x20]
        # The `F` build byte-swaps the attribute word. Keyed off the marker
        # rather than the file size, which is the other signal and the indirect
        # one: an `F` object happens to be 0x20 bytes longer, but that is its
        # extra per-sequence positions, not the thing being asked about.
        swapped = marker.rstrip().endswith(b"F")
        ctx.set(KEY_TILEMAP_ENDIAN, "little" if swapped else "big")
        # The animation table, from the tail this container is about to cut away.
        # Read here rather than by the codec because the codec is handed the
        # payload alone and the table is past it — and read at all only because a
        # reader wants it; the write side still preserves these bytes opaquely,
        # so nothing downstream can make a save depend on this being right.
        ctx.set(
            KEY_TILEMAP_ANIMATIONS,
            read_sequences(
                source.data,
                payload + HEADER,
                OBJ_SEQUENCES[payload],
                OBJ_SEQUENCE_STEPS,
            ),
        )
        return _payload(source, ctx, 0, payload)

    def write(self, data: bytes, dest: WriteTarget, ctx: PipelineContext) -> bytes:
        # The form comes from the records being saved, not from the file already
        # at the path: splicing 64 frames into an ordinary object drops 32 of
        # them, and a path with no file at it has no form to offer at all. Where
        # the two agree the whole tail is preserved, animation table included —
        # and that covers the `F` build, whose extra 0x20 bytes ride along
        # untouched because the payload is where they are measured from.
        existing = dest.existing or _blank_obj()
        payload = _obj_payload(existing)
        if payload == len(data):
            return splice(existing, 0, data)
        if len(data) in OBJ_SIZES:
            return splice(_blank_obj(len(data)), 0, data)
        # Records that are neither form's length: the destination decides, as it
        # did before either form was named here.
        return splice(existing, 0, data[:payload])

    def describe(
        self, source: ReadSource, ctx: PipelineContext
    ) -> tuple[ContainerField, ...]:
        data = source.data
        payload = _obj_payload(data)
        marker = _text(data[payload + 0x10 : payload + 0x20])
        swapped = marker.rstrip().endswith("F")
        extended = payload == OBJ_PAYLOADS[1]
        groups = OBJ_SEQUENCES[payload]
        slots = OBJ_SUBSPRITES_PER_FRAME[payload]
        return (
            *_metadata_fields(data, payload),
            ContainerField(
                "Form",
                f"{'extended' if extended else 'ordinary'} - "
                f"{payload // (slots * SUBSPRITE_RECORD)} frames"
                f" of {slots} subsprites",
                "Which of the two sizes this is shows in *where* the\n"
                "signature sits, so the records stop there. Found by\n"
                "signature rather than by file length, so a file with an\n"
                "unexpected tail still reads.",
            ),
            ContainerField(
                "Build marker",
                f"{marker or 'blank'} - attribute word "
                f"{'byte-swapped' if swapped else 'as stored'}",
                "A later build of the tool writes the attribute word the\n"
                "other way round and says so here. Published as the cell\n"
                "byte order, overriding the codec's own assumption.",
            ),
            ContainerField(
                "Animation table",
                f"{format_size(groups * OBJ_SEQUENCE_STEPS * 2)}"
                f" at {payload + HEADER:#06x}, "
                f"{_live_sequences(data, payload + HEADER, groups, OBJ_SEQUENCE_STEPS)}"
                f" of {groups} sequences used",
                "Runs of (duration, frame) naming frames in the payload.\n"
                "Read for playback only: celPix draws the frames\n"
                "themselves in file order, and a save writes these bytes\n"
                "back exactly as they were read.",
            ),
        )


class ObzContainer:
    """Transfer object: 0x6000 of subsprite records, then a 0xA00 tail.

    The same shape as a sprite object with the header taken away — the tool wrote
    these to ship a whole set of frames to the devkit rather than to reopen them,
    and none of the 148 in the corpus carries the family signature. So detection
    has only the extension and the length, and a write has only the tail to
    preserve — which holds the animation table, read here exactly as an object's
    is and at the same confidence (``scgcad-formats.md`` §9).

    The records inside are **not** an object's records. Its own codec says how
    (:class:`~celpix.plugins.builtins.object_codec.ObzCodec`).
    """

    info = PluginInfo(
        id="container.scgcad-obz",
        name="S-CG-CAD transfer object (OBZ)",
        stage=Stage.CONTAINER,
        extensions=(".obz",),
        exact_size=OBZ_SIZE,
        short_name="OBZ",
    )
    default_tilemap_preset = "preset.tilemap.scgcad-obz"

    def read(self, source: ReadSource, ctx: PipelineContext) -> bytes:
        ctx.set(KEY_TILEMAP_COLUMNS, OBJECT_COLUMNS)
        # The table sits where the records stop, with no header between them —
        # the one difference from an object, whose 0x100 metadata block comes
        # first. Same steps, same terminator, wider groups.
        ctx.set(
            KEY_TILEMAP_ANIMATIONS,
            read_sequences(source.data, OBZ_PAYLOAD, OBZ_SEQUENCES, OBZ_SEQUENCE_STEPS),
        )
        return _payload(source, ctx, 0, OBZ_PAYLOAD)

    def write(self, data: bytes, dest: WriteTarget, ctx: PipelineContext) -> bytes:
        existing = dest.existing or bytes(OBZ_SIZE)
        return splice(existing, 0, data[:OBZ_PAYLOAD])

    def describe(
        self, source: ReadSource, ctx: PipelineContext
    ) -> tuple[ContainerField, ...]:
        return (
            ContainerField(
                "Signature",
                "none - identified by length and suffix",
                "The one member of this family that carries no signature\n"
                "anywhere: it was written to be consumed by the devkit\n"
                "rather than reopened, so its length is all there is to\n"
                "go on.",
            ),
            ContainerField(
                "Payload",
                f"{format_size(OBZ_PAYLOAD)} at 0x000000 - "
                f"{OBZ_PAYLOAD // (64 * SUBSPRITE_RECORD)} frames"
                f" of 64 subsprites",
                "The same shape as a sprite object with the header taken\n"
                "away. The records inside are not an object's records,\n"
                "which is why this reads through its own cell codec.",
            ),
            ContainerField(
                "Animation table",
                f"{format_size(OBZ_SIZE - OBZ_PAYLOAD)} at {OBZ_PAYLOAD:#06x}, "
                f"{_live_sequences(source.data, OBZ_PAYLOAD, *OBZ_TABLE)}"
                f" of {OBZ_SEQUENCES} sequences used",
                "16 sequences of 64 (duration, frame), read for playback\n"
                "only. The whole tail is kept exactly as it stands, so a\n"
                "save leaves the frame timings alone.",
            ),
        )


class StdContainer:
    """Converted screen: 0x1000 bytes, 64x64 bare character numbers, no header.

    Not something the authoring tool writes — a 1992 converter made it out of a
    screen, keeping the **low byte** of each cell and dropping the high one, then
    laid the screen's four blocks out 2x2 (``scgcad-formats.md`` §10). So it
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
                "Headerless and fixed-size. Not something the authoring\n"
                "tool writes: a converter made it out of a screen, and\n"
                "wrote nothing to say so.",
            ),
            ContainerField(
                "Payload",
                f"{format_size(STD_SIZE)} - the whole file, {STD_COLUMNS} wide",
                "Bare character numbers, the screen's four blocks laid\n"
                "out 2x2. The width is fixed by the format and published\n"
                "as such - 4 KiB of bytes would not suggest it.",
            ),
            ContainerField(
                "Cell contents",
                "low byte only - attributes dropped",
                "The converter kept the low byte of each screen cell and\n"
                "discarded the high one, so there are no palette rows or\n"
                "flip bits left to read. That is why this holds a\n"
                "screen's shape but reads through an index-only cell.",
            ),
        )


def _obj_payload(data: bytes) -> int:
    """Where this object's records stop — which is where its signature starts.

    By signature position rather than by file length, so the two sizes are told
    apart by the same thing detection used and a file with an unexpected tail
    still reads.
    """
    for at in OBJ_PAYLOADS:
        if data[at : at + len(SIGNATURE)] == SIGNATURE:
            return at
    return OBJ_PAYLOADS[0]


def _blank_obj(payload: int = OBJ_PAYLOADS[0]) -> bytes:
    """An empty object of the form ``payload`` bytes of records names.

    The signature's *position* is the whole of what says which form a file is, so
    a blank has to be the right length with it in the right place; there is
    nowhere else in the header the choice is written down. The build marker is
    left blank, which reads as the common build and its big-endian attribute word
    — the only one celPix could claim to have written.
    """
    out = bytearray(OBJ_SIZES[payload])
    out[payload : payload + len(SIGNATURE)] = SIGNATURE
    return bytes(out)


class ColContainer:
    """Palette file: 0x200 of colour, then the 0x100 header and its 0x100 tail.

    The only member of the family that frames a *palette*, and the reason a
    container is needed at all: the colours stop halfway. Read whole, the
    metadata block decodes as 128 more BGR555 entries — junk rows 16-31 of a 4bpp
    palette — which look like colours because any two bytes do.

    The file holds 256 entries where the console has 256 too, so the trailing
    block is not a second bank: a screen picks which 128-colour half to draw
    through with its own ``col_half`` field (``scgcad-formats.md`` §2).
    """

    info = PluginInfo(
        id="container.scgcad-col",
        name="S-CG-CAD palette (COL)",
        stage=Stage.CONTAINER,
        extensions=(".col",),
        magic=((COL_HEADER_AT, SIGNATURE),),
        exact_size=COL_SIZE,
        short_name="COL",
        content_kinds=(ContentKind.PALETTE,),
    )

    def read(self, source: ReadSource, ctx: PipelineContext) -> bytes:
        return _payload(source, ctx, 0, COL_PAYLOAD)

    def write(self, data: bytes, dest: WriteTarget, ctx: PipelineContext) -> bytes:
        # Splice, so a colour edit leaves the tool's own metadata block alone —
        # it names the version that wrote the file, and celPix is not it.
        existing = dest.existing or _blank_col()
        return splice(existing, 0, data[:COL_PAYLOAD])

    def describe(
        self, source: ReadSource, ctx: PipelineContext
    ) -> tuple[ContainerField, ...]:
        return (
            *_metadata_fields(source.data, COL_HEADER_AT),
            ContainerField(
                "Colors",
                f"{format_size(COL_PAYLOAD)} at 0x000000 - {COL_PAYLOAD // 2} entries",
                "Where the colours stop, which is the whole reason this\n"
                "file needs a container: read to the end of the file, the\n"
                "metadata block decodes as 128 more entries of junk that\n"
                "look like colours because any two bytes do.",
            ),
            ContainerField(
                "Metadata block",
                f"{format_size(COL_SIZE - COL_PAYLOAD)}"
                f" at {COL_HEADER_AT:#06x}, preserved",
                "Spliced around on write, so editing a colour leaves the\n"
                "tool's own metadata as it found it. Not a second bank of\n"
                "colours - a screen picks which 128-colour half to draw\n"
                "through with a field of its own.",
            ),
        )


def _blank_col() -> bytes:
    out = bytearray(COL_SIZE)
    out[COL_HEADER_AT : COL_HEADER_AT + len(SIGNATURE)] = SIGNATURE
    return bytes(out)


def _blank(size: int) -> bytes:
    """An empty file of this family: zeroes with the signature in place."""
    out = bytearray(size)
    out[: len(SIGNATURE)] = SIGNATURE
    return bytes(out)


# A tile bank, by file size: payload length, the pixel preset that reads it, and
# its depth in bits. All 1024 tiles; the depth is what changes. Size and the
# header's own depth byte agree across the whole surveyed corpus, so either would
# do and neither has to trust the other.
CGX_BANKS: dict[int, tuple[int, str, int]] = {
    0x4500: (0x4000, "preset.pixel.snes-2bpp", 2),
    0x8500: (0x8000, "preset.pixel.snes-4bpp", 4),
    0x10100: (0x10000, "preset.pixel.snes-8bpp", 8),
}
# The same three, keyed by how many bytes of tiles they hold — which is what the
# write side has to name a variant from. What comes out of the pipeline is the
# payload; its length is the depth the user is actually saving at, and the ctx
# hint the read published is not, having only ever seeded a picker they own.
CGX_BY_PAYLOAD: dict[int, int] = {
    payload: size for size, (payload, *_) in CGX_BANKS.items()
}
CGX_ROW_TABLE = 0x400  # one byte per tile, each a palette row


def _cgx_bank(data: bytes) -> tuple[int, str, int] | None:
    """Which of the three banks ``data`` is, by length and then by signature.

    Length settles it for every well-formed file — the three differ by more than
    their payloads, since only 2bpp and 4bpp carry an attribute table. Signature
    position is the fallback for one that has picked up a tail, and it is the
    same signal detection matched on: the metadata block sits *after* the tiles,
    so where it starts is where they stop. Reading a tailed bank by length alone
    hands the header and the table on as three dozen tiles of noise and states no
    depth at all, which is exactly what the framing exists to prevent.

    ``None`` for bytes that are neither, which the caller passes through whole.
    """
    bank = CGX_BANKS.get(len(data))
    if bank is not None:
        return bank
    for bank in CGX_BANKS.values():
        payload = bank[0]
        if data[payload : payload + len(SIGNATURE)] == SIGNATURE:
            return bank
    return None


def _blank_cgx(payload: int) -> bytes:
    """An empty bank of the variant ``payload`` bytes of tiles names.

    Stamped so the file states that variant both ways a real one does — the
    signature's position, and the depth byte behind it — because a bank written
    with neither is not a bank: nothing detects it, and reopening it lands on raw
    bytes at a guessed depth — which is what a save to a path with no file at it
    would otherwise produce.

    The attribute table is zero, and 8bpp has none. Row 0 is a row rather than a
    sentinel (`scgcad-formats.md` §6), so a fresh bank saying every tile is row 0
    is a statement it can stand behind, and the base its header states is 0 to
    match.
    """
    size = CGX_BY_PAYLOAD[payload]
    _, _, bpp = CGX_BANKS[size]
    out = bytearray(size)
    out[payload : payload + len(SIGNATURE)] = SIGNATURE
    out[payload + CGX_DEPTH] = _DEPTH_BYTE[bpp]
    return bytes(out)


def _cgx_row_base(data: bytes, payload: int, bpp: int) -> int | None:
    """The palette row this bank's tiles count their own row 0 from, or None.

    ``col_half * 128 + col_cell * n`` colours, the same per-file base a screen or
    a panel applies (``scgcad-formats.md`` §3.3), divided through by ``n`` into
    rows. 1,072 of the 2,304 surveyed banks set one — 766 of them a whole colour
    half, which is eight rows out at 4bpp.

    It is the base for everything drawn *from* the bank, so it answers for the
    formats that carry a palette row and have nowhere to put a base: a sprite
    object's 3-bit field, an 8bpp bank's absent attribute table. None where the
    header is not there to be read — thirty-nine banks of the corpus carry no
    signature at all, their 0x100 header zeroed, and a zero base read off that is
    an invention rather than a reading.
    """
    header = data[payload : payload + HEADER]
    if header[: len(SIGNATURE)] != SIGNATURE or len(header) <= CGX_COL_CELL:
        return None
    return _row_base(header[CGX_COL_HALF], header[CGX_COL_CELL], bpp)


def _cgx_rows(data: bytes, payload: int, bpp: int) -> bytes:
    """A bank's per-tile palette rows as the file states them, or empty when it
    states none.

    **Relative rows**, exactly as the table holds them: what they count from is
    published beside them as :data:`~celpix.core.context.KEY_TILE_PALETTE_ROW_BASE`,
    and the host applies the base to a named row once, at render
    (:func:`~celpix.pipeline.pipeline.drawn_palette_row`). Folding it in here as
    well would move a bank's art twice.

    The table is only there when the header is. The thirty-nine header-less banks
    hold 0xFC throughout, a metadata block allocated and never written. Their
    tiles are real and at the depth the size gives, so the payload is still cut
    and the preset still published; the table is not a statement about them.

    An 8bpp bank is too short to hold a table at all, which is also the guard
    the only other implementation of this format applies.
    """
    if bpp >= 8:
        return b""
    if _cgx_row_base(data, payload, bpp) is None:
        return b""
    at = payload + HEADER
    return data[at : at + CGX_ROW_TABLE]


class CgxContainer:
    """Tile bank: the tiles, then a 0x100 header, then a per-tile row table.

    Payload-first like a screen, so reading the file raw *almost* works — which
    is the problem. The trailing header and table decode as a few dozen tiles of
    convincing noise at the end of every bank, and the bit depth has to be
    guessed from three that all look plausible. Both are in the file.

    **Three variants, one per depth**, and they differ by more than their
    payloads: 8bpp is too short to hold the attribute table at all, so the three
    lengths are 0x4500, 0x8500 and 0x10100 for 0x4000, 0x8000 and 0x10000 of
    tiles. Which one a file is comes from its length, and failing that from where
    its signature sits (:func:`_cgx_bank`) — the two signals detection and
    reading now share, so a bank with a tail is not read as a fourth thing.

    A write names the variant from the **payload** instead, because that is the
    depth being saved and the header of the file already there may be a
    different one. Same variant, and everything around the tiles is preserved;
    otherwise the bank is built (:func:`_blank_cgx`), which is also what a save
    to a path with no file at it gets.

    Two hints go out on the context rather than being applied here: the pixel
    preset the payload is in, and the per-tile palette rows. A container's job is
    to say what it knows, not to reach into the view.
    """

    info = PluginInfo(
        id="container.scgcad-cgx",
        name="S-CG-CAD tile bank (CGX)",
        stage=Stage.CONTAINER,
        extensions=(".cgx",),
        # One offset per depth: the signature sits at the payload's end, so where
        # it is *is* which bank this is.
        magic=tuple((payload, SIGNATURE) for payload, _, _ in CGX_BANKS.values()),
        short_name="CGX",
    )

    def read(self, source: ReadSource, ctx: PipelineContext) -> bytes:
        bank = _cgx_bank(source.data)
        if bank is None:
            # A size the family does not have: hand the bytes on whole rather
            # than cutting at a guess. Better a few trailing junk tiles than a
            # silently truncated bank.
            return plain_read(source, ctx)
        payload, preset_id, bpp = bank
        ctx.set(KEY_PIXEL_PRESET, preset_id)
        rows = _cgx_rows(source.data, payload, bpp)
        if rows:
            ctx.set(KEY_TILE_PALETTE_ROWS, rows)
        # Published beside the table rather than only folded into it: a tilemap
        # bound to this bank draws its own cells' rows from the same origin, and
        # a sprite object's 3-bit field has no header of its own to say so.
        base = _cgx_row_base(source.data, payload, bpp)
        if base is not None:
            ctx.set(KEY_TILE_PALETTE_ROW_BASE, base)
        ctx.set(KEY_SOURCE_OFFSET, 0)
        return source.data[:payload]

    def write(self, data: bytes, dest: WriteTarget, ctx: PipelineContext) -> bytes:
        # Splice, so the header and the row table come back untouched — the rows
        # are the file's own statement about its tiles and celPix has no reason
        # to rewrite them from an edit that changed pixels.
        bank = _cgx_bank(dest.existing)
        if bank is not None and bank[0] == len(data):
            return splice(dest.existing, 0, data)
        # Either there is no file there yet or the depth has changed under it,
        # and both need a container built rather than one reused: the three
        # variants are different lengths, so writing 4bpp tiles into a 2bpp bank
        # can only drop half of them, and writing them into nothing at all leaves
        # a payload no reader can identify. What the old file said about a
        # different depth — its colour selectors, its table — is not a statement
        # about these tiles, so none of it carries over.
        if len(data) not in CGX_BY_PAYLOAD:
            # Not one of the three payload sizes: the same pass-through the read
            # half does for a length this family does not have.
            return splice(dest.existing, 0, data)
        return splice(_blank_cgx(len(data)), 0, data)

    def describe(
        self, source: ReadSource, ctx: PipelineContext
    ) -> tuple[ContainerField, ...]:
        data = source.data
        bank = _cgx_bank(data)
        if bank is None:
            return (
                ContainerField(
                    "Bank size",
                    f"{len(data)} bytes - not a size this family has",
                    "The three tile banks are told apart by their length,\n"
                    "and failing that by where the signature sits. This\n"
                    "file answers to neither, so the bytes are handed on\n"
                    "whole rather than cut at a guess: better a few\n"
                    "trailing junk tiles than a silently truncated bank.",
                ),
            )
        payload, _, bpp = bank
        stated = data[payload + CGX_DEPTH] if len(data) > payload + CGX_DEPTH else None
        if bpp >= 8:
            table = "none - 8bpp banks carry no table"
        elif not _cgx_rows(data, payload, bpp):
            table = "present but not read - the header is absent"
        else:
            base = _cgx_row_base(data, payload, bpp)
            table = (
                f"{format_size(CGX_ROW_TABLE)} at {payload + HEADER:#06x}"
                f", counted from row {base}"
            )
        return (
            *_metadata_fields(data, payload),
            ContainerField(
                "Payload",
                f"{format_size(payload)} at 0x000000 - 1024 tiles",
                "Payload first, header after, which is what makes reading\n"
                "this file raw *almost* work: the trailing block decodes\n"
                "as a few dozen tiles of convincing noise.",
            ),
            ContainerField(
                "Bit depth",
                f"{bpp}bpp - {len(data)} bytes"
                + (
                    ", header says none"
                    if stated is None
                    else f", header says {_DEPTH_BPP.get(stated & 3, bpp)}bpp"
                ),
                "In the file twice over, so it need not be guessed - the\n"
                "three depths look alike enough that a wrong pick reads as\n"
                "plausible garbage. The length is one statement and the\n"
                "byte at +0x20 behind the signature is the other; they\n"
                "agree across the whole surveyed corpus. Published as the\n"
                "pixel format the view starts at.",
            ),
            ContainerField(
                "Palette row table",
                table,
                "One byte per tile naming the palette row it is meant to\n"
                "be read under, counted from the base this file's header\n"
                "states. Published so it can seed pinned palette regions,\n"
                "and preserved on write: the rows are the file's own\n"
                "statement, not something to re-derive from a pixel edit.\n"
                "A bank with no header states no rows: the table is there\n"
                "but nothing wrote it.",
            ),
        )
