"""PS2 textures stored in GS-memory order — ``reshape.ps2-gs-swizzle``.

A PS2 game can keep an indexed texture in the order the Graphics Synthesizer's
memory holds it rather than in rows. It uploads the bytes as a 32-bit image
(PSMCT32), and the GS samples the same memory as a 4bpp or 8bpp texture (PSMT4
/ PSMT8). The two formats tile memory differently: pages, blocks inside a page,
columns inside a block, and a half-column shift on alternate rows. So the file
holds the texture's pixels in GS-memory order, and read as rows it draws as
interlaced stripes — recognisable shapes cut into bands, which reads as a wrong
width but no width fixes.

The reshape replays that round trip. It writes the region as PSMCT32 rows,
reads it back through PSMT4/PSMT8 addressing, and hands the pixel codec plain
rows; ``unshape`` applies the inverse, so an edit lands back in GS order. The
tables are the GS's own block and column layouts
(``docs/graphics-formats-reference/ps2-gs-formats.md``). At 8bpp the result
equals the widely used closed-form PSMT8 unswizzle pixel for pixel.

**The width is a preset's, the height the region's.** The permutation depends on
the texture's width, and a reshape has no per-entry inputs, so each shipped
preset names a depth and a width (``ps2-psmt4-w128``) and the height is whatever
the region's length makes it.

**The upload rectangle is solved for, not stated.** Nothing in the bytes says
what shape the game uploaded them as. The PSMCT32 rectangle has to land on
exactly the GS addresses the texture reads — the only uploads that leave the
texture whole — and for a texture narrower or shorter than a page that is
usually one rectangle (64x16 for a 64x128 PSMT4 texture, 32x32 for 128x64).
Where several fit, which one a game used is its converter's convention, not the
GS's. The published one is taken: at 4bpp the upload is half the texture's
*height* wide and a quarter of its *width* tall, at 8bpp half and half (the PS2
Linux "HOWTO swizzle textures"). The 4bpp rule is also the one rectangle that
fits wherever only one does, so it is a single rule for every shape; a 256x128
texture read with the transposed one scrambles its pages. A region that is not
a whole texture of the preset's width, or
that no upload rectangle fits, is refused rather than passed through: stripes
with no message would look like the wrong preset rather than the wrong slice.

Which textures a game stores this way is the game's choice, not the GS's; the
stripes are the tell (``docs/rom-mapping/console-others.md``).
"""

from __future__ import annotations

from functools import lru_cache

from celpix.core.context import PipelineContext
from celpix.core.errors import Stage
from celpix.plugins._params import adapter_spec, integer, one_of
from celpix.plugins.base import reshape_info

GS_SWIZZLE_ENGINE = "reshape.ps2-gs-swizzle"

# The GS's texture limit: TEX0 states width and height as log2, at most 10.
MAX_WIDTH = 1024

# Block number by (block row, block column) inside a page. PSMCT32 and PSMT8
# pages are 8 blocks across and 4 down; a PSMT4 page is 4 across and 8 down.
_BLOCK32 = (
    (0, 1, 4, 5, 16, 17, 20, 21),
    (2, 3, 6, 7, 18, 19, 22, 23),
    (8, 9, 12, 13, 24, 25, 28, 29),
    (10, 11, 14, 15, 26, 27, 30, 31),
)
_BLOCK8 = _BLOCK32
_BLOCK4 = (
    (0, 2, 8, 10),
    (1, 3, 9, 11),
    (4, 6, 12, 14),
    (5, 7, 13, 15),
    (16, 18, 24, 26),
    (17, 19, 25, 27),
    (20, 22, 28, 30),
    (21, 23, 29, 31),
)
# Word inside a PSMCT32 block (8x8 pixels, 64 words) by (row, column).
_COLUMN32 = (
    (0, 1, 4, 5, 8, 9, 12, 13),
    (2, 3, 6, 7, 10, 11, 14, 15),
    (16, 17, 20, 21, 24, 25, 28, 29),
    (18, 19, 22, 23, 26, 27, 30, 31),
    (32, 33, 36, 37, 40, 41, 44, 45),
    (34, 35, 38, 39, 42, 43, 46, 47),
    (48, 49, 52, 53, 56, 57, 60, 61),
    (50, 51, 54, 55, 58, 59, 62, 63),
)


def _block_columns(
    a0: list[int], a1: list[int], b0: list[int], b1: list[int], per_column: int
) -> tuple[tuple[int, ...], ...]:
    """A PSMT8/PSMT4 block's pixel-to-position table from its first column.

    A block is four columns of four rows. The first column's rows are ``a0 a1
    b0 b1``; every odd column swaps its row pairs and steps the pixel parity by
    one — the half-column shift — and each column sits ``per_column`` pixels on.
    """
    rows: list[tuple[int, ...]] = []
    for column in range(4):
        base = per_column * column
        if column % 2 == 0:
            pattern = ((a0, 0), (a1, 0), (b0, 0), (b1, 0))
        else:
            pattern = ((b0, -1), (b1, -1), (a0, 1), (a1, 1))
        rows += [tuple(v + step + base for v in row) for row, step in pattern]
    return tuple(rows)


# Byte inside a PSMT8 block (16x16 pixels) by (row, column).
_COLUMN8 = _block_columns(
    [0, 4, 16, 20, 32, 36, 48, 52, 2, 6, 18, 22, 34, 38, 50, 54],
    [8, 12, 24, 28, 40, 44, 56, 60, 10, 14, 26, 30, 42, 46, 58, 62],
    [33, 37, 49, 53, 1, 5, 17, 21, 35, 39, 51, 55, 3, 7, 19, 23],
    [41, 45, 57, 61, 9, 13, 25, 29, 43, 47, 59, 63, 11, 15, 27, 31],
    64,
)
# Nibble inside a PSMT4 block (32x16 pixels) by (row, column).
_COLUMN4 = _block_columns(
    [0, 8, 32, 40, 64, 72, 96, 104, 2, 10, 34, 42, 66, 74, 98, 106,
     4, 12, 36, 44, 68, 76, 100, 108, 6, 14, 38, 46, 70, 78, 102, 110],
    [16, 24, 48, 56, 80, 88, 112, 120, 18, 26, 50, 58, 82, 90, 114, 122,
     20, 28, 52, 60, 84, 92, 116, 124, 22, 30, 54, 62, 86, 94, 118, 126],
    [65, 73, 97, 105, 1, 9, 33, 41, 67, 75, 99, 107, 3, 11, 35, 43,
     69, 77, 101, 109, 5, 13, 37, 45, 71, 79, 103, 111, 7, 15, 39, 47],
    [81, 89, 113, 121, 17, 25, 49, 57, 83, 91, 115, 123, 19, 27, 51, 59,
     85, 93, 117, 125, 21, 29, 53, 61, 87, 95, 119, 127, 23, 31, 55, 63],
    128,
)  # fmt: skip


def _psmct32_byte(x: int, y: int, pages_across: int) -> int:
    """Byte address of PSMCT32 pixel (x, y); a page is 64x32 pixels, 8 KiB."""
    page = (y // 32) * pages_across + x // 64
    block = _BLOCK32[y % 32 // 8][x % 64 // 8]
    return (page * 2048 + block * 64 + _COLUMN32[y % 8][x % 8]) * 4


def _psmt8_byte(x: int, y: int, pages_across: int) -> int:
    """Byte address of PSMT8 pixel (x, y); a page is 128x64 pixels."""
    page = (y // 64) * pages_across + x // 128
    block = _BLOCK8[y % 64 // 16][x % 128 // 16]
    return page * 8192 + block * 256 + _COLUMN8[y % 16][x % 16]


def _psmt4_nibble(x: int, y: int, pages_across: int) -> int:
    """Nibble address of PSMT4 pixel (x, y); a page is 128x128 pixels."""
    page = (y // 128) * pages_across + x // 128
    block = _BLOCK4[y % 128 // 16][x % 128 // 32]
    return page * 16384 + block * 512 + _COLUMN4[y % 16][x % 32]


@lru_cache(maxsize=8)
def gs_permutation(bpp: int, width: int, height: int) -> tuple[int, ...] | None:
    """``perm[i]``: the stored pixel that row-order pixel ``i`` is read from.

    A pixel is a nibble at 4bpp (low nibble first, as PSMT4 stores it) and a
    byte at 8bpp. None when no PSMCT32 upload covers exactly the memory the
    texture reads. Cached because a texture's permutation is the same on every
    load and costs a pass over every pixel to build.
    """
    size = width * height * bpp // 8
    texture_pages = max(1, width // 128)  # PSMT4 and PSMT8 pages are 128 wide
    if bpp == 4:
        reads = [
            _psmt4_nibble(x, y, texture_pages)
            for y in range(height)
            for x in range(width)
        ]
        wanted = {nibble >> 1 for nibble in reads}
    else:
        reads = [
            _psmt8_byte(x, y, texture_pages)
            for y in range(height)
            for x in range(width)
        ]
        wanted = set(reads)
    # The published conversion's upload shape, tried first (module docstring).
    convention = (height // 2, width // 4) if bpp == 4 else (width // 2, height // 2)
    shapes = [
        (w32, size // 4 // w32)
        for w32 in (8, 16, 32, 64, 128, 256, 512, 1024)
        if size // 4 // w32 and w32 * (size // 4 // w32) * 4 == size
    ]
    shapes.sort(key=lambda shape: shape != convention)
    for w32, h32 in shapes:
        pages_across = max(1, (w32 + 63) // 64)
        stored_at: dict[int, int] = {}  # GS byte address -> region byte
        for y in range(h32):
            for x in range(w32):
                address = _psmct32_byte(x, y, pages_across)
                for k in range(4):
                    stored_at[address + k] = (y * w32 + x) * 4 + k
        if stored_at.keys() != wanted:
            continue
        if bpp == 4:
            return tuple(stored_at[n >> 1] * 2 + (n & 1) for n in reads)
        return tuple(stored_at[a] for a in reads)
    return None


class GsSwizzleReshape:
    """A PSMT4 or PSMT8 texture of one width, stored in GS-memory order.

    Depth and width are constructor arguments rather than subclasses, as with
    :class:`~celpix.plugins.split_parts.SplitPartsReshape`: every shipped width
    is a TOML preset on this one engine (:func:`gs_swizzle_from_spec`).
    """

    def __init__(
        self,
        bpp: int,
        width: int,
        *,
        plugin_id: str = GS_SWIZZLE_ENGINE,
        name: str = "PS2 GS swizzle",
        category: str = "",
    ) -> None:
        self.bpp = bpp
        self.width = width
        self.info = reshape_info(plugin_id, name, category)

    def _perm(self, length: int) -> tuple[int, ...]:
        pixels = length * 8 // self.bpp
        height = pixels // self.width
        perm = None
        if height and height * self.width == pixels and pixels * self.bpp == length * 8:
            perm = gs_permutation(self.bpp, self.width, height)
        if perm is None:
            raise ValueError(
                f"{length} bytes is not a {self.bpp}bpp texture {self.width} pixels "
                f"wide that a 32-bit upload fills: slice exactly the texture's pixels"
            )
        return perm

    def _apply(self, data: bytes, forward: bool) -> bytes:
        perm = self._perm(len(data))
        if self.bpp == 8:
            src = data
        else:
            src = bytearray(len(data) * 2)
            src[0::2] = bytes(b & 0xF for b in data)
            src[1::2] = bytes(b >> 4 for b in data)
        dst = bytearray(len(src))
        if forward:
            for i, j in enumerate(perm):
                dst[i] = src[j]
        else:
            for i, j in enumerate(perm):
                dst[j] = src[i]
        if self.bpp == 8:
            return bytes(dst)
        return bytes(lo | hi << 4 for lo, hi in zip(dst[0::2], dst[1::2], strict=True))

    def reshape(self, data: bytes, ctx: PipelineContext) -> bytes:
        return self._apply(data, forward=True)

    def unshape(self, data: bytes, ctx: PipelineContext) -> bytes:
        return self._apply(data, forward=False)


def gs_swizzle_from_spec(spec: dict) -> GsSwizzleReshape:
    """Build the plugin a parsed ``reshape.ps2-gs-swizzle`` preset spec describes."""
    plugin_id, name, category, params = adapter_spec(
        spec, engine_id=GS_SWIZZLE_ENGINE, stage=Stage.RESHAPE, known=("bpp", "width")
    )
    if "bpp" not in params or "width" not in params:
        raise ValueError("params.bpp and params.width are required")
    bpp = one_of(params, "bpp", (4, 8), None)
    width = integer(params, "width", low=8, high=MAX_WIDTH)
    if width & (width - 1):
        raise ValueError(
            f"width must be a power of two, as TEX0 states it, got {width}"
        )
    return GsSwizzleReshape(
        bpp, width, plugin_id=plugin_id, name=name, category=category
    )
