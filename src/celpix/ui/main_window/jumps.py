"""Jumping from an entry to where its bytes come from, and bookmarks.

**Jump to Source** shows a slice's bytes in its parent: the parent is handed the
slice's presets, palette and bindings, keeps its own view geometry, and lands
byte-exactly on the slice's offset, opening first if it was closed. A
**bookmark** is the position-only sibling — a view origin with a snapshot of the
settings at creation time and no document of its own, anchored to a whole file
or palette — and jumping to one is the same flow with the snapshot applied
wholesale. The jumps share one body (:meth:`~JumpsMixin._jump_into_parent`):
they differ only in which snapshot they install on the parent before re-reading
it. Each rewrites what the project records for the parent, so each is one undo
step (:class:`~celpix.ui.undo_commands.JumpToParentCommand`,
``docs/design/undo-redo.md`` §3); the parent's unsaved edits are carried into
the re-read document rather than asked about, since a jump changes how the file
is *read*, never which bytes it has. A composite's jump to one of its pieces
opens that piece's entry as it already reads, and so takes no step at all.

Reads, through ``self``: ``_doc`` and ``_workspace`` (created in
``MainWindow.__init__``), and the session capture, seed and re-read of
:mod:`~celpix.ui.main_window.session`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from celpix.core.document import Document
from celpix.project.inputs import (
    with_bindings,
)
from celpix.project.workspace import (
    CompositePiece,
    Entry,
    EntryKind,
    palette_source_for,
)
from celpix.ui.main_window.interpretation import _same_bytes
from celpix.ui.undo_commands import (
    AddEntryCommand,
    JumpToParentCommand,
    ParentState,
)


class JumpsMixin:
    """Jump to Source, composite piece jumps, and bookmarks.

    A slice of :class:`~celpix.ui.main_window.window.MainWindow`, not a
    standalone object. See the module docstring for what it owns and which
    attributes it reads that others create.
    """

    # -- jumping to a parent -------------------------------------------------
    def _jump_to_slice_source(self, slice_entry: Entry) -> None:
        """Files dock ▸ Jump to Source: show a slice's bytes in its parent.

        The inverse of :meth:`~...slices.SlicesMixin._seed_slice_from_parent`
        (which seeds a new slice
        from its parent): it reconfigures the *parent* with the *slice's* own
        pixel and palette settings and lands the view on the slice's offset, so
        the slice's tiles appear at their real position in the whole file. The
        parent is opened first if it was closed. A nested slice's parent is the
        slice it was cut from, shown at the child's offset in its decoded bytes —
        always open while the child can be read, so never reopened.

        A compressed slice arrives with its codec in the *preview* combo, not in
        the parent's own read: the main view always shows raw bytes, so the
        packed structure is what sits at that address, and the decompression
        preview overlay is where those bytes become the slice's tiles again. A
        raw slice leaves the combo at none, which is its ``compression_id``.
        """
        if slice_entry.kind is not EntryKind.SLICE:
            return
        # The current entry's session snapshot lags the live toolbar until a
        # switch captures it, and the palette mode is read off that snapshot -
        # so freshen it, or jumping from the slice on screen carries the palette
        # it had when it was last switched away from rather than the one it is
        # showing. (Same reason _seed_slice_from_parent captures.)
        if slice_entry is self._workspace.current:
            self._capture_session()
        # The slice's settings live on its session (seeded on first load); seed
        # it from the toolbar if it was never activated, exactly as a load would.
        if slice_entry.session is None:
            slice_entry.session = self._seed_session(slice_entry)
        src = slice_entry.session
        # How the slice's bytes are read, not what its own combo was showing: a
        # decompressed slice previews as none (there is nothing left to unpack),
        # and it is the codec that unpacked it that makes the packed bytes at
        # this address readable.
        session = self._settings_session(
            src, preview_compression_id=slice_entry.compression_id
        )
        palette = palette_source_for(slice_entry)
        # The slice's own bindings travel up with its codec: the preview at the
        # source decodes with the same table the slice does, or it would show
        # nothing where the slice shows tiles (``main_window/inputs.py``).
        bound = slice_entry.inputs.get(slice_entry.compression_id)

        def target(parent: Entry) -> ParentState:
            # Keep the parent's view geometry (columns/rows/grid); the origin is
            # landed after load, once the new preset's tile size is known.
            prior = parent.doc.view if parent.doc is not None else parent.pending_view
            return ParentState(
                session=session,
                pending_view=(
                    replace(prior, tile_offset=0, byte_nudge=0)
                    if prior is not None
                    else None
                ),
                pending_palette=palette,
                inputs=(
                    with_bindings(parent, slice_entry.compression_id, bound)
                    if bound
                    else parent.inputs
                ),
                tilemap_preset_id=parent.tilemap_preset_id,
                reread=True,
            )

        self._jump_into_parent(slice_entry, target)

    def _jump_to_piece(self, piece: CompositePiece) -> None:
        """Files dock ▸ a composite's Jump to Source ▸ one piece: open the
        piece's entry with the first byte the piece takes in view.

        Unlike a slice's jump this reconfigures nothing: a piece addresses its
        entry's *resolved* bytes — what that entry already shows when opened,
        under its own format — so the entry's own settings are the right ones
        and there is no snapshot to hand over, and no undo step to take. The
        offset is into that buffer from 0, which is the binding jump's
        arithmetic (:meth:`_go_to_binding`).
        """
        source = piece.entry
        if source is None or not any(e is source for e in self._workspace.entries):
            return
        self._activate_entry(source)
        if self._workspace.current is source and self._doc is not None:
            self._land_on_byte(self._anchor_base() + piece.offset)
            self._refresh_view()

    def _jump_into_parent(
        self, child: Entry, target: Callable[[Entry], ParentState]
    ) -> None:
        """Show ``child``'s parent file under the state ``target`` builds for it
        and land on ``child``'s offset — the shared body of Jump to Source and
        Jump to Bookmark, which differ only in *which* snapshot they hand over:
        a slice's live settings, or a bookmark's recorded ones.

        One undo step (:class:`~celpix.ui.undo_commands.JumpToParentCommand`):
        the snapshot replaces the parent's own recorded format, palette, view
        and bindings, which is a change to what the project saves. A parent that
        is not open is opened first, and that open is part of the same step — a
        macro — so one Ctrl+Z takes back the whole jump, row and all.
        """
        parent = self._workspace.parent_of(child)
        if parent is not None:
            self._push_jump(parent, child, target)
            return
        if child.parent_kind is EntryKind.SLICE:
            # A nested slice's parent is a slice, closed only with everything
            # under it — so this is a broken chain, and reopening the file would
            # land on an offset that was never a file offset.
            self.statusBar().showMessage(f"{child.name}'s parent slice is not open.")
            return
        # Reopened as what the child was cut from: a slice of a palette file
        # names a registered palette, and it comes back as one.
        if child.parent_kind is EntryKind.PALETTE:
            opened = Entry(
                name=Path(child.path).name,
                kind=EntryKind.PALETTE,
                path=child.path,
                container_id=self._detect_palette_container(child.path),
                palette_preset_id=self._palette_import_preset_id(),
            )
        else:
            opened = Entry(
                name=Path(child.path).name, kind=EntryKind.FILE, path=child.path
            )
        self._undo_stack.beginMacro(f'jump to "{child.name}"')
        try:
            self._push_command(AddEntryCommand(self, opened, f"open {opened.name}"))
            # A file's open has already read it and reported one that will not
            # read, so a jump re-reading it would only say so twice. A palette is
            # registered unread (the add never activates one), so the jump's
            # re-read is its first load and the one place a failure is reported.
            if opened.doc is not None or opened.kind is EntryKind.PALETTE:
                self._push_jump(opened, child, target)
        finally:
            self._undo_stack.endMacro()

    def _push_jump(
        self, parent: Entry, child: Entry, target: Callable[[Entry], ParentState]
    ) -> None:
        if parent is self._workspace.current:
            self._capture_session()  # the target reads the live view geometry
        was = parent.doc
        self._push_command(JumpToParentCommand(self, parent, child, target(parent)))
        # A jump that landed always re-read into a new document; one that could
        # not has reported why and left the old one in place.
        if parent.doc is not was and self._workspace.current is parent:
            self.statusBar().showMessage(f"Jumped to {child.name} in {parent.name}")

    def _parent_state(self, parent: Entry) -> ParentState:
        """What a jump would leave behind on ``parent`` right now — the command's
        capture, taken as each direction leaves the state it undoes."""
        if parent is self._workspace.current:
            self._capture_session()  # the live toolbar is newer than the session
        return ParentState(
            session=replace(parent.session) if parent.session is not None else None,
            pending_view=parent.pending_view,
            pending_palette=parent.pending_palette,
            inputs=parent.inputs,
            tilemap_preset_id=parent.tilemap_preset_id,
            doc=parent.doc,
        )

    def _apply_parent_state(
        self,
        parent: Entry,
        state: ParentState,
        *,
        land: Entry | None = None,
        position: int | None = None,
    ) -> bool:
        """Install ``state`` on ``parent`` and show it; land on ``land``'s offset,
        or on ``position`` counted from the view's position 0.

        A ``reread`` state is the jump itself: the parent's document is read
        again under the supplied snapshot, since a new format, palette and view
        all arrive through the load's pending fields. The unsaved bytes the old
        document held are carried into the new one (:meth:`_carry_unsaved`) — a
        jump changes how the file is *read*, never which bytes it has, so there
        is nothing to confirm and nothing to lose. Any other state holds the
        document itself and is simply put back.

        False, with the parent exactly as it was, when the re-read fails.
        """
        previous = parent.doc
        kept = (parent.session, parent.pending_view, parent.pending_palette)
        kept_inputs = parent.inputs
        kept_cells = parent.tilemap_preset_id
        # Copies: a load consumes and a capture rewrites these in place, and the
        # command's state must survive to be applied again.
        parent.session = replace(state.session) if state.session is not None else None
        parent.pending_view = (
            replace(state.pending_view) if state.pending_view is not None else None
        )
        parent.pending_palette = (
            replace(state.pending_palette)
            if state.pending_palette is not None
            else None
        )
        parent.inputs = state.inputs
        parent.tilemap_preset_id = state.tilemap_preset_id
        if state.reread:
            # No pending seed of the re-read's own: the jump arrives under the
            # child's palette and view, installed just above, not the parent's.
            # A carry that fails after a good read rolls back the same way.
            if not self._reread_entry(
                parent, live=self._unsaved_cells(parent), seed_pending=False
            ) or not self._carry_unsaved(previous, parent):
                self._restore_document(parent, previous)
                parent.session, parent.pending_view, parent.pending_palette = kept
                parent.inputs = kept_inputs
                parent.tilemap_preset_id = kept_cells
                return False
        else:
            parent.doc = state.doc
        # A row names its map's layout off the cell format
        # (:meth:`~...tilemap_bar.TilemapBarMixin._apply_tilemap_binding`).
        if parent.tilemap_preset_id != kept_cells:
            self._files_panel.refresh_entry(parent)
        if parent is self._workspace.current:
            self._on_current_entry_changed(parent)  # show it again in place
        else:
            self._activate_entry(parent)
        if land is not None and self._workspace.current is parent and self._doc:
            # A nested slice's offset counts from byte 0 of its parent slice's
            # buffer, which is position 0 of the view; a slice of a file's is
            # written down in the file's coordinates, which is what the landing
            # takes.
            at = land.slice_offset
            if parent.kind is EntryKind.SLICE:
                at += self._anchor_base()
            self._land_on_byte(at)
        elif position is not None and self._workspace.current is parent and self._doc:
            self._land_on_byte(self._anchor_base() + position)
        # The parent's format may have moved either way, and a map bound to it
        # holds tiles decoded under the old one.
        self._reresolve_bound_art(self._maps_drawing_from([parent]))
        return True

    def _carry_unsaved(self, previous: Document | None, parent: Entry) -> bool:
        """Move ``previous``'s unsaved edits onto ``parent``'s freshly read
        document; False — and the alert — if they cannot be carried.

        The pixel buffer is the whole of a pixel file's edits, and the re-read
        leaves it holding the file's own bytes; since the jump does not touch
        which bytes the region has, the edited buffer fits the new document
        exactly. A tilemap's cells come across in the read itself (``live``).
        Palette edits come too, while the palette is still read from the same
        place — under a different source they belong to a palette no longer on
        screen, as they do after any palette switch, and undo brings them back.
        """
        doc = parent.doc
        if previous is None or doc is None or previous.is_tilemap:
            return True
        if parent.pixel_dirty:
            if not _same_bytes(previous.pixel_config, doc.pixel_config) or len(
                previous.pixel_data
            ) != len(doc.pixel_data):
                self._alert(
                    f"{parent.name} has unsaved changes that cannot be carried "
                    "into it read this way. Write it first.",
                    title="celPix",
                )
                return False
            doc.pixel_data = previous.pixel_data
        if parent.palette_dirty and previous.palette_config == doc.palette_config:
            doc.palette = previous.palette
            doc.palette_ctx = previous.palette_ctx
            doc.palette_base_bytes = previous.palette_base_bytes
            doc.palette_edits = set(previous.palette_edits)
        return True

    # -- bookmarks -----------------------------------------------------------
    def _new_bookmark_current(self) -> None:
        """File ▸ New Bookmark on the current entry's file."""
        entry = self._workspace.current
        if entry is not None:
            self._new_bookmark_for(entry)

    def _new_bookmark_for(self, entry: Entry) -> None:
        """Bookmark ``entry``'s current position and settings (the current FILE
        or PALETTE only - the snapshot reads the live view, which nothing else
        has).

        The snapshot is the same trio a project persists per entry - session,
        view options, palette source - copied off the live state, plus the
        view origin as an absolute file offset. A bookmark never loads a
        document, so nothing ever consumes its session/pending fields: they
        *are* the bookmark, applied back onto the parent by every jump.
        """
        if (
            entry is not self._workspace.current
            or self._doc is None
            or entry.kind not in (EntryKind.FILE, EntryKind.PALETTE)
        ):
            return
        self._capture_session()  # the snapshot must read the live toolbar state
        offset = self._slice_prefill_offset()
        assert entry.session is not None  # _capture_session just wrote it
        bookmark = Entry(
            # Named like the offset box shows the position (address format
            # and all) - the icon, not the name, marks it as a bookmark.
            name=self._format_offset(offset),
            kind=EntryKind.BOOKMARK,
            path=entry.path,
            parent_kind=entry.kind,
            slice_offset=offset,
            session=replace(entry.session),
            # The offset carries the position; the view snapshot keeps the
            # geometry (columns/rows/palette row/arrangement) with the origin
            # zeroed, since the jump lands it byte-exactly itself. Not the zoom:
            # it is app-wide, so a jump leaves it where the user is standing
            # rather than pulling them back to where they were when they marked
            # the spot (:class:`~celpix.core.document.ViewOptions`).
            pending_view=replace(self._doc.view, tile_offset=0, byte_nudge=0),
            pending_palette=palette_source_for(entry),
        )
        self._push_command(
            AddEntryCommand(self, bookmark, f'new bookmark "{bookmark.name}"')
        )
        self.statusBar().showMessage(f"Bookmarked {bookmark.name} in {entry.name}.")

    def _jump_to_bookmark(self, bookmark: Entry) -> None:
        """Files dock ▸ double-click / Jump to Bookmark: reapply the snapshot
        to the parent file and land on the bookmark's offset.

        The :meth:`_jump_to_slice_source` flow, with the snapshot applied
        wholesale - session (header settings included: the snapshot *is* the
        parent's own state as of creation), palette source and view geometry
        are copied onto the parent, its cached document dropped so it re-reads
        through them, and the view lands on the absolute offset. Copies, never
        the originals: the parent's first load consumes its pending fields,
        and the bookmark must survive to be jumped to again.
        """
        if bookmark.kind is not EntryKind.BOOKMARK:
            return

        def target(parent: Entry) -> ParentState:
            # Copies, never the originals: the parent's load consumes its pending
            # fields, and the bookmark must survive to be jumped to again.
            session = bookmark.session or parent.session
            return ParentState(
                session=replace(session) if session is not None else None,
                pending_view=(
                    replace(bookmark.pending_view)
                    if bookmark.pending_view is not None
                    else None
                ),
                pending_palette=(
                    replace(bookmark.pending_palette)
                    if bookmark.pending_palette is not None
                    else None  # the snapshot renders through the default palette
                ),
                inputs=parent.inputs,
                tilemap_preset_id=parent.tilemap_preset_id,
                reread=True,
            )

        self._jump_into_parent(bookmark, target)

    def _use_bookmark_as_palette(self, bookmark: Entry) -> None:
        """Files dock ▸ Use as Palette: set the current view's palette to an
        offset palette read at the bookmark's offset.

        The palette lands on whatever is on screen as long as it is anchored to
        the bookmark's file - a **slice** of it included, because a slice's
        Offset palette is addressed in its parent's coordinates too and reaches
        outside its own window by design (``docs/design/palette-editing.md``
        §2). So bookmarking where a palette sits and then colouring a slice with
        it costs no round trip through the parent. Only a view onto some *other*
        file has to navigate there first, or the offset would name the wrong
        bytes; even then the view position is left where it is, and only the
        palette changes. The offset is handed to the same Offset-mode load a
        typed palette offset uses, so it is undoable and persists as an offset
        palette exactly like one.
        """
        # A bookmark of a palette file marks a place among its swatches, not
        # graphics bytes to colour something else with: the menu hides the item
        # for one, and a shortcut reaching here must not open the file a second
        # time as a graphic to read it through.
        if (
            bookmark.kind is not EntryKind.BOOKMARK
            or bookmark.parent_kind is EntryKind.PALETTE
        ):
            return
        current = self._workspace.current
        anchored = (
            current is not None
            and current.kind.has_document
            and current.path == bookmark.path
        )
        if anchored or self._workspace.parent_of(bookmark) is not None:
            self._bookmark_palette_on_file(bookmark, anchored=anchored)
            return
        # The bookmark's file isn't open, so the gesture opens it — which is a
        # row in the list like any other open, and one Ctrl+Z has to take back
        # together with the palette it was opened for.
        self._undo_stack.beginMacro(f'use "{bookmark.name}" as palette')
        try:
            self._load_pixel(bookmark.path)
            self._bookmark_palette_on_file(bookmark, anchored=False)
        finally:
            self._undo_stack.endMacro()

    def _bookmark_palette_on_file(self, bookmark: Entry, *, anchored: bool) -> None:
        """The rest of :meth:`_use_bookmark_as_palette` once the bookmark's file
        is open: show it unless the view is already ``anchored`` to it, then load
        the Offset palette at the bookmark."""
        if not anchored:
            parent = self._workspace.parent_of(bookmark)
            if parent is None:
                return
            if self._workspace.current is not parent:
                self._activate_entry(parent)
            if self._workspace.current is not parent:
                return  # vanished file / bad codec - leave the view untouched
        if self._doc is None:
            return
        # A bookmark's offset is already in the parent's coordinates, which is
        # what an Offset palette addresses - hand it over as it stands.
        self._load_palette_at_offset(bookmark.slice_offset)
