"""Putting edits back on disk, and the one rule that makes a region safe to.

Every path that writes bytes out — File ▸ Write, Write All, the files dock's own
Write, and the unsaved-changes gate the project and quit paths go through first.

**One region, one authority** (``docs/design/slices-and-parents.md``). A slice's
bytes are a *derived* view of a window of its parent's, so the parent owns them.
Four methods carry that between them and none of them is where the rule lives:
:meth:`~WritingMixin._propagate_pixel_edit` folds a slice edit into the parent's
buffer as it lands, :meth:`~WritingMixin._fold_slice_edits_into` is the fold
itself, :meth:`~WritingMixin._write_pixels_through_parent` routes a save out
through the parent so its container runs, and
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

from PySide6.QtWidgets import QMessageBox

from celpix.core.context import KEY_SOURCE_OFFSET
from celpix.core.errors import PipelineError
from celpix.pipeline import pipeline
from celpix.project.workspace import Entry, EntryKind


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
        box = QMessageBox(self)
        box.setWindowTitle("celPix - unsaved changes")
        box.setText(f"{consequence} ({names}). Write them to disk first?")
        write = box.addButton(write_label, QMessageBox.ButtonRole.AcceptRole)
        box.addButton(skip_label, QMessageBox.ButtonRole.DestructiveRole)
        cancel = box.addButton(QMessageBox.StandardButton.Cancel)
        if default_write:
            box.setDefaultButton(write)
        box.exec()
        if box.clickedButton() is cancel:
            return False
        if box.clickedButton() is write:
            self._write_all()
            # A write that failed left its entry dirty - don't proceed past it.
            return not self._workspace.dirty_entries()
        return True

    def _write_current(self) -> None:
        """File ▸ Write: the current file or slice, and the palette file it shows.

        A File-mode palette is owned by its own PALETTE entry, so a color edit
        dirties *that* and never the graphic rendering it
        (``docs/design/palette-editing.md`` §2). Ctrl+W means "save what I am
        looking at", and the palette on screen is part of that - so an unsaved
        one is written with the graphic rather than needing a second gesture in
        the Files list. Only when it actually has changes: writing a clean
        ``.pal`` would bump the mtime of a file other graphics may share, for
        nothing.

        The two are separate files and are written independently, so a failure on
        one (already reported) doesn't take the other with it; the status line
        names whatever really landed.
        """
        entry = self._workspace.current
        if entry is None or entry.doc is None:
            return
        # Read before the write, which clears the flags it acts on.
        palette_only = entry.palette_dirty and not entry.pixel_dirty
        has_palette_file = entry.doc.palette_config.write_enabled
        palette = self._linked_palette_entry()
        if palette is None or palette.doc is None or not palette.palette_dirty:
            palette = None
        wrote_entry = self._write_entry(entry)
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
        landed = [f"{entry.name} ({wrote})"] if wrote_entry else []
        if wrote_palette:
            landed.append(palette.name)
        if landed:
            self.statusBar().showMessage(f"Wrote {' and '.join(landed)}.")

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
        # A PALETTE entry writes its own .pal (its pixel half is inert); every
        # other entry writes its graphic, which is view-only when any stage it
        # reads through has no save-side half to put the bytes back with.
        writable = (
            entry.doc.palette_config.write_enabled
            if entry.kind is EntryKind.PALETTE
            else entry.doc.pixel_config.write_enabled
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

        A slice inside a region its parent reorders takes the long way round
        (:meth:`_write_pixels_through_parent`) — its bytes have no file position
        of their own.
        """
        assert entry.doc is not None
        if entry is self._workspace.current:
            self._commit_float()
        self._capture_session()  # keep the current entry's session snapshot fresh
        palette_only = entry.palette_dirty and not entry.pixel_dirty
        via_parent = not palette_only and entry.doc.pixel_config.writes_through_parent
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
        if writes_region and entry.kind is EntryKind.FILE:
            self._mark_region_saved(entry)
        # Invalidated even for a palette-only write: in Offset mode the palette's
        # target *is* this entry's own file, so other entries on it are stale too.
        # Every file of a region, since a save can have rewritten any of them.
        for path in entry.paths:
            self._workspace.invalidate_path(path, keep=entry)
        self._refresh_stale_current()
        return True

    def _fold_slice_edits_into(
        self, parent: Entry, also: Entry | None = None
    ) -> list[Entry]:
        """Bring ``parent``'s buffer up to date with its slices' unsaved edits.

        A slice's bytes are a **derived** view of a window of its parent's
        region — the identity for a plain slice, a decode for a compressed or
        reshaped one, which is why the two cannot simply share one buffer. So
        they are separate copies with the parent's as the file's authority, and
        reconciliation runs one way: each dirty slice is re-encoded exactly as a
        save would lay it down (``pipeline.encoded_pixel_bytes``) and spliced in
        at the offset it was read from.

        Called wherever that buffer is about to be *believed* — shown, or
        written — so looking at a file shows what was edited through its slices,
        and writing it puts those edits on disk. Idempotent: the same bytes over
        the same range. ``also`` folds one more slice whether or not it is dirty,
        for an explicit Write on a clean one. Returns what was folded, so a write
        can mark exactly those saved.

        A slice that cannot encode (no compressor, no unshape) is skipped rather
        than failing the fold: it has nothing to contribute and never had, and
        its own Write reports the problem in its own right.
        """
        if parent.kind is not EntryKind.FILE or parent.doc is None:
            return []
        base = parent.doc.pixel_ctx.get(KEY_SOURCE_OFFSET, 0)
        folded: list[Entry] = []
        for child in self._workspace.children_of(parent):
            if child.kind is not EntryKind.SLICE or child.doc is None:
                continue
            if not (child.pixel_dirty or child is also):
                continue
            try:
                shaped = pipeline.encoded_pixel_bytes(child.doc, self._registry)
            except PipelineError:
                if child is also:
                    raise  # the one being written reports its own failure
                continue
            start = child.slice_offset - base
            # A slice anchored outside the parent's window was never cut from
            # this buffer (`workspace._parent_view_bytes`), so it has no place
            # in it to fold back into.
            if start < 0 or start + len(shaped) > len(parent.doc.pixel_data):
                continue
            parent.doc.replace_bytes(start, shaped)
            folded.append(child)
        return folded

    def _propagate_pixel_edit(self, entry: Entry, owner_revision: int) -> None:
        """Carry a pixel edit across the file/slice boundary **as it lands**.

        The file's buffer is the authority for its bytes, so an edit made
        through a slice is folded into it immediately rather than at show or
        write time. That is what keeps the two from racing: the parent then
        holds every unsaved change to the region, so a later edit made *on* the
        parent composes on top of them instead of being reverted by a stale
        slice window folded in behind it. Its revision is stamped alongside -
        the file has unsaved changes, and both rows say so; writing either
        clears both, because it is one set of bytes.

        Editing the parent goes the other way: every slice cache is dropped, the
        **dirty ones included**. Dropping those is safe only because of the fold
        above - their edits are already in this buffer, so re-deriving from it
        loses nothing and picks up what was just edited besides.
        """
        if entry.kind is EntryKind.SLICE:
            parent = self._workspace.find_file(entry.path)
            if parent is None:
                return
            # Only a loaded parent needs folding; an unloaded one folds on the
            # way in (:meth:`_on_current_entry_changed`), which is necessarily
            # before anyone can look at or write it.
            #
            # ``also`` names this slice whatever its dirty state, because an
            # **undo** back to the saved bytes leaves it clean and those clean
            # bytes are exactly what the parent has to be given back - folding
            # only what is dirty would strand the edit in the parent's buffer
            # after it had been undone in the slice's.
            if parent.doc is not None:
                try:
                    self._fold_slice_edits_into(parent, also=entry)
                except PipelineError:
                    # An edit that cannot be encoded still belongs on screen;
                    # the write path is where that failure is worth reporting.
                    pass
            self._workspace.set_pixel_revision(parent, owner_revision)
            self._files_panel.refresh_entry(parent)
        elif entry.kind is EntryKind.FILE:
            for child in self._workspace.children_of(entry):
                if child.kind is EntryKind.SLICE and child.doc is not None:
                    self._workspace.drop_document(child)

    def _mark_region_saved(self, parent: Entry) -> None:
        """Mark clean everything whose unsaved bytes just went to disk with
        ``parent``.

        A write of a file writes its whole region, and every dirty slice of it
        has its edits inside that region already - folded as they landed
        (:meth:`_propagate_pixel_edit`) - so they are on disk too. Leaving them
        marked dirty would claim otherwise, and a later write of one would put
        its own window back over whatever has happened since.
        """
        self._workspace.mark_saved(parent, palette=False)
        for child in self._workspace.children_of(parent):
            if child.kind is EntryKind.SLICE and child.pixel_dirty:
                self._workspace.mark_saved(child, palette=False)

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

        Its **sibling** slices' unsaved edits go too, and so do the parent's own:
        one write of one region cannot honour some of what that region currently
        holds and not the rest. Everything folded comes back clean.

        False (already reported) when there is no parent to write through; a
        pipeline failure raises for the caller to report.
        """
        assert entry.doc is not None
        parent = self._workspace.find_file(entry.path)
        if parent is None:
            self._alert(
                f"{entry.name} is a region of {Path(entry.path).name}, so it is "
                "written as part of that file - which is no longer open.",
                title="celPix - write",
            )
            return False
        # Loudly, not quietly: if the parent won't open, *why* is what the user
        # needs, and this method has nothing to add to it.
        if parent.doc is None and not self._load_entry(parent):
            return False
        folded = self._fold_slice_edits_into(parent, also=entry)
        if entry not in folded:
            self._alert(
                f"{entry.name} lies outside {parent.name}'s region, so there is "
                "nowhere in it to write these bytes back to.",
                title="celPix - write",
            )
            return False
        # The parent's pixel pathway alone: its palette is a separate source in a
        # separate file, and this write says nothing about it.
        pipeline.save(parent.doc, self._registry, palette=False)
        self._mark_region_saved(parent)
        return True

    def _refresh_stale_current(self) -> None:
        """Re-read the active entry if a save into its file dropped its cache,
        preserving the on-screen view position and palette."""
        entry = self._workspace.current
        if entry is None or entry.doc is not None:
            return
        stale = self._doc  # the document still on screen
        if not self._load_entry(entry):
            return  # reported; the stale view stays until the next activation
        if stale is not None:
            entry.doc.view = stale.view
            entry.doc.palette = stale.palette
            entry.doc.palette_config = stale.palette_config
            entry.doc.palette_ctx = stale.palette_ctx
        self._doc = entry.doc
        self._refresh_view()
