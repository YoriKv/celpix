"""The session's open-entries collection: files, slices, and bookmarks.

A :class:`Workspace` is the model behind the UI's open-files list. It holds an
ordered list of :class:`Entry` — a whole **file**, a **slice** (an
offset+length region of a parent file, optionally decompressed, that acts as
its own document), or a **bookmark** (an offset into a parent file plus a
snapshot of settings, with no document or view of its own) — plus a *current*
pointer for the single active view. Bookmarks are never current: they are
jumped *through*, reconfiguring their parent, not activated.
It is session-lifetime only; persisting it is :mod:`celpix.project.projectfile`'s
job (``docs/design/project-format.md``).

The workspace is Qt-free. The UI subscribes to the plain callback lists
(``on_added`` …) to mirror changes into its list widget; nothing here knows
about widgets, documents' rendering, or the pipeline's execution — an entry
only *carries* its lazily loaded :class:`~celpix.core.document.Document` and
the config factory (:func:`pixel_config_for`) that tells the pipeline how to
read it.

**Slices reference their parent by path, and read from disk.** With no editing
yet, a parent's in-memory bytes can never be newer than the file, so the
ordinary Read stage serves slices as-is via a bounded
:class:`~celpix.plugins.base.FileRef`. Cached documents of other entries on the
same path go stale only when one of them saves — :meth:`Workspace.invalidate_path`
drops those caches (except dirty ones: an invalidation must never discard
in-memory changes) so they reload fresh on next activation. External changes to
the file on disk are ignored, as they always were for the single document.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from os.path import abspath, basename, exists, normcase
from typing import Callable

from celpix.core.address import format_hex
from celpix.core.context import (
    KEY_COMPRESSED_SIZE,
    KEY_DECOMPRESS_COMPLETE,
    PipelineContext,
)
from celpix.core.document import Document, ViewOptions
from celpix.core.errors import Stage
from celpix.pipeline.pathway import PathwayConfig
from celpix.plugins.base import FileRef
from celpix.plugins.registry import Registry


class EntryKind(Enum):
    FILE = auto()
    SLICE = auto()
    BOOKMARK = auto()


@dataclass
class PaletteSource:
    """Where an entry's palette colours come from, as restorable plain data.

    Exactly one shape is meaningful (``docs/design/project-format.md`` §4.3):
    inline ``colors`` (ARGB ints — no external source), an external palette
    file ``path`` (+ ``offset`` into it), or just an ``offset`` into the
    entry's own pixel file. A live entry keeps this information on its
    document's palette config; this form exists for entries whose document
    isn't loaded yet (project restore) and is consumed on first activation.
    """

    colors: list[int] | None = None
    path: str | None = None
    offset: int = 0


@dataclass(frozen=True)
class SliceParams:
    """The four entry fields a slice's coordinates comprise.

    Plain, Qt-free data shared by the slice dialog (which produces it) and the
    slice-edit undo command (which stores a before/after pair) — one type so a
    dialog result flows straight into a command without a field-by-field copy.
    """

    name: str
    offset: int
    length: int | None
    decompress_id: str


@dataclass
class EntrySession:
    """Per-entry snapshot of the UI session — what entry-switching restores.

    Plain data (project-file material). Only the state that is *not*
    already carried by the entry's :class:`Document` lives here: view geometry,
    offset/nudge and subpalette are in ``Document.view``, and the palette
    itself plus both pathway configs are on the document.
    """

    pixel_preset_id: str
    palette_preset_id: str
    palette_mode: str = "default"  # default | file | offset | emulator
    compression_id: str = "decompress.none"  # the preview combo, not a slice codec
    headered: bool = False
    header_length: int = 512
    # The selected tile range, inclusive: ``selected_tile`` is the anchor (and
    # what single-selection consumers read); ``selected_last`` >= it for a
    # drag range, None when the selection is a single tile (or absent).
    selected_tile: int | None = None
    selected_last: int | None = None


@dataclass(eq=False)  # identity semantics: two slices may share coordinates
class Entry:
    """One open item: a whole file, an offset+length slice of one, or a bookmark.

    ``path`` is the file itself for FILE entries and the **parent** file for
    SLICE and BOOKMARK entries. **Slices and bookmarks never nest**: both are
    always anchored to a whole file, never to another slice, so their ``path``
    always names a FILE and the open-entries list is exactly two levels deep.
    ``slice_offset`` is an absolute offset from byte 0 of the file —
    deliberately not header-relative, so a slice or bookmark never shifts when
    the parent's header-skip display setting changes. ``slice_length`` may
    start ``None`` for a decompressed slice ("to be discovered"): the first
    load backfills it from the structure's true extent so save-back is
    slot-bounded.

    A BOOKMARK is a position marker, not a document: it has no length and is
    never loaded or made current. It repurposes the restore fields as its
    permanent settings snapshot — ``session``, ``pending_view`` and
    ``pending_palette`` hold the parent's state as of the bookmark's creation,
    and (unlike on a file/slice) are never consumed; jumping copies them back
    onto the parent.
    """

    name: str
    kind: EntryKind
    path: str
    slice_offset: int = 0
    slice_length: int | None = None
    decompress_id: str = "decompress.none"
    doc: Document | None = None  # lazy: loaded on first activation
    session: EntrySession | None = None
    dirty: bool = False  # unsaved in-memory changes (edits set it, Write clears it)
    # Project-restored display state, held until the lazy document exists and
    # consumed on its first load (the live state then lives on the document).
    pending_view: ViewOptions | None = None
    pending_palette: PaletteSource | None = None
    # Set when an external palette source (file/emulator mode) couldn't be
    # reached on load: the entry renders on the default palette but keeps its
    # palette_mode display, and this holds the source so it can be re-pointed
    # (Locate missing files) and re-saved. None when the palette is healthy or
    # still unloaded (an unloaded source lives on pending_palette).
    missing_palette: PaletteSource | None = None


class Workspace:
    """The ordered open-entries list + current pointer, with change callbacks."""

    def __init__(self) -> None:
        self.entries: list[Entry] = []
        self.current: Entry | None = None
        self.on_added: list[Callable[[Entry], None]] = []
        self.on_removed: list[Callable[[Entry], None]] = []
        self.on_current_changed: list[Callable[[Entry | None], None]] = []
        self.on_dirty_changed: list[Callable[[Entry], None]] = []

    # -- lookups -----------------------------------------------------------
    @staticmethod
    def _path_key(path: str) -> str:
        # The project lives on a Windows drive but is used from both OSes, so
        # path identity must survive case differences on the same file.
        return normcase(abspath(path))

    def find_file(self, path: str) -> Entry | None:
        """The FILE entry for ``path``, if one is open (slices never match)."""
        key = self._path_key(path)
        for entry in self.entries:
            if entry.kind is EntryKind.FILE and self._path_key(entry.path) == key:
                return entry
        return None

    def slices_of(self, entry: Entry) -> list[Entry]:
        """The SLICE entries carved from ``entry``'s file, in list order.

        Only a FILE has slices — slices never nest — so this is a single hop,
        never recursive.
        """
        return [e for e in self.children_of(entry) if e.kind is EntryKind.SLICE]

    def children_of(self, entry: Entry) -> list[Entry]:
        """The SLICE and BOOKMARK entries anchored to ``entry``'s file, in
        list order (empty unless ``entry`` is a FILE — children never nest)."""
        if entry.kind is not EntryKind.FILE:
            return []
        key = self._path_key(entry.path)
        return [
            e
            for e in self.entries
            if e.kind is not EntryKind.FILE and self._path_key(e.path) == key
        ]

    def parent_of(self, entry: Entry) -> Entry | None:
        """The open FILE entry a SLICE or BOOKMARK is anchored to (None for a
        FILE, or when the parent is closed/never open)."""
        return None if entry.kind is EntryKind.FILE else self.find_file(entry.path)

    def dirty_entries(self) -> list[Entry]:
        return [e for e in self.entries if e.dirty]

    # -- mutations ---------------------------------------------------------
    def open_file(self, path: str) -> Entry:
        """Add a FILE entry for ``path`` — or return the one already open.

        Identity is the (normalized) path: a document *is* its file, so opening
        it twice yields the same entry rather than a duplicate.
        """
        existing = self.find_file(path)
        if existing is not None:
            return existing
        from os.path import basename

        entry = Entry(name=basename(path), kind=EntryKind.FILE, path=path)
        self.entries.append(entry)
        self._notify(self.on_added, entry)
        return entry

    def add_slice(
        self,
        parent_path: str,
        name: str,
        offset: int,
        length: int | None,
        decompress_id: str = "decompress.none",
    ) -> Entry:
        """Add a SLICE of the file at ``parent_path``.

        ``parent_path`` is always a whole file: slices never nest, so a slice's
        parent is a FILE, never another slice.
        """
        entry = Entry(
            name=name,
            kind=EntryKind.SLICE,
            path=parent_path,
            slice_offset=offset,
            slice_length=length,
            decompress_id=decompress_id,
        )
        self.entries.append(entry)
        self._notify(self.on_added, entry)
        return entry

    def insert(self, entry: Entry, index: int) -> None:
        """Insert an already-constructed entry at ``index`` (undo/redo path:
        re-adding restores the *same* Entry object, so its document, session
        and any commands referencing it stay valid)."""
        self.entries.insert(index, entry)
        self._notify(self.on_added, entry)

    def close(self, entry: Entry) -> list[Entry]:
        """Remove ``entry`` — and, for a file, the slices/bookmarks under it.

        A slice or bookmark nested under a closed parent would be an orphan in
        the list, so the parent takes its children with it (the UI confirms
        first). Returns everything removed. If the current entry was among
        them, ``current`` moves to a list neighbour — skipping bookmarks,
        which cannot be current — or None when no candidate remains.
        """
        removed = [entry, *self.children_of(entry)]
        anchor = min(self.entries.index(e) for e in removed)
        for e in removed:
            self.entries.remove(e)
            self._notify(self.on_removed, e)
        if self.current in removed:
            after = self.entries[anchor:]
            before = reversed(self.entries[:anchor])
            neighbour = next(
                (e for e in after if e.kind is not EntryKind.BOOKMARK),
                next((e for e in before if e.kind is not EntryKind.BOOKMARK), None),
            )
            self.set_current(neighbour)
        return removed

    def replace(self, entries: list[Entry], current: Entry | None) -> None:
        """Swap the whole list for ``entries`` — a loaded project replaces the
        workspace, never merges into it.

        Notifies removal of every old entry and addition of every new one, and
        sets ``current`` last so the activation lands on a populated list.
        """
        self.set_current(None)
        for entry in list(self.entries):
            self.entries.remove(entry)
            self._notify(self.on_removed, entry)
        self.entries.extend(entries)
        for entry in entries:
            self._notify(self.on_added, entry)
        self.set_current(current)

    def set_current(self, entry: Entry | None) -> None:
        if entry is self.current:
            return
        assert entry is None or entry in self.entries
        # A bookmark has no document or view of its own — it can never be shown.
        assert entry is None or entry.kind is not EntryKind.BOOKMARK
        self.current = entry
        self._notify(self.on_current_changed, entry)

    def set_dirty(self, entry: Entry, dirty: bool = True) -> None:
        if entry.dirty != dirty:
            entry.dirty = dirty
            self._notify(self.on_dirty_changed, entry)

    def invalidate_path(self, path: str, keep: Entry | None = None) -> None:
        """Drop cached documents of entries rooted at ``path`` (after a save).

        ``keep`` — the entry that just saved — retains its cache. Dirty entries
        also retain theirs: their document holds unsaved changes, and dropping
        it would silently lose them; they simply stay based on the pre-save
        bytes until written or explicitly reloaded.
        """
        key = self._path_key(path)
        for entry in self.entries:
            if entry is keep or entry.dirty:
                continue
            if self._path_key(entry.path) == key:
                entry.doc = None

    @staticmethod
    def _notify(callbacks: list[Callable[[Entry], None]], entry) -> None:
        for callback in list(callbacks):
            callback(entry)


def pixel_config_for(
    entry: Entry, preset_id: str, header_offset: int, registry: Registry
) -> PathwayConfig:
    """The pixel pathway config that reads (and writes back) ``entry``.

    A slice needs no special pipeline machinery: it is an ordinary config whose
    source is a *bounded* FileRef into the parent — Read slices the region,
    Decompress unpacks it, and at save time the same bounds make Write splice
    into (and never overflow) the parent's slot.

    The compressor is derived from the slice's decompressor by the built-in
    ``decompress.X`` ↔ ``compress.X`` id convention. A scheme with no
    registered compressor (view-only compression format) yields a config with
    ``write_enabled=False`` — the slice loads and views fine, it just can't be
    written back.
    """
    if entry.kind is EntryKind.FILE:
        return PathwayConfig(
            source=FileRef(entry.path, offset=header_offset),
            interpret_preset_id=preset_id,
        )
    compress_id = entry.decompress_id.replace("decompress.", "compress.", 1)
    write_enabled = True
    try:
        registry.plugin(Stage.COMPRESS, compress_id)
    except KeyError:
        compress_id = "compress.none"
        write_enabled = False
    return PathwayConfig(
        source=FileRef(
            entry.path, offset=entry.slice_offset, length=entry.slice_length
        ),
        interpret_preset_id=preset_id,
        decompress_id=entry.decompress_id,
        compress_id=compress_id,
        write_enabled=write_enabled,
    )


def palette_source_for(entry: Entry) -> PaletteSource | None:
    """The entry's live palette as restorable plain data — ``None`` for default.

    Derived from the loaded document (its palette config is the truth for the
    file/offset modes) plus the session's mode; a never-activated entry has no
    live state, so its pending source (if any) is returned as-is. This is the
    inverse of :meth:`_apply_restored_state`'s consumption of ``pending_palette``
    — it's what both project-save and new-slice seeding read to carry a palette
    forward. An offset source is an absolute file offset, so it resolves against
    a slice's parent file exactly as it does for the parent itself.
    """
    # A degraded palette (its file went missing) keeps its intended source here
    # rather than on the live config, so save and new-slice seeding carry the
    # reference forward even while the entry renders on the default palette.
    if entry.missing_palette is not None:
        return entry.missing_palette
    if entry.doc is None or entry.session is None:
        return entry.pending_palette
    mode = entry.session.palette_mode
    source = entry.doc.palette_config.source
    if mode == "file":
        return PaletteSource(path=source.path, offset=source.offset)
    if mode == "offset":
        return PaletteSource(offset=source.offset)
    if mode == "emulator":
        # Only the state file's path is stored; where the palette sits inside it
        # (and which console codec decodes it) is re-detected on restore, so a
        # newer detector or an edited state stays authoritative over stale coords.
        return PaletteSource(path=source.path)
    return None


# -- missing-reference handling (docs/design/project-format.md §3) ---------
def data_missing(entry: Entry) -> bool:
    """Whether the entry's own data file is gone from disk.

    For a slice or bookmark this is the parent file (their ``path``); a missing
    parent leaves the child unloadable exactly as a missing file does.
    """
    return not exists(entry.path)


def entry_palette_path(entry: Entry) -> str | None:
    """The external palette-source file the entry references, or ``None``.

    Only file/emulator modes have an external palette file, and its path is read
    from wherever the entry currently keeps it: the degraded source (loaded, but
    its file went missing), the live document config (loaded and healthy), or
    the pending source (not yet activated).
    """
    session = entry.session
    if session is None or session.palette_mode not in ("file", "emulator"):
        return None
    if entry.missing_palette is not None:
        return entry.missing_palette.path
    if entry.doc is not None:
        return entry.doc.palette_config.source.path or None
    if entry.pending_palette is not None:
        return entry.pending_palette.path
    return None


def palette_missing(entry: Entry) -> bool:
    """Whether the entry's external palette file is referenced but gone."""
    path = entry_palette_path(entry)
    return path is not None and not exists(path)


def entry_reference_missing(entry: Entry) -> bool:
    """Whether either file the entry references (its data or its palette) is
    gone — the condition the files list flags with a warning highlight."""
    return data_missing(entry) or palette_missing(entry)


def missing_paths(ws: Workspace) -> list[str]:
    """Every referenced path not on disk, de-duplicated, in list order.

    Unions each entry's data file with its external palette file, so one shared
    ROM (a file plus the slices/bookmarks under it) yields a single worklist
    entry — located once, corrected everywhere.
    """
    seen: set[str] = set()
    result: list[str] = []
    for entry in ws.entries:
        candidates = [entry.path] if data_missing(entry) else []
        if palette_missing(entry):
            candidates.append(entry_palette_path(entry))
        for path in candidates:
            key = Workspace._path_key(path)
            if key not in seen:
                seen.add(key)
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
    key = Workspace._path_key(old_path)
    new_name = basename(new_path)
    touched: list[Entry] = []
    for entry in ws.entries:
        data_moved = bool(entry.path) and Workspace._path_key(entry.path) == key
        if data_moved:
            entry.path = new_path
            # A FILE's display name mirrors its on-disk basename, so a located
            # file that was renamed (or re-extensioned) takes the new name;
            # slices and bookmarks keep the names the user gave them.
            if entry.kind is EntryKind.FILE:
                entry.name = new_name
        changed = data_moved
        for source in (entry.missing_palette, entry.pending_palette):
            if source is None or not source.path:
                continue
            if Workspace._path_key(source.path) == key:
                source.path = new_path
                changed = True
        if changed:
            touched.append(entry)
    return touched


def default_slice_name(
    offset: int, length: int | None, decompress_id: str = "decompress.none"
) -> str:
    """The generated name for an unnamed slice: ``offset (length) compression``.

    No parent-filename prefix — the slice nests under its parent in the list,
    so the coordinates alone identify it. The length is omitted while still
    unknown (a compressed slice awaiting discovery), as is the pass-through
    compression.
    """
    parts = [format_hex(offset)]
    if length is not None:
        parts.append(f"({format_hex(length)})")
    if decompress_id != "decompress.none":
        parts.append(decompress_id.removeprefix("decompress."))
    return " ".join(parts)


def backfill_slice_length(entry: Entry, ctx: PipelineContext) -> bool:
    """Fill in a decompressed slice's extent discovered at load; True if it did.

    A slice created without a length ("decompress from here, wherever it ends")
    reads to end-of-file, and the decompressor reports the structure's true
    byte extent in the context. Recording that extent onto the entry bounds
    every later load — and, crucially, makes save-back slot-enforced. Only a
    *complete* decompress counts: a truncated/partial extent would bound the
    slice at the wrong size.
    """
    if entry.kind is not EntryKind.SLICE or entry.slice_length is not None:
        return False
    consumed = ctx.get(KEY_COMPRESSED_SIZE)
    if not consumed or not ctx.get(KEY_DECOMPRESS_COMPLETE):
        return False
    entry.slice_length = consumed
    return True
