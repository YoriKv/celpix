"""S-CG-CAD containers — SCR/PNL/MAP/OBJ/OBZ/STD maps, CGX tiles, COL palettes.

One SNES-era authoring tool's file family, plus the two files its pipeline made
*out* of them (OBZ, STD). Each member is a fixed-size file: a payload, and — for
everything the tool itself reopens — a 0x100-byte metadata block carrying a
32-byte ASCII signature. Byte-exact specs, the confidence level behind each
claim, and the corpus they were verified against are in
``docs/graphics-formats-reference/scgcad-formats.md``.

What makes these containers rather than codecs is that each one **frames** its
payload: it has to be cut out of the file, and everything around it — header,
trailer, a PNL panel's second table — has to come back unchanged on write. The
payload is then read by the ordinary codec for what it holds, which is why the
two cell byte orders in this family cost a preset each rather than a plugin each.

They do not all frame the same *kind* of thing: a COL is a palette, a CGX is
pixels, and the other six are tilemaps. The COL says so in
``PluginInfo.content_kinds``, which keeps the palette pathway from being offered
a screen's container and the other pathways from being offered a palette's; the
tilemap members say so with ``default_tilemap_preset``, which is also what picks
an opened file's first cell codec.

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
not padding. A screen's 0x200 trailer and a PNL panel's 0x8000 second table are
both **visibility tables** — a per-cell "draw this / don't", which cannot be
derived from the tile data and would be destroyed by regenerating it. They sit
outside the payload the cell codec is handed, so they are not read into
``Cell.visible``; they ride through untouched (``scgcad-formats.md`` §2.1, §3.2).

The members live in three modules by what they frame — :mod:`.screens` (SCR, PNL,
MAP, STD), :mod:`.objects` (OBJ, OBZ) and :mod:`.banks` (COL, CGX) — over the
signature, metadata block and depth arithmetic they share in :mod:`._common`.
Every public name is re-exported here, so ``scgcad.X`` reads as it always has.
"""

from __future__ import annotations

from ._common import (
    HEADER,
    SIGNATURE,
)
from .banks import (
    CGX_BANKS,
    CGX_BY_PAYLOAD,
    CGX_COL_CELL,
    CGX_COL_HALF,
    CGX_DEPTH,
    CGX_ROW_TABLE,
    COL_HEADER_AT,
    COL_PAYLOAD,
    COL_SIZE,
    CgxContainer,
    ColContainer,
)
from .objects import (
    OBJ_PAYLOADS,
    OBJ_SEQUENCE_STEPS,
    OBJ_SEQUENCES,
    OBJ_SIZE,
    OBJ_SIZES,
    OBJ_SUBSPRITES_PER_FRAME,
    OBJECT_COLUMNS,
    OBZ_PAYLOAD,
    OBZ_SEQUENCE_STEPS,
    OBZ_SEQUENCES,
    OBZ_SIZE,
    OBZ_TABLE,
    SUBSPRITE_RECORD,
    ObjContainer,
    ObzContainer,
)
from .screens import (
    MAP_COLUMNS,
    MAP_PAYLOAD,
    MAP_SIZE,
    PANEL_COLUMNS,
    PNL_COL_CELL,
    PNL_COL_HALF,
    PNL_SIZE,
    PNL_STAMP_EXPONENTS,
    PNL_STAMP_MAX_EXPONENT,
    PNL_TABLE,
    SCR_BASE_WORD,
    SCR_COL_CELL,
    SCR_COL_HALF,
    SCR_DEPTH,
    SCR_HEADER_AT,
    SCR_PAYLOAD,
    SCR_SIZE,
    SCR_TILE_SIZE,
    SCREEN_COLUMNS,
    SCREEN_PAGES_ACROSS,
    SCREEN_ROWS,
    STD_COLUMNS,
    STD_SIZE,
    MapContainer,
    PnlContainer,
    ScrContainer,
    StdContainer,
)

__all__ = [
    "CGX_BANKS",
    "CGX_BY_PAYLOAD",
    "CGX_COL_CELL",
    "CGX_COL_HALF",
    "CGX_DEPTH",
    "CGX_ROW_TABLE",
    "COL_HEADER_AT",
    "COL_PAYLOAD",
    "COL_SIZE",
    "CgxContainer",
    "ColContainer",
    "HEADER",
    "MAP_COLUMNS",
    "MAP_PAYLOAD",
    "MAP_SIZE",
    "MapContainer",
    "OBJECT_COLUMNS",
    "OBJ_PAYLOADS",
    "OBJ_SEQUENCES",
    "OBJ_SEQUENCE_STEPS",
    "OBJ_SIZE",
    "OBJ_SIZES",
    "OBJ_SUBSPRITES_PER_FRAME",
    "OBZ_PAYLOAD",
    "OBZ_SEQUENCES",
    "OBZ_SEQUENCE_STEPS",
    "OBZ_SIZE",
    "OBZ_TABLE",
    "ObjContainer",
    "ObzContainer",
    "PANEL_COLUMNS",
    "PNL_COL_CELL",
    "PNL_COL_HALF",
    "PNL_SIZE",
    "PNL_STAMP_EXPONENTS",
    "PNL_STAMP_MAX_EXPONENT",
    "PNL_TABLE",
    "PnlContainer",
    "SCREEN_COLUMNS",
    "SCREEN_PAGES_ACROSS",
    "SCREEN_ROWS",
    "SCR_BASE_WORD",
    "SCR_COL_CELL",
    "SCR_COL_HALF",
    "SCR_DEPTH",
    "SCR_HEADER_AT",
    "SCR_PAYLOAD",
    "SCR_SIZE",
    "SCR_TILE_SIZE",
    "SIGNATURE",
    "STD_COLUMNS",
    "STD_SIZE",
    "SUBSPRITE_RECORD",
    "ScrContainer",
    "StdContainer",
]
