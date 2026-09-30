"""The session's open-entries collection: files, slices, bookmarks, palettes and
composites.

A :class:`Workspace` is the model behind the UI's open-files list. It holds an
ordered list of :class:`Entry` — a whole **file**, a **slice** (an
offset+length region of a parent file, optionally decompressed, that acts as
its own document), a **bookmark** (an offset into a parent file plus a
snapshot of settings, with no document or view of its own), a **palette**
(an external palette file, remembering the codec it was imported with, that
opens as a sheet of swatches and is also applied onto other entries), or a
**composite** (other entries' pixel bytes joined in order, owning no file) —
plus a *current* pointer for the single active view. A bookmark is never
current: it is jumped *through*, reconfiguring its parent.
It is session-lifetime only; persisting it is :mod:`celpix.project.projectfile`'s
job (``docs/design/project-format.md``).

The workspace is Qt-free. The UI subscribes to the plain callback lists
(``on_added`` …) to mirror changes into its list widget; nothing here knows
about widgets, documents' rendering, or the pipeline's execution — an entry
only *carries* its lazily loaded :class:`~celpix.core.document.Document` and
the config factory (:func:`pixel_config_for`) that tells the pipeline how to
read it.

**A slice of a file references its parent by path**, a **nested slice** — one
cut from another slice — references its parent slice by identity
(:attr:`Entry.parent_entry`), and **one region has one authority**: the parent
owns its bytes and a slice is a derived view of a window of them, to any depth
(``docs/design/slices-and-parents.md``). Reading is an ordinary bounded
:class:`~celpix.plugins.base.FileRef` served by the ordinary container — from the
file on disk, *except* where the parent's own buffer is the only truth (it holds
unsaved pixel edits, it reorders, or it is a slice whose decoded bytes are the
nested slice's coordinate space), when :func:`pixel_config_for` points the source
at that buffer instead (``FileRef.data``). Writing never deposits at those
bounds: the pathway is flagged ``writes_through_parent`` and the host folds the
slice into the parent's buffer and writes the *parent* — up the chain to the file,
so the file's container runs over bytes that changed inside it.

Cached documents of other entries on the same path go stale only when one of them
saves — :meth:`Workspace.invalidate_path` drops those caches (except dirty ones:
an invalidation must never discard in-memory changes) so they reload fresh on next
activation. A file rewritten on disk by another program is noticed and merged
by :mod:`celpix.project.diskchanges`, which the window drives.

This module holds the collection itself — :class:`Workspace`, and how its rows
are filed and sorted (:func:`section_kind`, :func:`sorted_entries`). What one
entry is lives in :mod:`celpix.project.entry`, how it is read in
:mod:`celpix.project.configs`, composites and swatch views in
:mod:`celpix.project.composites`, and what its row reports in
:mod:`celpix.project.entrystate`; all of it is re-exported here, so a caller
imports this one module.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from os.path import abspath, basename, normcase

from celpix.core.aspect import PixelAspect
from celpix.core.capabilities import ContentKind
from celpix.core.tilemap import LAYOUT_SPRITE, LAYOUT_TEXT
from celpix.plugins.base import (
    NO_COMPRESSION,
    NO_RESHAPE,
    RAW_CONTAINER,
)
from celpix.plugins.registry import Registry
from celpix.project.composites import (
    VIEW_AS_PALETTE_PRESET,
    can_compose,
    can_supply_palette,
    composite_config,
    composite_format_for,
    composite_layout,
    composite_preset_id,
    is_composable,
    is_swatch_preset,
    record_composite_layout,
    swatch_preset_id,
    swatch_session,
    swatch_session_for,
)
from celpix.project.configs import (
    backfill_slice_length,
    entry_view_bytes,
    interpret_params_for,
    outside_parent,
    own_bytes,
    pixel_config_for,
    reorders_bytes,
    tilemap_config_for,
)
from celpix.project.entry import (
    CompositeLayout,
    CompositePiece,
    Entry,
    EntryKind,
    EntrySession,
    LoadFailure,
    PaletteMode,
    PaletteSource,
    ParentSliceMissing,
    PieceSpan,
    SliceOutsideParent,
    SliceParams,
    SortKey,
    TileMode,
    TileSource,
    anchor_kind,
    new_composite,
    new_slice,
    slice_links,
    slice_of,
)
from celpix.project.entrystate import (
    MissingPreset,
    data_missing,
    default_slice_name,
    entry_export_name,
    entry_notices,
    entry_palette_entry,
    entry_palette_path,
    export_basename,
    exportable_entries,
    load_failed,
    missing_paths,
    one_disk_scan,
    palette_missing,
    palette_source_for,
    path_exists,
    path_is_palette_only,
    relocate_path,
    repair_presets,
    retarget_files,
    unavailable,
)

# The open-entries collection and, re-exported, everything the rest of the app
# asks about one entry: what it is (:mod:`celpix.project.entry`), how it is
# read (:mod:`celpix.project.configs`), what a composite or a swatch view
# assembles (:mod:`celpix.project.composites`) and what its row says
# (:mod:`celpix.project.entrystate`) — one module to import for all of it.
__all__ = [
    "VIEW_AS_PALETTE_PRESET",
    "CompositeLayout",
    "CompositePiece",
    "Entry",
    "EntryKind",
    "EntrySession",
    "LoadFailure",
    "MissingPreset",
    "PaletteMode",
    "PaletteSource",
    "ParentSliceMissing",
    "PieceSpan",
    "SliceOutsideParent",
    "SliceParams",
    "SortKey",
    "TileMode",
    "TileSource",
    "Workspace",
    "anchor_kind",
    "backfill_slice_length",
    "can_compose",
    "can_supply_palette",
    "composite_config",
    "composite_format_for",
    "composite_layout",
    "composite_preset_id",
    "data_missing",
    "default_slice_name",
    "entry_export_name",
    "entry_notices",
    "entry_palette_entry",
    "entry_palette_path",
    "entry_view_bytes",
    "export_basename",
    "exportable_entries",
    "file_kind",
    "interpret_params_for",
    "is_composable",
    "is_swatch_preset",
    "load_failed",
    "missing_paths",
    "new_composite",
    "new_slice",
    "one_disk_scan",
    "outside_parent",
    "own_bytes",
    "palette_missing",
    "palette_source_for",
    "path_exists",
    "path_is_palette_only",
    "pixel_config_for",
    "record_composite_layout",
    "relocate_path",
    "reorders_bytes",
    "repair_presets",
    "retarget_files",
    "section_kind",
    "slice_links",
    "slice_of",
    "sorted_entries",
    "swatch_preset_id",
    "swatch_session",
    "swatch_session_for",
    "tilemap_config_for",
    "unavailable",
]

# The digit runs :func:`_natural_key` compares as numbers. Captured, so the split
# keeps them and the key sees the whole name rather than only its text.
_DIGIT_RUN = re.compile(r"(\d+)")


class Workspace:
    """The ordered open-entries list + current pointer, with change callbacks."""

    def __init__(self) -> None:
        self.entries: list[Entry] = []
        self.current: Entry | None = None
        # Project-level (not per-entry) settings the project file persists. The
        # pixel-format filter is view-only — which codecs the Pixel dropdown
        # lists — so it rides on the workspace root rather than any one entry.
        self.hidden_pixel_presets: set[str] = set()
        # The shape one pixel is drawn at, for every surface in the window
        # (:mod:`celpix.core.aspect`). Here rather than on an entry because it is
        # a fact about the *screen* the project is being read on: a machine has
        # one, and two entries of the same project drawn at two shapes would be
        # two different claims about the same monitor.
        #
        # **None means nobody has answered yet**, which is not the same as
        # square: it is what leaves the question open for a container's hint to
        # settle on first load (:data:`~celpix.core.context.KEY_PIXEL_ASPECT`).
        # Once anything has answered — a hint or the user — the answer stands and
        # is what the project stores.
        self.pixel_aspect: PixelAspect | None = None
        self.on_added: list[Callable[[Entry], None]] = []
        self.on_removed: list[Callable[[Entry], None]] = []
        # Fired instead of per-entry removals when the whole list is swapped —
        # see :meth:`replace`. A listener that mirrors the list drops everything
        # it holds and rebuilds from the additions that follow.
        self.on_reset: list[Callable[[], None]] = []
        self.on_current_changed: list[Callable[[Entry | None], None]] = []
        self.on_dirty_changed: list[Callable[[Entry], None]] = []
        self._revision = 0  # allocator for per-entry revision tokens

    # -- lookups -----------------------------------------------------------
    @staticmethod
    def path_key(path: str) -> str:
        """``path`` reduced to what makes two references the same file.

        The project lives on a Windows drive but is used from both OSes, so path
        identity must survive case differences on the same file. Public because
        the same question is asked of lists this workspace does not hold — a
        paste works out where every row lands before it changes anything, and
        has to ask it against the list it is building.
        """
        return normcase(abspath(path))

    def _find(self, kind: EntryKind, path: str) -> Entry | None:
        key = self.path_key(path)
        for entry in self.entries:
            if entry.kind is kind and self.path_key(entry.path) == key:
                return entry
        return None

    def find_file(self, path: str) -> Entry | None:
        """The FILE entry for ``path``, if one is open (slices never match)."""
        return self._find(EntryKind.FILE, path)

    def find_palette(self, path: str) -> Entry | None:
        """The PALETTE entry for ``path``, if one is registered — same
        path-is-identity rule as :meth:`find_file`, per kind."""
        return self._find(EntryKind.PALETTE, path)

    def palette_render_targets(self, path: str) -> list[Entry]:
        """Loaded entries whose document mirrors the palette file at ``path``.

        Matched on the live palette config, not the saved session mode, so it is
        reliable *mid-switch* — the graphics to re-mirror the instant a file
        palette's colors change. Only entries with a document are returned; an
        unloaded one re-mirrors on its next load.

        Mirrors only: an **Entry**-mode reader of a slice of the file names the
        same path in its config, but its colours are a decode of the slice's run
        and follow it through :attr:`Entry.palette_entry`, not the whole file's.
        """
        key = self.path_key(path)
        out = []
        for entry in self.entries:
            if not entry.kind.has_document or entry.doc is None:
                continue
            # A palette entry's own document names its own file as its palette
            # source: it is the thing being mirrored *from*, never onto.
            if entry.kind is EntryKind.PALETTE or entry.palette_entry is not None:
                continue
            src = entry.doc.palette_config.source.path
            if src and self.path_key(src) == key:
                out.append(entry)
        return out

    def add_palette(
        self, path: str, preset_id: str | None, container_id: str = RAW_CONTAINER
    ) -> Entry:
        """Append a PALETTE entry for ``path`` (or return the one already there).

        The **non-undoable** registration, like :meth:`open_file` — used by the
        restore/self-heal path when a graphic references a ``.pal`` the project
        never registered. Interactive registration goes through an
        ``AddEntryCommand`` so it can be undone.

        ``container_id`` is detected by the caller, which holds the registry;
        plain bytes is the answer for the ``.pal`` this path usually gets and the
        one that leaves the entry reading its whole file.
        """
        existing = self.find_palette(path)
        if existing is not None:
            return existing
        entry = Entry(
            name=basename(path),
            kind=EntryKind.PALETTE,
            path=path,
            container_id=container_id,
            palette_preset_id=preset_id,
        )
        self.entries.append(entry)
        self._notify(self.on_added, entry)
        return entry

    def palette_consumers(self, palette: Entry) -> list[Entry]:
        """The graphics entries whose File-mode palette *is* this PALETTE file.

        The reverse of the file → palette reference: a File-mode graphic records
        its palette by path (its live config, or its pending source before load),
        so a shared ``.pal`` is matched by path-identity here — loaded or not.
        This is what lets removing a palette find, and re-home, every graphic that
        renders through it. Empty for anything but a PALETTE entry.
        """
        if palette.kind is not EntryKind.PALETTE:
            return []
        key = self.path_key(palette.path)
        users = []
        for entry in self.entries:
            if not entry.kind.has_document or entry.kind is EntryKind.PALETTE:
                continue  # a palette's own colours are its own, not a use of them
            session = entry.session
            if session is None or session.palette_mode is not PaletteMode.FILE:
                continue
            path = entry_palette_path(entry)
            if path is not None and self.path_key(path) == key:
                users.append(entry)
        return users

    def palette_entry_consumers(self, source: Entry) -> list[Entry]:
        """The entries whose ENTRY-mode palette is decoded from ``source``.

        :meth:`palette_consumers`' twin for the other cross-entry palette
        reference, and matched by **identity** rather than by path for the
        reason a composite piece is: the binding names the entry itself, so a
        file and a slice of it are different sources even where their bytes
        overlap, and a composite has no path to be matched by at all.

        The audience for a change to ``source``'s bytes: each of these holds a
        palette decoded out of them, so an edit — to the source, to a piece of
        it, or from another consumer — reaches them only if it is put there.
        Both loaded and unloaded consumers are returned; an unloaded one
        re-decodes on its next load anyway, which is why the caller filters.
        """
        return [
            entry
            for entry in self.entries
            if entry.kind.has_document and entry_palette_entry(entry) is source
        ]

    def slices_of(self, entry: Entry) -> list[Entry]:
        """The SLICE entries cut directly from ``entry``, in list order.

        One hop: a file's own slices, or a slice's nested ones — not theirs in
        turn. :meth:`descendants_of` is the whole subtree.
        """
        return [e for e in self.children_of(entry) if e.kind is EntryKind.SLICE]

    def children_of(self, entry: Entry) -> list[Entry]:
        """The entries anchored **directly** to ``entry``, in list order.

        A FILE's or a PALETTE's slices and bookmarks, matched by path, and a
        SLICE's nested slices, matched by identity (:attr:`Entry.parent_entry`).
        Empty for every other kind. A FILE and a PALETTE sharing a path are not
        each other's children, and nor are their slices: a child says which kind
        of row it was cut from (:attr:`Entry.parent_kind`) — which is also what
        keeps a nested slice, sharing its root file's path, off the file's list.
        """
        return self._children_in(entry, self.entries)

    @staticmethod
    def _children_in(entry: Entry, entries: list[Entry]) -> list[Entry]:
        """:meth:`children_of` against an arbitrary list — what :meth:`reorder`
        asks of the list it is *about* to commit, where the live one still holds
        the group it has lifted out."""
        is_child = Workspace.child_test(entry)
        return [e for e in entries if is_child(e)]

    @staticmethod
    def child_test(entry: Entry) -> Callable[[Entry], bool]:
        """The test :meth:`children_of` puts to each row, to ask of rows one at
        a time.

        For a caller after the *first* child in some stretch of the list: it can
        stop there, where building the whole list costs a :meth:`path_key` per
        row — asked once per row added, as a project load does, that is
        quadratic in the entries.
        """
        if entry.kind is EntryKind.SLICE:
            return lambda e: (
                e.kind is EntryKind.SLICE
                and e.parent_kind is EntryKind.SLICE
                and e.parent_entry is entry
            )
        if entry.kind not in (EntryKind.FILE, EntryKind.PALETTE):
            return lambda _e: False
        key = Workspace.path_key(entry.path)
        return lambda e: (
            e.kind in (EntryKind.SLICE, EntryKind.BOOKMARK)
            and e.parent_kind is entry.kind
            and Workspace.path_key(e.path) == key
        )

    def descendants_of(self, entry: Entry) -> list[Entry]:
        """Every entry anchored under ``entry`` at any depth, depth first.

        A child comes before its own children and siblings keep list order, which
        is the order the list itself holds a group in (:meth:`reorder` keeps it
        so) — so this is also the group's rows as they sit under ``entry``.
        """
        return self.descendants_in(entry, self.entries)

    @staticmethod
    def descendants_in(entry: Entry, entries: list[Entry]) -> list[Entry]:
        """:meth:`descendants_of` against an arbitrary list, in one pass over it —
        what a paste asks of the list it is building before any of it is added.

        The nested levels are indexed by parent once rather than asked of the
        list per slice: a ROM with hundreds of slices would otherwise make every
        close and every move quadratic in them. The seen-set is what ends a walk
        round a parent chain a hand-edited project made circular.
        """
        nested: dict[int, list[Entry]] = {}
        for e in entries:
            if e.kind is EntryKind.SLICE and e.parent_kind is EntryKind.SLICE:
                if e.parent_entry is not None:
                    nested.setdefault(id(e.parent_entry), []).append(e)
        out: list[Entry] = []
        seen = {id(entry)}

        def walk(children: list[Entry]) -> None:
            for child in children:
                if id(child) in seen:
                    continue
                seen.add(id(child))
                out.append(child)
                walk(nested.get(id(child), []))

        walk(
            nested.get(id(entry), [])
            if entry.kind is EntryKind.SLICE
            else Workspace._children_in(entry, entries)
        )
        return out

    def parent_of(self, entry: Entry) -> Entry | None:
        """The open entry a SLICE or BOOKMARK is anchored to.

        For a slice or bookmark of a file, the open FILE or PALETTE — which of
        the two whole-file kinds is the child's own word
        (:attr:`Entry.parent_kind`), because a ``.pal`` can be open as both at
        once and the path cannot tell them apart. For a **nested slice**, the
        parent slice it holds (:attr:`Entry.parent_entry`), while that is still
        in the list.

        None for the three kinds that anchor to nothing: a FILE and a PALETTE,
        whose path is their own file, and a **COMPOSITE**, which has no path at
        all. That last one is not merely tidy — a composite's ``path`` is ``""``,
        which :meth:`path_key` resolves to the working *directory*, so asking
        would be looking a file up by a name no file has.
        """
        if entry.kind in (EntryKind.FILE, EntryKind.PALETTE, EntryKind.COMPOSITE):
            return None
        if entry.parent_kind is EntryKind.SLICE:
            parent = entry.parent_entry
            # ``in`` compares identity here: Entry is eq=False.
            return parent if parent is not None and parent in self.entries else None
        return self._find(entry.parent_kind, entry.path)

    def ancestors_of(self, entry: Entry) -> list[Entry]:
        """``entry``'s parent, its parent's parent, and so on — nearest first.

        Stops at the first link that is not open, so the last element is the
        file the chain ends at only when the chain is whole
        (:meth:`root_of`). Guarded against a circular chain, which only a
        hand-edited project can hold and which then simply stops.
        """
        out: list[Entry] = []
        seen = {id(entry)}
        parent = self.parent_of(entry)
        while parent is not None and id(parent) not in seen:
            seen.add(id(parent))
            out.append(parent)
            parent = self.parent_of(parent)
        return out

    def root_of(self, entry: Entry) -> Entry | None:
        """The FILE or PALETTE ``entry``'s chain ends at, or None.

        The entry itself for a whole file. None where the chain does not reach
        one — a composite, a slice of a file that is not open, or a nested slice
        whose chain is broken on the way up. The one to ask wherever an answer is
        in **file** coordinates rather than a parent's: an Offset palette and a
        "this file" input binding both address the ROM beside the stream, not the
        stream a nested slice windows into.
        """
        if entry.kind in (EntryKind.FILE, EntryKind.PALETTE):
            return entry
        chain = self.ancestors_of(entry)
        top = chain[-1] if chain else None
        if top is None or top.kind not in (EntryKind.FILE, EntryKind.PALETTE):
            return None
        return top

    def chain_broken(self, entry: Entry) -> bool:
        """Whether a nested slice somewhere up ``entry``'s chain has lost its parent.

        A nested slice's bytes exist only as a window of its parent slice's
        decoded buffer, so with that parent gone there is nothing to read — and
        its offset is not a file offset, so falling back to the file would read
        bytes it never named. False for a slice of a file, whose parent may be
        closed without harm (it reads the file), and for everything else.
        """
        seen: set[int] = set()
        node = entry
        while node.kind is EntryKind.SLICE and node.parent_kind is EntryKind.SLICE:
            if id(node) in seen:
                return True  # a circular chain has no bytes at the end of it
            seen.add(id(node))
            parent = self.parent_of(node)
            if parent is None:
                return True
            node = parent
        return False

    def dirty_entries(self) -> list[Entry]:
        """Every entry with anything unsaved, on either pathway.

        Callers that ask "is there unsaved work?" — the close/replace prompts,
        Write All — mean both kinds; which files a write then touches is
        :meth:`Entry.pixel_dirty`/:attr:`Entry.palette_dirty`'s job, not this one's.
        """
        return [e for e in self.entries if e.pixel_dirty or e.palette_dirty]

    # -- mutations ---------------------------------------------------------
    def open_file(self, path: str, extra_paths: tuple[str, ...] = ()) -> Entry:
        """Add a FILE entry for ``path`` — or return the one already open.

        Identity is the (normalized) path: a document *is* its file, so opening
        it twice yields the same entry rather than a duplicate.

        ``extra_paths`` opens several files as **one** region, joined in the
        order given (an arcade board's graphics ROMs). ``path`` is still the
        entry's identity, so the already-open check is on it alone: reopening
        the first chip returns the region that was built from it rather than
        starting a second, competing document over the same bytes.
        """
        existing = self.find_file(path)
        if existing is not None:
            return existing
        entry = Entry(
            name=basename(path),
            kind=EntryKind.FILE,
            path=path,
            extra_paths=tuple(extra_paths),
        )
        self.entries.append(entry)
        self._notify(self.on_added, entry)
        return entry

    def add_slice(
        self,
        parent_path: str,
        name: str,
        offset: int,
        length: int | None,
        compression_id: str = NO_COMPRESSION,
        reshape_id: str = NO_RESHAPE,
        *,
        parent_kind: EntryKind = EntryKind.FILE,
    ) -> Entry:
        """Build a slice of ``parent_path`` and append it directly.

        The **non-undoable** add, like :meth:`open_file`. The UI does not take
        this path for slices the user creates: those go through an
        ``AddEntryCommand`` so the new entry can be undone, which means the
        command has to own the insertion (see :func:`new_slice`, which builds the
        entry the command then adds). This stays for callers with no undo stack
        to answer to — scripting the model directly, and the tests.

        The parent is looked up so the slice inherits its file list (see
        :func:`slice_of`); a parent that isn't open contributes only its path,
        which is right, since a closed one is a single file as far as anything
        here knows.

        ``parent_kind`` says which entry over ``parent_path`` is the parent: a
        ``.pal`` can be open as both a FILE and a registered PALETTE, and a
        slice of each is a different thing (:attr:`Entry.parent_kind`).
        """
        parent = self._find(parent_kind, parent_path)
        entry = (
            slice_of(parent, name, offset, length, compression_id, reshape_id)
            if parent is not None
            else new_slice(
                parent_path,
                name,
                offset,
                length,
                compression_id,
                reshape_id=reshape_id,
                parent_kind=parent_kind,
            )
        )
        # Placed by the same rule the undoable path uses — offset order among the
        # parent's children. Undo is the only thing the two adds differ in, and
        # letting them differ in *position* too would mean a slice landing
        # somewhere else depending on which of them made it.
        self.insert(entry, self.add_index_for(entry))
        return entry

    def add_slice_under(
        self,
        parent: Entry,
        name: str,
        offset: int,
        length: int | None,
        compression_id: str = NO_COMPRESSION,
        reshape_id: str = NO_RESHAPE,
    ) -> Entry:
        """:meth:`add_slice` given the parent entry — the non-undoable add for a
        slice of a slice, whose parent a path cannot name (:func:`slice_of`)."""
        entry = slice_of(parent, name, offset, length, compression_id, reshape_id)
        self.insert(entry, self.add_index_for(entry))
        return entry

    def insert(self, entry: Entry, index: int) -> None:
        """Insert an already-constructed entry at ``index`` (undo/redo path:
        re-adding restores the *same* Entry object, so its document, session
        and any commands referencing it stay valid)."""
        self.entries.insert(index, entry)
        self._notify(self.on_added, entry)

    def add_index_for(self, entry: Entry) -> int:
        """Where a newly created ``entry`` belongs in the list.

        The end, except a **slice or bookmark of an open parent** — a file, or a
        slice for a nested one — which lands in offset order among that parent's
        own children. That seeding is the only thing the offsets decide: from
        then on the order is the user's, so dragging a row moves it and an
        offset edit leaves it where it sits (:meth:`reorder`). Seeding rather
        than sorting is what lets both be true — a list nobody has arranged
        still reads low-to-high, which is the order slices are usually carved in.

        Offsets are compared among **siblings** only, since a nested slice's
        offset counts in its parent's decoded buffer and says nothing about
        where it falls among its parent's own siblings. A sibling is taken with
        its whole subtree, so a new row never lands between a slice and the
        slices nested under it.

        A child whose parent isn't open has no group to sort within, so it goes
        to the end like anything else.
        """
        parent = self.parent_of(entry)
        if parent is None or entry.kind not in (EntryKind.SLICE, EntryKind.BOOKMARK):
            return len(self.entries)
        siblings = self.children_of(parent)
        later = next((e for e in siblings if e.slice_offset > entry.slice_offset), None)
        if later is not None:
            return self.entries.index(later)
        # Past the parent's whole subtree, or straight after the parent when it
        # has none — never merely "at the parent's index + 1", which would bury
        # a new slice under the ones already there.
        group = [parent, *self.descendants_of(parent)]
        return max(self.entries.index(e) for e in group) + 1

    def reorder(self, entry: Entry, before: Entry | None) -> bool:
        """Move ``entry`` so it sits immediately in front of ``before``.

        The single reordering primitive, behind the drag gesture, Alt+Up/Down and
        the sorts: every kind's order is the user's, and one operation says so for
        all of them. ``before`` is the entry the moved row lands in front
        of, ``None`` for last in its own group. False when nothing moved.

        A **row takes everything under it along**: a file its slices and
        bookmarks, a slice the slices nested in it, to any depth. They are
        matched by path or by parent rather than position, so the list *could*
        leave them behind — but a parent has to precede its children for the
        panel to nest them (that is the order a project reload replays), so the
        whole group moves as one, and ``before`` names the row the group as a
        whole goes in front of.

        For a **slice or bookmark** ``None`` means after its parent's whole
        subtree rather than at the end of the list: a child dropped last in its
        parent's group is still that parent's child, and letting it drift past
        unrelated entries would only break the contiguity the move above relies
        on.

        What ``before`` does *not* have to be is the entry that immediately
        follows in this flat list. The panel groups rows into sections, so two
        rows adjacent on screen may have another section's file between them
        here; inserting in front of ``before`` gets their relative order right
        either way, which is the only thing the display reads.
        """
        group = [entry, *self.descendants_of(entry)]
        members = {id(member) for member in group}
        if before is not None and id(before) in members:
            return False
        rest = [e for e in self.entries if id(e) not in members]
        if before is not None:
            at = rest.index(before)
        elif entry.kind in (EntryKind.SLICE, EntryKind.BOOKMARK):
            parent = self.parent_of(entry)
            if parent is None:
                at = len(rest)
            else:
                tail = [parent, *self.descendants_in(parent, rest)]
                at = max(rest.index(e) for e in tail) + 1
        else:
            at = len(rest)
        rest[at:at] = group
        if rest == self.entries:  # Entry is eq=False, so this compares identity
            return False
        self.entries[:] = rest
        return True

    def restore_order(self, order: list[Entry]) -> bool:
        """Lay the whole list out in ``order`` — the same entries, rearranged.

        The exact inverse of a :meth:`reorder`, which the neighbour it names
        cannot always be: ``None`` sends a file to the end of the *flat* list,
        while it came from the end of its own section with another section's
        files possibly after it. The display reads the same either way, but the
        saved order does not. False — and nothing touched — when ``order`` is
        not a rearrangement of exactly the entries here, or already is the list.
        """
        if len(order) != len(self.entries) or {id(e) for e in order} != {
            id(e) for e in self.entries
        }:
            return False
        if order == self.entries:  # Entry is eq=False, so this compares identity
            return False
        self.entries[:] = order
        return True

    def close(self, entry: Entry, *, with_children: bool = True) -> list[Entry]:
        """Remove ``entry`` — and everything anchored under it, to any depth.

        A slice or bookmark nested under a closed parent would be an orphan in
        the list — and a nested slice under a closed parent slice would have no
        bytes at all — so the parent takes its whole subtree with it (the UI
        confirms first). Returns everything removed. If the current entry was among
        them, ``current`` moves to a list neighbour — skipping bookmarks, which
        cannot be current, and palettes, which are registered rather than
        browsed (:func:`_browsable`) — or None when no candidate remains.

        ``with_children=False`` removes ``entry`` alone. That is the undo of an
        *add*: children are matched by path, so a file opened under a slice or
        bookmark already in the list adopts it, and taking the adoptee out with
        the file would lose a row the add never put there.
        """
        removed = [entry, *(self.descendants_of(entry) if with_children else ())]
        anchor = min(self.entries.index(e) for e in removed)
        for e in removed:
            self.entries.remove(e)
            self._notify(self.on_removed, e)
        if self.current in removed:
            # A bookmark can never be current, so the neighbour search skips
            # it — and a palette, which can be shown but is *registered*
            # rather than browsed: closing a graphic should land on the next
            # graphic, not on the colour table beside it.
            after = self.entries[anchor:]
            before = reversed(self.entries[:anchor])
            neighbour = next(
                (e for e in after if _browsable(e)),
                next((e for e in before if _browsable(e)), None),
            )
            self.set_current(neighbour)
        return removed

    def replace(self, entries: list[Entry], current: Entry | None) -> None:
        """Swap the whole list for ``entries`` — a loaded project replaces the
        workspace, never merges into it.

        The old list goes as **one** ``on_reset``, not an ``on_removed`` per
        entry: closing a project is a single operation, and reporting it n times
        makes every listener pay its per-removal cost n times over for a list
        that is about to be empty anyway (a tree unwound row by row, a visit
        trail rebuilt per entry, a missing-file scan re-run over the shrinking
        remainder). Additions stay per-entry — the new list *is* built one entry
        at a time. ``current`` is set last, so the activation lands on a
        populated list.
        """
        self.set_current(None)
        self.entries.clear()
        for callback in list(self.on_reset):
            callback()
        self.entries.extend(entries)
        for entry in entries:
            self._notify(self.on_added, entry)
        self.set_current(current)

    def set_current(self, entry: Entry | None) -> None:
        if entry is self.current:
            return
        assert entry is None or entry in self.entries
        # A bookmark has no document or view of its own — it can never be shown.
        assert entry is None or entry.kind.has_document
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
        """Record ``revision`` on the entry's data pathway (an edit applying,
        or an undo putting the previous token back)."""
        self._set_revision(entry, "pixel_revision", revision)

    def set_palette_revision(self, entry: Entry, revision: int) -> None:
        """Record one on the entry's *palette* pathway, leaving its data alone."""
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
        """Discard an entry's cached document, preserving its palette source
        and its view.

        The palette must survive a document drop because for a **custom**
        palette the document is the *only* place its colors exist — nothing on
        disk backs them. Capturing the source into ``pending_palette`` hands
        them to the reload the same way a project restore does, so re-reading
        the pixel bytes never silently reverts an edited palette to the
        generated default. For the file-backed modes this is simply a
        re-resolution of the reference they already carry.

        The view survives on the same footing: once a load has consumed
        ``pending_view`` the document is the only place the entry's columns,
        palette row and offset exist, so a drop that did not stash them handed
        the reload the codec's defaults — a composite re-listed while on screen
        came back on palette row 0, black, until the project was reopened. A
        pending view not yet consumed is the newer answer and is left alone.
        """
        source = palette_source_for(entry)
        if source is not None:
            entry.pending_palette = source
        if entry.pending_view is None and entry.doc is not None:
            entry.pending_view = entry.doc.view
        entry.doc = None
        # A drop means something about what the entry reads has changed, which
        # is the one reason to try opening a failed entry again.
        entry.load_failure = None
        # A composite's spans describe the buffer that just went; keeping them
        # would leave the one question they answer being answered about bytes
        # nothing holds any more (:attr:`Entry.piece_spans`).
        entry.piece_spans = ()

    def invalidate_path(self, path: str, keep: Entry | None = None) -> None:
        """Drop cached documents of entries rooted at ``path`` (after a save).

        ``keep`` — the entry that just saved — retains its cache. Entries with
        unsaved changes on *either* pathway also retain theirs: their document
        holds those changes, and dropping it would silently lose them; they
        simply stay based on the pre-save bytes until written or explicitly
        reloaded.

        A region's later chips count as much as the file it is named after: a
        save that rewrites one of them leaves every other entry reading it —
        including one that only borrows it as its *second* file — holding stale
        bytes.
        """
        key = self.path_key(path)
        for entry in self.entries:
            if entry is keep or entry.pixel_dirty or entry.palette_dirty:
                continue
            if any(self.path_key(p) == key for p in entry.paths):
                self.drop_document(entry)

    @staticmethod
    def _notify(callbacks: list[Callable[[Entry], None]], entry) -> None:
        for callback in list(callbacks):
            callback(entry)


def _browsable(entry: Entry) -> bool:
    """Whether ``entry`` is what a view moves onto when the one shown goes away."""
    return entry.kind.has_document and entry.kind is not EntryKind.PALETTE


def section_kind(entry: Entry, registry: Registry | None = None) -> ContentKind:
    """Which section of the open-entries list ``entry``'s row is filed under.

    Almost always :attr:`Entry.content_kind` — the headings name what the bytes
    *are*, so that is what files them. The one exception is a **composite view
    read as swatches**: a join of a ROM's colour tables, byte ranges and pads and
    all, *is* the colour table a game assembles, and a colour table belongs with
    the palettes whatever kind of entry holds it
    (``docs/design/palette-editing.md``).

    Its ``content_kind`` stays PIXELS, deliberately and unchanged: that is what
    its capability set, :func:`can_compose` and :func:`can_supply_palette` are
    asked, and a composite is pixels to every one of them
    (``docs/design/composite-entry.md`` §1). Which *section* a row is filed under
    is a separate question, and this is the only place it is answered — so the
    tree, the position a new row lands at and the by-type sort cannot disagree
    about it.

    The other exception is stated rather than read off a format: a **palette
    file** is filed with the palettes by being one (:attr:`Entry.kind`), and so
    is every slice and bookmark cut from it, at any depth (:func:`anchor_kind`) —
    the row sits under its parent's, and its parent is in that section. Its
    ``content_kind`` is PIXELS for the same reason a swatch composite's is: what
    files a row and what its bytes are remain two questions.

    **Otherwise only a composite.** A graphics file or a slice of one read as
    swatches is a graphics file being *looked at* through a colour codec, which
    is a way of looking and not what the row is; the picker puts it back a
    moment later. A composite is assembled out of nothing but those runs, so the
    format is a statement about the entry itself.

    ``registry`` is what resolves the format to its engine. Without one — a panel
    built before the window has wired one up — every row files by its content
    kind, which is the right answer for everything but a swatch composite.
    """
    if entry.kind is EntryKind.PALETTE or (
        entry.kind in (EntryKind.SLICE, EntryKind.BOOKMARK)
        and anchor_kind(entry) is EntryKind.PALETTE
    ):
        return ContentKind.PALETTE
    if entry.kind is not EntryKind.COMPOSITE or registry is None:
        return entry.content_kind
    session = entry.session
    # A composite the user has never opened has no session yet; its seed is the
    # same answer :func:`composite_preset_id` gives the view it is about to get,
    # so a freshly assembled colour table is filed correctly on the first pass.
    preset = (
        session.pixel_preset_id
        if session is not None
        else composite_preset_id(entry, registry)
    )
    if not is_swatch_preset(preset, registry):
        return entry.content_kind
    return ContentKind.PALETTE


def file_kind(entry: Entry) -> ContentKind:
    """What ``entry``'s **file** holds, as its container and a resize read it.

    :attr:`Entry.content_kind` says what the entry's *bytes* are to the editor,
    and a palette file's are pixels — swatches — like a swatch composite's. Its
    container is a different reader: it cuts colours out of a file that frames
    them, counts them in colours, and is picked from the containers that frame
    a palette (``PluginInfo.content_kinds``). That is the one question this
    answers, and it is asked by the container and resize dialogs alone.
    """
    if entry.kind is EntryKind.PALETTE:
        return ContentKind.PALETTE
    return entry.content_kind


#: What **by type** means, in the order the rows land: the picture first, then
#: the three readings of a map — an even grid, the same cells placed freely, the
#: same cells read as words — and the palettes applied onto all of them last.
#: Keyed by string so one table covers both halves of the question, the content
#: kind's own value and the *layout* a tilemap's cell format declares (the same
#: declaration the row's icon reads).
_TYPE_ORDER: dict[str, int] = {
    ContentKind.PIXELS.value: 0,
    ContentKind.TILEMAP.value: 1,
    LAYOUT_SPRITE: 2,
    LAYOUT_TEXT: 3,
    ContentKind.PALETTE.value: 4,
}

#: Where bookmarks land in that order: after every kind of content, as one block.
#: A bookmark is a position rather than a region, so its content kind is whatever
#: it was built with (nothing sets one) and ranking it by that scatters the marks
#: through the pixel slices — which is the opposite of what a sort is asked for.
_BOOKMARK_RANK = max(_TYPE_ORDER.values()) + 1


def sorted_entries(
    entries: list[Entry],
    key: SortKey,
    *,
    layout: Callable[[Entry], str] | None = None,
    registry: Registry | None = None,
) -> list[Entry]:
    """``entries`` in ``key`` order — one group of rows, rearranged.

    Sorting acts on a *group* (a file's children, or the files of one section),
    because that is the only span whose order means anything: the list is flat
    here but nested and sectioned on screen, so a sort across the whole of it
    would rearrange rows the user cannot see together.

    By **offset** the name breaks ties, so two bookmarks on one position still
    land in a stable, readable order; by **name** the offset is not consulted at
    all — a group sorted by name and holding two of the same name keeps the order
    it had, Python's sort being stable. Offsets are the child kinds' question
    only: a file and a palette are the whole of their bytes and every one of them
    would answer 0.

    By **type** nothing breaks a tie, deliberately: sorts compose, so a group put
    in name order and then in type order reads as names within each type. It is
    the cheapest way to state "fontmaps last, alphabetically" and it is why the
    rank is the whole of the key.

    ``layout`` answers what a tilemap's cell **format** declares its cells to be
    (``"sprite"``, ``"text"``, or ``""`` for an even grid). Handed in because it
    is a question for the preset registry rather than for the entry, and only the
    type sort asks it: without one every map ranks as a plain tilemap, which is
    what an unrecognised format is anyway.

    ``registry`` is what the *section* question needs (:func:`section_kind`), so
    that a swatch composite sorts with the palettes it is filed among rather than
    with the pixel entries it is not.
    """
    if key is SortKey.OFFSET:
        return sorted(entries, key=lambda e: (e.slice_offset, _natural_key(e.name)))
    if key is SortKey.TYPE:
        return sorted(entries, key=lambda e: _type_rank(e, layout, registry))
    return sorted(entries, key=lambda e: _natural_key(e.name))


def _type_rank(
    entry: Entry,
    layout: Callable[[Entry], str] | None,
    registry: Registry | None = None,
) -> int:
    """Where ``entry`` sits in :data:`_TYPE_ORDER`.

    A bookmark is ranked by being one (:data:`_BOOKMARK_RANK`) rather than by its
    content kind, which it never had a reason to set. A tilemap is asked what its
    format lays its cells out as; everything else is asked which **section** it is
    filed under (:func:`section_kind`) — the same question the tree asks, so a
    sort can never put a row somewhere its heading says it is not. An unknown
    answer either way ranks with the plain reading of the kind it belongs to,
    since a map celPix has no format for is still a map and sorting is not the
    place to say otherwise.
    """
    if entry.kind is EntryKind.BOOKMARK:
        return _BOOKMARK_RANK
    if entry.content_kind is ContentKind.TILEMAP and layout is not None:
        declared = layout(entry)
        if declared in _TYPE_ORDER:
            return _TYPE_ORDER[declared]
    return _TYPE_ORDER.get(section_kind(entry, registry).value, 0)


def _natural_key(name: str) -> tuple[tuple[int, object], ...]:
    """A sort key that reads runs of digits as numbers, case-insensitively.

    Plain string order is wrong for exactly the names this list holds: default
    slice names lead with a hex offset (``0x800`` sorts after ``0x1000``
    lexically, once the widths differ), and hand-typed ones number their variants
    (``tile10`` before ``tile2``). Splitting on digit runs and comparing those as
    integers puts both right.

    Each part carries a leading flag saying which kind it is, so a number is never
    compared against text. Two names line up by kind on their own — the split
    always starts with a (possibly empty) text piece and alternates from there —
    but that is a property of the splitting, and the flag is what makes it one of
    the key, which is where a comparison actually happens.
    """
    return tuple(
        (1, int(part)) if part.isdigit() else (0, part.casefold())
        for part in _DIGIT_RUN.split(name)
    )
