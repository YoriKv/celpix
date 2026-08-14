"""The pieces one pipeline stage is run out of, shared by every pathway.

A leaf module: it imports nothing from the ones that use it, which is what lets
the load/save plumbing, the renderer, the container report and the codec queries
all reach the same helpers without the package folding in on itself.

Two kinds of thing live here. :func:`_run` is how a stage is executed at all —
every failure inside one is funnelled into a
:class:`~celpix.core.errors.PipelineError` naming the stage, the direction and
the pathway, so a stage that cannot proceed halts the pipeline instead of
returning something partial. :func:`_acquire` is the host's half of the
container contract: the files behind a :class:`~celpix.plugins.base.FileRef` (or
the in-memory buffer standing in for them) are opened once, here, and joined end
to end, so every container is handed the same buffer and none has to know which
of the two it is looking at.

The rest is the tile geometry a pixel codec is asked for. Whether a codec
honours a tile size at all is probed rather than assumed
(:func:`_with_tile_size`); the view's bitmap-width override is resolved once on
load (:func:`bitmap_params`) and read back off the document afterwards
(:func:`tile_params`), so a later decode cannot cut the bytes into different
tiles than the view placed.

Depositing bytes, running a whole pathway and drawing what one produces are all
elsewhere — this module knows about a stage, not about the pipeline.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from celpix.core.arrangement import bitmap_tile_size
from celpix.core.context import PipelineContext
from celpix.core.document import Document
from celpix.core.errors import Pathway, PipelineError, Stage
from celpix.core.notices import warn
from celpix.pipeline.pathway import PathwayConfig
from celpix.plugins.base import FileRef, PixelCodecPlugin, ReadSource, SourceFile
from celpix.plugins.registry import Registry

T = TypeVar("T")


def _run(
    stage: Stage,
    pathway: Pathway,
    fn: Callable[[], T],
    action: str = "",
    *,
    plugin: str = "",
) -> T:
    """Run one stage, translating any failure into a hard-stop PipelineError.

    ``action`` is the direction within the stage (``read``/``write``,
    ``decompress``/``compress``) — a stage covers both, and which one failed is
    what the user needs to know first (:class:`PipelineError`).

    ``plugin`` is the id of whatever is running — the container, the compression
    scheme, the preset a codec resolved from. It is passed here rather than
    looked up because only the caller has it: this function is handed a thunk,
    and a thunk says nothing about whose code is inside it. A stage that names it
    is the difference between "the pixel codec failed" and knowing which of three
    installed codecs to go and look at.
    """
    try:
        return fn()
    except PipelineError:
        raise
    except Exception as exc:  # noqa: BLE001 — deliberately funnel every failure
        raise PipelineError(stage, pathway, str(exc), action, plugin=plugin) from exc


def _probe(
    engine,  # noqa: ANN001 — any stage plugin, reached by getattr
    name: str,
    params: dict,
    read: Callable[[object], T],
    default: T,
    *,
    ctx: PipelineContext,
    plugin: str = "",
) -> T:
    """Ask one **optional** codec method, so that a bad answer cannot cost the load.

    The counterpart to :func:`_run`, and the two are the whole of how the host
    calls into a plugin. ``_run`` is for the calls that *are* the result — a
    decode, an encode, a container's read — where a failure has no fallback and
    the honest end is a :class:`PipelineError` naming the plugin. This is for the
    optional half of a stage protocol, where **absence is already defined**: a
    codec that does not implement ``index_limit`` leaves its references
    unbounded, one that says nothing about ``palette_row_granularity`` keeps a
    row per cell, and one silent on ``has_palette_rows`` is taken as carrying
    them (``docs/design/plugin-system.md`` §1).

    So a method that *cannot answer* is treated as one that was never written.
    Failing the load instead would lose the picture over a piece of metadata the
    host already has a documented answer for — and it would make the policy
    depend on which method a plugin happened to get wrong. What stops that being
    silent is the notice: the fallback is recorded on the context and surfaced
    against the entry, which is how the plugin's author finds out.

    ``read`` is what turns the answer into the value the host uses, and it runs
    **inside** the guard for the same reason the call does: a method that returns
    ``2`` where a pair was asked for is exactly as broken as one that raises, and
    unpacking it outside would put the crash back where the guard was meant to be.
    """
    ask = getattr(engine, name, None)
    if ask is None:
        return default
    try:
        return read(ask(params))
    except Exception as exc:  # noqa: BLE001 — a probe must not fail the load
        warn(
            ctx,
            f"The format could not answer {name}(), so its default was used",
            f"{exc}\n"
            f"Read as if the format had not defined {name},\n"
            f"which is what a format staying quiet means.",
            source=plugin,
        )
        return default


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
    engine, preset = reg.engine_for(cfg.interpret_preset_id, PixelCodecPlugin)
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
    for path, blob in zip(ref.paths, blobs, strict=True):
        spans.append(SourceFile(path, at, len(blob)))
        at += len(blob)
    joined = b"".join(blobs)
    return ReadSource(joined, ref.path, ref.offset, ref.length), tuple(spans)
