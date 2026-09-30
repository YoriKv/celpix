"""What the open-entries list says about an entry, and the fixes it offers.

Whether an entry's files and palette are there (:func:`data_missing`,
:func:`palette_missing`, with :func:`one_disk_scan` answering a whole repaint
from one look at the disk), whether it loaded (:func:`load_failed`), the notices
its row carries (:func:`entry_notices`), and the repairs a project needs when a
format or a file has gone — :func:`repair_presets`, :func:`relocate_path`,
:func:`retarget_files`. Also the names an entry is exported and sliced under
(:func:`export_basename`, :func:`default_slice_name`) and where its palette
reads from (:func:`palette_source_for`).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from os.path import basename, exists, splitext
from typing import TYPE_CHECKING

from celpix.core.address import format_hex
from celpix.core.document import Document
from celpix.core.errors import Stage
from celpix.core.notices import Notice, NoticeLevel, notices
from celpix.plugins.base import (
    NO_COMPRESSION,
    NO_RESHAPE,
)
from celpix.plugins.registry import Registry
from celpix.project.entry import (
    Entry,
    EntryKind,
    LoadFailure,
    PaletteMode,
    PaletteSource,
    anchor_kind,
)

if TYPE_CHECKING:
    from celpix.project.workspace import Workspace


def palette_source_for(entry: Entry) -> PaletteSource | None:
    """The entry's live palette as restorable plain data — ``None`` for default.

    Derived from the loaded document (its palette config is the truth for the
    file/offset modes) plus the session's mode; a never-activated entry has no
    live state, so its pending source (if any) is returned as-is. This is the
    inverse of :meth:`_apply_restored_state`'s consumption of ``pending_palette``
    — it's what both project-save and new-slice seeding read to carry a palette
    forward. An offset source is an absolute file offset, so it resolves against
    the file a slice's chain ends at exactly as it does for that file itself.
    """
    # A palette entry's palette is its own file, decoded from its own bytes and
    # rebuilt from them on every load: nothing about it is restorable state, and
    # carrying a File-mode source naming its own path forward would have the
    # next load mirror the entry onto itself, write-disabled.
    if entry.kind is EntryKind.PALETTE:
        return None
    # A degraded palette (its file went missing) keeps its intended source here
    # rather than on the live config, so save and new-slice seeding carry the
    # reference forward even while the entry renders on the default palette.
    if entry.missing_palette is not None:
        return entry.missing_palette
    if entry.doc is None or entry.session is None:
        return entry.pending_palette
    mode = entry.session.palette_mode
    source = entry.doc.palette_config.source
    if mode is PaletteMode.CUSTOM:
        # The custom palette *is* the project data — there is no file behind it,
        # so the colors themselves are what round-trips.
        return PaletteSource(colors=list(entry.doc.palette.colors))
    if mode is PaletteMode.FILE:
        return PaletteSource(path=source.path, offset=source.offset)
    if mode is PaletteMode.OFFSET:
        return PaletteSource(offset=source.offset)
    if mode is PaletteMode.ENTRY:
        # The object, not a path: a composite source has none, and the position
        # a project stores is computed at save time and nowhere else.
        return PaletteSource(entry=entry.palette_entry, offset=source.offset)
    if mode is PaletteMode.EMULATOR:
        # Only the state file's path is stored; where the palette sits inside it
        # (and which console codec decodes it) is re-detected on restore, so a
        # newer detector or an edited state stays authoritative over stale coords.
        return PaletteSource(path=source.path)
    return None


# -- missing-reference handling (docs/design/project-format.md §3) ---------
#: The answers :func:`path_exists` is serving, while a scan is open; ``None``
#: outside one, which is the ordinary state.
_scanned: dict[str, bool] | None = None


@contextmanager
def one_disk_scan() -> Iterator[None]:
    """Stat each referenced path at most once for the length of one operation.

    Three separate readers ask whether a file is still there — the loader
    resolving a stored path, every row of the Files list drawing its warning, and
    :func:`missing_paths` arming Locate — and on a project of several hundred
    entries that is thousands of stats over a few hundred distinct paths. Where
    those paths live on a network share, or on a Windows drive seen through WSL,
    a stat costs milliseconds and the redundancy is the whole of the wait.

    What the scan asserts is only that **the disk does not change under one
    operation**, so asking six times cannot be more accurate than asking once. It
    deliberately does *not* span an operation that changes what is on disk — the
    relocation walk above all, which is exactly a user putting files back where
    the app can see them. Outside a scan nothing is remembered, which is what
    keeps an ordinary row repaint able to notice a file that has just gone.

    Nests: an inner scope shares the outer one's answers rather than starting
    over, so a helper may open a scan of its own without knowing who called it.
    """
    global _scanned
    outer = _scanned
    if outer is None:
        _scanned = {}
    try:
        yield
    finally:
        _scanned = outer


def path_exists(path: str) -> bool:
    """Whether ``path`` is on disk — from the open scan, if there is one.

    ``os.path.exists`` outside a scan, and the reason every missing-file question
    in this module goes through here rather than calling it directly
    (:func:`one_disk_scan`).
    """
    scanned = _scanned
    if scanned is None:
        return exists(path)
    answer = scanned.get(path)
    if answer is None:
        answer = scanned[path] = exists(path)
    return answer


def data_missing(entry: Entry) -> bool:
    """Whether any of the entry's data files is gone from disk.

    For a slice or bookmark this is the root file (their ``path``, at any depth
    of nesting); a missing parent leaves the child unloadable exactly as a
    missing file does. **Any**
    of a several-file region counts: the region is the files joined, so one
    absent chip does not shorten it, it moves every byte after the gap.
    """
    return any(not path_exists(path) for path in entry.paths)


def entry_palette_path(entry: Entry) -> str | None:
    """The external palette-source file the entry references, or ``None``.

    Only file/emulator modes have an external palette file, and its path is read
    from wherever the entry currently keeps it: the degraded source (loaded, but
    its file went missing), the live document config (loaded and healthy), or
    the pending source (not yet activated).
    """
    session = entry.session
    if session is None or not session.palette_mode.has_external_file:
        return None
    if entry.kind is EntryKind.PALETTE:
        return None  # its palette file is its own file, already in ``paths``
    if entry.missing_palette is not None:
        return entry.missing_palette.path
    if entry.doc is not None:
        return entry.doc.palette_config.source.path or None
    if entry.pending_palette is not None:
        return entry.pending_palette.path
    return None


def entry_palette_entry(entry: Entry) -> Entry | None:
    """The entry ``entry``'s ENTRY-mode palette is decoded from, or ``None``.

    :func:`entry_palette_path`'s twin for the other cross-entry reference, and
    read from wherever the entry currently keeps it for the same three reasons:
    the degraded source (loaded, but its source has been closed), the live
    binding (loaded and healthy), or the pending source (not yet activated).

    Deliberately **not** gated on ``session.palette_mode``, where the path twin
    is: a session's mode is only written on an entry switch, so the graphic on
    screen — the one whose source is most likely to be closed or edited out from
    under it — would answer for the mode it had when it was opened. The binding
    itself is the live fact, and it is cleared wherever the palette moves
    somewhere else (:meth:`~celpix.ui.main_window.palette_source.
    PaletteSourceMixin._apply_palette_state`).
    """
    if entry.missing_palette is not None and entry.missing_palette.entry is not None:
        return entry.missing_palette.entry
    if entry.palette_entry is not None:
        return entry.palette_entry
    if entry.pending_palette is not None:
        return entry.pending_palette.entry
    return None


def load_failed(entry: Entry) -> LoadFailure | None:
    """The failure keeping ``entry`` without a document; None while it holds one.

    The mark is only meaningful on an entry with no document: a re-read that
    failed and put the previous document back leaves the entry working, and a
    working entry is never inert or red whatever a stale mark says. Read through
    this rather than the field, so the sites that restore a document cannot
    strand a mark that anything acts on.
    """
    return entry.load_failure if entry.doc is None else None


def unavailable(entry: Entry) -> bool:
    """Whether activating ``entry`` shows the inert state rather than a document.

    The two causes the window treats alike: its file (or its parent's) is gone
    (:func:`data_missing`), or its last load failed and nothing about it has
    changed since (:func:`load_failed`). Which one it is, the row and the status
    line say; that it is one of them is all an activation needs to know.
    """
    return data_missing(entry) or load_failed(entry) is not None


def palette_missing(entry: Entry) -> bool:
    """Whether the palette source the entry references can no longer be reached.

    An external file that has gone from disk, or — for an ENTRY palette — a
    source entry that has been closed. The second is recorded rather than
    probed: ``missing_palette`` is set only where a restore degraded, so a
    source naming an entry *is* the "it was not open" answer, and asking the
    list again would need a workspace this module-level question has not got.
    """
    source = entry.missing_palette
    if source is not None and source.entry is not None:
        return True
    path = entry_palette_path(entry)
    return path is not None and not path_exists(path)


def path_is_palette_only(ws: Workspace, path: str) -> bool:
    """Whether nothing in ``ws`` reads ``path`` as pixel data.

    True for a file referenced only as a palette — an entry's external palette
    source, or a registered ``.pal`` row and the slices cut from it. What
    tells the two kinds of missing file apart when the user is being asked to
    find one: a palette file follows the graphic that uses it and may never have
    been picked by hand, so being asked for it by bare name reads as "which of
    my ROMs is this?".
    """
    # Late: the workspace module re-exports this one.
    from celpix.project.workspace import Workspace  # noqa: PLC0415 — circular by nature

    key = Workspace.path_key(path)
    # A slice or bookmark cut from a registered palette reads the palette's bytes, not a
    # ROM's, so it leaves the file a palette. Stated here rather than through
    # :func:`~celpix.project.workspace.section_kind`, which also files by content kind.
    return not any(
        entry.kind is not EntryKind.PALETTE
        and anchor_kind(entry) is not EntryKind.PALETTE
        and any(Workspace.path_key(p) == key for p in entry.paths)
        for entry in ws.entries
    )


def entry_notices(entry: Entry) -> tuple[Notice, ...]:
    """What the stages said while reading ``entry`` — every pathway, pixel first.

    Read off the live document rather than stored on the entry, because that is
    where they are already: a notice is produced by a load and the document *is*
    the result of one, so the two cannot fall out of step. An entry whose document
    has never been built has nothing to report, which is correct — nothing has
    been read yet.

    **All three contexts**, since a notice is recorded by whichever pathway ran
    and a tilemap entry's stages run on its own: a cell codec that had to assume
    something, or one whose optional metadata could not be read
    (:func:`~celpix.pipeline._stage.probe`), has the same claim on the row's
    tooltip as a container that dropped a tail.

    Then what the **document** refuses (:func:`_refusal_notices`), which no
    stage could have said: it is a fact about the pair of a map and its source.
    """
    if entry.doc is None:
        return ()
    return (
        notices(entry.doc.pixel_ctx)
        + notices(entry.doc.palette_ctx)
        + notices(entry.doc.tilemap_ctx)
        + _refusal_notices(entry.doc)
    )


def _refusal_notices(doc: Document) -> tuple[Notice, ...]:
    """What ``doc`` draws differently from what its formats asked for.

    A stamp the chain states and cannot lay out
    (:attr:`~celpix.core.document.Document.stamp_refusal`), and indices that
    were to count units and are read as corners
    (:attr:`~celpix.core.document.Document.addressing_refusal`). Warnings, by
    the level's own test: the picture is not simply what the file says.

    **Derived on every ask rather than recorded at load**, because neither is
    fixed by the load. Both follow the source — its cell size, the width it is
    viewed at — and a re-chain moves them without reading this entry again, so
    a recorded notice would outlive the refusal it reports, or miss one.
    """
    return tuple(
        Notice(
            NoticeLevel.WARNING,
            r.summary,
            f"{r.why[:1].upper()}{r.why[1:]},\nso {r.instead}",
        )
        for r in doc.refusals
    )


@dataclass(frozen=True)
class MissingPreset:
    """One entry field that named a format this build hasn't got, and its stand-in.

    ``used`` is what the field now holds — the stage's default format, or ``""``
    where the field was cleared instead (an alphabet, which has no stand-in).
    """

    entry: Entry
    stage: Stage
    wanted: str
    used: str


def repair_presets(entries: list[Entry], registry: Registry) -> list[MissingPreset]:
    """Point every entry at a format ``registry`` has; report what was swapped.

    The Interpret-stage counterpart to what
    :func:`~celpix.project.configs.pixel_config_for` does for the byte stages, and it
    has to be a *repair* rather than a resolution at the point of use: a byte stage's
    pass-through is one substitution inside one config, where a preset id is read by a
    dozen surfaces — the codec combo, the transform probes, the cell width, every decode
    — and each of them answering "no format" separately is how a missing preset became
    an uncaught ``KeyError`` in the first place. One pass, before the entries are shown,
    leaves every one of those reading a format that exists.

    Called wherever the registry and the entries can disagree, which is exactly
    where the registry is rebuilt: opening a project (whose ``plugins/`` folder
    may supply formats the previous one did not), and refreshing plugins.

    **The stored id is overwritten**, so saving the project afterwards writes the
    stand-in and the original reference is gone — which is why the swap is
    reported and shown rather than made quietly. Quitting without saving keeps the
    project file as it was, and installing the missing plugin makes it open
    correctly again.
    """
    replaced: list[MissingPreset] = []

    def resolved(entry: Entry, stage: Stage, wanted: str) -> str:
        if not wanted:
            return wanted
        used = registry.resolve_preset(stage, wanted)
        if used != wanted:
            replaced.append(MissingPreset(entry, stage, wanted, used))
        return used

    for entry in entries:
        if entry.session is not None:
            entry.session.pixel_preset_id = resolved(
                entry, Stage.INTERPRET_PIXEL, entry.session.pixel_preset_id
            )
            entry.session.palette_preset_id = resolved(
                entry, Stage.INTERPRET_PALETTE, entry.session.palette_preset_id
            )
        if entry.palette_preset_id:
            entry.palette_preset_id = resolved(
                entry, Stage.INTERPRET_PALETTE, entry.palette_preset_id
            )
        if entry.tilemap_preset_id:
            entry.tilemap_preset_id = resolved(
                entry, Stage.INTERPRET_TILEMAP, entry.tilemap_preset_id
            )
        # Nothing here for the font alphabet: it is the entry's own data now
        # (``font_chars`` / ``font_codes``), so there is no plugin for it to be
        # missing and nothing to repair.
    return replaced


def missing_paths(ws: Workspace) -> list[str]:
    """Every referenced path not on disk, de-duplicated, in list order.

    Unions each entry's data file with its external palette file, so one shared
    ROM (a file plus the slices/bookmarks under it) yields a single worklist
    entry — located once, corrected everywhere.
    """
    # Late: the workspace module re-exports this one.
    from celpix.project.workspace import Workspace  # noqa: PLC0415 — circular by nature

    seen: set[str] = set()
    result: list[str] = []
    with one_disk_scan():
        for entry in ws.entries:
            # Each *individually* missing file of a region, not the whole list:
            # the user locates the one chip that moved, and the ones still on
            # disk must not be put in front of them again.
            candidates = list(entry.paths)
            palette = entry_palette_path(entry)
            if palette is not None:
                candidates.append(palette)
            for path in candidates:
                # De-duplicated *before* the stat, not after: a ROM carries its
                # slices and bookmarks, and every one of them names that same
                # file, so asking the disk per entry made closing one - or
                # opening a project - probe it dozens of times over.
                key = Workspace.path_key(path)
                if key in seen:
                    continue
                seen.add(key)
                if not path_exists(path):
                    result.append(path)
    return result


def relocate_path(ws: Workspace, old_path: str, new_path: str) -> list[Entry]:
    """Repoint every reference to ``old_path`` at ``new_path``; return the
    entries touched.

    Rewrites an entry's data ``path`` and any pending/degraded palette source
    naming the same file, so relocating a shared ROM fixes the file and its
    slices/bookmarks (and any palette read from it) together. Pure data — the
    caller reloads the affected documents/palettes.
    """
    # Late: the workspace module re-exports this one.
    from celpix.project.workspace import Workspace  # noqa: PLC0415 — circular by nature

    key = Workspace.path_key(old_path)
    old_name, new_name = basename(old_path), basename(new_path)
    touched: list[Entry] = []
    for entry in ws.entries:
        data_moved = bool(entry.path) and Workspace.path_key(entry.path) == key
        if data_moved:
            entry.path = new_path
            # A FILE's or PALETTE's display name defaults to its on-disk
            # basename, so a located file that was renamed (or re-extensioned)
            # takes the new name — but only while the row is still showing that
            # default. A name the user typed is theirs, and survives the move;
            # slices and bookmarks are always named that way.
            if entry.kind in (EntryKind.FILE, EntryKind.PALETTE):
                if entry.name == old_name:
                    entry.name = new_name
        # A region's later chips move the same way, and independently: locating
        # one of them must not disturb the others or the order they join in.
        moved_extra = tuple(
            new_path if Workspace.path_key(p) == key else p for p in entry.extra_paths
        )
        if moved_extra != entry.extra_paths:
            entry.extra_paths = moved_extra
            data_moved = True
        changed = data_moved
        for source in (entry.missing_palette, entry.pending_palette):
            if source is None or not source.path:
                continue
            if Workspace.path_key(source.path) == key:
                source.path = new_path
                changed = True
        if changed:
            touched.append(entry)
    return touched


def retarget_files(ws: Workspace, entry: Entry, paths: tuple[str, ...]) -> list[Entry]:
    """Re-point a FILE at ``paths``, carrying its children; the entries touched.

    A file list is the entry's identity as much as its content: ``paths[0]`` is the row
    in the Files list, the key a slice or bookmark finds its parent by, and the file a
    save is attributed to. So the children move in the same step — a child's offset
    addresses the *joined* buffer (:func:`~celpix.project.configs.pixel_config_for`), so
    it has to be joined the same way to mean anything, and one left on the old path
    would no longer find its parent at all. Slices nested under those move too: they
    carry the file list their chain ends at.

    A FILE's display name defaults to its first file's basename, so it follows
    the list too — unless the user has renamed the row, which is theirs to keep
    (the same rule as :func:`relocate_path`). Pure data — the caller drops the
    affected documents and re-reads them.
    """
    if entry.kind is not EntryKind.FILE or not paths:
        return []
    first, *rest = paths
    named_after_file = entry.name == basename(entry.path)
    # The children are found *before* the path moves — they are keyed by the one
    # that is about to change.
    touched = [entry, *ws.descendants_of(entry)]
    for moved in touched:
        moved.path = first
        moved.extra_paths = tuple(rest)
    if named_after_file:
        entry.name = basename(first)
    return touched


def exportable_entries(ws: Workspace) -> list[Entry]:
    """The entries a bulk (whole-project) export should render, in list order.

    Every slice and every FILE that has **no** slices. A file that *has* slices
    is skipped: its slices are the curated regions worth exporting, so dumping the
    whole file alongside them would be redundant (and a whole ROM is rarely a
    useful image). A sliced file is exported only when the user names it
    explicitly (the single-entry Export), never in bulk — matching the rule that a
    file with slices isn't exported unless it alone is selected. A bookmark
    holds no graphic of its own and never appears.

    A registered **palette** never appears either, sliced or not: its swatch
    sheet is a view of a colour table, not art a bulk export is after. A slice
    cut from one is a slice like any other and does appear — someone carved
    that range out on purpose, which is the same claim any slice makes.

    A **slice with slices nested in it** follows the file's rule, for the file's
    reason: it is the stream its nested slices were carved out of (the tiles and
    the map one decompression holds), and they are the curated regions. Nested
    slices themselves appear like any slice.

    A **composite** always appears, and is not redundant with the pieces it is
    assembled from even though its bytes are theirs: the picture it makes is one
    nobody else in the list draws, which is the whole reason it exists. It has no
    slices of its own to defer to either — a slice is cut from a file, a palette
    or another slice, never from a composite.
    """
    result: list[Entry] = []
    for entry in ws.entries:
        if entry.kind is EntryKind.COMPOSITE:
            result.append(entry)
        elif entry.kind in (EntryKind.FILE, EntryKind.SLICE) and not ws.slices_of(
            entry
        ):
            result.append(entry)
    return result


# Characters kept verbatim in an export filename; everything else becomes '_' so
# a slice name (which may hold spaces, parentheses, or path separators) is always
# a safe basename on every platform.
_SAFE_NAME = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_."
)


def export_basename(entry: Entry) -> str:
    """A filesystem-safe basename (no extension) for exporting ``entry``.

    A FILE keeps its own stem (``foo.chr`` → ``foo``). A slice is prefixed with
    its parent file's stem so slices of different files don't collide in one
    export folder, and its own (possibly punctuation-heavy) name is sanitized
    (``foo`` + ``1000 (800)`` → ``foo_1000__800_``). The caller still de-dupes,
    since two slices of one file can share a name.

    A **composite** has no file to take a stem from, so its own name is the whole
    answer — which is also the only name it has ever had, being the one the user
    typed when they built it.
    """
    if entry.kind is EntryKind.COMPOSITE:
        return _sanitize(entry.name)
    parent_stem = splitext(basename(entry.path))[0] or "export"
    if entry.kind is not EntryKind.SLICE:
        return _sanitize(parent_stem)
    return f"{_sanitize(parent_stem)}_{_sanitize(entry.name)}"


def entry_export_name(entry: Entry) -> str:
    """A filesystem-safe basename (no extension) from ``entry``'s **own** name.

    What an export of a hand-picked set of rows is named by: the user chose
    those rows by what the list calls them, so the files carry the same names
    rather than :func:`export_basename`'s parent-prefixed ones. A file or
    palette still named after its path drops the extension, so ``foo.chr``
    leaves as ``foo`` rather than ``foo.chr.png``. The caller still de-dupes.
    """
    name = entry.name
    if entry.kind in (EntryKind.FILE, EntryKind.PALETTE) and name == basename(
        entry.path
    ):
        name = splitext(name)[0]
    return _sanitize(name)


def _sanitize(name: str) -> str:
    cleaned = "".join(c if c in _SAFE_NAME else "_" for c in name).strip("._")
    return cleaned or "export"


def default_slice_name(
    offset: int,
    length: int | None,
    compression_id: str = NO_COMPRESSION,
    reshape_id: str = NO_RESHAPE,
) -> str:
    """The generated name for an unnamed slice:
    ``offset (length) reshape compression``.

    No parent-filename prefix — the slice nests under its parent in the list,
    so the coordinates alone identify it. The length is omitted while still
    unknown (a compressed slice awaiting discovery), as are the pass-through
    reshape and compression.
    """
    parts = [format_hex(offset)]
    if length is not None:
        parts.append(f"({format_hex(length)})")
    if reshape_id != NO_RESHAPE:
        parts.append(reshape_id.removeprefix("reshape."))
    if compression_id != NO_COMPRESSION:
        parts.append(compression_id.removeprefix("compression."))
    return " ".join(parts)
