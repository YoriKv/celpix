"""The strictly linear pipeline: run both pathways for load and for save.

Load runs each pathway forward — container.read -> reshape -> decompress ->
interpret — and converges the results into a :class:`Document`. Save mirrors
it — interpret.encode -> compress -> unshape -> container.write — per pathway,
with the palette's save optional. Any stage that cannot proceed raises
:class:`PipelineError`, which halts the pipeline and names the stage + direction
+ pathway + reason; nothing partial is written (``docs/design/overview.md`` §2).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable, NamedTuple, TypeVar

from celpix.core import ceil_div, transform
from celpix.core.address import format_hex
from celpix.core.arrangement import (
    BlockLayout,
    bitmap_tile_size,
    compose_window,
    has_2d_reading,
    reflow_2d,
    scatter_2d,
)
from celpix.core.context import (
    KEY_SOURCE_FILES,
    KEY_SOURCE_OFFSET,
    KEY_SOURCE_PATH,
    KEY_TILEMAP_ANIMATIONS,
    KEY_TILEMAP_PALETTE_ROW_BASE,
    PipelineContext,
    hint_info,
)
from celpix.core.document import Document
from celpix.core.errors import Pathway, PipelineError, Stage
from celpix.core.index_grid import IndexGrid
from celpix.core.notices import KEY_NOTICES, Notice, notices, warn
from celpix.core.palette import Palette
from celpix.core.sprite import (
    DEFAULT_SUBSPRITE_TILES,
    Frame,
    frame_bounds,
)
from celpix.core.tilemap import Cell, resolve_cell
from celpix.pipeline.pathway import PathwayConfig
from celpix.plugins.base import (
    NO_RESHAPE,
    RAW_CONTAINER,
    CompressionPlugin,
    ContainerField,
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

    An assembled document's ``columns`` is **its own**, not the caller's: the width
    and the placement are one answer, and a picture laid out at any other width
    interleaves the pages instead of putting them side by side
    (:attr:`~celpix.core.document.Document.assembled_columns`). So the two cannot
    be passed in separately and disagree — which is what a render reached without
    going through the view would otherwise do.
    """
    return expand_cells(doc, reg, doc.laid_out_cells, doc.assembled_columns or columns)


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
    and the cells arrive already expanded into blocks for it to square up.

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
    cols = max(1, doc.assembled_columns or columns)
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
        start, stop = max(0, -base), doc.tile_count - base
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
        # The block a coordinate names, walked the source's own rows the way the
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
    pair = doc.sprite_size_pair
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
                    drawn_palette_row(sub.palette_row, doc.palette_row_base, rows)
                    * space,
                )
    return image, sheet


def subsprite_at(
    doc: Document, reg: Registry, columns: int, x: int, y: int
) -> tuple[int, int] | None:
    """Which subsprite the sheet pixel ``(x, y)`` shows — ``(frame, subsprite)``.

    :func:`sprite_image` run backwards, and the only way to ask the question: a
    sprite object has no cell grid to divide a position by, its subsprites sit at
    signed pixel offsets that are mostly not tile-aligned, and they overlap. A
    slot cannot answer it — one 8x8 square of the sheet routinely holds pieces of
    three subsprites — which is why the canvas reports the pixel for this.

    **Front to back, and what is drawn wins.** The file lists a frame's
    subsprites topmost-first, so the walk is in file order; but index 0 is
    transparent and a large subsprite is mostly hole, so the first one whose
    *box* covers the pixel is not always the one the user is pointing at. So the
    first one that actually draws something there is the answer, and the
    front-most box hit is the fallback — a click on a subsprite's transparent
    part still picks it where nothing else claims the pixel, rather than picking
    nothing and reading as a dead click.

    ``columns`` is the frames-across the view is laid out at, as everywhere else
    on the sprite side; ``None`` for a pixel off the sheet, past the last frame,
    or on a document that is not a sprite object.
    """
    if not doc.is_sprite:
        return None
    frames = doc.shown_frames
    sheet = sprite_sheet(doc, columns)
    left, top, width, height = sheet.box
    if not (0 <= x < sheet.across * width and 0 <= y < sheet.down * height):
        return None
    at = (y // height) * sheet.across + (x // width)
    if not 0 <= at < len(frames):
        return None
    # Into the object's own coordinates: every frame is drawn in the same box, so
    # backing the box's origin out is what turns a sheet pixel into the offset a
    # subsprite states.
    px, py = x % width + left, y % height + top
    pair = doc.sprite_size_pair
    step_x, step_y = max(1, doc.tile_width), max(1, doc.tile_height)
    source = tile_bank(doc, reg)
    covering: tuple[int, int] | None = None
    for index, sub in enumerate(frames[at]):
        side = sub.tiles(pair)
        dx, dy = px - sub.x, py - sub.y
        col, row = dx // step_x, dy // step_y
        if not (0 <= col < side and 0 <= row < side):
            continue
        if covering is None:
            covering = (at, index)
        tile_index = sub.tile_indices(pair)[row * side + col] + doc.tile_base_index
        if not 0 <= tile_index < len(source):
            continue
        tile = source[tile_index]
        # The walk already mirrored the *order* of the tiles, so what is left is
        # mirroring the pixel inside the one under the cursor — the render's own
        # two halves, taken apart (:func:`~celpix.core.tilemap.tile_run`).
        tx, ty = dx % step_x, dy % step_y
        if sub.flip_h:
            tx = tile.width - 1 - tx
        if sub.flip_v:
            ty = tile.height - 1 - ty
        if tile.get(tx, ty):
            return at, index
    return covering


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
    # It is handed ``ctx`` as well as the cells, because where the frames are cut
    # is not always in the preset: a format that counts each frame keeps those
    # counts between its records, so the container is the only thing that can have
    # read them (:data:`~celpix.core.context.KEY_TILEMAP_FRAME_SIZES`).
    frames = None
    size_pair = DEFAULT_SUBSPRITE_TILES
    if hasattr(engine, "frames"):
        frames = _run(
            Stage.INTERPRET_TILEMAP,
            Pathway.TILEMAP,
            lambda: engine.frames(cells, preset.params, ctx),
        )
        size_pair = _run(
            Stage.INTERPRET_TILEMAP,
            Pathway.TILEMAP,
            lambda: engine.size_pair(preset.params),
        )
        # Bounded here as well as at the allocation itself, because *this* is the
        # call the UI can take back: a read that fails leaves the binding on what
        # it was and says why, where a render that fails has already replaced the
        # document it was drawing (``docs/design/tilemap-entry.md`` §6). The box
        # is measured over nominal 8px tiles — the real ones come from whatever
        # entry the map is bound to, and are not known until the document exists —
        # which is close enough for a limit the offsets, not the tiles, blow past.
        box = frame_bounds(frames, size_pair)
        _check_sprite_extent(
            len(frames) * box[2] * box[3],
            f"{len(frames)} frames of subsprites, {box[2]}x{box[3]} pixels each",
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
    # Where the format's rows count from. Usually a plain preset parameter — a
    # sprite's 3-bit field counts from CGRAM row 8 whatever file it came from,
    # which is a fact about the console rather than about this map.
    #
    # But a *file* can say otherwise, and two of this family do: a screen and a
    # panel each carry a colour-half and a colour-cell byte that move the base
    # per file (`scgcad-formats.md` §3.3). The container reads those and publishes
    # the answer, so it wins over the preset where it is present — 94% of panels
    # state base 1, and taking the preset's 0 draws every one of them through the
    # wrong sixteen colours.
    stated = ctx.get(KEY_TILEMAP_PALETTE_ROW_BASE)
    row_base = (
        int(stated)
        if stated is not None
        else int(preset.params.get("palette_row_base", 0) or 0)
    )
    return TilemapData(
        cells, cell_bytes, tiles, ctx, data, frames, size_pair, rows, mask, row_base
    )


def encode_cells(
    cells: list[Cell],
    preset_id: str,
    reg: Registry,
    ctx: PipelineContext | None = None,
) -> bytes:
    """``cells`` back to bytes under ``preset_id`` — the save-side half.

    Separate from a whole-document save because a tilemap edit is local: the
    caller encodes the cells that changed and splices them into the buffer
    :func:`load_tilemap_data` returned, rather than re-encoding a grid whose
    trailing partial row was never part of the file.

    ``ctx`` should be the **document's own**, as :func:`_save_tilemap` passes it:
    a container may have stated something about this file that the preset cannot
    know, and the live case is byte order — 26 corpus sprite objects come from a
    build that stores the attribute word the other way round and say so in their
    header (:data:`~celpix.core.context.KEY_TILEMAP_ENDIAN`). Encoding those
    against a fresh context would write every word back swapped. A default one is
    kept for callers with no document behind them, where the preset is the whole
    of the answer.
    """
    engine, preset = reg.engine_for(preset_id)
    ctx = PipelineContext() if ctx is None else ctx
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


@dataclass(frozen=True)
class ContainerReport:
    """What one container made of one file — the model behind Container Info.

    Three groups, in the order a reader needs them. ``fields`` is what the
    container itself says it read
    (:meth:`~celpix.plugins.base.ContainerPlugin.describe`),
    ``hints`` is what its read published for the stages after it, and ``notices``
    is anything it had to drop, assume or substitute on the way. Both hint and
    notice rows are the read's *own*, not the entry's: they come from a context
    nothing else has touched, so every row here is attributable to this container.

    ``error`` is set when the read raised. The rest of the report still stands —
    a plugin may record notices and *then* fail, and what it managed to publish
    before giving up is usually what explains why.
    """

    container_id: str
    container_name: str
    paths: tuple[str, ...]
    source_size: int
    payload_offset: int
    payload_size: int
    fields: tuple[ContainerField, ...] = ()
    hints: tuple[ContainerField, ...] = ()
    notices: tuple[Notice, ...] = ()
    error: str = ""


def inspect_container(cfg: PathwayConfig, reg: Registry) -> ContainerReport:
    """Run ``cfg``'s container read alone and report what it did with the file.

    The **container stage on its own**, on a context of its own: reshape,
    decompress and the codec are not run, and nothing here reaches the entry's
    document. That isolation is the point — the live context an entry carries has
    every later stage's contributions mixed into it, and this has to be able to
    say "the *container* published this".

    A re-read rather than a look at the loaded document, so a file that has never
    been opened (or never opened successfully) can still be inspected: reaching
    for this is most useful precisely when the entry did not come out as expected.

    Failures are reported, not raised. A missing file, an unregistered container
    and a read that threw all land in :attr:`ContainerReport.error`, because a
    popup that explains what went wrong is more use here than one that refuses to
    open.
    """
    try:
        plugin = reg.plugin(Stage.CONTAINER, cfg.container_id)
    except KeyError as exc:
        # ``args[0]`` rather than ``str``: a KeyError renders its message with the
        # quotes it was raised with, and this one is a sentence.
        return ContainerReport(
            cfg.container_id, cfg.container_id, (), 0, 0, 0, error=str(exc.args[0])
        )
    name = plugin.info.name
    paths = tuple(cfg.source.paths)
    try:
        source, files = _acquire(cfg.source)
    except OSError as exc:
        return ContainerReport(cfg.container_id, name, paths, 0, 0, 0, error=str(exc))
    ctx = PipelineContext()
    # Set as the real load sets them, before the read: a container may consult
    # either while assembling its payload. Both are the *host's* provenance, so
    # they are filtered back out of the hints below — this report is about what
    # the container contributed.
    ctx.set(KEY_SOURCE_PATH, source.path)
    ctx.set(KEY_SOURCE_FILES, files)
    error = ""
    payload = b""
    try:
        payload = plugin.read(source, ctx)
    except Exception as exc:  # noqa: BLE001 - a plugin may raise anything at all
        error = f"{type(exc).__name__}: {exc}"
    return ContainerReport(
        container_id=cfg.container_id,
        container_name=name,
        paths=paths,
        source_size=len(source.data),
        payload_offset=int(ctx.get(KEY_SOURCE_OFFSET, 0) or 0),
        payload_size=len(payload),
        fields=_described_fields(plugin, source, ctx),
        hints=_hint_fields(ctx),
        notices=notices(ctx),
        error=error,
    )


def _described_fields(
    plugin: object, source: ReadSource, ctx: PipelineContext
) -> tuple[ContainerField, ...]:
    """``plugin.describe(...)``, or ``()`` — the method is optional and untrusted.

    Reached by ``getattr`` for the reason every optional plugin method is: a
    container written before it existed, or one with nothing to report, is not
    missing anything. A plugin that raises here (or hands back something that
    isn't a field) loses its rows rather than the popup: the read has already
    succeeded by this point, and a display-only method must not be able to
    retract that.
    """
    describe = getattr(plugin, "describe", None)
    if not callable(describe):
        return ()
    try:
        return tuple(f for f in describe(source, ctx) if isinstance(f, ContainerField))
    except Exception:  # noqa: BLE001 - see the docstring
        return ()


# What the host put on the context itself, which is not this container's doing —
# and the notices, which the report carries whole rather than as name/value rows.
_HOST_KEYS = frozenset({KEY_SOURCE_PATH, KEY_SOURCE_FILES, KEY_NOTICES})


def _hint_fields(ctx: PipelineContext) -> tuple[ContainerField, ...]:
    """Everything the container published, as labelled rows.

    Enumerated off the context rather than asked for, so a **plugin's own** key
    shows up too — labelled with the bare key, which is still evidence that
    something was published and something downstream may be reading it.
    """
    rows = []
    for key, value in sorted(ctx.items().items()):
        if key in _HOST_KEYS:
            continue
        label, detail = hint_info(key)
        # The key itself in the tooltip: it is what a plugin author reads the
        # value with, and the only identifier a hint nobody has labelled has.
        detail = f"{detail}\n\nContext key: {key}" if detail else f"Context key: {key}"
        rows.append(ContainerField(label, _hint_value(key, value), detail))
    return tuple(rows)


def _hint_value(key: str, value: object) -> str:
    """A context value as one short line.

    An offset is quoted in hex, as every address in the app is — the one place a
    key's *meaning* changes how its value reads. Three shapes then read badly
    under ``str``: a side table is interesting for its size rather than its
    contents, a pair of ints is always a width and a height, and a bool is a
    yes/no answer rather than Python.
    """
    if key == KEY_SOURCE_OFFSET and isinstance(value, int):
        return format_hex(value)
    if isinstance(value, (bytes, bytearray)):
        return f"{len(value)} bytes"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if (
        isinstance(value, tuple)
        and len(value) == 2
        and all(isinstance(part, int) for part in value)
    ):
        return f"{value[0]} x {value[1]}"
    if key == KEY_TILEMAP_ANIMATIONS and isinstance(value, tuple):
        live = [sequence for sequence in value if sequence]
        steps = sum(len(sequence.steps) for sequence in live)
        return f"{len(live)} of {len(value)} sequences, {steps} steps"
    # A backstop for anything else that is big: this lands in one cell of a table
    # whose first column sizes to its contents, so a value that reprs to
    # kilobytes does not just read badly, it drags every other row's layout with
    # it. Better a truncated answer than a window shaped by one of them.
    text = str(value)
    return f"{text[:57]}..." if len(text) > 60 else text


def palette_entry_size(preset_id: str, reg: Registry) -> int:
    """Byte size of one palette **read unit** — the stride palette windows step by.

    One entry for nearly every format. The handheld grayscale registers pack
    several into a unit, so pair this with :func:`palette_entries_per_unit`
    whenever converting between a colour count and a byte length —
    :func:`palette_read_bytes` and :func:`palette_entry_capacity` are that
    conversion.
    """
    engine, preset = reg.engine_for(preset_id)
    return _run(
        Stage.INTERPRET_PALETTE,
        Pathway.PALETTE,
        lambda: engine.bytes_per_entry(preset.params),
    )


def palette_entries_per_unit(preset_id: str, reg: Registry) -> int:
    """How many palette entries one read unit holds — 1 unless they are packed.

    Optional on the codec surface, so a colour codec that predates packed
    entries (or a third-party one that never needed them) answers 1 by omission.
    """
    engine, preset = reg.engine_for(preset_id)
    per_unit = getattr(engine, "entries_per_unit", None)
    if per_unit is None:
        return 1
    return max(
        1,
        _run(
            Stage.INTERPRET_PALETTE,
            Pathway.PALETTE,
            lambda: per_unit(preset.params),
        ),
    )


def palette_read_bytes(count: int, preset_id: str, reg: Registry) -> int:
    """Bytes a read window needs to hold ``count`` palette entries.

    Rounded up to a whole unit: a packed format has no way to read three of the
    four shades in a Game Boy's palette byte.
    """
    per_unit = palette_entries_per_unit(preset_id, reg)
    units = (max(0, count) + per_unit - 1) // per_unit
    return units * palette_entry_size(preset_id, reg)


def palette_entry_capacity(nbytes: int, preset_id: str, reg: Registry) -> int:
    """How many whole palette entries fit in ``nbytes`` — floored to whole units.

    The inverse of :func:`palette_read_bytes`. Flooring is what keeps a window
    off a partial trailing unit, which the colour codecs reject.
    """
    size = palette_entry_size(preset_id, reg)
    if size <= 0:
        return 0
    return (max(0, nbytes) // size) * palette_entries_per_unit(preset_id, reg)


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
    per_unit = getattr(engine, "entries_per_unit", None)
    step = max(1, per_unit(preset.params)) if per_unit is not None else 1
    out = bytearray(original)
    for index in doc.palette_edits:
        # The unit the entry lives in, not the entry: a Game Boy shade shares its
        # byte with three others, so the byte is the smallest thing that can be
        # put back. Its neighbours re-encode to what they already were.
        start = (index // step) * size
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
