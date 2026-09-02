"""The forward-flowing pipeline context.

Stages are decoupled but not blind: each may *read* what earlier stages recorded
and *contribute* entries for later ones. **Everything here is advisory** — a
recommendation a downstream stage or the user may follow, adjust, or ignore, never
an enforced constraint (see ``docs/design/overview.md`` §5).

Two things flow through it today. **Provenance**: the container's read records
where the bytes came from so its write can default to putting them back in the
same place. And the **compression contract**: Decompress records how big the
structure was and whether it decoded whole, which is what a save-back has to fit
into. :mod:`celpix.core.notices` rides on the same bag. It is intentionally an
open, typed key/value store — plugins may define new keys and stages ignore keys
they do not understand.
"""

from __future__ import annotations

from typing import Any

# Well-known context keys. Plugins may add their own; these are the ones the
# built-in stages agree on. Kept as constants so producers and consumers can't
# drift on the spelling.
KEY_SOURCE_PATH = "source.path"  # str: filesystem path the bytes were read from
KEY_SOURCE_OFFSET = "source.offset"  # int: byte offset within that source
# tuple[SourceFile, ...]: every file that went into the buffer a container was
# handed, in order, with the range each supplied. One entry for the ordinary
# single-file source; several when a region is spread over its board's ROM chips.
# Advisory like everything here — a container is handed the files already joined
# precisely so it need not consult this, but one assembling a region from named
# chips can.
KEY_SOURCE_FILES = "source.files"
# int: size of the compressed structure in the source, recorded by Decompress.
# A container usually over-reads (offset to end-of-file), so this — not the
# input length — is the slot a save-back has to fit into.
KEY_COMPRESSED_SIZE = "compression.compressed-size"
# bool: set before Decompress by window-preview callers handing in a *bounded*
# buffer (the visible view window) that may cut a structure short. A
# decompressor that honours it returns the valid prefix it decoded when the
# source ends mid-stream instead of raising; structurally corrupt data still
# raises. Decompressors that don't understand the key just keep strict
# behaviour.
KEY_DECOMPRESS_PARTIAL = "compression.allow-partial"
# bool: whether Decompress found the structure's own end (terminator / known
# size) inside the buffer — i.e. KEY_COMPRESSED_SIZE is the structure's true
# extent, not a truncation point. Distinguishes "the whole structure is in
# view" from a best-effort partial decode.
KEY_DECOMPRESS_COMPLETE = "compression.complete"
# str: why this palette pathway is carrying a placeholder instead of the file's
# colors - the decode error the read fell back from. A palette file records
# nothing about its own encoding, so the format is always a guess; set when that
# guess doesn't fit, so the palette still opens (as an obvious sentinel) and the
# format can be corrected from the dock. Its presence is what marks those colors
# as ours rather than the file's, and it is why the pathway is read-only.
KEY_PALETTE_ERROR = "palette.error"
# str: the palette preset a container believes its entries are in, when the
# format says. The palette-pathway twin of :data:`KEY_PIXEL_PRESET`, and rarer
# for a reason — the comment above is the ordinary case, a palette file recording
# nothing about its own encoding. A `TPL` file is the exception: its header names
# the format outright, which is worth reporting precisely because every other
# palette's format is a guess. Advisory like everything here; nothing is obliged
# to adopt it. Set by the *host* instead on a write into nothing (a new file),
# naming the codec the blank payload is in, for a container whose header has to
# state one and has no file to copy it from.
KEY_PALETTE_PRESET = "palette.preset"
# What a tilemap container read out of its file's *own* header, for the view to
# start from. Advisory like everything here: each is a setting the user can then
# change, and a format that states none of them is simply read without hints.
#
# int: how many cells across the map is, when the format fixes it — a screen and
# a panel are 32, a stamp layout 64. Without it the width is a guess the user
# has to make, and a wrong guess shears the picture into diagonal stripes rather
# than failing, which is the worst way to be wrong.
#
# A **codec** may state it too, for a payload that arrived with no container to
# speak for it, and normally behind the container for the reason above. One
# exception, and it is what marks the line: where the width decides which *byte*
# a field is packed into rather than only how the cells are cut, it is not a hint
# at all and the codec states it over anything already there. The NES attribute
# plane is addressed in the page's own rows, so a width from elsewhere would put
# the host's row groups on a grid `encode` does not write
# (:mod:`celpix.plugins.builtins.nes_nametable`).
KEY_TILEMAP_COLUMNS = "tilemap.columns"
# tuple[int, int]: how many tiles one cell covers, when the header says. A screen
# carries this as a tile-size byte and most real ones are 16x16, so a screen
# whose cells are metatiles reads correctly instead of drawing as a mosaic of
# unrelated tiles. Only published where the field survives being checked against
# the corpus — a panel has three bytes shaped like this one and none of them is
# it (``docs/graphics-formats-reference/scgcad-formats.md`` §3.1).
KEY_TILEMAP_CELL_TILES = "tilemap.cell-tiles"
# int: the palette row a cell's own row 0 counts from, when the *file* says.
# Distinct from the preset's ``palette_row_base``, which is a fact about a kind
# of cell — a sprite's rows always start at CGRAM 8. A screen and a panel carry
# two header bytes (``col_half``, ``col_cell``) that move the base per *file*,
# so no preset value can be right for all of them: 1,013 of 1,080 surveyed
# panels want base 1 and 62 want 0 (``scgcad-formats.md`` §3.3). Published only
# where those bytes have been checked against the corpus.
KEY_TILEMAP_PALETTE_ROW_BASE = "tilemap.palette-row-base"
# tuple[int, int]: how many **cells** of this map one **stamp** covers, for a map
# whose cells are subdivided into stamps a *referring* map indexes. Counted in
# this file's cells — not tiles, and not a cell size: the step at which another
# format's coordinates land, which a referrer multiplies by ``cell_tiles`` when
# it wants tiles (:attr:`~celpix.core.document.Document.stamp_tiles`).
#
# A panel is the one shipped format that states it (header 0x69/0x6A, as
# exponents), and the reader that needs it is the stamp layout bound to it: a
# layout's entry names the stamp's *top-left* cell and the rest of the stamp
# follows from this pair, with the positions between two entries holding nothing
# anyone should draw (``docs/graphics-formats-reference/scgcad-formats.md`` §4).
# Published by the **source** map and read by the referrer where the referrer's
# own format has not declared a ``stamp_cells`` of its own — the two sides of
# one answer: a panel's header divides it for whoever calls, while a metatile
# table's format fixes it for whatever it calls into
# (``ui/main_window/session.py``, ``_chain_stamp_cells``).
KEY_TILEMAP_STAMP_CELLS = "tilemap.stamp-cells"
# int: how many cell *rows* one **page** holds, for a format whose file is several
# independent maps end to end rather than one — a screen file is four 32x32
# screens (``docs/graphics-formats-reference/scgcad-formats.md`` §2). Published
# alongside the width above, which is the page's width: a paged format states
# both or neither, since a page with no stated width has no shape.
#
# What it buys is the **assembly**: the pages have to be laid out into one
# picture, and this is what says there are several to lay out
# (``docs/design/tilemap-entry.md`` §6).
KEY_TILEMAP_PAGE_ROWS = "tilemap.page-rows"
# int: how many pages across, for a format whose assembly is **structural** rather
# than a reading. Published beside the page height above, and answering the
# question that one raises: a screen's four quadrants are one 64x64 tilemap and
# the editor's own loader says so outright — `load_scr` writes the four quadrants
# into a single array at row stride 64
# (``docs/graphics-formats-reference/scgcad-asset-pipeline.md`` §2.7).
#
# Absent — **or zero** — means the format does not state one, which leaves the
# layout the user's to choose. That is the case this pair was first built for and
# it turned out not to be the screen's; it is kept because "several maps end to
# end" and "and here is how they go together" are genuinely two claims, and a
# format may make the first without the second.
#
# The two spellings are the same answer because the readers coerce both, and the
# zero is what lets a codec **state** the absence: one that takes the geometry
# over from whatever framed the bytes has to answer for all three keys, or it
# leaves its own page height beside somebody else's arrangement
# (:mod:`celpix.plugins.builtins.nes_nametable`).
KEY_TILEMAP_PAGES_ACROSS = "tilemap.pages-across"
# "little" | "big": the byte order a container knows its cells are in, where that
# is a property of the *file* rather than of its format. The S-CG-CAD sprite
# object is the case it exists for: 26 of the 1,341 in the corpus come from a
# later build of the tool that stores the attribute word the other way round, and
# they say so in their own header. Overrides the preset's assumption, since the
# file is the better authority; a codec that ignores it is simply reading a
# format where the question never arises.
KEY_TILEMAP_ENDIAN = "tilemap.endian"
# tuple[int, ...]: how many cells each frame of a sprite map holds, for a format
# that stores its frames **variable-length** rather than in fixed slots. Every
# other sprite format gives every frame the same number of subsprite slots and
# marks the unused ones, so a frame is a fixed stride into the cells and no one
# has to be told where the boundaries are. A format that instead counts each
# frame keeps those counts *between* its records, which is structure rather than
# payload — so the container takes them out and states them here, and the codec
# groups by them (:meth:`~celpix.plugins.builtins.object_codec.SprCodec.frames`).
# Absent means fixed slots, which is what ``subsprites_per_frame`` sizes.
KEY_TILEMAP_FRAME_SIZES = "tilemap.frame-sizes"
# int: how many subsprite slots one frame of a **fixed-slot** sprite map holds,
# where the *file* settles it rather than the preset. A sprite object comes in two
# forms carrying the same payload size, and they divide it differently — the
# ordinary one 32 frames of 64, the extended one 64 frames of **128** — so the
# stride cannot be read off the byte count and a preset written for one mis-frames
# the other. Which form a file is is exactly what the container had to decide to
# find the signature at all, so it is the thing that knows
# (``graphics-formats-reference/scgcad-formats.md`` §8.1). Advisory in the usual
# way: absent means the preset's ``subsprites_per_frame`` stands.
KEY_TILEMAP_SUBSPRITES_PER_FRAME = "tilemap.subsprites-per-frame"
# tuple[int, int]: the two subsprite sizes **in tiles** a size bit picks between,
# where the *reader* has settled them. The one thing on this list no file and no
# format knows: the pair was a register the scene set, and the corpus gives no
# way to recover it from the bytes (``scgcad-formats.md`` §8.2). So it is the
# user's, per entry, kept in the project (``Entry.sprite_size_pair``) — and
# published here because the codec is what resolves a record's size bit into a
# rectangle, at decode time, which is well below anything that has heard of an
# entry.
#
# Host-stated rather than container-stated, which makes it the tilemap twin of
# :data:`KEY_DECOMPRESS_PARTIAL`: set on the way *in*, for the plugin to honour,
# rather than reported on the way out. Absent means the format's own answer
# stands, which is what a fresh entry gets and what a codec asked directly gets.
KEY_TILEMAP_SUBSPRITE_TILES = "tilemap.subsprite-tiles"
# tuple[Sequence, ...]: the order a sprite map's frames are meant to play in,
# where the format carries such a table (:mod:`celpix.core.animation`). It lives
# in the part of the file the container preserves opaquely — past the records, and
# past the header — so the container is the only thing that has both the bytes and
# the offsets to read it from; the codec is handed the payload alone and never
# sees it. Absent means the format has no sequences, which is every one but the
# sprite family. Advisory in the strongest sense: nothing renders from it, and a
# save writes the table back from the bytes it was read from rather than from
# this, so a file whose table this misreads still round-trips.
KEY_TILEMAP_ANIMATIONS = "tilemap.animations"
# bool: whether the sequences above are a *reading of the data* rather than a
# spec. One format in hand is — the Yoshi sprite trailer's two 40-byte blocks are
# emitted by their writer as opaque byte arrays, so "the first block is frames and
# the second durations" comes off the corpus and not off the code
# (``graphics-formats-reference/ys-sprite-patterns.md`` §4). Published so a reader
# can say which it is looking at, since a player showing an inferred split as
# confidently as a confirmed one is how a guess becomes a fact by repetition.
# Absent means confirmed, which is the ordinary case.
KEY_TILEMAP_ANIMATIONS_INFERRED = "tilemap.animations-inferred"
# str: the pixel preset a container believes its payload is in, when the format
# says. A tile bank that records its own bit depth should not need it guessed —
# the depths look alike enough that a wrong pick reads as plausible garbage. The
# pixel-pathway twin of a tilemap container's ``default_tilemap_preset``, on the
# context rather than as an attribute because it varies per *file*, not per
# plugin. Advisory: it seeds the format picker and the user owns it after. Set
# by the host on a write into nothing, as ``KEY_PALETTE_PRESET`` is.
KEY_PIXEL_PRESET = "pixel.preset"
# bytes: one palette row per tile, when the format carries a side table of them.
# A tile bank that records which row each tile is meant to be read under is
# saying what pinned palette regions otherwise have to be told by hand, so it
# seeds them (``docs/design/palette-editing.md`` §3). **Relative rows**, counted
# from the key below like every other named row — the host applies the base once,
# at render, so a table that folded it in already would move the art twice.
KEY_TILE_PALETTE_ROWS = "pixel.tile-palette-rows"
# int: the palette row this *bank's* tiles count their own row 0 from, when its
# header says. The pixel-pathway twin of
# :data:`KEY_TILEMAP_PALETTE_ROW_BASE`, and the answer for the formats that
# leave the question open: a sprite object names a 3-bit row and has nowhere to
# put a base, while the bank its subsprites draw from states one outright. So a
# tilemap bound to such a bank takes the base from the art rather than from a
# preset constant, which can only ever be the commonest case
# (``docs/graphics-formats-reference/scgcad-formats.md`` §8.5) — and the bank
# itself opens on it, its own pinned rows counting from the same origin.
KEY_TILE_PALETTE_ROW_BASE = "pixel.palette-row-base"
# tuple[int, int]: the shape of one **pixel** on the hardware this file was drawn
# for, as a width:height ratio (:mod:`celpix.core.aspect`). A 200-line screen at
# 640 across draws a pixel twice as tall as it is wide, so its art is a squashed
# rectangle at 1:1 and nothing in the bytes says so — the machine knows, and a
# container that knows which machine it is reading is the only thing in the
# pipeline that can pass it on.
#
# Unlike its neighbours here this one is **not per entry**: what it seeds is a
# project-wide display setting (``docs/design/pixel-aspect.md``), since a screen
# has one shape and every surface in the window is drawing to it. So it seeds
# and does not govern — the first entry to publish one answers the project's
# question, and the user owns it afterwards, exactly as a tilemap's stated width
# seeds Cols.
KEY_PIXEL_ASPECT = "pixel.aspect"
# A font's alphabet is deliberately **not** a context key. It is the user's own
# project data on the pixels entry, gated by Use as Font
# (``docs/design/fontmap-entry.md`` §4): a container stating one would be read
# whatever that tick said, so the one control over whether a sheet's codes mean
# anything would stop deciding it.
# One more well-known key lives in :mod:`celpix.core.notices` rather than here:
# what a stage wants to *tell the user* without failing. It keeps company with
# the notice type and its helpers, since unlike the scalars above it is only ever
# read or written through them.


# What each well-known key is called when it is *shown to the user*, and what
# acting on it does — the container-info popup lists the hints a container
# published and has to say more than the raw key. Beside the keys rather than in
# the UI so the two cannot drift, and only the ones a **container** can publish:
# the rest are stage-to-stage plumbing with nothing for a reader to act on.
#
# The second string is a tooltip, so it follows the tooltip rule: hard-wrapped at
# ~60 characters, one clause per line (Qt never wraps a plain-text tooltip).
HINT_INFO: dict[str, tuple[str, str]] = {
    KEY_SOURCE_OFFSET: (
        "Payload offset",
        "Where in the file the bytes this entry shows begin.\n"
        "Every address in the view and every slice carved from\n"
        "it is anchored here.",
    ),
    KEY_TILEMAP_COLUMNS: (
        "Map width",
        "How many cells across the map is laid out at.\n"
        "Fixed by the format; without it the width is a guess,\n"
        "and a wrong one shears the picture into diagonal stripes.",
    ),
    KEY_TILEMAP_CELL_TILES: (
        "Cell size",
        "How many tiles one cell covers, when the header says.\n"
        "Read small, a metatile map draws one quarter of every\n"
        "cell and drops the rest.",
    ),
    KEY_TILEMAP_PALETTE_ROW_BASE: (
        "Palette row base",
        "The palette row this map's own row 0 counts from, as\n"
        "its header states it. Read as 0 when the file says 1,\n"
        "every tile draws through the wrong sixteen colours.",
    ),
    KEY_TILEMAP_STAMP_CELLS: (
        "Stamp size",
        "How many cells one stamp covers, for a map that another\n"
        "map's coordinates index in stamps. Read by the layout\n"
        "bound to this one, not by this one.",
    ),
    KEY_TILEMAP_PAGE_ROWS: (
        "Page height",
        "The file is several independent maps end to end, this\n"
        "many cell rows each, assembled into one picture.",
    ),
    KEY_TILEMAP_PAGES_ACROSS: (
        "Pages across",
        "How the pages above go together, where the format\n"
        "settles it rather than leaving it to be read.",
    ),
    KEY_TILEMAP_ENDIAN: (
        "Cell byte order",
        "The byte order this file's cells are in, where the file\n"
        "itself states it. Overrides the format preset, the file\n"
        "being the better authority about its own bytes.",
    ),
    KEY_TILEMAP_FRAME_SIZES: (
        "Frame sizes",
        "How many subsprites each frame holds, for a format that\n"
        "counts them rather than giving every frame the same\n"
        "number of slots. Read off the file's own counts.",
    ),
    KEY_TILEMAP_SUBSPRITES_PER_FRAME: (
        "Subsprites per frame",
        "How many subsprite slots one frame holds, where the file\n"
        "settles it rather than the format preset. The two sizes\n"
        "of sprite object divide the same payload differently.",
    ),
    KEY_TILEMAP_ANIMATIONS: (
        "Animation sequences",
        "The order this file says its frames play in, read from\n"
        "the table past its records. Shown here and in the\n"
        "animation player; nothing is drawn or written from it.",
    ),
    KEY_TILEMAP_ANIMATIONS_INFERRED: (
        "Animation layout inferred",
        "Whether the sequences above are a reading of the data\n"
        "rather than a spec. One format's writer emits its\n"
        "animation blocks opaquely, so their split is read off\n"
        "the corpus and shown as a guess, not a fact.",
    ),
    KEY_PIXEL_PRESET: (
        "Pixel format",
        "The graphics format the container believes its payload\n"
        "is in, from the file's own header. It seeds the format\n"
        "picker; you own the choice after that.",
    ),
    KEY_TILE_PALETTE_ROWS: (
        "Per-tile palette rows",
        "A side table naming the palette row each tile is meant\n"
        "to be read under, which is what pinned palette regions\n"
        "otherwise have to be told by hand.",
    ),
    KEY_TILE_PALETTE_ROW_BASE: (
        "Palette row base",
        "The palette row this bank's tiles count their own row 0\n"
        "from, as its header states it. A tilemap bound to the\n"
        "bank counts from here too, unless its own file says.",
    ),
    KEY_PIXEL_ASPECT: (
        "Pixel aspect",
        "The shape of one pixel on the machine this file was\n"
        "drawn for, when the container knows which machine that\n"
        "is. It seeds View > Pixel Aspect, which is a setting for\n"
        "the whole project; you own the choice after that.",
    ),
    KEY_PALETTE_PRESET: (
        "Color format",
        "The color encoding the container read out of the file's\n"
        "own header. Almost no palette file states one, so where\n"
        "this appears the format is a fact rather than the usual\n"
        "guess. It does not change the dock's picker on its own.",
    ),
    KEY_PALETTE_ERROR: (
        "Palette read error",
        "The colors shown are a placeholder, not the file's:\n"
        "decoding them the chosen way failed. Correct the format\n"
        "in the palette dock to read the real ones.",
    ),
}


def hint_info(key: str) -> tuple[str, str]:
    """``(label, what it means)`` for a context key; the bare key when unknown.

    A plugin may define keys of its own, so the fallback shows the key itself
    rather than hiding a hint nobody has written a label for — an unexplained row
    is still evidence that something was published.
    """
    return HINT_INFO.get(key, (key, ""))


class PipelineContext:
    """An open key/value bag of advisory recommendations, per pathway."""

    __slots__ = ("_entries",)

    def __init__(self) -> None:
        self._entries: dict[str, Any] = {}

    def set(self, key: str, value: Any) -> None:
        self._entries[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        return self._entries.get(key, default)

    def items(self) -> dict[str, Any]:
        """Everything recorded here, for a caller that has to enumerate the bag.

        A copy, so a reader cannot write through it: the bag is open and stages
        add to it in order, which is a contract the enumerating side has no part
        in. What needs it is the container-info popup — "what did this container
        publish" has no answer that consults keys one at a time, since a plugin
        may have defined its own.
        """
        return dict(self._entries)

    def __repr__(self) -> str:
        return f"PipelineContext({sorted(self._entries)})"
