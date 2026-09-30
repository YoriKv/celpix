"""Where a palette is read from when it is not the entry's own.

The cross-entry palette sources — an Offset palette read out of another entry's
bytes (:func:`offset_palette_source`), an ENTRY palette taken from another
entry's resolved buffer (:func:`entry_palette_source`), an external palette file
(:func:`file_palette_config`) and an emulator state
(:func:`emulator_palette_config`) — plus the placeholder a document draws on
before any of them is applied (:func:`fallback_palette`). Each answers with a
read window or a pathway config; :mod:`celpix.project.documents` decides which
one an entry uses (``docs/design/palette-editing.md``).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from celpix.core.errors import Pathway, PipelineError, Stage
from celpix.core.palette import FULL_PALETTE_COUNT, Palette
from celpix.pipeline import pipeline
from celpix.pipeline.pathway import PathwayConfig
from celpix.plugins.base import DEFAULT_PIXEL_PRESET, NO_RESHAPE, FileRef
from celpix.plugins.registry import Registry
from celpix.project.workspace import (
    Entry,
    EntryKind,
    PaletteSource,
    Workspace,
    can_supply_palette,
    composite_preset_id,
    entry_view_bytes,
    reorders_bytes,
)

if TYPE_CHECKING:
    from celpix.core.emustate import StateFormat


def fallback_palette() -> Palette:
    """The generated palette shown until a real one is loaded — full length.

    Sized to the whole 256 rather than one palette row's worth: the generator
    puts a contrasting row first, a **grayscale ramp second** and distinct
    colors after, none of which exists at all if only the format's index
    space is asked for (a 4bpp view would stop at 16 — one row, no ramp).
    At full length every palette row the row spin can reach is populated, so
    single-channel data can be read as a ramp by stepping to row 1, and
    forking Default → Custom keeps the palette exactly the size it was.
    """
    return Palette.default(FULL_PALETTE_COUNT)


def placeholder_palette_config(preset_id: str) -> PathwayConfig:
    """The no-palette-loaded config: empty source, never written back."""
    return PathwayConfig(
        source=FileRef(""), interpret_preset_id=preset_id, write_enabled=False
    )


def palette_entry_target(
    workspace: Workspace, entry: Entry, source: Entry | None
) -> Entry | None:
    """The open entry an ENTRY palette names, or None when it names nothing usable.

    :func:`~celpix.project.declarations.binding_target`'s twin, and the same
    two questions in the same order: the entry has to still be **open** — which
    holding it by identity cannot answer, a closed entry being a live object
    undo may yet put back — and it has to be something this consumer may read
    (:func:`~celpix.project.workspace.can_supply_palette`). Scanned by identity
    rather than with ``in``, which would ask :class:`Entry` for an equality it
    deliberately does not have.
    """
    if source is None:
        return None
    if not any(open_ is source for open_ in workspace.entries):
        return None
    return source if can_supply_palette(entry, source) else None


# ---------------------------------------------------------------------------
# Offset palettes, in the owning file's coordinates (docs/design/palette-editing.md §2)


def palette_offset_owner(workspace: Workspace, entry: Entry | None) -> Entry | None:
    """The FILE entry whose coordinates ``entry``'s Offset palette is in.

    ``entry`` itself when it is a whole file; the file its chain ends at when it
    is a slice, because a slice's palette offsets are file-absolute and
    deliberately reach outside its own window — a nested slice's included, since
    colours live in the ROM beside a compressed stream rather than inside it.
    ``None`` when that file is not open.

    A **composite** comes from no file, so it borrows the coordinates of its
    first piece that has an entry — the file a VRAM window's first bank sits in,
    which is where a ROM keeps the colours for it. ``None`` for a composite of
    pads only. Reordering the pieces across files moves the owner with them.
    """
    if entry is None:
        return None
    if entry.kind in (EntryKind.FILE, EntryKind.PALETTE):
        return entry
    if entry.kind is EntryKind.COMPOSITE:
        first = next((p.entry for p in entry.pieces if p.entry is not None), None)
        return palette_offset_owner(workspace, first) if first is not entry else None
    return workspace.root_of(entry)


def offset_palette_files(workspace: Workspace, entry: Entry) -> tuple[str, ...]:
    """The files ``entry``'s Offset palette offsets address: the owner's, or its
    own when the parent is not open — which is that same list, since a slice
    carries the parent's files."""
    owner = palette_offset_owner(workspace, entry)
    return owner.paths if owner is not None else entry.paths


def reordered_view(
    registry: Registry,
    workspace: Workspace,
    owner: Entry,
    settle: Callable[[Entry], None] | None = None,
    fallback_preset: str = DEFAULT_PIXEL_PRESET,
) -> tuple[bytes, int] | None:
    """``owner``'s view buffer and its base, or ``None`` when reading the file
    would give the same bytes. A permuting container or an active reshape makes
    the buffer a different address space from the file, and then the buffer is
    the only place an offset means anything."""
    if not reorders_bytes(owner, registry):
        return None
    if settle is not None:
        settle(owner)
    return entry_view_bytes(
        owner,
        registry,
        owner.session.pixel_preset_id if owner.session is not None else fallback_preset,
        workspace,
    )


def offset_palette_space(
    registry: Registry,
    workspace: Workspace,
    entry: Entry,
    settle: Callable[[Entry], None] | None = None,
    fallback_preset: str = DEFAULT_PIXEL_PRESET,
) -> tuple[tuple[bytes, int] | None, int]:
    """The address space ``entry``'s Offset palette reads: ``(view, end)``.

    ``view`` is the owner's ``(buffer, base)`` when it reorders bytes, else
    ``None``, meaning offsets are file offsets into the joined files. ``base``
    mirrors the owner's own anchor: under an active reshape offsets are 0-based
    buffer positions; under a permuting container they keep the recorded start.
    """
    owner = palette_offset_owner(workspace, entry)
    view = (
        reordered_view(registry, workspace, owner, settle, fallback_preset)
        if owner is not None
        else None
    )
    if view is not None:
        data, base = view
        if owner is not None and owner.reshape_id != NO_RESHAPE:
            base = 0
        return (data, base), base + len(data)
    paths = offset_palette_files(workspace, entry)
    return None, sum(Path(p).stat().st_size for p in paths)


def _palette_window_bytes(avail: int, preset_id: str, registry: Registry) -> int:
    """How many of ``avail`` bytes a cross-entry palette read takes: floored to
    whole colour entries — the codecs reject a partial trailing one — and capped
    at a full palette. 0 when not even one entry fits."""
    colors = min(
        FULL_PALETTE_COUNT, pipeline.palette_entry_capacity(avail, preset_id, registry)
    )
    if colors <= 0:
        return 0
    return pipeline.palette_read_bytes(colors, preset_id, registry)


def offset_palette_source(
    registry: Registry,
    workspace: Workspace,
    entry: Entry,
    byte_off: int,
    preset_id: str,
    settle: Callable[[Entry], None] | None = None,
    fallback_preset: str = DEFAULT_PIXEL_PRESET,
) -> tuple[FileRef | None, bool]:
    """The read window for an Offset palette at ``byte_off``, and whether a color
    edit can be written back through it.

    Floored to whole entries — the color codecs reject a partial trailing one —
    and capped at a full palette. ``(None, ...)`` when not even one entry fits.
    Where the owner reorders bytes the window is cut from its view buffer and the
    pathway comes back write-off: a length-bounded ``FileRef`` cannot say where a
    permuted splice belongs.
    """
    view, end = offset_palette_space(
        registry, workspace, entry, settle, fallback_preset
    )
    writable = view is None
    base = 0 if view is None else view[1]
    avail = end - byte_off if byte_off >= base else 0
    length = _palette_window_bytes(avail, preset_id, registry)
    if not length:
        return None, writable
    paths = offset_palette_files(workspace, entry)
    if view is None:
        return FileRef(paths, offset=byte_off, length=length), True
    data, base = view
    return FileRef(
        paths, offset=byte_off, length=length, data=data, data_base=base
    ), False


def source_view_preset(
    source: Entry,
    registry: Registry,
    fallback_preset: str = DEFAULT_PIXEL_PRESET,
) -> str:
    """The pixel format ``source``'s own buffer is read at, for a cross-entry read.

    Its session's, as the entry on screen is read. A **composite** the user has
    never opened has no session yet and cannot take the caller's format either:
    its buffer is *assembled* at a pixel format — un-ranged pieces are rounded up
    to a whole tile of it (:func:`~celpix.project.workspace.composite_layout`) —
    so reading one at the stage default would shift every byte after a ragged
    piece. Its seed is the same answer the view it is about to get will take
    (:func:`~celpix.project.workspace.composite_preset_id`), which is what keeps
    the colours a consumer reads out of it from changing the moment it is opened.
    """
    session = source.session
    if session is not None:
        return session.pixel_preset_id
    if source.kind is EntryKind.COMPOSITE:
        return composite_preset_id(source, registry)
    return fallback_preset


def entry_palette_source(
    registry: Registry,
    workspace: Workspace,
    source: Entry,
    byte_off: int,
    preset_id: str,
    settle: Callable[[Entry], None] | None = None,
    fallback_preset: str = DEFAULT_PIXEL_PRESET,
) -> FileRef | None:
    """The read window an ENTRY palette takes out of ``source``'s resolved bytes.

    :func:`offset_palette_source`'s twin for the other cross-entry palette
    reference, sized by the same two rules — floored to whole colour entries,
    because the codecs reject a partial trailing one, and capped at a full
    palette. ``None`` when not even one entry fits.

    ``byte_off`` indexes the source's **resolved** data from 0 — what it looks
    like once its own container, reshape and decompressor have run, which is the
    same space a composite piece's range addresses
    (:class:`~celpix.project.workspace.CompositePiece`). The bytes ride on the
    ref inline, so the read never touches disk and never runs a second set of
    byte stages over a buffer that has already been through its own.

    The window is **not** writable on its own account: the colours belong to the
    source's bytes, and an edit rides that entry's pixel pathway exactly as a
    reordered Offset palette rides its owner's
    (``docs/design/palette-editing.md``).

    ``settle`` pays whatever fold the source owes before its buffer is believed,
    the way :func:`reordered_view` does for an Offset owner.
    """
    if settle is not None:
        settle(source)
    data, _base = entry_view_bytes(
        source,
        registry,
        source_view_preset(source, registry, fallback_preset),
        workspace,
    )
    avail = len(data) - byte_off if byte_off >= 0 else 0
    length = _palette_window_bytes(avail, preset_id, registry)
    if not length:
        return None
    return FileRef(
        # A composite comes from no file, so its own name is what the address
        # display and any error message have to call this buffer.
        source.paths or (source.name or "entry",),
        offset=byte_off,
        length=length,
        data=data,
        data_base=0,
    )


def entry_palette_config(
    entry: Entry,
    source: PaletteSource,
    registry: Registry,
    workspace: Workspace,
    settle: Callable[[Entry], None] | None = None,
    preset_id: str | None = None,
) -> PathwayConfig:
    """The pathway ``entry`` reads its ENTRY-mode palette through.

    One builder for every route into the mode — the project restore, the headless
    load and the dock's own re-decode — so what a reopened project shows and what
    the gesture produced cannot differ. Raises where the app degrades to the
    default palette: a source that is no longer open, or one with too few bytes
    left at the stated offset for a single colour.

    The **consumer** owns the colour format, which is the whole difference from
    File mode: these are bytes in somebody else's buffer, and how to read them is
    a fact about the picture being coloured rather than about the entry holding
    them. *Which* of the consumer's two answers is the live one depends on the
    route, so ``preset_id`` is explicit rather than always the session's: a
    loaded entry's format is on its **document's** palette config, and its
    session's copy is only written on an entry switch — so the entry on screen
    would otherwise be re-decoded at whatever format it was opened with, silently
    undoing a Format pick. The session's is right for the restore route alone,
    where there is no live config yet.
    """
    session = entry.session
    assert session is not None
    wanted = preset_id or session.palette_preset_id
    target = palette_entry_target(workspace, entry, source.entry)
    if target is None:
        named = source.entry.name if source.entry is not None else "nothing"
        raise PipelineError(
            Stage.CONTAINER,
            Pathway.PALETTE,
            f"the palette is read from {named}, which is not open",
            plugin=wanted,
        )
    ref = entry_palette_source(
        registry, workspace, target, source.offset, wanted, settle
    )
    if ref is None:
        raise PipelineError(
            Stage.CONTAINER,
            Pathway.PALETTE,
            f"not enough data at that offset in {target.name}",
            plugin=wanted,
        )
    # Never writable on its own account: the bytes are the source's, so a colour
    # edit rides that entry's pixel pathway instead
    # (``docs/design/palette-editing.md``).
    return PathwayConfig(source=ref, interpret_preset_id=wanted, write_enabled=False)


def file_palette_config(
    path: str, offset: int, preset_id: str, container_id: str
) -> PathwayConfig:
    """The writable pathway a PALETTE entry reads and writes its ``.pal`` with.

    Source and dest are the same file; ``container_id`` is what cuts the colors
    out of a file that holds more than colors, and re-wraps them on the way back.
    """
    return PathwayConfig(
        source=FileRef(path, offset=offset),
        dest=FileRef(path, offset=offset),
        interpret_preset_id=preset_id,
        container_id=container_id,
    )


def emulator_palette_config(
    path: str, registry: Registry
) -> tuple[StateFormat, PathwayConfig]:
    """Detect the emulator state at ``path``: ``(format, palette config)``.

    The console dictates the codec, so this cannot be set to the wrong colour
    format. View-only: a state is a memory dump, never a palette written back.
    Raises :class:`~celpix.core.emustate.StateError` when nothing is recognised.
    """
    from celpix.core import emustate  # noqa: PLC0415 — only the emulator mode needs it

    data = Path(path).read_bytes()
    fmt, region = emustate.locate_palette(data)
    length = min(
        len(region.data),
        pipeline.palette_read_bytes(region.count, region.preset_id, registry),
    )
    ref = FileRef(path, offset=0, length=length, data=region.data)
    return fmt, PathwayConfig(
        source=ref, interpret_preset_id=region.preset_id, write_enabled=False
    )
