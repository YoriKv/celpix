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
from celpix.core.context import KEY_SOURCE_OFFSET, PipelineContext
from celpix.core.palette import Palette
from celpix.core.paletteregions import PaletteRegions
from celpix.core.tilemap import Cell
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
    group at a time. It changes no bytes and no indices: the row reaches the
    screen as a shift applied to the *rendered* indices only
    (:mod:`celpix.core.paletteregions`). ``show_palette_regions`` is the toggle
    between that and the plain single-row view, like ``show_rearranged``.

    ``tile_rearrangement`` rearranges *which* tile each position shows, so
    scattered tiles can be viewed and edited side by side; it moves no bytes, and
    an edit made at a rearranged position still writes back to the tile's real
    home (:mod:`celpix.core.tilerearrangement`). ``show_rearranged`` is the
    toggle between that
    view and the file's true order — off makes the map inert without discarding
    it. The map composes *before* the block placement: it decides which tile
    fills a slot, the arrangement decides where that slot lands.
    """

    columns: int = 16
    rows: int = 16
    zoom: int = 4
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

    # -- the tilemap half (``docs/design/tilemap-entry.md``) -----------------
    # Set only on a tilemap document, and what :attr:`is_tilemap` tests. The
    # cells are the *entry's own* file; ``pixel_data`` above holds the bytes of
    # whatever tile source it is bound to, decoded by the ordinary pixel
    # machinery. That split is the whole trick: every tile path — decode, the
    # window slicing, ``replace_bytes`` — keeps working unchanged over the
    # bound tiles, which is what a later pixel edit on the tilemap view will
    # write through to reach the real art.
    cells: list[Cell] | None = None
    # What the cells *resolve to*, when they are not tile references themselves.
    # A stamp layout's entry names a cell in a panel, and the panel supplies the
    # tile and its attributes — so the map is drawn from these while ``cells``
    # stays the file's own words (``docs/design/tilemap-entry.md`` §6). None for
    # an ordinary tilemap, whose cells already name tiles.
    resolved_cells: list[Cell] | None = None
    tilemap_config: PathwayConfig | None = None
    tilemap_ctx: PipelineContext = field(default_factory=PipelineContext)
    tilemap_data: bytes = b""  # the cells' own bytes, for a splice-style save
    # How many tiles one cell covers, and the index step between a cell's tile
    # rows. The step is not always the width: SNES 16x16 BG tiles are N, N+1,
    # N+0x10, N+0x11 because VRAM behaves as a 16-tile-wide array
    # (``docs/graphics-formats-reference/snes-hardware-notes.md`` §5).
    cell_tiles: tuple[int, int] = (1, 1)
    cell_row_stride: int = 0  # 0 = the cell's own width, i.e. consecutive tiles
    # The source tile that cell index 0 draws (a format's base-character field).
    tile_base_index: int = 0
    # Set instead of a grid layout when the cells are **sprite parts**: their
    # pixel offsets are not tile-aligned, so no cell grid can hold them and the
    # view draws frames of freely placed parts (:mod:`celpix.core.sprite`).
    # ``cells`` still holds the file's own records, so the save path is unchanged
    # and a write puts back exactly what was read.
    sprite_frames: list[tuple] | None = None
    sprite_size_pair: tuple[int, int] = (8, 16)

    @property
    def is_tilemap(self) -> bool:
        """Whether this document's own content is a tilemap rather than pixels."""
        return self.cells is not None

    @property
    def is_indirect(self) -> bool:
        """Whether these cells point at another map's cells rather than at tiles.

        True only for a stamp layout. It is what makes such a document
        **view-only for now**: an edit here would have to decide whether the user
        meant to restamp (change which panel cell is named) or to change the
        stamp itself (edit the panel), and the format's own answer to that — an
        attribute-source flag whose two behaviours we cannot yet tell apart —
        is the part still unconfirmed (``scgcad-formats.md`` §4).
        """
        return self.resolved_cells is not None

    @property
    def is_sprite(self) -> bool:
        """Whether these cells are sprite parts rather than grid positions."""
        return self.sprite_frames is not None

    @property
    def cells_editable(self) -> bool:
        """Whether a cell edit here has a well-defined thing to change.

        False for the two documents whose cells are not what is on screen: a
        stamp layout, where an edit would have to choose between restamping and
        editing the stamp (:attr:`is_indirect`), and a sprite object, where a
        canvas position resolves to a *part* through an overlap order rather than
        to a cell through a grid. Both are view-only until that is designed
        (``docs/design/tilemap-entry.md`` §9); neither is read-only on disk, so
        the distinction is about the gesture, not the file.
        """
        return self.is_tilemap and not self.is_indirect and not self.is_sprite

    @property
    def drawn_cells(self) -> list[Cell]:
        """The cells the view should draw — resolved ones where they exist."""
        return (
            self.resolved_cells
            if self.resolved_cells is not None
            else (self.cells or [])
        )

    @property
    def tiles_per_cell(self) -> int:
        across, down = self.cell_tiles
        return max(1, across) * max(1, down)

    def cell_tile_indices(self, cell: Cell) -> list[int]:
        """The source tile indices ``cell`` draws, in the order they appear.

        One index for an ordinary hardware cell; four for a 16x16 metatile,
        stepped by :attr:`cell_row_stride` rather than by the cell's width.

        A flip reverses the *order* as well as mirroring each tile: a mirrored
        metatile shows its right-hand tile on the left, mirrored. Both halves are
        needed and neither is sufficient — the same compound rule
        :meth:`~celpix.core.tilemap.CellGrid.flipped_h` follows over a block.
        """
        across, down = max(1, self.cell_tiles[0]), max(1, self.cell_tiles[1])
        stride = self.cell_row_stride or across
        first = self.tile_base_index + cell.index
        return [
            first
            + (down - 1 - row if cell.flip_v else row) * stride
            + (across - 1 - col if cell.flip_h else col)
            for row in range(down)
            for col in range(across)
        ]

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
