"""The session's open-entries collection: files, slices, bookmarks, palettes.

A :class:`Workspace` is the model behind the UI's open-files list. It holds an
ordered list of :class:`Entry` — a whole **file**, a **slice** (an
offset+length region of a parent file, optionally decompressed, that acts as
its own document), a **bookmark** (an offset into a parent file plus a
snapshot of settings, with no document or view of its own), or a **palette**
(an external palette file, remembering the codec it was imported with) — plus
a *current* pointer for the single active view. Bookmarks and palettes are
never current: a bookmark is jumped *through*, reconfiguring its parent, and a
palette is *applied* to whichever entry is on screen, not activated.
It is session-lifetime only; persisting it is :mod:`celpix.project.projectfile`'s
job (``docs/design/project-format.md``).

The workspace is Qt-free. The UI subscribes to the plain callback lists
(``on_added`` …) to mirror changes into its list widget; nothing here knows
about widgets, documents' rendering, or the pipeline's execution — an entry
only *carries* its lazily loaded :class:`~celpix.core.document.Document` and
the config factory (:func:`pixel_config_for`) that tells the pipeline how to
read it.

**Slices reference their parent by path.** A slice is an ordinary bounded
:class:`~celpix.plugins.base.FileRef` into the parent, so the normal Read stage
serves it — from the file on disk, *except* while the parent holds unsaved pixel
edits. Then the file is the stale copy, so :func:`pixel_config_for` points the
slice's source at the parent's live buffer instead (``FileRef.data``) while
leaving the write target on disk: carving a slice out of a ROM you have been
editing shows the edits, and writing it back still lands in the file.

Cached documents of other entries on the same path go stale only when one of them
saves — :meth:`Workspace.invalidate_path` drops those caches (except dirty ones:
an invalidation must never discard in-memory changes) so they reload fresh on next
activation. External changes to the file on disk are ignored, as they always were
for the single document.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from os.path import abspath, basename, exists, normcase, splitext
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
from celpix.plugins.base import NO_COMPRESS, NO_DECOMPRESS, FileRef
from celpix.plugins.registry import Registry


class EntryKind(Enum):
    FILE = auto()
    SLICE = auto()
    BOOKMARK = auto()
    PALETTE = auto()


class PaletteMode(str, Enum):
    """Where an entry's palette colors come from.

    ``value`` is the stable string persisted in the project file (str-valued for
    exactly that reason, like :class:`~celpix.core.errors.Stage`), so the on-disk
    schema is unchanged by this being a type rather than a bare string.

    The distinctions between the modes drive several different decisions, and the
    properties below are the single statement of each — they were previously four
    separate literal tuples kept in step by hand across the window, the workspace
    and the project reader. See ``docs/design/palette-editing.md`` for what a
    color edit can be written back to in each.
    """

    DEFAULT = "default"  # the generated fallback palette
    FILE = "file"  # a standalone palette file
    OFFSET = "offset"  # raw bytes at an offset in the entry's own pixel file
    EMULATOR = "emulator"  # pulled from an emulator save state (view-only)
    CUSTOM = "custom"  # colors stored in the .celpix project itself

    @classmethod
    def parse(cls, value: object) -> PaletteMode:
        """``value`` as a mode, falling back to DEFAULT for anything unknown —
        a hand-authored or newer project file names a mode this build has no
        meaning for, and opening on the generated palette beats failing."""
        try:
            return cls(value)
        except ValueError:
            return cls.DEFAULT

    @property
    def is_real(self) -> bool:
        """Whether real colors are in play, as opposed to the generated fallback.

        Anything but DEFAULT must survive a pixel reload rather than being
        regenerated at the new format's index space.
        """
        return self is not PaletteMode.DEFAULT

    @property
    def has_source(self) -> bool:
        """Whether the palette can be re-read/re-decoded from somewhere.

        Narrower than :attr:`is_real`: CUSTOM is real but exists only in the
        project, so a format re-decode or plugin refresh has nothing to load.
        """
        return self in (PaletteMode.FILE, PaletteMode.OFFSET, PaletteMode.EMULATOR)

    @property
    def decodes_raw_bytes(self) -> bool:
        """Whether a *choice* of color codec applies — the only modes where the
        format picker means anything. DEFAULT and CUSTOM carry their own colors,
        and an emulator state's console dictates its codec."""
        return self in (PaletteMode.FILE, PaletteMode.OFFSET)

    @property
    def has_external_file(self) -> bool:
        """Whether the colors come from a file of their own, whose name the
        palette dock shows and whose loss degrades the entry."""
        return self in (PaletteMode.FILE, PaletteMode.EMULATOR)

    @property
    def is_exportable(self) -> bool:
        """Whether "Export to File…" has anything to offer.

        FILE already *is* a ``.pal`` and DEFAULT is generated from nothing, so
        exporting either would only copy something the user already has.
        """
        return self not in (PaletteMode.DEFAULT, PaletteMode.FILE)


@dataclass
class PaletteSource:
    """Where an entry's palette colors come from, as restorable plain data.

    Exactly one shape is meaningful (``docs/design/project-format.md`` §4.3):
    inline ``colors`` (ARGB ints — the **custom** palette, which has no external
    source and lives entirely in the project), an external palette file ``path``
    (+ ``offset`` into it), or just an ``offset`` into the
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
    palette_mode: PaletteMode = PaletteMode.DEFAULT
    compression_id: str = NO_DECOMPRESS  # the preview combo, not a slice codec
    headered: bool = False
    header_length: int = 512
    # The selection. ``selected_tile`` is the anchor (and what single-selection
    # consumers read); ``selected_last`` >= it bounds a range, None when the
    # selection is a single tile (or absent). ``selection_cells`` is set only for
    # a *rectangle* selection — its (columns, rows) extent in canvas cells, which
    # together with the anchor and the restored view geometry re-derives exactly
    # which tiles it covered.
    selected_tile: int | None = None
    selected_last: int | None = None
    selection_cells: tuple[int, int] | None = None

    def __post_init__(self) -> None:
        # PaletteMode is str-valued so it persists as itself, which makes a bare
        # string quietly *equal* to the right member while failing every ``is``
        # check the window branches on. Normalising here makes the annotation
        # true of every session however it was built - project file, plugin, or
        # test - so those identity comparisons are safe by construction.
        self.palette_mode = PaletteMode.parse(self.palette_mode)


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

    A PALETTE is an external palette file registered with the session: its
    ``path`` is the palette file itself (top-level, never a child of a FILE
    even when their paths collide), and ``palette_preset_id`` remembers the
    codec it was last read with - the format it was registered under, kept in
    step with the format dropdown while this file is the palette on screen - so
    applying it later decodes the same way it last did, regardless of where the
    dropdown has moved for some other palette since. Like a bookmark
    it has no document or view and is never current — it is applied *onto*
    the entry being shown.
    """

    name: str
    kind: EntryKind
    path: str
    slice_offset: int = 0
    slice_length: int | None = None
    decompress_id: str = NO_DECOMPRESS
    doc: Document | None = None  # lazy: loaded on first activation
    session: EntrySession | None = None
    # Unsaved in-memory changes, tracked **per pathway** because the two write to
    # different files: the pixel pathway is the entry's own data (its pixel bytes
    # — for a slice, spliced back into the parent file), the palette pathway a
    # separate source (a .pal, or the palette's own region of a ROM). Keeping
    # them apart is what stops a color edit from rewriting the graphic
    # (docs/design/palette-editing.md §2).
    #
    # Each pathway holds a *revision token* rather than a flag: an edit command
    # stamps a fresh token when it applies and puts the previous one back when
    # it undoes, and a write records the token it saved. "Dirty" is then simply
    # "the live token isn't the saved one", which goes clean again when an undo
    # walks back to the saved state — and stays dirty when it walks back *past*
    # a save point. Tokens rather than a counter because a count can collide
    # (undo one edit, make a different one) and would then report clean wrongly.
    pixel_revision: int = 0
    pixel_saved_revision: int = 0
    palette_revision: int = 0
    palette_saved_revision: int = 0

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
    # PALETTE entries only: the palette codec the file was imported with.
    palette_preset_id: str | None = None

    @property
    def pixel_dirty(self) -> bool:
        """Unsaved changes to the entry's own data (its pixel bytes)."""
        return self.pixel_revision != self.pixel_saved_revision

    @property
    def palette_dirty(self) -> bool:
        """Unsaved changes on the entry's palette pathway."""
        return self.palette_revision != self.palette_saved_revision


def new_slice(
    parent_path: str,
    name: str,
    offset: int,
    length: int | None = None,
    decompress_id: str = NO_DECOMPRESS,
) -> Entry:
    """A SLICE entry over ``parent_path`` — not yet in any workspace.

    Building and *adding* are separate because the UI's adds are undoable: an
    ``AddEntryCommand`` needs the entry to exist before it is pushed, and the
    command owns the insertion (so undo/redo re-add the very same object). This
    is the one statement of what a slice entry is, shared by that path and by
    :meth:`Workspace.add_slice`.

    ``parent_path`` is always a whole *file* — slices never nest, so a slice's
    parent is a FILE, never another slice — and it becomes the entry's ``path``:
    a slice is named by the file it cuts into, not by one of its own.
    ``offset`` is likewise absolute in that file, and ``length`` may be ``None``
    for a compressed slice whose extent is discovered on first load.
    """
    return Entry(
        name=name,
        kind=EntryKind.SLICE,
        path=parent_path,
        slice_offset=offset,
        slice_length=length,
        decompress_id=decompress_id,
    )


class Workspace:
    """The ordered open-entries list + current pointer, with change callbacks."""

    def __init__(self) -> None:
        self.entries: list[Entry] = []
        self.current: Entry | None = None
        self.on_added: list[Callable[[Entry], None]] = []
        self.on_removed: list[Callable[[Entry], None]] = []
        self.on_current_changed: list[Callable[[Entry | None], None]] = []
        self.on_dirty_changed: list[Callable[[Entry], None]] = []
        self._revision = 0  # allocator for per-entry revision tokens

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

    def find_palette(self, path: str) -> Entry | None:
        """The PALETTE entry for ``path``, if one is registered — same
        path-is-identity rule as :meth:`find_file`, per kind."""
        key = self._path_key(path)
        for entry in self.entries:
            if entry.kind is EntryKind.PALETTE and self._path_key(entry.path) == key:
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
        list order (empty unless ``entry`` is a FILE — children never nest).
        A PALETTE entry sharing the path is not a child: its path names the
        palette file itself, not a parent."""
        if entry.kind is not EntryKind.FILE:
            return []
        key = self._path_key(entry.path)
        return [
            e
            for e in self.entries
            if e.kind in (EntryKind.SLICE, EntryKind.BOOKMARK)
            and self._path_key(e.path) == key
        ]

    def parent_of(self, entry: Entry) -> Entry | None:
        """The open FILE entry a SLICE or BOOKMARK is anchored to (None for a
        FILE or PALETTE — their path is their own file — or when the parent
        is closed/never open)."""
        if entry.kind in (EntryKind.FILE, EntryKind.PALETTE):
            return None
        return self.find_file(entry.path)

    def dirty_entries(self) -> list[Entry]:
        """Every entry with anything unsaved, on either pathway.

        Callers that ask "is there unsaved work?" — the close/replace prompts,
        Write All — mean both kinds; which files a write then touches is
        :meth:`Entry.pixel_dirty`/:attr:`Entry.palette_dirty`'s job, not this one's.
        """
        return [e for e in self.entries if e.pixel_dirty or e.palette_dirty]

    # -- mutations ---------------------------------------------------------
    def open_file(self, path: str) -> Entry:
        """Add a FILE entry for ``path`` — or return the one already open.

        Identity is the (normalized) path: a document *is* its file, so opening
        it twice yields the same entry rather than a duplicate.
        """
        existing = self.find_file(path)
        if existing is not None:
            return existing
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
        decompress_id: str = NO_DECOMPRESS,
    ) -> Entry:
        """Build a slice of ``parent_path`` and append it directly.

        The **non-undoable** add, like :meth:`open_file`. The UI does not take
        this path for slices the user creates: those go through an
        ``AddEntryCommand`` so the new entry can be undone, which means the
        command has to own the insertion (see :func:`new_slice`, which builds the
        entry the command then adds). This stays for callers with no undo stack
        to answer to — scripting the model directly, and the tests.
        """
        entry = new_slice(parent_path, name, offset, length, decompress_id)
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
            # Bookmarks and palettes can never be current, so the neighbour
            # search skips them.
            viewable = (EntryKind.FILE, EntryKind.SLICE)
            after = self.entries[anchor:]
            before = reversed(self.entries[:anchor])
            neighbour = next(
                (e for e in after if e.kind in viewable),
                next((e for e in before if e.kind in viewable), None),
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
        # Bookmarks and palettes have no document or view of their own — they
        # can never be shown.
        assert entry is None or entry.kind in (EntryKind.FILE, EntryKind.SLICE)
        self.current = entry
        self._notify(self.on_current_changed, entry)

    def next_revision(self) -> int:
        """A fresh revision token, unique across the whole workspace.

        Never reused, so a token identifies one exact state of one pathway:
        that is what lets an undo restore "the state that was saved" rather
        than merely "one edit fewer" (see :class:`Entry`).
        """
        self._revision += 1
        return self._revision

    def set_pixel_revision(self, entry: Entry, revision: int) -> None:
        """Stamp the entry's data pathway with ``revision`` (an edit applying,
        or an undo putting the previous token back)."""
        self._set_revision(entry, "pixel_revision", revision)

    def set_palette_revision(self, entry: Entry, revision: int) -> None:
        """Stamp the entry's *palette* pathway, leaving its data alone."""
        self._set_revision(entry, "palette_revision", revision)

    def mark_saved(
        self, entry: Entry, *, pixel: bool = True, palette: bool = True
    ) -> None:
        """Record the current revisions as the ones on disk — the entry reads
        clean until it is edited away from them again.

        Also the honest way to drop changes that no longer exist (a slice
        re-pointed at another region discards its document): there is nothing
        unsaved once the edits themselves are gone.
        """
        before = (entry.pixel_dirty, entry.palette_dirty)
        if pixel:
            entry.pixel_saved_revision = entry.pixel_revision
        if palette:
            entry.palette_saved_revision = entry.palette_revision
        if (entry.pixel_dirty, entry.palette_dirty) != before:
            self._notify(self.on_dirty_changed, entry)

    def _set_revision(self, entry: Entry, field: str, revision: int) -> None:
        before = (entry.pixel_dirty, entry.palette_dirty)
        setattr(entry, field, revision)
        if (entry.pixel_dirty, entry.palette_dirty) != before:
            self._notify(self.on_dirty_changed, entry)

    def drop_document(self, entry: Entry) -> None:
        """Discard an entry's cached document, preserving its palette source.

        The palette must survive a document drop because for a **custom**
        palette the document is the *only* place its colors exist — nothing on
        disk backs them. Capturing the source into ``pending_palette`` hands
        them to the reload the same way a project restore does, so re-reading
        the pixel bytes never silently reverts an edited palette to the
        generated default. For the file-backed modes this is simply a
        re-resolution of the reference they already carry.
        """
        source = palette_source_for(entry)
        if source is not None:
            entry.pending_palette = source
        entry.doc = None

    def invalidate_path(self, path: str, keep: Entry | None = None) -> None:
        """Drop cached documents of entries rooted at ``path`` (after a save).

        ``keep`` — the entry that just saved — retains its cache. Entries with
        unsaved changes on *either* pathway also retain theirs: their document
        holds those changes, and dropping it would silently lose them; they
        simply stay based on the pre-save bytes until written or explicitly
        reloaded.
        """
        key = self._path_key(path)
        for entry in self.entries:
            if entry is keep or entry.pixel_dirty or entry.palette_dirty:
                continue
            if self._path_key(entry.path) == key:
                self.drop_document(entry)

    @staticmethod
    def _notify(callbacks: list[Callable[[Entry], None]], entry) -> None:
        for callback in list(callbacks):
            callback(entry)


def pixel_config_for(
    entry: Entry,
    preset_id: str,
    header_offset: int,
    registry: Registry,
    workspace: Workspace | None = None,
) -> PathwayConfig:
    """The pixel pathway config that reads (and writes back) ``entry``.

    A slice needs no special pipeline machinery: it is an ordinary config whose
    source is a *bounded* FileRef into the parent — Read slices the region,
    Decompress unpacks it, and at save time the same bounds make Write splice
    into (and never overflow) the parent's slot.

    Pass ``workspace`` so a slice of a parent with **unsaved pixel edits** reads
    those edits rather than the stale bytes on disk (see the module docstring);
    without it — or with a clean/unloaded parent — the file is the source, which
    is the same thing. The rebase is what keeps that honest: the parent's buffer
    starts at its header skip, so it is handed over as ``data`` with a matching
    ``data_base``, leaving the slice's own ``offset`` file-absolute for Read,
    Write and the address display alike.

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
        compress_id = NO_COMPRESS
        write_enabled = False
    live, live_base = _unsaved_parent_bytes(entry, workspace)
    return PathwayConfig(
        source=FileRef(
            entry.path,
            offset=entry.slice_offset,
            length=entry.slice_length,
            data=live,
            data_base=live_base,
        ),
        # Write always targets the file: the in-memory source above is only about
        # reading what the parent has not written out yet.
        dest=FileRef(entry.path, offset=entry.slice_offset, length=entry.slice_length),
        interpret_preset_id=preset_id,
        decompress_id=entry.decompress_id,
        compress_id=compress_id,
        write_enabled=write_enabled,
    )


def _unsaved_parent_bytes(
    entry: Entry, workspace: Workspace | None
) -> tuple[bytes | None, int]:
    """A dirty parent's live pixel bytes and the file offset they start at.

    ``(None, 0)`` — read the file — unless the slice's parent is open, loaded and
    holds unsaved *pixel* edits. A dirty **palette** doesn't qualify: it lives on
    the other pathway and in another file, so it says nothing about these bytes.
    The slice must also fall inside the parent's window; one anchored before the
    parent's header skip isn't in that buffer at all, so it reads from disk.
    """
    if workspace is None:
        return (None, 0)
    parent = workspace.find_file(entry.path)
    if parent is None or parent.doc is None or not parent.pixel_dirty:
        return (None, 0)
    base = parent.doc.pixel_config.source.offset
    if entry.slice_offset < base:
        return (None, 0)
    return (parent.doc.pixel_data, base)


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
    if mode is PaletteMode.CUSTOM:
        # The custom palette *is* the project data — there is no file behind it,
        # so the colors themselves are what round-trips.
        return PaletteSource(colors=list(entry.doc.palette.colors))
    if mode is PaletteMode.FILE:
        return PaletteSource(path=source.path, offset=source.offset)
    if mode is PaletteMode.OFFSET:
        return PaletteSource(offset=source.offset)
    if mode is PaletteMode.EMULATOR:
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
    if session is None or not session.palette_mode.has_external_file:
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
            # A FILE's or PALETTE's display name mirrors its on-disk basename,
            # so a located file that was renamed (or re-extensioned) takes the
            # new name; slices and bookmarks keep the names the user gave them.
            if entry.kind in (EntryKind.FILE, EntryKind.PALETTE):
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


def exportable_entries(ws: Workspace) -> list[Entry]:
    """The entries a bulk (whole-project) export should render, in list order.

    Every slice, plus every FILE that has **no** slices. A file that *has* slices
    is skipped: its slices are the curated regions worth exporting, so dumping the
    whole file alongside them would be redundant (and a whole ROM is rarely a
    useful image). A sliced file is exported only when the user names it
    explicitly (the single-entry Export), never in bulk — matching the rule that a
    file with slices isn't exported unless it alone is selected. Bookmarks and
    palettes hold no graphic of their own and never appear.
    """
    result: list[Entry] = []
    for entry in ws.entries:
        if entry.kind is EntryKind.SLICE:
            result.append(entry)
        elif entry.kind is EntryKind.FILE and not ws.slices_of(entry):
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
    """
    parent_stem = splitext(basename(entry.path))[0] or "export"
    if entry.kind is not EntryKind.SLICE:
        return _sanitize(parent_stem)
    return f"{_sanitize(parent_stem)}_{_sanitize(entry.name)}"


def _sanitize(name: str) -> str:
    cleaned = "".join(c if c in _SAFE_NAME else "_" for c in name).strip("._")
    return cleaned or "export"


def default_slice_name(
    offset: int, length: int | None, decompress_id: str = NO_DECOMPRESS
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
    if decompress_id != NO_DECOMPRESS:
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
