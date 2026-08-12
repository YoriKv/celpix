"""The plugin contract.

Every pipeline stage is an extension point and every concrete behaviour —
including the built-ins — is a plugin on this API (``docs/design/overview.md``
§3). The host owns the machinery (pipeline, model, registry, file/context
plumbing); a plugin describes only what is unique to it.

Two tiers sit behind these protocols. A *preset* (:class:`Preset`) is a
parameter set a generic engine interprets, so a new planar or color format is
data rather than code; a plugin class implementing one of the protocols below is
the escape hatch for behaviour data cannot express. Qt-free — these run headless.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from celpix.core.capabilities import ContentKind
from celpix.core.context import (
    KEY_COMPRESSED_SIZE,
    KEY_DECOMPRESS_COMPLETE,
    KEY_DECOMPRESS_PARTIAL,
    KEY_SOURCE_OFFSET,
    PipelineContext,
)
from celpix.core.errors import Stage
from celpix.core.index_grid import IndexGrid
from celpix.core.palette import Palette
from celpix.core.tilemap import Cell, CellOp

# The three pass-through ids the *host* knows by name: each names a condition
# several behaviours key off, not merely one more plugin.
#
# Uncompressed bytes map linearly to file offsets, so addresses stay meaningful
# and slices can be carved from the view; the overlay and scan tools only mean
# anything once a real decompressor is chosen.
NO_COMPRESSION = "compression.none"

# Where detection lands when nothing claims a file.
RAW_CONTAINER = "container.raw-file"

# A reshaped region is a byte permutation, so view positions no longer name file
# offsets: addresses go dark and slices can't be carved until this is selected.
NO_RESHAPE = "reshape.none"

# The same three keyed by stage. Each one *does nothing*, so standing in for a
# plugin the registry hasn't got leaves the stage a no-op rather than a different
# transform (:meth:`~celpix.plugins.registry.Registry.resolve_stage`).
STAGE_PASSTHROUGH: dict[Stage, str] = {
    Stage.CONTAINER: RAW_CONTAINER,
    Stage.RESHAPE: NO_RESHAPE,
    Stage.COMPRESSION: NO_COMPRESSION,
}

# What each Interpret stage falls back to when the preset an entry names is one
# this build hasn't got (:meth:`~celpix.plugins.registry.Registry.resolve_preset`).
#
# A byte stage has a pass-through to stand in with, so a missing plugin there
# costs nothing but the transform. Interpret has no such thing — *something* has
# to say how many bits a pixel is before anything can be drawn — so the stand-in
# is a plain, commonplace format instead: what a fresh window starts on. It will
# usually be the wrong one, and that is the point of saying so out loud rather
# than degrading quietly.
STAGE_DEFAULT_PRESET: dict[Stage, str] = {
    Stage.INTERPRET_PIXEL: "preset.pixel.snes-4bpp",
    Stage.INTERPRET_PALETTE: "preset.palette.bgr555",
    Stage.INTERPRET_TILEMAP: "preset.tilemap.snes-bg",
}


# The headings a format picker groups its entries under, in the order they are
# shown. A plugin or preset names one in its ``category`` field; anything else it
# names is honoured too and sorted alphabetically after these, so a third-party
# preset is never rejected for inventing a heading — the list is a *convention*,
# so the shipped formats don't grow three spellings of "Nintendo", not a
# whitelist.
#
# The two **source** headings lead, and are the one pair a format does not choose
# for itself: discovery applies them to everything it loads out of a plugin folder
# (:func:`~celpix.plugins.discovery.load_directory`), overriding whatever the file
# said. Anything you dropped in yourself is a handful of entries among a hundred
# shipped ones, and burying them under a vendor heading is exactly the "scroll
# past everything to find the one you want" this grouping exists to end. The
# project's own folder comes first of the two, being the narrower answer to
# "what am I working on".
#
# Then vendor, because that is how someone hunting a shipped format asks the
# question ("what does the SNES use?"). The generic buckets after them are for the
# presets that belong to no one machine — a bitplane layout several consoles
# happen to share, a texel format that is just a pixel's bits — and they come last
# because reaching for one means the vendor list didn't have it.
CATEGORIES: tuple[str, ...] = (
    "Project plugins",
    "Your plugins",
    "Nintendo",
    "Sega",
    "NEC",
    "SNK",
    "Sony",
    "Bandai",
    "Arcade",
    "Authoring tools",
    "Text",
    "Generic planar",
    "Generic linear",
    "Direct color",
    "Indexed",
    "Generic",
)


def category_order(category: str) -> tuple[int, str]:
    """Sort key placing ``category`` in :data:`CATEGORIES` order.

    Unlisted headings sort alphabetically after the listed ones, and the empty
    category — a format that names none — sorts *before* every heading, so an
    untagged entry stays at the top of its picker rather than under the
    groups. That is what keeps the pass-through ("None (uncompressed)") where a
    default belongs, and what makes a picker of entirely untagged formats read as
    the plain flat list it was before any of this.
    """
    if not category:
        return (-1, "")
    if category in CATEGORIES:
        return (CATEGORIES.index(category), "")
    return (len(CATEGORIES), category)


@dataclass(frozen=True)
class FileRef:
    """A read source / write destination on disk — the **host's** descriptor.

    Not what a container sees: the host resolves this into a :class:`ReadSource`
    or :class:`WriteTarget`, doing the open itself, so acquiring bytes is one
    implementation rather than one per plugin.

    ``paths`` is a **list** because a graphics region is not always one file — an
    arcade board's tiles routinely live on several ROM chips that only mean
    anything joined. The files are concatenated in the order given and the
    container is handed the result, never learning there was more than one. Order
    is the caller's to get right and never inferred: nothing in the files says
    which chip comes first, and guessing would silently interleave a sprite sheet
    wrong rather than fail. A bare string is accepted and wrapped.

    ``offset`` is where the meaningful bytes begin (past a ROM header, say) and
    ``length`` optionally bounds them (``None`` = to end). With several files both
    address the **concatenation**, the only coordinate space left once the pieces
    are joined.

    ``data``, when set, *is* the source bytes (still sliced by
    ``offset``/``length``), so a reader yields them without touching disk — how a
    palette pulled from an emulator memory image flows through the ordinary
    pipeline, and how a slice reads a dirty parent's unsaved bytes rather than the
    stale file. ``path`` is still carried for provenance and display; write
    destinations never set ``data``.

    ``data_base`` is the file offset ``data[0]`` corresponds to, keeping
    ``offset`` **file-absolute** whether the bytes come from disk or memory: a
    buffer starting part way into the file declares where it begins rather than
    forcing every consumer into relative offsets. Ignored when ``data`` is None —
    a file is always its own base.
    """

    paths: str | tuple[str, ...]
    offset: int = 0
    length: int | None = None
    data: bytes | None = None
    data_base: int = 0

    def __post_init__(self) -> None:
        # Normalise so every consumer sees a tuple and two refs naming the same
        # file compare equal however each was spelled. A bare string is wrapped
        # rather than iterated: `tuple("rom.bin")` would become eight refs.
        if isinstance(self.paths, str):
            object.__setattr__(self, "paths", (self.paths,))
        elif not isinstance(self.paths, tuple):
            object.__setattr__(self, "paths", tuple(self.paths))

    @property
    def path(self) -> str:
        """The first file: this source's identity for provenance and display.

        A multi-file source still needs one name — the entry it belongs to, the
        file a save-back is attributed to, the row in the Files list. The rest are
        in :attr:`paths` and on the context under ``KEY_SOURCE_FILES``.
        """
        return self.paths[0] if self.paths else ""


@dataclass(frozen=True)
class SourceFile:
    """One file's contribution to the buffer a container was handed.

    The host publishes these in order as ``KEY_SOURCE_FILES`` on the context, so a
    container that cares how its bytes were assembled can find out which chip
    supplied which range. Nothing is required to look — joining the files in the
    host is what spares the ordinary container from having to.

    ``start`` is the offset into the joined buffer, the coordinate space
    everything downstream already works in.
    """

    path: str
    start: int
    length: int


@dataclass(frozen=True)
class ReadSource:
    """What a container's Read is handed: the bytes, and where they sit.

    ``data`` is the **whole** source — the file's contents, or the in-memory
    buffer a :class:`FileRef` carried — never a pre-cut window, because where the
    payload begins is the container's own answer: an iNES reader has to see the
    header to find the CHR ROM past it, and a copier header is only detectable
    from the file's full length. The requested window comes alongside as
    ``offset``/``length`` for the containers that honour it.

    ``base`` is the file offset ``data[0]`` corresponds to (from
    :attr:`FileRef.data_base`), keeping ``offset`` **file-absolute** whatever
    buffer it resolves against. :meth:`window` applies both, which is the whole of
    what a container adding no framing of its own has to do.

    ``path`` is provenance for messages only. A container never opens it: the
    bytes are already here, and re-reading the file would serve stale ones
    whenever the source is an unsaved buffer.
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

    ``existing`` is the destination's current contents (``b""`` when it does not
    exist yet) and the write returns what should replace them, so a container is a
    byte transform in both directions and the host does the one thing that touches
    disk. That is what lets a container preserve everything it did not decode (a
    header, the program banks, whatever surrounds a slot) without re-deriving how
    to read and rewrite a file, and what keeps it ignorant of how many files its
    bytes came from.

    ``offset``/``length`` describe the slot inside the destination the edited
    bytes belong to; :attr:`whole_file` is the common case where they are the
    whole of it. Returning just the payload truncates the file to that slot —
    :func:`splice` is the correct one-liner.
    """

    existing: bytes
    path: str = ""
    offset: int = 0
    length: int | None = None

    @property
    def whole_file(self) -> bool:
        """Whether the edited bytes are the entire destination, not a slot in it."""
        return self.offset == 0 and self.length is None


def format_size(count: int) -> str:
    """``count`` bytes as one short phrase, for a :class:`ContainerField` value.

    Whole binary multiples get the unit they are quoted in — a cart is "512 KiB",
    never "524288 bytes" — and anything else stays exact, since a length that is
    *not* round is usually the interesting thing about it.
    """
    for unit, size in (("MiB", 1 << 20), ("KiB", 1 << 10)):
        if count >= size and count % size == 0:
            return f"{count // size} {unit}"
    return f"{count} bytes"


def plain_read(source: ReadSource, ctx: PipelineContext) -> bytes:
    """The read of a container that strips no framing: publish, then window.

    The payload begins where the caller asked rather than at an answer of the
    format's own, so the offset is published unchanged — and stays file-absolute
    even when the bytes came from a buffer starting part way into the file
    (:meth:`ReadSource.window`). The read-side twin of :func:`splice`, identical
    for every container that only acts on the write side.
    """
    ctx.set(KEY_SOURCE_OFFSET, source.offset)
    return source.window()


def splice(existing: bytes, at: int, data: bytes) -> bytes:
    """``existing`` with ``data`` laid over it at ``at``, keeping every other byte.

    Zero-extends when the result reaches past the end, so writing into a file
    shorter than the slot — or one that isn't there yet — works rather than
    failing on a gap. Shared by every built-in container's write half and offered
    to third-party ones: preserving the bytes around a slot is identical for all.
    """
    out = bytearray(existing)
    end = at + len(data)
    if len(out) < end:
        out.extend(b"\x00" * (end - len(out)))
    out[at:end] = data
    return bytes(out)


@dataclass(frozen=True)
class ContainerField:
    """One value a container read out of a file, for the container-info popup.

    ``name`` is the field as the *format* calls it and ``value`` what this file
    holds, formatted the way the format is normally quoted — hex for an offset, a
    count with the size it works out to beside it. ``detail`` is the part a hex
    editor cannot give: what the container **used** the value for, or why it
    deliberately ignored it.

    ``detail`` is shown as the row's tooltip, so it follows the tooltip rule —
    hard-wrapped with explicit newlines at ~60 characters, one clause per line,
    since Qt never wraps a plain-text tooltip
    (``docs/py-qt-reference/pyside6-pitfalls.md``).
    """

    name: str
    value: str
    detail: str = ""


@dataclass(frozen=True)
class PluginInfo:
    """A plugin's identity. ``id`` is stable and namespaced by stage.

    ``stage`` is **optional for a folder-dropped plugin**: the folder determines
    it (:data:`~celpix.plugins.discovery.FOLDER_STAGE`), as it does for a preset's
    TOML and a code format's :class:`~celpix.plugins.formats.FormatInfo`, so a
    container in ``containers/`` need not repeat that it is one. A stated stage
    that agrees is tolerated; a conflicting one is a load issue. Everything
    shipped states it anyway — the built-ins register with no folder behind them,
    and the examples match so a copied file describes itself wherever it lands.

    ``self_delimiting`` is **Compression-only** and describes the *scheme*, not
    one decode: false means the stream carries no end marker, so its extent is
    knowable only from outside (a slice length, a container's byte count). It is
    static — true before a byte is read — and the UI phrases "the decode stopped
    here" differently for the two: a scheme with an end marker that didn't reach
    one was cut short and widening the window can fix it, while one without simply
    decodes as far as it is fed.

    ``extensions`` and ``magic`` are **Container-only** and together form its
    *signature*, what makes opening a file pick its container instead of asking.
    ``extensions`` are lowercase suffixes including the dot (``(".nes",)``);
    ``magic`` is a tuple of ``(offset, bytes)`` probes, any one matching being a
    hit (a format with two byte orders declares both). Both are inert data because
    detection runs over every registered container — including untrusted ones —
    before a file is open, so it must not execute plugin code.

    A container declaring ``magic`` is matched **only** by it: the bytes assert
    what the format is, so a ``.nes`` file without ``NES\\x1a`` is not an iNES
    file whatever it is called. With no ``magic`` it falls back to ``extensions``
    alone, the best some wrappers offer (Sega ``.smd`` carries no marker).

    ``exact_size`` narrows to one file length, for the fixed-size authoring
    formats where several members of a family share a signature *at the same
    offset* and differ only in how long they are — nothing else tells a panel
    from a stamp layout. Zero (the default) means the container does not care.
    Like the two below it this only ever *rejects*: a length alone would claim
    every binary that happened to be that long.

    ``size_modulo`` (``(modulus, remainder)``) and ``min_size`` **narrow** a match
    rather than making one: they apply only once an extension or magic has already
    matched, since a size rule alone would seize any binary of the right length.
    They identify the wrappers that carry no marker at all — a 512-byte copier
    header shows up as a ROM being 512 bytes over a whole number of KiB, which
    carts never are. ``min_size`` keeps that arithmetic off files far too small to
    be what it describes: a 512-byte ``.4bpp.sfc`` tile sheet is also "512 over
    zero KiB", and the rule only means something with a plausible cartridge
    behind the header.

    ``short_name`` is a compact form of ``name`` for places showing a plugin
    *inline with other text* — the Files list tags each file with its container,
    where "Game Boy ROM (checksum repair on write)" would bury the filename.
    Empty means ``name`` is already short enough.

    ``content_kinds`` is **Container-only**: what kind of entry this frames. A
    palette file and a graphics file are unwrapped by disjoint sets of formats,
    and offering either list to the other is how a user is invited to read a
    palette as an iNES ROM. The default is the graphics pair, which is what every
    container was before any of them framed a palette, so a plugin that says
    nothing keeps the behaviour it had. The plain-bytes container declares all
    three: it is where detection lands for anything unclaimed, whatever the entry
    holds.

    ``preserves_offsets`` is **Container-only**: does a byte's position survive
    the read? A container that only *skips* — a header, a copier's 512-byte
    prefix — leaves every remaining byte where it was, so a view position is
    still a file offset once the recorded start is added back. One that
    **reorders** (``.smd``'s odd/even split, an interleaved SNES image, an N64
    byte-order normalisation) makes its output a different address space from the
    file, so a position in one names nothing in the other. An Offset palette
    resolves against the container's *output* either way, but only a
    position-preserving container can also write an edit back through a plain
    file offset (``docs/design/palette-editing.md`` §2).

    ``category`` is the heading a picker files this plugin under
    (:data:`CATEGORIES`) — presentation only, and empty means "no heading".
    """

    id: str
    name: str
    stage: Stage | None = None
    self_delimiting: bool = True
    extensions: tuple[str, ...] = ()
    magic: tuple[tuple[int, bytes], ...] = ()
    size_modulo: tuple[int, int] | None = None
    min_size: int = 0
    exact_size: int = 0
    short_name: str = ""
    content_kinds: tuple[ContentKind, ...] = (ContentKind.PIXELS, ContentKind.TILEMAP)
    preserves_offsets: bool = True
    category: str = ""


# The methods a plugin must have to be that kind of plugin at all. Only the
# *load* direction is required; the save half is optional and its absence is how
# a view-only format is shipped (:class:`ContainerPlugin`).
#
# The folder a plugin is dropped in supplies its stage, so a container dropped in
# ``pixel/`` would otherwise register as a pixel codec and fail at decode time.
# Checking the shape at registration catches that, and a typo'd method name too.
STAGE_METHODS: dict[Stage, tuple[str, ...]] = {
    Stage.CONTAINER: ("read",),
    Stage.RESHAPE: ("reshape",),
    Stage.COMPRESSION: ("decompress",),
    Stage.INTERPRET_PIXEL: ("decode", "encode", "bytes_per_tile", "tile_size"),
    Stage.INTERPRET_PALETTE: ("decode", "encode", "bytes_per_entry"),
    Stage.INTERPRET_TILEMAP: ("decode", "encode", "bytes_per_cell", "cell_tiles"),
}


# The optional save-side half per byte-handling stage — the load half's inverse.
# A plugin is that kind of plugin if it has the row above, and can be *written*
# through if it also has this one. The interpret stages have no row here: their
# encode is required, so there is no view-only interpret plugin.
SAVE_METHOD: dict[Stage, str] = {
    Stage.CONTAINER: "write",
    Stage.RESHAPE: "unshape",
    Stage.COMPRESSION: "compress",
}


def missing_methods(plugin: object, stage: Stage) -> list[str]:
    """Which of ``stage``'s required methods ``plugin`` does not have."""
    return [m for m in STAGE_METHODS[stage] if not callable(getattr(plugin, m, None))]


def check_declared_stage(spec: dict, stage: Stage) -> None:
    """Raise if ``spec`` states a stage that disagrees with ``stage``.

    The folder a spec was found in is authoritative. Stating the stage anyway is
    tolerated when it agrees, keeping a preset self-describing, but a conflicting
    one is an error rather than a silent relocation into the wrong pathway. One
    rule for every kind of preset, so the message doesn't depend on the folder.
    """
    declared = spec.get("stage")
    if declared is not None and declared != stage.value:
        raise ValueError(
            f"stage {declared!r} conflicts with the folder's stage {stage.value!r} - "
            "remove the stage field; the folder determines it"
        )


@runtime_checkable
class Plugin(Protocol):
    """Common to every plugin: it carries its :class:`PluginInfo`."""

    info: PluginInfo


class ContainerPlugin(Plugin, Protocol):
    """One on-disk wrapper, both directions: unwrap it to read, restore it to write.

    ``read`` takes source bytes to payload bytes and ``write`` takes edited payload
    bytes to the destination's new contents. Neither opens a path — the host has
    already acquired the bytes (:class:`ReadSource`) and writes back what ``write``
    returns — so a container is a pure byte transform. Only the container knows
    where its payload starts; it publishes that as ``KEY_SOURCE_OFFSET`` on
    ``ctx``, which everything downstream showing an address or anchoring a slice
    reads.

    Returning the destination *whole* rather than just the payload lets a
    container repair an invariant spanning the file (a ROM checksum) or restore
    framing around the payload, which a plugin confined to its slot could not do.

    **``write`` may be omitted**, and a container without one is *view-only*: its
    files open and cannot be saved. Unwrapping is not something plain bytes can
    undo — they would go back scrambled or in the wrong place, leaving the file
    worse than it was loaded — so rather than have each container declare whether
    its unwrapping is reversible, saving simply requires the method. Forgetting it
    costs a save, not a file.
    """

    def read(self, source: ReadSource, ctx: PipelineContext) -> bytes: ...

    def write(self, data: bytes, dest: WriteTarget, ctx: PipelineContext) -> bytes: ...

    def describe(
        self, source: ReadSource, ctx: PipelineContext
    ) -> tuple[ContainerField, ...]:
        """What this container read out of ``source``, as name/value rows.

        Optional, and for the user rather than the pipeline: the container-info
        popup shows these beside the hints the read published, so a file that
        opened as something unexpected can be understood without a hex editor —
        *this* is the depth byte the tiles were decoded at, *that* is why the
        payload stops where it does. Say what each value was used for in
        :attr:`ContainerField.detail`, including where it was deliberately
        **not** used: a header field this container reads past on purpose is
        worth a row saying so, since the next person to open the file in a hex
        editor will find it and wonder.

        The host calls it right after :meth:`read`, on that read's own context
        and the same :class:`ReadSource`, so anything already worked out can be
        taken off ``ctx`` instead of parsed twice. Nothing downstream sees the
        result and nothing may be published from here — a container that strips
        no framing has nothing to report and simply omits the method.
        """
        ...


class ReshapePlugin(Plugin, Protocol):
    """One region-scoped byte reordering, both directions.

    A reshape is a **length-preserving permutation of the whole region** — an
    arcade board's plane-per-chip split joined back together, Mode 7 VRAM's
    interleave separated. It is its own stage rather than a compression scheme
    because it is *region-scoped* where compression is *structure-scoped*: it has
    no extent, no end marker, cannot be scanned for, and is only correct over the
    entire buffer (the part boundaries are fractions of ``len(data)``). It runs
    between the container and the decompressor, so a compressed structure inside
    an interleaved ROM pair is contiguous by the time compression sees it.

    ``reshape`` runs on load and ``unshape`` on save; the two must be exact
    inverses so the round trip is byte-identical. **``unshape`` may be omitted**,
    following :meth:`ContainerPlugin.write`'s rule — its absence makes the data
    view-only.
    """

    def reshape(self, data: bytes, ctx: PipelineContext) -> bytes: ...

    def unshape(self, data: bytes, ctx: PipelineContext) -> bytes: ...


class CompressionPlugin(Plugin, Protocol):
    """One compression scheme, both directions. Pass-through when uncompressed.

    ``compress`` is optional on :meth:`ContainerPlugin.write`'s rule: a scheme
    reverse-engineered far enough to view but not to re-encode ships
    ``decompress`` alone, and its data opens read-only.

    """

    def decompress(self, data: bytes, ctx: PipelineContext) -> bytes: ...

    def compress(self, data: bytes, ctx: PipelineContext) -> bytes: ...


class PartialDecompression:
    """Both :class:`CompressionPlugin` methods, over a decoder that finds its end.

    A scheme whose decoder can report **where the structure ended** owes the
    pipeline two facts on every read, and they are the same few lines whatever the
    scheme is: take the partial flag off the context, decode, then publish the
    compressed size and whether the structure completed.

    Publishing both is a contract rather than a convenience.
    :data:`~celpix.core.context.KEY_COMPRESSED_SIZE` is what a save-back measures
    its slot against and what the structure scan reads;
    :data:`~celpix.core.context.KEY_DECOMPRESS_COMPLETE` is what stops a slice
    created without a length from backfilling its extent from a decode that merely
    ran out of buffer. A scheme with **no end to find** — PackBits, LZ16 — states
    that for itself instead and does not use this.

    A subclass supplies :meth:`_decode` and :meth:`_encode`: ``staticmethod`` where
    the module functions take the data alone, an ordinary method where the variant
    has a parameter to bind (the size-prefix width of an SLZ framing, say).
    ``_encode`` is optional on the same rule as ``compress`` itself — a scheme that
    can be read but not written leaves it unset, and the class then has to leave
    ``compress`` off too, since shipping the method *is* the declaration that a
    save-back works.
    """

    def _decode(self, data: bytes, *, partial: bool) -> tuple[bytes, int, bool]:
        """``(output, consumed, complete)`` for one structure at ``data[0]``."""
        raise NotImplementedError

    def _encode(self, data: bytes) -> bytes:
        raise NotImplementedError

    def decompress(self, data: bytes, ctx: PipelineContext) -> bytes:
        out, consumed, complete = self._decode(
            data, partial=bool(ctx.get(KEY_DECOMPRESS_PARTIAL))
        )
        ctx.set(KEY_COMPRESSED_SIZE, consumed)
        ctx.set(KEY_DECOMPRESS_COMPLETE, complete)
        return out

    def compress(self, data: bytes, ctx: PipelineContext) -> bytes:
        return self._encode(data)


def writes_back(plugin: Plugin, stage: Stage) -> bool:
    """Whether ``plugin`` ships ``stage``'s save-side half.

    The one test behind every "view-only" decision, and a plain attribute check
    because both halves live on one object: shipping the method *is* the
    declaration. :data:`SAVE_METHOD` names it, so no caller has to.
    """
    return callable(getattr(plugin, SAVE_METHOD[stage], None))


class PixelCodecPlugin(Plugin, Protocol):
    """The pixel-side view interpretation: bytes ⇄ a list of tiles.

    ``params`` is a preset's parameter set (bpp, tile size, plane offsets, …). The
    engine walks whatever buffer it is given one tile at a time — **buffer-relative
    and stateless**, so handing it a window covering just the visible tiles decodes
    exactly that window. That is what enables deferred, windowed decoding of large
    files without ``decode`` knowing the window's size or position.

    The host must know a tile's **byte size** to cut that window out;
    :meth:`bytes_per_tile` exposes it as a pure function of ``params``, keeping the
    codec the authority on its own atomic geometry.
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
        """Byte size of one palette **read unit** under ``params`` — the
        palette-side mirror of :meth:`PixelCodecPlugin.bytes_per_tile`, so the
        host can size a byte window for a wanted number of entries.

        A unit is one entry for every format whose entry is a whole number of
        bytes, which is all but the handheld grayscale registers; those pack
        several entries into a unit and say so with ``entries_per_unit``."""
        ...

    # entries_per_unit is optional, reached with getattr and assumed to be 1: a
    # colour codec whose entries are byte-sized (every one of them until the
    # Game Boy's four-shades-per-byte BGP register) has nothing to declare.
    def entries_per_unit(self, params: dict[str, Any]) -> int:
        """How many palette entries one :meth:`bytes_per_entry` unit holds."""
        ...


class TilemapCodecPlugin(Plugin, Protocol):
    """The tilemap-side view interpretation: bytes ⇄ a :class:`CellGrid`.

    The third interpret engine, shaped like the pixel one and for the same
    reason. A tilemap file is a long run of fixed-width cells, so the engine is
    **buffer-relative and stateless** — hand it a window covering the visible
    rows and it decodes exactly that window, which is what allows a large map to
    be read a screen at a time rather than whole.

    ``decode`` takes bytes to a flat list of cells in the file's own order;
    laying that list out as a grid is the host's, since a file rarely states its
    own width (``docs/graphics-formats-reference/scgcad-formats.md`` §4) and the
    view has to be able to try one.

    :meth:`bytes_per_cell` sizes the byte window, mirroring
    :meth:`PixelCodecPlugin.bytes_per_tile`. :meth:`cell_tiles` is how many
    *tiles* one cell covers — 1x1 for a hardware BG map, 2x2 for a panel whose
    cells are 16x16 metatiles — which the renderer needs and cannot infer from
    the byte size.

    Unlike the other two interpret stages, ``encode`` is not the whole of the
    save side: the attributes a cell carries may not all fit the format it is
    being written back to. A codec that cannot represent a field must say so by
    dropping it in ``encode`` rather than raising — the alternative is a file
    that cannot be saved at all because one cell has a priority bit.
    """

    def decode(
        self, data: bytes, params: dict[str, Any], ctx: PipelineContext
    ) -> list[Cell]: ...

    def encode(
        self, cells: list[Cell], params: dict[str, Any], ctx: PipelineContext
    ) -> bytes: ...

    def bytes_per_cell(self, params: dict[str, Any]) -> int:
        """Byte size of one cell under ``params`` (for byte-window slicing)."""
        ...

    def cell_tiles(self, params: dict[str, Any]) -> tuple[int, int]:
        """How many tiles ``(across, down)`` one cell covers under ``params``."""
        ...

    def transform_cell(
        self, cell: Cell, op: CellOp, params: dict[str, Any]
    ) -> Cell | None:
        """``cell`` as ``op`` leaves it, or **None** when the format cannot say it.

        Optional, and the way a format declares what its cells can *do*. Which
        transforms a tilemap supports is not a property of tilemaps: a console BG
        entry has both mirror bits, a Game Boy map entry is a bare index with
        neither, and a stamp layout's word is a coordinate with no room for any.
        Only the codec knows which bits — if any — say a flip, so the tool names
        the operation and hands it here rather than toggling a field itself
        (``docs/design/tilemap-entry.md`` §4).

        **None is the whole refusal protocol.** The host probes with a blank cell
        before touching a selection, so a format that cannot do something is told
        to the user once, in the status bar, and nothing is written — rather than
        a bit being set in the model that :meth:`encode` then silently drops on
        save, which is what an unconditional flip does to an index-only format.

        A plugin that omits this method refuses every transform. That is the safe
        direction for the one it would get wrong: a codec written before the
        method existed cannot have been asked which of its fields a flip means.

        Supporting a *rotation* takes one more step than a flip, and it is not
        this method's: :class:`~celpix.core.tilemap.Cell` has no rotation field,
        because no format in hand has a rotation bit to put in one. A format that
        does would add the field, a preset entry to place it, and the tile-side
        turn in :func:`~celpix.pipeline.pipeline.tilemap_tiles`; this method is
        then the same shape it already is.
        """
        ...

    def index_limit(self, params: dict[str, Any]) -> int | None:
        """The highest ``index`` a cell can hold, or **None** if it has no index.

        Optional, and the way a format declares that a cell's *reference* can be
        set at all — the counterpart of :meth:`transform_cell` for the one field
        every tilemap has an opinion about. What the host does with it is bound a
        control to the format's own range rather than to a guess: a console BG
        entry spends 10 bits on the tile number and a stamp layout 14 on a panel
        coordinate, and a value too wide for the field would be masked down by
        :meth:`encode` into a cell nobody asked for.

        What the number *means* follows the document, not this method. On an
        ordinary map it is a tile in the bound source; on a chained one it is a
        position in the map being drawn through, so setting it **restamps** that
        cell (``docs/design/tilemap-entry.md`` §3.1). The codec is answering the
        same question either way: how wide is the field.

        A plugin that omits this method has its cell references left alone, which
        is the safe direction — a codec that was never asked where its index lives
        must not have one inferred for it. Returning None says the same thing for a
        format that genuinely has no index field to write.
        """
        ...

    def palette_row_limit(self, params: dict[str, Any]) -> int | None:
        """The highest ``palette_row`` a cell can hold, or **None** for none.

        :meth:`index_limit` for the colour field, and the same protocol for the
        same reason: a row too wide for the field would be masked down by
        :meth:`encode` into a cell nobody asked for. It is what lets the host
        *assign* a row to a selection — the tilemap reading of the pin gesture,
        which stores the row where the file already keeps it instead of in the
        project (``docs/design/palette-editing.md`` §3).

        Distinct from :meth:`has_palette_rows`, which is about **reading**: that
        one is taken as True when a plugin stays quiet, because a format carrying
        rows silently would otherwise be drawn with a view-wide row on top. This
        one is about **writing**, where the safe default is the other way round —
        a codec that was never asked where its row lives must not have one
        inferred and written into its bytes.
        """
        ...

    def has_palette_rows(self, params: dict[str, Any]) -> bool:
        """Whether this format gives a cell a palette row to name.

        Optional, and about the **format** rather than the file: a screen whose
        cells all sit on row 0 still answers True, because the format has the
        field and the zeros are its cells' answer. False is for a format with
        nowhere to put one — a Game Boy map entry is a bare tile number, and a
        converted screen kept only the low byte of each cell.

        What it decides is who picks the colours. Where a format has rows, the
        file has answered and the row travels in the indices; the view's
        subpalette is switched off there, because a second row on top would
        shift a map that is already in the colours it was authored in. Where a
        format has none, nothing has answered and the picture still has to be
        read under some row, so the subpalette control supplies it exactly as it
        does on a pixel document (``docs/design/tilemap-entry.md`` §8).

        A plugin that omits this method is taken to have rows. That is the safe
        direction of the two: a format that carries rows and stayed quiet would
        otherwise have a view-wide row added on top of its cells' own.
        """
        ...

    def has_line_flag(self, params: dict[str, Any]) -> bool:
        """Whether this format ends a line on a **bit the cell carries**.

        Optional, and the terminator's half of :meth:`has_palette_rows`: some
        text formats spend no code on a line break and set a bit on the line's
        last character instead (``docs/design/fontmap-entry.md`` §4). The
        alphabet has to know, because it decides whether a typed newline sets a
        bit on the cell before the caret or writes a cell of its own
        (:attr:`~celpix.core.font.FontAlphabet.flag_break`), and it is the
        **format's** answer rather than the font's — two streams in one game
        routinely share a font and punctuate differently.

        A plugin that omits this method is taken not to have one, which is the
        safe direction here: a break that costs a cell is writable in any
        format, where a bit inferred onto a format without one would be written
        into bytes that have no room for it.
        """
        ...


@dataclass(frozen=True)
class Preset:
    """A named, data-only interpretation: which engine to use and its parameters.

    The data-first tier for the View stage. A preset targets an ``engine_id`` (a
    registered pixel, color or tilemap codec) and supplies the ``params`` that
    engine interprets; ``stage`` says which pathway it belongs to.

    ``category`` is the heading the format picker files it under
    (:data:`CATEGORIES`). Presentation only — nothing in the pipeline reads it —
    and omitting it leaves the preset ungrouped at the top of the list.
    """

    id: str
    name: str
    stage: Stage  # one of the INTERPRET_* stages
    engine_id: str
    params: dict[str, Any] = field(default_factory=dict)
    category: str = ""
