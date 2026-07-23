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
    ArrangementPreset("linear", "Default 1D - Linear"),
    ArrangementPreset("2d", "2D - wide bitmap (N64/NDS)", two_dimensional=True),
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

    @property
    def is_plain(self) -> bool:
        """True when placement is plain row-major (no block grouping)."""
        return self._bc == 1 and self._br == 1

    @property
    def _bc(self) -> int:
        # A block can't be wider than the canvas; clamp so partial-width blocks
        # never place tiles past the right edge.
        return max(1, min(self.block_columns, self.columns))

    @property
    def _br(self) -> int:
        return max(1, self.block_rows)

    @property
    def _blocks_per_row(self) -> int:
        return max(1, self.columns // self._bc)

    def slot_to_cell(self, slot: int) -> tuple[int, int]:
        """The ``(tile_x, tile_y)`` canvas cell a linear slot lands in."""
        bc, br, bpr = self._bc, self._br, self._blocks_per_row
        per_blockrow = bpr * bc * br
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


def compose_linear(tiles: list, columns: int):
    """Lay ``tiles`` into a ``columns``-wide grid image (row-major).

    Returns a grid of the same type as the input tiles (index or direct-colour).
    """
    if not tiles:
        return IndexGrid(0, 0)
    cols = max(1, columns)
    tw, th = tiles[0].width, tiles[0].height
    rows = (len(tiles) + cols - 1) // cols
    return _compose(tiles, cols, tw, th, first_tile=0, rows=rows, layout=None)


def compose_window(
    tiles: list,
    columns: int,
    first_tile: int,
    rows: int,
    layout: BlockLayout | None = None,
):
    """Lay out ``rows`` rows of ``columns`` tiles starting at tile ``first_tile``.

    The image is always ``columns`` × ``rows`` tiles so the canvas size stays stable
    while navigating; slots outside ``tiles`` (a partial window at the file end, or a
    negative ``first_tile``) are left blank. ``layout`` places tiles into blocks
    (default: plain row-major). Returns a grid of the same type as the input tiles.
    Composing only the visible band is what keeps viewing large files cheap — see the
    module docstring.
    """
    if not tiles:
        return IndexGrid(0, 0)
    cols = max(1, columns)
    rows = max(1, rows)
    tw, th = tiles[0].width, tiles[0].height
    return _compose(
        tiles, cols, tw, th, first_tile=first_tile, rows=rows, layout=layout
    )


def _compose(
    tiles: list,
    cols: int,
    tw: int,
    th: int,
    *,
    first_tile: int,
    rows: int,
    layout: BlockLayout | None,
):
    """Blit ``cols`` × ``rows`` tiles from ``first_tile`` into one grid.

    ``layout`` decides each slot's cell (default: row-major); slots whose tile
    index falls outside ``tiles``, or whose cell falls outside the ``cols`` × ``rows``
    image, stay blank — so a full layout, a partial window, and a block grouping all
    share one path. Works for either grid type — index (1 byte/pixel) or
    direct-colour ARGB (4 bytes/pixel) — by blitting in units of the tiles'
    ``bytes_per_pixel`` and building the output grid of the same type.
    """
    if layout is None:
        layout = BlockLayout(cols)
    bpx = tiles[0].bytes_per_pixel
    image = type(tiles[0])(cols * tw, rows * th)
    dst = image.data
    dst_stride = cols * tw * bpx
    src_stride = tw * bpx
    row_bytes = tw * bpx
    for slot in range(cols * rows):
        idx = first_tile + slot
        if idx < 0 or idx >= len(tiles):
            continue
        tile_x, tile_y = layout.slot_to_cell(slot)
        if tile_x >= cols or tile_y >= rows:
            continue
        base_x = tile_x * tw
        base_y = tile_y * th
        src = tiles[idx].data
        for y in range(th):
            d0 = (base_y + y) * dst_stride + base_x * bpx
            s0 = y * src_stride
            dst[d0 : d0 + row_bytes] = src[s0 : s0 + row_bytes]
    return image


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
    th = max(1, tile_height)
    if bytes_per_tile <= 0 or bytes_per_tile % th != 0:
        return window
    row_bytes = bytes_per_tile // th
    stripe = cols * bytes_per_tile  # one bitmap-row of `cols` tiles
    pad = -len(window) % stripe
    if pad:
        window = window + bytes(pad)
    out = bytearray(len(window))
    for stripe_base in range(0, len(window), stripe):
        for tile_x in range(cols):
            tile_base = stripe_base + tile_x * bytes_per_tile
            for pixel_row in range(th):
                s0 = stripe_base + pixel_row * (cols * row_bytes) + tile_x * row_bytes
                out[
                    tile_base + pixel_row * row_bytes : tile_base
                    + (pixel_row + 1) * row_bytes
                ] = window[s0 : s0 + row_bytes]
    return bytes(out)
