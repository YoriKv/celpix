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
from celpix.core.arrangement import reflow_2d
from celpix.core.context import PipelineContext
from celpix.core.document import Document
from celpix.core.errors import Pathway, PipelineError, Stage
from celpix.core.index_grid import IndexGrid
from celpix.core.palette import Palette
from celpix.pipeline.pathway import PathwayConfig
from celpix.plugins.base import DecompressPlugin
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
    tw, th = engine.tile_size(preset.params)
    return PixelData(data, tile_bytes, tw, th, ctx)


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


def load_palette(cfg: PathwayConfig, reg: Registry) -> tuple[Palette, PipelineContext]:
    """Run the palette pathway forward: Read -> Decompress -> decode to a Palette."""
    ctx = PipelineContext()
    data = _read_and_decompress(cfg, ctx, reg, Pathway.PALETTE)
    engine, preset = reg.engine_for(cfg.interpret_preset_id)
    colors = _run(
        Stage.INTERPRET_PALETTE,
        Pathway.PALETTE,
        lambda: engine.decode(data, preset.params, ctx),
    )
    return colors, ctx


def palette_entry_size(preset_id: str, reg: Registry) -> int:
    """Byte size of one palette entry under the preset — for sizing palette reads."""
    engine, preset = reg.engine_for(preset_id)
    return _run(
        Stage.INTERPRET_PALETTE,
        Pathway.PALETTE,
        lambda: engine.bytes_per_entry(preset.params),
    )


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
    colors, palette_ctx = load_palette(palette, reg)
    return Document(
        pixel_data=px.data,
        bytes_per_tile=px.bytes_per_tile,
        tile_width=px.tile_width,
        tile_height=px.tile_height,
        palette=colors,
        pixel_config=pixel,
        palette_config=palette,
        pixel_ctx=px.ctx,
        palette_ctx=palette_ctx,
    )


def save(doc: Document, reg: Registry) -> None:
    """Encode + compress + write both pathways (each pathway's Write is optional).

    ``write_enabled=False`` on the pixel pathway marks a view-only document — e.g.
    a decompressed slice whose scheme has no compressor — and skips its write.
    """
    if doc.pixel_config.write_enabled:
        _save_pixel(doc, reg)
    if doc.palette_config.write_enabled:
        _save_palette(doc, reg)


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
    # The decompressed pixel bytes are the source of truth. With no per-tile editing
    # yet they are unchanged since load, so compress + write them straight back (an
    # edit path will re-encode changed windows into pixel_data before this). Writing
    # the bytes is exactly equivalent to encode(decode(bytes)) for these codecs, and
    # avoids decoding the whole file just to save it. Note that a real compressor
    # may make different encoding choices than the original stream, so saving even
    # untouched data can rewrite equivalent-but-different bytes inside the slot —
    # harmless, and moot once dirty tracking arrives with editing.
    _compress_and_write(
        doc.pixel_config, doc.pixel_data, doc.pixel_ctx, reg, Pathway.PIXEL
    )


def _save_palette(doc: Document, reg: Registry) -> None:
    cfg = doc.palette_config
    engine, preset = reg.engine_for(cfg.interpret_preset_id)
    data = _run(
        Stage.INTERPRET_PALETTE,
        Pathway.PALETTE,
        lambda: engine.encode(doc.palette, preset.params, doc.palette_ctx),
    )
    _compress_and_write(cfg, data, doc.palette_ctx, reg, Pathway.PALETTE)


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
