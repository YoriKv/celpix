"""The import pathway: outside pixels → tiles in a document's own format.

Everything that brings graphics in from *outside* the pipeline enters here — a
paste from an external image editor, an imported PNG. The pipeline proper reads
bytes that are already in a retro format; import instead receives finished
pixels (full 32-bit color, arbitrary size) and has to answer two questions the
pipeline never asks:

1. **Which index is this color?** An indexed target can only reference the
   entries of its active subpalette, so every incoming color is fitted to the
   nearest one (:mod:`celpix.core.quantize`). A direct-color target skips this —
   it stores colors, and its codec's masks do the narrowing.
2. **Where do the tiles start and stop?** The image is one rectangle; the model
   is a linear stream of fixed-size tiles. It is cut on the tile grid and walked
   back into slot order through the same :class:`BlockLayout` the view composes
   with, so a blocked view (a 2×2 metatile, an 8×16 sprite) round-trips.

The result is a list of tiles, a report of how lossy the fit was, and how far
into each edge tile the image reached — the caller merges the uncovered
remainder with what the file already holds (:func:`merge_uncovered`), encodes
through the document's codec and splices the bytes. Qt-free: converting a
``QImage`` into the :class:`ArgbGrid` this takes is the ``ui`` side's job, so
the same entry point serves a file importer that never touches Qt.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from celpix.core import ceil_div
from celpix.core.argb_grid import ArgbGrid
from celpix.core.arrangement import BlockLayout, split_coverage, split_grid
from celpix.core.index_grid import IndexGrid
from celpix.core.quantize import ColorMatcher, QuantizeReport


@dataclass(frozen=True)
class ImportTarget:
    """The shape incoming pixels must be fitted into: one document's format.

    ``colors`` is the candidate palette **window** — the active subpalette, so a
    produced index is exactly what a tile stores. It is empty for a
    ``direct_color`` target, which keeps ARGB pixels and lets its codec's masks
    do the narrowing.

    The block axes mirror the view's, so an image is cut into slots the same way
    the view composed them; ``columns`` for the layout comes from the image's own
    width, not the view's, since a pasted image carries its own extent.
    """

    tile_width: int
    tile_height: int
    colors: tuple[int, ...] = ()
    direct_color: bool = False
    block_columns: int = 1
    block_rows: int = 1
    block_order: str = "row"
    # Where transparent source pixels land. Index 0 is the retro convention (and
    # the entry Celpix's PNG export marks transparent); None means "no hole" and
    # transparent pixels are matched on color like any other.
    transparent_index: int | None = 0

    def layout_for(self, columns: int) -> BlockLayout:
        return BlockLayout(
            max(1, columns), self.block_columns, self.block_rows, self.block_order
        )


@dataclass(frozen=True)
class ImportedTiles:
    """Tiles ready for the document's codec, plus how they were arrived at.

    ``coverage`` is parallel to ``tiles``: the ``(width, height)`` of each tile
    the source image actually reached. An image that doesn't end on a tile
    boundary leaves the rest of its edge tiles uncovered, and a write must fill
    that remainder from the file rather than from the padding
    (:func:`merge_uncovered`). Empty means "every tile fully covered" — what an
    internal clipboard payload, which carries whole tiles, hands over.
    """

    tiles: list = field(default_factory=list)
    columns: int = 0  # the image's width in tiles — its natural row length
    rows: int = 0
    report: QuantizeReport = QuantizeReport()
    coverage: tuple[tuple[int, int], ...] = ()

    def covered(self, index: int) -> tuple[int, int] | None:
        """Slot ``index``'s covered rectangle, or None when it is fully covered."""
        if index >= len(self.coverage):
            return None
        tile = self.tiles[index]
        width, height = self.coverage[index]
        if width >= tile.width and height >= tile.height:
            return None
        return width, height

    @property
    def partial(self) -> bool:
        """Whether any tile is only partly covered — i.e. whether a write has to
        read the file back to fill the rest in."""
        return any(self.covered(i) is not None for i in range(len(self.tiles)))


def import_argb(source: ArgbGrid, target: ImportTarget) -> ImportedTiles:
    """Fit a full-color image into ``target``'s tiles.

    Quantizes the whole image once (so the matcher's per-color cache is shared
    across every tile) and then cuts it on the tile grid. A direct-color target
    skips quantization entirely and carries the ARGB pixels through.
    """
    columns = ceil_div(source.width, target.tile_width) if source.width else 0
    rows = ceil_div(source.height, target.tile_height) if source.height else 0
    if not columns or not rows:
        return ImportedTiles([], 0, 0, QuantizeReport())
    layout = target.layout_for(columns)
    coverage = tuple(
        split_coverage(
            source.width, source.height, target.tile_width, target.tile_height, layout
        )
    )
    if target.direct_color:
        tiles = split_grid(source, target.tile_width, target.tile_height, layout)
        return ImportedTiles(tiles, columns, rows, QuantizeReport(), coverage)
    grid, report = quantize_grid(source, target)
    tiles = split_grid(grid, target.tile_width, target.tile_height, layout)
    return ImportedTiles(tiles, columns, rows, report, coverage)


def merge_uncovered(tile, base, covered: tuple[int, int] | None):
    """``tile``, with everything outside its ``covered`` rectangle taken from
    ``base`` — the tile already in the file at that position.

    An imported or pasted image rarely ends on a tile boundary, and the pad
    :func:`~celpix.core.arrangement.split_grid` leaves is not data. Writing it
    would erase pixels the source never spoke for, so the destination's own
    pixels fill the remainder instead and only the covered rectangle changes.

    ``covered`` is ``None`` for a fully covered tile (``tile`` unchanged), and
    the whole tile is left alone when nothing at all was covered. A ``base`` of a
    different type or geometry can't be merged with, so ``tile`` wins there —
    the caller decoded it from the same document, so that only happens if the
    view's format moved underneath the paste.
    """
    if covered is None:
        return tile
    width, height = covered
    if (
        base is None
        or type(base) is not type(tile)
        or (base.width, base.height) != (tile.width, tile.height)
    ):
        return tile
    if width <= 0 or height <= 0:
        return base
    merged = type(tile)(tile.width, tile.height, bytes(base.data))
    stride = tile.width * tile.bytes_per_pixel
    span = width * tile.bytes_per_pixel
    dst, src = merged.data, tile.data
    for y in range(height):
        dst[y * stride : y * stride + span] = src[y * stride : y * stride + span]
    return merged


def quantize_grid(
    source: ArgbGrid, target: ImportTarget
) -> tuple[IndexGrid, QuantizeReport]:
    """Map every pixel of ``source`` to its nearest entry in ``target.colors``.

    Returns the index grid plus a :class:`QuantizeReport` — pixel and distinct-
    color counts of what matched exactly, which is what tells the user whether a
    paste landed faithfully or was approximated.
    """
    matcher = ColorMatcher(target.colors, transparent_index=target.transparent_index)
    grid = IndexGrid(source.width, source.height)
    src, dst = source.data, grid.data
    exact_pixels = 0
    for i in range(source.width * source.height):
        argb = int.from_bytes(src[i * 4 : i * 4 + 4], "little")
        index, exact = matcher.match(argb)
        dst[i] = index & 0xFF
        exact_pixels += exact
    seen = matcher.cache
    return grid, QuantizeReport(
        pixels=len(dst),
        exact_pixels=exact_pixels,
        source_colors=len(seen),
        exact_colors=sum(1 for _index, exact in seen.values() if exact),
    )


def import_indexed(
    tiles: list, source_colors: tuple[int, ...], target: ImportTarget
) -> tuple[list, QuantizeReport]:
    """Re-fit already-indexed tiles whose indices don't suit ``target``.

    The internal clipboard carries indices, and indices *are* the data — a
    same-format paste keeps them verbatim. This is the fallback for when it
    can't: the source's own palette turns each index back into a color, and that
    color is matched into the target's window. So copying a 4bpp sprite into a
    2bpp view (or into a document with a different palette) still lands as the
    closest picture rather than as scrambled indices.

    Takes and returns tiles in linear slot order — no layout is involved, since
    an internal copy already carries tiles rather than a composed image.
    """

    def color_of(index: int) -> int:
        return source_colors[index] if index < len(source_colors) else 0

    if target.direct_color:
        out = []
        for tile in tiles:
            mapped = ArgbGrid(tile.width, tile.height)
            buf = mapped.data
            for i, index in enumerate(tile.data):
                buf[i * 4 : i * 4 + 4] = (color_of(index) & 0xFFFFFFFF).to_bytes(
                    4, "little"
                )
            out.append(mapped)
        return out, QuantizeReport()

    matcher = ColorMatcher(target.colors, transparent_index=target.transparent_index)
    out = []
    exact_pixels = pixels = 0
    for tile in tiles:
        mapped = IndexGrid(tile.width, tile.height)
        buf = mapped.data
        for i, index in enumerate(tile.data):
            new_index, exact = matcher.match(color_of(index))
            buf[i] = new_index & 0xFF
            exact_pixels += exact
        pixels += tile.width * tile.height
        out.append(mapped)
    seen = matcher.cache
    return out, QuantizeReport(
        pixels=pixels,
        exact_pixels=exact_pixels,
        source_colors=len(seen),
        exact_colors=sum(1 for _index, exact in seen.values() if exact),
    )
