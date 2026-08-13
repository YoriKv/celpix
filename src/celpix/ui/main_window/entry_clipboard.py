"""Cut, copy, paste and duplicate over the *rows* of the files pane.

What travels is an **entry** — a reference to a file plus the settings that file
is read through — and never the bytes behind it. Copying a slice puts its
offsets, codec and session on the clipboard; pasting it gives the project a
second way in to a region that is still written exactly once, and cutting a row
takes the way in away without touching anything on disk. This is the list
equivalent of :mod:`~celpix.ui.main_window.clipboard_ops`, which does the same
four verbs over tiles.

The payload is the **project form** of an entry, absolute-pathed
(:func:`~celpix.project.projectfile.entries_payload`). One writer for the file
and the clipboard means a paste can never carry less than a save does, and it is
what lets a copy cross between two celPix windows — and outlive either of them.

Two rules shape everything below.

**A file is its path.** ``find_file`` returns the entry for a path rather than
one of several, and a slice finds its parent by path, so a second row over one
file would be a second document over one buffer: two sets of unsaved edits, and
a write that silently drops one of them. So a file or a palette can be copied
freely and pasted *anywhere it is not already open*, and duplicating one in place
is refused. A slice and a bookmark carry no such identity — ``Entry`` is
``eq=False`` precisely because two of them may share coordinates — so those are
the kinds Duplicate is for.

**A pasted slice belongs to the file the paste is aimed at.** Pointing at a
second, differently-patched copy of a ROM and pasting a whole set of slices onto
it is the operation this exists for; falling back to the parent the copy
remembers is what makes a paste into an empty project restore what was copied.
"""

from __future__ import annotations

from dataclasses import replace
from os.path import basename, exists

from celpix.project import projectfile
from celpix.project.workspace import Entry, EntryKind, Workspace
from celpix.ui import clipboard
from celpix.ui.undo_commands import PasteEntriesCommand
from celpix.ui.widgets import counted

#: The two kinds that are a *window into* another file, and so the two a paste
#: has to find a parent for.
_CHILD_KINDS = (EntryKind.SLICE, EntryKind.BOOKMARK)

#: The kinds that may exist more than once, because their identity is not a path.
#: A superset of :data:`_CHILD_KINDS`, and the two were one list until a
#: **composite** turned out to be the second without being the first: it is
#: identified by nothing (its ``path`` is ``""``) and so may be pasted twice, but
#: it is a window into no file and has no parent to be found for it.
_MULTIPLE_KINDS = (*_CHILD_KINDS, EntryKind.COMPOSITE)

#: The slot a **tile binding** is remembered under in :meth:`~EntryClipboardMixin.
#: _live_bindings`, beside the numbered slots a composite's pieces use. Negative
#: so it can never collide with a piece index.
TILE_SLOT = -1


def _rows_at(
    entries: list[Entry], path: str, kinds: tuple[EntryKind, ...]
) -> list[Entry]:
    """The rows of ``kinds`` in ``entries`` whose path is ``path``.

    :class:`~celpix.project.workspace.Workspace`'s own lookups against a list the
    workspace does not hold yet: a paste works out every position before it
    changes anything, so each placement has to be decided against what the ones
    before it already added.
    """
    key = Workspace.path_key(path)
    return [e for e in entries if e.kind in kinds and Workspace.path_key(e.path) == key]


class EntryClipboardMixin:
    """The four clipboard verbs over the files pane's rows.

    A slice of :class:`~celpix.ui.main_window.window.MainWindow`, not a
    standalone object: it reads and writes the window's workspace and its undo
    stack. See the module docstring for what it owns, and the package docstring
    for why these are mixins.
    """

    # -- copying out ----------------------------------------------------------
    def _copy_entry(self, entry: Entry) -> None:
        """Put ``entry`` on the clipboard — with its children, if it is a file.

        A file takes its slices and bookmarks the way a removal does: they are
        windows into it and mean nothing without it, so a copy that left them
        behind would paste a ROM and lose the work of finding things inside it.
        """
        copied = self._entry_group(entry)
        clipboard.put_entries(
            projectfile.entries_payload(
                copied, self._workspace.entries, clipboard.SESSION_TOKEN
            ),
            [e.path for e in copied],
            self._live_bindings(copied),
        )
        self.statusBar().showMessage(f"Copied {self._describe(copied)}.")

    def _live_bindings(self, entries: list[Entry]) -> dict[tuple[int, int], Entry]:
        """Every entry a copied row points at, under the record key it is written
        with.

        The half of a copy that has to stay an **object**. A reference to an entry
        is held by identity everywhere else in the editor for a stated reason — a
        position "silently names a different entry the moment anything ahead of it
        is closed or reordered" (:class:`~celpix.project.workspace.TileSource`) —
        and a clipboard is precisely where that happens, since a copy outlives
        every drag, close and open between it and the paste.

        The key is ``(position at copy time, slot)``. The position is what the
        record carries as ``source_index`` — not a reference to anything, just the
        join between the two halves of one payload. The **slot** distinguishes the
        several references one row can hold: ``TILE_SLOT`` for a map's binding,
        and the piece's own index for a composite, which points at one entry per
        run rather than one in total.
        """
        positions = {id(e): i for i, e in enumerate(self._workspace.entries)}
        bound: dict[tuple[int, int], Entry] = {}
        for entry in entries:
            at = positions.get(id(entry))
            if at is None:
                continue
            source = entry.tile_source
            if source is not None and source.entry is not None:
                bound[(at, TILE_SLOT)] = source.entry
            for slot, piece in enumerate(entry.pieces):
                if piece.entry is not None:
                    bound[(at, slot)] = piece.entry
        return bound

    def _cut_entry(self, entry: Entry) -> None:
        """Copy ``entry`` and take it out of the list.

        No confirmation, unlike Remove — which is not an inconsistency but the
        difference between the two gestures. A cut says where the row is going,
        it is on the clipboard the moment it leaves, and the removal itself is
        one Ctrl+Z away with the entry's document and session intact. Remove says
        only "gone", which is why it asks.

        The one cut that still asks is a **palette other entries render**:
        removing it re-homes each of them onto a frozen copy of its colors, which
        is a change to those entries rather than to this row, and the prompt
        naming them is the only place that is said.
        """
        self._copy_entry(entry)
        self._remove_entry(entry, confirm=False)

    def _entry_group(self, entry: Entry) -> list[Entry]:
        """``entry`` and the rows that travel with it, in list order."""
        return [entry, *self._workspace.children_of(entry)]

    @staticmethod
    def _describe(entries: list[Entry]) -> str:
        """``'"tiles.bin" and 2 slices'`` — what a status line calls a group."""
        first = f'"{entries[0].name}"'
        rest = len(entries) - 1
        return first if not rest else f"{first} and {counted(rest, 'row')}"

    # -- pasting in -----------------------------------------------------------
    def _paste_entries(self, target: Entry | None) -> None:
        """Add the clipboard's entries, aimed at ``target``'s row."""
        payload = clipboard.take_entries() or {}
        copied = projectfile.entries_from_payload(payload)
        if not copied:
            self.statusBar().showMessage("Nothing on the clipboard to paste here.")
            return
        # The remembered bindings belong to whichever copy this process took last,
        # so they are only this payload's if this process wrote it. A copy from
        # another window keys nothing here, and its maps arrive unbound.
        bindings = clipboard.take_bindings() if self._is_this_session(payload) else {}
        self._place_copies(copied, target, bindings=bindings)

    def _duplicate_entry(self, entry: Entry) -> None:
        """A second row over the same region, without touching the clipboard.

        Only where a second row can *mean* something — a slice or a bookmark. On
        a file or a palette the answer is a message rather than a disabled key: a
        row's identity being its path is not obvious from looking at it, and
        Ctrl+D doing nothing at all would read as a bug.
        """
        if entry.kind not in _MULTIPLE_KINDS:
            what = "A file" if entry.kind is EntryKind.FILE else "A palette"
            self.statusBar().showMessage(
                f"{what} can only be open once - copy it into another project "
                "instead, or duplicate one of its slices."
            )
            return
        # Round-tripped through the payload rather than deep-copied by hand, so a
        # duplicate is the same operation as a paste and cannot drift from it —
        # but through memory, leaving whatever is on the clipboard alone.
        payload = projectfile.entries_payload(
            [entry], self._workspace.entries, clipboard.SESSION_TOKEN
        )
        copied = projectfile.entries_from_payload(payload)
        if copied:
            self._place_copies(
                copied,
                entry,
                bindings=self._live_bindings([entry]),
                verb="Duplicated",
            )

    @staticmethod
    def _is_this_session(payload: dict) -> bool:
        """Whether the copy was taken by *this* running editor — which is what
        says the bindings it left in memory are the ones this payload means."""
        return projectfile.payload_session(payload) == clipboard.SESSION_TOKEN

    def _place_copies(
        self,
        copied: list[projectfile.CopiedEntry],
        target: Entry | None,
        *,
        bindings: dict[int, Entry],
        verb: str = "Pasted",
    ) -> None:
        """Work out where every copied entry lands, then push it as one step.

        Everything is decided here and nothing during the apply: the command
        holds finished (index, entry) placements, so its redo lands exactly what
        its first run did rather than re-deriving positions against a list that
        has moved on. Those indices are safe to hold because undo is
        last-in-first-out — anything that moved a row since is taken back before
        this step is — which is the same reasoning an undone removal restores by.
        """
        host = self._paste_host(target)
        # The files the payload brought with it. A child whose own parent is in
        # the copy stays with that parent wherever it lands, and only a child
        # pasted *alone* is re-aimed at the targeted row — copying a ROM and its
        # slices onto another project must not scatter the slices over whichever
        # file happened to be right-clicked.
        carried = {
            Workspace.path_key(record.entry.path)
            for record in copied
            if record.entry.kind is EntryKind.FILE
        }
        placed: list[Entry] = []
        skipped: list[str] = []
        # The files the copy could not bring, because a row already holds their
        # path. Their children stay behind with them: a file takes its slices in
        # both directions, so a copy of a whole ROM pasted back into the project
        # it came from adds nothing rather than a second set of its slices.
        left_behind: set[str] = set()
        # A running copy of the list, so each placement is worked out against
        # what the ones before it already added — two slices pasted together must
        # not both claim the same index.
        pending = list(self._workspace.entries)
        placements: list[tuple[int, Entry]] = []

        def place(entry: Entry) -> None:
            at = self._paste_index(entry, pending)
            pending.insert(at, entry)
            placements.append((at, entry))
            placed.append(entry)

        for record in copied:
            entry = record.entry
            key = Workspace.path_key(entry.path)
            if entry.kind not in _MULTIPLE_KINDS:
                if _rows_at(pending, entry.path, (entry.kind,)):
                    skipped.append(entry.name)  # a file is its path; never twice
                    left_behind.add(key)
                    continue
            elif entry.kind in _CHILD_KINDS:
                if key in left_behind:
                    continue
                own = key in carried
                parent = (None if own else host) or next(
                    iter(_rows_at(pending, entry.path, (EntryKind.FILE,))), None
                )
                if parent is not None:
                    self._reparent(entry, parent)
                elif exists(entry.path):
                    # Pasted into a project that has never seen the file the
                    # slice cuts into: open it too, so the row arrives nested
                    # under something rather than loose beside the files. Only
                    # when the file is really there — a paste onto a machine that
                    # hasn't got it should not invent an entry that cannot load.
                    place(self._file_entry_for(entry))
            entry.name = self._free_name(entry, pending)
            place(entry)
        if not placements:
            self.statusBar().showMessage(
                f"Already open: {', '.join(skipped)}."
                if skipped
                else "Nothing to paste here."
            )
            return
        self._rebind_copies(copied, placed, bindings)
        first = next(
            (e for _, e in placements if e.kind.has_document),
            None,
        )
        self._push_command(
            PasteEntriesCommand(
                self,
                placements,
                f"paste {self._describe(placed)}",
                activate=first,
            )
        )
        note = f" ({len(skipped)} already open)" if skipped else ""
        self.statusBar().showMessage(f"{verb} {self._describe(placed)}{note}.")

    def _paste_host(self, target: Entry | None) -> Entry | None:
        """The file a pasted slice or bookmark should be cut out of.

        The targeted row's own file: a file row is itself, a slice or bookmark
        row is the file it already belongs to. A palette row names no file to cut
        into, so it aims at nothing and the copy falls back to the parent it
        remembers.
        """
        if target is None:
            return None
        if target.kind is EntryKind.FILE:
            return target
        return self._workspace.parent_of(target)

    @staticmethod
    def _reparent(entry: Entry, parent: Entry) -> None:
        """Re-anchor a copied slice or bookmark onto ``parent``.

        Its **offsets are kept**, which is the whole use: pasting a set of slices
        onto a second dump of the same ROM is how one finds the same regions in
        it. What has to change with the parent is the file list those offsets are
        counted against — a region spread over several chips is addressed as the
        join, so a child carries its parent's whole list or means nothing
        (:attr:`~celpix.project.workspace.Entry.extra_paths`).
        """
        entry.path = parent.path
        entry.extra_paths = parent.extra_paths

    @staticmethod
    def _file_entry_for(child: Entry) -> Entry:
        """A FILE row for the file a pasted child cuts into."""
        return Entry(
            name=basename(child.path),
            kind=EntryKind.FILE,
            path=child.path,
            extra_paths=child.extra_paths,
        )

    @staticmethod
    def _paste_index(entry: Entry, pending: list[Entry]) -> int:
        """Where a pasted row goes: last in its group.

        Not the offset-sorted position a freshly *carved* slice gets. A paste is
        an arrangement the user is making by hand — several rows in the order
        they copied them — and dropping each one into the middle of the existing
        list by its offset would scatter it.
        """
        if entry.kind not in _CHILD_KINDS:
            return len(pending)
        siblings = _rows_at(pending, entry.path, _CHILD_KINDS)
        if siblings:
            return pending.index(siblings[-1]) + 1
        parent = next(iter(_rows_at(pending, entry.path, (EntryKind.FILE,))), None)
        return pending.index(parent) + 1 if parent is not None else len(pending)

    def _free_name(self, entry: Entry, pending: list[Entry]) -> str:
        """``entry``'s name, made distinct from the rows it is landing among.

        Only against its own group, and only when it collides: a slice pasted
        onto a *different* file keeps the name it was copied under, since there
        is nothing there for it to be confused with. A duplicate made in place
        always collides, which is what makes " copy" the ordinary outcome of
        Ctrl+D and an exception everywhere else.
        """
        group = self._name_group(entry, pending)
        taken = {e.name for e in group}
        if entry.name not in taken:
            return entry.name
        candidate = f"{entry.name} copy"
        n = 2
        while candidate in taken:
            candidate = f"{entry.name} copy {n}"
            n += 1
        return candidate

    @staticmethod
    def _name_group(entry: Entry, pending: list[Entry]) -> list[Entry]:
        """The rows a pasted one has to be told apart from — its siblings under
        the same file, or the top-level rows of the same kind."""
        if entry.kind in _CHILD_KINDS:
            return _rows_at(pending, entry.path, _CHILD_KINDS)
        return [e for e in pending if e.kind is entry.kind]

    @staticmethod
    def _rebind_copies(
        copied: list[projectfile.CopiedEntry],
        placed: list[Entry],
        bindings: dict[tuple[int, int], Entry],
    ) -> None:
        """Point every reference a copied row holds back at an entry.

        Two kinds of row hold one: a **tilemap**, at the bank it draws, and a
        **composite view**, at each of the entries its runs are assembled from
        (``docs/design/composite-entry.md``). Both are resolved the same way,
        because both are the same problem — an entry is not a value, so a copy
        cannot carry one — and two things can answer it, asked in this order:

        - the target was **copied alongside** the row, so the pasted copy of it is
          what the row should point at — a duplicated map keeps pointing at the
          bank it came with rather than reaching back past it. Matched inside the
          payload, where the recorded position and ``source_index`` are two
          numbers written against one snapshot and mean the same thing by
          construction;
        - the target is **still open here**, in which case ``bindings`` has the
          entry itself (:data:`~celpix.ui.clipboard._COPIED_BINDINGS`). Deliberately
          the object and not a position: a copy sits on the clipboard across every
          drag, close and open the user makes before pasting, and a number written
          when it was taken would by then name whatever had moved into that place.

        Anything else — a copy pasted into another window, or into the same one
        after the target was closed — resolves to nothing, and the two kinds
        degrade the way each already does elsewhere: a map goes **unbound**
        (placeholder cells, re-pointable, never somebody else's tiles), and a
        composite's run becomes a **pad of the length it had**, so the pieces
        after it stay on the index every map addressing them expects.
        """
        by_source = {
            record.source_index: record.entry
            for record in copied
            if record.source_index >= 0
        }

        def target_for(record: projectfile.CopiedEntry, at: int, slot: int):
            copied_too = by_source.get(at) if at >= 0 else None
            return copied_too or bindings.get((record.source_index, slot))

        for record in copied:
            if record.entry not in placed:
                continue
            if record.tile_source is not None:
                found = target_for(record, record.tile_source_index, TILE_SLOT)
                record.entry.tile_source = (
                    replace(record.tile_source, entry=found)
                    if found is not None
                    else None
                )
            if record.entry.pieces:
                record.entry.pieces = tuple(
                    replace(piece, entry=target_for(record, at, slot))
                    for slot, (piece, at) in enumerate(
                        zip(record.entry.pieces, record.piece_sources)
                    )
                )

    # -- the undo command's two directions --------------------------------------
    def _apply_paste_entries(
        self, placements: list[tuple[int, Entry]], activate: Entry | None
    ) -> None:
        """Insert the pasted rows at their recorded positions and show the first.

        Ascending order, like an undone removal: a file goes back before the
        slices that nest under it, so the panel has something to hang them on as
        they arrive.
        """
        for index, entry in sorted(placements, key=lambda pair: pair[0]):
            self._workspace.insert(entry, min(index, len(self._workspace.entries)))
        self._sync_locate_action()
        # Asked once the rows are in: it is their arrival that gives a map back
        # the art it was bound to, exactly as a restore does.
        self._reresolve_bound_art(self._maps_drawing_from([e for _, e in placements]))
        if activate is not None:
            self._activate_entry(activate)

    def _apply_unpaste_entries(self, placements: list[tuple[int, Entry]]) -> None:
        """Take the pasted rows back out — the command's undo.

        Reverse order, and each one checked for still being there: a pasted file
        and a pasted slice of it are two placements, and closing the file already
        takes the slice with it.
        """
        for _index, entry in reversed(placements):
            if entry in self._workspace.entries:
                self._apply_close_entry(entry)
