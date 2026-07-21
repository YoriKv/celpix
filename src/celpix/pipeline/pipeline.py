"""The strictly linear pipeline: run both pathways for load and for save.

Load runs each pathway forward — Read -> Decompress -> interpret — and converges
the results into a :class:`Document`. Save mirrors it — interpret.encode ->
Compress -> Write — per pathway, with palette Write optional. Any stage that
cannot proceed raises :class:`PipelineError`, which halts the pipeline and names
the stage + pathway + reason; nothing partial is written
(``docs/design/overview.md`` §2).
"""

from __future__ import annotations

from typing import Callable, NamedTuple, TypeVar

from celpix.core.context import PipelineContext
from celpix.core.document import Document
from celpix.core.errors import Pathway, PipelineError, Stage
from celpix.core.index_grid import IndexGrid
from celpix.core.palette import Palette
from celpix.pipeline.pathway import PathwayConfig
from celpix.plugins.registry import Registry

T = TypeVar("T")


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
    the whole file. The whole-buffer alignment check that ``decode`` would do is done
    here instead, so a misaligned file still hard-stops at the interpret stage.
    """
    ctx = PipelineContext()
    data = _read_and_decompress(cfg, ctx, reg, Pathway.PIXEL)
    preset = reg.preset(cfg.interpret_preset_id)
    engine = reg.plugin(Stage.INTERPRET_PIXEL, preset.engine_id)
    tile_bytes = _run(
        Stage.INTERPRET_PIXEL,
        Pathway.PIXEL,
        lambda: engine.bytes_per_tile(preset.params),
    )
    if tile_bytes <= 0 or len(data) % tile_bytes != 0:
        raise PipelineError(
            Stage.INTERPRET_PIXEL,
            Pathway.PIXEL,
            f"data length {len(data)} is not a multiple of tile size {tile_bytes}",
        )
    tw, th = engine.tile_size(preset.params)
    return PixelData(data, tile_bytes, tw, th, ctx)


def decode_window(
    doc: Document, reg: Registry, first_tile: int, count: int
) -> list[IndexGrid]:
    """Decode ``count`` tiles starting at tile ``first_tile`` — deferred decode.

    Slices the raw pixel bytes to just that window and hands the codec the slice;
    because the codec decodes exactly the tiles in the buffer it is given, no
    whole-file decode is needed. A partial/empty window (near or past the end)
    decodes to fewer/zero tiles.
    """
    window = doc.window_bytes(first_tile, count)
    if not window:
        return []
    preset = reg.preset(doc.pixel_config.interpret_preset_id)
    engine = reg.plugin(Stage.INTERPRET_PIXEL, preset.engine_id)
    return _run(
        Stage.INTERPRET_PIXEL,
        Pathway.PIXEL,
        lambda: engine.decode(window, preset.params, PipelineContext()),
    )


def load_palette(cfg: PathwayConfig, reg: Registry) -> tuple[Palette, PipelineContext]:
    """Run the palette pathway forward: Read -> Decompress -> decode to a Palette."""
    ctx = PipelineContext()
    data = _read_and_decompress(cfg, ctx, reg, Pathway.PALETTE)
    preset = reg.preset(cfg.interpret_preset_id)
    colors = _run(
        Stage.INTERPRET_PALETTE,
        Pathway.PALETTE,
        lambda: reg.plugin(Stage.INTERPRET_PALETTE, preset.engine_id).decode(
            data, preset.params, ctx
        ),
    )
    return colors, ctx


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
    """Encode + compress + write both pathways (palette Write is optional)."""
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
    # avoids decoding the whole file just to save it.
    _compress_and_write(
        doc.pixel_config, doc.pixel_data, doc.pixel_ctx, reg, Pathway.PIXEL
    )


def _save_palette(doc: Document, reg: Registry) -> None:
    cfg = doc.palette_config
    preset = reg.preset(cfg.interpret_preset_id)
    data = _run(
        Stage.INTERPRET_PALETTE,
        Pathway.PALETTE,
        lambda: reg.plugin(Stage.INTERPRET_PALETTE, preset.engine_id).encode(
            doc.palette, preset.params, doc.palette_ctx
        ),
    )
    _compress_and_write(cfg, data, doc.palette_ctx, reg, Pathway.PALETTE)


def _compress_and_write(
    cfg: PathwayConfig,
    data: bytes,
    ctx: PipelineContext,
    reg: Registry,
    pathway: Pathway,
) -> None:
    packed = _run(
        Stage.COMPRESS,
        pathway,
        lambda: reg.plugin(Stage.COMPRESS, cfg.compress_id).compress(data, ctx),
    )
    _run(
        Stage.WRITE,
        pathway,
        lambda: reg.plugin(Stage.WRITE, cfg.write_id).write(
            packed, cfg.write_target(), ctx
        ),
    )
