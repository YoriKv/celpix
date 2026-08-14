"""Decoded data turned into a picture: tiles, tilemaps and sprite objects.

Everything between a decoded tile and the image a view, an export or a hit test
works on. Three shapes share the machinery: a run of tiles laid out through an
arrangement, a tilemap whose cells name tiles out of a bank, and a sprite object
whose subsprites sit at signed pixel offsets in a shared bounding box. They are
one module because the picture on screen and the picture an export writes have
to be the same one — :func:`tilemap_image` and :func:`tile_source_image` are
deliberately the same composition, so what a panel offers is what will land.

For the same reason the things a second caller could get subtly different are
settled here once: the palette-row arithmetic every named row goes through
(:func:`drawn_palette_row`), the tile bank and its cache
(:func:`tile_bank`), the byte region a tile-run edit covers under the 2D walk
(:func:`tile_region`), and the sprite sheet's grid (:func:`sprite_sheet`), which
the render and the canvas both measure against.

Nothing here reads or writes a file. The bytes arrive already read, reshaped and
decompressed by :mod:`celpix.pipeline.pipeline`, and what goes back to disk goes
back through it — :func:`encode_tiles` produces the replacement bytes for a tile
edit and stops there. Qt-free like the rest of the model layer: the result is an
:class:`~celpix.core.index_grid.IndexGrid`, and painting one is the UI's job.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import NamedTuple

from celpix.core import ceil_div, transform
from celpix.core.arrangement import (
    BlockLayout,
    compose_window,
    has_2d_reading,
    reflow_2d,
    scatter_2d,
)
from celpix.core.context import PipelineContext
from celpix.core.document import Document
from celpix.core.errors import Pathway, PipelineError, Stage
from celpix.core.index_grid import IndexGrid
from celpix.core.sprite import Frame, Subsprite, frame_bounds
from celpix.core.tilemap import Cell, resolve_cell
from celpix.pipeline._stage import _run, tile_params
from celpix.pipeline.metrics import pixel_bpp
from celpix.plugins.base import PixelCodecPlugin
from celpix.plugins.registry import Registry


def decode_window(
    doc: Document,
    reg: Registry,
    first_tile: int,
    count: int,
    nudge: int = 0,
    *,
    columns: int | None = None,
    two_dimensional: bool = False,
) -> list[IndexGrid]:
    """Decode ``count`` tiles starting at tile ``first_tile`` — deferred decode.

    Slices the raw pixel bytes to just that window and hands the codec the slice;
    because the codec decodes exactly the tiles in the buffer it is given, no
    whole-file decode is needed. A partial/empty window (near or past the end)
    decodes to fewer/zero tiles. ``nudge`` shifts the tile grid by that many
    bytes (sub-tile alignment — see :meth:`Document.window_bytes`).

    With ``two_dimensional`` (and the view's ``columns``), the raw window is
    rewalked from wide-bitmap order into per-tile order before decode
    (:func:`~celpix.core.arrangement.reflow_2d`) — the codec is unchanged.
    """
    window = doc.window_bytes(first_tile, count, nudge)
    if not window:
        return []
    if two_dimensional and columns:
        window = reflow_2d(window, doc.bytes_per_tile, doc.tile_height, columns)
    engine, preset = reg.engine_for(
        doc.pixel_config.interpret_preset_id, PixelCodecPlugin
    )
    params = tile_params(doc, engine, preset.params)
    return _run(
        Stage.INTERPRET_PIXEL,
        Pathway.PIXEL,
        lambda: engine.decode(window, params, PipelineContext()),
        plugin=preset.id,
    )


@dataclass(frozen=True)
class TileRegion:
    """The byte range a tile-run edit reads or rewrites, and the run it covers.

    ``first_tile``/``count`` are the tiles the region spans — normally exactly
    the requested run, but under the 2D walk they widen to whole bitmap-rows
    (see :func:`tile_region`). ``start``/``length`` are that region's bytes in
    ``Document.pixel_data``, clamped to the data's end.
    """

    first_tile: int
    count: int
    start: int
    length: int


def tiles_per_stripe(doc: Document, columns: int, two_dimensional: bool) -> int:
    """How many tiles' bytes interleave together — 1 unless the 2D walk applies.

    In wide-bitmap mode a tile's pixel-rows are strided ``columns`` tiles apart,
    so ``columns`` tiles share one interleaved byte stripe and no single tile
    owns a contiguous byte range.
    """
    if not two_dimensional or not has_2d_reading(doc.bytes_per_tile, doc.tile_height):
        return 1
    return max(1, columns)


def tile_region(
    doc: Document,
    first_tile: int,
    count: int,
    *,
    nudge: int = 0,
    columns: int = 1,
    two_dimensional: bool = False,
    anchor: int = 0,
) -> TileRegion:
    """The region an edit to ``count`` tiles at ``first_tile`` must work over.

    In 1D that is the run itself: tiles are contiguous, so each one's bytes can
    be replaced on its own. Under the 2D walk the run is widened to whole byte
    stripes, because the interleave is only defined per bitmap-row — and the
    stripe grid is anchored at ``anchor`` (the view's tile offset), the same
    origin the on-screen reflow uses, so what is written matches what is shown.

    A run that starts before the first whole stripe of that frame is trimmed to
    it rather than written against a truncated stripe: only reachable by
    selecting tiles, scrolling the view past them, and pasting, and dropping a
    tile beats scrambling one.
    """
    tb = doc.bytes_per_tile
    stripe = tiles_per_stripe(doc, columns, two_dimensional)
    if stripe > 1:
        phase = anchor % stripe
        start_tile = phase + ((first_tile - phase) // stripe) * stripe
        if start_tile < 0:  # the run sits in the frame's leading partial stripe
            start_tile += stripe
        end_tile = (
            phase
            + ceil_div(max(first_tile + count, start_tile) - phase, stripe) * stripe
        )
    else:
        start_tile, end_tile = first_tile, first_tile + count
    start = nudge + start_tile * tb
    length = max(0, (end_tile - start_tile) * tb)
    length = max(0, min(length, len(doc.pixel_data) - start))
    return TileRegion(start_tile, end_tile - start_tile, start, length)


def decode_tiles(
    doc: Document,
    reg: Registry,
    first_tile: int,
    count: int,
    *,
    nudge: int = 0,
    columns: int = 1,
    two_dimensional: bool = False,
    anchor: int = 0,
) -> list:
    """Decode exactly the tiles of a run — the copy side of tile editing.

    :func:`decode_window` decodes a *window* and assumes the window's own start
    is the 2D stripe origin, which is only true for the view. This decodes an
    arbitrary run in the view's frame: in 2D it decodes the enclosing stripes
    (:func:`tile_region`) and slices the run back out.
    """
    if count <= 0:
        return []
    if tiles_per_stripe(doc, columns, two_dimensional) == 1:
        return decode_window(doc, reg, first_tile, count, nudge)
    region = tile_region(
        doc,
        first_tile,
        count,
        nudge=nudge,
        columns=columns,
        two_dimensional=True,
        anchor=anchor,
    )
    tiles = decode_window(
        doc,
        reg,
        region.first_tile,
        region.count,
        nudge,
        columns=columns,
        two_dimensional=True,
    )
    skip = first_tile - region.first_tile
    return tiles[max(0, skip) : max(0, skip) + count]


def encode_tiles(
    doc: Document,
    reg: Registry,
    first_tile: int,
    tiles: list,
    *,
    nudge: int = 0,
    columns: int = 1,
    two_dimensional: bool = False,
    anchor: int = 0,
) -> tuple[int, bytes]:
    """Encode ``tiles`` at ``first_tile`` into replacement bytes for the document.

    Returns ``(start, data)`` — splice ``data`` in at byte ``start`` and the run
    holds those tiles. **Only the edited tiles' bytes differ**: the surrounding
    bytes of the region are carried through untouched rather than decoded and
    re-encoded, because a codec round-trips *pixels*, not bytes (bits outside a
    format's masks, and index values it can't produce, would not survive), so
    rewriting a neighbour to edit its stripe-mate would corrupt it.

    In 1D the region is the run and the encoded bytes are the whole answer.
    Under the 2D walk each tile's bytes are scattered back across its bitmap-row
    at the same stride :func:`~celpix.core.arrangement.reflow_2d` gathers them
    from, leaving every other tile in the stripe byte-identical.

    Bytes past the end of the data are dropped: editing never grows a file.
    """
    if not tiles:
        return (nudge + first_tile * doc.bytes_per_tile, b"")
    tb = doc.bytes_per_tile
    engine, preset = reg.engine_for(
        doc.pixel_config.interpret_preset_id, PixelCodecPlugin
    )
    params = tile_params(doc, engine, preset.params)
    blob = _run(
        Stage.INTERPRET_PIXEL,
        Pathway.PIXEL,
        lambda: engine.encode(tiles, params, PipelineContext()),
        plugin=preset.id,
    )
    stripe = tiles_per_stripe(doc, columns, two_dimensional)
    if stripe == 1:
        start = nudge + first_tile * tb
        return start, blob[: max(0, len(doc.pixel_data) - start)]
    region = tile_region(
        doc,
        first_tile,
        len(tiles),
        nudge=nudge,
        columns=columns,
        two_dimensional=True,
        anchor=anchor,
    )
    out = bytearray(doc.pixel_data[region.start : region.start + region.length])
    for i in range(len(tiles)):
        slot = first_tile + i - region.first_tile
        if slot < 0:  # trimmed by tile_region — see there
            continue
        scatter_2d(out, slot, blob[i * tb : (i + 1) * tb], tb, doc.tile_height, stripe)
    return region.start, bytes(out)


def decode_and_compose(
    pixel_bytes: bytes,
    engine,  # noqa: ANN001 — a pixel-interpret plugin
    params,  # noqa: ANN001 — the preset's engine params
    layout: BlockLayout,
    two_dimensional: bool,
    max_rows: int | None,
    biases: list[int] | None = None,
):
    """Decode a pixel-byte buffer and lay the tiles out through an arrangement.

    The Qt-free core shared by the live view, the decompression overlay, and
    export: 2D reflow → decode → block layout → compose. ``pixel_bytes`` begins
    at whatever origin the caller wants (a window of the doc's bytes for the live
    view, the whole file for export, a decompressed scratch for the overlay).
    ``max_rows`` caps the composed height (the live view's fixed window); ``None``
    sizes to the data (export and the overlay show every tile). ``biases`` shifts
    each slot's indices for a pinned subpalette (see :func:`compose_tiles`).
    Returns ``(grid, filled)`` — an index or direct-color grid, and the count of
    real tiles (excluding any 2D-reflow / partial-tile padding) so a caller can
    background the rest.
    """
    cols = layout.columns
    tile_bytes = engine.bytes_per_tile(params)
    _tw, tile_h = engine.tile_size(params)
    filled = ceil_div(len(pixel_bytes), tile_bytes) if tile_bytes else 0
    buffer = (
        reflow_2d(pixel_bytes, tile_bytes, tile_h, cols)
        if two_dimensional
        else pixel_bytes
    )
    # Zero-pad the trailing partial tile so a short buffer still decodes.
    if tile_bytes and len(buffer) % tile_bytes:
        buffer = buffer + bytes(-len(buffer) % tile_bytes)
    tiles = engine.decode(buffer, params, PipelineContext()) if buffer else []
    return compose_tiles(tiles, layout, max_rows, biases), filled


def _rows_needed(count: int, layout: BlockLayout) -> int:
    """Cell rows ``count`` tiles occupy under ``layout``.

    Plain placement is the ceiling; a block layout has to be asked, since its
    last block row can leave the bottom cell rows short. Only that final block
    row is walked, though — every earlier one is full, so it contributes exactly
    its own height, and a window can hold tens of thousands of tiles.
    """
    if count <= 0:
        return 1
    if layout.is_plain:
        return ceil_div(count, max(1, layout.columns))
    period = layout.slots_per_block_row
    start = ((count - 1) // period) * period
    return 1 + max(layout.slot_to_pos(s)[1] for s in range(start, count))


def compose_tiles(
    tiles: list,
    layout: BlockLayout,
    max_rows: int | None,
    biases: list[int] | None = None,
):
    """Lay an already-decoded tile list out through an arrangement.

    The compose half of :func:`decode_and_compose`, split out because a
    rearranged view (``core.tilerearrangement``) cannot get its tiles from one
    contiguous byte buffer: the tiles on screen are gathered from wherever the map sends
    them, and only the placement is shared. ``max_rows`` caps the composed height
    (the live view's fixed window); ``None`` sizes to the data.

    ``biases`` is one index shift per slot, the last step of rendering a pinned
    subpalette region (:mod:`celpix.core.paletteregions`): the composed image
    carries a single colour table, so a tile that must render through another
    palette row has the row folded into its indices instead. It applies here
    because this is where both render routes meet and where slot *k* is still
    identifiable as ``tiles[k]`` — after composition the tiles are one buffer and
    the block layout has scattered them. Direct-colour grids are left alone: they
    carry their own ARGB and index no palette. ``None`` (the default) composes
    exactly as before, which is every path that has nothing pinned.
    """
    if biases:
        # Indexed by slot rather than zipped: a short bias list must leave the
        # remaining tiles composed as they are, not truncate the window to it.
        count = len(biases)
        tiles = [
            tile.shifted(biases[i])
            if i < count and biases[i] and tile.bytes_per_pixel == 1
            else tile
            for i, tile in enumerate(tiles)
        ]
    cols = layout.columns
    need_rows = _rows_needed(len(tiles), layout)
    canvas_rows = need_rows if max_rows is None else max(1, min(max_rows, need_rows))
    if layout.is_plain:
        # Narrow a single partial row to its tiles; a taller image keeps full
        # width and lets the caller background the trailing slots.
        shown_cols = cols if need_rows > 1 else min(cols, max(1, len(tiles)))
        return compose_window(tiles, shown_cols, 0, canvas_rows)
    return compose_window(tiles, cols, 0, canvas_rows, layout)


def tile_bank(doc: Document, reg: Registry) -> list:
    """Every tile of the source ``doc`` draws from, decoded once and kept.

    Decoded whole and indexed, not windowed: a map's cells reach anywhere in the
    bank, so there is no contiguous window to slice. A tile bank is small (1024
    tiles is the usual hardware ceiling) where the maps drawn from it are not.

    **Held on the document between renders** (:attr:`Document.tile_bank_cache`),
    because a tilemap is drawn entire on every refresh — a spin change, a
    selection, an undo — and re-decoding the whole source each time is work
    proportional to the bank rather than to what changed. The cache keys on the
    bytes themselves *by identity* plus the geometry the codec was asked for, so
    a pixel edit (which splices a new ``bytes``) and a format switch (which
    re-cuts the geometry) both miss and re-decode. Holding the key's buffer here
    is what makes the identity test sound: the object cannot be freed, so its
    ``id`` cannot be reused underneath us.
    """
    key = (
        doc.pixel_data,
        doc.bytes_per_tile,
        doc.tile_width,
        doc.tile_height,
        doc.pixel_config.interpret_preset_id,
    )
    cached = doc.tile_bank_cache
    if cached is not None and cached[0][0] is key[0] and cached[0][1:] == key[1:]:
        return cached[1]
    tiles = decode_tiles(doc, reg, 0, doc.tile_count) or []
    doc.tile_bank_cache = (key, tiles)
    return tiles


def patch_tile_bank(
    doc: Document, reg: Registry, splices: list[tuple[int, bytes]]
) -> None:
    """Carry a byte edit into the cached bank instead of dropping it.

    The channel that makes editing a tile **live across every cell that draws
    it**. :func:`tile_bank` keys on the buffer's identity, so an edit — which
    splices a fresh ``bytes`` — would otherwise miss and re-decode the whole
    source on the next repaint, once per committed gesture. Here only the tiles
    the splice actually reached are re-decoded (usually one), dropped into the
    cached list in place, and the cache is re-keyed to the buffer as it now
    stands. Because the map's tiles are all derived from that one list on the
    following refresh, one edited tile updates everywhere it appears at once.

    Call it **after** the splices have landed in ``doc.pixel_data``: the new key
    is read off the document rather than passed in, which is what keeps the two
    from drifting apart.

    Anything that cannot be answered locally — no cache to patch, a geometry
    that changed under it, a decode that fails — drops the cache rather than
    guessing, so the next render rebuilds it and the fallback is a slow repaint
    instead of a stale picture.
    """
    cached = doc.tile_bank_cache
    if cached is None:
        return
    key, bank = cached
    tile_bytes = doc.bytes_per_tile
    shape = (
        tile_bytes,
        doc.tile_width,
        doc.tile_height,
        doc.pixel_config.interpret_preset_id,
    )
    if not tile_bytes or key[1:] != shape:
        doc.tile_bank_cache = None
        return
    try:
        for start, data in splices:
            if start < 0 or not data:
                continue
            first = start // tile_bytes
            fresh = decode_tiles(
                doc, reg, first, ceil_div(start + len(data), tile_bytes) - first
            )
            for at, tile in enumerate(fresh):
                if first + at < len(bank):
                    bank[first + at] = tile
    except PipelineError:
        doc.tile_bank_cache = None
        return
    doc.tile_bank_cache = ((doc.pixel_data, *shape), bank)


def drawn_palette_row(row: int, base: int, rows: int = 0) -> int:
    """The palette row a stored ``row`` draws through, under a row base.

    The one arithmetic every named row goes through — a cell's, a subsprite's, a
    pinned region's — so the four places that ask cannot answer differently
    (:attr:`~celpix.core.document.Document.palette_row_base`).

    ``rows`` is the **wrap** modulus, and 0 turns wrapping off — which is what
    the document answers unless the user asked for it
    (:meth:`~celpix.core.document.Document.palette_row_wrap`). Off, a row the
    base pushes below the palette's first stops there, since no colour table has
    a negative index; on, it comes round the other end, because the base states a
    *distance* and a distance that overshoots still means something — row 0 of
    eight, taken 8 rows up a palette eight rows tall, is row 0 again. Which of
    those reads better is the file's business rather than a rule
    (``docs/design/palette-editing.md`` §3), so it is a toggle.
    """
    total = row + base
    return total % rows if rows > 0 else max(0, total)


def tilemap_tiles(
    doc: Document, reg: Registry, columns: int
) -> tuple[list, BlockLayout]:
    """A tilemap's cells expanded into ready-to-place tiles, and a layout.

    The whole of what makes a tilemap render through the *pixel* view rather
    than a parallel one — :func:`expand_cells` over the cells the view **lays
    out**, which is the file's own order unless the map is several pages
    assembled side by side
    (:attr:`~celpix.core.document.Document.laid_out_cells`). That is the one place
    an assembly reaches the picture; nothing downstream of here knows about it.

    A document that **fixes** its own width uses that, not the caller's: the width
    and the placement are one answer, and a picture laid out at any other width
    interleaves an assembly's pages instead of putting them side by side, or
    breaks a dense stamp's rows in the middle of a stamp
    (:attr:`~celpix.core.document.Document.drawn_columns`). So the two cannot
    be passed in separately and disagree — which is what a render reached without
    going through the view would otherwise do.
    """
    return expand_cells(doc, reg, doc.laid_out_cells, doc.drawn_columns or columns)


def expand_cells(
    doc: Document,
    reg: Registry,
    cells: list[Cell],
    columns: int,
    block: tuple[int, int] | None = None,
) -> tuple[list, BlockLayout]:
    """``cells`` turned into ready-to-place tiles, and the layout to place them.

    Each cell becomes the source tiles it draws, oriented as its flip bits say
    and carrying its palette row, and the two results line up so the existing
    composer can finish the job:

    - **tiles** — every cell's tiles, consecutively, so a metatile's four land
      next to each other.
    - **layout** — a :class:`BlockLayout` whose *block* is one cell, which is
      what places a 2x2 metatile's four consecutive tiles as a square.

    Taking the cells as an argument rather than reading them off the document is
    what lets the **tile source sheet** be the same picture as the map: it is
    this function over one synthetic cell per tile ID
    (:func:`tile_source_image`), so a tile in the panel and the same tile in a
    map are composed by one code path and cannot come out different.

    The palette row travels **in the indices**, the same mechanism pinned
    palette regions use (:meth:`~celpix.core.index_grid.IndexGrid.shifted`): the
    composed image carries one colour table, so a cell that renders through
    another row has that row folded in. It is folded in here rather than handed
    to :func:`compose_tiles` as a bias list so that the shift shares the memo
    below — the same tile drawn twice through the same row is shifted once.

    **Two memos, at two scales, and the second is what a real map hits.** What a
    cell draws is decided entirely by ``(index, row, flip_h, flip_v)`` — priority
    and ``flags`` reach no pixel — and maps repeat themselves heavily: a screen's
    backdrop is one cell over half of it. So a *cell* that has been seen before
    contributes its tile run again without the walk being redone, which is what
    takes the per-cell cost down to a dict lookup on the ordinary map. Underneath
    it the *tile* memo keys on ``(tile, flip_h, flip_v, shift)``, and catches what
    the first cannot: distinct cells drawing the same tile through the same row,
    which is most of a bank's use. The flip-and-shift work is then per distinct
    combination rather than per cell, and every repeat is the same grid object
    appearing in the list again.

    ``block`` is how many tiles one *placed unit* covers, and defaults to one
    cell's worth. The stamp sheet is what overrides it: there a unit is a whole
    stamp of several cells (:attr:`~celpix.core.document.Document.stamp_tiles`),
    and the cells arrive already expanded stamp by stamp for it to square up.

    A cell naming a tile the source does not have renders blank rather than
    failing: a tilemap is routinely authored against a bank that is loaded
    elsewhere, and half a picture is more useful than an error.
    """
    unit = block or doc.cell_tiles
    across, down = max(1, unit[0]), max(1, unit[1])
    blank = IndexGrid(doc.tile_width, doc.tile_height)
    space = 1 << pixel_bpp(doc.pixel_config.interpret_preset_id, reg)
    source = tile_bank(doc, reg)
    count = len(source)
    base = doc.palette_row_base
    rows = doc.palette_row_wrap(space)
    runs: dict[tuple[int, int, bool, bool], list] = {}
    drawn: dict[tuple[int, bool, bool, int], object] = {}
    tiles: list = []
    for cell in cells:
        flip_h, flip_v = cell.flip_h, cell.flip_v
        run = runs.get((cell.index, cell.palette_row, flip_h, flip_v))
        if run is None:
            shift = drawn_palette_row(cell.palette_row, base, rows) * space
            run = []
            for index in doc.cell_tile_indices(cell):
                key = (index, flip_h, flip_v, shift)
                tile = drawn.get(key)
                if tile is None:
                    tile = source[index] if 0 <= index < count else blank
                    if flip_h:
                        tile = transform.flip_horizontal(tile)
                    if flip_v:
                        tile = transform.flip_vertical(tile)
                    if shift and tile.bytes_per_pixel == 1:
                        # Direct-colour grids carry their own ARGB and index no
                        # palette, so there is no row to fold into them.
                        tile = tile.shifted(shift)
                    drawn[key] = tile
                run.append(tile)
            runs[(cell.index, cell.palette_row, flip_h, flip_v)] = run
        tiles.extend(run)
    layout = BlockLayout(max(1, columns) * across, across, down, "row")
    return tiles, layout


class TilemapImage(NamedTuple):
    """A whole tilemap drawn, and the two numbers a caller needs after.

    ``drawn`` is how many leading canvas **slots** hold data, which is what the
    canvas backgrounds the rest of the picture from. Tiles for a grid; for a
    sprite object it is the frames' slots and *not* the subsprites blitted into
    them (:attr:`SpriteSheet.slots`) — their count is not a position on the canvas,
    and reading it as one paints a stray band across the sheet.

    ``palette_rows`` is how many rows were folded into the indices, which is what
    an export has to size its colour table to: one image carries one table, so a
    map drawing through four palette rows needs four rows of it
    (:func:`~celpix.ui.export.document_image`).

    ``hidden`` is the ``(x, y, w, h)`` pixel rectangles of the positions the map
    does not draw (:attr:`~celpix.core.tilemap.Cell.visible`), for the caller to
    paint the background over. Rectangles rather than positions because the
    geometry — cell size, the assembly's width — is settled here and a second
    caller working it out again is a second chance to get it wrong; and because
    a run of hidden cells merges into one of them, which is what most of a
    sparsely-drawn layout is.
    """

    grid: IndexGrid
    drawn: int
    palette_rows: int
    hidden: tuple[tuple[int, int, int, int], ...] = ()


def hidden_rects(doc: Document, columns: int) -> tuple[tuple[int, int, int, int], ...]:
    """The pixel rectangles of ``doc``'s undrawn positions, runs merged.

    In **laid-out** order, since these land on the composed picture and an
    assembled screen draws its pages side by side
    (:attr:`~celpix.core.document.Document.laid_out_cells`).

    ``columns`` is resolved the way :func:`tilemap_tiles` resolves it — an
    assembly owns its width — so a caller that has only the view's number gets
    rectangles against the width the tiles were actually laid out at. Doing it
    here rather than at each call site is what stops the live preview and the
    committed render from disagreeing by a row.

    Empty for every format with no visibility bit, which is all but one, and the
    walk short-circuits on the ``all`` scan rather than building a list per cell.
    """
    cells = doc.laid_out_cells
    if all(cell.visible for cell in cells):
        return ()
    across, down = doc.cell_tiles
    cell_w, cell_h = across * doc.tile_width, down * doc.tile_height
    cols = max(1, doc.drawn_columns or columns)
    rects: list[tuple[int, int, int, int]] = []
    # A row at a time, so a run can only merge within one: spanning the wrap would
    # paint a rectangle across the whole picture and blank the left of the next row.
    for at in range(0, len(cells), cols):
        row = cells[at : at + cols]
        y = at // cols * cell_h
        start: int | None = None
        for col in range(len(row) + 1):  # one past the end flushes the last run
            if col < len(row) and not row[col].visible:
                if start is None:
                    start = col
                continue
            if start is not None:
                rects.append((start * cell_w, y, (col - start) * cell_w, cell_h))
                start = None
    return tuple(rects)


def tilemap_image(doc: Document, reg: Registry, columns: int) -> TilemapImage:
    """A tilemap document rendered whole, by whichever of its two shapes it is.

    The single place both the canvas and PNG export go through, so an exported
    map is the picture on screen rather than a second rendering that could drift
    from it. Always the whole document — a tilemap has no view window
    (``docs/design/tilemap-entry.md`` §8) — and ``columns`` means cells across
    for a grid and *frames* across for a sprite object.
    """
    space = 1 << pixel_bpp(doc.pixel_config.interpret_preset_id, reg)
    base = doc.palette_row_base
    rows = doc.palette_row_wrap(space)
    hidden: tuple[tuple[int, int, int, int], ...] = ()
    if doc.is_sprite:
        frames = doc.shown_frames
        top = max(
            (
                drawn_palette_row(s.palette_row, base, rows)
                for frame in frames
                for s in frame
            ),
            default=0,
        )
        grid, sheet = sprite_image(doc, reg, columns)
        drawn = sheet.slots
        # An object has no undrawn *position* to paint: its records sit at signed
        # offsets rather than in a grid, and one that is not drawn is dropped
        # outright by the codec rather than leaving a hole behind
        # (``plugins.builtins.object_codec``).
    else:
        top = max(
            (
                drawn_palette_row(cell.palette_row, base, rows)
                for cell in doc.drawn_cells
            ),
            default=0,
        )
        tiles, layout = tilemap_tiles(doc, reg, columns)
        # No bias list: the rows are already in the indices (see tilemap_tiles).
        grid, drawn = compose_tiles(tiles, layout, None), len(tiles)
        hidden = hidden_rects(doc, columns)
    return TilemapImage(grid, drawn, min(max(1, 256 // max(1, space)), top + 1), hidden)


def tile_source_span(doc: Document, limit: int | None = None) -> range:
    """Every tile ID a tilemap's cells can reach — the run that resolves.

    An **ID** is what a cell holds and what a hex editor shows at its bytes: the
    file's own number, before the binding's base tile
    (:attr:`~celpix.core.document.Document.tile_base_index`). The run that comes
    back is the contiguous span of those numbers that resolves to something to
    look at (``docs/design/tilemap-entry.md`` §8).

    This is the **legality** question — is there anything at this ID — which is
    not the same as what the tile source panel offers: where a cell covers
    several tiles every ID in here resolves, but only one in four names a whole
    unit, and it is those the sheet lays out (:func:`tile_source_ids`). Anything
    validating an ID a user already has wants this one.

    Only the resolving run, rather than the format's whole index space: a 10-bit
    console cell can name 1024 tiles and is routinely bound to a 256-tile bank,
    so three quarters of that space would be blank grid to scroll past. Where the
    two disagree the *bank* is the honest answer — a cell pointed past it draws
    nothing.

    Three readings, and they are three different questions:

    - A **chained** map's cells are coordinates into another map's cells
      (§3.1), so the IDs are positions in that list and the bank behind it is
      one hop further away.
    - An ordinary map's IDs are the ones landing inside the bank once the base
      is added: ``base + id`` in ``[0, tile_count)``. The base is signed, so a
      map numbering from ``$100`` against a slice of exactly those tiles has a
      base of ``-0x100`` and a run **starting at** ``$100`` — the numbers the
      file actually holds.
    - A **sprite object**'s reading is the same one. It has no cell grid, but a
      *subsprite* names a tile in exactly the same numbers a cell does, so the
      run of tiles it can reach is the bank the same way — what differs is that
      nothing here is placed into a grid, not what the numbers mean.

    ``limit`` is the codec's index-field width
    (:meth:`~celpix.plugins.base.TilemapCodecPlugin.index_limit`), passed in
    because looking it up needs the registry's preset params. ``None`` means the
    format did not answer, and an unanswered field is left alone rather than
    clamped to a guess — the same protocol the flips follow.
    """
    if not doc.is_tilemap:
        return range(0)
    if doc.chain is not None:
        start, stop = 0, len(doc.chain.source)
    else:
        base = doc.tile_base_index
        # In whatever unit the indices are: tiles for every ordinary map, and
        # **glyphs** where a font's are several tiles each
        # (:attr:`~celpix.core.document.Document.glyph_count`). Reading the
        # tile count there would offer twice the codes the sheet has letters.
        start, stop = max(0, -base), doc.glyph_count - base
    if limit is not None:
        stop = min(stop, limit + 1)
    return range(start, max(start, stop))


def tile_source_ids(doc: Document, limit: int | None = None) -> Sequence[int]:
    """Which tile IDs the tile source panel offers — one entry per whole unit.

    :func:`tile_source_span` for a cell that draws a single tile, which is most
    documents: every ID resolves and every ID is its own picture, so the run is
    the menu.

    Where a cell draws **several** tiles the two part company. A 16x16 cell names
    the tile at its top-left and takes the other three from around it — ``N+1``,
    ``N+0x10``, ``N+0x11`` in a 16-tile-wide VRAM array — so ID ``N+1`` names a
    unit overlapping ``N``'s by three quarters, and offering the whole span shows
    the bank four times over as a smear of one-tile-shifted windows. What is
    actually on offer is the bank *read in that unit*: the IDs sitting on the
    unit's own grid, one entry per distinct 16x16 metatile
    (``docs/graphics-formats-reference/scgcad-formats.md`` §2.3, where that
    alignment is the test that shows the screen's cell-size byte means what it
    says). A **stamped chain** is the same shape one level up — a coordinate
    names a rectangle of source cells, stepped by the source's width — so it is
    the same walk over ``stamp_cells`` and the source's own columns.

    A **sprite object**'s unit is the tile whichever size its subsprites are.
    Both sizes occur within one object and a sheet cannot be laid out in two, so
    it is laid out in the thing both are made of — a large subsprite's ID is on
    it, as the tile it names and the corner its other three are found from.

    Alignment is the sheet's, not the format's: an unaligned index is legal and
    draws the unit starting there, which is what the tool that wrote it did. So
    this narrows what is *offered*, never what can be held — the Cell spin still
    reaches every ID in the span, a cell already holding an unaligned one keeps
    it, and the sheet simply has no square to ring it in.

    The sequence is in reading order and **not contiguous**, so a position in it
    is a slot and only the values are IDs: index it, do not do arithmetic on it.
    """
    span = tile_source_span(doc, limit)
    chain = doc.chain
    if doc.glyph_layout is not None:
        # A **glyph** is already the unit its index is counted in, so every one
        # of them names a whole unit and there is nothing to narrow: the
        # alignment question below is what a *tile* number has to answer when the
        # thing it names is bigger than it, and a block number never does.
        return span
    if chain is not None:
        unit, stride, offset = doc.stamp_cells, chain.source_columns, 0
    else:
        unit = doc.cell_tiles
        # The walk runs in the bank's numbering, where the neighbours are: the
        # base is what turns a file's ID into it, so alignment is measured on
        # ``base + id`` and never on the file's own number.
        stride, offset = doc.cell_row_stride or unit[0], doc.tile_base_index
    across, down = max(1, unit[0]), max(1, unit[1])
    if across == 1 and down == 1:
        return span
    stride = max(across, stride)
    return [
        at
        for at in span
        if (at + offset) % stride % across == 0 and (at + offset) // stride % down == 0
    ]


class TileSheet(NamedTuple):
    """A tilemap's tile source drawn as a sheet, and which IDs are in it.

    The two travel together because they have to agree: the panel resolves a
    click to a slot and the slot means nothing without the list saying which ID
    that slot holds. Computing them apart is how a sheet ends up labelled with
    the previous document's numbers.
    """

    grid: IndexGrid
    ids: Sequence[int]


def tile_source_image(
    doc: Document,
    reg: Registry,
    columns: int,
    limit: int | None = None,
    palette_row: int = 0,
) -> TileSheet:
    """The tiles ``doc`` can draw from, laid out as a sheet — the panel's picture.

    The tile-source twin of :func:`tilemap_image`, and deliberately the same
    machinery: one synthetic cell per ID through :func:`expand_cells`, so a tile
    in the panel is composed by the code path that composes it in the map and
    the two cannot come out looking different. That is the whole point of the
    panel — what is on offer has to be what will land.

    A **chained** map's ID is a position in the map it stamps from, so the cell
    is that source cell **resolved** (:func:`~celpix.core.tilemap.resolve_cell`)
    rather than a bare index: a stamp is its tile *plus* its attributes, and
    previewing it without them would show a picture the stamp does not make.
    Where the source states a **stamp size**, an ID names a whole stamp of its
    cells and the preview is that stamp — a quarter of one is not what will land,
    and the tile source panel's promise is that what is on offer is.

    ``columns`` is in units, as everywhere else here, and a unit is one cell — a
    2x2 metatile where the cell format says so — or a whole stamp of cells. The
    layout's block is sized to whichever it is.

    ``palette_row`` is the row those synthetic cells claim, which is the whole of
    what decides the sheet's colours: a tile is indices until a row is chosen for
    it, and a bank authored for row 3 read at row 0 is the right art in the wrong
    palette. **Not applied to a chained map**, whose cells resolve to real source
    cells carrying rows of their own — a stamp is its tile *plus* its attributes,
    and recolouring the preview would show a picture the stamp does not make.
    """
    ids = tile_source_ids(doc, limit)
    chain = doc.chain
    if chain is None:
        cells = [Cell(index=at, palette_row=palette_row) for at in ids]
    else:
        # The stamp a coordinate names, walked the source's own rows the way the
        # map resolves it (:func:`~celpix.core.tilemap.expand_stamps`) — one cell
        # per ID wherever nothing is stamped, the loops collapsing to a single
        # pass.
        across, down = doc.stamp_cells
        stride = max(1, chain.source_columns)
        cells = [
            resolve_cell(
                Cell(index=at),
                chain.source,
                carry_rows=chain.carry_rows,
                at=at + dx + dy * stride,
            )
            for at in ids
            for dy in range(down)
            for dx in range(across)
        ]
    tiles, layout = expand_cells(doc, reg, cells, columns, doc.stamp_tiles)
    return TileSheet(compose_tiles(tiles, layout, None), ids)


def glyph_sheet(
    doc: Document,
    reg: Registry,
    columns: int,
    layout: BlockLayout,
    palette_row: int = 0,
) -> TileSheet:
    """A **font sheet** laid out one glyph per slot, where a glyph is a block.

    The alphabet editor's picture of a font entry that has no map drawing
    through it yet — the sheet on its own, which is where a table is typed up
    before any string has been carved out to test it against
    (``docs/design/fontmap-entry.md`` §4). :func:`tile_source_image` answers the
    other direction, through a fontmap's binding, and the two have to agree
    about what slot *n* holds: it is the picture code ``base + n`` draws.

    ``layout`` is the **font's own** arrangement and decides both halves — which
    tiles make up glyph *n* (:meth:`~celpix.core.arrangement.BlockLayout.
    block_slots`) and how many glyphs there are. ``columns`` is the editor's own
    width and is unrelated to it: the sheet is re-flowed to whatever reads best
    in the window, and only the grouping travels.

    ``palette_row`` is folded into the indices, as
    :func:`expand_cells` does it for the map's synthetic cells, so the caller
    renders the result without offsetting the colour table a second time.
    """
    tiles = tile_bank(doc, reg)
    ids = list(range(layout.blocks(len(tiles))))
    blank = IndexGrid(doc.tile_width, doc.tile_height)
    shift = palette_row * (1 << pixel_bpp(doc.pixel_config.interpret_preset_id, reg))
    across = max(1, layout.block_columns)
    down = max(1, layout.block_rows)
    drawn: dict[int, object] = {}
    out: list = []
    for glyph in ids:
        run = layout.block_slots(glyph)
        for at in range(across * down):
            slot = run[at] if at < len(run) else -1
            tile = drawn.get(slot)
            if tile is None:
                tile = tiles[slot] if 0 <= slot < len(tiles) else blank
                # Direct-colour grids carry their own ARGB and index no palette,
                # so there is no row to fold into them.
                if shift and tile.bytes_per_pixel == 1:
                    tile = tile.shifted(shift)
                drawn[slot] = tile
            out.append(tile)
    placed = BlockLayout(max(1, columns) * across, across, down, "row")
    return TileSheet(compose_tiles(out, placed, None), ids)


# A sprite sheet is allocated whole, one byte per pixel, and every number that
# sizes it comes out of the file: a subsprite's offsets are signed and up to 16
# bits wide, and how many frames there are is however many the bytes divide into.
# Point a subsprite cell format at bytes that are not it — which the format picker
# lets a user do, and which detection cannot always prevent — and those multiply
# into an image no machine holds: 4 KB of unrelated data decodes to offsets around
# ±32000 and asks for 33 GB. So the extent is bounded and a sheet past it is
# reported as the misinterpretation it is, rather than left to surface as a
# MemoryError with no stage, no pathway and nothing on it a user could act on.
SPRITE_SHEET_PIXELS = 64 << 20  # a genuine object's sheet is ~100x under this


def _check_sprite_extent(pixels: int, what: str) -> None:
    if pixels <= SPRITE_SHEET_PIXELS:
        return
    raise PipelineError(
        Stage.INTERPRET_TILEMAP,
        Pathway.TILEMAP,
        f"{what}, past the {SPRITE_SHEET_PIXELS >> 20} megapixel sheet limit - "
        "these bytes are unlikely to be this cell format",
    )


class SpriteSheet(NamedTuple):
    """How a sprite object's frames are laid out — the geometry the render and
    the canvas have to agree about.

    A sprite object has no cells on screen: its subsprites sit at signed pixel
    offsets
    and every frame is drawn in one shared bounding box, so what the canvas places
    and selects in is the plain **tile** — the frame being the block those tiles
    group into. That grid is this, and it is computed once here rather than
    re-derived by each side, because a selection read off a different grid than the
    picture was placed on names a different part of the sheet.

    ``frame`` and the tile counts are in tiles; ``box`` is the pixel bounding box
    (:func:`~celpix.core.sprite.frame_bounds`), which only the blit needs.
    """

    frames: int
    across: int  # frames laid side by side
    down: int  # rows of frames
    frame: tuple[int, int]  # one frame's size, in tiles
    box: tuple[int, int, int, int]  # left, top, width, height, in pixels

    @property
    def columns(self) -> int:
        """The whole sheet's width in tiles — the canvas's column count."""
        return self.across * self.frame[0]

    @property
    def rows(self) -> int:
        """The whole sheet's height in tiles."""
        return self.down * self.frame[1]

    @property
    def slots(self) -> int:
        """Canvas slots that hold a frame.

        A leading run, because the frame is the canvas's block: frame *n*'s tiles
        are slots ``n * w * h`` onward, so the slots past the last frame — a partial
        bottom row of frames, or the space beside a sheet drawn wider than it has
        frames — are the tail this bounds.
        """
        return self.frames * self.frame[0] * self.frame[1]


def sprite_sheet(doc: Document, columns: int) -> SpriteSheet:
    """``doc``'s frames laid ``columns`` across — see :class:`SpriteSheet`.

    ``columns`` is in *frames*, which is what the view's Cols means on a sprite
    object; everything the sheet reports back is in tiles.
    """
    frames = doc.shown_frames
    box = frame_bounds(frames, doc.tile_width, doc.tile_height)
    across = max(1, columns)
    return SpriteSheet(
        frames=len(frames),
        across=across,
        down=ceil_div(len(frames), across),
        # Floor division deliberately: it is how the canvas recovers a count from
        # the image it was handed, and the two have to arrive at the same grid.
        frame=(
            max(1, box[2] // max(1, doc.tile_width)),
            max(1, box[3] // max(1, doc.tile_height)),
        ),
        box=box,
    )


def sprite_image(
    doc: Document, reg: Registry, columns: int
) -> tuple[IndexGrid, SpriteSheet]:
    """A sprite object's frames drawn side by side — one image, and its layout.

    The one render path that cannot go through :func:`compose_tiles`, because a
    subsprite sits at a signed *pixel* offset and a composer places tiles in a
    grid. So the tiles are fetched the same way a tilemap's are, and then blitted
    rather than composed (:mod:`celpix.core.sprite`).

    Three rules the frames need and a tilemap does not:

    - **Index 0 is transparent.** Subsprites overlap, and one drawn as a solid
      square would erase whatever it was meant to sit in front of.
    - **They are drawn back to front.** The file lists them front-first —
      subsprite 0 is the topmost — so the run is walked in reverse.
    - **One box for the whole object.** Every frame is drawn in the same
      bounding box (:func:`~celpix.core.sprite.frame_bounds`), so a strip shows
      the object's motion instead of re-centring it away frame by frame.

    Unlike :func:`expand_cells`, nothing here is memoized on the tile, and that
    is measured rather than assumed. A frame strip does draw each of an object's
    tiles once per frame it appears in, so the same
    ``(tile, flip_h, flip_v, row)`` memo looks like it should apply — but it buys
    nothing here, because the flip is not what this path spends its time on. The
    blit is (``docs/design/tilemap-entry.md`` §8.2), and an object is a few
    hundred tiles against a map's several thousand cells. Adding the memo would
    put a fourth place in step with :class:`~celpix.core.tilemap.Cell`'s drawing
    fields for no gain.
    """
    frames = doc.shown_frames
    sheet = sprite_sheet(doc, columns)
    left, top, width, height = sheet.box
    across = sheet.across
    # The exact allocation, checked before it is made: the read is bounded on an
    # estimate (:func:`load_tilemap_data`), and this is where the tile size the
    # map is *bound* to, and the columns the view is laid out at, finally join it.
    _check_sprite_extent(
        across * width * sheet.down * height,
        f"a {across * width}x{sheet.down * height} pixel sprite sheet",
    )
    image = IndexGrid(across * width, sheet.down * height)
    source = tile_bank(doc, reg)
    space = 1 << pixel_bpp(doc.pixel_config.interpret_preset_id, reg)
    rows = doc.palette_row_wrap(space)
    for at, frame in enumerate(frames):
        ox = (at % across) * width - left
        oy = (at // across) * height - top
        for sub in reversed(frame):
            _draw_subsprite(
                image, source, sub, ox + sub.x, oy + sub.y, doc, space, rows
            )
    return image, sheet


def _draw_subsprite(
    image: IndexGrid,
    source: list[IndexGrid],
    sub: Subsprite,
    x: int,
    y: int,
    doc: Document,
    space: int,
    rows: int,
) -> None:
    """Blit one subsprite's tiles with its top-left corner at ``(x, y)``.

    The step both sprite sheets are built out of — the frame strip
    (:func:`sprite_image`) and the sheet of records (:func:`subsprite_sheet`) —
    and shared rather than written twice because a piece has to look the same in
    both: the same tile walk, the same pair of flips, the same palette row folded
    in the same way. Two copies of this is how a record ends up drawn in one row
    over there and another over here.

    A subsprite's own tiles step by the *tile* size, the codec's and not an
    assumed 8: a subsprite states its size in tiles for exactly this reason, and
    a literal here would put the second half of every multi-tile one 8px from the
    first whatever the tiles behind it measure.

    The row bias is settled once per piece rather than per tile — every tile of a
    subsprite draws through the record's one row.
    """
    wide, _tall = sub.size()
    step_x, step_y = max(1, doc.tile_width), max(1, doc.tile_height)
    bias = drawn_palette_row(sub.palette_row, doc.palette_row_base, rows) * space
    for slot, index in enumerate(sub.tile_indices()):
        index += doc.tile_base_index
        if not 0 <= index < len(source):
            continue
        tile = source[index]
        if sub.flip_h:
            tile = transform.flip_horizontal(tile)
        if sub.flip_v:
            tile = transform.flip_vertical(tile)
        _blit(
            image,
            tile,
            x + (slot % wide) * step_x,
            y + (slot // wide) * step_y,
            bias,
        )


def subsprite_key(sub: Subsprite) -> tuple:
    """What makes two records the same **piece**: everything that draws it.

    Deliberately every input :func:`_draw_subsprite` reads and nothing else, so
    "same key" and "same pixels" are the same statement rather than two rules
    that could drift — the tiles it draws in the order it draws them, the shape
    they are laid out in, the two mirrors applied to each, and the row they are
    coloured through. What is left out is the whole of what a key is *for*:
    ``x`` and ``y`` say where the piece sits in its frame, and the sheet of
    unique pieces is exactly the reading that has thrown placement away.

    :attr:`~celpix.core.sprite.Subsprite.priority` and
    :attr:`~celpix.core.sprite.Subsprite.group` are left out for a different
    reason — they are carried so a save can put them back, and neither reaches
    the renderer, so two records differing only in one of them would be two
    squares a user could not tell apart.
    """
    return (
        tuple(sub.tile_indices()),
        sub.size(),
        sub.palette_row,
        sub.flip_h,
        sub.flip_v,
    )


class SubspriteSheet(NamedTuple):
    """A sprite map's records drawn one to a square, and which record each is.

    The four travel together for :class:`TileSheet`'s reason: a click, a ring or
    a caption resolves to a *slot*, and a slot means nothing without the list
    saying which record sits there. ``records`` holds ``(frame, subsprite)``
    pairs — both indices into what is **drawn**
    (:attr:`~celpix.core.document.Document.shown_frames`), which is the numbering
    the canvas's own pick uses, so a pick and a square can be compared directly.

    Unique-piece sheets hold **one record per square all the same**: the first
    occurrence, standing for the rest. A square is still a thing in the file, so
    a caller with a pick in its hand asks :func:`subsprite_key` which square that
    pick's art is in rather than looking the pick itself up.

    ``cell`` is one square's size in pixels: the largest subsprite in the object,
    in whole tiles. See :func:`subsprite_sheet` for why they are all that size.

    ``boxes`` is where each record's own art landed — ``(x, y, w, h)`` in sheet
    pixels, one per slot. It is the square only for the pieces that *are* the
    largest; on an object that mixes sizes a smaller piece sits centred in a box
    of its own, and a ring drawn on the square would claim the gutter around it
    is part of the record. Given rather than recomputed by the viewer, so the
    outline and the blit cannot disagree about where a piece is.
    """

    grid: IndexGrid
    records: list[tuple[int, int]]
    cell: tuple[int, int]
    boxes: list[tuple[int, int, int, int]]


def subsprite_sheet(
    doc: Document, reg: Registry, columns: int, *, by_frame: bool = True
) -> SubspriteSheet:
    """Every subsprite of a sprite map, one to a square, in frame order.

    The frame strip (:func:`sprite_image`) draws the object; this draws its
    *parts*. A frame is a heap of overlapping pieces at signed offsets and the
    front ones hide the back ones, so what a file is **made of** is not
    recoverable from the strip by looking — which is the same gap the tile source
    panel fills for a tilemap's cells, one level along
    (``docs/design/tilemap-entry.md`` §8).

    **Two readings, and ``by_frame`` picks which.** They answer two different
    questions about the same file, which is why neither is a mode of the other:

    - ``by_frame`` (the default) is **one square per record**, repeats included.
      The frames of an object reuse the same piece over and over and each
      occurrence is its own record in its own frame, so this is the file's own
      listing — what it holds, in the order it holds it, with the frame a square
      belongs to still recoverable from it.
    - Off, it is **one square per distinct piece** (:func:`subsprite_key`), in
      the order they first appear. An object is typically a handful of pieces
      posed over dozens of frames, so the listing above says the same few things
      over and over; this is the *inventory* — how much art there actually is,
      which is the question the frame-by-frame reading buries.

    Either way a square holds a real record. Under the second reading it is the
    **first** occurrence, standing for the others rather than replacing them:
    what is dropped is the repetition, not the fact that a square is a thing in
    the file.

    **Every square is the same size — the largest subsprite in the object.** A
    lattice needs one cell size and an object mixes them (a size *bit* picks one
    of two, and a format that states rectangles mixes more), so the choice is
    which size to lay out in. The largest is the only one that never clips: a
    grid of the smaller would cut every large piece to a quarter of itself.
    Smaller pieces are **centred** in their square, so each reads as the one
    object it is rather than as the corner of a bigger one — centred by pixel and
    not rounded to a whole tile, since the gap around a 1x1 piece in a 2x2 square
    is one tile and rounding half of it away would put the piece straight back in
    the corner, which is the reading being avoided.

    An odd gap therefore lands the piece one pixel off centre rather than on a
    half pixel: the inset is floored, and a viewer magnifying the sheet by a
    whole number keeps every piece — and the ring on its ``boxes`` entry —
    on the pixel lattice the art is drawn on.

    ``columns`` is in squares, and every number here is derived from the object,
    so the sheet is bounded like the strip is (:func:`_check_sprite_extent`):
    point a subsprite cell format at bytes that are not it and the record count,
    not just the offsets, is whatever the bytes divide into.
    """
    frames = doc.shown_frames
    records = [
        (at, index) for at, frame in enumerate(frames) for index in range(len(frame))
    ]
    if not by_frame:
        seen: set[tuple] = set()
        unique = []
        for at, index in records:
            key = subsprite_key(frames[at][index])
            if key not in seen:
                seen.add(key)
                unique.append((at, index))
        records = unique
    tile_w, tile_h = max(1, doc.tile_width), max(1, doc.tile_height)
    across = down = 1
    for at, index in records:
        wide, tall = frames[at][index].size()
        across, down = max(across, wide), max(down, tall)
    cell_w, cell_h = across * tile_w, down * tile_h
    columns = max(1, columns)
    # Never zero rows: an object with no records at all still needs a sheet to
    # draw nothing on, the same answer :func:`~celpix.core.sprite.frame_bounds`
    # gives an object with no subsprites.
    rows_of_cells = max(1, ceil_div(len(records), columns))
    _check_sprite_extent(
        columns * cell_w * rows_of_cells * cell_h,
        f"a {columns * cell_w}x{rows_of_cells * cell_h} pixel subsprite sheet",
    )
    image = IndexGrid(columns * cell_w, rows_of_cells * cell_h)
    source = tile_bank(doc, reg)
    space = 1 << pixel_bpp(doc.pixel_config.interpret_preset_id, reg)
    rows = doc.palette_row_wrap(space)
    boxes: list[tuple[int, int, int, int]] = []
    for slot, (at, index) in enumerate(records):
        sub = frames[at][index]
        wide, tall = sub.size()
        box_w, box_h = wide * tile_w, tall * tile_h
        x = (slot % columns) * cell_w + (cell_w - box_w) // 2
        y = (slot // columns) * cell_h + (cell_h - box_h) // 2
        boxes.append((x, y, box_w, box_h))
        _draw_subsprite(image, source, sub, x, y, doc, space, rows)
    return SubspriteSheet(image, records, (cell_w, cell_h), boxes)


@dataclass(frozen=True)
class SpriteHit:
    """What one sheet pixel is: the piece that owns it, and the byte behind it.

    ``tile`` is an index into the bound bank with the base already applied, and
    ``x``/``y`` are the pixel *inside* that tile with the subsprite's flips
    undone — so the two together name a byte a pen can write, which is the whole
    reason this is one answer rather than two lookups that could disagree.

    ``tile`` is **None** where the piece claims the pixel but has no byte there:
    a subsprite pointing outside the bank draws blank. The pick still wants that
    hit (it is a real piece under the cursor); an edit has nothing to write and
    drops it.
    """

    frame: int
    subsprite: int
    piece: Subsprite
    tile: int | None
    x: int
    y: int


def _sprite_walk(
    doc: Document,
    reg: Registry,
    columns: int,
    x: int,
    y: int,
    hoist: tuple[list[Frame], SpriteSheet, list[IndexGrid]] | None,
) -> Iterator[tuple[SpriteHit, bool]]:
    """Every subsprite whose box covers the sheet pixel, front to back.

    :func:`sprite_image` run backwards, and the shared body of the two questions
    asked of it: each hit is paired with whether that piece actually *draws*
    there, which is what separates the one under the cursor from the ones merely
    around it. Yields nothing for a pixel off the sheet, past the last frame, or
    on a document that is not a sprite object.
    """
    if not doc.is_sprite:
        return
    frames, sheet, source = hoist or (doc.shown_frames, None, None)
    if sheet is None:
        sheet = sprite_sheet(doc, columns)
    left, top, width, height = sheet.box
    if not (0 <= x < sheet.across * width and 0 <= y < sheet.down * height):
        return
    at = (y // height) * sheet.across + (x // width)
    if not 0 <= at < len(frames):
        return
    # Into the object's own coordinates: every frame is drawn in the same box, so
    # backing the box's origin out is what turns a sheet pixel into the offset a
    # subsprite states.
    px, py = x % width + left, y % height + top
    step_x, step_y = max(1, doc.tile_width), max(1, doc.tile_height)
    if source is None:
        source = tile_bank(doc, reg)
    for index, sub in enumerate(frames[at]):
        across, down = sub.size()
        dx, dy = px - sub.x, py - sub.y
        col, row = dx // step_x, dy // step_y
        if not (0 <= col < across and 0 <= row < down):
            continue
        tile_index = sub.tile_indices()[row * across + col] + doc.tile_base_index
        # The walk already mirrored the *order* of the tiles, so what is left is
        # mirroring the pixel inside the one under the cursor — the render's own
        # two halves, taken apart (:func:`~celpix.core.tilemap.tile_run`).
        tx, ty = dx % step_x, dy % step_y
        if sub.flip_h:
            tx = step_x - 1 - tx
        if sub.flip_v:
            ty = step_y - 1 - ty
        inside = 0 <= tile_index < len(source)
        yield (
            SpriteHit(at, index, sub, tile_index if inside else None, tx, ty),
            inside and bool(source[tile_index].get(tx, ty)),
        )


def sprite_hit(
    doc: Document,
    reg: Registry,
    columns: int,
    x: int,
    y: int,
    *,
    hoist: tuple[list[Frame], SpriteSheet, list[IndexGrid]] | None = None,
) -> SpriteHit | None:
    """What one sheet pixel *is* — see :class:`SpriteHit`.

    The only way to ask the question: a sprite object has no cell grid to divide
    a position by, its subsprites sit at signed pixel offsets that are mostly not
    tile-aligned, and they overlap. A slot cannot answer it — one 8x8 square of
    the sheet routinely holds pieces of three subsprites — which is why the
    canvas reports the pixel for this, and why the pixel is also the unit a
    stroke through a sprite writes back in (``docs/design/tilemap-entry.md``
    §8.5).

    **Front to back, and what is drawn wins.** The file lists a frame's
    subsprites topmost-first, so the walk is in file order; but index 0 is
    transparent and a large subsprite is mostly hole, so the first one whose
    *box* covers the pixel is not always the one the user is pointing at. So the
    first one that actually draws something there is the answer, and the
    front-most box hit is the fallback — a click on a subsprite's transparent
    part still finds it where nothing else claims the pixel, rather than finding
    nothing and reading as a dead click. A pen lands on the same piece the
    eyedropper samples, which is what keeps "edit what you can see" true here.

    ``columns`` is the frames-across the view is laid out at, as everywhere else
    on the sprite side; ``None`` for a pixel off the sheet, past the last frame,
    or on a document that is not a sprite object.

    ``hoist`` is what a caller resolving many pixels lifts out of its loop —
    :func:`sprite_hoist`'s answer. A fill asks this once per pixel it touches, and
    all three of its parts are rebuilt per call otherwise: the shown frames are a
    fresh list, the sheet walks every frame to find the shared box, and the bank
    is a decode.
    """
    covering: SpriteHit | None = None
    for hit, drawn in _sprite_walk(doc, reg, columns, x, y, hoist):
        if drawn:
            return hit
        if covering is None:
            covering = hit
    return covering


def sprite_hits(
    doc: Document,
    reg: Registry,
    columns: int,
    x: int,
    y: int,
    *,
    hoist: tuple[list[Frame], SpriteSheet, list[IndexGrid]] | None = None,
) -> list[SpriteHit]:
    """*Every* subsprite under the sheet pixel, in the order a click picks them.

    :func:`sprite_hit`'s answer is the first of these, and the rest are what a
    second click on the same tile moves on to: the pieces the front-most one is
    hiding. Same rule, applied all the way down rather than stopping — the ones
    that draw there first, in file order, then the ones whose box covers the
    pixel without putting anything in it.
    """
    drawn: list[SpriteHit] = []
    covering: list[SpriteHit] = []
    for hit, is_drawn in _sprite_walk(doc, reg, columns, x, y, hoist):
        (drawn if is_drawn else covering).append(hit)
    return drawn + covering


def sprite_hoist(doc: Document, reg: Registry, columns: int):  # noqa: ANN201
    """What :func:`sprite_hit` wants lifted out of a per-pixel loop.

    One call before the loop instead of three inside it. Opaque to the caller:
    it carries the tuple from one hit to the next without unpacking it.
    """
    return doc.shown_frames, sprite_sheet(doc, columns), tile_bank(doc, reg)


def subsprite_at(
    doc: Document, reg: Registry, columns: int, x: int, y: int
) -> tuple[int, int] | None:
    """Which subsprite the sheet pixel ``(x, y)`` shows — ``(frame, subsprite)``.

    The pick's half of :func:`sprite_hit`, which is where the walk and the rule
    it follows are stated.
    """
    hit = sprite_hit(doc, reg, columns, x, y)
    return None if hit is None else (hit.frame, hit.subsprite)


def subsprites_at(
    doc: Document, reg: Registry, columns: int, x: int, y: int
) -> list[tuple[int, int]]:
    """Every subsprite under the sheet pixel, front-most first — the pick's cycle.

    :func:`subsprite_at` is this list's first entry;
    :func:`sprite_hits` is where the order comes from.
    """
    return [(hit.frame, hit.subsprite) for hit in sprite_hits(doc, reg, columns, x, y)]


@lru_cache(maxsize=64)
def _transparent_shift(bias: int) -> bytes:
    """The 256-byte map that adds ``bias`` to an index but **keeps 0 at 0**.

    The sprite twin of :func:`~celpix.core.index_grid._shift`, and the difference
    is the whole reason it is a second table: that one moves index 0 along with
    the rest, which is right for a background and turns every transparent pixel
    of a subsprite into an opaque colour of its row. Saturating at 255 for the
    reason the other one does — a row clamped to the palette cannot reach it, so
    this only keeps a hand-edited project from raising instead of rendering.

    Cached because an object holds a handful of distinct palette rows and a frame
    blits a few dozen subsprites through them.
    """
    return bytes([0]) + bytes(min(255, i + bias) for i in range(1, 256))


def _blit(target: IndexGrid, tile: IndexGrid, x: int, y: int, bias: int) -> None:
    """Draw ``tile`` at ``(x, y)``, leaving index 0 and anything off-canvas alone.

    Index 0 is transparent here — subsprites overlap, and one drawn as a solid
    square would erase what it sits in front of — so this cannot be the plain row
    copy :func:`~celpix.core.arrangement._compose_plain` uses. What it can avoid
    is paying for that per *pixel*, and the three steps compound:

    - **The clip is per blit, not per pixel.** A subsprite lands at a signed
      offset, so the columns falling outside the sheet are a prefix and a suffix
      of every row and never a hole in the middle — one pair of bounds serves the
      whole tile, where testing ``0 <= tx < width`` inside the loop paid for it
      once per pixel to learn the same thing.
    - **The palette row is folded in by a translate**, against
      :func:`_transparent_shift`, rather than added and clamped per pixel. Same
      answer at C speed — and, more to the point, it is what leaves a row with
      *nothing left to do to it*.
    - **A row with no transparent pixel is then one store.** Most of a
      subsprite's rows are solid, and for those the test that made this
      per-pixel does not apply at all: ``0 not in line`` is a C scan that buys a
      slice assignment. This is the step the two above exist to enable, and it is
      where most of the saving is.

    The per-pixel fallback stays for rows that do have holes, which is what the
    format actually holds; what changed is that it now runs over a clipped,
    already-biased line and so has a single test in it.
    """
    width, height = target.width, target.height
    tile_w = tile.width
    first = max(0, -x)
    last = min(tile_w, width - x)
    if last <= first:
        return
    run = last - first
    dst = target.data
    src = tile.data
    table = _transparent_shift(bias) if bias else None
    for row in range(tile.height):
        ty = y + row
        if not 0 <= ty < height:
            continue
        start = row * tile_w
        line = src[start + first : start + last]
        if table is not None:
            line = line.translate(table)
        at = ty * width + x + first
        if 0 not in line:
            dst[at : at + run] = line
            continue
        for col, value in enumerate(line):
            if value:
                dst[at + col] = value
