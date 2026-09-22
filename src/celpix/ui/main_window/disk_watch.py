"""Noticing that another program rewrote an open file, and reloading it.

An emulator flushing its save RAM, an assembler rebuilding the ROM, a hex
editor: any of them can rewrite a file while an entry read from it is on screen.
Left alone, the window keeps showing bytes the file no longer holds, and the
next Write puts them back over the other program's work. So every file the
workspace reads is watched, a change is offered as a reload, and a reload
carries the unsaved edits made here across onto the new contents
(``docs/design/disk-changes.md``).

**Subscribed, not polled.** A ``QFileSystemWatcher`` holds every open file, and
a signal from it means only that the file was *touched*: what decides anything
is the bytes, hashed once the signals rest and told against what was last read
or written here, so a touch that changed nothing asks nothing. What the watcher
cannot follow — a write made from the Windows side of a WSL mount, or from
another machine on a network share — is caught by one look at the files each
time celPix becomes the active application, which is also when the question
lands best; and by File ▸ Reload From Disk, asked for outright.

The merge is byte-wise and lives in :mod:`celpix.project.diskchanges`; what this
module owns is *which* buffers to merge. The rule is the one every edit already
follows — one region, one authority (``docs/design/slices-and-parents.md``): a
file's unsaved edits, its slices' included, are folded into the file's own
buffer, that buffer is merged once, and every derived view is re-derived from
it. A palette carries its edited entries the way its save splices them.
"""

from __future__ import annotations

import os
from os.path import basename

from PySide6.QtCore import QEvent, QFileSystemWatcher, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from celpix.core.document import Document
from celpix.core.errors import PipelineError
from celpix.core.palette import Palette
from celpix.pipeline import pipeline
from celpix.pipeline.pathway import PathwayConfig
from celpix.project.diskchanges import DiskState, Merge, carry_color_edits, merge_bytes
from celpix.project.workspace import Entry, EntryKind, palette_source_for
from celpix.ui.widgets import counted

#: How long after the watcher's last signal a file is looked at: a program
#: writes a ROM in several passes, and each is a signal.
CHANGE_REST_MS = 300


def _same_palette_bytes(a: PathwayConfig, b: PathwayConfig) -> bool:
    """Do both palette configs decode the same window of the same file the same
    way? Asked of the buffer *address* rather than of the whole config, since a
    reload rebuilds the config around a fresh in-memory buffer."""
    return (
        a.source.paths,
        a.source.offset,
        a.source.length,
        a.interpret_preset_id,
        a.container_id,
    ) == (
        b.source.paths,
        b.source.offset,
        b.source.length,
        b.interpret_preset_id,
        b.container_id,
    )


class _Reload:
    """One reload's running tally — what was kept, and what could not be read."""

    def __init__(self) -> None:
        self.kept = 0
        self.conflicts = 0
        self.dropped = 0
        self.failed: list[str] = []
        self.reloaded: list[Entry] = []

    def add(self, merge: Merge | None) -> None:
        if merge is None:
            return
        self.kept += merge.kept
        self.conflicts += merge.conflicts
        self.dropped += merge.dropped


class DiskWatchMixin:
    """Watching open files for outside changes, and the reload that follows.

    A slice of :class:`~celpix.ui.main_window.window.MainWindow`, not a
    standalone object: it reads the workspace and re-reads documents through the
    window's own load path. See the module docstring for what it owns, and the
    package docstring for why these are mixins.
    """

    def _init_disk_watch(self) -> None:
        self._disk_state = DiskState()
        # Files seen changed but not yet offered: the offer waits for the window
        # to be the active one, so the question lands when the user is looking.
        self._pending_disk_changes: list[str] = []
        # Set while the offer's own dialog is up. Showing it re-activates the
        # window, which is itself a reason to check, and a check must not stack a
        # second dialog on the first.
        self._offering_disk_reload = False
        self._fs_watcher = QFileSystemWatcher(self)
        self._fs_watcher.fileChanged.connect(self._on_file_touched)
        # A program writes a ROM in several passes and each is a signal; the
        # look waits until they rest, and one look answers for all of them.
        self._disk_rest = QTimer(self)
        self._disk_rest.setSingleShot(True)
        self._disk_rest.setInterval(CHANGE_REST_MS)
        self._disk_rest.timeout.connect(self._check_disk_changes)
        ws = self._workspace
        ws.on_added.append(lambda _entry: self._sync_disk_watch())
        ws.on_removed.append(lambda _entry: self._sync_disk_watch())
        ws.on_reset.append(self._sync_disk_watch)

    # -- watching -----------------------------------------------------------
    def _sync_disk_watch(self) -> None:
        """Make the watched set the workspace's: every file any entry reads.

        A composite has no file and a bookmark's is its parent's, so this is the
        union of :attr:`~celpix.project.workspace.Entry.paths` — and a file that
        several entries share is watched once, as it was when the first arrived.
        The watcher follows the same set (:meth:`_rewatch`).
        """
        paths = [p for e in self._workspace.entries for p in e.paths if p]
        self._disk_state.retain(paths)
        self._disk_state.track(paths)
        self._rewatch()

    def _rewatch(self) -> None:
        """Subscribe the watcher to exactly the tracked files that exist.

        Run at every change of the set and at every look, because the watcher
        lets go of a path on its own: a program that saves by writing a new
        file and renaming it over the old one *replaces* the watched file, and
        the subscription goes with the one that was replaced. Taking the path
        up again here is what keeps the second such save noticed.
        """
        wanted = self._disk_state.paths()
        held = set(self._fs_watcher.files())
        gone = [p for p in held if p not in wanted]
        if gone:
            self._fs_watcher.removePaths(gone)
        new = [p for p in wanted if p not in held and os.path.exists(p)]
        if new:
            self._fs_watcher.addPaths(new)

    def _note_written(self, paths: list[str] | tuple[str, ...]) -> None:
        """Record that *this* program just wrote ``paths``, so the write is not
        offered back as someone else's change. Every write site calls it."""
        self._disk_state.refresh(paths)

    def _on_file_touched(self, _path: str) -> None:
        """The watcher's signal: the file was touched. Looked at once the
        signals rest, since what decides anything is the bytes."""
        self._disk_rest.start()

    def _check_disk_changes(self) -> None:
        """The look: note which files hold other bytes, and offer them if the
        moment is right. Every tracked file is asked, not only the ones
        signalled — the check is a stat each, and the activation path below
        has no signal to go by. A touch that left the bytes as they were is not
        a change (:meth:`~celpix.project.diskchanges.DiskState.changed`)."""
        self._rewatch()
        for path in self._disk_state.changed():
            key = self._workspace.path_key(path)
            if not any(
                self._workspace.path_key(p) == key for p in self._pending_disk_changes
            ):
                self._pending_disk_changes.append(path)
        self._offer_disk_reload()

    def changeEvent(self, event) -> None:  # noqa: ANN001 - Qt override
        """Coming back to the window is the moment to ask about what changed
        while the user was in the other program: what the watcher signalled
        and held back, and — one stat per file, once, not a poll — a change on
        a filesystem the watcher cannot follow."""
        super().changeEvent(event)
        if event.type() == QEvent.Type.ActivationChange and self.isActiveWindow():
            # Deferred out of the activation itself: a modal shown from inside
            # the event that made the window active is shown to a window Qt has
            # not finished activating.
            QTimer.singleShot(0, self._check_disk_changes)

    def _offer_disk_reload(self) -> None:
        """Put the pending changes to the user, if nothing else has the screen.

        Not while another dialog is up — the file picker, a write's own report —
        and not while celPix is in the background: a question about a file the
        user is at that moment rewriting in the other program is answered best
        once they come back to this one, which :meth:`changeEvent` catches. Any
        window of ours counts as being here, a floating panel included.
        """
        if (
            not self._pending_disk_changes
            or self._offering_disk_reload
            or QApplication.activeWindow() is None
            or QApplication.activeModalWidget() is not None
        ):
            return
        paths, self._pending_disk_changes = self._pending_disk_changes, []
        self._offering_disk_reload = True
        try:
            unsaved = [e.name for e in self._unsaved_on(paths)]
            if self._ask_disk_reload(paths, unsaved):
                self._reload_from_disk(paths)
            else:
                # Declined: not asked again while the files hold these bytes.
                # The next change asks afresh, and File ▸ Reload From Disk
                # still has the declined one to do.
                self._disk_state.decline(paths)
        finally:
            self._offering_disk_reload = False

    def _ask_disk_reload(self, paths: list[str], unsaved: list[str]) -> bool:
        """The prompt; True to reload. Its own dialog rather than ``_confirm``
        because "Cancel" is the wrong name for keeping the in-memory bytes."""
        names = [basename(p) for p in paths]
        if len(names) == 1:
            text = f"{names[0]} was changed on disk by another program."
        else:
            listed = "\n".join(f"• {name}" for name in names)
            text = (
                f"{len(names)} files were changed on disk by another program:\n{listed}"
            )
        text += "\n\nReload from disk?"
        if unsaved:
            text += (
                f"\n\nThe unsaved changes made here ({', '.join(unsaved)}) are kept: "
                "they are put back over the new contents, byte for byte."
            )
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("celPix - changed on disk")
        box.setText(text)
        reload = box.addButton("Reload", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Keep In Memory", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(reload)
        box.exec()
        return box.clickedButton() is reload

    def _on_paths(self, paths: list[str]):  # noqa: ANN202
        """A predicate: does an entry read any of ``paths``?"""
        keys = {self._workspace.path_key(p) for p in paths}
        key = self._workspace.path_key

        def test(entry: Entry) -> bool:
            return any(key(p) in keys for p in entry.paths)

        return test

    def _unsaved_on(self, paths: list[str]) -> list[Entry]:
        """The entries with unsaved work whose bytes come from ``paths``."""
        on = self._on_paths(paths)
        return [e for e in self._workspace.dirty_entries() if on(e)]

    # -- reloading ----------------------------------------------------------
    def _reload_current_from_disk(self) -> None:
        """File ▸ Reload From Disk: the entry on screen, from its file as it is.

        The same reload the watcher offers, asked for outright — for a change
        the watcher never saw (a filesystem it cannot follow, a rewrite inside
        one tick of a coarse clock), or one the user declined and now wants. A
        slice reloads through its file, as everything about a slice goes. A
        file that holds what was read costs a look and nothing else: the
        re-read would change nothing, and re-deriving every view for it is
        not free.
        """
        entry = self._workspace.current
        if entry is None or not entry.paths:
            return
        paths = list(entry.paths)
        if self._disk_state.is_current(paths):
            self.statusBar().showMessage(f"{entry.name} is up to date with disk.")
            return
        keys = {self._workspace.path_key(p) for p in paths}
        # Whatever the poll was holding about these files is answered by this.
        self._pending_disk_changes = [
            p
            for p in self._pending_disk_changes
            if self._workspace.path_key(p) not in keys
        ]
        self._reload_from_disk(paths)

    def _reload_from_disk(self, paths: list[str]) -> None:
        """Re-read every entry on ``paths`` from the files as they are now.

        In the order the ownership rule dictates: each **file** entry first, its
        buffer merged once with its slices' edits already folded in
        (:meth:`_reload_region`), then its **palette** file, then the current
        entry put back on screen. A float still hovering over the view is set
        down first, since it is an edit and has to be in the buffer to be kept.
        """
        ws = self._workspace
        if ws.current is not None:
            self._commit_float()
        self._capture_session()
        on = self._on_paths(paths)
        tally = _Reload()
        for entry in list(ws.entries):
            if entry.kind is EntryKind.FILE and on(entry):
                self._reload_region(entry, tally)
        # A slice whose file is not itself open reads the file for itself, so
        # it is its own region here (``slices-and-parents.md`` §2).
        for entry in list(ws.entries):
            if (
                entry.kind is EntryKind.SLICE
                and on(entry)
                and entry.doc is not None
                and ws.find_file(entry.path) is None
            ):
                tally.add(self._reload_document(entry, tally))
        for entry in list(ws.entries):
            if entry.kind is EntryKind.PALETTE and on(entry):
                self._reload_palette_entry(entry, tally)
        self._disk_state.refresh(paths)
        for entry in tally.reloaded:
            self._files_panel.refresh_entry(entry)
        self._put_current_back(tally.reloaded)
        self._report_reload(paths, tally)

    def _reload_region(self, file: Entry, tally: _Reload) -> None:
        """Reload ``file`` and everything derived from its bytes.

        Its buffer is the authority for the region, so the unsaved edits its
        slices hold are settled into it first and it is merged once. The slices
        are then dropped and re-derived from the merged buffer, exactly as an
        edit to the file drops them (``_propagate_pixel_edit``) — except one
        whose fold was refused, which holds the only copy of its edit, and one
        with unsaved *colours*, which are not in the buffer and are carried
        across a re-read instead. A file never loaded has nothing to merge; its
        loaded slices read the file for themselves and are reloaded as their own
        regions.
        """
        ws = self._workspace
        children = [
            c
            for c in ws.children_of(file)
            if c.kind is EntryKind.SLICE and c.doc is not None
        ]
        if file.doc is None:
            for child in children:
                tally.add(self._reload_document(child, tally))
            self._bytes_moved([file, *children], tally)
            return
        self._settle_region(file)
        merge = self._reload_document(file, tally)
        if merge is None:
            return  # unreadable now: it keeps its bytes, and so do its slices
        tally.add(merge)
        for child in children:
            if child.fold_refused is not None:
                continue
            if child.palette_dirty:
                self._reread_from_disk(child, tally)
            else:
                ws.drop_document(child)
        self._bytes_moved([file, *children], tally)

    def _bytes_moved(self, owners: list[Entry], tally: _Reload) -> None:
        """Everything that holds a *decode* of ``owners``' bytes reads them again.

        The tail of the landing sequence an edit takes
        (``tile_bytes._land_byte_edit``), minus the splice-patching a reload has
        no splices for: maps bound to any owner re-read their art (a dropped
        slice's is read back through the merged file), composites over them are
        reassembled and the maps over those re-read, and palettes decoded out of
        any of them are decoded again.
        """
        self._reresolve_bound_art(self._maps_drawing_from(owners))
        rebuilt = self._reassemble_composites(owners)
        self._reread_input_dependents(owners)
        self._reresolve_bound_art(self._maps_drawing_from(rebuilt))
        self._redecode_entry_palettes([*owners, *rebuilt])
        tally.reloaded.extend(rebuilt)

    def _reload_document(self, entry: Entry, tally: _Reload) -> Merge | None:
        """Re-read ``entry`` from disk and put its unsaved edits back over the
        result; the merge, or None when the entry could not be read (reported
        through ``tally``) and keeps the document it had.

        A pixel entry's edits are the bytes that differ from the buffer as read
        (:attr:`~celpix.core.document.Document.pixel_base_bytes`). A tilemap's
        are its cell bytes, and merged bytes are decoded into cells by handing
        them to the read as ``live`` — the same route an unsaved map takes
        through any other re-read.
        """
        previous = entry.doc
        assert previous is not None
        dirty = entry.pixel_dirty
        if not self._reread_from_disk(entry, tally):
            return None
        fresh = entry.doc
        assert fresh is not None
        if not dirty:
            return Merge(b"", 0, 0, 0)
        if fresh.is_tilemap:
            merge = merge_bytes(
                previous.tilemap_base_bytes, previous.tilemap_data, fresh.tilemap_data
            )
            if merge.data != fresh.tilemap_data and not self._reread_from_disk(
                entry, tally, live=merge.data
            ):
                return None
            return merge
        merge = merge_bytes(
            previous.pixel_base_bytes, previous.pixel_data, fresh.pixel_data
        )
        fresh.pixel_data = merge.data  # the baseline stays the file's own bytes
        return merge

    def _reread_from_disk(
        self, entry: Entry, tally: _Reload, *, live: bytes | None = None
    ) -> bool:
        """Drop ``entry``'s document and load it again; False if that failed.

        :meth:`~...tilemap_bar.TilemapBarMixin._reread_tilemap`'s shape, for
        any kind of entry: the view and the palette source go across through the
        entry's pending fields, and a failed read puts the old document back so
        the entry is either re-read or exactly as it was. Unsaved **colours**
        are carried by hand, because a re-read of an Offset or Entry palette
        decodes the new bytes and would leave the edits behind.
        """
        previous = entry.doc
        pending_palette = entry.pending_palette
        if pending_palette is None:
            entry.pending_palette = palette_source_for(entry)
        pending_view = entry.pending_view
        if pending_view is None and previous is not None:
            entry.pending_view = previous.view
        entry.doc = None
        if not self._load_entry(entry, quiet=True, live=live):
            entry.doc = previous
            entry.pending_palette = pending_palette
            entry.pending_view = pending_view
            tally.failed.append(entry.name)
            return False
        fresh = entry.doc
        assert fresh is not None
        if previous is not None and entry.palette_dirty:
            self._carry_palette(previous, fresh)
        tally.reloaded.append(entry)
        return True

    @staticmethod
    def _carry_palette(previous: Document, fresh: Document) -> None:
        """Put ``previous``'s unsaved colour edits onto ``fresh``'s palette.

        Entry by entry where the two decode the same bytes — the file's new
        colours with the edited ones over them, which is what a save would
        splice. Where they do not (the source moved under the reload), the
        edited palette is kept whole: an edit is never the thing to lose.
        """
        if _same_palette_bytes(previous.palette_config, fresh.palette_config):
            fresh.palette = Palette(
                carry_color_edits(
                    fresh.palette.colors,
                    previous.palette.colors,
                    previous.palette_edits,
                )
            )
        else:
            fresh.palette = previous.palette
            fresh.palette_ctx = previous.palette_ctx
            fresh.palette_config = previous.palette_config
            fresh.palette_base_bytes = previous.palette_base_bytes
        fresh.palette_edits = set(previous.palette_edits)

    def _reload_palette_entry(self, entry: Entry, tally: _Reload) -> None:
        """Re-read a registered palette file and re-mirror it onto every graphic
        showing it. Unsaved colour edits go back over the new colours."""
        previous = entry.doc
        if previous is None:
            return  # never loaded: the next use reads the file as it is now
        cfg = previous.palette_config
        try:
            loaded = pipeline.load_palette(cfg, self._registry)
        except (PipelineError, OSError):
            tally.failed.append(entry.name)
            return
        entry.doc = Document.palette_only(loaded.palette, cfg, loaded.ctx, loaded.data)
        if entry.palette_dirty:
            self._carry_palette(previous, entry.doc)
            tally.kept += len(previous.palette_edits)
        self._mirror_palette(entry)
        tally.reloaded.append(entry)
        if self._preview_palette is entry:
            self._refresh_palette_dock()

    def _put_current_back(self, reloaded: list[Entry]) -> None:
        """Repaint the entry on screen from whatever the reload left it with."""
        current = self._workspace.current
        if current is None:
            return
        if current.doc is None:
            self._refresh_stale_current()  # dropped as a slice of a merged file
            return
        if current.doc is not self._doc or any(e is current for e in reloaded):
            self._doc = current.doc
            self._drop_unavailable_edit_mode()
        self._refresh_view()

    def _report_reload(self, paths: list[str], tally: _Reload) -> None:
        names = [basename(p) for p in paths]
        what = names[0] if len(names) == 1 else counted(len(names), "file")
        message = f"Reloaded {what} from disk."
        if tally.kept:
            message += f" Kept {counted(tally.kept, 'unsaved byte')}"
            notes = []
            if tally.conflicts:
                notes.append(f"{tally.conflicts} also changed on disk")
            if tally.dropped:
                notes.append(f"{tally.dropped} past the new end of the file")
            message += f" ({'; '.join(notes)})." if notes else "."
        self.statusBar().showMessage(message)
        if tally.failed:
            self._alert(
                f"{what} was changed on disk, but "
                f"{counted(len(tally.failed), 'entry')} could not be re-read and "
                f"still shows the old bytes: {', '.join(tally.failed)}.",
                title="celPix - changed on disk",
            )
