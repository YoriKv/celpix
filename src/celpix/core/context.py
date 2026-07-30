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
# What a tilemap container read out of its file's *own* header, for the view to
# start from. Advisory like everything here: each is a setting the user can then
# change, and a format that states none of them is simply read without hints.
#
# int: how many cells across the map is, when the format fixes it — a screen and
# a panel are 32, a stamp layout 128. Without it the width is a guess the user
# has to make, and a wrong guess shears the picture into diagonal stripes rather
# than failing, which is the worst way to be wrong.
KEY_TILEMAP_COLUMNS = "tilemap.columns"
# tuple[int, int]: how many tiles one cell covers, when the header says. A screen
# carries this as a tile-size byte and most real ones are 16x16, so a screen
# whose cells are metatiles reads correctly instead of drawing as a mosaic of
# unrelated tiles. Only published where the field survives being checked against
# the corpus — a panel has three bytes shaped like this one and none of them is
# it (``docs/graphics-formats-reference/scgcad-formats.md`` §3.1).
KEY_TILEMAP_CELL_TILES = "tilemap.cell-tiles"
# int: how many cell *rows* one **page** holds, for a format whose file is several
# independent maps end to end rather than one — a screen file is four 32x32
# screens (``docs/graphics-formats-reference/scgcad-formats.md`` §2). Published
# alongside the width above, which is the page's width: a paged format states
# both or neither, since a page with no stated width has no shape.
#
# What it buys is the **assembly**: nothing in the file records how those four
# make up a larger screen, so laying them out is the user's choice and this is
# what says there is a choice to make (``docs/design/tilemap-entry.md`` §6).
KEY_TILEMAP_PAGE_ROWS = "tilemap.page-rows"
# "little" | "big": the byte order a container knows its cells are in, where that
# is a property of the *file* rather than of its format. The S-CG-CAD sprite
# object is the case it exists for: 26 of the 1,341 in the corpus come from a
# later build of the tool that stores the attribute word the other way round, and
# they say so in their own header. Overrides the preset's assumption, since the
# file is the better authority; a codec that ignores it is simply reading a
# format where the question never arises.
KEY_TILEMAP_ENDIAN = "tilemap.endian"
# str: the pixel preset a container believes its payload is in, when the format
# says. A tile bank that records its own bit depth should not need it guessed —
# the depths look alike enough that a wrong pick reads as plausible garbage. The
# pixel-pathway twin of a tilemap container's ``default_tilemap_preset``, on the
# context rather than as an attribute because it varies per *file*, not per
# plugin. Advisory: it seeds the format picker and the user owns it after.
KEY_PIXEL_PRESET = "pixel.preset"
# bytes: one palette row per tile, when the format carries a side table of them.
# A tile bank that records which row each tile is meant to be read under is
# saying what pinned palette regions otherwise have to be told by hand, so it
# seeds them (``docs/design/palette-editing.md`` §3).
KEY_TILE_PALETTE_ROWS = "pixel.tile-palette-rows"
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
    KEY_TILEMAP_PAGE_ROWS: (
        "Page height",
        "The file is several independent maps end to end, this\n"
        "many cell rows each. How they assemble into one larger\n"
        "picture is yours to choose - the file does not say.",
    ),
    KEY_TILEMAP_ENDIAN: (
        "Cell byte order",
        "The byte order this file's cells are in, where the file\n"
        "itself states it. Overrides the format preset, the file\n"
        "being the better authority about its own bytes.",
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
