"""Self-contained code formats — a preset's behaviour written directly.

A *format* is what a preset describes (one selectable pixel or palette
interpretation) implemented in code rather than as engine parameters, for
interpretations no engine's parameters can express. One class and one
``registry.register_format(...)`` call put it in the format picker like any
preset, with no companion preset to author.

:func:`adapt_format` folds it into the existing machinery rather than teaching
the pipeline a new tier: the format becomes a codec engine (ignoring the
``params`` every codec method carries — a format *is* its own parameterisation)
plus an empty-params :class:`Preset` naming that engine. Preset → engine
resolution and the UI's preset listing then work unchanged. Qt-free.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from celpix.core.context import PipelineContext
from celpix.core.errors import Stage
from celpix.core.index_grid import IndexGrid
from celpix.core.palette import Palette
from celpix.plugins.base import PluginInfo, Preset


@dataclass(frozen=True)
class FormatInfo:
    """A format's identity: ``id`` doubles as engine id and preset id."""

    id: str
    name: str


@runtime_checkable
class PixelFormat(Protocol):
    """A pixel interpretation implemented directly: bytes ⇄ tiles.

    :class:`~celpix.plugins.base.PixelCodecPlugin`'s contract minus ``params``.
    Decode and encode stay buffer-relative and stateless, so windowed decoding of
    a large file keeps working.
    """

    info: FormatInfo

    def decode(self, data: bytes, ctx: PipelineContext) -> list[IndexGrid]: ...

    def encode(self, tiles: list[IndexGrid], ctx: PipelineContext) -> bytes: ...

    def bytes_per_tile(self) -> int: ...

    def tile_size(self) -> tuple[int, int]: ...


@runtime_checkable
class PaletteFormat(Protocol):
    """A palette interpretation implemented directly: bytes ⇄ a palette."""

    info: FormatInfo

    def decode(self, data: bytes, ctx: PipelineContext) -> Palette: ...

    def encode(self, palette: Palette, ctx: PipelineContext) -> bytes: ...

    def bytes_per_entry(self) -> int: ...


class _PixelFormatEngine:
    """Presents a :class:`PixelFormat` on the ``PixelCodecPlugin`` surface."""

    def __init__(self, fmt: PixelFormat) -> None:
        self._fmt = fmt
        self.info = PluginInfo(fmt.info.id, fmt.info.name, Stage.INTERPRET_PIXEL)

    def decode(
        self, data: bytes, params: dict[str, Any], ctx: PipelineContext
    ) -> list[IndexGrid]:
        return self._fmt.decode(data, ctx)

    def encode(
        self, tiles: list[IndexGrid], params: dict[str, Any], ctx: PipelineContext
    ) -> bytes:
        return self._fmt.encode(tiles, ctx)

    def bytes_per_tile(self, params: dict[str, Any]) -> int:
        return self._fmt.bytes_per_tile()

    def tile_size(self, params: dict[str, Any]) -> tuple[int, int]:
        return self._fmt.tile_size()


class _PaletteFormatEngine:
    """Presents a :class:`PaletteFormat` on the ``ColorCodecPlugin`` surface."""

    def __init__(self, fmt: PaletteFormat) -> None:
        self._fmt = fmt
        self.info = PluginInfo(fmt.info.id, fmt.info.name, Stage.INTERPRET_PALETTE)

    def decode(
        self, data: bytes, params: dict[str, Any], ctx: PipelineContext
    ) -> Palette:
        return self._fmt.decode(data, ctx)

    def encode(
        self, palette: Palette, params: dict[str, Any], ctx: PipelineContext
    ) -> bytes:
        return self._fmt.encode(palette, ctx)

    def bytes_per_entry(self, params: dict[str, Any]) -> int:
        return self._fmt.bytes_per_entry()


_ENGINES = {
    Stage.INTERPRET_PIXEL: _PixelFormatEngine,
    Stage.INTERPRET_PALETTE: _PaletteFormatEngine,
}


def adapt_format(fmt: Any, stage: Stage) -> tuple[Any, Preset]:
    """Wrap a format as ``(engine, implicit preset)`` for registration.

    Sharing one id between the engine and the preset is safe: the registry keys
    plugins by ``(stage, id)`` and presets by ``id`` in separate spaces.
    """
    try:
        engine_cls = _ENGINES[stage]
    except KeyError:
        raise ValueError(
            f"formats exist only for interpret stages, not {stage.value}"
        ) from None
    engine = engine_cls(fmt)
    preset = Preset(
        id=fmt.info.id,
        name=fmt.info.name,
        stage=stage,
        engine_id=fmt.info.id,
        params={},
    )
    return engine, preset
