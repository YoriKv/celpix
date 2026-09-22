"""Palette bytes read from **another open entry**'s resolved data.

Entry mode: the palette a game *assembles*. Colour rows are copied into colour
RAM from several ROM places, some of them compressed, and what the hardware ends
up holding is the join rather than any one of them. celPix can already express
that join — a composite view over the ROM's palette tables, byte ranges and pads
and all (``docs/design/composite-entry.md``) — and this is what lets a graphic
read one as its colours.

**No second kind of composite**, and that is the whole shape of the feature. The
source is an ordinary pixel entry: a whole file, a slice of one, or a composite
of either. It need not be showing itself as swatches, because bytes are bytes —
:func:`~celpix.project.workspace.can_supply_palette` is the one rule, read by the
picker that offers sources and by the loader that reads them, so what is offered
and what is accepted cannot disagree.

**The consumer owns the colour format.** Which format a run of bytes decodes
under is a fact about the picture being coloured, not about the entry the bytes
sit in — so the dock's Format row stays live here exactly as it is in Offset
mode, and the source's own swatch format only *seeds* it when a source is first
picked.

**An edit rides the source's pixel pathway**, which is celPix's *one region, one
authority* rule (``docs/design/slices-and-parents.md``) reaching the palette
pathway for the second time: the palette's own config is ``write_enabled=False``,
the freshly encoded colour is spliced into the source's buffer, and everything
downstream follows for free — a slice source records the fold it owes its parent,
a composite source cuts the bytes up by owning piece, bound maps re-sync. Bytes
with no writable owner — a composite's pad — refuse the edit rather than keeping
it somewhere the next reassembly takes it away from.

What is not here: the other five load modes and the commit every one of them ends
in (:mod:`~celpix.ui.main_window.palette_source`), the address arithmetic Offset
mode needs (:mod:`~celpix.ui.main_window.palette_offset`), the dock widgets this
drives (:mod:`~celpix.ui.main_window.palette_dock`), and what a colour edit does
once it has an owner (:mod:`~celpix.ui.main_window.color_editing`).
"""

from __future__ import annotations

from PySide6.QtGui import QCursor

from celpix.core.address import format_hex
from celpix.core.document import Document
from celpix.core.errors import PipelineError
from celpix.pipeline import pipeline
from celpix.pipeline.pathway import PathwayConfig
from celpix.plugins.base import PALETTE_PRESET_PARAM, FileRef
from celpix.project import documents
from celpix.project.workspace import (
    Entry,
    EntryKind,
    PaletteMode,
    PaletteSource,
    can_supply_palette,
    interpret_params_for,
)
from celpix.ui.searchable_combo import pick_from_list


class PaletteEntryMixin:
    """Where an Entry-mode palette is read from, and where an edit to it lands.

    A slice of :class:`~celpix.ui.main_window.window.MainWindow`, not a
    standalone object: it reads and writes the window's own widgets and its
    single live ``_doc``. See the module docstring for what it owns, and the
    package docstring for why these are mixins.
    """

    # -- which entries may supply one ---------------------------------------
    def _palette_entry_candidates(self, entry: Entry | None) -> list[Entry]:
        """The open entries ``entry`` could take its palette bytes from."""
        if entry is None:
            return []
        return [e for e in self._workspace.entries if can_supply_palette(entry, e)]

    def _entry_palette_target(self, entry: Entry | None = None) -> Entry | None:
        """:func:`~celpix.project.documents.palette_entry_target` for an entry's
        live binding — ``None`` while the source it names is not open."""
        entry = entry if entry is not None else self._workspace.current
        if entry is None:
            return None
        return documents.palette_entry_target(
            self._workspace, entry, entry.palette_entry
        )

    def _palette_entry_gone(self, entry: Entry, source: PaletteSource) -> bool:
        """Whether ``source``'s entry can no longer supply ``entry``'s palette.

        The mode's own "missing file": a row that has been closed, or one a
        project named that cannot supply bytes at all. Both degrade to the
        generated palette with the reference kept, rather than failing the load
        (:meth:`~...palette_source.PaletteSourceMixin._restore_palette_source`).
        """
        return documents.palette_entry_target(self._workspace, entry, source.entry) is (
            None
        )

    @staticmethod
    def _entry_palette_offset(entry: Entry) -> int:
        """Where in its source ``entry``'s palette starts, degraded or not.

        The live config while the palette is loaded, and the stashed source while
        it is not — which is what lets a re-decode after the source comes back
        read the window the entry actually asked for rather than 0.
        """
        if entry.missing_palette is not None:
            return entry.missing_palette.offset
        return entry.doc.palette_config.source.offset if entry.doc is not None else 0

    # -- picking one ---------------------------------------------------------
    def _pick_palette_entry(self) -> Entry | None:
        """Ask which open entry to read the palette from; ``None`` on cancel.

        A searchable popup of the eligible rows at the cursor, the same picker
        as the composite dialog's *Add source*: the answer is one row out of the
        list that is on screen anyway, so a dialog would be a second, worse copy
        of the Files pane — and a project can hold hundreds, which is why it
        searches. An entry that cannot supply bytes is simply absent rather than
        greyed, because the reason it cannot (it is a map, a palette, this entry
        itself) is a property of that entry rather than anything fixable here.
        """
        candidates = self._palette_entry_candidates(self._workspace.current)
        if not candidates:
            self.statusBar().showMessage("No other pixel entries are open.")
            return None
        row = pick_from_list(self, [c.name for c in candidates], QCursor.pos())
        return candidates[row] if row is not None else None

    def _seed_palette_format_from(self, source: Entry) -> str:
        """The colour format a freshly picked ``source`` suggests.

        A swatch view already states how its bytes decode — that is what the
        *View as Palette* codec's ``palette_preset_id`` param is — so picking one
        as a palette should not make the user say it a second time. Every other
        source says nothing about its bytes, and the dock keeps whatever format
        it was on.
        """
        wanted = interpret_params_for(
            source,
            source.session.pixel_preset_id if source.session is not None else "",
            self._registry,
        ).get(PALETTE_PRESET_PARAM)
        if isinstance(wanted, str) and wanted:
            return wanted
        return self._palette_preset_id()

    # -- reading it ----------------------------------------------------------
    def _entry_palette_source(
        self, source: Entry, byte_off: int, preset_id: str | None = None
    ) -> FileRef | None:
        """:func:`~celpix.project.documents.entry_palette_source`, settling the
        source first so its unsaved edits are what the palette reads."""
        return documents.entry_palette_source(
            self._registry,
            self._workspace,
            source,
            byte_off,
            preset_id or self._palette_preset_id(),
            self._settle_region,
            self._pixel_preset_id(),
        )

    def _entry_palette_config(
        self, entry: Entry, source: PaletteSource, preset_id: str | None = None
    ) -> PathwayConfig:
        """:func:`~celpix.project.documents.entry_palette_config` with the
        window's settle.

        ``preset_id`` is handed over by the **re-decode**, where the live
        document's format is the truth; the restore route omits it and takes the
        session's, which is the only answer there is before a document exists.
        """
        return documents.entry_palette_config(
            entry,
            source,
            self._registry,
            self._workspace,
            self._settle_region,
            preset_id,
        )

    def _load_palette_from_entry(
        self, source: Entry, byte_off: int = 0, preset_id: str | None = None
    ) -> bool:
        """Read ``source``'s bytes at ``byte_off`` as the current palette.

        The single forward gesture behind the mode dropdown's *Entry* pick, the
        files pane's *Use as Palette*, and the offset box while the mode is on.
        ``False`` (with the failure already reported) so the dropdown can revert
        instead of lying about where the colours came from.

        The binding is written onto the entry **before** the commit, because the
        commit's undo capture reads it: a :class:`~celpix.ui.undo_commands.
        PaletteState` carries the source so stepping back through a re-pick lands
        on the entry that was showing rather than on the one just chosen.
        """
        entry = self._workspace.current
        if self._doc is None or entry is None:
            return False
        preset_id = preset_id or self._palette_preset_id()
        try:
            ref = self._entry_palette_source(source, byte_off, preset_id)
        except (PipelineError, OSError) as exc:
            self._alert(f"Cannot read {source.name}: {exc}", title="celPix - palette")
            return False
        if ref is None:
            self._alert(
                f"Not enough data at that offset in {source.name} for a palette entry.",
                title="celPix - palette",
            )
            return False
        # A plain byte offset: this indexes the source's resolved buffer from 0,
        # which no bank layout describes (:meth:`~...palette_offset.
        # PaletteOffsetMixin._format_palette_offset`).
        where = format_hex(byte_off)
        return self._load_and_commit_palette(
            PathwayConfig(
                source=ref,
                interpret_preset_id=preset_id,
                # The bytes are the source's: an edit rides *its* pixel pathway,
                # never this one (:meth:`_sync_entry_palette_bytes`).
                write_enabled=False,
            ),
            mode=PaletteMode.ENTRY,
            label=f"read palette from {source.name}",
            status=lambda n: f"Read {n} colors from {source.name} at {where}",
            source_entry=source,
        )

    def _use_entry_as_palette(self, source: Entry) -> None:
        """Files pane ▸ *Use as Palette*: apply ``source`` to the current graphic.

        One undoable palette change, like a ``.pal``'s double-click. Refused
        where there is nothing to apply it to, or where the row cannot supply
        bytes — the same rule the picker filters by, restated here because a
        context menu is reached without going through it.
        """
        entry = self._workspace.current
        if self._doc is None or entry is None:
            self.statusBar().showMessage("Open pixel data first.")
            return
        if not can_supply_palette(entry, source):
            self.statusBar().showMessage(
                f"{source.name} has no pixel bytes to read a palette from."
            )
            return
        self._load_palette_from_entry(source, 0, self._seed_palette_format_from(source))

    # -- editing it ----------------------------------------------------------
    def _entry_palette_owned(self, source: Entry, at: int, length: int) -> bool:
        """Whether ``[at, at+length)`` of ``source`` has somewhere an edit can go.

        A plain file or slice owns all of its own bytes, so the question is only
        whether it can be written at all. A **composite** owns only the runs its
        pieces cover: the blank pads standing for holes in the window being
        reproduced belong to nobody, and so does any run whose source is closed
        or unreadable. :meth:`~...tile_bytes.TileBytesMixin._composite_runs` is
        the one place that is decided, so the refusal here and the deposit below
        cannot disagree about which bytes those are.
        """
        if source.kind is EntryKind.COMPOSITE:
            # The spans are recorded by an assembly, so a composite nobody has
            # opened owns nothing at all until it is loaded — and the deposit
            # would load it a moment later anyway.
            if source.doc is None and not self._load_entry(source, quiet=True):
                return False
            covered = sum(
                last - first
                for _owner, _at, first, last in self._composite_runs(source, at, length)
            )
            return covered >= length
        if source.doc is None and not self._load_entry(source, quiet=True):
            return False
        return source.doc is not None and source.doc.pixel_config.write_enabled

    def _entry_palette_refusal(self, index: int) -> str | None:
        """Why colour ``index`` cannot be edited here, or ``None`` if it can.

        The palette's bytes are somebody else's, and a colour whose own read unit
        falls where nothing owns them has nowhere to be written — the rule
        :meth:`~...tile_bytes.TileBytesMixin._owned_regions` already applies to a
        pixel stroke over a composite's pad. Refused rather than forked to a
        Custom palette: forking would quietly sever the link to the ROM bytes,
        which is the entire point of the mode.
        """
        source = self._entry_palette_target()
        if source is None:
            return "The entry this palette is read from is not open."
        doc = self._palette_doc()
        if doc is None:
            return None
        at, length = self._palette_unit_span(doc, index)
        if length <= 0 or self._entry_palette_owned(source, at, length):
            return None
        return (
            f"That color sits in bytes {source.name} has no source for, "
            "so the edit was not applied."
        )

    def _palette_unit_span(self, doc: Document, index: int) -> tuple[int, int]:
        """Where colour ``index``'s **read unit** sits in the source's bytes.

        The unit and not the entry, for the reason
        :func:`~celpix.pipeline.pipeline.spliced_palette_bytes` splices one: a
        Game Boy shade shares its byte with three others, so the byte is the
        smallest thing that can be put back. ``(0, 0)`` where the format cannot
        be asked, which reads as "nothing to refuse".
        """
        preset_id = doc.palette_config.interpret_preset_id
        try:
            size = pipeline.palette_entry_size(preset_id, self._registry)
            per_unit = pipeline.palette_entries_per_unit(preset_id, self._registry)
        except (PipelineError, KeyError):
            return (0, 0)
        if size <= 0:
            return (0, 0)
        return doc.palette_config.source.offset + (index // per_unit) * size, size

    def _entry_palette_pixel_owner(self) -> Entry | None:
        """The entry whose pixel bytes hold the on-screen Entry palette.

        :meth:`~...palette_offset.PaletteOffsetMixin._offset_palette_pixel_owner`
        one mode over, and the same contract: ``None`` for every other palette,
        and the entry loaded on demand, since an edit deposited into a source the
        user has never activated still has to reach its buffer.
        """
        if self._palette_mode is not PaletteMode.ENTRY or self._doc is None:
            return None
        source = self._entry_palette_target()
        if source is None:
            return None
        if source.doc is None and not self._load_entry(source, quiet=True):
            return None
        return source

    def _palette_unit_splices(
        self, doc: Document, source: Entry, index: int
    ) -> list[tuple[int, bytes]]:
        """Where colour ``index``'s freshly encoded bytes go in ``source``.

        **One read unit, and no more.** Splicing the whole window would deposit
        it into every piece the window crosses, stamp each of them dirty and make
        the next Write re-encode bytes nobody touched — which on a compressed
        slice is a search for a packing of its own unchanged data, the thing
        ``docs/design/palette-editing.md`` names as the reason a palette save
        splices rather than re-encoding. The unit rather than the entry because a
        packed format puts four Game Boy shades in one byte, and the byte is the
        smallest thing that can be put back.

        The bytes come from a whole-window splice all the same
        (``pipeline.spliced_palette_bytes``), because that is what knows how to
        encode an entry over the buffer it was read from; only the *cut* is
        narrowed. Empty where the unit falls outside the window, or — on a
        **composite** — where nothing owns it, the same clipping a stroke over a
        pad gets.
        """
        try:
            window = pipeline.spliced_palette_bytes(doc, self._registry)
        except PipelineError as exc:
            self._report(exc)
            return []
        base = doc.palette_config.source.offset
        at, length = self._palette_unit_span(doc, index)
        if length <= 0 or at < base or at + length > base + len(window):
            return []
        cut = window[at - base : at - base + length]
        if source.kind is not EntryKind.COMPOSITE:
            return [(at, cut)]
        return [
            (first, cut[first - at : last - at])
            for _owner, _pos, first, last in self._composite_runs(source, at, length)
        ]

    def _sync_entry_palette_bytes(
        self, doc: Document, source: Entry, index: int, consumer: Entry | None = None
    ) -> list[Entry]:
        """Deposit colour ``index``'s bytes into ``source``'s pixel data.

        The persistence half of an Entry-palette edit, and deliberately the very
        machinery a *pixel* edit on ``source`` would use: re-encode the edited
        entry over the splice base and hand its read unit to
        :meth:`~...tile_bytes.TileBytesMixin._land_byte_edit` as an ordinary
        splice. Everything downstream then follows for free — a slice records the
        fold it owes its parent, a composite cuts the bytes up by owning piece,
        tile caches are patched, stale joins are dropped and bound maps re-sync.

        Recomputed from the palette's state rather than diffed, so it runs on
        undo as well as redo and the source's buffer always mirrors the palette
        on screen. The revisions are the command's, stamped by the caller.

        ``consumer`` is the entry ``doc`` belongs to, and it is handed down as the
        document the landing may **not** take away: the source can be the
        consumer's own parent file, and a landing there drops every slice below it
        (:meth:`~...writing.WritingMixin._propagate_pixel_edit`) — the consumer
        among them, leaving the window drawing an orphan while the palette it is
        showing is the one this edit was made on.

        Returns every entry whose bytes the deposit moved, so the caller can
        re-decode the *other* palettes read out of them in one pass.
        """
        if source.doc is None and not self._load_entry(source, quiet=True):
            return []
        if source.doc is None:
            return []
        splices = self._palette_unit_splices(doc, source, index)
        if not splices:
            return []
        return self._land_byte_edit(source, splices, keep=consumer)

    def _palette_entry_owners(self, source: Entry, index: int) -> tuple[Entry, ...]:
        """Every entry editing colour ``index`` here is also an edit to.

        :meth:`~...tile_bytes.TileBytesMixin._pixel_edit_owners` asked of that
        colour's **read unit** — the same span the deposit writes, so the entries
        that go dirty are exactly the entries whose bytes moved. Asked of the
        whole window instead, a single colour dirtied every piece the palette
        crosses.

        The source itself leads, unless it owns nothing (a composite); then come
        each piece the unit falls in and any parent those pieces owe a fold to.
        What :class:`~celpix.ui.undo_commands.ColorEditCommand` reads the before
        revisions off, in the order it will stamp the after ones.
        """
        doc = self._palette_doc()
        if doc is None:
            return ()
        at, length = self._palette_unit_span(doc, index)
        owners: list[Entry] = []
        if source.kind is not EntryKind.COMPOSITE:
            owners.append(source)
        for owner in self._pixel_edit_owners(source, [(at, bytes(length))]):
            if not any(owner is seen for seen in owners):
                owners.append(owner)
        return tuple(owners)

    # -- keeping consumers current -------------------------------------------
    def _redecode_entry_palettes(
        self, sources: list[Entry], *, skip: Entry | None = None
    ) -> None:
        """Re-decode every loaded palette read out of one of ``sources``.

        The one place an Entry palette is refreshed, whatever made it stale: an
        edit to the source, an undo or redo of one, a colour edit from another
        consumer, a composite reassembled under it, a piece arriving or leaving.
        All of them are the same problem — a palette is a *decode* of bytes that
        have moved — and it is answered by reading them again under the same mode
        and the same config shape.

        **No undo step is pushed**, because nothing about the consumer changed:
        the edit that moved the bytes is already a step of its own, and a second
        one here would make one Ctrl+Z take back half of it. A consumer whose
        source can no longer be read is left showing the colours it has, which is
        the conservative answer while the gesture that reached here was about
        something else entirely.

        ``skip`` is the consumer an edit was *made* on, whose palette is the
        authority the new bytes came from.
        """
        seen: list[Entry] = []
        for source in sources:
            for consumer in self._workspace.palette_entry_consumers(source):
                if consumer is skip or consumer.doc is None:
                    continue
                if not any(consumer is other for other in seen):
                    seen.append(consumer)
        for consumer in seen:
            self._redecode_entry_palette(consumer)
        if seen and self._doc is not None:
            self._refresh_view()

    def _degrade_entry_palettes(self, sources: list[Entry]) -> None:
        """Fall every palette read out of one of ``sources`` back to the default.

        :meth:`_redecode_entry_palettes`' arriving-and-leaving twin, run when a
        source is closed: the binding still names the entry — it is a live object
        undo may yet put back — but it is no longer in the list, so there is
        nothing to read. The consumer keeps its mode, shows the generated
        palette, and its row is marked, which is what a tilemap bound to a closed
        bank already does (``docs/design/tilemap-entry.md`` §1).
        """
        repaint = False
        for source in sources:
            for consumer in self._workspace.palette_entry_consumers(source):
                if consumer.doc is None or consumer.missing_palette is not None:
                    continue
                consumer.missing_palette = PaletteSource(
                    entry=source, offset=self._entry_palette_offset(consumer)
                )
                consumer.doc.palette = self._fallback_palette()
                self._files_panel.refresh_entry(consumer)
                repaint = repaint or consumer is self._workspace.current
        if repaint and self._doc is not None:
            self._refresh_view()

    def _redecode_entry_palette(self, consumer: Entry) -> None:
        """Read ``consumer``'s palette again from its source's current bytes.

        Every input is read off the **live** state rather than the session: the
        offset from the config the palette is showing (or the stashed source
        while it is degraded), and the colour format from that config too. A
        session's copy of either is only written on an entry switch, so taking it
        here would re-decode the entry on screen at the format and offset it was
        opened with — silently undoing a Format pick or an offset step made since
        (:func:`~celpix.project.documents.entry_palette_config`).
        """
        doc, session = consumer.doc, consumer.session
        if doc is None or session is None:
            return  # not a consumer at all; nothing to re-read it with
        source = PaletteSource(
            entry=consumer.palette_entry, offset=self._entry_palette_offset(consumer)
        )
        try:
            cfg = self._entry_palette_config(
                consumer, source, doc.palette_config.interpret_preset_id
            )
            loaded = pipeline.load_palette(cfg, self._registry)
        except (PipelineError, OSError):
            return
        consumer.missing_palette = None
        doc.palette, doc.palette_ctx = loaded.palette, loaded.ctx
        # The splice base moves with the bytes: a later edit re-splices against
        # what is there now rather than resurrecting what was there before. The
        # touched set goes with it, those entries having just been written.
        doc.palette_base_bytes, doc.palette_edits = loaded.data, set()
        doc.palette_config = cfg
        self._files_panel.refresh_entry(consumer)  # its row may have been marked
