"""What a tilemap draws through, and who is stale when an entry moves.

A map's cells name tiles of whatever entry it is **bound** to, and that entry
may be another map whose cells name *its* source in turn, to any depth
(``docs/design/tilemap-entry.md`` §3.1); a composite assembles its tile source
out of several entries (``docs/design/composite-entry.md``). Those bindings form
a dependency graph across the open entries, and this module owns it:

- **the chain walk** — whether an entry may supply tiles at all
  (:meth:`~BindingsMixin._can_supply_tiles`), where a chain loops or is refused,
  which entries it passes through (:meth:`~BindingsMixin._chain_entries`) and
  which one owns the art at the end of it
  (:meth:`~BindingsMixin._tile_bank_owner`);
- **the audiences** — the maps and composites reading from an entry, in the
  order a refresh has to reach them (:meth:`~BindingsMixin._entries_bound_to`,
  :meth:`~BindingsMixin._maps_drawing_from`,
  :meth:`~BindingsMixin._chain_dependents`);
- **the invalidations** — re-assembling composites, re-resolving the art a map
  draws, re-chaining the maps above an entry whose cells or width moved
  (:meth:`~BindingsMixin._reassemble_composites`,
  :meth:`~BindingsMixin._reresolve_bound_art`,
  :meth:`~BindingsMixin._rechain_dependents`);
- **the bound-tiles read** a tilemap load draws its art from
  (:meth:`~BindingsMixin._load_bound_tiles`).

Split from :mod:`~celpix.ui.main_window.session`, which owns which entry is on
screen and the load funnels this module re-enters
(:meth:`~...session.SessionMixin._load_entry`,
:meth:`~...session.SessionMixin._restore_session`). Reads, through ``self``,
``_workspace``, ``_registry`` and ``_files_panel`` (created in
``MainWindow.__init__``), the window's ``_doc``, and the region settle and
pixel config its neighbours own (:meth:`~...writing.WritingMixin._settle_region`,
:meth:`~...interpretation.InterpretationMixin._pixel_config`).
"""

from __future__ import annotations

import weakref
from dataclasses import replace
from pathlib import Path

from celpix.core.capabilities import ContentKind
from celpix.core.document import Document
from celpix.core.errors import PipelineError, fault_report
from celpix.core.notices import warn
from celpix.pipeline import pipeline
from celpix.pipeline.pathway import PathwayConfig
from celpix.project import documents
from celpix.project.documents import BoundTiles, TileSourceUnavailable
from celpix.project.workspace import (
    Entry,
    EntryKind,
    TileSource,
    composite_layout,
    composite_preset_id,
    record_composite_layout,
    tilemap_config_for,
    unavailable,
)
from celpix.ui.widgets import wrap_lines


class BindingsMixin:
    """The tile-binding dependency graph: the chain, its audiences, the re-reads.

    A slice of :class:`~celpix.ui.main_window.window.MainWindow`, not a
    standalone object. See the module docstring for what it owns and which
    attributes it reads that others create.
    """

    # The document whose unstated width the maps above it were last re-pointed
    # at, and that width (:meth:`_resync_chain_widths`). Weak, so a closed
    # entry's document is not kept alive by a render that has moved on.
    _chain_width_synced: tuple[weakref.ref[Document], int] | None = None

    # -- what a map draws through --------------------------------------------
    def _binding_target(self, source: TileSource) -> Entry | None:
        """:func:`~celpix.project.documents.binding_target` — the one place a
        binding becomes a usable entry, for every reader of one."""
        return documents.binding_target(self._workspace, source)

    def _draws_through_tilemap(self, entry: Entry) -> bool:
        """:func:`~celpix.project.documents.draws_through_tilemap`."""
        return documents.draws_through_tilemap(self._workspace, entry)

    def _can_supply_tiles(self, entry: Entry, candidate: Entry) -> bool:
        """:func:`~celpix.project.documents.can_supply_tiles`, less a sprite object
        — behind the binding combo, the "From file..." check and every hop of the
        chain walk, so none of them can disagree.

        A sprite object is refused by its **format**, before anything is loaded:
        its records sit at signed pixel offsets rather than in a grid, so there is
        no cell N for a coordinate to name and the resolution would refuse it
        every time (:meth:`_bound_tilemap`). Offering it would be offering a
        binding that can only ever draw nothing.
        """
        if not documents.can_supply_tiles(self._workspace, entry, candidate):
            return False
        return not (
            candidate.content_kind is ContentKind.TILEMAP
            and self._tilemap_is_sprite(candidate)
        )

    def _passes_cells_on(self, hop: Entry) -> bool:
        """Whether the tilemap ``hop``, reached by a binding, lends its cells on.

        The half of the chain gate that the binding cannot answer: a map that
        will not open, or that opened as a sprite object, has no cells to stamp
        and no art behind it, whatever the bindings say. An entry not loaded yet
        passes unless it is already known not to open (:func:`unavailable`) — a
        middle table dropped by an edit elsewhere is re-read the next time a map
        above it is, and resolves as it did before.
        """
        doc = hop.doc
        if doc is None:
            return not unavailable(hop)
        return doc.is_tilemap and not doc.is_sprite

    def _chain_refusal(self, entry: Entry, bound: Entry) -> str:
        """Why ``entry``, bound to the tilemap ``bound``, draws nothing through it.

        The one wording for the bar's note and the notice on the entry's row,
        naming the link that has to move — the binding here may be fine, and it
        is a binding or a file further down that is broken. Asked in the order
        the gate refuses (:meth:`_chain_entries`), so the reason given is the one
        that applied.
        """
        if self._chain_loops(entry, bound):
            return f"{bound.name}'s chain loops back on itself"
        if self._tilemap_is_sprite(bound) or (
            bound.doc is not None and bound.doc.is_sprite
        ):
            return f"{bound.name} is a sprite object, with no cells to stamp"
        hops = self._chain_entries(entry)
        last = hops[-1] if hops else None
        if last is not None and last.content_kind is ContentKind.TILEMAP:
            if not self._passes_cells_on(last):
                return f"{last.name} did not open"
        return f"{bound.name} cannot supply tiles"

    def _chain_loops(self, entry: Entry, candidate: Entry) -> bool:
        """:func:`~celpix.project.documents.chain_loops`."""
        return documents.chain_loops(self._workspace, entry, candidate)

    def _chain_entries(self, entry: Entry) -> list[Entry]:
        """Every entry ``entry``'s tiles pass through, in order, ending at the art.

        Empty for a map bound to nothing; ending at a tilemap for a chain that
        draws blank at its far end. Every hop is **gated**, not just the first,
        and by the gate the resolution uses (:meth:`_bound_tilemap`): the
        binding has to qualify (:meth:`_can_supply_tiles`) — a source can gain a
        binding of its own after this one was made, bind `a` to `b` and then `b`
        back to `a` — and a tilemap reached has to lend its cells on
        (:meth:`_passes_cells_on`). One that does not is the last hop: it is
        what the chain is stuck on, and nothing past it is reached. Re-asking
        per hop is what keeps this answer, the load and the bar's note saying
        one thing — and so what keeps a map whose chain did not resolve from
        owning the art a binding further down names.
        """
        hops: list[Entry] = []
        at = entry
        while at.content_kind is ContentKind.TILEMAP:
            source = at.tile_source
            target = self._binding_target(source) if source is not None else None
            if target is None:
                break
            if not self._can_supply_tiles(at, target) or target is entry:
                break
            if any(hop is target for hop in hops):
                break
            hops.append(target)
            if target.content_kind is ContentKind.TILEMAP and not (
                self._passes_cells_on(target)
            ):
                break
            at = target
        return hops

    def _bound_tilemap(self, entry: Entry) -> Document | None:
        """The tilemap ``entry`` draws through, loaded — or None if it draws art.

        Any tilemap may take its cells from another tilemap's, and that one from
        another, to any depth; what stops a chain is a **loop**
        (``docs/design/tilemap-entry.md`` §3.1).

        The gate is :meth:`_can_supply_tiles`, checked on the binding before the
        source is loaded. That ordering is what keeps this from recursing: two
        maps bound to each other both fail the gate rather than each loading the
        other.

        Loading the source is the ordinary entry load, so it settles its *own*
        chain first and comes back holding it — which is what
        :func:`~celpix.project.documents.chained_document` carries on as the
        next hop. A source that will not load, or loads as a sprite object, is
        refused after the load by :meth:`_passes_cells_on`, the walk's own
        question; the map then draws nothing rather than reading the source's
        file as art (:meth:`_load_bound_tiles`).
        """
        source = entry.tile_source
        candidate = self._binding_target(source) if source is not None else None
        if candidate is None or not self._can_supply_tiles(entry, candidate):
            return None
        if candidate.content_kind is not ContentKind.TILEMAP:
            return None
        # Tried again even where a previous attempt failed: this is a re-read of
        # the map above it, which is when a source the user has since fixed
        # should come back.
        if candidate.doc is None and not self._load_entry(candidate, quiet=True):
            return None
        if not self._passes_cells_on(candidate):
            return None
        return candidate.doc

    def _tile_bank_owner(self, entry: Entry) -> Entry | None:
        """The pixel entry whose bytes ``entry`` draws its tiles from.

        A tilemap's ``pixel_data`` is a *copy* of that entry's art, read through
        its pathway — so a pixel edit made on the map has to be deposited into the
        entry that owns those bytes rather than spliced into the borrowed buffer,
        which nothing else can see and no save would write. This is the same "one
        region, one authority" rule a slice follows into its parent
        (``docs/design/slices-and-parents.md``); the owner here is reached through
        the binding instead of through the file list.

        Walks the binding to the art (:meth:`_chain_entries`): one hop for an
        ordinary map, and one more per map a chained one draws through, whose
        tiles belong to the art at the end of the chain rather than to the
        stamps in between. None when the binding names nothing, names something
        that is not art, or reaches it only through a chain that does not
        resolve — one that loops, or passes through a map that will not open or
        is a sprite object: an edit with no owner is refused rather than
        deposited into a guess.

        A loaded map bound to a tilemap that came up **without a chain** owns
        nothing either, whatever the walk would say now: its ``pixel_data`` is
        the empty stand-in, not a copy of any bank, so there is nothing a stroke
        on it could be deposited from.
        """
        if entry.content_kind is ContentKind.PIXELS:
            return entry
        doc = entry.doc
        if (
            doc is not None
            and doc.is_tilemap
            and doc.chain is None
            and self._draws_through_tilemap(entry)
        ):
            return None
        hops = self._chain_entries(entry)
        last = hops[-1] if hops else None
        if last is None or last.content_kind is not ContentKind.PIXELS:
            return None
        return last

    def _composites_using(self, owner: Entry) -> list[Entry]:
        """Every open composite with ``owner`` among its pieces, in list order.

        The audience for a change to ``owner``'s bytes on the assembly side, as
        :meth:`_entries_bound_to` is on the binding side: each of them holds a
        *join* of those bytes, which an edit reaches only if something puts it
        there — and since a join cannot be patched in place
        (:meth:`_reassemble_composites`), what it gets is a dropped cache and a
        fresh assembly.

        Identity, not paths: a piece names the entry itself, so a file and a
        slice of it are different pieces even though the bytes overlap.
        """
        return [
            other
            for other in self._workspace.entries
            if other.kind is EntryKind.COMPOSITE
            and any(piece.entry is owner for piece in other.pieces)
        ]

    def _open_composite_files(self, entry: Entry) -> None:
        """Open the files ``entry``'s pieces are cut from, wherever one is closed.

        A composite owns no bytes, so anything asked in **its own** coordinates is
        answered by a file one of its pieces belongs to — which an Offset palette
        is, and it is the only thing about a composite that is
        (``docs/design/palette-editing.md`` §2,
        :func:`~celpix.project.documents.palette_offset_owner`). A piece that is a
        *slice* of a file nobody opened left that question with no answer at all:
        the mode was refused for having "no piece from a file" while the file sat
        right there on disk. So reading a composite brings those files into the
        list, once, rather than every consumer learning to look behind a slice for
        one.

        The row is added the way a jump to a closed parent adds one
        (:meth:`~...jumps.JumpsMixin._jump_into_parent`) — a plain FILE on the
        raw container, since a slice's offsets are file-absolute and a detected
        header skip would move the base out from under them — and **not** through
        an undo command: it follows from the composite being read rather than from
        a gesture, and there is no step for an undo to take back.

        Only a slice hides a file. A FILE piece *is* the row, and one that has
        left the list is the ordinary closed-source case the assembly already
        degrades — a second row over the same path would not put the piece back on
        it. A composite can never be a piece
        (:func:`~celpix.project.workspace.can_compose`), so there is no composite
        nesting to walk. A file no longer on disk is left alone, the way every
        other missing file here is: the composite still assembles, and the run it
        could not read goes blank and says so
        (:func:`~celpix.project.workspace.composite_layout`).
        """
        for piece in entry.pieces:
            source = piece.entry
            if source is None or source.kind is not EntryKind.SLICE:
                continue
            if self._workspace.parent_of(source) is not None:
                continue
            if source.parent_kind is EntryKind.SLICE:
                # A nested slice's parent is a slice, not a file: its offset
                # names a place in that slice's decoded bytes, so opening the
                # root file would give it nothing to read. Its run goes blank
                # like any other piece that cannot be read.
                continue
            if not Path(source.path).is_file():
                continue
            if source.parent_kind is EntryKind.PALETTE:
                # A slice of a palette file names a registered palette, and it
                # comes back as one — in the format the slice reads its colour
                # words in, which is the file's own format as the palette had
                # it. The dock's Import as… is only a guess for a file nobody
                # has read yet; here the slice already says what the bytes are.
                seen = source.session and source.session.palette_view_preset_id
                self._workspace.add_palette(
                    source.path,
                    seen
                    if seen and self._registry.has_preset(seen)
                    else self._palette_import_preset_id(),
                    self._detect_palette_container(source.path),
                )
            else:
                self._workspace.open_file(source.path, source.extra_paths)

    def _composite_layout(self, entry: Entry, preset_id: str = ""):
        """Assemble ``entry``'s pieces, settling each one's region first.

        The composite's twin of :meth:`~...interpretation.InterpretationMixin.
        _pixel_config`, and settling is why it exists rather than being a bare
        call to :func:`~celpix.project.workspace.composite_layout`: a piece reads
        its own live buffer, and a *file* piece with an edited slice of its own
        owes that slice a fold before its bytes are true
        (``docs/design/slices-and-parents.md`` §2). Assembling without settling
        would join the pre-fold version and put a stale run in the composite.

        The run lengths it measures are recorded on the entry, so every later
        question about where a piece sits is answered from those rather than by
        assembling again (:attr:`~celpix.project.workspace.Entry.piece_spans`).
        """
        for piece in entry.pieces:
            self._settle_region(piece.entry)
        layout = composite_layout(entry, self._registry, self._workspace, preset_id)
        if record_composite_layout(entry, layout):
            self._files_panel.refresh_entry(entry)
        return layout

    def _region_of(self, entry: Entry) -> list[Entry]:
        """``entry`` and every open entry whose bytes are the same file region.

        A slice's bytes live inside its parent's, so the two are one region with
        two names: an edit through either changes what the other shows
        (``docs/design/slices-and-parents.md``). The answer is the entry first,
        then every link above it for a slice — its parent, and for a nested one
        each parent slice up to the file — then every slice under it, to any
        depth: never sibling slices, which share a parent but not necessarily
        any bytes, and never anything for a composite, whose bytes are all
        somebody else's.
        """
        out = [entry]
        if entry.kind is EntryKind.SLICE:
            out += self._workspace.ancestors_of(entry)
        if entry.kind in (EntryKind.FILE, EntryKind.PALETTE, EntryKind.SLICE):
            out += [
                child
                for child in self._workspace.descendants_of(entry)
                if child.kind is EntryKind.SLICE
            ]
        return out

    # -- who is stale when an entry moves ------------------------------------
    def _entries_bound_to(self, owner: Entry) -> list[Entry]:
        """Every open entry that draws its tiles from ``owner``'s bytes.

        The audience for a change to those bytes: each of them holds a decoded
        copy, so an edit made on any one of them — or on ``owner`` itself — has to
        reach the rest or two views of one bank drift apart
        (:meth:`~celpix.ui.main_window.tile_bytes.TileBytesMixin._apply_pixel_bytes`).

        Resolved through :meth:`_tile_bank_owner`, so a chained map counts as
        drawing from the bank at the end of its chain — which is where its own
        ``pixel_data`` came from. **Shallowest first**, so a caller re-reading
        them re-reads a map's source before the map snapshots it
        (:meth:`_by_chain_depth`).
        """
        return self._by_chain_depth(
            [
                other
                for other in self._workspace.entries
                if other is not owner
                and other.doc is not None
                and other.doc.is_tilemap
                and self._tile_bank_owner(other) is owner
            ]
        )

    def _by_chain_depth(self, maps: list[Entry]) -> list[Entry]:
        """``maps`` with every source before the maps drawing through it.

        A chained map snapshots its source's document when it is read
        (:func:`~celpix.project.documents.chained_document`), so re-reading a
        map before the stamp table under it would carry the table's old
        chain and art straight into the new document. Depth is the number of
        hops to the end of the chain (:meth:`_chain_entries`), taken once, before
        anything is re-read; the sort is stable, so equal depths keep list order.
        """
        depth = {id(entry): len(self._chain_entries(entry)) for entry in maps}
        return sorted(maps, key=lambda entry: depth[id(entry)])

    def _chain_dependents(self, entry: Entry) -> list[Entry]:
        """Every open, loaded map drawing through ``entry`` at any depth.

        Shallowest first — the maps bound to ``entry``, then the maps bound to
        those — which is the order they have to be re-read in.

        Walked on the **bindings**, through maps with no document as much as
        through loaded ones. A middle table dropped by an edit elsewhere still
        stands between ``entry`` and the maps above it, which hold a snapshot
        of it taken when they were read; stopping at the gap would leave them
        drawing the old picture however often they were re-activated. Only the
        loaded ones are returned — a map with no document is read fresh when
        something next asks for it. The visited set is what ends a walk round
        a binding loop.
        """
        found: list[Entry] = []
        visited = {id(entry)}
        frontier = [entry]
        while frontier:
            at = frontier.pop(0)
            for other in self._workspace.entries:
                source = other.tile_source
                if id(other) in visited or source is None or source.entry is not at:
                    continue
                visited.add(id(other))
                frontier.append(other)
                if other.doc is not None:
                    found.append(other)
        return found

    def _maps_drawing_from(self, owners: list[Entry]) -> list[Entry]:
        """Every open map whose art comes from one of ``owners``.

        The audience for an owner *arriving or leaving*, where
        :meth:`_entries_bound_to` is the audience for its bytes changing — same
        question, asked of several owners because a file is removed with its
        slices and a map may be bound to any of them.

        The answer has to be taken **before** a removal and **after** a restore:
        a binding resolves only while the entry it names is open
        (:meth:`_binding_target`), so at the other end of either there is nothing
        left to ask.
        """
        found: list[Entry] = []
        for owner in owners:
            for other in self._entries_bound_to(owner):
                if not any(other is seen for seen in found):
                    found.append(other)
        # And the maps drawing *through* an owner that is itself a map — a stamp
        # table closed out from under a map leaves it holding the table's
        # cells exactly as a closed bank leaves a map holding its art.
        for other in self._workspace.entries:
            if other.doc is None or any(other is seen for seen in found):
                continue
            if any(
                hop is owner for hop in self._chain_entries(other) for owner in owners
            ):
                found.append(other)
        return found

    def _reassemble_composites(
        self, owners: list[Entry], *, keep: Entry | None = None
    ) -> list[Entry]:
        """Rebuild every composite assembled out of ``owners``; return the ones hit.

        The **one** way a composite's join is invalidated, whatever made it stale:
        a piece arriving or leaving the list, or a piece's bytes being edited.
        Both are the same problem — a composite holds a copy of bytes it joined,
        and a join cannot be patched in place, because an offset in it is not an
        offset in the piece it came from. So it is dropped and assembled again.
        (:meth:`_reresolve_bound_art` is the map-side twin, and *that* one can
        patch, which is why the two stay separate.)

        Each owner is taken **with its region** (:meth:`_region_of`): a slice's
        bytes are its parent's, so an edit to either is an edit to the other, and
        a composite built on the other is exactly as stale as one built on the
        entry the stroke named. Without that, a composite over a file went on
        showing the old bytes after a stroke on a slice of it, and one over the
        slice after a stroke on the file — until something else happened to drop
        it. The reassembly reads each piece through the settle that pays the
        fold a slice edit owes (:meth:`_composite_layout`), which is what makes
        the parent's run true rather than merely fresh.

        ``keep`` is the composite an edit was **made on**, which already holds the
        bytes and whose buffer is the one the stroke landed in. It is the only
        exemption — a second composite over the same source is stale whether or
        not it happens to be on screen.

        The rebuild is a drop, not a re-read: a composite has no unsaved state of
        its own to carry across one — every edit made on it was deposited into its
        pieces as it landed (``docs/design/composite-entry.md``) — so the next
        activation reads it fresh, and the one on screen is reloaded here because
        nothing else is going to ask. Quiet, because the gesture that reached here
        was about some other entry: a composite whose pieces have gone missing
        must not put a modal in front of the removal that caused it.

        The list comes back because the maps drawing through these composites
        need the same treatment one step later, and the caller is what knows to
        ask.
        """
        seen: list[Entry] = []
        for stale in [e for owner in owners for e in self._region_of(owner)]:
            for composite in self._composites_using(stale):
                # One composite can hold several of these owners — a file and a
                # slice of it, both pieces of the same composite — and rebuilding
                # it once per owner would reload it once per piece.
                if composite is keep or any(composite is other for other in seen):
                    continue
                seen.append(composite)
                self._rebuild_composite(composite)
        return seen

    def _rebuild_composite(self, composite: Entry, pixel_preset_id: str = "") -> None:
        """Drop ``composite``'s join and, when it is on screen, assemble it again.

        The one step both invalidations end in: a piece's bytes or presence
        changing under the composite (:meth:`_reassemble_composites`), and the
        composite's own list being re-written
        (:meth:`~...slices.SlicesMixin._apply_composite_params`). The second
        used to ask the first, which looks for composites *using* the entry and
        so never found the entry itself — a composite is never a piece — and an
        edit to the list changed the name in the Files pane and nothing on
        screen.

        Off screen the drop is the whole of it: the next activation reads the
        entry fresh. On screen it is reloaded here, quietly, because nothing
        else is going to ask — and its session is captured first, so the offset
        and format the widgets hold ride across the reload with the view the
        drop stashes (:meth:`~celpix.project.workspace.Workspace.drop_document`).
        A reload that fails keeps the document it had, so the entry and the
        window never disagree about what is on screen.

        ``pixel_preset_id``, when given, is the format the entry is re-read at —
        the composite dialog's Pixel / Palette choice. It is written over the
        *captured* session, since capturing reads the pixel picker and would
        otherwise put the old format straight back. A composite never opened
        gets a session only where the format differs from the seed it would
        start on anyway, so an unchanged one keeps following its first source.
        """
        current = composite is self._workspace.current
        if current:
            self._capture_session()
        if pixel_preset_id:
            if composite.session is not None:
                composite.session.pixel_preset_id = pixel_preset_id
            elif pixel_preset_id != composite_preset_id(composite, self._registry):
                composite.session = replace(
                    self._seed_session(composite), pixel_preset_id=pixel_preset_id
                )
        previous = composite.doc
        self._workspace.drop_document(composite)
        if not current:
            return
        if self._load_entry(composite, quiet=True):
            self._doc = composite.doc
            self._restore_session(composite)
        else:
            self._restore_document(composite, previous)
        self._refresh_view()

    def _reresolve_bound_art(self, maps: list[Entry]) -> None:
        """Re-read ``maps`` against whatever their bindings reach **now**.

        A map holds a decoded *copy* of its bank, so closing that bank — or an
        undo putting it back — changes nothing about the map until it is read
        again: it went on drawing art out of a file no longer in the list, and
        the "no tiles bound" state a map with an unresolved source is supposed to
        show never arrived (``docs/design/tilemap-entry.md`` §1). That is the
        arriving-and-leaving twin of :meth:`_resync_tile_bindings`, which patches
        the same copies when the bytes change underneath them.

        An ordinary re-read, so an unsaved cell edit rides across it and a map
        bound through a chain resolves its hop exactly as a fresh load would.
        Quiet, because the gesture was about another entry: a map whose own file
        has since gone missing must not put a modal in front of a removal.

        Entries closed along with the owner are skipped — nothing is left to
        redraw them into — and the view is only repainted if one of them is what
        is on screen, which is the same signal :meth:`_rechain_dependents` gives
        its caller.
        """
        # Whatever draws through one of them is as stale as it is, and has to be
        # read after it (:meth:`_by_chain_depth`).
        for entry in list(maps):
            for other in self._chain_dependents(entry):
                if not any(other is seen for seen in maps):
                    maps = [*maps, other]
        repaint = False
        for entry in self._by_chain_depth(maps):
            if not any(open_ is entry for open_ in self._workspace.entries):
                continue
            if not self._reread_tilemap(entry, quiet=True):
                continue
            if entry is self._workspace.current:
                self._doc = entry.doc
                repaint = True
        if repaint:
            self._drop_unavailable_edit_mode()
            self._refresh_view()

    def _resync_tile_bindings(
        self, owner: Entry, splices: list[tuple[int, bytes]]
    ) -> None:
        """Carry a pixel edit into every open map that borrows ``owner``'s bytes.

        A map holds a decoded **copy** of the bank it is bound to, so an edit to
        those bytes reaches it only if it is put there. That is the pixel twin of
        :meth:`_rechain_dependents`, which does the same for the cells a chained
        map borrows, and it runs in both directions from one place: whether the
        stroke was made on the bank's own entry or on a map drawing through it,
        the bytes end up in ``owner`` and every other view of them is caught here.

        The same splices, because the copies were decoded through the *same*
        pathway — ``_tile_source_config`` builds the map's reader from the bound
        entry's own config, so the two buffers hold the same bytes at the same
        offsets and a splice that is right for one is right for the other. Each
        map's cached bank is patched rather than dropped, so the repaint the
        caller is about to do costs only the tiles that changed
        (``docs/design/tilemap-entry.md`` §8.2).
        """
        for other in self._entries_bound_to(owner):
            self._land_splices(other.doc, splices)

    def _rechain_dependents(self, entry: Entry) -> bool:
        """Re-point every open map drawing through ``entry`` at its new cells.

        True when one of them is the entry on screen, so the caller knows a
        repaint is owed — which happens when an undo lands on a map the view has
        since moved off, onto one that draws through it.

        A cell edit replaces the entry's cell list rather than mutating it, so a
        chained map still holding the old one would keep drawing the stamps as
        they were (:class:`~celpix.core.cellchain.CellChain`). Called from the one
        place a cell list changes, which is what keeps two views of the same stamps
        in step without either being reloaded.

        The **geometry** is re-read along with the cells: the stamp size, the
        source's width and what an ordinal index counts in are the source's
        answers (:func:`~celpix.project.documents.index_reading`), and a source
        whose stated shape
        moved (a codec or preset switch re-reading its header) would otherwise
        leave every dependent stamping at the old one until reloaded.
        """
        stale: list[Entry] = []
        current = self._rechain_through(entry, {id(entry)}, stale)
        if not stale:
            return current
        # Above a map with no document, the maps still loaded hold a snapshot
        # of it that nothing here can re-point — so they are read again, which
        # loads the gap fresh, shallowest first.
        for other in self._by_chain_depth(stale):
            if not self._reread_tilemap(other, quiet=True):
                continue
            if other is self._workspace.current:
                self._doc = other.doc
                self._drop_unavailable_edit_mode()
                current = True
        return current

    def _resync_chain_widths(self, entry: Entry) -> None:
        """Re-point the maps drawing through ``entry`` when its Cols moved.

        A source that states no width and publishes no stride is stamped at the
        width its **view** is laid at
        (:func:`~celpix.project.documents.chain_source_columns`), so Cols on it
        is part of what every map above it draws: the step between a stamp's
        rows and, where indices count stamps on a sheet, which stamp each number
        names. A dependent holds that width as a snapshot.

        Called on every refresh of the entry on screen, and the view is rebuilt
        far more often than Cols moves — so the width last synced is remembered
        and a refresh at the same one costs a comparison. A move re-points the
        dependents outright, which also reaches the maps above one that has no
        document to compare (:meth:`_rechain_dependents`). A document not seen
        before is compared against its **direct** dependents only: each one
        re-pointed re-points the maps above it in turn.
        """
        doc = entry.doc
        if doc is None or not doc.is_tilemap or doc.cells is None:
            return
        if documents.chain_width_is_stated(doc):
            return
        width = self._chain_source_columns(doc)
        synced = self._chain_width_synced
        self._chain_width_synced = (weakref.ref(doc), width)
        if synced is not None and synced[0]() is doc:
            if synced[1] != width:
                self._rechain_dependents(entry)
            return
        for other in self._workspace.entries:
            source = other.tile_source
            if source is None or source.entry is not entry or other.doc is None:
                continue
            chain = other.doc.chain
            if chain is not None and chain.source_columns != width:
                self._rechain_dependents(entry)
                return

    def _rechain_through(
        self, entry: Entry, seen: set[int], stale: list[Entry]
    ) -> bool:
        """:meth:`_rechain_dependents`, one hop at a time.

        A dependent's own chain is part of what a map drawing through *it*
        snapshots (:attr:`~celpix.core.cellchain.CellChain.through`), so each one
        re-pointed here re-points the maps above it in turn — an edit to a
        bottom table restamps the table over it and the map over that. The
        seen-set is belt and braces: the gate already refuses a chain that loops.

        A dependent with **no document** cannot be re-pointed, but the maps
        above it can still be stale: its loaded dependents go into ``stale`` for
        the caller to re-read (:meth:`_chain_dependents` walks the gap).
        """
        through = entry.doc
        cells = through.cells if through is not None else None
        if through is None or cells is None:
            return False
        current = False
        for other in self._workspace.entries:
            doc = other.doc
            source = other.tile_source
            if other is entry or source is None or source.entry is not entry:
                continue
            if id(other) in seen:
                continue
            if doc is None:
                seen.add(id(other))
                for above in self._chain_dependents(other):
                    if id(above) not in seen:
                        seen.add(id(above))
                        stale.append(above)
                continue
            if doc.chain is None:
                continue
            seen.add(id(other))
            stamp = self._chain_stamp_cells(
                other,
                through,
                documents.cell_stamp(self._registry, other, doc.tilemap_ctx),
            )
            # What an ordinal counts is cut by the source's stamp layout, so it
            # is re-read with it.
            reading = documents.index_reading(
                self._registry, other, through=through, stamp=stamp
            )
            refused = (doc.stamp_refusal, doc.addressing_refusal)
            doc.chain = replace(
                doc.chain,
                source=cells,
                stamp=stamp,
                source_columns=self._chain_source_columns(through),
                stamp_column_major=self._chain_column_major(through),
                through=through.chain,
                geometry=reading.geometry,
            )
            doc.addressing_refusal = reading.refusal
            doc.resolve()
            if refused != (doc.stamp_refusal, doc.addressing_refusal):
                # The row wears what the document refuses
                # (:func:`~celpix.project.workspace.entry_notices`), and this is
                # a map the gesture was not made on, so nothing else redraws it.
                self._files_panel.refresh_entry(other)
            current = current or other is self._workspace.current
            current = self._rechain_through(other, seen, stale) or current
        return current

    # -- reading the bound tiles ---------------------------------------------
    def _load_bound_tiles(self, entry: Entry) -> BoundTiles:
        """The tiles a tilemap entry draws from, or an empty stand-in.

        A binding that cannot be read degrades to no tiles rather than failing
        the entry, on the same rule a missing palette follows: the map is still
        worth showing, and the binding is the part the user can re-point. What
        went wrong rides on the stand-in as a **notice**, so the row wears the
        warning mark and its tooltip says which read failed — the same place a
        stage that had to assume something reports, and read the same way. Not
        a dialog: one per load of the map said the same thing every time.
        """
        source = entry.tile_source
        if source is None or not source.is_bound:
            return self._no_tiles()
        bound = self._binding_target(source)
        if bound is not None and bound.content_kind is ContentKind.TILEMAP:
            # Reached only when the resolution refused the map this one draws
            # through (:meth:`_bound_tilemap`). Its file holds cells, and reading
            # them through a pixel codec would draw coordinates as art — and
            # hand a pen stroke a bank to land in. So nothing is read, and the
            # row says which link is broken.
            tiles = self._no_tiles()
            warn(
                tiles.ctx,
                "Source map not resolved",
                wrap_lines(
                    f"{self._chain_refusal(entry, bound)}.\n"
                    "Re-point the map's tile source, or fix the map it names."
                ),
                source="host",
            )
            return tiles
        # The bank's own region settles first: its bytes are what this read is
        # about to take, and a slice of it may owe them
        # (:meth:`~...writing.WritingMixin._settle_region`).
        self._settle_region(source.entry)
        try:
            cfg = self._tile_source_config(entry, source)
        except (PipelineError, KeyError, TileSourceUnavailable) as exc:
            return self._unreadable_tiles(exc)
        live = self._live_bound_tiles(source, cfg)
        if live is not None:
            return live
        try:
            px = pipeline.load_pixel_data(cfg, self._registry)
        except (PipelineError, KeyError, OSError) as exc:
            return self._unreadable_tiles(exc)
        return BoundTiles(
            px.data, px.bytes_per_tile, px.tile_width, px.tile_height, px.ctx, cfg
        )

    def _live_bound_tiles(
        self, source: TileSource, cfg: PathwayConfig
    ) -> BoundTiles | None:
        """:func:`~celpix.project.documents.live_bound_tiles`."""
        return documents.live_bound_tiles(self._workspace, source, cfg)

    def _no_tiles(self) -> BoundTiles:
        """:func:`~celpix.project.documents.no_tiles`, at the combo's format."""
        return documents.no_tiles(self._registry, self._pixel_preset_id())

    def _tilemap_config(self, entry: Entry, preset_id: str) -> PathwayConfig:
        """The pathway that reads ``entry``'s own file as cells.

        The map *is* this file, where the pixel config a tilemap carries points
        somewhere else entirely: the two configs on a tilemap document address
        different files, which is the point.

        Through the workspace, like every other config this window builds, and
        for the reason :func:`~celpix.project.workspace.tilemap_config_for`
        gives — a slice's offset names bytes in its **parent's** buffer, not in
        the file, wherever the parent's container does more than skip a header.
        """
        return tilemap_config_for(entry, preset_id, self._registry, self._workspace)

    def _tile_source_config(self, entry: Entry, source: TileSource) -> PathwayConfig:
        """:func:`~celpix.project.documents.tile_source_config`, through this
        window's :meth:`~...interpretation.InterpretationMixin._pixel_config` so
        a bound slice's parent settles first."""
        return documents.tile_source_config(
            self._workspace, entry, source, self._pixel_config, self._pixel_preset_id()
        )

    def _unreadable_tiles(self, exc: Exception) -> BoundTiles:
        """The no-tiles stand-in, carrying why the bound tiles could not be read.

        Worded so as not to imply the map failed: its cells are what is on
        screen, and the binding is what to look at. The reason is wrapped here
        because it is a stage's own message, which nothing hard-wrapped for a
        tooltip (``docs/py-qt-reference/pyside6-pitfalls.md``).
        """
        tiles = self._no_tiles()
        fault = exc.fault if isinstance(exc, PipelineError) else None
        if isinstance(exc, KeyError):
            # A registry miss: a format id this build has not got. Its str() is
            # the message in quotes, so the message itself is taken, and said
            # under the name the user knows the thing by. A binding that did not
            # resolve is a TileSourceUnavailable, whose str() is its sentence.
            why = f"unknown format: {exc.args[0]}" if exc.args else "unknown format"
        else:
            why = str(exc)
        warn(
            tiles.ctx,
            "Bound tiles could not be read",
            wrap_lines(
                f"{why}\nRe-point the map's tile source, or fix the entry it names."
            ),
            # Attributed to the plugin that raised where one did, and carrying
            # its traceback: a codec that *crashed* over the bank is a plugin
            # fault, which the fault dialog raises once and its author needs.
            source=(exc.plugin if isinstance(exc, PipelineError) else "") or "host",
            report=fault_report(fault) if fault is not None else "",
        )
        return tiles
