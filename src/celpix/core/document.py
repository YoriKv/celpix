"""The interpreted session model the UI binds to and Write serializes.

A :class:`Document` is the point where the two pathways converge (overview.md §2):
the **pixel bytes** (decompressed, decoded on demand a window at a time), the
**palette**, the **view options**, and the two pathway configs + contexts needed to
round-trip. It is Qt-free and mutable: the editing tools act on it in place — pixel
edits splice bytes (:meth:`Document.replace_bytes`), color edits swap in a new
:class:`~celpix.core.palette.Palette`.

**Deferred decoding.** Large files are never decoded whole: the document holds the
raw pixel bytes plus the codec's atomic geometry (bytes/tile, tile pixel size), and
the view decodes only the visible window of tiles on demand (via
``pipeline.decode_window``). The bytes are the source of truth — an edit encodes the
changed tiles back into them (``pipeline.encode_tiles``) and Write compresses and
writes the buffer as it stands.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from celpix.core import ceil_div
from celpix.core.context import (
    KEY_SOURCE_OFFSET,
    KEY_TILEMAP_ANIMATIONS,
    KEY_TILEMAP_ANIMATIONS_INFERRED,
    KEY_TILEMAP_COLUMNS,
    KEY_TILEMAP_PAGE_ROWS,
    KEY_TILEMAP_PAGES_ACROSS,
    PipelineContext,
)
from celpix.core.font import Alphabet
from celpix.core.palette import Palette, palette_row_count
from celpix.core.paletteregions import PaletteRegions
from celpix.core.sprite import DEFAULT_SUBSPRITE_TILES, drawn_frames
from celpix.core.tilemap import (
    Cell,
    expand_stamps,
    page_assemblies,
    page_order,
    resolve_cell,
    resolve_pages_across,
    stamp_origin,
    tile_run,
)
from celpix.core.tilerearrangement import TileRearrangement
from celpix.pipeline.pathway import PathwayConfig
from celpix.plugins.base import FileRef


class GridMode(str, Enum):
    """Which scale the canvas's grid is drawn at, when it is shown at all.

    The grid always has two levels — a fine one and a stronger one a step up
    from it — and this picks what the fine one counts. It is **one setting for
    the whole project**, not per entry: it says how the user wants to look at
    pixels, which does not change from one file to the next, so it rides on the
    :class:`~celpix.project.workspace.Workspace` beside the pixel-format filter
    rather than in :class:`ViewOptions`. ``value`` is the stable string
    persisted in the project file (str-valued for exactly that reason, like
    :class:`~celpix.project.workspace.PaletteMode`).

    - ``TILE`` — fine on every tile, coarse on the 8×8-tile square.
    - ``PIXEL`` — fine on every image pixel, coarse on every tile. Only useful
      zoomed in, and the canvas drops the pixel level again when the zoom is too
      low for it to read as a lattice rather than a wash.

    ``Workspace.block_grid`` moves the coarse level of *either* onto the
    arrangement's own block, and ``Workspace.show_grid`` is the on/off switch
    over both — kept apart from the scale so that turning the grid off and on
    again brings back the scale it was last read at.
    """

    TILE = "tile"
    PIXEL = "pixel"

    @classmethod
    def parse(cls, value: object, default: GridMode) -> GridMode:
        """``value`` as a mode, falling back to ``default`` for anything else."""
        try:
            return cls(value)
        except ValueError:
            return default


@dataclass
class ViewOptions:
    """How the tiles are laid out and rendered — pure display state.

    ``subpalette_row`` selects which ``2^bpp`` window of a larger palette a tile
    renders through (``base = row * 2**bpp`` — the pixel format's index space
    sizes the subpalette); the sample ``.pal``s are 256-color CGRAM dumps, so
    this matters even for viewing.

    Large files are viewed through a fixed **window**: ``rows`` tile-rows starting
    at tile ``tile_offset`` (the top-left corner of the view). Navigation moves
    ``tile_offset`` — ±``columns`` for a row step, ±1 for a tile step — instead of
    free-scrolling the whole file, so only the window is ever decoded/rendered.

    ``byte_nudge`` shifts the whole tile grid forward that many bytes
    (``0 <= nudge < bytes_per_tile``), so graphics that don't start on a tile
    boundary can be aligned; tile navigation stays in whole tiles on the nudged
    grid.

    The **arrangement** axes are pure display placement/addressing (overview.md
    §4). ``block_columns`` × ``block_rows`` group tiles into blocks (default 1×1 =
    plain row-major); ``block_order`` fills each block row-major, column-major
    (Mega Drive / Neo Geo sprites), or row-interleaved (8×16 sprite sheets) — see
    :data:`~celpix.core.arrangement.BLOCK_ORDERS`. ``two_dimensional`` reads the
    source as one wide bitmap ``columns`` tiles across instead of back-to-back
    tiles — a different byte walk applied before decode (arrangement's ``reflow_2d``).

    ``bitmap_width`` (0 = off) says the data is a bitmap that many pixels wide.
    It belongs to ``two_dimensional`` and applies only with it: the codec's tile
    size is replaced by the largest divisor of the width that still fits inside
    it (:func:`~celpix.core.arrangement.bitmap_tile_size`) so whole tiles span
    the bitmap, and ``columns`` follows as the count that does. It is display
    state like the rest of these, but unlike them it changes the codec's
    geometry — so it is resolved on the load path, and only for codecs whose
    tile size is a parameter at all.

    ``palette_regions`` pins regions of the picture to their own subpalette row,
    overriding ``subpalette_row`` for the tiles inside them — so a bank whose art
    is drawn under several hardware palettes can be read at once instead of one
    group at a time. A pinned row is a **named** row like a cell's, so it counts
    from :attr:`Document.palette_row_base` rather than being absolute. It changes
    no bytes and no indices: the row reaches the
    screen as a shift applied to the *rendered* indices only
    (:mod:`celpix.core.paletteregions`). ``show_palette_regions`` is the toggle
    between that and the plain single-row view, like ``show_rearranged`` — but
    unlike it, a **local preference** the UI carries app-wide rather than entry
    state: it is here because rendering and export read the view options as one
    bundle, and it is not written to the project file.

    ``tile_rearrangement`` rearranges *which* tile each position shows, so
    scattered tiles can be viewed and edited side by side; it moves no bytes, and
    an edit made at a rearranged position still writes back to the tile's real
    home (:mod:`celpix.core.tilerearrangement`). ``show_rearranged`` is the
    toggle between that
    view and the file's true order — off makes the map inert without discarding
    it. The map composes *before* the block placement: it decides which tile
    fills a slot, the arrangement decides where that slot lands.

    ``pages_across`` is how many of a paged tilemap's independent maps
    (:attr:`Document.pages`) lie side by side, the rest following in bands below
    (:func:`~celpix.core.tilemap.page_order`). A **passthrough**: no control sets
    it, because every paged format celPix reads states its own assembly and
    :attr:`Document.pages_across` takes that answer over this one. It is here so
    a project carrying one round-trips, and so a format that holds pages without
    stating their layout has somewhere for a stored choice to live. Display-only
    in the same sense as the axes above — the cells keep the file's order, only
    where each is drawn moves — and an assembly owns ``columns`` while it
    applies, since an assembly *is* a width
    (:attr:`Document.assembled_columns`).
    """

    columns: int = 16
    rows: int = 16
    # Screen pixels per image pixel. A float for the sake of the one reducing
    # level the view offers (a half-size read of a file too big for the window);
    # every other level is a whole number.
    zoom: float = 4.0
    subpalette_row: int = 0
    tile_offset: int = 0  # top-left tile index into the pixel bytes
    byte_nudge: int = 0  # sub-tile byte shift of the whole grid
    block_columns: int = 1  # tiles per block, horizontally
    block_rows: int = 1  # tiles per block, vertically
    block_order: str = "row"  # fill within a block: row | column | row-interleave
    two_dimensional: bool = False  # read the source as a wide bitmap, not tiles
    bitmap_width: int = 0  # pixels across the bitmap is (0 = the codec's own tiles)
    tile_rearrangement: TileRearrangement = (
        TileRearrangement()
    )  # display-only rearrangement of tile positions
    show_rearranged: bool = (
        True  # apply tile_rearrangement, or show the file's true order
    )
    # Byte regions pinned to their own subpalette row, overriding subpalette_row
    # for the tiles inside them (:mod:`celpix.core.paletteregions`).
    palette_regions: PaletteRegions = PaletteRegions()
    show_palette_regions: bool = True  # apply them, or render everything at the row
    # Whether a named row the palette row base pushes past either end of the
    # palette **wraps** to the other end, or stops at the palette's first row
    # (:attr:`Document.palette_row_base`). Off by default: the file's rows and
    # the palette usually line up, and there a wrap can only turn a base that is
    # wrong into art drawn through a row that looks plausible — where stopping
    # short leaves the mismatch visible. On, for reading a file against a palette
    # holding a different slice of CGRAM than it was authored for. A **local
    # preference** like ``show_palette_regions`` above, here for the same reason:
    # rendering and export read the view options as one bundle.
    wrap_palette_rows: bool = False
    # How many **pages** a paged tilemap lays across (:attr:`Document.pages`), and
    # so how its independent maps assemble into one picture: a screen file's four
    # 32x32 screens read 1x4, 2x2 or 4x1. 0 means nothing has chosen, which lands
    # on the squarest arrangement the page count admits. Read only where no format
    # has stated an assembly, and never written by a control — see the docstring.
    # Meaningless on every other kind of document, where it stays 0. Display-only
    # like the block axes above — the cells keep the file's own order and only
    # where each is drawn moves.
    pages_across: int = 0
    # A **sprite map**'s counterpart of the two toggles above: show every frame
    # slot the file has room for, or stop after the last one holding a drawn
    # subsprite (:func:`~celpix.core.sprite.drawn_frames`). Off by default,
    # because most of a file's slots are empty and a mostly blank sheet buries
    # the sprite at the top of it; on, for reading what the file holds past that
    # point. Display-only in the same sense as the rest of these — the records
    # are untouched either way, and neither reading moves a byte.
    show_all_frames: bool = False
    # Draw palette index 0 as nothing rather than as the colour sitting there.
    # On the console index 0 of a BG palette row is **transparent** — the backdrop
    # shows through — so a tile whose pixels are all 0 occupies space and paints
    # none of it. That is how these formats say "empty": a screen's blank cell
    # names a real tile number whose art happens to be a run of zero bytes
    # (``docs/graphics-formats-reference/scgcad-formats.md``), and drawing it
    # opaque turns a third of a map into a flat slab of whichever colour row 0
    # holds. Off by default because index 0 is an ordinary colour to edit and
    # hiding it wholesale would be the wrong default for a pixel document; the
    # tilemap bar is where it can be asked for, on the entry it answers for.
    transparent_zero: bool = False


@dataclass(frozen=True)
class CellChain:
    """The tilemap a chained map's cells are coordinates into, and how to read it.

    Held on the document rather than looked up per edit, which is what makes
    resolution a **model** operation: a restamp rebuilds
    :attr:`Document.resolved_cells` from what is already here
    (:meth:`Document.resolve`), with no workspace and no reload, so the new stamp
    is on screen as soon as the cell changes.

    ``source`` is the other map's cell list as it stood when this one was bound.
    Editing *that* map replaces its list rather than mutating it, so the host
    re-points the chain of anything drawing through it
    (``docs/design/tilemap-entry.md`` §3.1) — a snapshot that silently aged would
    be worse than one that is refreshed on the one event that invalidates it.

    ``carry_rows`` is the *referring* format's answer to
    :meth:`~celpix.plugins.base.TilemapCodecPlugin.has_palette_rows`, which is not
    the same question as :attr:`Document.cells_carry_palette_rows` — that one is
    true if either side of the chain states rows, because it gates the view's
    subpalette. This one says whose row wins per cell.

    ``stamp`` and ``source_columns`` are the **source's** shape, not the
    referrer's: how many source cells one coordinate names, and how wide the
    source is, which is the step between that stamp's rows
    (:func:`~celpix.core.tilemap.expand_stamps`). Both live here because both are
    the source map's answers — a panel states its stamp size in its own header and
    the layout's file does not know it, so the same layout draws differently
    against a differently divided panel. ``(1, 1)`` is the ordinary chain, where
    one coordinate names one cell and there is no stamp to expand.
    """

    source: list[Cell]
    carry_rows: bool = True
    stamp: tuple[int, int] = (1, 1)
    source_columns: int = 0


@dataclass
class Document:
    pixel_data: bytes  # raw, decompressed pixel bytes — the whole file
    bytes_per_tile: int  # codec geometry, for slicing/indexing the bytes by tile
    tile_width: int
    tile_height: int
    palette: Palette
    pixel_config: PathwayConfig
    palette_config: PathwayConfig
    pixel_ctx: PipelineContext = field(default_factory=PipelineContext)
    palette_ctx: PipelineContext = field(default_factory=PipelineContext)
    view: ViewOptions = field(default_factory=ViewOptions)
    # The palette exactly as it was read, plus which entries have been edited
    # since. Together these make a palette save **splice** rather than rewrite:
    # a color codec is not a bijection over its bytes — bits outside its masks
    # (BGR555's bit 15), and byte values that aren't valid entries at all (an
    # out-of-range indexed color), do not survive decode+encode. Re-encoding a
    # whole palette to save one edited color would therefore corrupt every
    # other entry, so Write reuses these original bytes for anything the user
    # did not touch (docs/design/palette-editing.md §2).
    palette_base_bytes: bytes = b""
    palette_edits: set[int] = field(default_factory=set)
    # The palette row a **named** row 0 means, for every kind of document: a
    # cell's row, a subsprite's, and a pinned region's on a pixel bank
    # (:mod:`celpix.core.paletteregions`). Each of those is a small number
    # relative to wherever its layer's colours were loaded, and only the file it
    # came from knows where that is: a console BG entry's 3-bit row counts from
    # CGRAM row 0, while a sprite's identical 3-bit field counts from row 8,
    # because sprite palettes live in the upper half
    # (``docs/graphics-formats-reference/snes-hardware-notes.md`` §6). Without it
    # a sprite draws through the background's colours — which is not a wrong
    # shade, it is the wrong sixteen colours.
    #
    # The base **in force**, which is what the file said until the user says
    # otherwise on the palette dock: the palette actually loaded need not be the
    # whole of CGRAM, and a sprite read against a palette file holding only the
    # object half counts from row 0 again. Signed for that reason
    # (:attr:`~celpix.project.workspace.Entry.palette_row_base`), and the render
    # wraps a row it pushes past either end
    # (:func:`~celpix.pipeline.pipeline.drawn_palette_row`).
    #
    # It never touches the view's own subpalette row, which is a row the user
    # picked in the palette that is loaded and so already absolute.
    palette_row_base: int = 0

    # -- the tilemap half (``docs/design/tilemap-entry.md``) -----------------
    # Set only on a tilemap document, and what :attr:`is_tilemap` tests. The
    # cells are the *entry's own* file; ``pixel_data`` above holds the bytes of
    # whatever tile source it is bound to, decoded by the ordinary pixel
    # machinery. That split is the whole trick: every tile path — decode, the
    # window slicing, ``replace_bytes`` — keeps working unchanged over the
    # bound tiles, which is what a later pixel edit on the tilemap view will
    # write through to reach the real art.
    cells: list[Cell] | None = None
    # Set when these cells are coordinates into *another tilemap's* cells rather
    # than tile numbers — what makes this a chained map
    # (``docs/design/tilemap-entry.md`` §3.1). Holding the chain rather than only
    # its result is what lets an edit re-resolve in place (:meth:`resolve`).
    chain: CellChain | None = None
    # What the cells resolve to through :attr:`chain`, kept beside them because
    # every draw wants them and only an edit changes them. Derived, never set by
    # hand: ``__post_init__`` and :meth:`resolve` are the only writers, so it
    # cannot drift from the cells it came from. None for an ordinary tilemap,
    # whose cells already name tiles.
    resolved_cells: list[Cell] | None = None
    tilemap_config: PathwayConfig | None = None
    tilemap_ctx: PipelineContext = field(default_factory=PipelineContext)
    # The cells' own bytes, kept in step with :attr:`cells` by whoever edits them
    # (the entry's file as it now stands, not as it was read). What Export Raw
    # writes and what the hex dump shows under a tilemap — the save path itself
    # re-encodes from the cells rather than reading this.
    tilemap_data: bytes = b""
    # Byte size of one cell under the tilemap codec, which is what turns a cell
    # index into a position in the bytes above (the hex highlight). The codec's
    # answer, recorded at load beside the two geometry fields below it.
    cell_bytes: int = 0
    # How many tiles one cell covers, and the index step between a cell's tile
    # rows. The step is not always the width: SNES 16x16 BG tiles are N, N+1,
    # N+0x10, N+0x11 because VRAM behaves as a 16-tile-wide array
    # (``docs/graphics-formats-reference/snes-hardware-notes.md`` §5).
    cell_tiles: tuple[int, int] = (1, 1)
    cell_row_stride: int = 0  # 0 = the cell's own width, i.e. consecutive tiles
    # The source tile that cell index 0 draws (a format's base-character field).
    tile_base_index: int = 0
    # How wide the format's tile-index *field* is, as a mask (0 = unbounded).
    # A multi-tile cell's neighbours are found by adding to that field, so the
    # addition wraps inside it: an SNES BG index is 10 bits, so the 16x16 cell at
    # 0x3FF draws 0x3FF, 0x000, 0x00F, 0x010 rather than running off the end
    # (``docs/graphics-formats-reference/snes-hardware-notes.md`` §5). Per format
    # rather than global, because the field is the format's: a bare Game Boy index
    # has no neighbours to find, and a stamp layout's word is a coordinate.
    index_mask: int = 0
    # Whether the cell *format* gives a cell a palette row to name, as the codec
    # declares it (:meth:`~celpix.plugins.base.TilemapCodecPlugin.has_palette_rows`).
    # A property of the format and not of this file: a screen whose cells all sit
    # on row 0 is still True, because the zeros are its cells' own answer. False
    # only where there is no field at all — a bare tile number — and that is what
    # hands the choice of colours back to the view's subpalette row
    # (``docs/design/tilemap-entry.md`` §8).
    cells_carry_palette_rows: bool = True
    # Set instead of a grid layout when the cells are **subsprites**: their
    # pixel offsets are not tile-aligned, so no cell grid can hold them and the
    # view draws frames of freely placed subsprites (:mod:`celpix.core.sprite`).
    # ``cells`` still holds the file's own records, so the save path is unchanged
    # and a write puts back exactly what was read.
    sprite_frames: list[tuple] | None = None
    # A sprite map's two subsprite sizes, **in tiles** (see
    # :data:`~celpix.core.sprite.DEFAULT_SUBSPRITE_TILES`): each size bit picks
    # one of the two, and no sprite file records the pair.
    sprite_size_pair: tuple[int, int] = DEFAULT_SUBSPRITE_TILES
    # Set instead when the cells are **character codes**: this is a *fontmap*, a
    # string of text stored as references into a font
    # (``docs/design/fontmap-entry.md``). Unlike the sprite flag above it changes
    # nothing about the picture — a text run draws as the grid of glyph tiles it
    # already is — so it is a plain declaration off the cell format rather than a
    # load product. It has to be one: an unbound fontmap has no alphabet and no
    # tiles, and is a fontmap all the same, which is exactly when the user needs
    # the controls that say so.
    text_layout: bool = False
    # What this fontmap's codes *say*: the font's glyph table with this stream's
    # own control codes laid over it (:func:`~celpix.pipeline.pipeline.
    # load_alphabet`). None where nothing is bound or the font has no alphabet
    # picked, which is not an error — the text then reads as hex, and every code
    # still round-trips.
    alphabet: Alphabet | None = None
    # The decoded tile source a tilemap draws from, kept between renders
    # (:func:`~celpix.pipeline.pipeline.tile_bank`). A map's cells reach anywhere
    # in the bank, so there is no window to slice and the *whole* source has to
    # be decoded — which would otherwise happen again on every repaint, for a
    # bank that only changes when the bytes or the codec do. Excluded from
    # equality and repr: it is a derived value, not part of what a document is.
    tile_bank_cache: tuple[tuple, list] | None = field(
        default=None, compare=False, repr=False
    )

    def __post_init__(self) -> None:
        self.resolve()

    def resolve(self) -> None:
        """Rebuild :attr:`resolved_cells` from :attr:`cells` through :attr:`chain`.

        The one writer of the derived list, called at construction and again after
        every cell edit, so a restamp shows the stamp it now names without the
        entry being re-read. A no-op on an unchained document, which has nothing
        to resolve through.

        A **stamped** chain resolves per drawn position rather than per entry
        (:func:`~celpix.core.tilemap.expand_stamps`): one coordinate names a stamp
        of source cells, so the list that comes back is in drawn order and the
        entries between two stamps are never read. It needs the referrer's own
        width to know where a row ends, so a stamp the file states but a width it
        does not falls back to the plain chain — resolving a grid whose shape is a
        guess would lay a shear on top of one.
        """
        chain = self.chain
        if chain is None or self.cells is None:
            self.resolved_cells = None
            return
        columns = self.stated_columns
        if chain.stamp != (1, 1) and columns:
            self.resolved_cells = expand_stamps(
                self.cells,
                chain.source,
                columns,
                chain.stamp,
                chain.source_columns,
                carry_rows=chain.carry_rows,
            )
            return
        self.resolved_cells = [
            resolve_cell(cell, chain.source, carry_rows=chain.carry_rows)
            for cell in self.cells
        ]

    @property
    def stated_columns(self) -> int:
        """How many cells across the *format* says this map is, or 0 for none.

        The container's width hint read straight off the context
        (:data:`~celpix.core.context.KEY_TILEMAP_COLUMNS`). Distinct from the
        view's Cols, which the user owns and which this only seeds: a stamped
        resolution has to snap positions to stamps, and it can only do that
        against a width the *file* fixes — snapping against a width the user was
        free to change would move every stamp the moment they changed it.
        """
        return int(self.tilemap_ctx.get(KEY_TILEMAP_COLUMNS, 0) or 0)

    @property
    def is_tilemap(self) -> bool:
        """Whether this document's own content is a tilemap rather than pixels."""
        return self.cells is not None

    @property
    def is_indirect(self) -> bool:
        """Whether these cells name another map's cells rather than tiles.

        Tests the :attr:`chain` and not its result, so it says what the document
        *is* rather than whether a derived list happens to be populated. What it
        changes is what an edit **means** — a cell edit here restamps, choosing a
        different source cell for that position — not whether one is allowed
        (:attr:`cells_editable`).
        """
        return self.chain is not None

    @property
    def is_sprite(self) -> bool:
        """Whether these cells are subsprites rather than grid positions."""
        return self.sprite_frames is not None

    @property
    def is_fontmap(self) -> bool:
        """Whether these cells are character codes — a **fontmap**.

        The two named tilemap variants sit at opposite ends of how much they
        change. A sprite map replaces the *layout* stage outright, because
        subsprites at signed pixel offsets cannot be a grid. A fontmap replaces
        nothing: a run of text draws as exactly the grid of glyph tiles its cells
        already describe, at whatever width the view is set to. What it adds is a
        second *reading* of the same cells — as words — which is why it is a flag
        beside the picture rather than a branch through it
        (``docs/design/fontmap-entry.md`` §2).

        Reads the declaration and not :attr:`alphabet`, so an entry with no font
        bound is still a fontmap and still offers the text window: that window is
        where the user finds out the codes mean nothing yet.
        """
        return self.is_tilemap and self.text_layout

    @property
    def text(self):  # -> Text
        """This fontmap's cells as readable text, with each character's cell.

        Decoded on demand rather than cached beside the cells: an edit changes
        the cells and the derived text at once, and a cache with no invalidation
        rule is how a text window comes to show the string before the last
        keystroke. The cost is a pass over the cells, which is the same pass the
        canvas makes to draw them.

        Empty text for a document that is not a fontmap, so a caller may ask
        without checking first — and hex for one whose font has no alphabet,
        which is the honest reading of codes nothing has explained.
        """
        from celpix.core.font import Alphabet as _Alphabet
        from celpix.core.font import Text as _Text

        if not self.is_fontmap:
            return _Text("", ())
        alphabet = self.alphabet or _Alphabet(code_digits=max(1, self.cell_bytes * 2))
        cells = self.cells or []
        return alphabet.decode(
            [cell.index for cell in cells], [cell.ends_line for cell in cells]
        )

    @property
    def folds_palette_rows(self) -> bool:
        """Whether this document's picture carries its palette row in the *indices*.

        True only for a tilemap whose format gives cells a row: that is the one
        composition where the row is folded in by
        :func:`~celpix.pipeline.pipeline.expand_cells` rather than applied at the
        colour table, and so the one where an index on screen is not the index a
        tile stores. Every consumer of that distinction reads it here — which
        colour table a render picks, what a pen has to add, what a commit has to
        take back off — so the picture and the bytes behind it cannot disagree
        about which of them the row is in.

        The pair matters because :attr:`cells_carry_palette_rows` **defaults to
        True**: it is a statement about a cell format, and a document with no
        cells has not made it. Reading it alone takes a pixel document down the
        tilemap branch of all of the above.
        """
        return self.is_tilemap and self.cells_carry_palette_rows

    @property
    def shown_frames(self) -> list:
        """The frames the view draws — every slot, or up to the last drawn one.

        The single answer to "which frames is this document showing", because
        three separate things have to agree about it: the image, the sheet
        geometry the canvas selects in, and the palette-row span an export sizes
        its colour table to. Read here rather than each of them applying
        :attr:`ViewOptions.show_all_frames` for itself, since a sheet laid out
        over one count and drawn over another is a selection that names the
        wrong part of the picture.

        Never empty, on both paths: an object with nothing drawn anywhere still
        needs one frame to draw nothing in
        (:func:`~celpix.core.sprite.drawn_frames`).
        """
        frames = self.sprite_frames or []
        if self.view.show_all_frames:
            return list(frames) or [()]
        return drawn_frames(frames)

    @property
    def animations(self) -> tuple:
        """The sequences this file's format carries, or empty where it carries none.

        The container reads them off the tail it preserves and states them
        (:data:`~celpix.core.context.KEY_TILEMAP_ANIMATIONS`); this is where a
        reader picks them up. Empty for every document that is not a sprite
        object, and for one whose table is all terminator — which most files'
        later groups are, so "has sequences" is
        :meth:`any` over this rather than its length.

        Deliberately **not** consulted by anything that draws: the frames are
        drawn from the records in file order, and a sequence only says what a
        player would step through (``docs/design/tilemap-entry.md`` §6).
        """
        return tuple(self.tilemap_ctx.get(KEY_TILEMAP_ANIMATIONS, ()) or ())

    @property
    def animations_inferred(self) -> bool:
        """Whether :attr:`animations` is a reading of the bytes rather than a spec.

        True for the one format whose writer emits its animation blocks opaquely,
        so the split into frames and durations comes off the corpus
        (:data:`~celpix.core.context.KEY_TILEMAP_ANIMATIONS_INFERRED`). Carried
        this far because it is the player that has to say it: a guess shown as
        confidently as a confirmed reading becomes a fact by repetition.
        """
        return bool(self.tilemap_ctx.get(KEY_TILEMAP_ANIMATIONS_INFERRED, False))

    @property
    def cells_editable(self) -> bool:
        """Whether a cell edit here has a well-defined thing to change.

        A **chained** map qualifies: its cells are coordinates, so changing one
        restamps that position — which source cell it names — and the stamp itself
        stays editable on the map it came from. Two well-defined gestures on two
        documents, which is what the question needed all along; what a format can
        actually express is then the codec's answer, not this one
        (:meth:`~celpix.plugins.base.TilemapCodecPlugin.transform_cell`,
        :meth:`~celpix.plugins.base.TilemapCodecPlugin.index_limit`).

        False only for a **sprite object**, where a canvas position resolves to a
        *subsprite* through an overlap order rather than to a cell through a grid,
        so there is no cell under the cursor to change
        (``docs/design/tilemap-entry.md`` §6, OBJ). It is not read-only on disk,
        so the distinction is about the gesture, not the file.
        """
        return self.is_tilemap and not self.is_sprite

    @property
    def drawn_cells(self) -> list[Cell]:
        """The cells the view should draw — resolved ones where they exist.

        In **drawn** order on a stamped chain, where one entry covers a stamp of
        positions and the file's order has nothing one-to-one to be in
        (:meth:`resolve`). Everything that names a file cell goes through
        :meth:`cell_at`.
        """
        return (
            self.resolved_cells
            if self.resolved_cells is not None
            else (self.cells or [])
        )

    @property
    def stamp_cells(self) -> tuple[int, int]:
        """How many source cells one pickable stamp covers — ``(1, 1)`` for none.

        The chain's stamp with one condition on it: the source's own cells must be
        single tiles. A stamp is placed as one layout block — a rectangle of
        consecutive tiles — and a stamp of *metatile* cells interleaves two
        rectangles no single block can express, so a format that stamped metatiles
        would preview stamp by stamp here and draw correctly on the map either
        way. No format in hand does: the only one that stamps is a PNL panel,
        whose word is one 8x8 tile in every file of the corpus
        (``docs/graphics-formats-reference/scgcad-formats.md`` §3.1).
        """
        chain = self.chain
        if chain is None or self.cell_tiles != (1, 1):
            return (1, 1)
        return chain.stamp

    @property
    def stamp_tiles(self) -> tuple[int, int]:
        """How many tiles one pickable stamp covers — the unit the sheet places.

        :attr:`cell_tiles` for everything unstamped, which is most documents. The
        tile source panel sizes its click targets off this, so a stamp is picked
        whole rather than by its corner.
        """
        across, down = self.stamp_cells
        return across * max(1, self.cell_tiles[0]), down * max(1, self.cell_tiles[1])

    @property
    def tiles_per_cell(self) -> int:
        across, down = self.cell_tiles
        return max(1, across) * max(1, down)

    # -- pages and their assembly (``docs/design/tilemap-entry.md`` §6) ------
    @property
    def page_size(self) -> tuple[int, int]:
        """One page's size in cells, or ``(0, 0)`` for a file that is one map.

        Read off the tilemap context, where the container published it: only the
        container knows its file holds four screens rather than one long one, and
        it has already said so
        (:data:`~celpix.core.context.KEY_TILEMAP_PAGE_ROWS`). Taken from there
        rather than copied into a field of its own so the two cannot disagree —
        a page's width *is* the width the format states, and a format that
        states no width has no page shape to speak of.
        """
        rows = int(self.tilemap_ctx.get(KEY_TILEMAP_PAGE_ROWS, 0) or 0)
        columns = self.stated_columns
        return (columns, rows) if columns > 0 and rows > 0 else (0, 0)

    @property
    def pages(self) -> int:
        """How many independent maps this file holds — 0 unless it holds several.

        Whole pages only. A cell count that is not a whole number of them is a
        file being read under a cell size it was not written in (a screen read as
        one-byte cells is twice as many cells as it has), and assembling *that*
        would lay a shear on top of a misreading. Answering 0 leaves it drawn
        back to back, where the mistake is at least visible as one.

        A **sprite object** is never paged: its records are subsprites at pixel
        offsets
        rather than a grid, so there is no page for a page to be a piece of.
        """
        columns, rows = self.page_size
        if not columns or self.is_sprite or self.cells is None:
            return 0
        per_page = columns * rows
        count = len(self.cells)
        pages = count // per_page
        return pages if pages > 1 and count % per_page == 0 else 0

    @property
    def stated_pages_across(self) -> int:
        """The assembly the **format** states, or 0 where it states none.

        A screen file is the case this exists for: its four quadrants are one
        64x64 tilemap and the editor's own loader says which corner each quadrant
        goes in, so the layout is structural rather than something to read off
        the picture (:data:`~celpix.core.context.KEY_TILEMAP_PAGES_ACROSS`).
        Checked against the pages actually present, so a file read under a cell
        size that halves the page count does not get laid out to a shape it no
        longer has.
        """
        across = int(self.tilemap_ctx.get(KEY_TILEMAP_PAGES_ACROSS, 0) or 0)
        return across if across in page_assemblies(self.pages) else 0

    @property
    def pages_across(self) -> int:
        """How many pages the view lays side by side — 1 when nothing is assembled.

        The **format's** answer wins outright where it has one: an assembly it
        states is a fact about the file, and offering the view's choice over it
        would let a setting shear a picture whose shape is not in question.

        Otherwise the view's, checked against the pages this file actually has
        (:func:`~celpix.core.tilemap.resolve_pages_across`), so an unpaged
        document and a stored assembly that no longer fits both answer 1 and the
        file draws in its own order.
        """
        pages = self.pages
        if not pages:
            return 1
        stated = self.stated_pages_across
        if stated:
            return stated
        columns, rows = self.page_size
        return resolve_pages_across(self.view.pages_across, columns, rows, pages)

    @property
    def assembled_columns(self) -> int:
        """How many cells across the assembly fixes this document at, or 0 for none.

        The width and the placement are two halves of one answer, so they are read
        off one property: a page is cut at a fixed size, so a picture laid out at
        any other width puts a page's rows where the next page's belong and the
        pages interleave — the same shear a wrong column count makes on an
        unassembled map, arrived at from the other direction.

        This is why it lives here rather than being enforced where the two happen
        to meet. The view's Cols is a *setting* that a refresh keeps in step with
        the assembly, but a render can be asked for by something that never went
        through a refresh at all — a bulk PNG export of entries that were loaded
        and never shown — so the layout takes the width from the document and the
        spin is what mirrors it (``docs/design/tilemap-entry.md`` §6).

        0 on everything unpaged, which leaves the caller's own column count the
        answer: that is the ordinary tilemap, whose width really is a preference.
        """
        columns, _rows = self.page_size
        return self.pages_across * columns if self.pages else 0

    @property
    def cell_order(self) -> tuple[int, ...] | None:
        """Which cell each drawn position holds — None when the two are the same.

        None rather than ``range(len(cells))`` for every unassembled document,
        which is most of them: it is what lets the render path and the selection
        skip the indirection entirely instead of paying for a permutation that
        permutes nothing.
        """
        across = self.pages_across
        if across <= 1:
            return None
        columns, rows = self.page_size
        return page_order(columns, rows, self.pages, across)

    @property
    def laid_out_cells(self) -> list[Cell]:
        """:attr:`drawn_cells` in the order the view lays them out.

        What the renderer walks, and the only place the assembly reaches the
        picture. Everything else about a cell — its position in
        :attr:`cells`, what a save writes, what a chained map's coordinate names,
        which record the hex dump highlights — stays in the **file's** order, so an
        assembly can be changed (or undone, or restored differently) without any
        of that meaning something else afterwards.
        """
        cells = self.drawn_cells
        order = self.cell_order
        return cells if order is None else [cells[at] for at in order]

    def cell_at(self, position: int) -> int:
        """Which cell the drawn position ``position`` holds.

        The inward half of :attr:`laid_out_cells`: the canvas resolves a click to
        a position in the picture, and an edit needs the cell that position draws.
        Identity on an unassembled document, and out-of-range positions come back
        unchanged so a caller's own bounds check stays the one that decides.

        Two steps, in file-order terms: the assembly says which file position a
        drawn one shows, and a **stamp** then snaps that to the entry whose stamp
        contains it (:func:`~celpix.core.tilemap.stamp_origin`) — so an edit
        anywhere inside a stamp changes the one entry that stamp came from, and
        the positions the format never wrote stay unwritten. Composed rather than
        alternated because they answer different halves of the same question; no
        format in hand does both.
        """
        order = self.cell_order
        if order is not None and 0 <= position < len(order):
            position = order[position]
        chain = self.chain
        columns = self.stated_columns
        if chain is None or chain.stamp == (1, 1) or not columns:
            return position
        return stamp_origin(position, columns, chain.stamp)

    def cell_tile_indices(self, cell: Cell) -> list[int]:
        """The source tile indices ``cell`` draws, in the order they appear.

        This document's geometry applied to :func:`~celpix.core.tilemap.tile_run`
        — the cell's tile counts, its :attr:`cell_row_stride` (defaulting to the
        cell's own width, i.e. consecutive tiles), and the base index the bound
        source starts at.

        The walk runs in the *format's* index space and :attr:`tile_base_index` is
        added after it, so :attr:`index_mask` wraps the neighbours inside the
        field the way the hardware's adder does. The two orders agree whenever
        there is no mask, addition being addition; they differ exactly where the
        run would otherwise leave the field.
        """
        across, down = self.cell_tiles
        if across <= 1 and down <= 1:
            # The ordinary hardware cell — one tile, no run to walk and no order
            # for a flip to reverse. Answered here rather than through the walk
            # because a repaint asks this once per cell, thousands of times. No
            # mask either: a lone index came out of the field already inside it.
            return [self.tile_base_index + cell.index]
        across, down = max(1, across), max(1, down)
        run = tile_run(
            cell.index,
            across,
            down,
            self.cell_row_stride or across,
            flip_h=cell.flip_h,
            flip_v=cell.flip_v,
        )
        if self.index_mask:
            return [self.tile_base_index + (index & self.index_mask) for index in run]
        return [self.tile_base_index + index for index in run]

    @classmethod
    def palette_only(
        cls,
        palette: Palette,
        config: PathwayConfig,
        ctx: PipelineContext,
        palette_base_bytes: bytes,
    ) -> Document:
        """A Document that carries only a palette — a PALETTE entry's live store.

        A registered palette file owns its colors *here*, rather than on whichever
        graphic happens to render it, so a color edit dirties the palette entry and
        Write saves it back to the ``.pal`` — the graphic is never touched
        (docs/design/palette-editing.md §2). The pixel half is inert: no bytes, zero
        tile geometry, and a non-writable pixel config, so the tile machinery and
        the pixel Write have nothing to act on (``tile_count`` is 0).
        """
        return cls(
            pixel_data=b"",
            bytes_per_tile=0,
            tile_width=0,
            tile_height=0,
            palette=palette,
            pixel_config=PathwayConfig(
                source=FileRef(""), interpret_preset_id="", write_enabled=False
            ),
            palette_config=config,
            palette_ctx=ctx,
            palette_base_bytes=palette_base_bytes,
        )

    @property
    def tile_count(self) -> int:
        # Ceiling: a trailing partial tile counts — it's viewable, zero-padded.
        tb = self.bytes_per_tile
        return ceil_div(len(self.pixel_data), tb) if tb else 0

    def palette_rows(self, index_space: int) -> int:
        """How many subpalette rows this document's palette holds.

        Asked of the document so the one place that copes with a document
        carrying no palette at all is here — a render with nothing loaded still
        has to place a named row somewhere, and every row the index space can
        express is a better answer than "row 0" for a document whose palette has
        yet to arrive.
        """
        return palette_row_count(len(self.palette) if self.palette else 0, index_space)

    def palette_row_wrap(self, index_space: int) -> int:
        """The modulus a named row wraps against, or 0 where wrapping is off.

        The gate in one place, so every reader of a named row asks the same
        question and none of them has to know the toggle exists
        (:attr:`ViewOptions.wrap_palette_rows`,
        :func:`~celpix.pipeline.pipeline.drawn_palette_row`).
        """
        return self.palette_rows(index_space) if self.view.wrap_palette_rows else 0

    def window_bytes(self, first_tile: int, count: int, nudge: int = 0) -> bytes:
        """The byte slice for ``count`` tiles starting at tile ``first_tile``.

        ``nudge`` shifts the whole tile grid forward that many bytes (sub-tile
        alignment). Clamped to the data, so a partial window at the file's end
        yields fewer tiles' worth of bytes (and an out-of-range request yields
        ``b""``). Codecs decode only whole tiles, so a trailing partial tile —
        from data that isn't a whole number of tiles, or from the nudge pushing
        the grid past the end — is zero-padded up to one. The codec decodes
        exactly the tiles in this slice — see the module docstring.
        """
        tb = self.bytes_per_tile
        if not tb:
            return b""
        start = max(0, first_tile) * tb + nudge
        end = min(len(self.pixel_data), max(0, first_tile + count) * tb + nudge)
        if end <= start:
            return b""
        window = self.pixel_data[start:end]
        pad = -len(window) % tb
        return window + bytes(pad) if pad else window

    def replace_bytes(self, start: int, data: bytes) -> None:
        """Splice ``data`` into the pixel bytes at ``start`` — the edit primitive.

        The decompressed bytes are the source of truth (see the module
        docstring), so every pixel edit ends here: tiles are encoded back to
        bytes and spliced in, and Write then compresses and writes the buffer.
        Editing never resizes a file — anything past the end is dropped, since
        the bytes live in a fixed slot in the source.
        """
        if start < 0 or not data:
            return
        data = data[: max(0, len(self.pixel_data) - start)]
        if data:
            self.pixel_data = (
                self.pixel_data[:start] + data + self.pixel_data[start + len(data) :]
            )

    @property
    def display_base(self) -> int:
        """The file byte this document's position 0 corresponds to.

        Raw sources (no decompressor, no reshape) show source-file-absolute
        addresses — past whatever a container skipped, or the slice offset for a
        raw slice — so ROM bank addresses stay meaningful wherever the bytes came
        from. A decompressed stream has no linear mapping back to file offsets,
        and a reshaped one is a byte permutation of its region, so both show their
        own 0-based positions instead of lying with file addresses.

        The base comes from what Read *recorded* rather than from the config's
        requested offset, because only the container knows where it actually
        began: it works its start out from the format (past a copier header, past
        the iNES header and the PRG banks) and the host never asked for it.

        This lives on the document because it is what pinned palette regions are
        anchored in (:mod:`celpix.core.paletteregions`), and export resolves them
        with no window to ask.
        """
        if not self.pixel_config.reads_raw_bytes:
            return 0
        return self.pixel_ctx.get(KEY_SOURCE_OFFSET, 0)

    @property
    def tilemap_display_base(self) -> int:
        """:attr:`display_base` for the other half of a tilemap document.

        A tilemap entry holds two files at once — its cells, and the tiles it is
        bound to — and they start in different places. The addresses beside its
        **cells** (the hex dump) are the entry's own file, past whatever its
        container skipped: a screen's payload begins at 0, a stamp layout's past
        its header. Same rule as above, asked of the tilemap pathway; 0 for a
        document that has no cells to address.
        """
        cfg = self.tilemap_config
        if cfg is None or not cfg.reads_raw_bytes:
            return 0
        return self.tilemap_ctx.get(KEY_SOURCE_OFFSET, 0)

    def clamp_tile_offset(
        self, offset: int, columns: int, rows: int, nudge: int = 0
    ) -> int:
        """A valid top-left tile offset for a ``columns`` × ``rows`` window."""
        return max(0, min(offset, self.last_page_tile_offset(columns, rows, nudge)))

    def last_page_tile_offset(self, columns: int, rows: int, nudge: int = 0) -> int:
        """The greatest top-left tile offset a ``columns`` × ``rows`` window may sit at.

        The last reachable window is the final page of tiles (the view never
        scrolls into all-blank space), mirroring how tile viewers stop at
        ``file_size - one_page`` — but rounded **up to a whole tile-row**. A file
        whose tile count isn't a multiple of ``columns`` has a partial last row,
        and stopping at the exact tile bound would start that page at a different
        column phase than every other one: the image would jump sideways on the
        final scroll step, and under the 2D walk — where a row of tiles is one
        interleaved byte stripe — the whole window would be re-cut from a
        mid-stripe origin and decode into different pixels entirely. Landing on a
        row boundary leaves a few blank cells after the last tile instead, which
        is how that remainder reads at every other scroll position.

        A byte ``nudge`` shifts the tile grid, so the bound moves with it; a
        trailing partial tile counts as usable (it renders zero-padded).
        """
        cols = max(1, columns)
        tb = self.bytes_per_tile
        usable = ceil_div(len(self.pixel_data) - nudge, tb) if tb else 0
        return max(0, ceil_div(usable - cols * max(1, rows), cols) * cols)

    def clamp_byte_position(self, pos: int, columns: int, rows: int) -> tuple[int, int]:
        """Clamp a byte-space view origin; split it into ``(offset, nudge)``.

        The greatest reachable origin is the last full page at nudge 0, so a
        byte step can never overshoot the end and snap backwards. This is the
        byte-space companion of :meth:`clamp_tile_offset` (which clamps tile moves
        that keep their nudge).
        """
        tb = self.bytes_per_tile
        if not tb:
            return (0, 0)
        max_pos = self.last_page_tile_offset(columns, rows) * tb
        return divmod(max(0, min(pos, max_pos)), tb)
