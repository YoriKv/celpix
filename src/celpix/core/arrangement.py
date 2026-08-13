"""Tile arrangement: composing a list of tiles into one viewable image.

The pixel codec decodes to a flat list of tiles; an arrangement lays them out into
a single :class:`IndexGrid`. The default is **linear** (1D): tiles fill
left-to-right, top-to-bottom, ``columns`` tiles wide.

Two arrangement axes sit on top of that, both pure **display** state (they never
change the codec — overview.md §4):

- **Block grouping / order** (:class:`BlockLayout`) — group tiles into
  ``block_columns`` × ``block_rows`` blocks, filled row-major, column-major, or
  row-interleaved. This is *placement only*: the same decoded tiles land in
  different cells. It is how N-tile sprites and 16×16 tile groups read as
  coherent units — 8×16 (row-interleave) and Mega Drive / Neo Geo sprites
  (column-major). This grouping is the **user's**, over decoded tiles; a cell
  format's *metatile* and a map's *stamp* are different things that draw the same
  square (``docs/design/terminology.md``).

  There is **one place it is more than display**, and it is worth knowing about:
  on an entry ticked *Use as Font*, the block is what a character code numbers.
  An 8×16 glyph is two tiles and this is the only thing that says which two, so a
  fontmap reads it as a claim about the sheet rather than as a preference
  (:meth:`BlockLayout.block_slots`, ``docs/design/fontmap-entry.md`` §4).
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
# plain → 2D → the sprite and tile-block groupings. The mappings behind each are worked
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
    # 2×2 tile blocks read row-major, commonly labelled x16y16: 16×16 units of
    # four 8×8
    # tiles. A *view* grouping, which is why it is a block and not a metatile —
    # it asserts nothing about any cell format (docs/design/terminology.md).
    ArrangementPreset(
        "block-2x2",
        "16×16 tile blocks (2×2)",
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
    """Maps a window's linear tile slots to canvas positions, and back.

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

    This is placement machinery, and its "block" is **whatever unit the caller
    passes** — the view's own grouping, a metatile cell's tiles, a whole stamp's,
    or a page of a multi-map file. That reuse is the point, but the word does not
    travel with it: a caller names the unit it hands over rather than calling a
    stamp or a page a block (``docs/design/terminology.md``).
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

    @cached_property
    def block_row_positions(self) -> tuple[tuple[int, int], ...]:
        """:meth:`slot_to_pos` for one block row — the whole mapping, tiled.

        The placement repeats with period :attr:`slots_per_block_row`, shifted
        down by ``block_rows`` each time: the position's *column* depends only on the
        slot's position within its block row, and the row differs by a constant.
        So a composer that would otherwise call :meth:`slot_to_pos` per slot can
        resolve one period and index it, which is what makes laying out a large
        block-arranged window (a metatile map is thousands of slots) cost the
        period rather than the window.
        """
        return tuple(self.slot_to_pos(slot) for slot in range(self.slots_per_block_row))

    def slot_to_pos(self, slot: int) -> tuple[int, int]:
        """The ``(tile_x, tile_y)`` canvas position a linear slot lands in."""
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

    def pos_to_slot(self, tile_x: int, tile_y: int) -> int | None:
        """The linear slot at position ``(tile_x, tile_y)`` — ``None`` if no tile
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

    def blocks(self, slots: int) -> int:
        """How many whole blocks ``slots`` tiles make up at this layout.

        The count in the unit the *blocks* are, which is what a caller numbering
        blocks rather than tiles needs — a 256-tile sheet read as 1x2 blocks is
        128 of them. Whole ones only: a trailing partial block is half a picture
        of something, and nothing that numbers blocks has a number for it.
        """
        per_row = self._blocks_per_row
        return max(0, slots // (per_row * self._bc * self._br)) * per_row

    def block_slots(self, block: int) -> list[int]:
        """The linear slots block number ``block`` covers, in reading order.

        The inverse of the placement, asked of a whole block rather than of one
        slot: blocks are numbered left to right and top to bottom, and what comes
        back is the tiles inside one of them, row by row — which is the order a
        composer places them in and the order the tiles are wanted in.

        This is what lets a caller *number in blocks* while the file numbers in
        tiles. The font alphabet is the case it was written for: an 8x16 glyph is
        two tiles the sheet stores a whole row apart, and the character code is
        the block number (``docs/design/fontmap-entry.md`` §4).

        Slots that fall outside the layout — a partial block column past the last
        whole one — are dropped rather than clamped, so a run that comes back
        short is short rather than wrong.
        """
        bc, br = self._bc, self._br
        brow, bx = divmod(max(0, block), self._blocks_per_row)
        found = (
            self.pos_to_slot(bx * bc + dx, brow * br + dy)
            for dy in range(br)
            for dx in range(bc)
        )
        return [slot for slot in found if slot is not None]


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
    row_bytes = tw * bpx
    blank = bytes(th * row_bytes)  # a whole missing tile: past the end, or before it
    datas = [tile.data for tile in tiles]
    if not layout.is_plain:
        # Resolve the block placement into a plain canvas-ordered list first, so
        # the blit below stays the one-join-per-image-row loop the plain layout
        # gets. A block layout only decides *which* tile lands in a cell, and
        # answering that once per cell is far cheaper than the alternative — a
        # slice assignment per tile per pixel row, which is what placing tiles
        # one at a time costs.
        datas = _in_pos_order(datas, layout, cols, rows, first_tile, blank)
        first_tile = 0
    _compose_plain(image.data, datas, cols, rows, th, row_bytes, first_tile, blank)
    return image


def _in_pos_order(
    datas: list,
    layout: BlockLayout,
    cols: int,
    rows: int,
    first_tile: int,
    blank: bytes,
) -> list:
    """``datas`` reordered from linear slots into row-major canvas positions.

    Undoes a :class:`BlockLayout`'s placement into the plain order
    :func:`_compose_plain` blits, filling a cell no slot reaches with ``blank``:
    a partial-width block column, or a window running past the last tile.

    The mapping is read off :attr:`BlockLayout.block_row_positions` rather than
    asked per slot, since it repeats every block row (see there).
    """
    count = len(datas)
    period = layout.slots_per_block_row
    pattern = layout.block_row_positions
    height = max(1, layout.block_rows)  # the clamp slot_to_pos applies
    out = [blank] * (cols * rows)
    for slot in range(cols * rows):
        index = first_tile + slot
        if index < 0 or index >= count:
            continue
        block_row, rem = divmod(slot, period)
        tile_x, tile_y = pattern[rem]
        tile_y += block_row * height
        if tile_x < cols and tile_y < rows:
            out[tile_y * cols + tile_x] = datas[index]
    return out


def _compose_plain(
    dst: bytearray,
    datas: list,
    cols: int,
    rows: int,
    th: int,
    row_bytes: int,
    first_tile: int,
    blank: bytes,
) -> None:
    """Blit a row-major window, one whole cell row of the image per write.

    The plain layout puts a cell row's tiles side by side, so each image row is
    the concatenation of the same pixel row of ``cols`` consecutive tiles — a
    ``join`` instead of a slice assignment per tile per row. The difference is
    the whole cost of composing when the tiles are small: a bitmap width can cut
    a window into tens of thousands of them.

    **A repeated cell row is composed once.** One cell row makes one contiguous
    band of the image, and which band is decided entirely by *which tile objects*
    sit in that row — so a row identical to the one above it is a second store of
    the same bytes. That is not a rare case in the formats this draws: a panel is
    mostly empty, a few hundred of its 512 rows carrying anything at all
    (``docs/design/tilemap-entry.md`` §9), and a screen's backdrop covers bands of
    it. The test is on the tiles' **identity**, not their pixels: the render memos
    hand back the same grid object for a repeat
    (:func:`~celpix.pipeline.pipeline.expand_cells`) and nothing mutates a tile
    during a compose, so it is a pointer compare per tile rather than a pixel one.

    Only the row immediately above is compared, and the band is re-read out of
    the image rather than held. A run is what these files actually hold — an
    empty region is contiguous — so a table of every distinct row would buy the
    non-adjacent case at the price of a second copy of the whole image, on a
    picture that may have no repeats at all. That picture is the pixel view,
    which shares this function and is all-distinct by nature; the comparison
    short-circuits on its very first tile there, which is what keeps the check
    from costing it anything.
    """
    count = len(datas)
    span = cols * row_bytes
    height = th * span  # one cell row's band of the image
    pos = 0
    previous: list | None = None
    for cell_y in range(rows):
        base = first_tile + cell_y * cols
        row_tiles = [
            datas[idx] if 0 <= idx < count else blank
            for idx in range(base, base + cols)
        ]
        if previous is not None and all(a is b for a, b in zip(row_tiles, previous)):
            dst[pos : pos + height] = dst[pos - height : pos]
            pos += height
            continue
        previous = row_tiles
        for y in range(th):
            start = y * row_bytes
            stop = start + row_bytes
            # A list rather than a generator: join materializes one either way,
            # and this is the loop that runs once per image row of every repaint.
            dst[pos : pos + span] = b"".join([src[start:stop] for src in row_tiles])
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
    an edge tile as whatever the file already holds instead of laying the pad
    down over pixels it has no colors for.

    This is how external pixels (a pasted or imported image) become tiles: the
    importer quantizes the whole image once, then splits it here.
    """
    bpx = grid.bytes_per_pixel
    src = grid.data
    src_stride = grid.width * bpx
    row_bytes = tile_width * bpx
    tiles = []
    positions = _split_positions(
        grid.width, grid.height, tile_width, tile_height, layout
    )
    for base_x, base_y, cover_w, cover_h in positions:
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
    back into a file must not lay down over real pixels.
    """
    positions = _split_positions(
        grid_width, grid_height, tile_width, tile_height, layout
    )
    return [(w, h) for _base_x, _base_y, w, h in positions]


def _split_positions(
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
    positions = []
    for slot in range(cols * rows):
        tile_x, tile_y = layout.slot_to_pos(slot)
        if tile_x >= cols or tile_y >= rows:
            positions.append((0, 0, 0, 0))
            continue
        base_x, base_y = tile_x * tile_width, tile_y * tile_height
        width = max(0, min(tile_width, grid_width - base_x))
        height = max(0, min(tile_height, grid_height - base_y))
        if not (width and height):
            width = height = 0
        positions.append((base_x, base_y, width, height))
    return positions


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


def tile_pixel_spans(
    slot: int,
    tile_width: int,
    tile_height: int,
    columns: int,
    two_dimensional: bool,
) -> list[tuple[int, int]]:
    """The ``(start, length)`` **pixel** runs one display slot occupies.

    The addressing pinned palette regions are built on
    (:mod:`celpix.core.paletteregions`): which pixels of the picture is the tile at
    this slot made of? Pixels rather than bytes because most retro codecs are
    planar — a byte there is one bitplane row, so a byte range names no run of
    pixels at all — while a pixel index is well defined whatever the codec.

    The space is the picture as laid out. In the ordinary 1D reading that is
    tile-major, so a tile is one contiguous ``tile_width * tile_height`` run. Under
    the 2D walk the source *is* one wide bitmap ``columns`` tiles across, so the
    space is that bitmap row-major and a tile is ``tile_height`` runs of
    ``tile_width``, a whole bitmap-row apart — the picture-side mirror of the byte
    strides :func:`reflow_2d` gathers along.

    Positions are relative to the view origin (slot 0), so a caller adds its own
    anchor once.
    """
    per_tile = tile_width * tile_height
    if not two_dimensional:
        return [(slot * per_tile, per_tile)]
    cols = max(1, columns)
    stripe_index, tile_x = divmod(slot, cols)
    stripe_base = stripe_index * cols * per_tile
    return [
        (stripe_base + row * (cols * tile_width) + tile_x * tile_width, tile_width)
        for row in range(tile_height)
    ]


def tile_first_pixel(
    slot: int,
    tile_width: int,
    tile_height: int,
    columns: int,
    two_dimensional: bool,
) -> int:
    """Where the tile at ``slot`` starts — :func:`tile_pixel_spans`' first run alone.

    What decides which pinned region a tile belongs to. Split out because the
    render path asks it once per visible tile and has no use for the rest of the
    runs: building the full list there would allocate ``tile_height`` tuples per
    tile, every refresh, to read one number off the front.

    Monotonic in ``slot`` under both walks — within a 2D stripe tiles advance by
    ``tile_width``, and a stripe boundary jumps forward by more — so a window's
    lookups walk the sorted regions forwards.
    """
    per_tile = tile_width * tile_height
    if not two_dimensional:
        return slot * per_tile
    cols = max(1, columns)
    stripe_index, tile_x = divmod(slot, cols)
    return stripe_index * cols * per_tile + tile_x * tile_width
