"""Putting edits back on disk, and the one rule that makes a region safe to.

Every path that writes bytes out — File ▸ Write, Write All, the files dock's own
Write, and the unsaved-changes gate the project and quit paths go through first.

**One region, one authority** (``docs/design/slices-and-parents.md``). A slice's
bytes are a *derived* view of a window of its parent's, so the parent owns them —
and a nested slice's parent is another slice, whose own parent owns *its* bytes,
up to the file. Four methods carry that between them and none of them is where
the rule lives: :meth:`~WritingMixin._propagate_pixel_edit` records what a slice
edit owes up its chain as it lands, :meth:`~WritingMixin._fold_slice_edits_into`
is the fold itself, :meth:`~WritingMixin._write_pixels_through_parent` routes a
save out through the chain so the file's container runs, and
:meth:`~WritingMixin._mark_region_saved` settles who is clean afterwards. Keeping
the four in one module is the point of the module: split up, each reads like a
special case of the others.

Writing is **per pathway**. A palette-only edit leaves the graphic untouched,
because the two live in different files and rewriting unchanged pixel bytes is at
best a needless mtime bump (``docs/design/palette-editing.md`` §2).

What *creates* the entries being written — projects, slices, bookmarks — is
:mod:`~celpix.ui.main_window.entries`; where the view goes between them is
:mod:`~celpix.ui.main_window.session`.
"""

from __future__ import annotations

from pathlib import Path

from celpix.core.context import KEY_SOURCE_OFFSET
from celpix.core.document import Document
from celpix.core.errors import PipelineError
from celpix.pipeline import pipeline
from celpix.project.workspace import Entry, EntryKind, unavailable
from celpix.ui.widgets import confirm_destructive, counted


def _and_list(names: list[str]) -> str:
    """``a``, ``a and b``, ``a, b and c`` — what one write can have landed in.

    Three files is the tilemap case: the map's own cells, the entry it borrows
    its tiles from, and the palette file on screen.
    """
    if len(names) < 3:
        return " and ".join(names)
    return f"{', '.join(names[:-1])} and {names[-1]}"


class WritingMixin:
    """Write-back to disk, and the file/slice reconciliation behind it.

    A slice of :class:`~celpix.ui.main_window.window.MainWindow`, not a
    standalone object: it reads and writes the window's own widgets and its
    single live ``_doc``. See the module docstring for what it owns, and the
    package docstring for why these are mixins.
    """

    def _resolve_dirty_entries(
        self,
        consequence: str,
        *,
        write_label: str = "Write All",
        skip_label: str = "Continue Without",
        default_write: bool = False,
    ) -> bool:
        """Unsaved-file-changes gate; True when OK to proceed.

        The one prompt for "there are unsaved edits in memory": write them to
        disk first (Accept), go ahead without doing so (Destructive), or cancel
        the whole action. The middle option's meaning - and so its label - is the
        caller's: project save/load *keeps* the edits in memory for later
        ("Continue Without"), while quitting drops them for good ("Discard"), so
        that path also defaults to writing, the least-lossy choice when Enter is
        hit blind. A project can't represent unsaved bytes either way, which is
        why saving/loading one has to resolve them first.
        """
        dirty = self._workspace.dirty_entries()
        if not dirty:
            return True
        names = ", ".join(e.name for e in dirty)

        def write() -> bool:
            self._write_all()
            # A write that failed left its entry dirty - don't proceed past it.
            return not self._workspace.dirty_entries()

        return confirm_destructive(
            self,
            "celPix - unsaved changes",
            f"{consequence} ({names}). Write them to disk first?",
            write_label,
            skip_label,
            write,
            default_safe=default_write,
        )

    def _write_current(self) -> None:
        """File ▸ Write: what is on screen — its own file, its tiles, its palette.

        Ctrl+W means "save what I am looking at", and on two kinds of entry that
        is more than one file, because celPix put the edit where its owner is
        rather than where it was made.

        A File-mode palette is owned by its own PALETTE entry, so a color edit
        dirties *that* and never the graphic rendering it
        (``docs/design/palette-editing.md`` §2). A **tilemap** draws tiles it
        borrows, so a pixel edit made on the map was deposited into the entry
        those bytes belong to and the map itself reads clean
        (``docs/design/tilemap-entry.md`` §8.1) — which left a painted map
        reporting a successful write with the painting still only in memory,
        recoverable just by finding the bank in the Files list. A **composite
        view** is the same story with several files at the end of it: it owns no bytes,
        so a stroke on one was deposited into whichever pieces it crossed, and
        every one of those is written here (``docs/design/composite-entry.md``).

        The companions are written **only when they actually have changes**:
        rewriting a clean ``.pal`` or a clean tile bank would bump the mtime of a
        file other entries may share, for nothing.

        They are separate files and are written independently, so a failure on
        one (already reported) doesn't take the others with it; the status line
        names whatever really landed.
        """
        entry = self._workspace.current
        if entry is None or entry.doc is None:
            return
        # Read before the write, which clears the flags it acts on.
        palette_only = entry.palette_dirty and not entry.pixel_dirty
        has_palette_file = entry.doc.palette_config.write_enabled
        palette = self._linked_palette_entry()
        # A palette file on screen is its own linked palette, and one write of
        # it covers both halves; a colour edit made through the dock dirties
        # its bytes as well as its colours, so either flag means unsaved.
        if (
            palette is None
            or palette is entry
            or palette.doc is None
            or not (palette.palette_dirty or palette.pixel_dirty)
        ):
            palette = None
        owners = self._unsaved_owners(entry)
        # A palette read out of another entry's bytes is that entry's to write,
        # the way a painted bank is — named apart from the tile owners because
        # what landed there is colours (``docs/design/palette-editing.md``).
        palette_owners = [
            e
            for e in self._unsaved_palette_owners(entry)
            if not any(e is seen for seen in owners)
        ]
        wrote_entry = self._write_entry(entry)
        # After the map, so the map's own write is what a failure on the bank is
        # reported against; the order is otherwise free, since a save skips the
        # cached documents of entries that are still dirty when it invalidates
        # its path (``Workspace.invalidate_path``).
        wrote_owners = [owner for owner in owners if self._write_owner(owner)]
        wrote_palette_owners = [e for e in palette_owners if self._write_owner(e)]
        wrote_palette = palette is not None and self._write_entry(palette)
        # Report what actually went to disk: a palette-only write leaves the
        # graphic alone, and Default/Custom/Emulator palettes have no file
        # behind them at all (docs/design/palette-editing.md).
        wrote = (
            "palette"
            if palette_only
            else "pixel + palette"
            if has_palette_file
            else "pixel"
        )
        # A **composite** owns no bytes, so its own write is its palette or
        # nothing at all — saying "pixel" there would name a file that was never
        # touched, while the pieces that really were are listed below.
        if entry.kind is EntryKind.COMPOSITE:
            wrote_entry = wrote_entry and has_palette_file
            wrote = "palette"
        landed = [f"{entry.name} ({wrote})"] if wrote_entry else []
        landed += [f"{owner.name} (tiles)" for owner in wrote_owners]
        landed += [f"{owner.name} (colors)" for owner in wrote_palette_owners]
        if wrote_palette:
            landed.append(palette.name)
        if landed:
            self.statusBar().showMessage(f"Wrote {_and_list(landed)}.")

    def _write_owner(self, owner: Entry) -> bool:
        """Write an entry whose bytes another view holds — if it still has any.

        Writing one slice of a file writes the **whole region**: the parent's
        buffer holds every slice's folded edits, so the write marks its siblings
        saved and drops their documents
        (:meth:`~celpix.project.workspace.Workspace.invalidate_path`). A second
        piece of the same file has therefore already gone out by the time this
        reaches it, and has nothing left to write from. False is the honest
        report — nothing further landed — rather than an assertion on a document
        the first write deliberately took away.
        """
        return owner.doc is not None and self._write_entry(owner)

    def _unsaved_owners(self, entry: Entry) -> list[Entry]:
        """The entries holding ``entry``'s bytes, where those have unsaved edits.

        Empty for everything that is not a tilemap with a painted-on bank, or a
        composite with a painted-on piece: a pixel entry owns its own art
        (:meth:`~...session.SessionMixin._tile_bank_owner` answers with the entry
        itself there), an unbound map has no bank at all, and one whose bank is
        clean has nothing that needs writing — a map may be opened, its cells
        edited and written a dozen times without anybody having touched a tile.

        A **list** because a composite view has as many owners as it has pieces,
        and one stroke can cross two of them. A map bound to one reaches them the
        same way: its bank is the composite, and the composite's own answer is
        its pieces.

        "Owners" rather than "tile sources", though a bound map is the commonest
        way to get here: :class:`~celpix.project.workspace.TileSource` is a
        specific thing in this codebase — a map's binding — and a composite's
        pieces are not one (``docs/design/composite-entry.md``). What these all
        have in common is owning bytes somebody else is showing.

        The **pixel** flag alone, which is the one a deposit sets. A palette edit
        made while looking at the map dirties the map's own entry, so a bank
        carrying one was edited on its own account and is the user's to write
        when they go back to it.
        """
        owner = self._tile_bank_owner(entry)
        if owner is None:
            return []
        if owner.kind is EntryKind.COMPOSITE:
            return self._dirty_composite_pieces(owner)
        if owner is entry or owner.doc is None:
            return []
        return [owner] if owner.pixel_dirty else []

    def _unsaved_palette_owners(self, entry: Entry) -> list[Entry]:
        """The entries holding ``entry``'s **palette** bytes, where those are dirty.

        Empty for every mode but ENTRY, whose colours live in another entry's
        buffer: a colour edit was deposited there and that entry's Write is what
        puts it on disk, exactly as a painted tile bank's is
        (``docs/design/palette-editing.md``). A composite source has as many
        owners as it has pieces, and answers with them.

        An Offset palette needs nothing here: its owner is the entry's own file
        (or its parent), which the write already routes through.
        """
        source = self._entry_palette_target(entry)
        if source is None:
            return []
        if source.kind is EntryKind.COMPOSITE:
            return self._dirty_composite_pieces(source)
        if source is entry or source.doc is None:
            return []
        return [source] if source.pixel_dirty else []

    def _dirty_composite_pieces(self, entry: Entry) -> list[Entry]:
        """``entry``'s source pieces with unsaved pixel edits, each named once.

        Where a composite's Write actually goes. A piece may appear twice in one
        composite — the same slice used at two places in a window is an ordinary
        thing to want — and writing it twice would be one redundant round trip through
        its container, so the run of pieces is reduced to the set of entries
        behind it.
        """
        out: list[Entry] = []
        for piece in entry.pieces:
            source = piece.entry
            if source is None or not source.pixel_dirty or source.doc is None:
                continue
            if not any(seen is source for seen in out):
                out.append(source)
        return out

    def _write_all(self) -> None:
        """File ▸ Write All: every entry with unsaved in-memory changes."""
        dirty = self._workspace.dirty_entries()
        written = [e.name for e in dirty if e.doc is not None and self._write_entry(e)]
        if written:
            self.statusBar().showMessage(
                f"Wrote {len(written)} item(s): {', '.join(written)}."
            )

    def _write_entry_checked(self, entry: Entry) -> None:
        """The files dock's context-menu Write - guards, then writes."""
        if entry.doc is None:
            return
        if entry.kind is EntryKind.COMPOSITE:
            # A composite has no file of its own, so "write it" means write the
            # pieces its edits were deposited into. Reporting it as view-only
            # would be true of the assembled buffer and false of everything the
            # user did to it (``docs/design/composite-entry.md``).
            written = [
                piece.name
                for piece in self._dirty_composite_pieces(entry)
                if self._write_entry(piece)
            ]
            # Its **own palette** is the one thing a composite has to write for
            # itself: an Offset palette on one is read out of the file its first
            # file-backed piece came from, and that window belongs to no piece's
            # pixel pathway (``docs/design/palette-editing.md`` §2). Left out, the
            # colour edit stayed unsaved while this said nothing had changed.
            if (
                entry.palette_dirty
                and entry.doc.palette_config.write_enabled
                and self._write_entry(entry)
            ):
                written.append(f"{entry.name}'s palette")
            self.statusBar().showMessage(
                f"Wrote {_and_list(written)}."
                if written
                else f"{entry.name} is assembled from other entries, and none of "
                "them has unsaved changes."
            )
            return
        # A PALETTE entry writes its own file through whichever half carries
        # the edit — its colours, or its bytes when a swatch was painted or a
        # slice folded in; every other entry writes its graphic, which is
        # view-only when any stage it reads through has no save-side half to
        # put the bytes back with.
        writable = (
            entry.doc.palette_config.write_enabled
            or entry.doc.data_config.write_enabled
            if entry.kind is EntryKind.PALETTE
            else entry.doc.data_config.write_enabled
        )
        if not writable:
            self._alert(
                f"{entry.name} is view-only - one of the stages it reads through "
                "has no way to write back (a compression scheme with no "
                "compressor, a reshape with no inverse, a missing plugin), so it "
                "can't be saved.",
                title="celPix - write",
            )
            return
        if self._write_entry(entry):
            self.statusBar().showMessage(f"Wrote {entry.name}.")

    def _write_entry(self, entry: Entry) -> bool:
        """Save one entry through the pipeline; True on success.

        Writes only the pathway that needs it: when the **palette alone** is
        dirty the graphic is left untouched, since the two live in different
        files and rewriting unchanged pixel bytes is at best a needless mtime
        bump (docs/design/palette-editing.md §2). Any other case - pixel edits,
        or an explicit Write on a clean entry - writes both, as it always did.

        A successful write invalidates the cached documents of other entries on
        the same file (their bytes are now stale) - including the one on screen
        when a slice is written back under its parent's feet, which is re-read
        immediately so the view shows the freshly written bytes.

        Pixels still floating over the current entry are set down first: a float
        is on screen but not in the document, and a file that doesn't match what
        the user is looking at is not what Write means.

        A slice takes the long way round (:meth:`_write_pixels_through_parent`)
        — through its parent, and up a chain of them for a nested slice — so the
        file's container is what puts the bytes down.
        """
        assert entry.doc is not None
        if entry is self._workspace.current:
            self._commit_float()
        self._capture_session()  # keep the current entry's session snapshot fresh
        palette_only = entry.palette_dirty and not entry.pixel_dirty
        # The entry's own data decides the route: a map carved from a file writes
        # its cells through that file like any slice, whatever the tile bank it
        # borrows its art from would do.
        via_parent = not palette_only and entry.doc.data_config.writes_through_parent
        writes_region = not (palette_only or via_parent)
        try:
            if via_parent and not self._write_pixels_through_parent(entry):
                return False
            if writes_region:
                # Belt and braces over the fold each slice edit already did: a
                # slice edited while this had no document folds here instead.
                self._fold_slice_edits_into(entry)
            pipeline.save(entry.doc, self._registry, pixel=writes_region)
        except PipelineError as exc:
            self._report(exc)
            return False
        self._workspace.mark_saved(entry, pixel=not palette_only)
        if entry.kind is EntryKind.SLICE and not palette_only:
            entry.fold_refused = None  # its own write just put the bytes down
        if writes_region and entry.kind in (EntryKind.FILE, EntryKind.PALETTE):
            self._mark_region_saved(entry)
            self._report_refused_folds(entry)
        # Invalidated even for a palette-only write: in Offset mode the palette's
        # target *is* this entry's own file, so other entries on it are stale too.
        # Every file of a region, since a save can have rewritten any of them —
        # and the palette's own, because a **composite** has no paths to name and
        # its Offset palette is written into the file a piece came from
        # (:func:`~celpix.project.documents.offset_palette_files`). Nothing else
        # would then have dropped the stale documents on it.
        touched = list(entry.paths)
        if entry.doc.palette_config.write_enabled:
            touched += entry.doc.palette_config.source.paths
        held = {
            e: e.doc
            for e in self._workspace.entries
            if e.kind is EntryKind.PALETTE and e is not entry and e.doc is not None
        }
        for path in touched:
            self._workspace.invalidate_path(path, keep=entry)
        self._note_written(touched)
        self._refresh_stale_current()
        self._reload_mirrored_palettes(held)
        return True

    def _reload_mirrored_palettes(self, held: dict[Entry, Document]) -> None:
        """Re-read each palette file a write just dropped, if anything shows it.

        ``held`` maps each palette entry to the document it had before the
        write; one whose document is gone, or was replaced by the current
        entry's re-read, is decoded afresh and mirrored.

        A palette file's colours live only in its own document; every File-mode
        graphic rendering it holds them by reference and the dock edits them
        there. Dropped and left unloaded, the graphics would keep the stale
        colours with nothing behind them, and a dock edit would land on the
        graphic's write-disabled mirror — dirtying a palette that Write skips
        for want of a document. Nothing else reloads it: only the current entry
        is re-read after a write, and a graphic links its palette on first load.
        One nobody renders stays dropped and loads when it is next needed.
        """
        reread = [
            pal
            for pal, doc in held.items()
            if pal.doc is not None and pal.doc is not doc
        ]
        reread += [
            pal
            for pal in held
            if pal.doc is None
            and self._workspace.palette_render_targets(pal.path)
            and self._load_palette_entry(pal, quiet=True)
        ]
        for pal in reread:
            self._mirror_palette(pal)
        if reread:
            self._refresh_palette_dock()

    def _fold_slice_edits_into(
        self, parent: Entry, also: Entry | None = None
    ) -> list[Entry]:
        """Bring ``parent``'s buffer up to date with its slices' unsaved edits.

        A slice's bytes are a **derived** view of a window of its parent's
        region — the identity for a plain slice, a decode for a compressed or
        reshaped one, which is why the two cannot simply share one buffer. So
        they are separate copies with the parent's as the authority, and
        reconciliation runs one way: each dirty slice is re-encoded exactly as a
        save would lay it down (``pipeline.encoded_pixel_bytes``) and spliced in
        at the offset it was read from.

        ``parent`` is a file, a palette, or a **slice** whose own slices window
        into its decoded buffer. A slice parent's offsets count from byte 0 of
        that buffer; a file's are file-absolute, rebased across whatever its
        container skipped.

        Called wherever that buffer is about to be *believed* — shown, or
        written — so looking at a file shows what was edited through its slices,
        and writing it puts those edits on disk. Idempotent: the same bytes over
        the same range. ``also`` folds one more slice whether or not it is dirty,
        for an explicit Write on a clean one. Returns what was folded, so a write
        can mark exactly those saved.

        **Deepest debts first.** A child that is itself owed folds by *its*
        children is settled before it is encoded, recursively, so what it hands
        up already holds them — and a child with no document that owes such a
        fold is loaded for the purpose, since its buffer is the only place the
        edits below it can be carried up through. A slice whose own buffer this
        fold changed then owes its parent in turn, which is recorded here.

        A slice that cannot encode (no compressor, no unshape) is skipped rather
        than failing the fold: it has nothing to contribute and never had, and
        its own Write reports the problem in its own right.

        **What is folded** is every dirty child, plus the ones ``parent`` is
        recorded as owing (:attr:`~celpix.project.workspace.Entry.pending_folds`)
        — a child undone back to clean still owes its bytes, and dirtiness alone
        would leave the edited version standing in the buffer after the undo. The
        debt is discharged whatever came of each child: one that cannot encode
        never could, and keeping it would re-attempt the failure at every read.
        The one exception is a clean child that could not be opened: it keeps
        the debt, since nothing else would bring it back to be folded once it
        opens.

        A child whose fold is **refused** is recorded as such on the child
        (:attr:`~celpix.project.workspace.Entry.fold_refused`). Four things
        refuse one: a re-encoded stream that no longer fits its slot, a slice
        this buffer does not reach — anchored before the window the parent's
        container opened on the file, or running past the end of it — a parent
        that is a **map**, whose own bytes are its cells and are written from
        them, and a child owed folds of its own that has no document and will
        not open, whose buffer is the only way up for the edits nested in it.
        Each is recorded, because the buffer then lacks that slice's edits while
        two things downstream assume otherwise: the parent's write marks its
        dirty slices saved, and the parent's edit drops their documents. Both
        read the record and leave such a slice alone, dirty and holding whatever
        document it has, with everything nested under it, and the write says
        so. Not re-attempted at every read for the reason above; the next write
        of either side tries again.
        """
        if not self._can_fold_into(parent) or id(parent) in self._folding:
            return []
        self._folding.add(id(parent))
        try:
            folded = self._fold_children(parent, also)
        finally:
            self._folding.discard(id(parent))
        # A palette file's colours are a decode of the bytes just folded into,
        # and every graphic mirroring them is showing the old ones.
        if folded and parent.kind is EntryKind.PALETTE:
            self._redecode_palette_entry(parent)
        if folded and parent.kind is EntryKind.SLICE:
            # The slice's own buffer just moved, which is an edit to *its*
            # parent: the debt goes one link up, and on up the chain.
            self._record_fold_debts(parent)
        return folded

    @staticmethod
    def _can_fold_into(parent: Entry) -> bool:
        """Whether ``parent`` is a kind with slices, and has a buffer to hold them."""
        kinds = (EntryKind.FILE, EntryKind.PALETTE, EntryKind.SLICE)
        return parent.kind in kinds and parent.doc is not None

    def _fold_children(self, parent: Entry, also: Entry | None) -> list[Entry]:
        """The body of :meth:`_fold_slice_edits_into`, with ``parent`` marked as
        being folded into."""
        assert parent.doc is not None
        owed = parent.pending_folds
        # A slice's children count from byte 0 of its decoded buffer; a file's
        # from byte 0 of the file, which its buffer may start past.
        base = (
            0
            if parent.kind is EntryKind.SLICE
            else parent.doc.pixel_ctx.get(KEY_SOURCE_OFFSET, 0)
        )
        folded: list[Entry] = []
        still_owed: set[Entry] = set()
        for child in self._workspace.children_of(parent):
            if child.kind is not EntryKind.SLICE:
                continue
            if not (child.pixel_dirty or child is also or child in owed):
                continue
            if child.pending_folds:
                # Its own nested slices first, so their edits ride up inside it.
                if child.doc is None and not self._load_entry(child, quiet=True):
                    # Its buffer is the only way up for the edits below it, so
                    # without one they reach neither this buffer nor the disk,
                    # and a skip left silent here has this parent's write mark
                    # them saved and its edit drop them. Refused like the others
                    # below, and the debt kept while the child is clean: a dirty
                    # one is retried by every fold, a clean one only if owed.
                    child.fold_refused = (
                        "could not be opened, so the edits nested in it have "
                        "nowhere to go"
                    )
                    if not child.pixel_dirty:
                        still_owed.add(child)
                    continue
                try:
                    self._fold_slice_edits_into(child)
                except PipelineError:
                    pass  # recorded on the grandchild; this child still folds
            if child.doc is None:
                continue
            if parent.doc.is_tilemap:
                # A map's own bytes are its cells, and it is written from them
                # rather than from the buffer they were read out of, so bytes
                # spliced in here would not survive its next write.
                child.fold_refused = (
                    f"lies inside {parent.name}, a tilemap, whose bytes are "
                    "written from its cells, so there is nowhere in it to fold "
                    "these bytes into"
                )
                continue
            try:
                # A map carved from the file folds its *cells*: its pixel buffer is
                # the art it borrows through its binding, which belongs to another
                # entry and to another offset — splicing that here would write a
                # tile bank over the map's own bytes.
                encode = (
                    pipeline.encoded_tilemap_bytes
                    if child.doc.is_tilemap
                    else pipeline.encoded_pixel_bytes
                )
                shaped = encode(child.doc, self._registry)
            except PipelineError as exc:
                if child is also:
                    raise  # the one being written reports its own failure
                child.fold_refused = str(exc)
                continue
            start = child.slice_offset - base
            # A slice anchored outside the parent's window was never cut from
            # this buffer (`workspace._parent_view_bytes`), so it has no place
            # in it to fold back into. Recorded like an encoder's refusal and
            # for the same reason: the buffer does not hold these bytes, so a
            # skip left silent here has the parent's write mark the slice saved
            # with nothing of it written.
            if start < 0 or start + len(shaped) > len(parent.doc.pixel_data):
                child.fold_refused = (
                    f"lies outside {parent.name}'s region, so there is nowhere "
                    "in it to fold these bytes into"
                )
                continue
            parent.doc.replace_bytes(start, shaped)
            child.fold_refused = None
            folded.append(child)
        parent.pending_folds.clear()
        parent.pending_folds.update(still_owed)
        return folded

    def _record_fold_debts(self, entry: Entry) -> None:
        """Record that ``entry``'s bytes are owed up its whole chain.

        Its parent owes it, and every further ancestor owes the one below: a
        nested slice's edit reaches the file only once each link has folded the
        one under it, and settling any link must find the debts above it. Each
        ancestor's borrowed copies go stale with it, and its row says so.
        """
        below = entry
        for ancestor in self._workspace.ancestors_of(entry):
            ancestor.pending_folds.add(below)
            self._drop_bound_copies(ancestor)
            self._files_panel.refresh_entry(ancestor)
            below = ancestor

    def _propagate_pixel_edit(self, entry: Entry, keep: Entry | None = None) -> None:
        """Carry a pixel edit across the parent/slice boundary **as it lands**.

        The parent's buffer is the authority for its bytes, so an edit made
        through a slice is owed to it immediately rather than discovered at show
        or write time. That is what keeps the two from racing: the parent then
        holds every unsaved change to the region by the time anything reads it,
        so a later edit made *on* the parent composes on top of them instead of
        being reverted by a stale slice window folded in behind it.

        Down a chain of **nested** slices the debt is recorded at every link —
        the parent slice owes the edited one, its parent owes it, up to the file
        (:meth:`_record_fold_debts`) — so the file's settle finds it.

        Editing a parent goes the other way: every slice cache **under** it is
        dropped, to any depth and the **dirty ones included**. Dropping those is
        safe only because of the fold above — their edits are already in this
        buffer, so re-deriving from it loses nothing and picks up what was just
        edited besides. A slice with both a parent and slices of its own does
        both halves.

        The parent's unsaved-state token is **not** stamped here. The file does
        have unsaved changes and both rows do say so, but which token it takes is
        the command's answer rather than this one's: an undo has to hand the
        parent back the exact state it was in before, which only the push site
        saw (:meth:`~...tile_bytes.TileBytesMixin._apply_pixel_bytes`). Splitting
        the two is what let one edit acquire *several* owners — a composite's
        pieces, a nested slice's ancestors — without this method having to know
        how many (``docs/design/composite-entry.md``).

        ``keep`` is a slice whose document the caller is **holding** and so cannot
        have taken away: an Entry-mode palette read out of the consumer's own
        parent lands its colour in the file, and the consumer is one of the slices
        below it (:meth:`~...palette_entry.PaletteEntryMixin.
        _sync_entry_palette_bytes`). Dropped, the window goes on drawing a
        document the entry no longer has — with the palette the edit was just made
        on inside it — and every later edit lands in a fresh one nobody sees. It
        is the same exemption :meth:`~...tile_bytes.TileBytesMixin.
        _reassemble_composites` takes for the entry a stroke was made on, and it
        is safe for the same reason: those bytes are on screen already.
        """
        if entry.kind is EntryKind.SLICE:
            # The debt is recorded, not paid. Re-encoding the slice is what a
            # fold costs, and on a compressed one that is a search for the
            # tightest packing — the better part of a second on a Mega Drive tile
            # bank, which a stroke cannot spend. So the fold runs at the next
            # place the buffer is *believed* instead (:meth:`_settle_region`),
            # which is every place it was already running and no more.
            #
            # This slice is recorded whatever its dirty state, because an **undo**
            # back to the saved bytes leaves it clean and those clean bytes are
            # exactly what the parent has to be given back — a fold of only what
            # is dirty would strand the edit in the parent's buffer after it had
            # been undone in the slice's.
            self._record_fold_debts(entry)
        if entry.kind in (EntryKind.FILE, EntryKind.PALETTE, EntryKind.SLICE):
            self._drop_derived_slices(entry, keep)

    def _drop_derived_slices(self, entry: Entry, keep: Entry | None) -> None:
        """Drop the document of every slice under ``entry``, to any depth.

        A slice whose fold was refused keeps its document, and so does everything
        nested under it: the buffer does not hold its edits, so re-deriving it
        from the buffer would be the one way to lose them — and its own nested
        slices' edits live in *its* buffer, which is not the one that moved. It
        goes on showing its own bytes rather than this edit until it fits and is
        written. ``keep``'s subtree is left alone for the same reason: its
        document is held, so nothing under it has moved.
        """
        for child in self._workspace.children_of(entry):
            if child.kind is not EntryKind.SLICE or child is keep:
                continue
            if child.fold_refused is not None:
                continue
            if child.doc is not None:
                self._drop_bound_copies(child)
                self._workspace.drop_document(child)
            self._drop_derived_slices(child, keep)

    def _settle_region(self, entry: Entry | None) -> None:
        """Pay any fold ``entry``'s region owes, before its bytes are believed.

        The lazy half of "edits fold into the owner" (``slices-and-parents.md``
        §2). An edit through a slice records the debt on the parent that owns
        those bytes — and on every link above it, for a nested slice — rather
        than re-encoding on the spot, and this is where it is settled: at each
        point the buffer is about to be handed to something that will act on it,
        and nowhere else.

        Those points are few, and they are the ones the eager fold was implicitly
        covering — every read of a file's live bytes already went through one of
        them, which is what makes the move safe rather than hopeful:

        - **A slice reading its parent**, through the one factory that builds a
          pathway config (:meth:`~...interpretation.InterpretationMixin.
          _pixel_config` → ``pixel_config_for``).
        - **A map reading the bank it is bound to**
          (:meth:`~...session.SessionMixin._load_bound_tiles`), the one deliberate
          exception to ``entry_view_bytes`` being the single funnel.
        - **An Offset palette** resolving against its owner's buffer
          (:meth:`~...palette_offset.PaletteOffsetMixin._reordered_view`).
        - **Showing** the file, and **writing** anything in its region, both of
          which folded before and still do.

        Takes **any member of the chain** — the entry about to be read, one of
        its slices, a slice nested anywhere under it — and settles from that
        entry up to the file, **bottom-up**: its own debts, then its parent's,
        and so on. Each fold settles the children it folds first
        (:meth:`_fold_slice_edits_into`), so the file is settled last and holds
        everything. Costs a set test per link when there is nothing owed, which
        is the common case.

        A link already being folded into stops the walk: the fold in progress is
        the one settling everything above it, and folding it again half-way
        through would hand its parent a buffer that is not finished yet.
        """
        if entry is None:
            return
        for node in (entry, *self._workspace.ancestors_of(entry)):
            if id(node) in self._folding:
                return
            if not node.pending_folds:
                continue
            try:
                self._fold_slice_edits_into(node)
            except PipelineError:
                # An edit that cannot be encoded still belongs on screen; the
                # write path is where that failure is worth reporting.
                pass

    def _settle_own(self, entry: Entry) -> None:
        """Pay the folds owed to ``entry`` alone — not the chain above it.

        What an edit landing on ``entry`` needs, where a reader needs the whole
        chain (:meth:`_settle_region`): the edit is about to drop every slice
        document under ``entry``, so what they owe it must be in its buffer
        first. The links above are not touched, which is what keeps a stroke
        through a compressed slice from re-encoding it into its parent on every
        stroke after the first.
        """
        if not entry.pending_folds or id(entry) in self._folding:
            return
        try:
            self._fold_slice_edits_into(entry)
        except PipelineError:
            pass  # reported by the write path, as for any settle

    def _drop_bound_copies(self, owner: Entry) -> None:
        """Drop the borrowed tiles of every map bound to ``owner``.

        The one thing :meth:`~...session.SessionMixin._resync_tile_bindings` cannot
        reach: it patches each map's copy with the *same splices*, which is only
        right while both buffers were decoded through one pathway. Across the
        slice/parent boundary they were not — a slice reads a window of its
        parent, so the offsets differ — and the edit arrives at the other side of
        that boundary as a fold rather than as splices at all.

        So the copies are dropped rather than patched, and re-read from the entry
        they borrow on the way back in. Without it a map bound to a slice went on
        drawing the art as it stood before the parent was edited, indefinitely:
        re-activating it does not reload a document it still has.

        The entry on screen is left alone — its document is the window's live one,
        and the user is editing the other side of this boundary, so it cannot be
        the one being dropped.
        """
        for bound in self._entries_bound_to(owner):
            if bound is self._workspace.current:
                continue
            if bound.pixel_dirty and bound.doc is not None and bound.doc.is_tilemap:
                # A map's unsaved cells live in its document and nowhere else, so
                # dropping it would throw the edit away for the sake of its art.
                # Re-read instead, which carries the cells across
                # (:meth:`~...tilemap_bar.TilemapBarMixin._reread_tilemap`).
                self._reread_tilemap(bound, quiet=True)
                continue
            self._workspace.drop_document(bound)

    def _carried_to(self, entry: Entry, root: Entry) -> bool:
        """Whether ``entry``'s last fold, and every one above it below ``root``,
        landed — so its edits are in ``root``'s buffer."""
        for node in (entry, *self._workspace.ancestors_of(entry)):
            if node is root:
                return True
            if node.fold_refused is not None:
                return False
        return False

    def _mark_region_saved(self, root: Entry) -> None:
        """Mark clean everything whose unsaved bytes just went to disk with
        ``root``.

        A write of a file writes its whole region, and every dirty slice under
        it — to any depth — has its edits inside that region already, folded
        link by link on the way up (:meth:`_fold_slice_edits_into`), so they are
        on disk too. Leaving them marked dirty would claim otherwise, and a later
        write of one would put its own window back over whatever has happened
        since.

        Every dirty slice **except one whose fold was refused**, and every slice
        nested under one: their edits are in no buffer the file holds and on no
        disk, so they stay dirty, and the caller reports them
        (:meth:`_report_refused_folds`). Marking them saved was how an edit that
        had grown past its slot vanished without a word.
        """
        self._workspace.mark_saved(root, palette=False)
        for child in self._workspace.descendants_of(root):
            if (
                child.kind is EntryKind.SLICE
                and child.pixel_dirty
                and self._carried_to(child, root)
            ):
                self._workspace.mark_saved(child, palette=False)

    def _report_refused_folds(self, root: Entry) -> None:
        """Tell the user which of ``root``'s slices a write just left behind.

        After a region write: a slice whose fold was refused — at its own link or
        at one above it — is still dirty and still loaded, so nothing is lost;
        but the write it was part of has reported success, and a user who takes
        that as "everything is on disk" would close the project over an edit
        that is not. One modal, naming each slice and the reason, which is the
        reason the encoder gave at the link that refused.
        """
        left = [
            child
            for child in self._workspace.descendants_of(root)
            if child.kind is EntryKind.SLICE
            and child.pixel_dirty
            and not self._carried_to(child, root)
        ]
        if not left:
            return
        lines = "\n".join(
            f"• {child.name}: {self._refusal_of(child, root)}" for child in left
        )
        self._alert(
            f"{root.name} was written, but the unsaved changes in "
            f"{counted(len(left), 'slice')} could not go with it and are still "
            f"unsaved:\n\n{lines}\n\nMake them fit and write again.",
            title="celPix - write",
        )

    def _refusal_of(self, entry: Entry, root: Entry) -> str:
        """Why ``entry``'s edits did not reach ``root``: its own refusal, or the
        one at the nearest link above it that its edits are waiting in."""
        if entry.fold_refused is not None:
            return entry.fold_refused
        for ancestor in self._workspace.ancestors_of(entry):
            if ancestor is root:
                break
            if ancestor.fold_refused is not None:
                return f"held in {ancestor.name}, which {ancestor.fold_refused}"
        return "it could not be folded"

    def _write_pixels_through_parent(self, entry: Entry) -> bool:
        """Persist a slice by folding it into its parent and writing that.

        A slice is a region *of* a file, so it is saved as part of that file
        rather than deposited at its own bounds: its edits are folded into the
        parent's buffer (:meth:`_fold_slice_edits_into`) and the parent's write
        carries the whole region out — through ``unshape`` and the container,
        split back across the region's chips. Where the parent reorders that is
        the only thing that *can* work; everywhere else it is what lets the
        parent's container run its own write half over bytes that changed inside
        it (a checksum repair, a re-wrapped header), which depositing around it
        skips.

        A **nested** slice goes up its chain a link at a time: folded into its
        parent slice, that slice into its own parent, and so on to the file,
        whose write is the one deposit. Every link is loaded for the purpose
        first, file first, so each is read against a parent that already holds
        what is above it.

        Its **sibling** slices' unsaved edits go too, and so do the parents'
        own: one write of one region cannot honour some of what that region
        currently holds and not the rest. Everything folded comes back clean.

        False (already reported) when there is no chain to write through or a
        link refused the fold; a pipeline failure raises for the caller to
        report.
        """
        assert entry.doc is not None
        chain = self._workspace.ancestors_of(entry)
        root = chain[-1] if chain else None
        if root is None or root.kind not in (EntryKind.FILE, EntryKind.PALETTE):
            what = (
                "the slice it was cut from, which is no longer open"
                if self._workspace.chain_broken(entry)
                else f"{Path(entry.path).name}, which is no longer open"
            )
            self._alert(
                f"{entry.name} is a region of {what}, so there is nothing to "
                "write it through.",
                title="celPix - write",
            )
            return False
        # Loudly, not quietly: if a link won't open, *why* is what the user
        # needs, and this method has nothing to add to it.
        for link in reversed(chain):
            if link.doc is None and not self._load_entry(link):
                return False
        below = entry
        for link in chain:
            folded = self._fold_slice_edits_into(link, also=below)
            if below not in folded:
                self._alert(
                    f"{below.name} could not be written into {link.name}: "
                    f"it {below.fold_refused or 'could not be folded'}.",
                    title="celPix - write",
                )
                return False
            below = link
        assert root.doc is not None
        # The file's pixel pathway alone: its palette is a separate source in a
        # separate file, and this write says nothing about it.
        pipeline.save(root.doc, self._registry, palette=False)
        self._note_written(root.paths)
        self._mark_region_saved(root)
        self._report_refused_folds(root)
        return True

    def _refresh_stale_current(self) -> None:
        """Re-read the active entry if a save into its file dropped its cache,
        preserving the on-screen view position and palette."""
        entry = self._workspace.current
        if entry is None or entry.doc is not None or unavailable(entry):
            # Inert on purpose - a missing file, a load that failed and has not
            # been given a reason to try again - so not something to re-read
            # here, where it would raise the very dialog the mark replaces.
            return
        stale = self._doc  # the document still on screen
        if stale is None:
            # Nothing on screen to preserve: the entry was inert and its mark
            # has just been lifted (the file changed on disk), so this is its
            # first showing rather than a refresh, and the full route rebuilds
            # the document UI the inert state greyed.
            self._on_current_entry_changed(entry)
            return
        if not self._load_entry(entry):
            return  # reported; the stale view stays until the next activation
        if stale is not None:
            entry.doc.view = stale.view
            entry.doc.palette = stale.palette
            entry.doc.palette_config = stale.palette_config
            entry.doc.palette_ctx = stale.palette_ctx
        self._doc = entry.doc
        self._refresh_view()
