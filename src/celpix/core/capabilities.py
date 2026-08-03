"""What kind of thing an entry holds, and which controls that kind supports.

Two questions the editor has to answer about every open entry, kept apart from
each other and from the entry's *bounding* (whole file, slice, bookmark — see
:class:`~celpix.project.workspace.EntryKind`):

- :class:`ContentKind` — pixels, a tilemap, or a palette. What the bytes *are*.
- :class:`Capability` — one thing the editor can do to a document. A content kind
  declares the set it supports, and a control declares the one it needs.

Before this existed, every control decided for itself whether it applied, inside
whichever ``_sync_*`` method owned it. That works while there is one kind of
document; with two, the answer for any given control is spread across a dozen
methods and the set a tilemap supports is knowable only by reading all of them.
:data:`CAPABILITIES` is that answer written down once
(``docs/design/tilemap-entry.md`` §4).

A capability is a tag on the *control*, not an implementation. Several controls
exist on more than one content kind and mean different things on each — flipping
a tile rewrites pixels on a pixel document and toggles an attribute bit on a
tilemap — so gating and dispatch are separate concerns and only the first lives
here. Qt-free, so the table is testable without a window.
"""

from __future__ import annotations

from enum import Enum, auto


class ContentKind(str, Enum):
    """What an entry's bytes are, independent of how the entry is bounded.

    ``value`` is the string persisted in the project file (str-valued for that
    reason, like :class:`~celpix.core.errors.Stage`), so the on-disk schema is a
    name rather than an ordinal that reordering this enum would silently change.

    ``PIXELS`` is the default and is **omitted** when a project is written, so
    every project that predates tilemaps loads unchanged and an ordinary pixel
    entry costs nothing.
    """

    PIXELS = "pixels"
    TILEMAP = "tilemap"
    PALETTE = "palette"

    @classmethod
    def parse(cls, value: object) -> ContentKind:
        """``value`` as a content kind, falling back to ``PIXELS``.

        Tolerant on the same rule as the rest of the project reader: an unknown
        kind — a newer celPix's, or a hand-edited typo — opens the entry as
        pixels rather than failing the load.
        """
        try:
            return cls(value)
        except ValueError:
            return cls.PIXELS


class Capability(Enum):
    """One thing the editor can do to a document, as a gate on its controls."""

    # -- pixel-side editing
    PIXEL_EDIT = auto()  # the tools panel: pen, fill, the pixel selection
    PALETTE_EDIT = auto()  # the color editor and eyedropper
    PALETTE_REGIONS = auto()  # pinned subpalette rows over spans of the picture
    TILE_REARRANGE = auto()  # the display-only permutation (tile-rearrange.md)

    # -- shared, but implemented per content kind
    PALETTE_ROW = auto()  # give the selection a named subpalette row of its own
    TILE_SELECT = auto()  # select a tile or a rectangle of them
    CLIPBOARD = auto()  # copy / cut / paste
    CELL_FLIP = auto()  # mirror a tile or a block of them
    CELL_ROTATE = auto()  # quarter-turn a tile or a block of them
    EXPORT_IMAGE = auto()  # render out to PNG
    IMPORT_IMAGE = auto()  # bring an image in (paste, Import from PNG)

    # -- interpretation and navigation
    PIXEL_CODEC = auto()  # the bit-depth / pixel-format picker
    TILEMAP_CODEC = auto()  # cell width, field layout, endianness
    PALETTE_CODEC = auto()  # the color-format picker
    NAVIGATION = auto()  # offset, nudge, the address bar
    GRID = auto()  # the grid overlay settings
    HEX_VIEW = auto()  # the hex panel
    COMPRESSION_SCAN = auto()  # structure scanning over the entry's bytes

    # -- tilemap-only
    TILE_BINDING = auto()  # choose where this tilemap's tiles come from
    STAMP = auto()  # place a cell from the bound tile source
    CELL_LABELS = auto()  # number each cell with the tile it names


# Everything that is about bytes rather than about pixels: an entry of any kind
# is a window on a file, so it shows a grid and a hex view whatever it holds.
#
# NAVIGATION is deliberately *not* here. It is about moving a view window
# through a file, and only a document that has one can do that. Nor is
# COMPRESSION_SCAN, for the same reason one step removed: the scan hunts for a
# structure *in the current window* and previews it as tiles, so a document with
# no window and no tiles of its own has nothing to point it at.
_BYTE_LEVEL = frozenset(
    {
        Capability.GRID,
        Capability.HEX_VIEW,
    }
)

CAPABILITIES: dict[ContentKind, frozenset[Capability]] = {
    ContentKind.PIXELS: _BYTE_LEVEL
    | {
        Capability.COMPRESSION_SCAN,
        Capability.NAVIGATION,
        Capability.PIXEL_EDIT,
        Capability.PALETTE_EDIT,
        Capability.PALETTE_REGIONS,
        Capability.PALETTE_ROW,
        Capability.TILE_REARRANGE,
        Capability.TILE_SELECT,
        Capability.CLIPBOARD,
        Capability.CELL_FLIP,
        Capability.CELL_ROTATE,
        Capability.EXPORT_IMAGE,
        Capability.IMPORT_IMAGE,
        Capability.PIXEL_CODEC,
        Capability.PALETTE_CODEC,
    },
    # A tilemap carries its own palette, so it edits colors and picks a color
    # format like any other entry (`docs/design/tilemap-entry.md` §3).
    #
    # PIXEL_EDIT is here because a tilemap's `pixel_data` **is** the bound entry's
    # art: a canvas position resolves through the cell it lands in to a tile of
    # that bank, so the pixels under the cursor are real and editable. Where they
    # are *deposited* is the bound entry rather than the map, which is the
    # capability's one wrinkle and not something a gate could express — the kind
    # can say a brush belongs here, only the document can say whether this
    # particular map has a bank to paint into (`docs/design/tilemap-entry.md` §8.1;
    # `_pixel_edit_available` is the finer answer).
    #
    # Five deliberate absences. PALETTE_REGIONS: a cell
    # already names its own palette row, so pinning a row over a span would be a
    # second, conflicting answer to a question the file has already answered —
    # which is why PALETTE_ROW is here instead, the same gesture landing in the
    # cells the file already answers with (`docs/design/palette-editing.md` §4).
    # TILE_REARRANGE: a rearrangement is display state precisely because it moves
    # no bytes, and moving a cell *is* the byte edit. IMPORT_IMAGE: bringing a
    # picture in would mean matching it against the bound tiles, which is a
    # quantize-to-tiles problem and not the pixel importer's — and unlike a brush
    # it has no cell under it to say which tile a given pixel belongs to.
    #
    # NAVIGATION is the fourth: a tilemap is always shown entire, so there is no
    # window to move through it and no offset to jump to. The row count and the
    # position bar address a coordinate space it does not have
    # (``docs/design/tilemap-entry.md`` §8). COMPRESSION_SCAN follows it out for
    # the same reason — the scan reads the current window and previews it as
    # tiles — and takes the compression picker with it, leaving the cell format
    # in the place the pixel format has on a pixel entry.
    #
    # CELL_ROTATE is the one that justifies splitting the transforms in two: a
    # hardware cell carries mirror bits and no transpose bit, so a tilemap can be
    # flipped and cannot be turned. One CELL_TRANSFORM capability would have had
    # to lie about one of the two.
    #
    # CELL_LABELS is tilemap-only because the label answers a question only a
    # tilemap has. A cell *names* a tile that lives somewhere else, and which one
    # is not recoverable by looking; a pixel document's tile has no name to show
    # — its position in the file is its identity, and the position bar already
    # says that.
    ContentKind.TILEMAP: _BYTE_LEVEL
    | {
        Capability.PIXEL_EDIT,
        Capability.PALETTE_EDIT,
        Capability.PALETTE_ROW,
        Capability.TILE_SELECT,
        Capability.CLIPBOARD,
        Capability.CELL_FLIP,
        Capability.CELL_LABELS,
        Capability.EXPORT_IMAGE,
        Capability.TILEMAP_CODEC,
        Capability.PALETTE_CODEC,
        Capability.TILE_BINDING,
        Capability.STAMP,
    },
    # A palette entry is applied to whichever entry is on screen rather than
    # activated, so it has no view of its own to navigate, scan or edit in.
    ContentKind.PALETTE: frozenset({Capability.PALETTE_CODEC}),
}


def supports(kind: ContentKind, capability: Capability) -> bool:
    """Whether ``kind`` supports ``capability`` — the one gate every control asks.

    Named rather than left as a set lookup at each call site so the question is
    spelled the same way everywhere, and so an unknown kind is a clean ``False``
    instead of a ``KeyError`` in the middle of a UI sync pass.
    """
    return capability in CAPABILITIES.get(kind, frozenset())
