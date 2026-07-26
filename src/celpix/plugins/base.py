"""The plugin contract.

Every pipeline stage is an extension point, and every concrete behaviour — even
the built-ins — is a plugin on this API; there is no privileged "core" path (see
``docs/design/overview.md`` §3). Plugins are kept **thin**: the host owns the
machinery (the pipeline, the model, the registry, file/context plumbing) and a
plugin describes only what is unique about it.

Two extensibility tiers live behind these protocols:

- **Data-first** — a *preset* (:class:`Preset`) is a parameter set a generic
  engine interprets; shipping a new planar format or color format is data, not
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

# The pass-through Decompress/Compress ids. These are the one pair of plugin ids
# the *host* has to know by name: "no compression" is not merely another scheme
# but the condition several behaviours key off — a raw byte stream maps linearly
# to file offsets (so addresses stay meaningful and slices can be carved from the
# view), and the overlay/scan tools only mean anything once a real decompressor is
# chosen. Named here, in the contract, rather than spelled out at each test.
NO_DECOMPRESS = "decompress.none"
NO_COMPRESS = "compress.none"

# The plain-bytes Read/Write pair — the container equivalent of the pass-through
# above, and named here for the same reason: "no container" is the fallback every
# detection lands on when nothing claims a file, so the host has to know it by
# name. A Read/Write pair is matched by the ``read.X`` ⇄ ``write.X`` id
# convention (:func:`~celpix.plugins.detect.container_write_id`), mirroring
# ``decompress.X`` ⇄ ``compress.X``.
RAW_READ = "read.raw-file"
RAW_WRITE = "write.raw-file"


@dataclass(frozen=True)
class FileRef:
    """A read source / write destination on disk.

    ``offset`` is where the meaningful bytes begin (e.g. past a ROM header);
    ``length`` optionally bounds them (``None`` = to end of file).

    ``data`` is the non-file generalisation the design anticipated (§9): when set,
    it *is* the source bytes (still sliced by ``offset``/``length``), so a reader
    yields them without touching disk. This is how a palette pulled out of an
    emulator memory image — bytes that live inside a compressed container, not at
    a file offset — flows through the ordinary pipeline, and how a slice reads a
    dirty parent's unsaved bytes instead of the stale file. ``path`` is still
    carried for provenance/display. Write destinations never set ``data``.

    ``data_base`` is the file offset ``data[0]`` corresponds to, so ``offset``
    stays **file-absolute** whether the bytes come from disk or memory. That is
    what lets an in-memory source keep one set of coordinates for reading, for the
    write target, and for the addresses the UI displays: a buffer that begins part
    way into the file (a parent read past its header) declares where it begins
    rather than forcing every consumer to work in relative offsets. Ignored when
    ``data`` is None — a file is always its own base.
    """

    path: str
    offset: int = 0
    length: int | None = None
    data: bytes | None = None
    data_base: int = 0


@dataclass(frozen=True)
class PluginInfo:
    """A plugin's identity. ``id`` is stable and namespaced by stage.

    ``self_delimiting`` is **Decompress-only** and describes the *scheme*, not any
    one decode: false means the stream carries no end marker, so its extent is
    knowable only from outside (a slice length, a container's byte count). It is
    declared here rather than recorded in the context because it is a static
    property — true before a single byte is read, and on every code path — and
    because the UI has to phrase "the decode stopped here" differently for the
    two: a scheme *with* an end marker that didn't reach one was cut short and
    can be fixed by widening the window, while one without simply decodes as far
    as it is fed and always will.

    ``extensions`` and ``magic`` are **Read-only** and together form a container's
    *signature* — what makes opening a file pick its container automatically
    instead of asking. ``extensions`` are lowercase suffixes including the dot
    (``(".nes",)``); ``magic`` is a tuple of ``(offset, bytes)`` probes, any one of
    which matching is a hit (a format with two byte orders declares both). They
    are declared rather than detected by a callback because detection has to run
    over every registered container before a file is open, and a data comparison
    can do that without executing anyone's code.

    A container that declares ``magic`` is matched **only** by it: the bytes are
    an assertion about the format, so a ``.nes`` file without ``NES\\x1a`` is not
    an iNES file whatever it is called. A container with no ``magic`` falls back
    to matching on ``extensions`` alone, which is the best some wrappers offer
    (Sega ``.smd`` carries no reliable marker).

    ``size_modulo`` (``(modulus, remainder)``) and ``min_size`` are the size
    signature, and they **narrow** a match rather than making one: a container
    that sets them claims a file only when its extension or magic already matched
    *and* the file's length agrees. They exist because some wrappers have no
    marker at all and are identifiable only by the shape of the file — a 512-byte
    copier header is spotted by the ROM being 512 bytes over a whole number of
    KiB, since carts never are.

    ``min_size`` is not a detail: a modulo rule on its own also matches files far
    too small to be what it is describing, and celPix is routinely pointed at
    those. A 512-byte ``.4bpp.sfc`` tile sheet is 512 bytes over zero KiB, so the
    copier rule would claim it and hand back nothing at all. The floor is the
    statement that the rule only means something once there is a plausible
    cartridge behind the header.

    Narrowing rather than claiming on its own is the other half of that safety: a
    size rule alone would seize any binary of the right length. Like the other
    terms these are inert data, so detection stays a comparison and never runs a
    plugin.

    ``short_name`` is a compact form of ``name`` for places that show a plugin
    *inline with other text* rather than on a row of its own — the Files list
    tags each file with its container, where the full "Game Boy ROM (checksum
    repair on write)" would bury the filename it is annotating. Empty means
    ``name`` is already short enough.

    """

    id: str
    name: str
    stage: Stage
    self_delimiting: bool = True
    extensions: tuple[str, ...] = ()
    magic: tuple[tuple[int, bytes], ...] = ()
    size_modulo: tuple[int, int] | None = None
    min_size: int = 0
    short_name: str = ""


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
    engine walks whatever buffer it is given, decoding/encoding one tile at a time —
    it is **buffer-relative and stateless**, so handing it a byte *window* (a slice
    of the file covering just the visible tiles) decodes exactly that window. That is
    what enables deferred, windowed decoding of large files without ``decode`` having
    to know the window's size or its position in the file.

    The host, however, must know a tile's **byte size** to cut that window out of the
    raw bytes; :meth:`bytes_per_tile` exposes it (a pure function of ``params``),
    keeping the codec the authority on its own atomic geometry.
    """

    def decode(
        self, data: bytes, params: dict[str, Any], ctx: PipelineContext
    ) -> list[IndexGrid]: ...

    def encode(
        self, tiles: list[IndexGrid], params: dict[str, Any], ctx: PipelineContext
    ) -> bytes: ...

    def bytes_per_tile(self, params: dict[str, Any]) -> int:
        """Byte size of one atomic tile under ``params`` (for byte-window slicing)."""
        ...

    def tile_size(self, params: dict[str, Any]) -> tuple[int, int]:
        """Pixel dimensions ``(width, height)`` of one atomic tile under ``params``."""
        ...


class ColorCodecPlugin(Plugin, Protocol):
    """The palette-side view interpretation: bytes ⇄ a :class:`Palette`."""

    def decode(
        self, data: bytes, params: dict[str, Any], ctx: PipelineContext
    ) -> Palette: ...

    def encode(
        self, palette: Palette, params: dict[str, Any], ctx: PipelineContext
    ) -> bytes: ...

    def bytes_per_entry(self, params: dict[str, Any]) -> int:
        """Byte size of one palette entry under ``params`` — the palette-side
        mirror of :meth:`PixelCodecPlugin.bytes_per_tile`, so the host can size
        a byte window for a wanted number of entries."""
        ...


@dataclass(frozen=True)
class Preset:
    """A named, data-only interpretation: which engine to use and its parameters.

    This is the concrete form of "plugins as mostly data" for the View stage. A
    preset targets an ``engine_id`` (a registered pixel or color codec) and
    supplies the ``params`` that engine interprets. ``pathway`` records whether it
    interprets pixel or palette bytes.
    """

    id: str
    name: str
    stage: Stage  # INTERPRET_PIXEL or INTERPRET_PALETTE
    engine_id: str
    params: dict[str, Any] = field(default_factory=dict)
