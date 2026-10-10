"""Slices and composites: carving an entry out of another, or assembling one.

A **slice** is an offset+length region of a parent that acts as its own
document (``docs/design/slices-and-parents.md``). It nests in a file, a palette
file or another slice, whose decoded bytes its offsets then count in. This
module creates them — from the dialog, the viewport or the selection — seeded
to read the parent the way the parent is read now
(:meth:`~SlicesMixin._seed_slice_from_parent`); edits one, re-pointing it and
re-reading what is nested in it; and resizes a compressed one by re-packing it
as an edit to its parent.

A **composite** is the opposite gesture: one tile source assembled out of
several entries rather than cut out of one (``docs/design/composite-entry.md``).
Its dialog, its measuring and its edit live here beside the slice's, since both
are "a new entry whose bytes are another entry's".

Where a slice leads *back* — Jump to Source, and the bookmarks that jump the same
way — is :mod:`~celpix.ui.main_window.jumps`'s; the re-read a re-point ends in
is :mod:`~celpix.ui.main_window.containers`'s.

Reads, through ``self``: ``_doc`` and ``_workspace`` (created in
``MainWindow.__init__``), ``_structure_extent`` (``MainWindow.__init__``, set by
the structure scan in :mod:`~celpix.ui.main_window.compression`), and the
session capture and seed (:meth:`~...session.SessionMixin._capture_session`,
:meth:`~...session.SessionMixin._settings_session`).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from celpix.core.address import format_hex
from celpix.core.capabilities import Capability, ContentKind
from celpix.core.document import Document, ViewOptions
from celpix.core.errors import PipelineError, Stage
from celpix.pipeline import pipeline
from celpix.pipeline.pathway import PathwayConfig
from celpix.plugins.base import (
    NO_COMPRESSION,
    InputKind,
)
from celpix.project.inputs import (
    IntegerFromBytes,
    RegionBinding,
    input_specs,
)
from celpix.project.workspace import (
    CompositePiece,
    Entry,
    EntryKind,
    SliceParams,
    anchor_kind,
    composite_format_for,
    composite_layout,
    composite_preset_id,
    is_swatch_preset,
    new_composite,
    own_bytes,
    palette_source_for,
    pixel_config_for,
    slice_of,
    swatch_session,
    tilemap_config_for,
)
from celpix.ui.composite_dialog import CompositeDialog, CompositeParams
from celpix.ui.slice_dialog import SliceDialog
from celpix.ui.undo_commands import (
    AddEntryCommand,
    CompositeEditCommand,
    SliceEditCommand,
)


class SlicesMixin:
    """Slices and composites: creating, editing and resizing them.

    A slice of :class:`~celpix.ui.main_window.window.MainWindow`, not a
    standalone object. See the module docstring for what it owns and which
    attributes it reads that others create.
    """

    # -- slice creation ------------------------------------------------------
    def _seed_slice_from_parent(self, slice_entry: Entry) -> None:
        """Open a new slice reading its parent the way the parent is read *now*.

        A slice is a region of its parent file viewed through the same codecs,
        so it should inherit the parent's current pixel preset and palette
        (format, mode, and the actual offset/file/colors) rather than the
        app-wide toolbar defaults - otherwise a slice carved from a file being
        viewed as, say, snes-4bpp with an offset palette would open blank as
        the built-in default. Pre-seeding the entry's session/pending-palette
        here means its first load skips :meth:`_seed_session`; both are
        consumed on that load. If the parent isn't open (or was never
        activated) there's nothing to copy - the toolbar seed then applies.

        The **palette row** and the **arrangement** (the Pattern picker's
        block size/order/2D, and the bitmap width that belongs to 2D) ride along
        too. Both are part of how the bytes are read rather than merely where the
        window sits: the row picks which colors the tiles index (with a 4bpp
        format over a 256-color palette, which 16), and the arrangement decides
        which bytes land in which tile at all - so a slice carved out of
        graphics being viewed on row 3 in 2x2 blocks has to arrive that way, or
        it opens in the wrong colors or as scrambled tiles. Both live in the
        view options rather than the session, hence the second hand-off.

        A slice of a **palette file** inherits the swatch view the same way —
        the parent's session names the swatch codec and the colour format it
        reads — and no palette source, since its parent has none to hand down
        (:func:`~celpix.project.workspace.palette_source_for`).
        """
        parent = self._workspace.parent_of(slice_entry)
        if parent is None:
            return
        # The current entry's session snapshot lags the live toolbar until a
        # switch captures it; freshen it so we copy what's actually on screen.
        if parent is self._workspace.current:
            self._capture_session()
        src = parent.session
        if src is None and parent.kind is EntryKind.PALETTE:
            # A registered palette nobody has opened has no session to copy,
            # but the one it will open on is known: swatches, in its own colour
            # format — which is what a run cut out of it has to read as too.
            # Asked for without being installed, since a palette's session is
            # saved with the project: carving a slice must not rewrite its
            # parent.
            src = swatch_session(parent, self._registry, self._palette_preset_id())
        if src is None:
            return
        # A slice's bytes are already decompressed - no preview codec.
        slice_entry.session = self._settings_session(
            src, preview_compression_id=NO_COMPRESSION
        )
        slice_entry.pending_palette = palette_source_for(parent)
        # Only the palette row and the arrangement: the rest of the geometry
        # is left at the defaults a fresh slice gets anyway, since the parent's
        # window size describes a different region than the one being carved
        # out. (Columns is the exception the bitmap width owns - it is
        # re-derived from the width on the render path.)
        view = parent.doc.view if parent.doc is not None else parent.pending_view
        if view is not None:
            slice_entry.pending_view = ViewOptions(
                palette_row=view.palette_row,
                block_columns=view.block_columns,
                block_rows=view.block_rows,
                block_order=view.block_order,
                two_dimensional=view.two_dimensional,
                bitmap_width=view.bitmap_width,
            )

    def _slice_prefill_offset(self, position: int | None = None) -> int:
        """A view position in the coordinates a slice offset is written in.

        The parent's own coordinates (:meth:`_anchor_base`), which is what a slice
        addresses: past whatever the container skipped for a whole file, 0-based in
        the reordered buffer under a reshape. Deliberately *not* the number the
        offset box shows while a slice is on screen - that counts from the slice's
        own first byte, while a slice carved here is anchored in the file the
        parent lineage reads. Taking the *config's* requested offset instead would
        prefill a headered file's slices short by its header - the config never
        names the start the container worked out for itself.

        ``position`` defaults to the grid's current byte position; the selection
        and structure gestures pass their own document-relative start.

        On a **slice** the new slice is nested in it, and its coordinates are the
        slice's own decoded buffer from byte 0 — so the view position is the
        offset as it stands.
        """
        assert self._doc is not None
        at = self._byte_position() if position is None else position
        current = self._workspace.current
        if current is not None and current.kind is EntryKind.SLICE:
            return at
        return self._anchor_base() + at

    def _slice_source(self) -> tuple[Entry, Document] | None:
        """The current entry + document if a slice can be carved from the view.

        Whatever is on screen can be carved out of, because its positions are
        the coordinates a slice offset is written in: a file's own, which a
        reshaped, interleaved or whole-file-decompressed view's still are,
        since a slice of such a parent reads that same buffer
        (:func:`~celpix.project.workspace.reorders_bytes`). A **slice** always
        qualifies too, decompressed or not: what is carved from it is nested in
        it, and a nested slice's offset is a position in exactly the buffer on
        screen. (:attr:`~celpix.pipeline.pathway.PathwayConfig.
        positions_are_slice_offsets` is not asked: it answers for a slice
        *beside* a decompressed one, and a new slice is always *under* what is
        on screen.) ``None`` when nothing qualifies; callers add any
        gesture-specific guard (a selection, a found structure).
        """
        entry, doc = self._workspace.current, self._doc
        if entry is None or doc is None:
            return None
        if entry.kind is EntryKind.SLICE:
            return (entry, doc) if self._can_hold_slices(entry) else None
        return (entry, doc)

    def _new_slice_current(self) -> None:
        """File ▸ New Slice… on the current entry's file."""
        entry = self._workspace.current
        if entry is not None:
            self._new_slice_for(entry)

    def _new_slice_for(self, entry: Entry) -> None:
        """Open the slice dialog for ``entry`` — a file, a palette, or a slice,
        which the new one is then nested in."""
        # Prefill from the view only when the dialog targets the file on screen;
        # a right-clicked non-current file has no live viewport to read.
        offset = (
            self._slice_prefill_offset()
            if entry is self._workspace.current and self._doc is not None
            else 0
        )
        self._create_slice_via_dialog(entry, offset=offset)

    def _new_slice_from_view_for(self, entry: Entry) -> None:
        """The files dock's New Slice from View - only the on-screen entry has
        a viewport, so anything else (a stale menu) is ignored."""
        if entry is self._workspace.current:
            self._new_slice_from_view()

    def _new_slice_from_view(self) -> None:
        """File ▸ New Slice from View: the dialog prefilled to cover the
        current viewport - the structure in view when the compression preview
        found one (its true extent beats the window's), else the visible
        window's bytes - plus the compression combo.

        Refused where there is no viewport to read, as well as on the action:
        the files dock builds its own row for this, so a guard on the gesture is
        what covers both. On a tilemap the prefill was not merely unhelpful, it
        was measured in another file's units - ``pixel_data`` there is the
        *bound* entry's tile bytes, so the length came off the tile bank's
        geometry and was then written into a slice of the map.
        """
        if not self._can(Capability.NAVIGATION):
            return
        src = self._slice_source()
        if src is None:
            return
        entry, doc = src
        length = None
        if self._structure_extent is not None:
            start, consumed = self._structure_extent
            if start == self._byte_position():
                length = consumed
        if length is None:
            # The visible window's byte extent, clamped to the data so a
            # partially blank last page doesn't slice past the end.
            page = self._columns.value() * self._view_rows() * doc.bytes_per_tile
            length = min(page, len(doc.pixel_data) - self._byte_position())
        self._create_slice_via_dialog(
            entry,
            offset=self._slice_prefill_offset(),
            length=length,
            compression_id=self._compression_id(),
        )

    def _new_slice_from_selection_for(self, entry: Entry) -> None:
        """The files dock's New Slice from Selection - the selection lives on
        the on-screen entry, so anything else (a stale menu) is ignored."""
        if entry is self._workspace.current:
            self._new_slice_from_selection()

    def _new_slice_from_selection(self) -> None:
        """File ▸ New Slice from Selection: the selected tiles' byte range.

        Raw prefill (no decompressor): the selection is a run of *decoded
        raw* tiles, so unlike from-view the compression preview combo does
        not describe it.

        A slice is one offset+length region, so the selection has to be a
        continuous run of tiles. A rectangle narrower than the view isn't -
        its rows sit apart in the file - and is refused rather than quietly
        widened to the enclosing span, which would take in tiles either side
        of every row that the user never selected.
        """
        src = self._slice_source()
        if src is None:
            return
        entry, doc = src
        tiles = self._selection_tiles()
        if tiles and sorted(tiles) != list(range(min(tiles), max(tiles) + 1)):
            self._alert(
                "New Slice from Selection needs a continuous run of tiles.",
                title="celPix - new slice",
                detail=(
                    "The rectangle's rows are not contiguous in the file. Select "
                    "the tiles as one run (Selection: Linear), or widen the "
                    "rectangle to the full view width."
                ),
            )
            return
        rng = self._selection_byte_range()
        if rng is None:
            return
        # Same tile→byte mapping as the hex highlight, but the trailing (possibly
        # partial) tile is clamped to the bytes that exist - a slice can't run
        # past end-of-data.
        start, length = rng
        end = min(len(doc.pixel_data), start + length)
        if end <= start:
            return
        self._create_slice_via_dialog(
            entry,
            offset=self._slice_prefill_offset(start),
            length=end - start,
        )

    def _new_composite(self) -> None:
        """File ▸ New Composite View…: assemble a tile source out of open entries.

        The tile window a converted console tilemap actually indexes: one flat
        index space filled from several files, which is what the hardware had in
        VRAM and what no single file holds (``docs/design/composite-entry.md``).

        Created through an ``AddEntryCommand`` like every other interactive add,
        so it can be undone — and so the *same object* comes back on redo, which
        every piece of every other composite and every tilemap binding names.
        """
        self._new_composite_from([])

    def _new_composite_from(self, sources: list[Entry]) -> None:
        """New Composite View with ``sources`` already listed, one whole run each.

        The Files list's *New Composite View* on a row or a selection. A separate
        method rather than a parameter on :meth:`_new_composite`, whose menu
        action's ``triggered(bool)`` would otherwise land in it. The rows are
        only a starting list — the dialog still offers every source and the user
        can reorder or cut it before anything is created.
        """
        entry = new_composite("")
        entry.pieces = tuple(
            self._measure_composite_piece(entry, CompositePiece(source))
            for source in sources
        )
        # Starts on whatever its first source is read as, the same seed its
        # format would take — a run of colour words shown as swatches is most
        # likely the start of a colour table.
        palette = self._composite_is_palette(entry)
        params = CompositeDialog.get_composite(
            self,
            entry=entry,
            candidates=list(self._workspace.entries),
            measure_unit=lambda as_palette: self._composite_units(entry, as_palette),
            measure=lambda piece: self._measure_composite_piece(entry, piece),
            name=self._unused_composite_name(),
            pieces=entry.pieces,
            palette=palette,
        )
        if params is None:
            return
        entry.name = params.name
        entry.pieces = params.pieces
        # Settled before the add, so the row is filed in the right section on
        # its first appearance rather than moved there on its first activation.
        preset = composite_format_for(entry, self._registry, palette=params.palette)
        if preset:
            entry.session = replace(self._seed_session(entry), pixel_preset_id=preset)
        self._push_command(
            AddEntryCommand(self, entry, f'new composite "{entry.name}"')
        )

    def _measure_composite_piece(
        self, composite: Entry, piece: CompositePiece
    ) -> CompositePiece:
        """``piece`` carrying the size it will assemble to — for a run just added.

        A new piece's ``measured`` is 0 until the composite is first assembled,
        which is after the dialog closes; the dialog would list the run, and
        every position after it, as if it held nothing. Assembling it alone as a
        one-piece composite answers with the very rules the view applies —
        resolved bytes, rounded up to a whole tile — rather than a second copy of
        them. Settled first for the reason :meth:`_composite_layout` settles.

        The tile it rounds to is ``composite``'s format where it has one, and
        otherwise the seed the piece itself would give; a new composite has no
        format until its first source decides it, and the real assembly
        re-measures on load either way.
        """
        if piece.is_pad or piece.entry is None:
            return piece
        self._settle_region(piece.entry)
        session = composite.session
        preset = session.pixel_preset_id if session is not None else ""
        probe = new_composite("", (piece,))
        try:
            layout = composite_layout(probe, self._registry, self._workspace, preset)
        except PipelineError:
            return piece
        return layout.pieces[0]

    def _unused_composite_name(self) -> str:
        """``Composite``, ``Composite 2``, … — the first the list has not got.

        A composite has no file to be named after, so it needs one made up; two
        rows called the same thing in the same section is the confusion this
        avoids. Only a starting point — the user renames it in the dialog or in
        the list, and nothing keeps the numbering true afterwards.
        """
        taken = {e.name for e in self._workspace.entries}
        if "Composite" not in taken:
            return "Composite"
        return next(
            f"Composite {n}"
            for n in range(2, len(taken) + 3)
            if f"Composite {n}" not in taken
        )

    def _composite_preset(self, entry: Entry) -> str:
        """The pixel format ``entry`` is read at — its session's, or the seed a
        composite with no session yet is about to be given."""
        session = entry.session
        if session is not None:
            return session.pixel_preset_id
        return composite_preset_id(entry, self._registry)

    def _composite_is_palette(self, entry: Entry) -> bool:
        """Whether ``entry`` reads as a colour table — the dialog's Palette."""
        return is_swatch_preset(self._composite_preset(entry), self._registry)

    def _composite_units(self, entry: Entry, palette: bool) -> tuple[int, str]:
        """``(bytes per unit, unit name)`` for ``entry`` read as pixels or swatches.

        The dialog states each run's position twice — as a byte and as a unit —
        and this is what converts between them. The entry's **own** format, not
        its first source's: those differ exactly where the feature is most used,
        since a tile window assembled from 4bpp banks is routinely read at 2bpp,
        and taking the source's would print a tile column off by a factor of two
        against the view the user is checking it against. Asked of whichever
        format the dialog's Pixel / Palette would switch it to, so flipping that
        re-counts the column at once (:func:`composite_format_for`).

        The unit is a tile, except through the **palette-swatch** codec: there
        one read unit is one colour word, the swatch it draws is what the user is
        transcribing, and calling that a tile would name the wrong thing in the
        one place the number is being checked against a colour table
        (``docs/design/palette-editing.md``). A **packed** colour format is the
        exception to the exception — a Game Boy palette byte is four shades in
        one unit, which the swatch codec draws as one tile several swatches wide —
        so the unit there really is a tile.

        A format this build hasn't got costs the reader the byte count (0) rather
        than the dialog.
        """
        preset = composite_format_for(
            entry, self._registry, palette=palette
        ) or self._composite_preset(entry)
        try:
            tile_bytes = pipeline.pixel_tile_bytes(preset, self._registry)
        except (PipelineError, KeyError):
            tile_bytes = 0
        if not palette:
            return tile_bytes, "Tile"
        # The colour format the swatches are read in: the entry's own, or the
        # toolbar's for one with no session yet, which is what it will be seeded.
        session = entry.session
        colour = (
            session.palette_view_preset_id
            if session is not None
            else self._palette_view_preset_id()
        )
        try:
            if pipeline.palette_entries_per_unit(colour, self._registry) != 1:
                return tile_bytes, "Tile"
        except (PipelineError, KeyError):
            return tile_bytes, "Tile"
        return tile_bytes, "Color"

    def _edit_composite(self, entry: Entry) -> None:
        """The files dock's Edit… on a composite — re-list its pieces in place.

        No unsaved-changes warning, unlike :meth:`_edit_slice`: a composite holds
        no edits of its own to discard. Anything painted through it is already in
        the pieces, and stays there however the list is rearranged.
        """
        if entry.kind is not EntryKind.COMPOSITE:
            return
        palette = self._composite_is_palette(entry)
        before = CompositeParams(
            entry.name, entry.pieces, palette, self._composite_preset(entry)
        )
        params = CompositeDialog.get_composite(
            self,
            entry=entry,
            candidates=list(self._workspace.entries),
            measure_unit=lambda as_palette: self._composite_units(entry, as_palette),
            measure=lambda piece: self._measure_composite_piece(entry, piece),
            name=entry.name,
            pieces=entry.pieces,
            palette=palette,
            title="Edit Composite View",
        )
        if params is None or params == before:
            return  # cancelled, or OK'd unchanged - nothing to undo
        # The exact format on both sides, so an undo of Pixel -> Palette puts
        # back the depth the user had rather than re-guessing one.
        params.pixel_preset_id = (
            composite_format_for(entry, self._registry, palette=params.palette)
            or before.pixel_preset_id
        )
        self._push_command(
            CompositeEditCommand(self, entry, before=before, after=params)
        )

    def _apply_composite_params(self, entry: Entry, params: CompositeParams) -> None:
        """Re-list a composite's pieces and re-assemble — the application path
        for composite edits and their undos.

        The re-assembly is a plain re-read of **this** entry
        (:meth:`~...bindings.BindingsMixin._rebuild_composite`), and the maps
        drawing through the composite are re-resolved after it: their tiles came
        out of the join that has just changed shape, so leaving them would have
        them drawing the old one indefinitely.
        """
        entry.name = params.name
        entry.pieces = params.pieces
        self._rebuild_composite(entry, params.pixel_preset_id)
        self._reresolve_bound_art(self._maps_drawing_from([entry]))
        self._files_panel.refresh_entry(entry)

    def _edit_slice(self, entry: Entry) -> None:
        """The files dock's Edit… - rewrite a slice's coordinates in place.

        The same dialog as New Slice, prefilled with the current values; on OK
        the entry is re-pointed and its cached document dropped, so the region
        is re-read (immediately when it is on screen, else on activation).
        """
        if entry.kind is not EntryKind.SLICE:
            return
        codec_id = self._slice_codec_id(entry)
        units = (
            self._slice_units(entry, codec_id)
            if entry.compression_id != NO_COMPRESSION
            else None
        )
        # The slices nested in this one re-read with it: their offsets count in
        # its decoded bytes, which the edit is about to change.
        nested = self._workspace.descendants_of(entry)
        dirty = [e for e in (entry, *nested) if e.pixel_dirty or e.palette_dirty]
        if dirty:
            whose = (
                "its"
                if dirty == [entry]
                else f"the unsaved changes of {', '.join(e.name for e in dirty)} -"
            )
            if not self._confirm_reread_discard(
                "celPix - edit slice", f"Editing {entry.name}", "it", whose=whose
            ):
                return
        # Bounded as New Slice bounds it (:meth:`_create_slice_via_dialog`): by
        # the parent's decoded bytes rather than the files' size where the
        # parent is a slice, or a file that decompresses whole.
        parent = self._workspace.parent_of(entry)
        extent = None
        if parent is not None and (
            parent.kind is EntryKind.SLICE or parent.compression_id != NO_COMPRESSION
        ):
            extent = self._slice_buffer_length(parent)
            if extent is None:
                return
        params = SliceDialog.get_slice(
            self,
            self._registry,
            extent=extent,
            source=parent.name if extent is not None and parent is not None else "",
            paths=entry.paths,  # a slice carries its parent's whole file list
            offset=entry.slice_offset,
            length=entry.slice_length,
            compression_id=entry.compression_id,
            reshape_id=entry.reshape_id,
            slot_fill=entry.slot_fill,
            match_parent=entry.match_parent,
            name=entry.name,
            title="Edit Slice",
            # Carried in and back out untouched: an edit re-points a live entry's
            # coordinates, and re-reading it as another kind of thing would take
            # its binding and its section in the Files list with it.
            content_kind=entry.content_kind,
            inputs_hint=lambda codec: self._inputs_hint(entry, codec),
            edit_inputs=lambda dialog, codec: self._edit_slice_inputs(
                dialog, entry, codec
            ),
            units=units,
            codec_id=codec_id,
        )
        if params is None:
            return
        # The size is not a coordinate: it is an edit to the parent's bytes, and
        # goes on the stack as one after the re-point it may depend on (a Length
        # widened to give the bigger stream room). One Ctrl+Z takes both back.
        resize, params = params.units, replace(params, units=None)
        before = SliceParams(
            entry.name,
            entry.slice_offset,
            entry.slice_length,
            entry.compression_id,
            entry.reshape_id,
            entry.content_kind,
            entry.slot_fill,
            match_parent=entry.match_parent,
        )
        moved = params != before
        if not moved and resize is None:
            return  # OK'd unchanged - nothing happened, nothing to undo
        both = moved and resize is not None
        if both:
            self._undo_stack.beginMacro(f"edit and resize {entry.name}")
        try:
            if moved:
                self._push_command(
                    SliceEditCommand(self, entry, before=before, after=params)
                )
            if resize is not None:
                self._resize_slice(entry, resize, codec_id)
        finally:
            if both:
                self._undo_stack.endMacro()

    # -- resizing a compressed slice -----------------------------------------
    def _slice_codec_id(self, entry: Entry) -> str:
        """The format a slice's payload is measured in — its live reading when
        it has one, since the toolbar may have moved past the stored session."""
        doc = entry.doc
        if doc is not None and entry.content_kind is ContentKind.PIXELS:
            return doc.pixel_config.interpret_preset_id
        return self._entry_codec_id(entry)

    def _slice_config(self, entry: Entry, codec_id: str) -> PathwayConfig:
        """The pathway ``entry``'s own bytes are read through, off the parent's
        live buffer rather than the file on disk."""
        if entry.content_kind is ContentKind.TILEMAP:
            return tilemap_config_for(entry, codec_id, self._registry, self._workspace)
        return pixel_config_for(entry, codec_id, self._registry, self._workspace)

    def _slice_units(self, entry: Entry, codec_id: str) -> int | None:
        """How many tiles or cells a compressed slice unpacks to, or ``None``
        where that cannot be read — which leaves the Size row out rather than
        stating a count nobody measured."""
        if not codec_id or entry.content_kind not in (
            ContentKind.PIXELS,
            ContentKind.TILEMAP,
        ):
            return None
        parent = self._workspace.parent_of(entry)
        self._settle_region(parent)
        return self._region_units(
            lambda: self._slice_config(entry, codec_id), entry.content_kind, codec_id
        )

    def _region_units(
        self, config: Callable[[], PathwayConfig], kind: ContentKind, codec_id: str
    ) -> int | None:
        """How many ``kind`` units the region read through ``config()`` holds
        under ``codec_id``, or ``None`` where it cannot be read.

        ``config`` is a builder rather than a config so a pipeline error raised
        while *building* the pathway counts as unreadable too, the same as one
        raised reading through it.
        """
        try:
            data, _ = pipeline.read_region(config(), self._registry)
            return pipeline.blank_units(kind, codec_id, len(data), self._registry)
        except (PipelineError, OSError):
            return None

    def _resize_slice(self, entry: Entry, units: int, codec_id: str) -> bool:
        """Re-pack ``entry`` to unpack to ``units``, as an edit to its parent.

        A slice has no file position of its own to write at — its bytes reach
        the disk through its parent's write, which is what runs the container's
        repairs (a ROM's checksums) — so the resize is not a write at all. The
        re-packed slot (:func:`~celpix.pipeline.pipeline.resized_slot_bytes`)
        is spliced into the parent's buffer as one ordinary byte edit: undoable,
        marking the file unsaved, and dropping the slice's cache so it re-reads
        at its new size. A **nested** slice's parent is a slice, so the splice
        lands in that slice's decoded bytes and is owed up its chain like any
        edit to it. ``through`` is the slice, so an undo comes back to the
        view the resize was asked for in (``docs/design/slices-and-parents.md``
        §5). False, already reported, when it did not happen.
        """
        parent = self._workspace.parent_of(entry)
        if parent is None:
            where = (
                "the slice it was cut from"
                if entry.parent_kind is EntryKind.SLICE
                else Path(entry.path).name
            )
            self._alert(
                f"{entry.name} is a region of {where}, which is not open. "
                "Cannot resize.",
                title="celPix - resize",
            )
            return False
        if parent.doc is None and not self._load_entry(parent):
            return False
        # Every other slice's unsaved edits into the buffer first, so the splice
        # below lands on top of them rather than a later fold landing on it.
        self._settle_region(parent)
        try:
            slot = pipeline.resized_slot_bytes(
                self._slice_config(entry, codec_id),
                kind=entry.content_kind,
                codec_id=codec_id,
                units=units,
                reg=self._registry,
            )
        except PipelineError as exc:
            self._report(exc)
            return False
        except (OSError, ValueError) as exc:
            self._alert(f"Cannot resize {entry.name}: {exc}", title="celPix - resize")
            return False
        assert parent.doc is not None
        if parent.doc.is_tilemap:
            self._alert(
                f"{entry.name} lies inside the tilemap {parent.name}, which is "
                "written from its cells. Cannot resize.",
                title="celPix - resize",
            )
            return False
        # A slice parent's buffer counts from 0; a file's from its anchor base —
        # where its container started reading, or 0 where a reshape or a
        # decompressor makes slice offsets positions in the reordered buffer.
        base = 0 if parent.kind is EntryKind.SLICE else int(parent.doc.anchor_base)
        start = entry.slice_offset - base
        if start < 0 or start + len(slot) > len(parent.doc.pixel_data):
            self._alert(
                f"{entry.name} lies outside {parent.name}'s region. Cannot resize.",
                title="celPix - resize",
            )
            return False
        noun = "cells" if entry.content_kind is ContentKind.TILEMAP else "tiles"
        self._push_pixel_regions(
            [(start, slot)],
            parent.doc.pixel_data,
            parent,
            f"resize {entry.name} to {units:,} {noun}",
            through=entry,
        )
        # The edit dropped every document under the parent, so the slices cut
        # from this one re-read at their next showing; a matched one is also
        # re-measured now, so its row says what it will read.
        self._refit_to_parents(self._workspace.descendants_of(entry))
        self.statusBar().showMessage(
            f"Resized {entry.name} to {units:,} {noun}; write {parent.name} to keep it"
        )
        return True

    def _inputs_hint(self, entry: Entry, codec: str) -> str:
        """The Slice dialog's one line about a codec's inputs: what ``entry``
        binds for it — the file's preview bindings a new slice will copy, or the
        slice's own — and ``""`` for a codec that declares none."""
        specs = input_specs(self._registry, Stage.COMPRESSION, codec)
        if not specs:
            return ""
        bound = entry.inputs.get(codec, {})
        parts = []
        for spec in specs:
            binding = bound.get(spec.key)
            if spec.kind is InputKind.FLAG:
                # Always delivered: unbound is the default, so say which.
                on = binding if isinstance(binding, bool) else bool(spec.default)
                parts.append(f"{spec.label}: {'yes' if on else 'no'}")
            elif spec.kind is InputKind.CHOICE:
                # Always delivered too; a key the plugin lacks is shown as it
                # is, since that is what the notice will name.
                key = binding if isinstance(binding, str) else spec.choice_default
                parts.append(f"{spec.label}: {spec.option_label(key) or key}")
            elif binding is None and spec.default is not None and not spec.required:
                # What the codec will be handed, not "unbound": an optional
                # integer with a default is delivered as that default.
                parts.append(f"{spec.label}: {spec.default} (default)")
            elif binding is None:
                parts.append(
                    f"{spec.label}: unbound" + ("" if spec.required else " (optional)")
                )
            elif isinstance(binding, RegionBinding):
                parts.append(
                    f"{spec.label}: {format_hex(binding.offset)}, {binding.length} B"
                )
            elif isinstance(binding, IntegerFromBytes):
                parts.append(f"{spec.label}: read at {format_hex(binding.offset)}")
            else:
                parts.append(f"{spec.label}: {binding}")
        return "; ".join(parts)

    def _apply_slice_params(self, entry: Entry, params: SliceParams) -> None:
        """Re-point a slice's coordinates and re-read the region - the
        application path for slice edits and their undos; works for
        non-current entries (their reload waits until activation)."""
        entry.name = params.name
        entry.slice_offset = params.offset
        entry.slice_length = params.length
        entry.compression_id = params.compression_id
        entry.reshape_id = params.reshape_id
        entry.slot_fill = params.slot_fill
        entry.match_parent = params.match_parent
        # How the region is *read* and *laid out* belongs to the entry, not to
        # the coordinates: re-pointing changes which bytes arrive, not the
        # format or arrangement they arrive in. The session snapshot only
        # tracks the live toolbar at explicit capture points, and the view lives
        # on the document about to be dropped - so both are taken here, or the
        # re-read comes back on the entry's stale format and the codec's default
        # geometry, silently dropping a wide-bitmap width (which _load_entry
        # needs *before* the load, to re-cut the tile size) along with the
        # columns, zoom and 2D walk built around it. Undo runs through here too,
        # so it restores the setup its own re-read is about to discard.
        if entry is self._workspace.current:
            self._capture_session()
        # Pixel edits die with the old region; the palette does not - it isn't
        # tied to the slice's coordinates, so drop_document carries it across.
        # Nothing is unsaved once the edits themselves are gone. The slices
        # nested in this one go with it: they are windows into the bytes that
        # just moved.
        moved = [entry, *self._workspace.descendants_of(entry)]
        self._reread_entries(moved)
        # And whatever reads its inputs out of them — a stream whose table is
        # this slice would go on decoding, and re-packing, with the old table.
        self._reread_input_dependents(moved)

    # -- the slice dialog ----------------------------------------------------
    def _create_slice_via_dialog(
        self,
        parent: Entry,
        *,
        offset: int = 0,
        length: int | None = None,
        compression_id: str = NO_COMPRESSION,
    ) -> None:
        if not self._can_hold_slices(parent):
            self._alert(
                f"{parent.name} is a tilemap slice. A tilemap is written from its "
                "cells, not its bytes, so a slice of it could not save edits. Cut "
                f"the slice from the entry {parent.name} was cut from instead.",
                title="celPix - new slice",
            )
            return
        # A slice parent's decoded bytes bound the new slice's offsets, in place
        # of the files' size: a nested slice counts from byte 0 of them. So do a
        # file's that decompresses whole, whose slices count in its unpacked
        # stream — longer than the file, which would bound them short.
        extent = None
        if parent.kind is EntryKind.SLICE or parent.compression_id != NO_COMPRESSION:
            extent = self._slice_buffer_length(parent)
            if extent is None:
                return  # reported: a parent that cannot be read has no bytes
        # The parent's whole file list, both to bound the dialog's offsets (a
        # region spread over several chips is addressed as the concatenation)
        # and so the slice inherits the list its offsets are relative to.
        #
        # The **Content** row is offered wherever the parent's own answer could be
        # wrong, which is both graphic readings of a file or of a slice of one. A
        # palette file's slices are runs of its colours, at any depth, so there
        # is nothing to pick.
        params = SliceDialog.get_slice(
            self,
            self._registry,
            paths=parent.paths,
            offset=offset,
            length=length,
            compression_id=compression_id,
            content_kind=parent.content_kind,
            choose_content=parent.kind in (EntryKind.FILE, EntryKind.SLICE)
            and anchor_kind(parent) is EntryKind.FILE
            and parent.content_kind in (ContentKind.PIXELS, ContentKind.TILEMAP),
            extent=extent,
            source=parent.name if extent is not None else "",
            inputs_hint=lambda codec: self._inputs_hint(parent, codec),
            # A new slice has nothing to bind *on* yet, so the badge edits the
            # parent file's bindings for the codec — which ``slice_of`` hands
            # straight down to the slice this dialog is about to create.
            edit_inputs=lambda dialog, codec: self._edit_slice_inputs(
                dialog, parent, codec
            ),
        )
        if params is None:
            return
        entry = slice_of(
            parent,
            params.name,
            params.offset,
            params.length,
            params.compression_id,
            reshape_id=params.reshape_id,
        )
        # ``slice_of`` carried the parent's kind down; the dialog's answer is the
        # user's word over it, and is the parent's own value when unchanged.
        entry.content_kind = params.content_kind
        entry.slot_fill = params.slot_fill
        entry.match_parent = params.match_parent
        self._seed_slice_from_parent(entry)
        self._push_command(AddEntryCommand(self, entry, f'new slice "{entry.name}"'))

    def _slice_buffer_length(self, entry: Entry) -> int | None:
        """How many bytes an entry's own decoded buffer holds — what a slice
        cut from a parent slice, or from a file that decompresses whole, is
        bounded by — or None, reported, when it cannot be read.

        Its live document's bytes when it has one (a map's are its cells'
        buffer, :func:`~celpix.project.workspace.own_bytes`), else the region
        read fresh through its chain, settled first so a pending edit above it
        counts.
        """
        held = own_bytes(entry)
        if held is not None:
            return len(held)
        preset = (
            entry.session.pixel_preset_id
            if entry.session is not None
            else self._pixel_preset_id()
        )
        try:
            data, _ctx = pipeline.read_region(
                self._pixel_config(entry, preset), self._registry
            )
        except PipelineError as exc:
            self._report(exc)
            return None
        except OSError as exc:
            self._alert(f"Cannot read {entry.name}: {exc}", title="celPix - slice")
            return None
        return len(data)
