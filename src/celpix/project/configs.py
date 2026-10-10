"""How an entry's bytes are read and written back: its pathway configs.

:func:`pixel_config_for` and :func:`tilemap_config_for` turn an
:class:`~celpix.project.entry.Entry` into the
:class:`~celpix.pipeline.pathway.PathwayConfig` the pipeline runs — a whole
file through its own container, a slice as a bounded window onto its parent's
buffer that writes through the parent (``docs/design/slices-and-parents.md``).
Beside them are the parent reads a slice goes through, the bytes an entry
shows (:func:`entry_view_bytes`), and the slice-length backfill a first load
performs. A composite's config is :mod:`celpix.project.composites`'.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from os.path import getsize
from typing import TYPE_CHECKING

from celpix.core.context import (
    KEY_COMPRESSED_SIZE,
    KEY_DECOMPRESS_COMPLETE,
    KEY_SOURCE_OFFSET,
    PipelineContext,
)
from celpix.core.errors import Stage
from celpix.pipeline import pipeline
from celpix.pipeline.pathway import PathwayConfig
from celpix.plugins.base import (
    DEFAULT_TILEMAP_PRESET,
    NO_COMPRESSION,
    NO_RESHAPE,
    PALETTE_PRESET_PARAM,
    PALETTE_SWATCH_ENGINE,
    FileRef,
)
from celpix.plugins.detect import resolved_container_id
from celpix.plugins.registry import Registry
from celpix.project.entry import (
    Entry,
    EntryKind,
)
from celpix.project.inputs import engine_id_of, resolve_inputs

if TYPE_CHECKING:
    from celpix.project.workspace import Workspace


def interpret_params_for(entry: Entry, preset_id: str, registry: Registry) -> dict:
    """The preset params ``entry`` lays over ``preset_id``'s own — the
    ``interpret_params`` of every pixel config built for it.

    One case today: a preset over the **palette-swatch** engine reads its bytes
    in whatever color format the entry's session names, so that format rides
    along as the engine's ``palette_preset_id``. Decided by the *engine* rather
    than the shipped preset's id, so a user's own preset over the same engine
    gets the picker too. A stored format this build hasn't got is left out
    rather than passed on: the engine would refuse the load outright, where its
    default reads the bytes as *something* and the picker shows what.

    Empty for every other format, which is what keeps this from being a cost to
    them: the merge downstream is a no-op on an empty dict.
    """
    session = entry.session
    if session is None or not registry.has_preset(preset_id):
        return {}
    if registry.preset(preset_id).engine_id != PALETTE_SWATCH_ENGINE:
        return {}
    wanted = session.palette_view_preset_id
    if not wanted or not registry.has_preset(wanted):
        return {}
    return {PALETTE_PRESET_PARAM: wanted}


def pixel_config_for(
    entry: Entry,
    preset_id: str,
    registry: Registry,
    workspace: Workspace | None = None,
) -> PathwayConfig:
    """The pixel pathway config that reads (and writes back) ``entry``.

    A slice needs no special pipeline machinery: it is an ordinary config whose
    source is a *bounded* FileRef into the parent — the container slices the
    region, the compression scheme unpacks it, and at save time the same bounds
    make the write splice into (and never overflow) the parent's slot.

    Pass ``workspace`` so a slice of a parent with **unsaved pixel edits** reads
    those edits rather than the stale bytes on disk (see the module docstring);
    without it — or with a clean/unloaded parent — the file is the source, which
    is the same thing. The rebase is what keeps that honest: the parent's buffer
    starts at its header skip, so it is handed over as ``data`` with a matching
    ``data_base``, leaving the slice's own ``offset`` file-absolute for reading,
    writing and the address display alike. A **nested** slice needs the workspace
    to be read at all: its bytes are always its parent slice's decoded buffer,
    reached through the chain above it (:func:`_parent_read`).

    A compression scheme that can be decoded but not re-encoded yields a config
    with ``write_enabled=False`` — the entry loads and views fine, it just can't
    be written back. A whole file adds the same rule for its **container**
    (``Entry.container_id``) — a slice does not go through one, for the reason
    given on that field — and both kinds apply it to their **reshape** and
    their **compression** (``Entry.reshape_id``, ``Entry.compression_id``),
    which either may carry: a file that is one compressed blob lifted out of a
    ROM decompresses whole on load and is re-packed whole on save. A stage
    whose plugin this build hasn't got at all is view-only too, and named in
    ``missing_plugins`` so the load can tell the user which one to install
    (:meth:`~celpix.plugins.registry.Registry.resolve_stage`).

    A slice is saved **through its parent** (``writes_through_parent``) rather
    than deposited at its own bounds, so its writability is its own stages'
    *and* its parent's — see the comment at the branch.

    A **composite** reads none of those stages: its bytes are already other
    entries' decompressed buffers, joined here rather than read from anywhere, so
    the config is an in-memory source over the assembled buffer with every byte
    stage on its pass-through
    (:func:`~celpix.project.composites.composite_config`).
    """
    if entry.kind is EntryKind.COMPOSITE:
        # Late: a composite's layout reads its pieces through this module.
        from celpix.project.composites import (  # noqa: PLC0415 — circular by nature
            composite_config,
        )

        return composite_config(entry, registry, workspace, preset_id=preset_id)
    # A palette file is a whole file like any other on this pathway: its
    # container cuts the colour words out of whatever frames them, and the
    # swatch codec reads those (``docs/design/palette-editing.md`` §2).
    # A slice's container is the plain-bytes default — it reads through its
    # parent's — so the one resolution serves every kind.
    resolved = entry.file_stages.resolve(registry)
    stages, writable, missing = resolved.stages, resolved.writable, resolved.missing
    if entry.kind in (EntryKind.FILE, EntryKind.PALETTE):
        own = _resolved_compression(entry, stages.compression_id, registry, workspace)
        return replace(stages, compression_id=own.compression_id).config(
            FileRef(entry.paths),
            preset_id,
            interpret_params=interpret_params_for(entry, preset_id, registry),
            write_enabled=writable and own.resolvable,
            missing_plugins=missing,
            inputs=own.inputs,
            input_problems=own.problems,
        )
    half = _slice_half(entry, preset_id, registry, workspace, stages.compression_id)
    return PathwayConfig(
        source=half.source,
        dest=half.dest,
        interpret_preset_id=preset_id,
        interpret_params=interpret_params_for(entry, preset_id, registry),
        reshape_id=stages.reshape_id,
        compression_id=half.compression_id,
        slot_fill=entry.slot_fill,
        write_enabled=writable and half.writable,
        writes_through_parent=half.through,
        missing_plugins=missing,
        inputs=half.inputs,
        input_problems=half.problems,
    )


@dataclass(frozen=True)
class _SliceHalf:
    """The half of a slice's config both pathways build alike: where it reads,
    where a write lands, its compression scheme and that scheme's inputs, and
    whether the slice's side of the save can be carried out."""

    source: FileRef
    dest: FileRef
    compression_id: str
    inputs: dict[Stage, dict[str, bytes | int | str]]
    problems: tuple[tuple[Stage, str, str], ...]
    writable: bool
    through: bool


def _slice_half(
    entry: Entry,
    preset_id: str,
    registry: Registry,
    workspace: Workspace | None,
    compression_id: str,
) -> _SliceHalf:
    """The shared half of :func:`pixel_config_for`'s and
    :func:`tilemap_config_for`'s slice branch."""
    read = _parent_read(entry, registry, preset_id, workspace)
    _fit_to_parent(entry, read)
    # **Every** slice is saved by splicing into the parent's buffer and writing
    # the parent, not by depositing at its own bounds. Under a reordering parent
    # that is the only thing that *can* work; everywhere else it is what keeps
    # the file whole - the parent's container gets to run its write half (repair
    # a checksum, re-wrap a header) over bytes that changed inside it, which a
    # splice around it silently skips.
    #
    # So the parent's own write is the thing this can fail on: a parent that
    # cannot save (a reshape with no unshape, a container with no write, a
    # plugin this build hasn't got) leaves the slice with nowhere to land. A
    # slice is part of the larger whole and cannot outrank it — and down a chain
    # of nested slices that is every link's, since each is saved through the one
    # above it. A tilemap slice is held to it as much as a pixel one: its cells
    # are bytes of the same file.
    own = _resolved_compression(entry, compression_id, registry, workspace)
    source = FileRef(
        # The parent's *whole* file list, not just the file the slice is named
        # after: a slice's offset addresses the parent's joined buffer, so
        # reading one chip of a several-chip region would put every offset past
        # the first chip somewhere else entirely.
        entry.paths,
        offset=entry.slice_offset,
        length=entry.slice_length,
        data=read.data,
        data_base=read.base,
    )
    # The slice's own bounds: a file position where the parent reads straight,
    # a position in its buffer where it reorders or is a slice. Either way they
    # bound the splice and the slot checks rather than naming a deposit —
    # `writes_through_parent` says the parent performs the delivery. Without a
    # workspace there is no parent to route through, and the config falls back
    # to depositing here (the factory's caller-beware form).
    return _SliceHalf(
        source=source,
        dest=_slice_dest(entry, source, read),
        compression_id=own.compression_id,
        inputs=own.inputs,
        problems=own.problems,
        writable=read.writable and own.resolvable,
        through=read.through,
    )


@dataclass(frozen=True)
class _ResolvedCompression:
    """An entry's own compression scheme with its inputs resolved: the scheme
    the config runs, the values it is handed, the problems the load reports,
    and whether the stage can honestly run at all."""

    compression_id: str
    inputs: dict[Stage, dict[str, bytes | int | str]]
    problems: tuple[tuple[Stage, str, str], ...]
    resolvable: bool


def _resolved_compression(
    entry: Entry,
    compression_id: str,
    registry: Registry,
    workspace: Workspace | None,
) -> _ResolvedCompression:
    """``compression_id`` as ``entry``'s config will run it, inputs resolved.

    Shared by a slice's config and a whole file's: either may decompress, and the
    scheme's inputs — a shared code table, an output size, how many parts a
    de-interleaved stream weaves — are resolved here because only the host can
    reach the parent's buffer and the other entries a binding names. A scheme
    whose inputs do not resolve is put on the pass-through, exactly as a scheme
    this build lacks is: the bytes show packed, Write greys out, and the notice
    says what to bind (``docs/design/plugin-inputs.md`` §4).
    """
    inputs, problems = _resolved_stage_inputs(
        entry, Stage.COMPRESSION, compression_id, registry, workspace
    )
    if problems:
        return _ResolvedCompression(NO_COMPRESSION, inputs, problems, False)
    return _ResolvedCompression(compression_id, inputs, problems, True)


@dataclass(frozen=True)
class _ParentRead:
    """What a slice's config takes from its parent: the bytes it reads, their
    base, whether the parent can carry a write out, and whether one is routed
    through it at all — and, for a slice that matches its parent's size, where
    the parent ends in the coordinates the slice's offset counts in (``None``
    for every other slice, or where nothing could say)."""

    data: bytes | None
    base: int
    writable: bool
    through: bool
    extent: int | None = None


def _parent_read(
    entry: Entry, registry: Registry, preset_id: str, workspace: Workspace | None
) -> _ParentRead:
    """The parent half of a slice's config, for both pathways.

    A slice of a **file** reads the files at its offset, or the parent's buffer
    where that is the only truth (:func:`_parent_view_bytes`), and is writable
    only as far as the file's own stages are.

    A **nested** slice always reads its parent slice's own decoded bytes, since
    its offset counts from byte 0 of them and names no file position at all. Its
    writability is the parent's *config*, which is the parent's own stages and,
    recursively, every link above — so the parent's config is built once here,
    with the workspace, and serves both questions: its read (when the parent has
    no document to borrow the bytes from) and its ``write_enabled``.

    A nested slice whose chain is broken
    (:meth:`~celpix.project.workspace.Workspace.chain_broken`) reads an **empty** buffer
    and cannot write: falling back to the file would read bytes at an offset that was
    never a file offset. The load funnel refuses it before this is reached; this is the
    backstop for everything else that builds a config.
    """
    parent = workspace.parent_of(entry) if workspace is not None else None
    if entry.parent_kind is not EntryKind.SLICE:
        reordered = parent is not None and reorders_bytes(parent, registry)
        data, base = _parent_view_bytes(entry, parent, reordered, registry, preset_id)
        writable = (
            parent is None
            or pixel_config_for(parent, preset_id, registry).write_enabled
        )
        extent = None
        if entry.match_parent:
            # Under a reordering parent the offset counts in its buffer, which
            # ends where the buffer does; anywhere else it is a file position,
            # and the region ends with the files — the bound the slice dialog
            # checks a typed length against.
            extent = (
                base + len(data)
                if reordered and data is not None
                else _region_size(entry.paths)
            )
        return _ParentRead(data, base, writable, parent is not None, extent)
    if parent is None or workspace is None or workspace.chain_broken(entry):
        return _ParentRead(b"", 0, False, True)
    parent_cfg = pixel_config_for(parent, preset_id, registry, workspace)
    data = own_bytes(parent)
    if data is None:
        data = pipeline.read_region(parent_cfg, registry)[0]
    return _ParentRead(data, 0, parent_cfg.write_enabled, True, len(data))


def _region_size(paths: tuple[str, ...]) -> int | None:
    """The joined size of ``paths`` on disk, or ``None`` where one cannot be
    stat'ed — a missing file, which the read reports in its own words."""
    try:
        return sum(getsize(path) for path in paths)
    except OSError:
        return None


def _fit_to_parent(entry: Entry, read: _ParentRead) -> None:
    """Re-measure a slice that matches its parent's size (:attr:`Entry.match_parent`).

    Run by both config factories on the parent half they just built, so the
    length is re-derived on every read rather than kept in step by whoever
    resized the parent: a file grown by the container dialog, a parent slice
    re-pointed or re-packed, a file rewritten on disk — each re-reads the slice
    through here, and there is no list of such events to fall behind.

    A parent that ends at or before the offset leaves the length alone: there is
    no positive length to give, and the reads that follow refuse the window in
    their own words (:func:`outside_parent`, ``pipeline._check_window``) rather
    than this inventing an empty slice.
    """
    extent = read.extent
    if not entry.match_parent or extent is None or extent <= entry.slice_offset:
        return
    entry.slice_length = extent - entry.slice_offset


def _slice_dest(entry: Entry, source: FileRef, read: _ParentRead) -> FileRef:
    """Where a slice's write is bounded: its own offset and length.

    A nested slice's carries the parent's buffer as well, where a slice of a file
    names only the files: the bounds are positions in that buffer, and a scheme
    that packs against the bytes before its stream (``KEY_SURROUND``) reads them
    from the destination — which for a nested slice is the parent's decoded
    bytes, never the file at an offset it does not name. Nothing deposits there
    either way (``writes_through_parent``).
    """
    if entry.parent_kind is EntryKind.SLICE:
        return source
    return FileRef(entry.paths, offset=entry.slice_offset, length=entry.slice_length)


def outside_parent(entry: Entry, cfg: PathwayConfig) -> bool:
    """Whether a nested slice's window runs off its parent slice's decoded bytes.

    Asked of the config, which carries the very buffer the read is about to cut
    (:func:`_parent_read`). The window was inside it when the slice was made —
    the dialog checks — but the buffer is a decode, and what it unpacks to moves
    with the parent: re-pointed, resized, or its stream edited underneath. The
    read itself cannot say so, since cutting a buffer past its end is an empty
    or a short window rather than an error, and a slice opening on bytes it was
    never cut from is worse than one that will not open.

    False for a slice of a file, whose window is the container's to honour, and
    for a length still to be discovered, which reads to the end of what is there.
    """
    if entry.parent_kind is not EntryKind.SLICE or cfg.source.data is None:
        return False
    size = len(cfg.source.data)
    start = entry.slice_offset
    length = entry.slice_length
    return start >= size or (length is not None and start + length > size)


def own_bytes(entry: Entry) -> bytes | None:
    """The bytes ``entry``'s loaded document holds as its **own**, or None.

    What a nested slice reads and what its fold is spliced into, so the two can
    never disagree about which buffer that is. A pixel document's own bytes are
    its ``pixel_data``; a **tilemap**'s are its cells' buffer (``tilemap_data``),
    since its ``pixel_data`` is art borrowed from whatever it is bound to and has
    nothing to do with the region the entry is. None without a document — the
    caller reads the region instead.
    """
    doc = entry.doc
    if doc is None:
        return None
    return doc.tilemap_data if doc.is_tilemap else doc.pixel_data


def tilemap_config_for(
    entry: Entry,
    preset_id: str,
    registry: Registry,
    workspace: Workspace | None = None,
) -> PathwayConfig:
    """The tilemap pathway config that reads (and writes back) ``entry``'s cells.

    :func:`pixel_config_for`'s rule applied to the other pathway, and it has to
    be: **a slice carries no container of its own**, so its offset only names the
    right bytes in the *parent's* buffer — the one the parent's container already
    produced. Built as a plain window onto the files instead, a map inside a
    container whose offsets do not survive its read (:func:`reorders_bytes`) is
    handed the wrapper's bytes at the slice's offset. Those decode as perfectly
    ordinary cells and draw as noise, which is the failure this exists to stop:
    a `.d88` floppy strips a 0x2B0 header *and* a 16-byte ID before every sector,
    so nothing after the first sector is where the file says it is.

    Whole-file tilemaps keep their own container, exactly as on the pixel side —
    there is no parent to read through, and the file's own read is the answer.
    """
    # The cell engine's inputs — a sprite mapping's parallel arrays — resolved
    # as the pixel side resolves a scheme's. With a problem the entry is read
    # with the stage's **default preset** instead and goes view-only, the same
    # degraded opening a missing preset gets: the cells draw as something rather
    # than nothing, and the notice says which input to bind.
    inputs, problems = _resolved_stage_inputs(
        entry,
        Stage.INTERPRET_TILEMAP,
        engine_id_of(registry, preset_id),
        registry,
        workspace,
    )
    writable = not problems
    if problems:
        preset_id = DEFAULT_TILEMAP_PRESET
    # FILE alone, where the pixel side also takes a PALETTE: a palette's content
    # kind is PIXELS (its swatches), so it never reaches a tilemap read.
    if entry.kind is EntryKind.FILE:
        # A map that is one compressed blob of its own: unpacked whole on load
        # and re-packed whole by the save, on the pixel side's rule.
        resolved = entry.file_stages.resolve(registry)
        own = _resolved_compression(
            entry, resolved.stages.compression_id, registry, workspace
        )
        return replace(resolved.stages, compression_id=own.compression_id).config(
            FileRef(entry.paths),
            preset_id,
            write_enabled=writable and resolved.writable and own.resolvable,
            missing_plugins=resolved.missing,
            inputs={**inputs, **own.inputs},
            input_problems=own.problems + problems,
        )
    # Resolved as the pixel side resolves them: a reshape or scheme this build
    # lacks reads as its pass-through, view-only and named in the notice,
    # rather than failing the read outright.
    resolved = entry.file_stages.resolve(registry)
    half = _slice_half(
        entry, preset_id, registry, workspace, resolved.stages.compression_id
    )
    return PathwayConfig(
        source=half.source,
        # The slice's own bounds bound the splice, and the parent performs the
        # delivery — the same routing a pixel slice gets, so a restamp lands
        # where the cells were read from rather than at a raw file position.
        dest=half.dest,
        interpret_preset_id=preset_id,
        reshape_id=resolved.stages.reshape_id,
        compression_id=half.compression_id,
        slot_fill=entry.slot_fill,
        write_enabled=writable and resolved.writable and half.writable,
        writes_through_parent=half.through,
        missing_plugins=resolved.missing,
        inputs={**inputs, **half.inputs},
        input_problems=half.problems + problems,
    )


def _resolved_stage_inputs(
    entry: Entry,
    stage: Stage,
    plugin_id: str,
    registry: Registry,
    workspace: Workspace | None,
) -> tuple[
    dict[Stage, dict[str, bytes | int | str]], tuple[tuple[Stage, str, str], ...]
]:
    """``stage``'s resolved inputs in the config's shape, and its problems as
    the ``(stage, summary, detail)`` notices the load will say.

    Empty on both counts for a plugin that declares nothing, which is nearly
    every shipped format — so a config for one is byte-identical to what it was
    before inputs existed.
    """
    if not plugin_id:
        return {}, ()
    resolved = resolve_inputs(entry, stage, plugin_id, registry, workspace)
    if not resolved.values and not resolved.problems:
        return {}, ()
    return (
        {stage: resolved.values},
        tuple((stage, p.summary, p.detail) for p in resolved.problems),
    )


def reorders_bytes(entry: Entry, registry: Registry) -> bool:
    """Does reading ``entry`` move its bytes about, so its positions are not file
    offsets?

    True for an active reshape, and for a container that **permutes** rather than
    merely skipping (:attr:`~celpix.plugins.base.PluginInfo.preserves_offsets`) —
    a ``.smd``, an interleaved SNES image, a byte-swapped N64 dump. In both cases
    the entry's buffer is the ROM as the machine addresses it and the file is a
    scrambled encoding of that, so a position in one names nothing in the other.
    True as well for a file that **decompresses whole**: its buffer is the
    unpacked stream, which the file holds no byte of.

    Everything that resolves an offset against this entry's coordinates keys off
    this: a slice of it has to read (and cannot write) through its buffer, and so
    does an Offset palette (``docs/design/palette-editing.md`` §2). A plugin the
    registry no longer has reads as plain bytes, which preserve positions.
    """
    if registry.resolve_stage(Stage.RESHAPE, entry.reshape_id)[0] != NO_RESHAPE:
        return True
    if (
        registry.resolve_stage(Stage.COMPRESSION, entry.compression_id)[0]
        != NO_COMPRESSION
    ):
        return True
    plugin = registry.plugin(
        Stage.CONTAINER, resolved_container_id(registry, entry.container_id)
    )
    return not plugin.info.preserves_offsets


def entry_view_bytes(
    entry: Entry,
    registry: Registry,
    preset_id: str,
    workspace: Workspace | None = None,
) -> tuple[bytes, int]:
    """``entry``'s view buffer and the file offset its first byte sits at.

    The single definition of "what this entry shows": its live document's
    **own** bytes when one is loaded (:func:`own_bytes` — a tilemap's cells,
    never the art it borrows from its bound bank), else the region read fresh
    through its own container, reshape and decompressor (``pipeline.read_region``
    — the preset is inert, the read stops before any codec runs, so the pixel
    config reads a tilemap's region exactly as its own pathway would). The base
    follows the document's :attr:`~celpix.core.document.Document.anchor_base`
    rule whether or not one exists — :attr:`~celpix.core.document.Document.
    tilemap_anchor_base` for a tilemap's cells: what Read **recorded** where the
    buffer is the file's bytes — only the container knows where it actually
    began (past a copier header, past the iNES header and PRG banks) — and 0
    under a reshape or a decompressor, whose buffer is a different address space
    from the file, so that an offset written down against it is the same number
    the view shows.

    Everything that resolves an offset in this entry's coordinates reads through
    this — a slice of a reordering parent, an Offset palette — so they can never
    disagree with the view about what the bytes at an offset are.

    One exception, and it is deliberate: a **tilemap's bound tiles** apply the
    same rule in their own function
    (:meth:`~celpix.ui.main_window.bindings.BindingsMixin._live_bound_tiles`),
    because they need the read's context and its tile geometry as well as its
    bytes, and this returns neither. The two must move together.
    """
    doc = entry.doc
    if doc is not None:
        if doc.is_tilemap:
            return doc.tilemap_data, doc.tilemap_anchor_base
        return doc.pixel_data, doc.anchor_base
    cfg = pixel_config_for(entry, preset_id, registry, workspace)
    data, ctx = pipeline.read_region(cfg, registry)
    return data, ctx.get(KEY_SOURCE_OFFSET, 0) if cfg.reads_raw_bytes else 0


def _parent_view_bytes(
    entry: Entry,
    parent: Entry | None,
    reordered: bool,
    registry: Registry,
    preset_id: str,
) -> tuple[bytes | None, int]:
    """The parent bytes a slice of a **file** reads through, and the file offset
    they start at. (A nested slice has no choice to make: it always reads its
    parent slice's buffer — :func:`_parent_read`.)

    ``(None, 0)`` — read the files — whenever the parent's own Read is a plain
    window onto them, because then the slice's offset lands on the same bytes
    either way. Two things make the parent's buffer the only correct source:

    - **A parent that reorders** (``reordered``, from :func:`reorders_bytes`),
      or decompresses whole. Then the file simply does not hold the bytes the
      slice's offset names, dirty or not — so the buffer is read even when the
      parent has no document of its own (it is re-read for this), since falling
      back to disk would quietly hand back a scrambled region.
    - **Unsaved pixel edits.** A dirty parent's live bytes are what the slice is a
      view of; the file still holds the old ones. A dirty *palette* doesn't
      qualify — it lives on the other pathway and in another file, so it says
      nothing about these bytes.

    Either way the slice must fall inside the parent's window: one anchored
    before whatever the parent's container skipped isn't in that buffer at all,
    so it reads from disk (and under a reorder those bytes are no one's to name).
    """
    if parent is None:
        return (None, 0)
    if not (reordered or (parent.doc is not None and parent.pixel_dirty)):
        return (None, 0)
    # Both cases want exactly what the parent's own view shows, which is the one
    # definition of that; with a loaded document it costs no read.
    data, base = entry_view_bytes(parent, registry, preset_id)
    return (data, base) if entry.slice_offset >= base else (None, 0)


def backfill_slice_length(entry: Entry, ctx: PipelineContext) -> bool:
    """Fill in a decompressed slice's extent discovered at load; True if it did.

    A slice created without a length ("decompress from here, wherever it ends")
    reads to end-of-file, and the decompressor reports the structure's true
    byte extent in the context. Recording that extent onto the entry bounds
    every later load — and, crucially, makes save-back slot-enforced. Only a
    *complete* decompress counts: a truncated/partial extent would bound the
    slice at the wrong size.

    **Never under an active reshape.** The discovered extent is measured in
    *reshaped* space, and re-bounding the window changes the region's length —
    which changes the permutation itself, so the slice would decode differently
    after discovery than during it. The slice dialog requires an explicit
    length whenever a reshape is chosen, so this guard is its backstop.
    """
    if entry.kind is not EntryKind.SLICE or entry.slice_length is not None:
        return False
    if entry.reshape_id != NO_RESHAPE:
        return False
    consumed = ctx.get(KEY_COMPRESSED_SIZE)
    if not consumed or not ctx.get(KEY_DECOMPRESS_COMPLETE):
        return False
    entry.slice_length = consumed
    return True
