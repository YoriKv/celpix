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
**clear codes** — a per-cell "draw this / don't" the artist set, which cannot be
derived from the tile data and would be destroyed by regenerating it. celPix has
no per-cell visibility to map them onto yet, so they ride through untouched
(``scgcad-formats.md`` §2.1, §3.2).
"""

from __future__ import annotations

from celpix.core.capabilities import ContentKind
from celpix.core.context import (
    KEY_PIXEL_PRESET,
    KEY_SOURCE_OFFSET,
    KEY_TILE_PALETTE_ROWS,
    KEY_TILEMAP_CELL_TILES,
    KEY_TILEMAP_COLUMNS,
    KEY_TILEMAP_ENDIAN,
    KEY_TILEMAP_PAGE_ROWS,
    PipelineContext,
)
from celpix.core.errors import Stage
from celpix.plugins.base import (
    PluginInfo,
    ReadSource,
    WriteTarget,
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
PNL_TABLE = 0x8000  # tile table; an equal-sized clear-code table follows it

MAP_SIZE = 0x2100
MAP_PAYLOAD = 0x2000  # 0x1000 entries, laid out 128 wide
MAP_WIDTH = 128

COL_SIZE = 0x400
COL_PAYLOAD = 0x200  # 256 BGR555 entries; the metadata block follows them
COL_HEADER_AT = 0x200

# A sprite object's records, then its header: 32 frames of 64 six-byte subsprites in
# the ordinary form and 128 frames in the extended one. Which it is shows in
# where the signature sits, so these double as the detection offsets.
OBJ_PAYLOADS = (0x3000, 0xC000)
OBJ_SIZE = 0x3500  # the ordinary form, for a file that does not exist yet

# The transfer form of a sprite object: 64 frames of 64 subsprites, then a 0xA00 tail
# holding the animation table. The one member of the family that carries **no
# signature at all** — none of the 148 in the corpus has one anywhere — so its
# length and its extension are the whole of what identifies it.
OBZ_SIZE = 0x6A00
OBZ_PAYLOAD = 0x6000

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

# A panel's header carries the same-looking byte at 0x62 and two metatile
# exponents at 0x69/0x6A, and **none of the three is this file's cell size**. A
# panel word is always one 8x8 tile: a 16x16 unit is stored as four adjacent
# words, which is measurable — across the whole corpus, in panels flagged either
# way, the four tiles a metatile would expand to are already present as the four
# words of a 2x2 group (`scgcad-formats.md` §3.1). Reading any of the three as a
# cell size draws the panel at four times its content. The one independent
# implementation of these formats reads the screen's byte and skips the panel's
# for the same reason.

# The word at screen +0x47 / panel +0x67 is deliberately NOT read. It reads like
# a base character index and is not one: it is non-zero in 83% of screens (most
# often 0x03EE), and adding it to every cell index sends the whole screen off the
# end of a 1024-tile bank. Neither candidate meaning survives the corpus — as a
# "clear character number" it matches the screen's actual background character
# 24% of the time, and its byte order is not even consistent between files
# (`scgcad-formats.md` §2, "Unresolved"). The one independent implementation of
# these formats adds it to nothing either. A tile base is the user's to set.

PANEL_COLUMNS = 32  # a panel is 32 cells wide; its 0x4000 cells make 512 rows
SCREEN_COLUMNS = 32  # one 32x32 screen — four of them make up a screen file
SCREEN_ROWS = 32  # and this is what makes each one a *page* rather than a band
MAP_COLUMNS = 128


def _cell_tiles(data: bytes, at: int) -> tuple[int, int]:
    """How many tiles one cell draws, from a ``8 * (value + 1)`` tile-size byte.

    Two values occur and only two: 0 is an 8x8 cell, 1 a 16x16 one, whose four
    tiles are ``N``, ``N+1``, ``N+0x10``, ``N+0x11`` — the console's own 16x16 BG
    arithmetic. Anything else is a corrupt byte and reads as 8x8: a file that
    unreasonable is better read small than not at all.
    """
    return (2, 2) if at < len(data) and data[at] == 1 else (1, 1)


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

    The four 0x800 screens are handed on as one buffer, with their size published
    so the view knows there are four (``KEY_TILEMAP_PAGE_ROWS``). How they
    assemble into a larger screen is *not* recorded anywhere in the file — the
    era's own tooling took a screen index on the command line — so which
    arrangement to draw is a view choice rather than something to decide here
    (``scgcad-formats.md`` §2).

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
        # Four maps in one file, not one map four times as tall: saying so is what
        # gives the view an assembly to offer, since the file itself records
        # nothing about which the artist meant (`scgcad-formats.md` §2, "Screen
        # assembly"). The rows are the page's; the columns above are its width.
        ctx.set(KEY_TILEMAP_PAGE_ROWS, SCREEN_ROWS)
        # A screen states its own cell size, and 949 of the 1,622 surveyed set
        # this byte. That they mean it is measurable: of the cells those screens
        # actually draw, 76.1% carry a metatile-aligned index against 30.0% for
        # the 8x8 ones — and 25% is what chance alone gives, so the 8x8 group is
        # flat noise and this one is not (`scgcad-formats.md` §2.3). Read small,
        # a 16x16 screen draws one quarter of every cell and drops the rest.
        header = source.data[SCR_HEADER_AT : SCR_HEADER_AT + HEADER]
        ctx.set(KEY_TILEMAP_CELL_TILES, _cell_tiles(header, SCR_TILE_SIZE))
        return _payload(source, ctx, 0, SCR_PAYLOAD)

    def write(self, data: bytes, dest: WriteTarget, ctx: PipelineContext) -> bytes:
        # Splicing over the existing file keeps the header and clear codes exactly
        # as they were read. A file that does not exist yet gets an all-0xFF clear
        # table — every cell visible, which is both what the tool itself writes on
        # import and the commonest state in the wild.
        existing = dest.existing or _blank_scr()
        return splice(existing, 0, data[:SCR_PAYLOAD])


def _blank_scr() -> bytes:
    out = bytearray(SCR_SIZE)
    out[SCR_HEADER_AT : SCR_HEADER_AT + len(SIGNATURE)] = SIGNATURE
    trailer = SCR_HEADER_AT + HEADER
    out[trailer:] = b"\xff" * (SCR_SIZE - trailer)
    return bytes(out)


class PnlContainer:
    """Panel file: 0x100 header, 0x8000 tile table, 0x8000 clear-code table.

    Only the tile table is the payload. The clear codes ride through untouched:
    bit 15 is the only live bit and it is never set on an empty cell, but it
    marks just 8% of the populated ones, so its polarity is unsettled and
    anything written there would be a guess (``scgcad-formats.md`` §3.2).
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
        return _payload(source, ctx, HEADER, PNL_TABLE)

    def write(self, data: bytes, dest: WriteTarget, ctx: PipelineContext) -> bytes:
        existing = dest.existing or _blank(PNL_SIZE)
        return splice(existing, HEADER, data[:PNL_TABLE])


class MapContainer:
    """Stamp layout: 0x100 header, then 0x1000 entries naming panel cells.

    A MAP holds no tiles and no attributes of its own — each entry is a
    coordinate into a panel, and resolving one needs that panel
    (``scgcad-formats.md`` §4). The container's job stops at cutting the entry
    table out; the two-level resolution belongs to the tile binding.
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


class ObjContainer:
    """Sprite object: subsprite records first, then the header and animation table.

    Payload-first like a screen, and the header's *offset* is what says which of
    the two sizes this is — 0x3000 for an object, 0xC000 for the extended form
    with four times the frames. Detection matches the signature at either.

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
        marker = source.data[payload + 0x10 : payload + 0x20]
        # The `F` build byte-swaps the attribute word. Keyed off the marker
        # rather than the file size, which is the other signal and the indirect
        # one: an `F` object happens to be 0x20 bytes longer, but that is its
        # extra per-sequence positions, not the thing being asked about.
        swapped = marker.rstrip().endswith(b"F")
        ctx.set(KEY_TILEMAP_ENDIAN, "little" if swapped else "big")
        return _payload(source, ctx, 0, payload)

    def write(self, data: bytes, dest: WriteTarget, ctx: PipelineContext) -> bytes:
        existing = dest.existing or _blank_obj()
        return splice(existing, 0, data[: _obj_payload(existing)])


class ObzContainer:
    """Transfer object: 0x6000 of subsprite records, then a 0xA00 tail.

    The same shape as a sprite object with the header taken away — the tool wrote
    these to ship a whole set of frames to the devkit rather than to reopen them,
    and none of the 148 in the corpus carries the family signature. So detection
    has only the extension and the length, and a write has only the tail to
    preserve: the animation table, which celPix reads no more here than it does
    in an object (``scgcad-formats.md`` §9).

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
        return _payload(source, ctx, 0, OBZ_PAYLOAD)

    def write(self, data: bytes, dest: WriteTarget, ctx: PipelineContext) -> bytes:
        existing = dest.existing or bytes(OBZ_SIZE)
        return splice(existing, 0, data[:OBZ_PAYLOAD])


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


def _blank_obj() -> bytes:
    out = bytearray(OBJ_SIZE)
    out[OBJ_PAYLOADS[0] : OBJ_PAYLOADS[0] + len(SIGNATURE)] = SIGNATURE
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
# whether a per-tile palette-row table follows the header. All 1024 tiles; the
# depth is what changes. Size and the header's own depth byte agree across the
# whole surveyed corpus, so either would do and neither has to trust the other.
CGX_BANKS: dict[int, tuple[int, str, bool]] = {
    0x4500: (0x4000, "preset.pixel.snes-2bpp", True),
    0x8500: (0x8000, "preset.pixel.snes-4bpp", True),
    0x10100: (0x10000, "preset.pixel.snes-8bpp", False),
}
CGX_ROW_TABLE = 0x400  # one byte per tile, each a palette row


class CgxContainer:
    """Tile bank: the tiles, then a 0x100 header, then a per-tile row table.

    Payload-first like a screen, so reading the file raw *almost* works — which
    is the problem. The trailing header and table decode as a few dozen tiles of
    convincing noise at the end of every bank, and the bit depth has to be
    guessed from three that all look plausible. Both are in the file.

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
        bank = CGX_BANKS.get(len(source.data))
        if bank is None:
            # A size the family does not have: hand the bytes on whole rather
            # than cutting at a guess. Better a few trailing junk tiles than a
            # silently truncated bank.
            return plain_read(source, ctx)
        payload, preset_id, has_rows = bank
        ctx.set(KEY_PIXEL_PRESET, preset_id)
        if has_rows:
            at = payload + HEADER
            ctx.set(KEY_TILE_PALETTE_ROWS, source.data[at : at + CGX_ROW_TABLE])
        ctx.set(KEY_SOURCE_OFFSET, 0)
        return source.data[:payload]

    def write(self, data: bytes, dest: WriteTarget, ctx: PipelineContext) -> bytes:
        # Splice, so the header and the row table come back untouched — the rows
        # are the file's own statement about its tiles and celPix has no reason
        # to rewrite them from an edit that changed pixels.
        bank = CGX_BANKS.get(len(dest.existing))
        payload = bank[0] if bank else len(data)
        return splice(dest.existing, 0, data[:payload])
