"""The plugin contract.

Every pipeline stage is an extension point, and every concrete behaviour — even
the built-ins — is a plugin on this API; there is no privileged "core" path (see
``docs/design/overview.md`` §3). Plugins are kept **thin**: the host owns the
machinery (the pipeline, the model, the registry, file/context plumbing) and a
plugin describes only what is unique about it.

Two extensibility tiers live behind these protocols:

- **Data-first** — a *preset* (:class:`Preset`) is a parameter set a generic
  engine interprets; shipping a new planar format or colour format is data, not
  code. The engine is a :class:`PixelCodecPlugin` / :class:`ColorCodecPlugin`.
- **Code** — the escape hatch for behaviour data can't express (a decompressor, a
  bespoke reader) is a plugin class implementing the relevant protocol.

Stages import Qt nowhere; these run headless.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from celpix.core.context import PipelineContext
from celpix.core.errors import Stage
from celpix.core.index_grid import IndexGrid
from celpix.core.palette import Palette


@dataclass(frozen=True)
class FileRef:
    """A read source / write destination on disk.

    ``offset`` is where the meaningful bytes begin (e.g. past a ROM header);
    ``length`` optionally bounds them (``None`` = to end of file). Generalising to
    non-file sources (emulator memory) later widens this type — see the MVP plan's
    open questions.
    """

    path: str
    offset: int = 0
    length: int | None = None


@dataclass(frozen=True)
class PluginInfo:
    """A plugin's identity. ``id`` is stable and namespaced by stage."""

    id: str
    name: str
    stage: Stage


@runtime_checkable
class Plugin(Protocol):
    """Common to every plugin: it carries its :class:`PluginInfo`."""

    info: PluginInfo


class ReadPlugin(Plugin, Protocol):
    """Acquire raw bytes from a source, recording provenance into ``ctx``."""

    def read(self, source: FileRef, ctx: PipelineContext) -> bytes: ...


class DecompressPlugin(Plugin, Protocol):
    """Turn compressed bytes into raw bytes. Pass-through when uncompressed."""

    def decompress(self, data: bytes, ctx: PipelineContext) -> bytes: ...


class CompressPlugin(Plugin, Protocol):
    """Mirror of :class:`DecompressPlugin`; may be absent for view-only formats."""

    def compress(self, data: bytes, ctx: PipelineContext) -> bytes: ...


class WritePlugin(Plugin, Protocol):
    """Write final bytes back to a destination."""

    def write(self, data: bytes, dest: FileRef, ctx: PipelineContext) -> None: ...


class PixelCodecPlugin(Plugin, Protocol):
    """The pixel-side view interpretation: bytes ⇄ a list of tiles.

    ``params`` is a preset's parameter set (bpp, tile size, plane offsets, …). The
    engine walks the whole buffer, decoding/encoding one tile at a time.
    """

    def decode(
        self, data: bytes, params: dict[str, Any], ctx: PipelineContext
    ) -> list[IndexGrid]: ...

    def encode(
        self, tiles: list[IndexGrid], params: dict[str, Any], ctx: PipelineContext
    ) -> bytes: ...


class ColorCodecPlugin(Plugin, Protocol):
    """The palette-side view interpretation: bytes ⇄ a :class:`Palette`."""

    def decode(
        self, data: bytes, params: dict[str, Any], ctx: PipelineContext
    ) -> Palette: ...

    def encode(
        self, palette: Palette, params: dict[str, Any], ctx: PipelineContext
    ) -> bytes: ...


@dataclass(frozen=True)
class Preset:
    """A named, data-only interpretation: which engine to use and its parameters.

    This is the concrete form of "plugins as mostly data" for the View stage. A
    preset targets an ``engine_id`` (a registered pixel or colour codec) and
    supplies the ``params`` that engine interprets. ``pathway`` records whether it
    interprets pixel or palette bytes.
    """

    id: str
    name: str
    stage: Stage  # INTERPRET_PIXEL or INTERPRET_PALETTE
    engine_id: str
    params: dict[str, Any] = field(default_factory=dict)
