"""Self-contained code formats — a preset's behaviour written directly.

A *format* is what a preset describes (one selectable pixel, palette or tilemap
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
from celpix.core.tilemap import Cell
from celpix.plugins.base import PluginInfo, Preset


@dataclass(frozen=True)
class FormatInfo:
    """A format's identity: ``id`` doubles as engine id and preset id.

    ``category`` is the picker heading it files under, exactly as a preset's is
    (:data:`~celpix.plugins.base.CATEGORIES`) — a code format is one more entry
    in the same list and should not have to sit outside the groups.
    """

    id: str
    name: str
    category: str = ""


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
    """A palette interpretation implemented directly: bytes ⇄ a palette.

    May also define ``entries_per_unit()`` — the same optional method
    :class:`~celpix.plugins.base.ColorCodecPlugin` carries, minus ``params``. A
    format that packs several entries into one read unit (a handheld shade
    register) has to declare it or be read one entry per unit.
    """

    info: FormatInfo

    def decode(self, data: bytes, ctx: PipelineContext) -> Palette: ...

    def encode(self, palette: Palette, ctx: PipelineContext) -> bytes: ...

    def bytes_per_entry(self) -> int: ...


@runtime_checkable
class TilemapFormat(Protocol):
    """A cell interpretation implemented directly: bytes ⇄ a list of cells.

    Buffer-relative and stateless like the other two, and for the same reason: a
    map is a long run of fixed-width cells, so a window of it decodes on its own.
    Laying the flat list out as a grid is the host's job — a tilemap file rarely
    states its own width.

    May also define the four optional methods
    :class:`~celpix.plugins.base.TilemapCodecPlugin` carries, each minus
    ``params``: ``transform_cell(cell, op)``, ``index_limit()``,
    ``palette_row_limit()`` and ``has_palette_rows()``. **A format that wants its
    cells edited has to define ``index_limit``** — the host refuses what a codec
    has not been asked about, so omitting it leaves the cell reference unsettable
    and every flip refused, exactly as it would for a full plugin that stayed
    quiet (see each method on :class:`~celpix.plugins.base.TilemapCodecPlugin`
    for why silence is the safe direction).
    """

    info: FormatInfo

    def decode(self, data: bytes, ctx: PipelineContext) -> list[Cell]: ...

    def encode(self, cells: list[Cell], ctx: PipelineContext) -> bytes: ...

    def bytes_per_cell(self) -> int: ...

    def cell_tiles(self) -> tuple[int, int]: ...


def _params_last(impl: Any) -> Any:
    """A format's method on a codec surface that passes ``params`` last."""

    def call(*args: Any) -> Any:
        return impl(*args[:-1])

    return call


def _params_middle(impl: Any) -> Any:
    """The same for ``frames``, whose ``params`` sits between cells and context."""

    def call(cells: Any, params: dict[str, Any], ctx: PipelineContext) -> Any:
        return impl(cells, ctx)

    return call


# The optional codec methods, per stage, and where each puts ``params``. A format
# writes the same method without it — a format *is* its own parameterisation — so
# adapting one is dropping that argument from wherever the surface carries it.
_OPTIONAL: dict[Stage, dict[str, Any]] = {
    Stage.INTERPRET_PIXEL: {},
    Stage.INTERPRET_PALETTE: {"entries_per_unit": _params_last},
    Stage.INTERPRET_TILEMAP: {
        "transform_cell": _params_last,
        "index_limit": _params_last,
        "palette_row_limit": _params_last,
        "has_palette_rows": _params_last,
        "size_pair": _params_last,
        "frames": _params_middle,
    },
}


def _forward_optional(engine: Any, fmt: Any, stage: Stage) -> None:
    """Bind the optional methods ``fmt`` defines onto ``engine``, and no others.

    The host reaches every optional method with ``getattr``/``hasattr`` on the
    **engine** — a flip asks ``transform_cell``, the cell spin asks
    ``index_limit``, a packed palette asks ``entries_per_unit``. Declaring them on
    the class would make the engine answer for a format that never wrote one, and
    each of those absences is load-bearing: silence is how a codec says "do not
    infer where my index field is" (see
    :class:`~celpix.plugins.base.TilemapCodecPlugin`). Binding per instance keeps
    absence meaning absence in both directions.
    """
    for name, adapt in _OPTIONAL[stage].items():
        impl = getattr(fmt, name, None)
        if callable(impl):
            setattr(engine, name, adapt(impl))


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


class _TilemapFormatEngine:
    """Presents a :class:`TilemapFormat` on the ``TilemapCodecPlugin`` surface."""

    def __init__(self, fmt: TilemapFormat) -> None:
        self._fmt = fmt
        self.info = PluginInfo(fmt.info.id, fmt.info.name, Stage.INTERPRET_TILEMAP)

    def decode(
        self, data: bytes, params: dict[str, Any], ctx: PipelineContext
    ) -> list[Cell]:
        return self._fmt.decode(data, ctx)

    def encode(
        self, cells: list[Cell], params: dict[str, Any], ctx: PipelineContext
    ) -> bytes:
        return self._fmt.encode(cells, ctx)

    def bytes_per_cell(self, params: dict[str, Any]) -> int:
        return self._fmt.bytes_per_cell()

    def cell_tiles(self, params: dict[str, Any]) -> tuple[int, int]:
        return self._fmt.cell_tiles()


_ENGINES = {
    Stage.INTERPRET_PIXEL: _PixelFormatEngine,
    Stage.INTERPRET_PALETTE: _PaletteFormatEngine,
    Stage.INTERPRET_TILEMAP: _TilemapFormatEngine,
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
    _forward_optional(engine, fmt, stage)
    preset = Preset(
        id=fmt.info.id,
        name=fmt.info.name,
        stage=stage,
        engine_id=fmt.info.id,
        params={},
        category=fmt.info.category,
    )
    return engine, preset
