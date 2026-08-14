"""Reading and writing a run of tiles as bytes.

The floor every pixel-side edit stands on. Above it the surfaces work in tile
positions - a paste, a transform, a brush stroke, a rearrangement drop - and each
of them ends here, where a run of tiles becomes a decode of the file's bytes or
an undoable splice back into them.

There is exactly **one read** (:meth:`~TileBytesMixin._decode_run`) and **one
write** (:meth:`~TileBytesMixin._apply_tile_edit`), and that is the point rather
than a tidiness: the display-only tile rearrangement
(:mod:`celpix.core.tilerearrangement`) is resolved in those two places and
nowhere else. A position the user sees is served whichever tile the map sends it
to, oriented as the map shows it, and an edit is encoded back to the index the
tile really occupies with the orientation taken off again. Everything upstream
works in the positions on screen and needs to know nothing about any of it; add a
second decode or a second encode elsewhere and a rearranged view starts editing
the wrong bytes.

A tilemap's bank has its own pair of entry points beside those two
(:meth:`~TileBytesMixin._apply_bank_tile_edit`), because a map reaches a
scattered set of tiles rather than a run and because the bytes belong to the
bound entry rather than to the document on screen. Both paths converge again on
the same encode and the same undo push, so the two rules that matter - splices
must be disjoint, and an edit that changes nothing is never pushed - are stated
once.

What is *not* here: which tiles a gesture chose (:mod:`~celpix.ui.main_window.
selection`), what any of the verbs mean (:mod:`~celpix.ui.main_window.
clipboard_ops`, :mod:`~celpix.ui.main_window.transform`,
:mod:`~celpix.ui.main_window.pixel_edit`), and the codecs themselves, which are
the Qt-free pipeline's.
"""

from __future__ import annotations

from collections.abc import Callable

from celpix.core.errors import PipelineError
from celpix.core.tilerearrangement import (
    apply_orientation,
    coalesce_runs,
    unapply_orientation,
)
from celpix.pipeline import pipeline
from celpix.project.workspace import (
    Entry,
    EntryKind,
)
from celpix.ui.undo_commands import (
    PixelEditCommand,
)


class TileBytesMixin:
    """The one decode and the one write behind every tile edit.

    A slice of :class:`~celpix.ui.main_window.window.MainWindow`, not a
    standalone object: it reads and writes the window's own widgets and its
    single live ``_doc``. See the module docstring for what it owns, and the
    package docstring for why these are mixins.
    """

    def _view_frame(self) -> dict:
        """The live view's frame, as the keywords ``decode_tiles``/``encode_tiles``
        take it: byte nudge, column count, 2D reflow and the window's anchor tile.

        Decode and encode must agree on all four or a round-trip lands on
        different bytes than it read, so they ask one place rather than each
        assembling the set from the widgets.
        """
        return {
            "nudge": self._nudge,
            "columns": self._columns.value(),
            "two_dimensional": self._two_d.isChecked(),
            "anchor": self._offset,
        }

    def _decode_run(self, first: int, count: int) -> list | None:
        """Decode ``count`` tiles from **virtual** index ``first``; None on refusal.

        The one place tiles are read, so it is also the one place the
        rearrangement (:mod:`celpix.core.tilerearrangement`) is resolved:
        ``first`` counts
        display positions, and each is served the tile that actually lives
        wherever the map sends it. Unrearranged — the ordinary case, and every
        case while the view shows the file's true order — this is the single
        contiguous decode it always was.

        A rearranged run is gathered from as few decodes as
        :func:`~celpix.core.tilerearrangement.coalesce_runs` can get away with.
        It ends at
        the first position with no tile behind it, exactly as a contiguous decode
        stops at the end of the data: the map only ever permutes tiles that
        exist, so the missing positions are the past-the-end tail and nothing
        earlier is dropped.

        Tiles come back in **display orientation** — a tile the map mirrors or
        turns is oriented here, once, so everything downstream sees what is on
        screen. :meth:`_actual_runs` undoes it on the way back out.
        """
        assert self._doc is not None
        tile_rearrangement = self._active_tile_rearrangement()
        if tile_rearrangement.is_identity():
            return self._decode_actual_run(first, count)
        wanted = tile_rearrangement.actual_run(first, count)
        decoded: dict[int, object] = {}
        for run_first, run_count in coalesce_runs(wanted):
            tiles = self._decode_actual_run(run_first, run_count)
            if tiles is None:
                return None
            decoded.update((run_first + i, tile) for i, tile in enumerate(tiles))
        gathered = []
        for index in wanted:
            if index not in decoded:
                break
            gathered.append(
                apply_orientation(decoded[index], tile_rearrangement.orient_of(index))
            )
        return gathered

    def _decode_actual_run(self, first: int, count: int) -> list | None:
        """Decode a run of **actual** tile indices; None if the pipeline refuses."""
        assert self._doc is not None
        try:
            return pipeline.decode_tiles(
                self._doc, self._registry, first, count, **self._view_frame()
            )
        except PipelineError as exc:
            self._report(exc)
            return None

    def _edit_run(
        self, first: int, count: int, mutate: Callable[[list], None], text: str
    ) -> int:
        """Decode the run at ``first``, let ``mutate`` rewrite it, push one edit.

        The shared spine of every pixel edit that reworks *existing* tiles — a
        transform, a merged stamp — which differ only in how they mutate the
        decoded list. Untouched tiles between the edited ones are decoded here and
        written straight back, so a rectangle's gaps ride along unchanged. Returns
        how many tiles were written (0 if the run won't decode). ``mutate`` gets the
        decoded list in place and may read the originals it overwrites — snapshot
        first if source and destination overlap (a block permutation does).
        """
        decoded = self._decode_run(first, count)
        if not decoded:
            return 0
        mutate(decoded)
        return self._apply_tile_edit(first, decoded, text)

    def _apply_tile_edit(self, first: int, tiles: list, text: str) -> int:
        """Encode ``tiles`` over the run at **virtual** ``first`` as one undoable edit.

        The one place tiles are written, and so — like :meth:`_decode_run` — the
        one place the rearrangement is resolved: each tile is encoded back to the
        index it really occupies, which is what makes a rearranged view
        display-only. Everything upstream (paste, the transforms, the drawing
        tools) keeps working in the positions the user sees and needs to know
        nothing about it.

        Returns how many tiles were written - fewer than offered when the run
        would overrun the data (editing never grows a file). An edit that would
        write back the bytes already there is skipped rather than pushed, so a
        redundant paste doesn't clutter the history.
        """
        assert self._doc is not None
        entry = self._workspace.current
        if entry is None or self._applying_undo:
            return 0
        tiles = tiles[: max(0, self._doc.tile_count - first)]
        if not tiles:
            return 0
        spans = self._encode_spans(self._actual_runs(first, tiles), self._view_frame())
        if spans is None:
            return 0
        self._push_pixel_regions(spans, self._doc.pixel_data, entry, text)
        return len(tiles)

    def _apply_bank_tile_edit(self, tiles: dict[int, object], text: str) -> int:
        """Write ``{bank index: tile}`` back through a tilemap, as one undoable edit.

        The tilemap twin of :meth:`_apply_tile_edit`, and it differs in the two
        ways a map differs from a file of tiles.

        **The indices are a set, not a run.** A map draws the bank in whatever
        order its cells ask for, so one gesture reaches a scattered handful of
        tiles; they are grouped into consecutive runs here only to keep the splice
        count down, not because the gesture had a shape.

        **The bytes belong to somebody else.** ``pixel_data`` on this document is a
        *copy* of the bound entry's art, so the command is pushed against that
        entry — which is what makes the map read clean, the bank read dirty, and a
        write of the bank the thing that puts the edit on disk
        (``docs/design/tilemap-entry.md`` §8.1, ``slices-and-parents.md``). The map
        travels as ``through`` so an undo comes back to the picture the stroke was
        drawn on rather than to the bank.

        The encode uses the codec's **plain 1-D frame** rather than
        :meth:`_view_frame`: the bank was decoded that way
        (:func:`~celpix.pipeline.pipeline.tile_bank`), so bank tile N is the Nth
        ``bytes_per_tile`` of the buffer, and handing over the view's Cols — which
        counts *cells* here — would scatter the bytes under the 2-D stripe walk.
        """
        doc = self._doc
        entry = self._workspace.current
        if doc is None or entry is None or self._applying_undo or not tiles:
            return 0
        owner = self._tile_bank_owner(entry)
        if owner is None:
            self.statusBar().showMessage(
                "This map has no tiles bound - nothing to paint on."
            )
            return 0
        if owner.doc is None:
            self._load_entry(owner, quiet=True)
        if owner.doc is None:
            return 0
        # The *owner's* bytes are what the splices land in and what an undo puts
        # back, so they are what "did this change anything" has to be asked of.
        # The map's copy is derived from them and agrees, but only one of the two
        # is the authority (``slices-and-parents.md``).
        source = owner.doc.pixel_data
        spans = self._encode_spans(self._bank_runs(tiles))
        if spans is None:
            return 0
        self._push_pixel_regions(spans, source, owner, text, through=entry)
        return len(tiles)

    @staticmethod
    def _bank_runs(tiles: dict[int, object]) -> list[tuple[int, list]]:
        """``{index: tile}`` as ``(first, tiles)`` runs of consecutive indices.

        One splice per run instead of one per tile: a stroke along a row of cells
        drawn from a run of the bank is the common case, and it is worth not
        paying a separate encode and a separate undo region for each of them.

        ``gap=0`` because this feeds a **write**: the gap-merging a read can
        afford would rewrite tiles between the runs, which belong to somebody
        else (:meth:`_encode_spans`).
        """
        return [
            (first, [tiles[index] for index in range(first, first + count)])
            for first, count in coalesce_runs(tiles, gap=0)
        ]

    def _push_pixel_regions(
        self,
        spans: list[tuple[int, bytes]],
        source: bytes,
        entry: Entry,
        text: str,
        *,
        through: Entry | None = None,
    ) -> None:
        """Push ``spans`` against ``source`` as one undoable edit, if they change it.

        The tail both write paths share — the pixel view's and a tilemap's — so
        the three rules in it are stated once. An edit that would write back the
        bytes already there is **skipped rather than pushed**, so a redundant
        paste does not clutter the history; the *before* half of every region is
        read from the buffer the splices will land in, which is not always the
        document on screen (:meth:`_apply_bank_tile_edit`); and a **composite**'s
        unowned bytes are dropped here (:meth:`_owned_regions`), which is the one
        place both paths pass through and so the only place that refusal can be
        made once.
        """
        regions = [
            (start, source[start : start + len(data)], data) for start, data in spans
        ]
        regions = [r for r in regions if r[1] != r[2]]
        regions = self._owned_regions(entry, regions)
        if regions:
            self._push_command(
                PixelEditCommand(self, entry, text, regions=regions, through=through)
            )

    def _owned_regions(
        self, entry: Entry, regions: list[tuple[int, bytes, bytes]]
    ) -> list[tuple[int, bytes, bytes]]:
        """``regions`` clipped to the bytes ``entry`` actually has an owner for.

        Only a **composite** ever loses anything here: its buffer holds runs that
        belong to no file — the blank pads standing for holes in the window being
        reproduced, and any run whose source is closed or unreadable — and an
        edit landing there has nowhere to be deposited
        (``docs/design/composite-entry.md``). Keeping it would put pixels on
        screen that the next reassembly silently takes away again, which is the
        worst of the three possible behaviours.

        So the stroke is refused over those bytes and lands everywhere else,
        which is the rule a hidden stamp position already follows on the tilemap
        side (``docs/design/tilemap-entry.md`` §6): the parts of a gesture that
        can be honoured are, and the user is told about the parts that cannot.
        Clipped rather than dropped whole, so a rectangle overlapping a pad edits
        the tiles beside it instead of nothing at all.
        """
        if entry.kind is not EntryKind.COMPOSITE:
            return regions
        kept = [
            (
                first,
                before[first - start : last - start],
                after[first - start : last - start],
            )
            for start, before, after in regions
            for _owner, _at, first, last in self._composite_runs(
                entry, start, len(before)
            )
        ]
        # Measured in **bytes**, not in regions: one region overlapping two runs
        # comes back as two, so counting them would report a loss where there was
        # none — and a region clipped at one end comes back as one, where
        # comparing them pairwise would miss the loss entirely.
        if sum(len(k[1]) for k in kept) != sum(len(r[1]) for r in regions):
            self.statusBar().showMessage(
                "Part of that edit fell on blank tiles this composite has no "
                "source for, and was not applied."
            )
        return kept

    def _encode_spans(
        self, runs: list[tuple[int, list]], frame: dict | None = None
    ) -> list[tuple[int, bytes]] | None:
        """``(start, bytes)`` splices that put each of ``runs`` where it belongs.

        Unrearranged this is the single splice it has always been. A rearranged
        run is cut wherever the actual indices stop being consecutive — strictly,
        unlike the gap-merging a *read* can afford, because the tiles in a gap
        belong to somebody else and must not be rewritten.

        The runs are worked out by the caller because the two write paths group
        differently: the pixel view resolves a rearrangement
        (:meth:`_actual_runs`), a tilemap coalesces scattered bank indices
        (:meth:`_bank_runs`). ``frame`` is likewise the caller's, since a
        tilemap's bank is encoded under the codec's plain 1-D reading rather than
        the view's.

        The splices are **disjoint**, which is what lets them be computed
        independently and applied in any order. That rests on rearrangement being
        unavailable under the 2D walk (:meth:`_rearrange_available`): there a
        tile's bytes interleave with its neighbours' and any write widens to the
        whole bitmap-row, so two runs in one stripe would each rewrite it and the
        second would carry through the first's pre-edit bytes. Off the 2D walk a
        tile owns a contiguous range, and maximal runs are separated by at least
        the tile that split them — so no two spans can touch.
        """
        assert self._doc is not None
        spans = []
        for run_first, run_tiles in runs:
            try:
                start, data = pipeline.encode_tiles(
                    self._doc, self._registry, run_first, run_tiles, **(frame or {})
                )
            except PipelineError as exc:
                self._report(exc)
                return None
            if data:
                spans.append((start, data))
        return spans

    def _actual_runs(self, first: int, tiles: list) -> list[tuple[int, list]]:
        """Split ``tiles`` into ``(actual_first, tiles)`` runs of consecutive homes.

        Also puts the **orientation** back on the way past: ``tiles`` arrive as
        they are displayed, and a tile the map shows mirrored or turned has to go
        back to the file the way the file holds it. Miss this and the mirror or
        turn bakes itself in — the tile would be transformed on disk *and* still
        transformed on screen, so the first thing the user would notice is the art
        coming apart.
        """
        tile_rearrangement = self._active_tile_rearrangement()
        if tile_rearrangement.is_identity():
            return [(first, tiles)]
        homes = tile_rearrangement.actual_run(first, len(tiles))
        runs: list[tuple[int, list]] = []
        for index, tile in zip(homes, tiles, strict=True):
            tile = unapply_orientation(tile, tile_rearrangement.orient_of(index))
            if runs and index == runs[-1][0] + len(runs[-1][1]):
                runs[-1][1].append(tile)
            else:
                runs.append((index, [tile]))
        return runs

    def _apply_pixel_bytes(
        self,
        splices: list[tuple[int, bytes]],
        revision: int,
        owners: tuple[tuple[Entry, int], ...] = (),
        *,
        entry: Entry,
    ) -> None:
        """Land a pixel edit's byte regions - :class:`PixelEditCommand`'s apply.

        The decompressed bytes are the document's source of truth, so an edit is
        a splice into them and Write picks it up from there. There can be several
        regions because a rearranged view scatters one gesture across the file;
        they land together, before the single refresh below. ``revision`` is the
        command's token for the state it just produced: stamping it on the
        *pixel* pathway makes the entry read dirty against what was last
        written, so an undo back to those bytes reports clean again.

        ``owners`` is the same token for **every other entry this one edit is
        also an edit to**, paired with the entry it belongs on. There is more
        than one because an edit can cross more than one boundary: a slice's
        bytes are its parent's, and a **composite**'s are several other entries'
        at once, so a stroke over an assembled run can land in two sources and
        one of their parents (``docs/design/composite-entry.md``).
        The list comes from the command rather than being re-derived here for the
        reason ``entry`` does — an undo has to hand back the exact unsaved state
        each side was in *before*, which only the push site saw.

        ``entry`` is **whose bytes these are**, which is not always the entry on
        screen: a pixel edit made through a tilemap lands in the tile bank the map
        is bound to (``docs/design/tilemap-entry.md`` §8.4). It is carried by the
        command rather than read from ``self._workspace.current`` for that reason,
        and because an undo reaching another entry has already switched to it by
        the time this runs.
        """
        # A lazily-loaded owner: an edit deposited into an entry the user has
        # never activated still has to reach its buffer, since that buffer is
        # what a write of it puts on disk.
        if entry.doc is None:
            self._load_entry(entry, quiet=True)
        if entry.doc is None:
            return
        self._land_splices(entry.doc, splices)
        # A **composite** is never stamped: it owns no bytes, so it has no
        # unsaved state of its own to record and nothing to write that would
        # clear one (``docs/design/composite-entry.md`` §4). Its edit's unsaved
        # state is entirely its pieces', which ``owners`` below carries — the
        # same shape a tilemap already has, where the map reads clean and the
        # bank it painted into reads dirty. Stamping it anyway left it listed by
        # every "unsaved changes" prompt with no gesture able to satisfy them.
        if entry.kind is not EntryKind.COMPOSITE:
            self._workspace.set_pixel_revision(entry, revision)
        for owner, owner_revision in owners:
            self._workspace.set_pixel_revision(owner, owner_revision)
        # The bytes reach the entries that own them before the structural half
        # below: a composite's pieces are where its edit actually lives, and a
        # piece that is a slice then owes its own parent a fold, which is what
        # `_propagate_pixel_edit` on that piece records.
        self._deposit_composite_edit(entry, splices)
        self._propagate_pixel_edit(entry)
        self._resync_tile_bindings(entry, splices)
        # Every composite assembled out of these bytes now holds a stale join of
        # them. A composite is never a piece of another, so editing one rebuilds
        # nothing here — its own pieces were seen to in the deposit above.
        self._reassemble_composites([entry])
        self._refresh_view()

    def _pixel_edit_owners(
        self, entry: Entry, splices: list[tuple[int, bytes]]
    ) -> tuple[Entry, ...]:
        """Every *other* entry an edit to ``entry``'s bytes is also an edit to.

        What :class:`~celpix.ui.undo_commands.PixelEditCommand` reads the before
        revisions off, and in the same order it will stamp the after ones. Two
        boundaries produce them, and they compose:

        - a **slice**'s bytes live inside its parent's region, so the file has
          unsaved changes too (``docs/design/slices-and-parents.md`` §3);
        - a **composite** owns nothing at all, so every piece under the edited
          regions is an owner — and a piece that is itself a slice brings its
          parent along, which is the composing case.

        Deliberately the entries an edit *lands* in rather than every entry that
        can see it: a map drawing from a source shows the change but has no
        unsaved state of its own to record (``docs/design/tilemap-entry.md`` §8.1).
        """
        out: list[Entry] = []

        def add(candidate: Entry | None) -> None:
            if candidate is not None and not any(e is candidate for e in out):
                out.append(candidate)

        if entry.kind is EntryKind.SLICE:
            add(self._workspace.find_file(entry.path))
        elif entry.kind is EntryKind.COMPOSITE:
            for start, data in splices:
                for owner, _at, _first, _last in self._composite_runs(
                    entry, start, len(data)
                ):
                    add(owner)
                    if owner.kind is EntryKind.SLICE:
                        add(self._workspace.find_file(owner.path))
        return tuple(out)

    def _composite_runs(
        self, entry: Entry, start: int, length: int
    ) -> list[tuple[Entry, int, int, int]]:
        """The owned pieces of composite ``entry`` that ``[start, start+length)``
        crosses, as ``(owner, offset in owner, first, last)`` in composite bytes.

        **The one place a composite position becomes an owner and an offset**, so
        the gesture that is *refused* over unowned bytes (:meth:`_owned_regions`)
        and the deposit that *lands* the rest (:meth:`_deposit_composite_edit`)
        cannot disagree about which bytes those are. They did while it was written
        twice: a run whose source failed to read was refused by neither and
        deposited by neither, so the stroke stayed on screen, in the undo stack,
        and vanished at the next reassembly.

        The spans come off the entry, where the assembly that built the buffer
        recorded them (:attr:`~celpix.project.workspace.Entry.piece_spans`) — so
        they describe the bytes actually on screen rather than a re-derivation
        from recorded lengths, and a composite with no document has none, which
        reads as "nothing here is owned".

        A run is owned only if its owner's document can also be *reached*: a span
        says which entry the bytes came from, not whether that entry can still be
        written into. Loading is attempted once here, which is the same load the
        deposit needed anyway.

        Empty for anything that is not a composite, which is what makes the calls
        to it unconditional.
        """
        if entry.kind is not EntryKind.COMPOSITE:
            return []
        out: list[tuple[Entry, int, int, int]] = []
        for span in entry.piece_spans:
            owner = span.owner
            if owner is None:
                continue
            first = max(start, span.start)
            last = min(start + length, span.end)
            if first >= last:
                continue
            if owner.doc is None:
                self._load_entry(owner, quiet=True)
            if owner.doc is None:
                continue  # a run that cannot be written is a run nothing owns
            out.append((owner, span.source_base + (first - span.start), first, last))
        return out

    def _deposit_composite_edit(
        self, entry: Entry, splices: list[tuple[int, bytes]]
    ) -> None:
        """Put a composite's edit into the entries whose bytes it really is.

        A composite's ``pixel_data`` is a *derived copy* of several entries', the
        way a tilemap's is of one, so the same rule applies and for the same
        reason: the copy is not what any save writes, so an edit that stopped here
        would be lost at the next reassembly. Each piece takes its own share
        (:meth:`_composite_runs`), and everything that follows from an edit to
        that piece follows here too — its bank cache is patched, its own maps
        re-sync, and a piece that is a **slice** records the fold it now owes its
        parent (``docs/design/slices-and-parents.md`` §2).

        **The same owner can hold more than one run**, which the composite's own
        buffer does not learn from the deposit: one slice used at two places in a
        tile window is an ordinary thing to want, and splicing into the owner
        leaves the *other* run on screen showing the bytes it had. So each landed
        edit is mirrored into every other run of the same owner that covers it —
        the composite's twin of :meth:`~...session.SessionMixin.
        _resync_tile_bindings`, which does this for the maps drawing from it.

        The revisions are not stamped here: they came off the command
        (:meth:`_apply_pixel_bytes`), so an undo hands each side back the exact
        unsaved state it had rather than a fresh token.
        """
        for start, data in splices:
            for owner, at, first, last in self._composite_runs(entry, start, len(data)):
                cut = data[first - start : last - start]
                self._land_splices(owner.doc, [(at, cut)])
                self._propagate_pixel_edit(owner)
                self._resync_tile_bindings(owner, [(at, cut)])
                # Every *other* composite sharing this piece is now stale; this
                # one holds the edit already and keeps its buffer.
                self._reassemble_composites([owner], keep=entry)
                self._mirror_shared_runs(entry, owner, at, cut, skip=first)

    def _mirror_shared_runs(
        self, entry: Entry, owner: Entry, at: int, data: bytes, *, skip: int
    ) -> None:
        """Show a landed edit in this composite's *other* runs of ``owner``.

        ``at`` is where the bytes went in the owner; ``skip`` is the run that
        already has them. Any other run of the same owner whose source range
        covers ``at`` is showing those bytes at a different composite position, so
        it is spliced there too — otherwise half a tile window updates and half
        does not, and which half depends on where the user happened to draw.
        """
        for span in entry.piece_spans:
            if span.owner is not owner or span.start == skip:
                continue
            begin = span.source_base
            overlap_at = at - begin
            if 0 <= overlap_at < span.length:
                room = min(len(data), span.length - overlap_at)
                self._land_splices(entry.doc, [(span.start + overlap_at, data[:room])])

    def _land_splices(self, doc, splices: list[tuple[int, bytes]]) -> None:  # noqa: ANN001 — a Document
        """Put ``splices`` into ``doc``'s bytes and into everything derived from them.

        The pair is what "these bytes changed" means to a document, and it is a
        pair rather than one call because a tilemap draws every cell from a cached
        decode of the same buffer (:func:`~celpix.pipeline.pipeline.tile_bank`).
        Carrying the edit into that cache rather than dropping it re-decodes only
        the tiles just written, and every cell drawing one of them then shows the
        change on the same repaint (``docs/design/tilemap-entry.md`` §8.2). A
        no-op on a document with no bank.

        One method because the same two steps are owed to every *other* document
        holding a copy of these bytes as well (:meth:`~...session.SessionMixin.
        _resync_tile_bindings`), and a third thing derived from a buffer would
        otherwise have to be found in two places.
        """
        for start, data in splices:
            doc.replace_bytes(start, data)
        pipeline.patch_tile_bank(doc, self._registry, splices)
