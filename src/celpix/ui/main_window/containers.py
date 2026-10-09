"""Containers and the file resize — and the re-read every re-point ends in.

An entry's **container** says how much of its file is payload and what frames
it; Edit File Container… changes it, or the file list a joined region reads,
and the entry is re-read through the new one. The Size row on the same dialog
**resizes the file** — the payload grown or cut to a number of tiles, cells or
colours and written back through the pathway the edit is about to install — with
the slices whose size matched the file's following it.

Every re-point, a slice's as much as a container's, ends the same way: the
entries whose bytes moved drop their documents (the one on screen is read again
at once, the rest on activation), the slices matching their parent's size are
re-measured, and the graphics rendering a palette that moved are shown its new
colours (:meth:`~ContainersMixin._reread_entries`,
:meth:`~ContainersMixin._refit_to_parents`,
:meth:`~ContainersMixin._reload_palette_consumers`). The container edit is
their main caller; :mod:`~celpix.ui.main_window.slices` and
:mod:`~celpix.ui.main_window.inputs` are the others.

The unsaved-edits questions such a re-read asks sit here too: Write first or
discard (:meth:`~ContainersMixin._confirm_container_discard`), and the plain
Yes/No a slice edit and an inputs change share
(:meth:`~ContainersMixin._confirm_reread_discard`).

Reads, through ``self``: ``_doc``, ``_workspace`` and ``_registry`` (created in
``MainWindow.__init__``), and the region settle and writes of
:mod:`~celpix.ui.main_window.writing`.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from PySide6.QtWidgets import (
    QMessageBox,
)

from celpix.core.capabilities import ContentKind
from celpix.core.context import KEY_SOURCE_OFFSET
from celpix.core.errors import PipelineError, Stage
from celpix.pipeline import pipeline
from celpix.pipeline.pathway import PathwayConfig
from celpix.pipeline.pipeline import inspect_container
from celpix.plugins.base import (
    STAGE_DEFAULT_PRESET,
    FileRef,
)
from celpix.project.workspace import (
    Entry,
    EntryKind,
    file_kind,
    pixel_config_for,
    retarget_files,
    tilemap_config_for,
)
from celpix.ui.container_dialog import ContainerDialog, ContainerEdit
from celpix.ui.container_info_dialog import ContainerInfoDialog
from celpix.ui.undo_commands import (
    ContainerEditCommand,
)
from celpix.ui.widgets import (
    confirm_destructive,
    counted,
)


class ContainersMixin:
    """Containers, the file resize, and the re-read after a re-point.

    A slice of :class:`~celpix.ui.main_window.window.MainWindow`, not a
    standalone object. See the module docstring for what it owns and which
    attributes it reads that others create.
    """

    # -- re-reading what a re-point moved ------------------------------------
    def _reread_entries(self, entries: list[Entry]) -> None:
        """Drop each entry's document so its next activation re-reads the file.

        The view is stashed as ``pending_view`` on the way out and the entry is
        marked saved: what changed is which bytes arrive, not how they are read
        once they do, so the format, arrangement and view have to survive the
        re-read rather than coming back on the codec's defaults. The current
        entry is reloaded immediately — the rest wait until they are activated.
        The caller captures the session first if it needs the *live* widget state
        rather than what was last stored.
        """
        for entry in entries:
            if entry.doc is not None:
                entry.pending_view = entry.doc.view
            self._workspace.mark_saved(entry)
            self._workspace.drop_document(entry)
            self._files_panel.refresh_entry(entry)
        self._refit_to_parents(entries)
        current = self._workspace.current
        if current in entries:
            self._on_current_entry_changed(current)  # re-read the new bytes now
        # A map drawing from or through one of these holds a copy of what it
        # used to read — its art, or its cells and the chain under them — and
        # the maps above it hold a snapshot of that in turn. They are re-read
        # against the new bytes as a close or a restore re-reads them, unsaved
        # cell edits riding across. Asked after the drop, which the walk steps
        # through by binding (:meth:`~...bindings.BindingsMixin._chain_dependents`).
        self._reresolve_bound_art(
            [
                other
                for other in self._maps_drawing_from(entries)
                if not any(other is entry for entry in entries)
            ]
        )
        for entry in entries:
            if entry.kind is not EntryKind.PALETTE:
                continue
            # The graphics showing a palette's colours are still holding the
            # ones the old framing produced, and those are exactly what just
            # changed. The one on screen was re-read above and only needs
            # mirroring; any other is re-read here.
            if entry is current and entry.doc is not None:
                self._mirror_palette(entry)
                self._refresh_view()
            else:
                self._reload_palette_consumers(entry)

    def _refit_to_parents(self, entries: list[Entry]) -> None:
        """Re-measure every slice in ``entries`` that matches its parent's size.

        Every read re-measures one anyway (``workspace._fit_to_parent``), so
        this is not what keeps the bytes right — it is what keeps the *row*
        right for a slice nobody re-opens: its tooltip and the length a save
        writes would otherwise describe the parent as it was. In list order,
        which puts a parent before the slices cut from it, so a chain of
        matched slices is measured link by link from the top.
        """
        for entry in entries:
            if entry.kind is not EntryKind.SLICE or not entry.match_parent:
                continue
            before = entry.slice_length
            try:
                self._slice_config(entry, self._slice_codec_id(entry))
            except (PipelineError, OSError):
                continue  # its own load says why, when it is opened
            if entry.slice_length != before:
                self._files_panel.refresh_entry(entry)

    def _reload_palette_consumers(self, entry: Entry) -> None:
        """Re-read a PALETTE entry and push its colors back onto every graphic.

        A graphic renders a file palette by reference (:meth:`_mirror_palette`),
        so re-reading the file is only half the job — without the mirror the view
        keeps showing colors decoded from bytes the entry no longer offers.
        A load failure leaves the old colors up rather than blanking the view;
        the entry's own error palette reports it when it is next opened.

        The session is snapshotted first for the reason
        :meth:`~...entries.EntriesMixin._remove_entries`
        does it: the current graphic's palette mode only reaches its session on a
        switch, so a palette in use *right now* would otherwise look unused and
        keep the colors it is being changed out of.
        """
        self._capture_session()
        if not self._workspace.palette_consumers(entry):
            return
        if self._load_palette_entry(entry):
            self._mirror_palette(entry)
            self._refresh_palette_dock()
            self._refresh_view()

    # -- containers ----------------------------------------------------------
    def _container_info_current(self) -> None:
        # Follows the entry on screen, like Edit File Container… beside it: a
        # file, or a palette file opened as swatches.
        entry = self._workspace.current
        if entry is not None and entry.kind in (EntryKind.FILE, EntryKind.PALETTE):
            self._show_container_info(entry)

    def _show_container_info(self, entry: Entry) -> None:
        """Files dock / File ▸ Container Info…: what ``entry``'s container read.

        The kinds that have a container of their own, which is the same pair
        :meth:`_change_container_for` acts on — a slice reads through its parent's
        coordinates, so the report a user wants for one is the parent's.

        The config is built here rather than taken from
        :func:`~celpix.project.workspace.pixel_config_for`: the container stage
        needs only the files and the container id, and asking for a whole pathway
        would mean naming a codec preset that nothing in this report interprets.
        The **stored** container id is passed, not the resolved one, so an entry
        whose plugin this build hasn't got says which one is missing instead of
        reporting on the plain-bytes fallback it degraded to.
        """
        if entry.kind not in (EntryKind.FILE, EntryKind.PALETTE):
            return
        cfg = PathwayConfig(
            source=FileRef(entry.paths),
            interpret_preset_id="",
            container_id=entry.container_id,
        )
        ContainerInfoDialog.show_report(self, inspect_container(cfg, self._registry))

    def _change_container_current(self) -> None:
        # The File menu's action follows the entry on screen — a file, or a
        # palette file opened as swatches; a slice is reached from the Files dock.
        entry = self._workspace.current
        if entry is not None and entry.kind in (EntryKind.FILE, EntryKind.PALETTE):
            self._change_container_for(entry)

    def _change_container_for(self, entry: Entry) -> None:
        """Files dock / File ▸ Edit File Container…: repick ``entry``'s files and
        how they are unwrapped.

        Detection chose the container when the file was opened; this is the
        override for what only a person can settle (an interleaved image is
        indistinguishable from a plain one, a headerless dump still ends in
        ``.nes``) — and, beside it, the list of files whose bytes make up the
        region, the region's reshape and the scheme a file that is one
        compressed blob unpacks through, none of which anything can detect at
        all. Slices are excluded for the reason on :attr:`Entry.container_id`
        — theirs are their parent's coordinates, and its file list; a
        *slice's* reshape and compression are edited in the slice dialog.

        A **palette** entry is included, and gets a different list: its file can
        be framed too (colours that stop before the bytes do), and the dialog is
        filtered to the containers that frame a palette so the two sets of
        formats never appear in each other's menu.
        """
        if entry.kind not in (EntryKind.FILE, EntryKind.PALETTE):
            return
        codec_id = self._entry_codec_id(entry)
        # No reshape or compression for a palette: the palette half of a palette
        # document (the colours the dock and every File-mode graphic read) takes
        # the file without either, so one here would transform the swatches
        # alone and leave the two halves describing different bytes of one file.
        palette = entry.kind is EntryKind.PALETTE
        edit = ContainerDialog.edit_container(
            self,
            self._registry,
            paths=entry.paths,
            stages=entry.file_stages,
            kind=file_kind(entry),
            codec_id=codec_id,
            units=self._entry_units(entry, codec_id),
        )
        if edit is None:
            return
        if palette:
            edit = replace(
                edit,
                stages=replace(
                    entry.file_stages, container_id=edit.stages.container_id
                ),
            )
        moved = edit.paths != entry.paths
        if not moved and edit.units is None and edit.stages == entry.file_stages:
            return
        if moved and not self._retarget_allowed(entry, edit.paths[0]):
            return
        # A container decides which bytes the file even has, and so does the file
        # list — so applying either is a re-read, and pixel edits describe
        # positions the new bytes may not have. They cannot come across, so the
        # user gets the choice first. A re-pointed file re-reads its slices with
        # it, so their edits are on the table too. A resize is the same story
        # told about the file rather than the reading of it, so it joins the gate.
        family = [entry, *self._workspace.descendants_of(entry)] if moved else [entry]
        # A slice matching the file's size is re-measured by whatever changed
        # it — a resize, or a container that frames the bytes differently —
        # so it is re-read with the file, and its edits are on the table too.
        family += self._size_followers(entry, family)
        if not self._confirm_container_discard(family):
            return
        # Before the command, and outside it: this one writes the file, and a
        # truncated tail is not something an undo can put back — so it is settled
        # while there is still a Cancel, and the undo stack is told nothing about
        # it. A refusal here calls the whole edit off rather than applying half
        # of what the dialog was left holding.
        if edit.units is not None and not self._resize_entry_file(
            entry, edit, codec_id
        ):
            return
        before = self._container_state(entry)
        # The size is dropped on the way in: it is already on disk, and a command
        # holding it would offer a redo of a write that has no undo.
        after = replace(edit, units=None)
        if after == before:
            # A resize and nothing else: the file changed under the entry, so it
            # has to be re-read, but a step whose two halves are the same would
            # be an undo that does nothing. Under the guard the command would
            # have run it in, so the re-read cannot push a step of its own.
            with self._undo_apply():
                self._apply_container_edit(entry, after)
            return
        self._push_command(
            ContainerEditCommand(self, entry, before=before, after=after)
        )

    @staticmethod
    def _container_state(entry: Entry) -> ContainerEdit:
        """``entry``'s reading as it stands, in the dialog's own shape — what an
        undo puts back, and what a measurement is taken under."""
        return ContainerEdit(entry.file_stages, entry.paths)

    # -- resizing a file -----------------------------------------------------
    def _entry_codec_id(self, entry: Entry) -> str:
        """The format ``entry``'s own bytes are read through — what measures it.

        A size row counts tiles, cells or colours, and only the codec knows what
        one of those costs, so the size question cannot be asked without this.
        Each content kind keeps it in its own place
        (``docs/design/project-format.md`` §4), and an entry that has never been
        activated has no session to keep it in at all — hence the stage's own
        default, which is what a fresh sheet opens on.
        """
        kind = file_kind(entry)
        if kind is ContentKind.PALETTE:
            return entry.palette_preset_id or self._palette_import_preset_id()
        if kind is ContentKind.TILEMAP:
            return (
                entry.tilemap_preset_id or STAGE_DEFAULT_PRESET[Stage.INTERPRET_TILEMAP]
            )
        if entry.session is not None:
            return entry.session.pixel_preset_id
        return STAGE_DEFAULT_PRESET[Stage.INTERPRET_PIXEL]

    def _resize_config(
        self, entry: Entry, edit: ContainerEdit, codec_id: str
    ) -> PathwayConfig:
        """The pathway a resize of ``entry`` reads and writes its file through.

        Built from the **edit** rather than from the entry as it stands: the
        stages the dialog was left holding are the ones the file is about to be
        read through, so they are the ones the resized bytes have to go back out
        through. Framing a payload with the old container and re-reading it with
        the new one would not produce the region the size row was describing.
        """
        edited = replace(entry, path=edit.paths[0], extra_paths=tuple(edit.paths[1:]))
        edited.set_file_stages(edit.stages)
        return self._file_config(edited, codec_id)

    def _file_config(self, entry: Entry, codec_id: str) -> PathwayConfig:
        """The pathway ``entry``'s own file is read and written through, measured
        in ``codec_id`` — what a resize, and a file about to be created, run
        the save's write half with.

        Through the same three builders a load uses rather than a config
        assembled here: they are what decide whether a stage can write at all —
        a plugin this build hasn't got leaves the pathway view-only — and a
        second answer to that question is exactly the kind that goes quietly
        out of date. A palette file's is its palette pathway, which takes the
        container alone (:class:`~celpix.project.entry.FileStages`).
        """
        if file_kind(entry) is ContentKind.PALETTE:
            return self._file_palette_config(
                entry.path, 0, codec_id, entry.container_id
            )
        if entry.content_kind is ContentKind.TILEMAP:
            return tilemap_config_for(entry, codec_id, self._registry)
        return pixel_config_for(entry, codec_id, self._registry)

    def _entry_units(self, entry: Entry, codec_id: str) -> int:
        """How many tiles, cells or colours ``entry``'s region holds right now.

        Read rather than remembered: only the container knows how much of the
        file is payload, and the entry may never have been loaded. A region that
        cannot be measured — an unreadable file, a codec that refuses the preset
        — comes back 0, and the dialog says so in place of a size rather than
        failing to open over a row that is not why it was reached for.
        """
        if not codec_id:
            return 0
        before = self._container_state(entry)
        units = self._region_units(
            lambda: self._resize_config(entry, before, codec_id),
            file_kind(entry),
            codec_id,
        )
        return 0 if units is None else units

    def _resize_entry_file(
        self, entry: Entry, edit: ContainerEdit, codec_id: str
    ) -> bool:
        """Resize ``entry``'s file to ``edit.units``; False if it did not happen.

        The one gesture in this dialog that changes the file rather than the
        reading of it, so it is also the one that has to ask: shrinking drops the
        tail, and no undo puts those bytes back (that is why this runs outside
        the command). Growing needs no question — it adds zeroes past everything
        that was there.

        A false answer calls the *whole* edit off, container and file list
        included: the user cancelled at a prompt about this dialog, and applying
        the half they did not cancel would be a change they never confirmed.
        """
        assert edit.units is not None
        cfg = self._resize_config(entry, edit, codec_id)
        try:
            current, ctx = pipeline.read_region(cfg, self._registry)
            before = len(current)
            after = pipeline.blank_size(
                file_kind(entry), codec_id, edit.units, self._registry
            )
        except PipelineError as exc:
            self._report(exc)
            return False
        except OSError as exc:
            self._alert(f"Cannot read {entry.path}: {exc}", title="celPix - resize")
            return False
        # Where the region starts in the file, which is the container's answer and
        # nobody else's — a slice's offset is file-absolute, so the region's new
        # end has to be put back into those coordinates before the two compare.
        base = int(ctx.get(KEY_SOURCE_OFFSET, 0) or 0)
        if after < before and not self._confirm_shrink(
            entry, before - after, base + after
        ):
            return False
        try:
            pipeline.resize_file(
                cfg,
                kind=file_kind(entry),
                codec_id=codec_id,
                units=edit.units,
                reg=self._registry,
            )
        except PipelineError as exc:
            self._report(exc)
            return False
        except (OSError, ValueError) as exc:
            self._alert(f"Cannot resize {entry.path}: {exc}", title="celPix - resize")
            return False
        self._note_written(entry.paths)
        self.statusBar().showMessage(
            f"Resized {entry.name} - {before:,} bytes to {after:,}"
        )
        return True

    def _confirm_shrink(self, entry: Entry, dropped: int, end: int) -> bool:
        """Ask before truncating ``entry``'s file; True to go ahead.

        Names the children that will not survive it as well as the byte count. A
        slice or bookmark is an offset into this file, and one anchored at or past
        ``end`` — the region's new last byte — has nothing left to read. That is
        the part of a shrink that costs more than the bytes, and it is the part
        the size row cannot show.
        """
        message = f"Shrinking {entry.name} drops {dropped:,} bytes from the end."
        orphaned = [
            child
            for child in self._workspace.children_of(entry)
            if child.slice_offset >= end
        ]
        if orphaned:
            names = ", ".join(child.name for child in orphaned)
            message += (
                f"\n\n{counted(len(orphaned), 'entry')} starting past the new "
                f"end will no longer load: {names}."
            )
        message += "\n\nThis cannot be undone."
        return self._confirm(
            message, title="celPix - resize", accept="Shrink", warn=True
        )

    def _apply_container_edit(self, entry: Entry, edit: ContainerEdit) -> None:
        """Put ``edit``'s file list, container, reshape and compression on
        ``entry`` and re-read - the application path for container edits and
        their undos.

        The children come along whenever the file list moved, and the slices
        nested under them: a slice's offset addresses the parent's *joined*
        buffer, so it has to be joined the same way to mean anything, and it
        finds its parent by the path that is about to change
        (:func:`~celpix.project.workspace.retarget_files`). They are collected
        before the move, while that path is still the old one.
        """
        moved = edit.paths != entry.paths
        family = [entry, *self._workspace.descendants_of(entry)] if moved else [entry]
        family += self._size_followers(entry, family)
        entry.set_file_stages(edit.stages)
        # Format, arrangement and view survive the re-read for the same reason
        # they survive a slice re-point (see
        # :meth:`~...slices.SlicesMixin._apply_slice_params`): what
        # changes is which bytes arrive, not how they are read once they do.
        if entry is self._workspace.current:
            self._capture_session()
        if moved:
            retarget_files(self._workspace, entry, edit.paths)
            # Moved in place, which no addition or removal announces.
            self._sync_disk_watch()
        self._sync_locate_action()  # the new list may name a file that isn't there
        self._reread_entries(family)

    def _size_followers(self, entry: Entry, family: list[Entry]) -> list[Entry]:
        """The slices whose length follows ``entry``'s size, with everything cut
        from them, less any already in ``family``.

        Only the children that match: one with a length of its own keeps it
        whatever the parent does, and so does everything under it. What is cut
        from a matched child comes along whether it matches or not, since its
        parent's bytes are about to be re-read under it.
        """
        followers = [
            e
            for child in self._workspace.slices_of(entry)
            if child.match_parent
            for e in (child, *self._workspace.descendants_of(child))
        ]
        return [e for e in followers if not any(e is f for f in family)]

    def _retarget_allowed(self, entry: Entry, first: str) -> bool:
        """Whether ``entry`` may take ``first`` as its file — i.e. its identity.

        The same rule as relocating onto an open file
        (:meth:`~...projects.ProjectsMixin._relocate_missing`),
        and for the same reason: an entry is its first file, so two entries naming
        one would be two documents editing the same bytes.
        """
        clash = self._workspace.find_file(first)
        if clash is None or clash is entry:
            return True
        self._alert(
            f"{Path(first).name} is already open in this project, so it can't "
            f"become {entry.name}'s file. Pick a different one, or close the "
            "duplicate first.",
            title="celPix - files",
        )
        return False

    def _confirm_reread_discard(
        self, title: str, action: str, what: str, *, whose: str = ""
    ) -> bool:
        """Yes/No: ``action`` re-reads ``what`` from disk, dropping unsaved edits.

        The plain Yes/No sibling of :meth:`_confirm_container_discard` — go on
        or stop, with no Write-first answer — shared by a slice edit and an
        inputs change so the two ask the one question in the same words.
        ``whose`` names the owners where they are not simply ``what``'s own.
        """
        owners = f"{whose} " if whose else ""
        answer = QMessageBox.question(
            self,
            title,
            f"{action} re-reads {what} from disk, discarding {owners}"
            "unsaved changes. Continue?",
        )
        return answer == QMessageBox.StandardButton.Yes

    def _confirm_container_discard(self, entries: list[Entry]) -> bool:
        """Unsaved-edits gate for a container/file-list change; True to proceed.

        The per-entry sibling of :meth:`_resolve_dirty_entries`: only these
        entries are about to be re-read, so offering Write All would touch files
        the user never asked about. Unsaved work is either half: a palette
        entry's colours edited through the dock, as much as bytes painted into
        any entry (a swatch or a folded slice included), and the re-read
        discards both.
        """
        dirty = [e for e in entries if e.pixel_dirty or e.palette_dirty]
        if not dirty:
            return True
        names = ", ".join(e.name for e in dirty)

        def write() -> bool:
            for entry in dirty:
                self._write_entry_checked(entry)
            # A failed write must not proceed — its edits would go with the re-read.
            return not any(e.pixel_dirty or e.palette_dirty for e in dirty)

        return confirm_destructive(
            self,
            "celPix - unsaved changes",
            f"{names} {'have' if len(dirty) > 1 else 'has'} unsaved edits. "
            "Applying this re-reads the bytes and discards them. Write them first?",
            "Write",
            "Discard",
            write,
            default_safe=True,  # least-lossy choice when Enter is hit blind
        )
