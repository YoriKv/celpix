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

# The pass-through compression id. This is one of the two plugin ids the *host*
# has to know by name: "no compression" is not merely another scheme but the
# condition several behaviours key off — a raw byte stream maps linearly to file
# offsets (so addresses stay meaningful and slices can be carved from the view),
# and the overlay/scan tools only mean anything once a real decompressor is
# chosen. Named here, in the contract, rather than spelled out at each test.
NO_COMPRESSION = "compression.none"

# The plain-bytes container — the equivalent of the pass-through above, and named
# here for the same reason: "no container" is the fallback every detection lands
# on when nothing claims a file, so the host has to know it by name.
RAW_CONTAINER = "container.raw-file"

# The pass-through reshape, named for the same reason as NO_COMPRESSION: "not
# reshaped" is the condition the offset-mapping behaviours key off — a reshaped
# region is a byte permutation, so view positions no longer name file offsets,
# addresses go dark and slices can't be carved from the view until it is none.
NO_RESHAPE = "reshape.none"


@dataclass(frozen=True)
class FileRef:
    """A read source / write destination on disk — the **host's** descriptor.

    This says where bytes live; it is not what a container plugin sees. The host
    resolves it into a :class:`ReadSource` or :class:`WriteTarget` — doing the
    open itself — so that acquiring bytes is one implementation in one place
    rather than one per plugin (:class:`ContainerPlugin`).

    ``paths`` is a **list**, because a graphics region is not always one file: an
    arcade board's tiles routinely live on several ROM chips that only mean
    anything joined together. The files are concatenated in the order given and
    the container is handed the result, so it never learns there was more than
    one — the ordinary single-file case is simply a list of one, and behaves
    exactly as it always did. A bare string is accepted and wrapped, so
    ``FileRef("rom.bin")`` still says what it always said.

    Order is the caller's to get right and is never inferred: nothing in the files
    themselves says which chip comes first, and guessing (alphabetical, say) would
    silently interleave a sprite sheet wrong rather than fail.

    ``offset`` is where the meaningful bytes begin (e.g. past a ROM header);
    ``length`` optionally bounds them (``None`` = to end). With several files
    those address the **concatenation**, not any one file — the joined buffer is
    the only thing the coordinates could be relative to once the pieces are
    together.

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

    paths: str | tuple[str, ...]
    offset: int = 0
    length: int | None = None
    data: bytes | None = None
    data_base: int = 0

    def __post_init__(self) -> None:
        # Normalise on the way in so every consumer sees a tuple and two refs
        # naming the same file compare equal however each was spelled. A bare
        # string is wrapped rather than iterated — `tuple("rom.bin")` would
        # quietly become a ref to eight one-character files.
        if isinstance(self.paths, str):
            object.__setattr__(self, "paths", (self.paths,))
        elif not isinstance(self.paths, tuple):
            object.__setattr__(self, "paths", tuple(self.paths))

    @property
    def path(self) -> str:
        """The first file: this source's identity for provenance and display.

        A multi-file source still has one name to be known by — the entry it
        belongs to, the file a save-back is attributed to, the row in the Files
        list. The rest are named in :attr:`paths` and, for a container that cares,
        on the context under ``KEY_SOURCE_FILES``.
        """
        return self.paths[0] if self.paths else ""


@dataclass(frozen=True)
class SourceFile:
    """One file's contribution to the buffer a container was handed.

    The host publishes these as ``KEY_SOURCE_FILES`` on the context, in order, so
    a container that *does* care how its bytes were assembled can find out —
    which chip supplied which range, and how many there were. Nothing is required
    to look: the point of joining the files in the host is that the ordinary
    container never has to.

    ``start`` is the offset into the joined buffer, not into the file, since that
    is the coordinate space everything downstream is already working in.
    """

    path: str
    start: int
    length: int


@dataclass(frozen=True)
class ReadSource:
    """What a container's Read is handed: the bytes, and where they sit.

    ``data`` is the **whole** source — the file's contents, or the in-memory
    buffer a :class:`FileRef` carried — never a pre-cut window, because where a
    container's payload begins is the container's own answer: an iNES reader has
    to see the header to find the CHR ROM past it, and a copier header is only
    detectable from the file's full length. The requested window is passed
    alongside as ``offset``/``length`` for the containers that do honour it.

    ``base`` is the file offset ``data[0]`` corresponds to, so ``offset`` stays
    **file-absolute** whatever buffer it is being resolved against — see
    :attr:`FileRef.data_base`, which it comes from. :meth:`window` applies both,
    which is the whole of what a container that adds no framing of its own has to
    do.

    ``path`` is provenance for messages only. A container never opens it: the
    bytes are already here, and reaching for the file instead would serve stale
    ones whenever the source is an unsaved buffer.
    """

    data: bytes
    path: str = ""
    offset: int = 0
    length: int | None = None
    base: int = 0

    @property
    def start(self) -> int:
        """Index into :attr:`data` of the requested window's first byte."""
        return max(0, self.offset - self.base)

    def window(self) -> bytes:
        """:attr:`data` cut to the requested ``offset``/``length`` window."""
        start = self.start
        end = len(self.data) if self.length is None else start + self.length
        return self.data[start:end]


@dataclass(frozen=True)
class WriteTarget:
    """What a container's Write is handed: the destination as it stands now.

    ``existing`` is the destination file's current contents (``b""`` when it does
    not exist yet), and the write returns what should replace them — so a
    container is a byte transform in both directions and the host does the one
    thing that touches disk. That is what lets a container preserve everything it
    did not decode (a header, the program banks, whatever surrounds a slot)
    without each one re-deriving how to read and rewrite a file, and it is what
    keeps a container ignorant of *how many* files its bytes came from.

    ``offset``/``length`` describe the slot inside the destination the edited
    bytes belong to — :attr:`whole_file` is the common case where they are the
    whole of it. Ignoring the slot and returning just the payload truncates the
    file to it; :func:`splice` is the correct one-liner.
    """

    existing: bytes
    path: str = ""
    offset: int = 0
    length: int | None = None

    @property
    def whole_file(self) -> bool:
        """Whether the edited bytes are the entire destination, not a slot in it."""
        return self.offset == 0 and self.length is None


def splice(existing: bytes, at: int, data: bytes) -> bytes:
    """``existing`` with ``data`` laid over it at ``at``, keeping every other byte.

    Zero-extends when the result reaches past the end, so writing into a file
    shorter than the slot (or into one that isn't there yet) works rather than
    failing on a gap. Shared by every built-in container's write half and offered
    to third-party ones, because preserving the bytes around a slot is the part
    that is easy to get wrong and identical for all of them.
    """
    out = bytearray(existing)
    end = at + len(data)
    if len(out) < end:
        out.extend(b"\x00" * (end - len(out)))
    out[at:end] = data
    return bytes(out)


@dataclass(frozen=True)
class PluginInfo:
    """A plugin's identity. ``id`` is stable and namespaced by stage.

    ``stage`` is **optional for a folder-dropped plugin**: the folder it was found
    in determines it (:data:`~celpix.plugins.discovery.FOLDER_STAGE`), exactly as
    it already does for a preset's TOML and for a code format's
    :class:`~celpix.plugins.formats.FormatInfo` — so a container in
    ``containers/`` need not repeat "this is a container", nor import
    :class:`~celpix.core.errors.Stage` to say it. A stated stage that agrees is
    tolerated; a conflicting one is a load issue.

    Everything shipped states it regardless — the built-ins because they register
    straight into a registry with no folder behind them, and the plugin examples
    to match, since they are what a plugin author copies and one line buys a file
    that describes itself wherever it ends up.

    ``self_delimiting`` is **Compression-only** and describes the *scheme*, not any
    one decode: false means the stream carries no end marker, so its extent is
    knowable only from outside (a slice length, a container's byte count). It is
    declared here rather than recorded in the context because it is a static
    property — true before a single byte is read, and on every code path — and
    because the UI has to phrase "the decode stopped here" differently for the
    two: a scheme *with* an end marker that didn't reach one was cut short and
    can be fixed by widening the window, while one without simply decodes as far
    as it is fed and always will.

    ``extensions`` and ``magic`` are **Container-only** and together form its
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

    ``preserves_offsets`` is **Container-only**: does a byte's position survive
    the read? A container that only *skips* — a header, a copier block — leaves
    every remaining byte where it was, so a view position is still a file offset
    once the recorded start is added back. One that **reorders** (``.smd``'s
    odd/even split, an interleaved SNES image, an N64 byte-order normalisation)
    makes its output a different address space from the file: the buffer is the
    ROM as the machine sees it and the file is a scrambled encoding of it, so a
    position in one names nothing in the other. Anything that re-reads by offset
    has to know which it is dealing with — an Offset palette resolves against
    the container's *output* either way, but only a position-preserving
    container can also write an edit back through a plain file offset
    (``docs/design/palette-editing.md`` §2).

    """

    id: str
    name: str
    stage: Stage | None = None
    self_delimiting: bool = True
    extensions: tuple[str, ...] = ()
    magic: tuple[tuple[int, bytes], ...] = ()
    size_modulo: tuple[int, int] | None = None
    min_size: int = 0
    short_name: str = ""
    preserves_offsets: bool = True


# The method a plugin at each stage must have to be that kind of plugin at all.
# Only the *load* direction is required — the save half is optional and its
# absence is the documented way to ship a view-only format (:class:`ContainerPlugin`).
#
# This is what replaces the stage declaration as the safety net: with the folder
# supplying the stage, a container dropped in ``pixel/`` would otherwise register
# as a pixel codec and fail at decode time. Checking the shape at registration
# catches that — and catches a typo'd method name, which a stage declaration
# never did.
STAGE_METHODS: dict[Stage, tuple[str, ...]] = {
    Stage.CONTAINER: ("read",),
    Stage.RESHAPE: ("reshape",),
    Stage.COMPRESSION: ("decompress",),
    Stage.INTERPRET_PIXEL: ("decode", "encode", "bytes_per_tile", "tile_size"),
    Stage.INTERPRET_PALETTE: ("decode", "encode", "bytes_per_entry"),
}


def missing_methods(plugin: object, stage: Stage) -> list[str]:
    """Which of ``stage``'s required methods ``plugin`` does not have."""
    return [m for m in STAGE_METHODS[stage] if not callable(getattr(plugin, m, None))]


@runtime_checkable
class Plugin(Protocol):
    """Common to every plugin: it carries its :class:`PluginInfo`."""

    info: PluginInfo


class ContainerPlugin(Plugin, Protocol):
    """One on-disk wrapper, both directions: unwrap it to read, restore it to write.

    ``read`` takes source bytes to payload bytes and ``write`` takes edited payload
    bytes to the destination's new contents. Neither opens a path — the host has
    already acquired the bytes (:class:`ReadSource`) and writes back what ``write``
    returns — so a container is a pure byte transform. What only the container can
    contribute is where its payload starts, published as ``KEY_SOURCE_OFFSET`` on
    ``ctx``: everything downstream that shows an address or anchors a slice reads it.

    Returning the destination *whole* rather than just the payload is what lets a
    container repair an invariant that spans the file (a ROM checksum) or restore
    framing that surrounds the payload, which a plugin confined to its own slot
    could not do.

    **``write`` may be omitted**, and a container without one is *view-only*: its
    files open and cannot be saved. Unwrapping is not something plain bytes can
    undo — the bytes would go back scrambled or in the wrong place, leaving the
    file worse than it was loaded — so rather than have each container declare
    whether its unwrapping is reversible (a question whose safe answer is always
    "supply the inverse"), the rule is simply that saving requires the method.
    Forgetting it costs a save, not a file.
    """

    def read(self, source: ReadSource, ctx: PipelineContext) -> bytes: ...

    def write(self, data: bytes, dest: WriteTarget, ctx: PipelineContext) -> bytes: ...


class ReshapePlugin(Plugin, Protocol):
    """One region-scoped byte reordering, both directions.

    A reshape is a **length-preserving permutation of the whole region** — an
    arcade board's plane-per-chip split joined back together, Mode 7 VRAM's
    interleave separated. It is its own stage rather than a compression scheme
    because it is *region-scoped* where compression is *structure-scoped*: it has
    no extent, no end marker, cannot be scanned for, and is only correct applied
    to the entire buffer (the part boundaries are fractions of ``len(data)``).
    It runs between the container and the decompressor, so a compressed structure
    inside an interleaved ROM pair is contiguous by the time compression sees it.

    ``reshape`` runs on load, ``unshape`` on save; the two must be exact
    inverses so the round trip is byte-identical. **``unshape`` may be
    omitted**, following the same rule as :meth:`ContainerPlugin.write` — its
    absence makes the data view-only.
    """

    def reshape(self, data: bytes, ctx: PipelineContext) -> bytes: ...

    def unshape(self, data: bytes, ctx: PipelineContext) -> bytes: ...


class CompressionPlugin(Plugin, Protocol):
    """One compression scheme, both directions. Pass-through when uncompressed.

    ``compress`` follows the same optional rule as
    :meth:`ContainerPlugin.write` — a scheme that can be decoded but not re-encoded
    is legitimate and common (a format reverse-engineered far enough to view), and
    ships ``decompress`` alone. Its data opens read-only.
    """

    def decompress(self, data: bytes, ctx: PipelineContext) -> bytes: ...

    def compress(self, data: bytes, ctx: PipelineContext) -> bytes: ...


def writes_back(plugin: Plugin, method: str) -> bool:
    """Whether ``plugin`` ships the save-side half named by ``method``.

    The one test behind every "view-only" decision — ``write`` for a container,
    ``unshape`` for a reshape, ``compress`` for a compression scheme. It is a
    plain attribute check because
    the halves live on one object now: shipping the method *is* the declaration,
    and there is nothing else that could disagree with it.
    """
    return callable(getattr(plugin, method, None))


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
