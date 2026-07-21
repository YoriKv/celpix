"""The strictly linear pipeline: run both pathways for load and for save.

Load runs each pathway forward — Read -> Decompress -> interpret — and converges
the results into a :class:`Document`. Save mirrors it — interpret.encode ->
Compress -> Write — per pathway, with palette Write optional. Any stage that
cannot proceed raises :class:`PipelineError`, which halts the pipeline and names
the stage + pathway + reason; nothing partial is written
(``docs/design/overview.md`` §2).
"""

from __future__ import annotations

from typing import Callable, TypeVar

from celpix.core.context import PipelineContext
from celpix.core.document import Document
from celpix.core.errors import Pathway, PipelineError, Stage
from celpix.core.index_grid import IndexGrid
from celpix.core.palette import Palette
from celpix.pipeline.pathway import PathwayConfig
from celpix.plugins.registry import Registry

T = TypeVar("T")


def _run(stage: Stage, pathway: Pathway, fn: Callable[[], T]) -> T:
    """Run one stage, translating any failure into a hard-stop PipelineError."""
    try:
        return fn()
    except PipelineError:
        raise
    except Exception as exc:  # noqa: BLE001 — deliberately funnel every failure
        raise PipelineError(stage, pathway, str(exc)) from exc


def load_pixel(
    cfg: PathwayConfig, reg: Registry
) -> tuple[list[IndexGrid], PipelineContext]:
    """Run the pixel pathway forward: Read -> Decompress -> decode to tiles."""
    ctx = PipelineContext()
    data = _read_and_decompress(cfg, ctx, reg, Pathway.PIXEL)
    preset = reg.preset(cfg.interpret_preset_id)
    tiles = _run(
        Stage.INTERPRET_PIXEL,
        Pathway.PIXEL,
        lambda: reg.plugin(Stage.INTERPRET_PIXEL, preset.engine_id).decode(
            data, preset.params, ctx
        ),
    )
    return tiles, ctx


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
    """Read + decompress + interpret both pathways into a Document."""
    tiles, pixel_ctx = load_pixel(pixel, reg)
    colors, palette_ctx = load_palette(palette, reg)
    return Document(
        pixel_tiles=tiles,
        palette=colors,
        pixel_config=pixel,
        palette_config=palette,
        pixel_ctx=pixel_ctx,
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
    cfg = doc.pixel_config
    preset = reg.preset(cfg.interpret_preset_id)
    data = _run(
        Stage.INTERPRET_PIXEL,
        Pathway.PIXEL,
        lambda: reg.plugin(Stage.INTERPRET_PIXEL, preset.engine_id).encode(
            doc.pixel_tiles, preset.params, doc.pixel_ctx
        ),
    )
    _compress_and_write(cfg, data, doc.pixel_ctx, reg, Pathway.PIXEL)


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
