"""Tile arrangement: composing a list of tiles into one viewable image.

The pixel codec decodes to a flat list of tiles; an arrangement lays them out into
a single :class:`IndexGrid`. The default is **linear** (1D): tiles fill
left-to-right, top-to-bottom, ``columns`` tiles wide.

Two arrangement axes sit on top of that, both pure **display** state (they never
change the codec — overview.md §4):

- **Block grouping / order** (:class:`BlockLayout`) — group tiles into
  ``block_columns`` × ``block_rows`` blocks, filled row-major, column-major, or
  row-interleaved. This is *placement only*: the same decoded tiles land in
  different cells. It is how N-tile sprites/metatiles read as coherent units —
  8×16 (row-interleave) and Mega Drive / Neo Geo sprites (column-major).
- **2D / wide-bitmap** (:func:`reflow_2d`) — a different *byte walk*: the source is
  treated as one wide bitmap ``columns`` tiles across, so each tile's pixel-rows are
  strided ``columns`` tiles apart in the file rather than contiguous. This changes
  which bytes form each tile (not where tiles land), so it happens on the raw window
  before decode. "Same bytes, different walk" — see
  ``docs/graphics-formats-reference/implementation-guide.md`` §5.

Large files are viewed through a **window** (:func:`compose_window`): only a fixed
band of rows starting at a tile offset is composed, so the cost of laying out and
rendering is bounded by the window, not the file. The full tile list stays the
model — decode and save are unaffected; only what reaches the canvas is windowed.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property

from celpix.core import ceil_div
from celpix.core.index_grid import IndexGrid

# The tile-fill orders a block-row supports. "row" fills each block row-major,
# block by block. "column" fills each block **column-major** (top-to-bottom down
# a column, then the next column) — how Sega Mega Drive and Neo Geo multi-tile
# sprites are stored. "row-interleave" fills one tile-row across *every* block
# before the next — the horizontal 8×16 sprite-sheet layout (tops, then bottoms).
BLOCK_ORDERS = ("row", "column", "row-interleave")


@dataclass(frozen=True)
class ArrangementPreset:
    """A named block/order/2D combination for the view's *Pattern* picker.

    The arrangement analogue of a bank-address preset: pure display parameters —
    the same block axes a :class:`BlockLayout` takes, plus the :func:`reflow_2d`
    2D byte walk — bundled under a recognisable name so common console layouts are
    one click instead of four fiddly controls. Bit depth is orthogonal (picked as
    the pixel format), so a preset says nothing about it. Selecting a preset fills
    and locks the individual controls; the UI's "Custom" entry (not in this list)
    unlocks them for hand editing.
    """

    id: str
    name: str
    block_columns: int = 1
    block_rows: int = 1
    block_order: str = "row"
    two_dimensional: bool = False

    @property
    def params(self) -> tuple[int, int, str, bool]:
        """The four arrangement values, in the order the view widgets hold them."""
        return (
            self.block_columns,
            self.block_rows,
            self.block_order,
            self.two_dimensional,
        )


# Documented block/order/2D combinations, named by the hardware that uses them
# (console names are hardware, not other projects — fine to name here). Order runs
# plain → 2D → the sprite/metatile groupings. The mappings behind each are worked
# out in docs/design-reference/navigation-and-preview.md and the tests in
# tests/test_arrangement.py. "Linear" is the default plain back-to-back walk.
ARRANGEMENT_PRESETS: tuple[ArrangementPreset, ...] = (
    ArrangementPreset("linear", "Default - Linear Tiles"),
    ArrangementPreset("2d", "2D - Bitmap (N64/NDS/PC/etc)", two_dimensional=True),
    # 8×16 NES/GB sprites: tile i (top) stacked over tile i+1 (bottom), the next
    # sprite in the next column — a 1×2 block filled block-by-block.
    ArrangementPreset("nes-8x16", "8×16 sprites, stacked (NES/GB)", block_rows=2),
    # The other 8×16 storage: a whole row of sprite tops, then the matching row of
    # bottoms (the horizontal sprite-sheet layout).
    ArrangementPreset(
        "8x16-sheet",
        "8×16 sprite sheet, interleaved",
        block_rows=2,
        block_order="row-interleave",
    ),
    # 2×2 metatiles read row-major (YY-CHR's x16y16): 16×16 units of four 8×8 tiles.
    ArrangementPreset(
        "metatile-2x2",
        "16×16 metatiles (2×2)",
        block_columns=2,
        block_rows=2,
    ),
    # Mega Drive / Neo Geo store a multi-tile sprite column-major (down each column,
    # then across) — here the common 2×2 (16×16) case.
    ArrangementPreset(
        "genesis-sprite",
        "Mega Drive / Neo Geo sprite (2×2, column)",
        block_columns=2,
        block_rows=2,
        block_order="column",
    ),
)


def arrangement_preset_for(
    block_columns: int, block_rows: int, block_order: str, two_dimensional: bool
) -> ArrangementPreset | None:
    """The preset matching these four values, or ``None`` for a custom arrangement.

    Lets the UI re-derive the *Pattern* selection from restored view state instead
    of persisting it separately: an exact-tuple match reselects (and relocks) that
    preset; anything else is Custom. A hand-tuned arrangement that happens to equal
    a preset reads back as that preset — same parameters, so the view is identical.
    """
    params = (block_columns, block_rows, block_order, two_dimensional)
    for preset in ARRANGEMENT_PRESETS:
        if preset.params == params:
            return preset
    return None


@dataclass(frozen=True)
class BlockLayout:
    """Maps a window's linear tile slots to canvas cell positions, and back.

    The canvas is ``columns`` tiles wide. Tiles group into blocks of
    ``block_columns`` × ``block_rows`` tiles; blocks tile the canvas
    left-to-right, top-to-bottom. ``block_order`` (see :data:`BLOCK_ORDERS`)
    decides the fill within a block-row — row-major, column-major (Mega
    Drive / Neo Geo sprites), or row-interleaved (8×16 sprite sheets).

    ``block_columns == block_rows == 1`` (the default) is plain row-major: every
    method then reduces exactly to ``slot ↔ (slot % columns, slot // columns)``,
    so the ordinary view path is unchanged (the order can't matter for a 1×1
    block). Both mappings share the same slot space the canvas uses (slot 0 = the
    window's first tile).
    """

    columns: int
    block_columns: int = 1
    block_rows: int = 1
    block_order: str = "row"

    # The three derived sizes below are read several times per slot mapped, and a
    # window maps thousands of slots per repaint — so they are computed once per
    # layout rather than per lookup. Cached on the instance, which is safe because
    # the dataclass is frozen: the inputs cannot change under the cache.
    @cached_property
    def is_plain(self) -> bool:
        """True when placement is plain row-major (no block grouping)."""
        return self._bc == 1 and self._br == 1

    @cached_property
    def _bc(self) -> int:
        # A block can't be wider than the canvas; clamp so partial-width blocks
        # never place tiles past the right edge.
        return max(1, min(self.block_columns, self.columns))

    @cached_property
    def _br(self) -> int:
        return max(1, self.block_rows)

    @cached_property
    def _blocks_per_row(self) -> int:
        return max(1, self.columns // self._bc)

    @cached_property
    def slots_per_block_row(self) -> int:
        """How many linear slots one row of blocks holds — the mapping's period."""
        return self._blocks_per_row * self._bc * self._br

    def slot_to_cell(self, slot: int) -> tuple[int, int]:
        """The ``(tile_x, tile_y)`` canvas cell a linear slot lands in."""
        if self.is_plain:  # every term below collapses; skip the block arithmetic
            cols = self._blocks_per_row  # 1×1 blocks: one per column, clamped ≥ 1
            return slot % cols, slot // cols
        bc, br, bpr = self._bc, self._br, self._blocks_per_row
        per_blockrow = self.slots_per_block_row
        blockrow, rem = divmod(slot, per_blockrow)
        if self.block_order == "row-interleave":
            inner_y, across = divmod(rem, bpr * bc)
            block_x, inner_x = divmod(across, bc)
        else:
            block_x, within = divmod(rem, bc * br)
            if self.block_order == "column":  # down each column, then the next
                inner_x, inner_y = divmod(within, br)
            else:  # "row" (default): left-to-right, then down
                inner_y, inner_x = divmod(within, bc)
        return block_x * bc + inner_x, blockrow * br + inner_y

    def cell_to_slot(self, tile_x: int, tile_y: int) -> int | None:
        """The linear slot at cell ``(tile_x, tile_y)`` — ``None`` if no tile
        maps there (a partial-width block column past the last whole block)."""
        if self.is_plain:
            cols = self._blocks_per_row
            return None if tile_x >= cols else tile_y * cols + tile_x
        bc, br, bpr = self._bc, self._br, self._blocks_per_row
        blockrow, inner_y = divmod(tile_y, br)
        block_x, inner_x = divmod(tile_x, bc)
        if block_x >= bpr:
            return None
        if self.block_order == "row-interleave":
            rem = inner_y * (bpr * bc) + block_x * bc + inner_x
        elif self.block_order == "column":
            rem = block_x * (bc * br) + inner_x * br + inner_y
        else:  # "row"
            rem = block_x * (bc * br) + inner_y * bc + inner_x
        return blockrow * (bpr * bc * br) + rem


def compose_window(
    tiles: list,
    columns: int,
    first_tile: int,
    rows: int,
    layout: BlockLayout | None = None,
):
    """Lay out ``rows`` rows of ``columns`` tiles starting at tile ``first_tile``.

    The image is always ``columns`` × ``rows`` tiles so the canvas size stays stable
    while navigating. ``layout`` decides each slot's cell (default: plain row-major);
    slots whose tile index falls outside ``tiles`` (a partial window at the file end,
    or a negative ``first_tile``), or whose cell falls outside the image, stay blank —
    so a full layout, a partial window, and a block grouping all share one path.

    Works for either grid type by blitting in units of the tiles' ``bytes_per_pixel``
    and returning a grid of their own type. Composing only the visible band is what
    keeps viewing large files cheap — see the module docstring.
    """
    if not tiles:
        return IndexGrid(0, 0)
    cols = max(1, columns)
    rows = max(1, rows)
    if layout is None:
        layout = BlockLayout(cols)
    first = tiles[0]
    tw, th = first.width, first.height
    bpx = first.bytes_per_pixel
    image = type(first)(cols * tw, rows * th)
    dst = image.data
    row_bytes = tw * bpx
    if layout.is_plain:
        _compose_plain(dst, tiles, cols, rows, th, row_bytes, first_tile)
        return image
    dst_stride = cols * row_bytes
    for slot in range(cols * rows):
        idx = first_tile + slot
        if idx < 0 or idx >= len(tiles):
            continue
        tile_x, tile_y = layout.slot_to_cell(slot)
        if tile_x >= cols or tile_y >= rows:
            continue
        src = tiles[idx].data
        d0 = tile_y * th * dst_stride + tile_x * row_bytes
        s0 = 0
        for _y in range(th):
            dst[d0 : d0 + row_bytes] = src[s0 : s0 + row_bytes]
            d0 += dst_stride
            s0 += row_bytes
    return image


def _compose_plain(
    dst: bytearray,
    tiles: list,
    cols: int,
    rows: int,
    th: int,
    row_bytes: int,
    first_tile: int,
) -> None:
    """Blit a row-major window, one whole image row per write.

    The plain layout puts a cell row's tiles side by side, so each image row is
    the concatenation of the same pixel row of ``cols`` consecutive tiles — one
    ``join`` and one store, instead of a slice assignment per tile per row. The
    difference is the whole cost of composing when the tiles are small: a bitmap
    width can cut a window into tens of thousands of them.
    """
    blank = bytes(th * row_bytes)  # a whole missing tile: past the end, or before it
    datas = [tile.data for tile in tiles]
    count = len(datas)
    span = cols * row_bytes
    pos = 0
    for cell_y in range(rows):
        base = first_tile + cell_y * cols
        row_tiles = [
            datas[idx] if 0 <= idx < count else blank
            for idx in range(base, base + cols)
        ]
        for y in range(th):
            start = y * row_bytes
            stop = start + row_bytes
            dst[pos : pos + span] = b"".join(src[start:stop] for src in row_tiles)
            pos += span


def split_grid(
    grid, tile_width: int, tile_height: int, layout: BlockLayout | None = None
):
    """Cut a composed image back into tiles — the inverse of :func:`compose_window`.

    Returns one tile per slot of the cell area the image covers, in **linear slot
    order**, so ``layout`` undoes exactly the placement
    that composed it (pass the same one). An image whose size isn't a whole
    number of tiles is zero-padded at the right/bottom edge, and a slot whose
    cell falls outside the image yields a blank tile — a block layout can leave
    such gaps, and dropping them would shift every later tile.

    That padding is a placeholder, not data: :func:`split_coverage` says how much
    of each tile the image actually reached, so an importer can leave the rest of
    an edge tile as whatever the file already holds instead of stamping the pad
    over pixels it has no colors for.

    This is how external pixels (a pasted or imported image) become tiles: the
    importer quantizes the whole image once, then splits it here.
    """
    bpx = grid.bytes_per_pixel
    src = grid.data
    src_stride = grid.width * bpx
    row_bytes = tile_width * bpx
    tiles = []
    cells = _split_cells(grid.width, grid.height, tile_width, tile_height, layout)
    for base_x, base_y, cover_w, cover_h in cells:
        tile = type(grid)(tile_width, tile_height)
        tiles.append(tile)
        if not (cover_w and cover_h):
            continue
        dst = tile.data
        take = cover_w * bpx
        s0 = base_y * src_stride + base_x * bpx
        d0 = 0
        for _y in range(cover_h):
            dst[d0 : d0 + take] = src[s0 : s0 + take]
            s0 += src_stride
            d0 += row_bytes
    return tiles


def split_coverage(
    grid_width: int,
    grid_height: int,
    tile_width: int,
    tile_height: int,
    layout: BlockLayout | None = None,
) -> list[tuple[int, int]]:
    """How many pixels of each :func:`split_grid` tile came from the image.

    Parallel to that function's result — same slot order, same length — as a
    ``(width, height)`` per tile: the whole tile for an interior one, less at the
    right/bottom edge of an image that isn't a whole number of tiles, and
    ``(0, 0)`` for a block-layout gap slot the image never reached. Everything
    outside that rectangle is padding :func:`split_grid` invented, which a write
    back into a file must not stamp over real pixels.
    """
    cells = _split_cells(grid_width, grid_height, tile_width, tile_height, layout)
    return [(w, h) for _base_x, _base_y, w, h in cells]


def _split_cells(
    grid_width: int,
    grid_height: int,
    tile_width: int,
    tile_height: int,
    layout: BlockLayout | None,
) -> list[tuple[int, int, int, int]]:
    """Per slot, the image pixel the tile starts at and how much of it is real.

    The one place the split geometry lives, so :func:`split_grid` and
    :func:`split_coverage` cannot disagree about which slot covers what — an
    importer pairs their results element by element. ``(base_x, base_y, 0, 0)``
    marks a slot the image never reached: a block-layout gap, or a cell past the
    right/bottom edge of an image that isn't a whole number of tiles.
    """
    cols = max(1, ceil_div(grid_width, tile_width))
    rows = max(1, ceil_div(grid_height, tile_height))
    if layout is None:
        layout = BlockLayout(cols)
    cells = []
    for slot in range(cols * rows):
        tile_x, tile_y = layout.slot_to_cell(slot)
        if tile_x >= cols or tile_y >= rows:
            cells.append((0, 0, 0, 0))
            continue
        base_x, base_y = tile_x * tile_width, tile_y * tile_height
        width = max(0, min(tile_width, grid_width - base_x))
        height = max(0, min(tile_height, grid_height - base_y))
        if not (width and height):
            width = height = 0
        cells.append((base_x, base_y, width, height))
    return cells


def bitmap_tile_size(bitmap_width: int, tile_width: int) -> int:
    """The tile size a ``bitmap_width``-pixel-wide image can be cut into.

    A wide-bitmap view only lines up when a whole number of tiles spans the
    bitmap's width, and a codec's natural tile is usually 8 — so a 306-pixel
    bitmap has no 8-px reading at all (306 / 8 = 38.25) and every row of the
    view slides two pixels further off. This picks the **largest divisor of
    ``bitmap_width`` that is no bigger than ``tile_width``** — 6 for that 306,
    so 51 tiles span it exactly. Square tiles: the same number sizes both axes,
    since only the width is constrained and a non-square display tile would
    just be a second thing to explain.

    Returns ``tile_width`` unchanged when there is no bitmap width to honour, so
    "off" is the ordinary path rather than a special case. A width smaller than
    one tile yields the width itself (it divides itself), and 1 is always a
    valid answer — a per-pixel grid, the degenerate but correct reading of a
    prime width.
    """
    if bitmap_width <= 0 or tile_width <= 0:
        return max(1, tile_width)
    for size in range(min(bitmap_width, tile_width), 0, -1):
        if bitmap_width % size == 0:
            return size
    return 1  # unreachable: 1 divides everything


def has_2d_reading(bytes_per_tile: int, tile_height: int) -> bool:
    """Whether this geometry can be read as a wide bitmap at all.

    The 2D walk splits a tile into ``tile_height`` equal per-row chunks and
    strides them apart, so geometry whose bytes don't divide into whole chunks
    has no wide-bitmap reading and everything falls back to the plain 1D order.
    One predicate for both directions of the walk (:func:`reflow_2d`,
    :func:`scatter_2d`) and for the callers deciding whether it applies.
    """
    return bytes_per_tile > 0 and tile_height > 0 and bytes_per_tile % tile_height == 0


def reflow_2d(
    window: bytes, bytes_per_tile: int, tile_height: int, columns: int
) -> bytes:
    """Rewalk a raw window from wide-bitmap (2D) order into per-tile (1D) order.

    In 2D the file is one bitmap ``columns`` tiles wide: a tile's successive
    pixel-rows sit ``columns`` tiles apart, so the row chunks of the ``columns``
    tiles across one block-row are interleaved. This gathers each tile's rows back
    into a contiguous ``bytes_per_tile`` block, so the *unmodified* codec then
    decodes it exactly as in 1D — the reflow is the whole difference between the
    modes. The window is padded up to a whole number of bitmap-rows first (the
    extra tiles decode blank, like any past-end padding).

    A ``bytes_per_tile`` that isn't a whole number of equal per-row chunks
    (``bytes_per_tile % tile_height``) has no wide-bitmap reading, so the window is
    returned untouched.
    """
    cols = max(1, columns)
    th = tile_height
    if not has_2d_reading(bytes_per_tile, th):
        return window
    row_bytes = bytes_per_tile // th
    stripe = cols * bytes_per_tile  # one bitmap-row of `cols` tiles
    pad = -len(window) % stripe
    if pad:
        window = window + bytes(pad)
    out = bytearray(len(window))
    # Within one bitmap-row the gather is a transpose of a th × cols grid of
    # row_bytes-sized chunks, and either axis can drive it. Walking the *chunk
    # bytes* moves one strided slice per (pixel row, byte) instead of a contiguous
    # copy per (tile, pixel row) — far fewer, larger operations whenever a tile's
    # pixel row is narrower than the bitmap is wide, which is the usual shape.
    strided = row_bytes < cols
    for stripe_base in range(0, len(window), stripe):
        if strided:
            end = stripe_base + cols * bytes_per_tile
            for pixel_row in range(th):
                src = stripe_base + pixel_row * (cols * row_bytes)
                dst = stripe_base + pixel_row * row_bytes
                for byte in range(row_bytes):
                    out[dst + byte : end : bytes_per_tile] = window[
                        src + byte : src + cols * row_bytes : row_bytes
                    ]
            continue
        for tile_x in range(cols):
            tile_base = stripe_base + tile_x * bytes_per_tile
            for pixel_row in range(th):
                s0 = stripe_base + pixel_row * (cols * row_bytes) + tile_x * row_bytes
                out[
                    tile_base + pixel_row * row_bytes : tile_base
                    + (pixel_row + 1) * row_bytes
                ] = window[s0 : s0 + row_bytes]
    return bytes(out)


def scatter_2d(
    out: bytearray,
    slot: int,
    data: bytes,
    bytes_per_tile: int,
    tile_height: int,
    columns: int,
) -> None:
    """Write one tile's contiguous bytes back into wide-bitmap (2D) order.

    The exact inverse of :func:`reflow_2d`'s gather, for a single tile: its
    pixel-rows go back to their strided homes ``columns`` tiles apart. It lives
    beside the gather because the two must stay inverses byte for byte — an
    edit written at the wrong stride lands in a neighbouring tile. Writes that
    fall past the buffer (a region clamped at end-of-data) are clipped, never
    grown.
    """
    row_bytes = bytes_per_tile // tile_height
    stripe_index, tile_x = divmod(slot, columns)
    stripe_base = stripe_index * columns * bytes_per_tile
    for row in range(tile_height):
        dst = stripe_base + row * (columns * row_bytes) + tile_x * row_bytes
        if dst >= len(out):
            return
        take = min(row_bytes, len(out) - dst)
        out[dst : dst + take] = data[row * row_bytes : row * row_bytes + take]
