"""The strictly linear pipeline: run both pathways for load and for save.

Load runs each pathway forward — Read -> Decompress -> interpret — and converges
the results into a :class:`Document`. Save mirrors it — interpret.encode ->
Compress -> Write — per pathway, with palette Write optional. Any stage that
cannot proceed raises :class:`PipelineError`, which halts the pipeline and names
the stage + pathway + reason; nothing partial is written
(``docs/design/overview.md`` §2).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, NamedTuple, TypeVar

from celpix.core import ceil_div
from celpix.core.arrangement import BlockLayout, compose_window, reflow_2d
from celpix.core.context import PipelineContext
from celpix.core.document import Document
from celpix.core.errors import Pathway, PipelineError, Stage
from celpix.core.index_grid import IndexGrid
from celpix.core.palette import Palette
from celpix.pipeline.pathway import PathwayConfig
from celpix.plugins.base import DecompressPlugin, FileRef
from celpix.plugins.registry import Registry

T = TypeVar("T")


@dataclass(frozen=True)
class ScanResult:
    """Where a forward structure scan ended (:func:`find_next_structure`).

    ``found`` is the hit offset or ``None``; ``end`` is the last offset examined
    (where the caller lands when there was no hit); ``stopped`` is True when the
    caller aborted the scan via its tick callback rather than reaching the end.
    """

    found: int | None
    end: int
    stopped: bool


def find_next_structure(
    data: bytes,
    plugin: DecompressPlugin,
    window_len: int,
    start: int,
    *,
    progress_every: int = 64,
    on_tick: Callable[[int], bool] | None = None,
) -> ScanResult:
    """The first offset ≥ ``start`` where ``plugin`` decodes a complete structure.

    Walks ``data`` one byte at a time, trying a strict decompress of the
    ``window_len``-byte window at each offset; a non-empty result is a hit. This
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
            if plugin.decompress(data[pos : pos + window_len], PipelineContext()):
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


def _run(stage: Stage, pathway: Pathway, fn: Callable[[], T]) -> T:
    """Run one stage, translating any failure into a hard-stop PipelineError."""
    try:
        return fn()
    except PipelineError:
        raise
    except Exception as exc:  # noqa: BLE001 — deliberately funnel every failure
        raise PipelineError(stage, pathway, str(exc)) from exc


def load_pixel_data(cfg: PathwayConfig, reg: Registry) -> PixelData:
    """Run the pixel pathway forward through Decompress, *without* decoding.

    Returns the raw decompressed bytes plus the codec's atomic geometry, so the view
    can decode only the visible window on demand (:func:`decode_window`) rather than
    the whole file. Data whose length isn't a whole number of tiles is fine — the
    trailing partial tile is zero-padded at decode time (``Document.window_bytes``).
    """
    ctx = PipelineContext()
    data = _read_and_decompress(cfg, ctx, reg, Pathway.PIXEL)
    return PixelData(data, *_pixel_geometry(cfg, reg), ctx)


def reinterpret_pixel_data(
    data: bytes, ctx: PipelineContext, cfg: PathwayConfig, reg: Registry
) -> PixelData:
    """Already-loaded bytes under ``cfg``'s Interpret preset — nothing re-read.

    Only the codec's geometry depends on the interpret preset; which bytes there
    are comes out of Read + Decompress, which this leaves alone. That is what
    makes switching formats non-destructive: unsaved edits live *in* these bytes,
    and re-running the pathway would pull the file's own bytes back over them.
    Raises the same :class:`PipelineError` a load would for an unusable preset.
    """
    return PixelData(data, *_pixel_geometry(cfg, reg), ctx)


def _pixel_geometry(cfg: PathwayConfig, reg: Registry) -> tuple[int, int, int]:
    """``(bytes_per_tile, tile_width, tile_height)`` of ``cfg``'s pixel codec."""
    engine, preset = reg.engine_for(cfg.interpret_preset_id)
    tile_bytes = _run(
        Stage.INTERPRET_PIXEL,
        Pathway.PIXEL,
        lambda: engine.bytes_per_tile(preset.params),
    )
    if tile_bytes <= 0:
        raise PipelineError(
            Stage.INTERPRET_PIXEL,
            Pathway.PIXEL,
            f"tile size {tile_bytes} is not positive",
        )
    return (tile_bytes, *engine.tile_size(preset.params))


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
    return _run(
        Stage.INTERPRET_PIXEL,
        Pathway.PIXEL,
        lambda: engine.decode(window, preset.params, PipelineContext()),
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


def stripe_tiles(doc: Document, columns: int, two_dimensional: bool) -> int:
    """How many tiles' bytes interleave together — 1 unless the 2D walk applies.

    In wide-bitmap mode a tile's pixel-rows are strided ``columns`` tiles apart,
    so ``columns`` tiles share one interleaved byte stripe and no single tile
    owns a contiguous byte range. Guarded exactly as
    :func:`~celpix.core.arrangement.reflow_2d` guards itself: geometry with no
    whole per-row chunk has no wide-bitmap reading, so it reads as plain 1D.
    """
    tb = doc.bytes_per_tile
    if not two_dimensional or tb <= 0 or doc.tile_height <= 0 or tb % doc.tile_height:
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
    stripe = stripe_tiles(doc, columns, two_dimensional)
    if stripe > 1:
        phase = anchor % stripe
        start_tile = phase + ((first_tile - phase) // stripe) * stripe
        while start_tile < 0:  # the run sits in the frame's leading partial stripe
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
    if stripe_tiles(doc, columns, two_dimensional) == 1:
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
    blob = _run(
        Stage.INTERPRET_PIXEL,
        Pathway.PIXEL,
        lambda: engine.encode(tiles, preset.params, PipelineContext()),
    )
    stripe = stripe_tiles(doc, columns, two_dimensional)
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
        _scatter_2d(out, slot, blob[i * tb : (i + 1) * tb], tb, doc.tile_height, stripe)
    return region.start, bytes(out)


def _scatter_2d(
    out: bytearray,
    slot: int,
    data: bytes,
    bytes_per_tile: int,
    tile_height: int,
    columns: int,
) -> None:
    """Write one tile's contiguous bytes back into wide-bitmap (2D) order.

    The exact inverse of the gather :func:`~celpix.core.arrangement.reflow_2d`
    performs, for a single tile: its pixel-rows go back to their strided homes
    ``columns`` tiles apart. Writes that fall past the buffer (a region clamped
    at end-of-data) are clipped, never grown.
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


def decode_and_compose(
    pixel_bytes: bytes,
    engine,  # noqa: ANN001 — a pixel-interpret plugin
    params,  # noqa: ANN001 — the preset's engine params
    layout: BlockLayout,
    two_dimensional: bool,
    max_rows: int | None,
):
    """Decode a pixel-byte buffer and lay the tiles out through an arrangement.

    The Qt-free core shared by the live view, the decompression overlay, and
    export: 2D reflow → decode → block layout → compose. ``pixel_bytes`` begins
    at whatever origin the caller wants (a window of the doc's bytes for the live
    view, the whole file for export, a decompressed scratch for the overlay).
    ``max_rows`` caps the composed height (the live view's fixed window); ``None``
    sizes to the data (export and the overlay show every tile). Returns
    ``(grid, filled)`` — an index or direct-color grid, and the count of real
    tiles (excluding any 2D-reflow / partial-tile padding) so a caller can
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
    # Rows the tiles occupy under this layout (plain: ceil; blocked: the tallest
    # cell). Capped to a fixed window for the live view; uncapped otherwise.
    need_rows = (
        1 + max(layout.slot_to_cell(s)[1] for s in range(len(tiles))) if tiles else 1
    )
    canvas_rows = need_rows if max_rows is None else max(1, min(max_rows, need_rows))
    if layout.is_plain:
        # Narrow a single partial row to its tiles; a taller image keeps full
        # width and lets the caller background the trailing slots.
        shown_cols = cols if need_rows > 1 else min(cols, max(1, len(tiles)))
        grid = compose_window(tiles, shown_cols, 0, canvas_rows)
    else:
        grid = compose_window(tiles, cols, 0, canvas_rows, layout)
    return grid, filled


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
    data = _read_and_decompress(cfg, ctx, reg, Pathway.PALETTE)
    engine, preset = reg.engine_for(cfg.interpret_preset_id)
    colors = _run(
        Stage.INTERPRET_PALETTE,
        Pathway.PALETTE,
        lambda: engine.decode(data, preset.params, ctx),
    )
    return PaletteData(colors, ctx, data)


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
    engine, preset = reg.engine_for(preset_id)

    def _round_trip() -> int:
        ctx = PipelineContext()
        data = engine.encode(Palette([argb]), preset.params, ctx)
        decoded = engine.decode(data, preset.params, ctx)
        return decoded.color(0)

    return _run(Stage.INTERPRET_PALETTE, Pathway.PALETTE, _round_trip)


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
        return bool(tiles) and getattr(tiles[0], "bytes_per_pixel", 1) == 4

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
        palette_bytes=pal.data,
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
    """
    if pixel and doc.pixel_config.write_enabled:
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
    _run(
        Stage.WRITE,
        Pathway.PALETTE,
        lambda: reg.plugin(Stage.WRITE, "write.raw-file").write(
            data, FileRef(path), doc.palette_ctx
        ),
    )


def _read_and_decompress(
    cfg: PathwayConfig, ctx: PipelineContext, reg: Registry, pathway: Pathway
) -> bytes:
    raw = _run(
        Stage.READ,
        pathway,
        lambda: reg.plugin(Stage.READ, cfg.read_id).read(cfg.source, ctx),
    )
    return _run(
        Stage.DECOMPRESS,
        pathway,
        lambda: reg.plugin(Stage.DECOMPRESS, cfg.decompress_id).decompress(raw, ctx),
    )


def _save_pixel(doc: Document, reg: Registry) -> None:
    # The decompressed pixel bytes are the source of truth: edits are already
    # spliced into them (encode_tiles -> Document.replace_bytes), so saving is just
    # compress + write of the buffer as it stands. Writing the bytes is exactly
    # equivalent to encode(decode(bytes)) for these codecs, and avoids decoding the
    # whole file just to save it. Note that a real compressor may make different
    # encoding choices than the original stream, so writing a *compressed* pathway
    # can rewrite equivalent-but-different bytes inside the slot even where nothing
    # was edited — harmless, and rare now that dirty tracking gates Write All.
    _compress_and_write(
        doc.pixel_config, doc.pixel_data, doc.pixel_ctx, reg, Pathway.PIXEL
    )


def _save_palette(doc: Document, reg: Registry) -> None:
    """Encode + write the palette, **splicing** only the entries that changed.

    A color codec round-trips ARGB faithfully but not *bytes*: anything outside
    its masks is dropped, and an indexed codec has no inverse at all. So writing
    a full re-encode would rewrite — and corrupt — entries the user never
    touched. Instead the freshly encoded bytes of edited entries are spliced
    into the buffer the palette was read from, leaving every other byte exactly
    as it was found (see :attr:`Document.palette_bytes`).

    Falls back to a whole-palette encode when there is nothing to splice into —
    no original bytes, or a palette whose length no longer matches them (a
    format switch changes the entry size, so the old buffer doesn't apply).
    """
    cfg = doc.palette_config
    engine, preset = reg.engine_for(cfg.interpret_preset_id)
    encoded = _run(
        Stage.INTERPRET_PALETTE,
        Pathway.PALETTE,
        lambda: engine.encode(doc.palette, preset.params, doc.palette_ctx),
    )
    data = _splice_palette(doc, encoded, engine, preset)
    _compress_and_write(cfg, data, doc.palette_ctx, reg, Pathway.PALETTE)
    # The file now holds these bytes, so they become the baseline for the next
    # splice and no entry is outstanding. Skipping this would make a second save
    # splice against pre-save bytes and undo the first one's edits.
    doc.palette_bytes = data
    doc.palette_edits = set()


def _splice_palette(doc: Document, encoded: bytes, engine, preset) -> bytes:  # noqa: ANN001
    original = doc.palette_bytes
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


def _compress_and_write(
    cfg: PathwayConfig,
    data: bytes,
    ctx: PipelineContext,
    reg: Registry,
    pathway: Pathway,
) -> None:
    """Compress ``data`` and write it to the config's target.

    A bounded target (``length`` set — a slice of a larger file) is a hard slot:
    a result that would overflow it raises before anything touches the file. A
    result *smaller* than the slot is written short, leaving the slot's tail
    bytes as they were — every supported scheme is self-delimiting, so the stale
    tail is inert, and not rewriting it keeps the file diff minimal.
    """
    packed = _run(
        Stage.COMPRESS,
        pathway,
        lambda: reg.plugin(Stage.COMPRESS, cfg.compress_id).compress(data, ctx),
    )
    target = cfg.write_target()
    if target.length is not None and len(packed) > target.length:
        raise PipelineError(
            Stage.WRITE,
            pathway,
            f"result ({len(packed)} bytes) exceeds the {target.length}-byte slot "
            f"at {target.offset:#x} in {target.path}",
        )
    _run(
        Stage.WRITE,
        pathway,
        lambda: reg.plugin(Stage.WRITE, cfg.write_id).write(packed, target, ctx),
    )
