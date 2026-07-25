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

from celpix.core import ceil_div
from celpix.core.context import PipelineContext
from celpix.core.palette import Palette
from celpix.core.tilemap import TileMap
from celpix.pipeline.pathway import PathwayConfig
from celpix.plugins.base import FileRef


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

    ``tile_map`` rearranges *which* tile each position shows, so scattered tiles
    can be viewed and edited side by side; it moves no bytes, and an edit made at
    a rearranged position still writes back to the tile's real home
    (:mod:`celpix.core.tilemap`). ``show_rearranged`` is the toggle between that
    view and the file's true order — off makes the map inert without discarding
    it. The map composes *before* the block placement: it decides which tile
    fills a slot, the arrangement decides where that slot lands.
    """

    columns: int = 16
    rows: int = 16
    zoom: int = 4
    show_grid: bool = False
    subpalette_row: int = 0
    tile_offset: int = 0  # top-left tile index into the pixel bytes
    byte_nudge: int = 0  # sub-tile byte shift of the whole grid
    block_columns: int = 1  # tiles per block, horizontally
    block_rows: int = 1  # tiles per block, vertically
    block_order: str = "row"  # fill within a block: row | column | row-interleave
    two_dimensional: bool = False  # read the source as a wide bitmap, not tiles
    bitmap_width: int = 0  # pixels across the bitmap is (0 = the codec's own tiles)
    tile_map: TileMap = TileMap()  # display-only rearrangement of tile positions
    show_rearranged: bool = True  # apply tile_map, or show the file's true order


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
    palette_bytes: bytes = b""
    palette_edits: set[int] = field(default_factory=set)

    @classmethod
    def palette_only(
        cls,
        palette: Palette,
        config: PathwayConfig,
        ctx: PipelineContext,
        palette_bytes: bytes,
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
            palette_bytes=palette_bytes,
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

    def clamp_offset(self, offset: int, columns: int, rows: int, nudge: int = 0) -> int:
        """A valid top-left tile offset for a ``columns`` × ``rows`` window."""
        return max(0, min(offset, self.last_page_offset(columns, rows, nudge)))

    def last_page_offset(self, columns: int, rows: int, nudge: int = 0) -> int:
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
        byte-space companion of :meth:`clamp_offset` (which clamps tile moves
        that keep their nudge).
        """
        tb = self.bytes_per_tile
        if not tb:
            return (0, 0)
        max_pos = self.last_page_offset(columns, rows) * tb
        return divmod(max(0, min(pos, max_pos)), tb)
