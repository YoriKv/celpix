"""The strictly linear pipeline: run both pathways for load and for save.

Load runs each pathway forward — container.read -> reshape -> decompress ->
interpret — and converges the results into a :class:`Document`. Save mirrors
it — interpret.encode -> compress -> unshape -> container.write — per pathway,
with the palette's save optional. Any stage that cannot proceed raises
:class:`PipelineError`, which halts the pipeline and names the stage + direction
+ pathway + reason; nothing partial is written (``docs/design/overview.md`` §2).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, NamedTuple, TypeVar

from celpix.core import ceil_div, transform
from celpix.core.arrangement import (
    BlockLayout,
    bitmap_tile_size,
    compose_window,
    has_2d_reading,
    reflow_2d,
    scatter_2d,
)
from celpix.core.context import KEY_SOURCE_FILES, KEY_SOURCE_PATH, PipelineContext
from celpix.core.document import Document
from celpix.core.errors import Pathway, PipelineError, Stage
from celpix.core.index_grid import IndexGrid
from celpix.core.notices import warn
from celpix.core.palette import Palette
from celpix.core.sprite import (
    DEFAULT_SUBSPRITE_TILES,
    Frame,
    drawn_frames,
    frame_bounds,
)
from celpix.core.tilemap import Cell
from celpix.pipeline.pathway import PathwayConfig
from celpix.plugins.base import (
    NO_RESHAPE,
    RAW_CONTAINER,
    CompressionPlugin,
    FileRef,
    ReadSource,
    SourceFile,
    WriteTarget,
)
from celpix.plugins.registry import Registry

T = TypeVar("T")


@dataclass(frozen=True)
class ScanResult:
    """Where a forward structure scan ended (:func:`find_next_structure`).

    ``found`` is the hit offset or ``None``; ``end`` is where the scan stopped
    (where the caller lands when there was no hit — ``len(data)`` once the whole
    buffer is exhausted); ``stopped`` is True when the caller aborted the scan
    via its tick callback rather than reaching the end.
    """

    found: int | None
    end: int
    stopped: bool


def find_next_structure(
    data: bytes,
    plugin: CompressionPlugin,
    probe_bytes: int,
    start: int,
    *,
    progress_every: int = 64,
    on_tick: Callable[[int], bool] | None = None,
) -> ScanResult:
    """The first offset ≥ ``start`` where ``plugin`` decodes a complete structure.

    Walks ``data`` one byte at a time, trying a strict decompress of the
    ``probe_bytes`` compressed bytes at each offset; a non-empty result is a hit. This
    is the Qt-free core of the toolbar's *Scan* — a hit is a *complete*, non-empty
    structure, since a best-effort partial decode "succeeds" on almost any bytes
    (so non-self-delimiting schemes are effectively unscannable). Every
    ``progress_every`` bytes ``on_tick(pos)`` is called if given; returning True
    aborts the scan (the UI pumps its event loop and reports a Stop there).
    """
    pos = start
    n = len(data)
    while pos < n:
        try:
            if plugin.decompress(data[pos : pos + probe_bytes], PipelineContext()):
                return ScanResult(pos, pos, False)
        except Exception:  # noqa: BLE001 — not a structure here; keep walking
            pass
        pos += 1
        if on_tick is not None and pos % progress_every == 0 and on_tick(pos):
            return ScanResult(None, pos, True)
    return ScanResult(None, pos, False)


class PixelData(NamedTuple):
    """The pixel pathway loaded up to (but not through) decode.

    The raw decompressed bytes plus the codec geometry needed to decode them a
    window at a time — see :func:`load_pixel_data`.
    """

    data: bytes
    bytes_per_tile: int
    tile_width: int
    tile_height: int
    ctx: PipelineContext


def _run(stage: Stage, pathway: Pathway, fn: Callable[[], T], action: str = "") -> T:
    """Run one stage, translating any failure into a hard-stop PipelineError.

    ``action`` is the direction within the stage (``read``/``write``,
    ``decompress``/``compress``) — a stage covers both, and which one failed is
    what the user needs to know first (:class:`PipelineError`).
    """
    try:
        return fn()
    except PipelineError:
        raise
    except Exception as exc:  # noqa: BLE001 — deliberately funnel every failure
        raise PipelineError(stage, pathway, str(exc), action) from exc


def load_pixel_data(
    cfg: PathwayConfig, reg: Registry, bitmap_width: int = 0
) -> PixelData:
    """Run the pixel pathway forward through Decompress, *without* decoding.

    Returns the raw decompressed bytes plus the codec's atomic geometry, so the view
    can decode only the visible window on demand (:func:`decode_window`) rather than
    the whole file. Data whose length isn't a whole number of tiles is fine — the
    trailing partial tile is zero-padded at decode time (``Document.window_bytes``).

    ``bitmap_width`` is the view's wide-bitmap width, which re-cuts the geometry
    (see :func:`bitmap_params`); 0 leaves the codec's own tiles alone.
    """
    ctx = PipelineContext()
    data = _read_reshape_decompress(cfg, ctx, reg, Pathway.PIXEL)
    return PixelData(data, *_pixel_geometry(cfg, reg, bitmap_width), ctx)


def reinterpret_pixel_data(
    data: bytes,
    ctx: PipelineContext,
    cfg: PathwayConfig,
    reg: Registry,
    bitmap_width: int = 0,
) -> PixelData:
    """Already-loaded bytes under ``cfg``'s Interpret preset — nothing re-read.

    Only the codec's geometry depends on the interpret preset; which bytes there
    are comes out of Read + Decompress, which this leaves alone. That is what
    makes switching formats non-destructive: unsaved edits live *in* these bytes,
    and re-running the pathway would pull the file's own bytes back over them.
    Raises the same :class:`PipelineError` a load would for an unusable preset.
    """
    return PixelData(data, *_pixel_geometry(cfg, reg, bitmap_width), ctx)


def _with_tile_size(engine, params: dict, size: tuple[int, int]) -> dict:  # noqa: ANN001
    """``params`` re-cut to ``size``, or ``params`` itself if that won't stick.

    Whether a codec honours a tile size at all is **probed, not assumed** from
    the preset: the merged params are handed back to ``tile_size`` and kept only
    if the engine reports the size we asked for. A codec can decline in either of
    two ways — by ignoring the keys and reporting its own geometry, or by
    rejecting them outright, as the planar engine does for a width that is not a
    whole number of eight-pixel groups — and the two mean the same thing here, so
    a raise counts as "no" rather than propagating. Returning ``params``
    unchanged is therefore the ordinary outcome.
    """
    if not all(size) or size == engine.tile_size(params):
        return params
    merged = {**params, "tile_width": size[0], "tile_height": size[1]}
    try:
        accepted = engine.tile_size(merged) == size
    except Exception:  # noqa: BLE001 — a probe must not be able to fail the load
        accepted = False
    return merged if accepted else params


def bitmap_params(engine, params: dict, bitmap_width: int) -> dict:  # noqa: ANN001
    """``params`` re-cut to the tile size a ``bitmap_width`` bitmap needs.

    The size itself is :func:`~celpix.core.arrangement.bitmap_tile_size`, applied
    to both axes; whether the codec accepts it is :func:`_with_tile_size`'s probe.
    """
    if bitmap_width <= 0:
        return params
    tile_w, _tile_h = engine.tile_size(params)
    size = bitmap_tile_size(bitmap_width, tile_w)
    return _with_tile_size(engine, params, (size, size))


def tile_params(doc: Document, engine, params: dict) -> dict:  # noqa: ANN001
    """``params`` carrying the tile geometry ``doc`` was actually built with.

    The load path resolves the bitmap-width override once
    (:func:`bitmap_params`) and records the result on the document; every later
    decode/encode has to hand the engine the *same* geometry or it would cut the
    bytes into different tiles than the view is placing. Reading it back off the
    document keeps that single resolution authoritative instead of recomputing
    it — and leaves params untouched whenever the document is on the codec's
    natural tiles, which is every format that has no tile-size parameter.
    """
    return _with_tile_size(engine, params, (doc.tile_width, doc.tile_height))


def _pixel_geometry(
    cfg: PathwayConfig, reg: Registry, bitmap_width: int = 0
) -> tuple[int, int, int]:
    """``(bytes_per_tile, tile_width, tile_height)`` of ``cfg``'s pixel codec."""
    engine, preset = reg.engine_for(cfg.interpret_preset_id)
    params = bitmap_params(engine, preset.params, bitmap_width)
    tile_bytes = _run(
        Stage.INTERPRET_PIXEL,
        Pathway.PIXEL,
        lambda: engine.bytes_per_tile(params),
    )
    if tile_bytes <= 0:
        raise PipelineError(
            Stage.INTERPRET_PIXEL,
            Pathway.PIXEL,
            f"bytes per tile ({tile_bytes}) is not positive",
        )
    return (tile_bytes, *engine.tile_size(params))


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
    engine, preset = reg.engine_for(doc.pixel_config.interpret_preset_id)
    params = tile_params(doc, engine, preset.params)
    return _run(
        Stage.INTERPRET_PIXEL,
        Pathway.PIXEL,
        lambda: engine.decode(window, params, PipelineContext()),
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
    engine, preset = reg.engine_for(doc.pixel_config.interpret_preset_id)
    params = tile_params(doc, engine, preset.params)
    blob = _run(
        Stage.INTERPRET_PIXEL,
        Pathway.PIXEL,
        lambda: engine.encode(tiles, params, PipelineContext()),
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
    return 1 + max(layout.slot_to_cell(s)[1] for s in range(start, count))


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


def drawn_palette_row(row: int, base: int) -> int:
    """The palette row a cell's ``row`` draws through, under a row base.

    Clamped at 0 because a row before the palette's first is not a row. The base
    is signed — a file whose cells name absolute rows, read against a palette
    holding only those, counts *down*
    (:attr:`~celpix.core.document.Document.palette_row_base`) — so without the
    clamp a cell below the base would ask for a negative index shift, which no
    colour table has.
    """
    return max(0, row + base)


def tilemap_tiles(
    doc: Document, reg: Registry, columns: int
) -> tuple[list, BlockLayout]:
    """A tilemap's cells expanded into ready-to-place tiles, and a layout.

    The whole of what makes a tilemap render through the *pixel* view rather
    than a parallel one. Each cell is turned into the source tiles it draws,
    oriented as its flip bits say and carrying its palette row, and the two
    results line up so the existing composer can finish the job:

    - **tiles** — every cell's tiles, consecutively, so a metatile's four land
      next to each other.
    - **layout** — a :class:`BlockLayout` whose *block* is one cell, which is
      what places a 2x2 metatile's four consecutive tiles as a square.

    The palette row travels **in the indices**, the same mechanism pinned
    palette regions use (:meth:`~celpix.core.index_grid.IndexGrid.shifted`): the
    composed image carries one colour table, so a cell that renders through
    another row has that row folded in. It is folded in here rather than handed
    to :func:`compose_tiles` as a bias list so that the shift shares the memo
    below — the same tile drawn twice through the same row is shifted once.

    That memo is the reason this is cheap on a large map. A map is thousands of
    cells over a bank of at most a few hundred distinct tiles, and what a cell
    draws is decided entirely by ``(tile, flip_h, flip_v, row)`` — so the
    flip-and-shift work is per distinct combination rather than per cell, and
    the repeats are the same grid object appearing in the list again.

    A cell naming a tile the source does not have renders blank rather than
    failing: a tilemap is routinely authored against a bank that is loaded
    elsewhere, and half a picture is more useful than an error.

    The cells are walked in the order the view **lays them out**, which is the
    file's own unless the map is several pages assembled side by side
    (:attr:`~celpix.core.document.Document.laid_out_cells`). That is the one place
    an assembly reaches the picture; nothing downstream of here knows about it.

    An assembled document's ``columns`` is **its own**, not the caller's: the width
    and the placement are one answer, and a picture laid out at any other width
    interleaves the pages instead of putting them side by side
    (:attr:`~celpix.core.document.Document.assembled_columns`). So the two cannot
    be passed in separately and disagree — which is what a render reached without
    going through the view would otherwise do.
    """
    cells = doc.laid_out_cells
    columns = doc.assembled_columns or columns
    across, down = max(1, doc.cell_tiles[0]), max(1, doc.cell_tiles[1])
    blank = IndexGrid(doc.tile_width, doc.tile_height)
    space = 1 << pixel_bpp(doc.pixel_config.interpret_preset_id, reg)
    source = tile_bank(doc, reg)
    count = len(source)
    drawn: dict[tuple[int, bool, bool, int], object] = {}
    tiles: list = []
    for cell in cells:
        shift = drawn_palette_row(cell.palette_row, doc.palette_row_base) * space
        flip_h, flip_v = cell.flip_h, cell.flip_v
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
            tiles.append(tile)
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
    """

    grid: IndexGrid
    drawn: int
    palette_rows: int


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
    if doc.is_sprite:
        frames = drawn_frames(doc.sprite_frames or [])
        top = max(
            (drawn_palette_row(s.palette_row, base) for frame in frames for s in frame),
            default=0,
        )
        grid, sheet = sprite_image(doc, reg, columns)
        drawn = sheet.slots
    else:
        top = max(
            (drawn_palette_row(cell.palette_row, base) for cell in doc.drawn_cells),
            default=0,
        )
        tiles, layout = tilemap_tiles(doc, reg, columns)
        # No bias list: the rows are already in the indices (see tilemap_tiles).
        grid, drawn = compose_tiles(tiles, layout, None), len(tiles)
    return TilemapImage(grid, drawn, min(max(1, 256 // max(1, space)), top + 1))


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
    frames = drawn_frames(doc.sprite_frames or [])
    box = frame_bounds(frames, doc.sprite_size_pair, doc.tile_width, doc.tile_height)
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
    """
    frames = drawn_frames(doc.sprite_frames or [])
    pair = doc.sprite_size_pair
    sheet = sprite_sheet(doc, columns)
    left, top, width, height = sheet.box
    across = sheet.across
    image = IndexGrid(across * width, sheet.down * height)
    source = tile_bank(doc, reg)
    space = 1 << pixel_bpp(doc.pixel_config.interpret_preset_id, reg)
    # A subsprite's own tiles step by the *tile* size, the codec's and not an
    # assumed 8: the size pair is stated in tiles for exactly this reason, and a
    # literal here would put the second half of every large one 8px from the first
    # whatever the tiles behind it measure
    # (:data:`~celpix.core.sprite.DEFAULT_SUBSPRITE_TILES`).
    step_x, step_y = max(1, doc.tile_width), max(1, doc.tile_height)
    for at, frame in enumerate(frames):
        ox = (at % across) * width - left
        oy = (at // across) * height - top
        for sub in reversed(frame):
            side = sub.tiles(pair)
            for slot, index in enumerate(sub.tile_indices(pair)):
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
                    ox + sub.x + (slot % side) * step_x,
                    oy + sub.y + (slot // side) * step_y,
                    drawn_palette_row(sub.palette_row, doc.palette_row_base) * space,
                )
    return image, sheet


def _blit(target: IndexGrid, tile: IndexGrid, x: int, y: int, bias: int) -> None:
    """Draw ``tile`` at ``(x, y)``, leaving index 0 and anything off-canvas alone.

    Per pixel, because index 0 has to be skipped and the offset need not be
    tile-aligned — neither a row copy nor a translate can express that. The cost
    is bounded by what a sprite object *is*: a few dozen subsprites a frame, and the
    corpus's largest object is a few hundred tiles all told.

    ``bias`` is the subsprite's palette row folded into the indices, the shift a
    pinned palette region uses — but applied **here**, per pixel, rather than to
    the tile up front. :meth:`~celpix.core.index_grid.IndexGrid.shifted` moves
    index 0 along with the rest, which on a background is right and on a sprite
    turns every transparent pixel into an opaque colour of that row.
    """
    width, height = target.width, target.height
    for row in range(tile.height):
        ty = y + row
        if not 0 <= ty < height:
            continue
        start = row * tile.width
        line = tile.data[start : start + tile.width]
        base = ty * width
        for col, value in enumerate(line):
            tx = x + col
            if value and 0 <= tx < width:
                target.data[base + tx] = min(255, value + bias)


class PaletteData(NamedTuple):
    """A loaded palette plus the bytes it came from.

    ``data`` is kept so a later save can splice edited entries into it instead
    of re-encoding the whole palette, which would not round-trip
    (see :func:`_save_palette`).
    """

    palette: Palette
    ctx: PipelineContext
    data: bytes


def load_palette(cfg: PathwayConfig, reg: Registry) -> PaletteData:
    """Run the palette pathway forward: Read -> Decompress -> decode to a Palette."""
    ctx = PipelineContext()
    data = _read_reshape_decompress(cfg, ctx, reg, Pathway.PALETTE)
    engine, preset = reg.engine_for(cfg.interpret_preset_id)
    colors = _run(
        Stage.INTERPRET_PALETTE,
        Pathway.PALETTE,
        lambda: engine.decode(data, preset.params, ctx),
    )
    return PaletteData(colors, ctx, data)


class TilemapData(NamedTuple):
    """The tilemap pathway loaded up to (but not through) grid layout.

    ``cells`` are in the file's own order; ``data`` is kept for the same reason
    the palette keeps its bytes — an edit is spliced into the buffer that was
    read rather than re-encoded from a partial grid. ``cell_tiles`` is how many
    tiles one cell covers, which only the codec knows.

    ``frames`` is set only by a codec whose cells are **subsprites**, whose
    pixel offsets no grid can express (:mod:`celpix.core.sprite`). None for every
    ordinary tilemap, which is drawn as the grid its cells already are.

    ``index_mask`` is the ``index`` field's own width, so a multi-tile cell's
    neighbours wrap inside the field rather than running past the end
    (:attr:`~celpix.core.document.Document.index_mask`) — the codec's
    :meth:`~celpix.plugins.base.TilemapCodecPlugin.index_limit`, since the mask
    *is* the highest value the field holds. 0 for a format that does not say.

    ``palette_row_base`` is the palette row a cell's row 0 means — 8 for a sprite,
    whose 3-bit field counts from the upper half of CGRAM
    (:attr:`~celpix.core.document.Document.palette_row_base`).

    ``palette_rows`` is whether the *format* gives a cell a palette row to name —
    the format's word, not this file's, so a screen whose cells all happen to sit
    on row 0 still reports True. What it gates is whether the view's subpalette
    applies at all (``docs/design/tilemap-entry.md`` §8).
    """

    cells: list[Cell]
    cell_bytes: int
    cell_tiles: tuple[int, int]
    ctx: PipelineContext
    data: bytes
    frames: list[Frame] | None = None
    size_pair: tuple[int, int] = DEFAULT_SUBSPRITE_TILES
    palette_rows: bool = True
    index_mask: int = 0
    palette_row_base: int = 0


def load_tilemap_data(cfg: PathwayConfig, reg: Registry) -> TilemapData:
    """Run the tilemap pathway forward: Read -> Decompress -> decode to cells.

    Stops at a flat list rather than a grid. A tilemap file rarely states its own
    width — a screen is four fixed 32x32 blocks, a stamp layout's 128 columns had
    to be recovered from the data — so the layout is the view's to choose and
    change without re-reading anything
    (``docs/graphics-formats-reference/scgcad-formats.md`` §4).
    """
    ctx = PipelineContext()
    data = _read_reshape_decompress(cfg, ctx, reg, Pathway.TILEMAP)
    engine, preset = reg.engine_for(cfg.interpret_preset_id)
    cells = _run(
        Stage.INTERPRET_TILEMAP,
        Pathway.TILEMAP,
        lambda: engine.decode(data, preset.params, ctx),
    )
    cell_bytes = _run(
        Stage.INTERPRET_TILEMAP,
        Pathway.TILEMAP,
        lambda: engine.bytes_per_cell(preset.params),
    )
    if cell_bytes <= 0:
        raise PipelineError(
            Stage.INTERPRET_TILEMAP,
            Pathway.TILEMAP,
            f"bytes per cell ({cell_bytes}) is not positive",
        )
    tiles = _run(
        Stage.INTERPRET_TILEMAP,
        Pathway.TILEMAP,
        lambda: engine.cell_tiles(preset.params),
    )
    # The optional half of the protocol, and the only one an engine may lack: a
    # format whose cells are subsprites groups them into frames itself, because
    # what they *mean* — which frame, at what offset, how big — is the same
    # knowledge that decoded them (:mod:`celpix.plugins.builtins.object_codec`).
    frames = None
    size_pair = DEFAULT_SUBSPRITE_TILES
    if hasattr(engine, "frames"):
        frames = _run(
            Stage.INTERPRET_TILEMAP,
            Pathway.TILEMAP,
            lambda: engine.frames(cells, preset.params),
        )
        size_pair = _run(
            Stage.INTERPRET_TILEMAP,
            Pathway.TILEMAP,
            lambda: engine.size_pair(preset.params),
        )
    # The other optional half: whether the format has a palette row for a cell to
    # name. **True when the engine does not answer**, which is the safe
    # direction — a format that does carry rows and stayed quiet must not have a
    # view-wide row added on top of the ones its cells already state.
    rows = True
    if hasattr(engine, "has_palette_rows"):
        rows = bool(
            _run(
                Stage.INTERPRET_TILEMAP,
                Pathway.TILEMAP,
                lambda: engine.has_palette_rows(preset.params),
            )
        )
    # The index field's own width, straight off the codec
    # (:meth:`~celpix.plugins.base.TilemapCodecPlugin.index_limit`) rather than a
    # second parameter that could disagree with the field table -- the mask *is*
    # the highest value the field holds. Probed like the other optional methods:
    # a format that cannot say leaves its references unbounded.
    mask = 0
    ask = getattr(engine, "index_limit", None)
    if ask is not None:
        try:
            top = ask(preset.params)
        except Exception:  # noqa: BLE001 — a probe must not fail the load
            top = None
        mask = top if top and top > 0 else 0
    # Where the format's rows count from. A plain parameter, unlike the index
    # mask: no field table implies it — it is a fact about the console's palette
    # layout for this kind of cell, not about how the cell is packed.
    row_base = int(preset.params.get("palette_row_base", 0) or 0)
    return TilemapData(
        cells, cell_bytes, tiles, ctx, data, frames, size_pair, rows, mask, row_base
    )


def encode_cells(cells: list[Cell], preset_id: str, reg: Registry) -> bytes:
    """``cells`` back to bytes under ``preset_id`` — the save-side half.

    Separate from a whole-document save because a tilemap edit is local: the
    caller encodes the cells that changed and splices them into the buffer
    :func:`load_tilemap_data` returned, rather than re-encoding a grid whose
    trailing partial row was never part of the file.
    """
    engine, preset = reg.engine_for(preset_id)
    ctx = PipelineContext()
    return _run(
        Stage.INTERPRET_TILEMAP,
        Pathway.TILEMAP,
        lambda: engine.encode(cells, preset.params, ctx),
        "encode",
    )


def read_region(cfg: PathwayConfig, reg: Registry) -> tuple[bytes, PipelineContext]:
    """A pathway's Read alone: container -> reshape -> decompress, no decoding.

    The front half of a load without a codec, for a caller that
    needs an entry's **view buffer** rather than its picture — an Offset palette
    resolving its coordinates against the file entry that owns them, when that
    entry is closed and has no live document to borrow the bytes from
    (``docs/design/palette-editing.md`` §2). The context comes back because the
    buffer alone doesn't say where it starts: only the container knows that, and
    it reports it as ``KEY_SOURCE_OFFSET``.
    """
    ctx = PipelineContext()
    return _read_reshape_decompress(cfg, ctx, reg, Pathway.PIXEL), ctx


def palette_entry_size(preset_id: str, reg: Registry) -> int:
    """Byte size of one palette entry under the preset — for sizing palette reads."""
    engine, preset = reg.engine_for(preset_id)
    return _run(
        Stage.INTERPRET_PALETTE,
        Pathway.PALETTE,
        lambda: engine.bytes_per_entry(preset.params),
    )


def quantize_color(argb: int, preset_id: str, reg: Registry) -> int:
    """``argb`` as it would come back after a round trip through ``preset_id``.

    Encode-then-decode of a one-entry palette: the color editor edits in full
    8-bit RGB, and this is what the chosen palette format can actually store —
    BGR555 drops the low three bits of each channel, an indexed format snaps to
    its nearest hardware color. Shown live beside the edited color so the loss
    is visible *before* it is written (docs/design/palette-editing.md).
    """
    return quantize_palette(Palette([argb]), preset_id, reg).color(0)


def quantize_palette(palette: Palette, preset_id: str, reg: Registry) -> Palette:
    """``palette`` as it comes back after a round trip through ``preset_id``.

    The whole-palette form of :func:`quantize_color`: encode the colors to the
    format's bytes and decode them straight back, so every entry lands on a
    value that format can actually hold. Used to *rebase* a Custom palette when
    its color format is changed — a Custom palette has no source bytes to
    reinterpret, so its stored ARGB colors are re-expressed in the new format
    instead of anything being re-read.
    """
    engine, preset = reg.engine_for(preset_id)

    def _round_trip() -> Palette:
        ctx = PipelineContext()
        data = engine.encode(palette, preset.params, ctx)
        return engine.decode(data, preset.params, ctx)

    return _run(Stage.INTERPRET_PALETTE, Pathway.PALETTE, _round_trip)


def palette_has_alpha(preset_id: str, reg: Registry) -> bool:
    """Whether ``preset_id`` actually stores an alpha channel.

    Probed behaviourally rather than by reading codec params, so it holds for
    every color engine — mask-based, indexed, or a plugin's own — without any
    of them growing a new method: a format with no alpha field decodes one back
    as opaque (``_mask.value_to_argb`` substitutes ``0xFF``), so a transparent
    color that survives the round trip proves the field exists.

    Drives whether the color editor offers an alpha input at all.
    """
    return quantize_color(0x00FFFFFF, preset_id, reg) >> 24 != 0xFF


def pixel_is_direct_color(preset_id: str, reg: Registry) -> bool:
    """Whether ``preset_id``'s codec produces colors rather than palette indices.

    Probed behaviourally — a blank tile is decoded and its grid type inspected —
    for the same reason :func:`palette_has_alpha` is: it then holds for every
    pixel engine, including a plugin's own, without any of them declaring a new
    capability flag. Tells the editing paths whether incoming pixels must be
    fitted to the palette or carried through as color.
    """
    engine, preset = reg.engine_for(preset_id)

    def _probe() -> bool:
        blank = bytes(engine.bytes_per_tile(preset.params))
        tiles = engine.decode(blank, preset.params, PipelineContext())
        return bool(tiles) and tiles[0].bytes_per_pixel == 4

    return _run(Stage.INTERPRET_PIXEL, Pathway.PIXEL, _probe)


def pixel_bpp(preset_id: str, reg: Registry) -> int:
    """Bits per pixel of a pixel preset, from its resolved engine's geometry.

    Derived (tile bits ÷ tile pixels) rather than read from ``params["bpp"]``: bpp
    is a property of the codec's tile layout, and not every codec spells it as a
    preset param — the wide/odd-tile codecs and code formats fix their geometry
    intrinsically and carry no ``bpp``. Every pixel engine exposes
    ``bytes_per_tile``/``tile_size``, so deriving it here is uniform and matches
    whatever the decoder actually produced. Rounded up so a non-whole bit depth
    still yields an index space wide enough for its largest index.
    """
    engine, preset = reg.engine_for(preset_id)

    def _bpp() -> int:
        w, h = engine.tile_size(preset.params)
        pixels = w * h
        if pixels <= 0:
            raise ValueError(f"tile {w}x{h} has no pixels")
        return ceil_div(engine.bytes_per_tile(preset.params) * 8, pixels)

    return _run(Stage.INTERPRET_PIXEL, Pathway.PIXEL, _bpp)


def load(pixel: PathwayConfig, palette: PathwayConfig, reg: Registry) -> Document:
    """Read + decompress both pathways into a Document (pixels decode on demand)."""
    px = load_pixel_data(pixel, reg)
    pal = load_palette(palette, reg)
    return Document(
        pixel_data=px.data,
        bytes_per_tile=px.bytes_per_tile,
        tile_width=px.tile_width,
        tile_height=px.tile_height,
        palette=pal.palette,
        pixel_config=pixel,
        palette_config=palette,
        pixel_ctx=px.ctx,
        palette_ctx=pal.ctx,
        palette_base_bytes=pal.data,
    )


def save(
    doc: Document, reg: Registry, *, pixel: bool = True, palette: bool = True
) -> None:
    """Encode + compress + write the requested pathways.

    ``write_enabled=False`` on the pixel pathway marks a view-only document — e.g.
    a decompressed slice whose scheme has no compressor — and skips its write.

    ``pixel``/``palette`` let a caller write one pathway alone. The two go to
    different files, so a palette-only edit has no business rewriting the graphic
    (which for a compressed slice could even re-encode to equivalent-but-different
    bytes — see :func:`_save_pixel`).

    A pathway marked ``writes_through_parent`` is refused rather than deposited:
    its target names a file position its bytes do not occupy, so writing it here
    would scatter them. Routing it is the host's job (it is the one that knows
    the parent) — see :func:`encoded_pixel_bytes`.
    """
    if pixel and doc.is_tilemap:
        # ``pixel`` means "the entry's own data", and a tilemap entry's own data
        # is its cells. Its pixel pathway points at whatever tile source it is
        # bound to — a *different* entry's file — which must never be written as
        # a side effect of saving the map (``docs/design/tilemap-entry.md`` §3).
        _save_tilemap(doc, reg)
    elif pixel and doc.pixel_config.write_enabled:
        if doc.pixel_config.writes_through_parent:
            raise PipelineError(
                Stage.CONTAINER,
                Pathway.PIXEL,
                "this region is inside one its parent reorders, so its bytes "
                "have no file position of their own; it must be written through "
                "the parent",
                "write",
            )
        _save_pixel(doc, reg)
    if palette and doc.palette_config.write_enabled:
        _save_palette(doc, reg)


def export_palette(
    doc: Document, path: str, reg: Registry, preset_id: str | None = None
) -> None:
    """Write ``doc``'s palette to ``path`` as a standalone palette file.

    A **whole-palette** encode, unlike the splicing :func:`_save_palette` does:
    the destination is a new file with nothing in it to preserve, so every entry
    is written and the file holds exactly the colors the panel is showing.
    Uncompressed and at offset 0 — a ``.pal`` is the bytes themselves, whatever
    container the palette was read out of.

    ``preset_id`` names the color format to write in, defaulting to the one the
    palette was *read* with. Callers exporting for interchange pass an explicit
    format instead: a ``.pal`` records nothing about its own encoding, so the
    format a reader has to guess should be a deliberate choice, not a side
    effect of how these colors happened to arrive.
    """
    engine, preset = reg.engine_for(preset_id or doc.palette_config.interpret_preset_id)
    data = _run(
        Stage.INTERPRET_PALETTE,
        Pathway.PALETTE,
        lambda: engine.encode(doc.palette, preset.params, doc.palette_ctx),
    )

    def write() -> None:
        container = reg.plugin(Stage.CONTAINER, RAW_CONTAINER)
        _deposit(FileRef(path), lambda t: container.write(data, t, doc.palette_ctx))

    _run(Stage.CONTAINER, Pathway.PALETTE, write, "write")


def _existing(path: str) -> bytes:
    """A *destination's* current bytes, or ``b""`` when it isn't there yet.

    Write-side only. A missing source is a hard failure — reading one lets the
    OSError out so the load stops and says so, rather than opening an empty
    document that looks like a file full of zeroes.
    """
    target = Path(path)
    return target.read_bytes() if target.exists() else b""


def _acquire(ref: FileRef) -> tuple[ReadSource, tuple[SourceFile, ...]]:
    """Resolve a :class:`FileRef` to the bytes a container is handed, plus the
    files those bytes came from.

    The host's half of the container contract: opening the files (or taking the
    in-memory buffer a caller supplied) happens here, once, so that every
    container gets the same answer and none of them has to know which of the two
    it is looking at. A container that opened the path itself would serve the
    file's *saved* bytes to a slice whose parent has unsaved edits.

    **Several files are joined end to end**, in the order the ref names them, and
    handed over as one buffer. That is the whole of multi-file support as a
    container sees it: nothing in the contract changes, and every container ever
    written works on a joined region without being told. The spans come back
    alongside for the caller to publish (``KEY_SOURCE_FILES``) and are what the
    container would consult if it did care.
    """
    if ref.data is not None:
        # An in-memory source is one buffer by construction — it is a slice of a
        # parent's live bytes, which were themselves already joined if the parent
        # had several files.
        source = ReadSource(ref.data, ref.path, ref.offset, ref.length, ref.data_base)
        return source, (SourceFile(ref.path, 0, len(ref.data)),)
    blobs = [Path(path).read_bytes() for path in ref.paths]
    spans, at = [], 0
    for path, blob in zip(ref.paths, blobs):
        spans.append(SourceFile(path, at, len(blob)))
        at += len(blob)
    joined = b"".join(blobs)
    return ReadSource(joined, ref.path, ref.offset, ref.length), tuple(spans)


def _deposit(ref: FileRef, produce: Callable[[WriteTarget], bytes]) -> None:
    """Hand ``ref``'s current bytes to ``produce`` and store what it returns.

    The mirror of :func:`_acquire`, and the only place a pathway writes a file.
    The destination is read first because a container returns it *whole* — that is
    what lets it keep the framing and the surrounding bytes it never decoded — so
    it has to be shown what is there to keep.

    With several files the returned buffer is cut back apart at the boundaries the
    files on disk have *now*, and each piece written to its own file. So the
    result has to be the length it was handed: the boundaries are the only thing
    that says which bytes belong to which file, and a buffer that changed size has
    moved every boundary after the change by an unknown amount. Refusing is the
    only safe answer — the alternative is writing most of a sprite sheet into the
    wrong chip. A single file has no boundaries to keep and so is free to grow or
    shrink, which is what a fresh palette export relies on.

    Files whose bytes did not change are left alone rather than rewritten
    identically: editing one tile of a region spread over four ROM chips should
    not touch the timestamps of the three that did not change.
    """
    if len(ref.paths) == 1:
        existing = _existing(ref.path)
        result = produce(WriteTarget(existing, ref.path, ref.offset, ref.length))
        Path(ref.path).write_bytes(result)
        return
    blobs = [_existing(path) for path in ref.paths]
    existing = b"".join(blobs)
    result = produce(WriteTarget(existing, ref.path, ref.offset, ref.length))
    if len(result) != len(existing):
        raise ValueError(
            f"result is {len(result)} bytes but the {len(ref.paths)} files it has "
            f"to be split back into hold {len(existing)}; the file boundaries "
            "would move, so nothing was written"
        )
    at = 0
    for path, blob in zip(ref.paths, blobs):
        chunk = result[at : at + len(blob)]
        at += len(blob)
        if chunk != blob:
            Path(path).write_bytes(chunk)


def _read_reshape_decompress(
    cfg: PathwayConfig, ctx: PipelineContext, reg: Registry, pathway: Pathway
) -> bytes:
    # Said once per load, here rather than where the config was built, because a
    # notice needs the context a load creates. Without it the user meets a file
    # that opens looking wrong with Write greyed out and nothing saying why.
    for stage, wanted in cfg.missing_plugins:
        warn(
            ctx,
            f"Missing plugin: {wanted}",
            f"This entry reads through a {stage.value} plugin this build\n"
            "does not have, so its bytes are shown untransformed and\n"
            "cannot be saved. Install the plugin, or choose a different\n"
            f"{stage.value} to make the entry editable again.",
            stage.value,
        )

    def read() -> bytes:
        source, files = _acquire(cfg.source)
        # Provenance the host owns, because it is the host that knows where the
        # bytes came from; the container publishes only KEY_SOURCE_OFFSET, which
        # is a fact about the format rather than about the source. Both keys are
        # set before the container runs, so one that wants to know how its buffer
        # was assembled can read KEY_SOURCE_FILES while assembling it.
        ctx.set(KEY_SOURCE_PATH, source.path)
        ctx.set(KEY_SOURCE_FILES, files)
        return reg.plugin(Stage.CONTAINER, cfg.container_id).read(source, ctx)

    raw = _run(Stage.CONTAINER, pathway, read, "read")
    # The reshape sits between the container and the decompressor so that a
    # compressed structure inside an interleaved region is contiguous by the
    # time compression sees it. It is handed the container's whole payload —
    # a reshape is region-scoped, and the region is what Read produced.
    shaped = _run(
        Stage.RESHAPE,
        pathway,
        lambda: reg.plugin(Stage.RESHAPE, cfg.reshape_id).reshape(raw, ctx),
        "reshape",
    )
    return _run(
        Stage.COMPRESSION,
        pathway,
        lambda: reg.plugin(Stage.COMPRESSION, cfg.compression_id).decompress(
            shaped, ctx
        ),
        "decompress",
    )


def _save_pixel(doc: Document, reg: Registry) -> None:
    # The decompressed pixel bytes are the source of truth: edits are already
    # spliced into them (encode_tiles -> Document.replace_bytes), so saving is just
    # compress + write of the buffer as it stands. Writing the bytes is exactly
    # equivalent to encode(decode(bytes)) for these codecs, and avoids decoding the
    # whole file just to save it. Note that a real compressor may make different
    # encoding choices than the original stream, so writing a *compressed* pathway
    # can rewrite equivalent-but-different bytes inside the slot even where nothing
    # was edited — harmless, and rare: dirty tracking gates Write All.
    _compress_unshape_write(
        doc.pixel_config, doc.pixel_data, doc.pixel_ctx, reg, Pathway.PIXEL
    )


def _save_tilemap(doc: Document, reg: Registry) -> None:
    """Encode a tilemap's cells and write them back through its own container.

    Re-encoded from the cells rather than written from the buffer they were read
    into, unlike :func:`_save_pixel`. A tilemap edit changes a *cell*, which is a
    model object and not a byte range, so the cells are the source of truth here
    and the buffer is only what they came from. The round trip is exact — every
    field a format has survives it (:mod:`celpix.plugins.builtins.tilemap_codec`)
    — so an unedited map still writes back the bytes it was read from.

    The container's write half is what preserves everything around the payload:
    a screen's trailing metadata block, a panel's flag table. Nothing here has to
    know those exist.
    """
    cfg = doc.tilemap_config
    if cfg is None or not cfg.write_enabled:
        return
    engine, preset = reg.engine_for(cfg.interpret_preset_id)
    data = _run(
        Stage.INTERPRET_TILEMAP,
        Pathway.TILEMAP,
        lambda: engine.encode(doc.cells or [], preset.params, doc.tilemap_ctx),
        "encode",
    )
    _compress_unshape_write(cfg, data, doc.tilemap_ctx, reg, Pathway.TILEMAP)


def encoded_pixel_bytes(doc: Document, reg: Registry) -> bytes:
    """The pixel buffer as a save would lay it down: compressed and un-reshaped.

    :func:`_save_pixel` without the deposit, for the entry whose bytes have no
    file position to be deposited *at*: a slice inside a region its parent
    reorders. Those bytes belong at an offset in the parent's buffer, and only
    the parent's own write — which carries the whole region through ``unshape``
    and the container — knows where that lands in the files. The host splices
    this in and writes the parent (``docs/design/reshape-stage.md`` §3), exactly
    as an Offset palette in the same position rides its owner
    (:func:`spliced_palette_bytes`).

    The slot checks still apply and are still made here, against the slice's own
    bounds: what is produced has to fit the window it came from wherever it is
    ultimately delivered.
    """
    return _compress_unshape(
        doc.pixel_config, doc.pixel_data, doc.pixel_ctx, reg, Pathway.PIXEL
    )


def _save_palette(doc: Document, reg: Registry) -> None:
    """Encode + write the palette, **splicing** only the entries that changed.

    A color codec round-trips ARGB faithfully but not *bytes*: anything outside
    its masks is dropped, and an indexed codec has no inverse at all. So writing
    a full re-encode would rewrite — and corrupt — entries the user never
    touched. Instead the freshly encoded bytes of edited entries are spliced
    into the buffer the palette was read from, leaving every other byte exactly
    as it was found (see :attr:`Document.palette_base_bytes`).

    Falls back to a whole-palette encode when there is nothing to splice into —
    no original bytes, or a palette whose length no longer matches them (a
    format switch changes the entry size, so the old buffer doesn't apply).
    """
    data = spliced_palette_bytes(doc, reg)
    _compress_unshape_write(
        doc.palette_config, data, doc.palette_ctx, reg, Pathway.PALETTE
    )
    # The file now holds these bytes, so they become the baseline for the next
    # splice and no entry is outstanding. Skipping this would make a second save
    # splice against pre-save bytes and undo the first one's edits.
    doc.palette_base_bytes = data
    doc.palette_edits = set()


def spliced_palette_bytes(doc: Document, reg: Registry) -> bytes:
    """The palette's byte image as a save would write it: the freshly encoded
    bytes of the *edited* entries spliced into the buffer it was read from.

    The encode half of :func:`_save_palette`, usable without a write target —
    an Offset palette living inside a reordered region persists an edit by
    splicing this into the owning entry's pixel buffer rather than through its
    own (write-disabled) pathway (``docs/design/palette-editing.md`` §2).
    """
    cfg = doc.palette_config
    engine, preset = reg.engine_for(cfg.interpret_preset_id)
    encoded = _run(
        Stage.INTERPRET_PALETTE,
        Pathway.PALETTE,
        lambda: engine.encode(doc.palette, preset.params, doc.palette_ctx),
    )
    return _splice_palette(doc, encoded, engine, preset)


def _splice_palette(doc: Document, encoded: bytes, engine, preset) -> bytes:  # noqa: ANN001
    original = doc.palette_base_bytes
    if not original or len(original) != len(encoded):
        return encoded
    size = _run(
        Stage.INTERPRET_PALETTE,
        Pathway.PALETTE,
        lambda: engine.bytes_per_entry(preset.params),
    )
    if size <= 0:
        return encoded
    out = bytearray(original)
    for index in doc.palette_edits:
        start = index * size
        if 0 <= start and start + size <= len(out):
            out[start : start + size] = encoded[start : start + size]
    return bytes(out)


def _compress_unshape_write(
    cfg: PathwayConfig,
    data: bytes,
    ctx: PipelineContext,
    reg: Registry,
    pathway: Pathway,
) -> None:
    """Compress ``data``, undo any reshape, and write it to the config's target."""
    shaped = _compress_unshape(cfg, data, ctx, reg, pathway)
    target = cfg.write_target()

    def write() -> None:
        container = reg.plugin(Stage.CONTAINER, cfg.container_id)
        _deposit(target, lambda dest: container.write(shaped, dest, ctx))

    _run(Stage.CONTAINER, pathway, write, "write")


def _compress_unshape(
    cfg: PathwayConfig,
    data: bytes,
    ctx: PipelineContext,
    reg: Registry,
    pathway: Pathway,
) -> bytes:
    """``data`` compressed and un-reshaped: the bytes that belong in the slot.

    The write minus the deposit, so the checks that make a slot safe are stated
    once and hold however the bytes are then delivered — through the container to
    a file, or spliced into a parent's buffer
    (:func:`encoded_pixel_bytes`).

    A bounded target (``length`` set — a slice of a larger file) is a hard slot:
    a result that would overflow it raises before anything touches the file. A
    result *smaller* than the slot is written short, leaving the slot's tail
    bytes as they were — every supported scheme is self-delimiting, so the stale
    tail is inert, and not rewriting it keeps the file diff minimal.

    **Under an active reshape the slot must be filled exactly.** A reshape's
    part boundaries are fractions of the region's length, so a short result is
    a *different region* whose unshape scatters every byte to the wrong chip —
    there is no such thing as writing the front of one.
    """
    packed = _run(
        Stage.COMPRESSION,
        pathway,
        lambda: reg.plugin(Stage.COMPRESSION, cfg.compression_id).compress(data, ctx),
        "compress",
    )
    shaped = _run(
        Stage.RESHAPE,
        pathway,
        lambda: reg.plugin(Stage.RESHAPE, cfg.reshape_id).unshape(packed, ctx),
        "unshape",
    )
    target = cfg.write_target()
    if target.length is not None and len(shaped) > target.length:
        raise PipelineError(
            Stage.CONTAINER,
            pathway,
            f"result ({len(shaped)} bytes) exceeds the {target.length}-byte slot "
            f"at {target.offset:#x} in {target.path}",
            "write",
        )
    if (
        cfg.reshape_id != NO_RESHAPE
        and target.length is not None
        and len(shaped) != target.length
    ):
        raise PipelineError(
            Stage.RESHAPE,
            pathway,
            f"result ({len(shaped)} bytes) must fill the {target.length}-byte "
            f"slot at {target.offset:#x} in {target.path} exactly: a reshape's "
            "boundaries are fractions of the region, so a shorter region is a "
            "different reshape",
            "unshape",
        )
    return shaped
