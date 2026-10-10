"""Palette bytes read from a position in the entry's own data.

Offset mode: the palette a ROM actually ships, sitting somewhere in the same file
as the graphics rather than in a ``.pal`` of its own. What that costs, and what
this module is, is the address arithmetic behind one number - the offset in the
box - and it is more than it looks.

**Whose coordinates the number is in.** A slice's palette offsets are its
*parent's*, deliberately unbounded by the slice, because a graphics block's
palette usually lives elsewhere in the ROM
(``docs/design/palette-editing.md`` §2). :meth:`~PaletteOffsetMixin.
_palette_offset_owner` is that entry, and every other question here is asked of
it: which files the offset addresses, how far it may run, and which address space
it indexes.

**Which address space that is.** A container that merely skips a header leaves
every byte where it was, so the offset names a file byte and the palette pathway
keeps its write half. A *permuting* container or an active reshape makes the view
buffer a different address space from the file, and then the buffer is the only
place the offset means anything: the read window is cut from it and the palette
pathway comes back write-off, because a length-bounded ``FileRef`` cannot say
where a permuted splice belongs. Colour edits still persist - through the buffer
owner's **own** data pathway (:meth:`~PaletteOffsetMixin.
_offset_palette_pixel_owner`), whose Write carries the whole region back through
``unshape`` and the container: a graphic's pixel bytes, or a map's cells.

**How far it may run.** Every window is floored to whole entries, because the
colour codecs reject a partial trailing one, and capped at a full palette. The
step buttons clamp against the same end the read window is sized from, so holding
an arrow at the edge stops rather than raising the past-EOF alert a typed offset
would.

What is not here: the other five load modes and the commit every one of them ends
in (:mod:`~celpix.ui.main_window.palette_source`), the dock widgets this drives
(:mod:`~celpix.ui.main_window.palette_dock`), and what a colour edit does once it
has an owner (:mod:`~celpix.ui.main_window.color_editing`). The offset field and
its step buttons are shared with **Entry** mode, which asks the same question of
another entry's buffer (:mod:`~celpix.ui.main_window.palette_entry`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from celpix.core.address import format_hex, parse_hex
from celpix.core.document import Document
from celpix.core.errors import PipelineError
from celpix.core.palette import FULL_PALETTE_COUNT
from celpix.pipeline import pipeline
from celpix.pipeline.pathway import PathwayConfig
from celpix.plugins.base import FileRef
from celpix.project import documents
from celpix.project.workspace import (
    Entry,
    EntryKind,
    PaletteMode,
    entry_view_bytes,
)


class PaletteOffsetMixin:
    """Where an Offset-mode palette is read from, and how far it may reach.

    A slice of :class:`~celpix.ui.main_window.window.MainWindow`, not a
    standalone object: it reads and writes the window's own widgets and its
    single live ``_doc``. See the module docstring for what it owns, and the
    package docstring for why these are mixins.
    """

    def _offset_palette_refusal(self, entry: Entry | None) -> str | None:
        """Why ``entry`` cannot hold an Offset palette, or None if it can.

        A **composite of pads only** is the one refusal. An Offset palette
        addresses a byte of a file in its owner's coordinates; a composite
        borrows its first file-backed piece's
        (:func:`~celpix.project.documents.palette_offset_owner`), and one with
        no such piece has no file at all. Said here, once, so the mode picker,
        the selection action and the load itself agree — left to the load, the
        refusal arrived as "not enough data at that offset", which is a wrong
        diagnosis of the right answer.
        """
        if (
            entry is not None
            and entry.kind is EntryKind.COMPOSITE
            and self._palette_offset_owner(entry) is None
        ):
            return (
                "This composite view has no file-backed run to read a palette "
                "from. Add one, or use File, Custom or Emulator mode."
            )
        return None

    def _initial_palette_offset(self) -> int:
        """Where Offset mode starts: the selected tile, else the window top-left.

        In the coordinates a palette offset is written in - the owner's, which for
        a slice is the file its chain ends at
        (:func:`~celpix.project.documents.palette_offset_owner`), because an
        Offset palette deliberately reaches outside the slice's own window. So
        this is not the number the offset box shows while a slice is on screen;
        that one counts from the slice. A view with no file coordinates - a
        decompressed one, a nested slice - starts at the bare view position
        (:meth:`~...navigation.NavigationMixin._tile_anchor_offset`).
        """
        assert self._doc is not None
        # No stamp here, so no on-screen snap - this only reads a byte offset.
        return self._tile_anchor_offset(self._anchor_tile())

    def _palette_offset_text(self) -> str:
        """The palette offset field's text provider; safe with no document.

        Shared with Entry mode, which uses the same field for the same kind of
        number — a byte position in a buffer — differing only in whose
        (``docs/design/palette-editing.md``).
        """
        if self._doc is None or not self._palette_mode_has_offset():
            return ""
        return self._format_palette_offset(
            self._doc.palette_config.source.offset, prefix=False
        )

    def _format_palette_offset(self, byte_off: int, *, prefix: bool = True) -> str:
        """``byte_off`` as the palette offset field and the status line show it.

        The **address format** in Offset mode: the number is a position in this
        entry's own file, so it reads in whatever bank layout the navbar is set to,
        exactly as the tile offset beside it does.

        Plain hex in **Entry** mode, because there the number indexes another
        entry's *resolved* buffer from 0 (``docs/design/palette-editing.md``) — a
        composite's join has no CPU address at all, and rendering offset 0 of one
        as ``80:8000`` names a bank nothing here is in.
        """
        if self._palette_mode is PaletteMode.ENTRY:
            return format_hex(byte_off, prefix=prefix)
        return self._format_offset(byte_off, prefix=prefix)

    def _parse_palette_offset(self, text: str) -> int | None:
        """The palette offset field's parser — :meth:`_format_palette_offset`'s
        inverse, and mode-aware for the same reason: a bank layout would read
        ``20`` typed in Entry mode as a banked address rather than as byte 0x20."""
        if self._palette_mode is PaletteMode.ENTRY:
            return parse_hex(text)
        return self._parse_address(text)

    def _palette_mode_has_offset(self) -> bool:
        """Whether the dock's offset field means something in the live mode."""
        return self._palette_mode in (PaletteMode.OFFSET, PaletteMode.ENTRY)

    def _on_palette_offset_committed(self, byte_off: int) -> None:
        # On failure the commit's own unconditional refresh reverts the text.
        if self._doc is None:
            return
        if self._palette_mode is PaletteMode.ENTRY:
            source = self._entry_palette_target()
            if source is not None:
                self._load_palette_from_entry(source, byte_off)
            return
        self._load_palette_at_offset(byte_off)

    def _step_palette_offset(
        self, delta: int, *, unit: Literal["color", "tile", "palette"] = "tile"
    ) -> None:
        """Nudge the palette window by ``delta`` units of ``unit``.

        The ◄/► buttons: one tile of the current pixel format is the step, so
        walking the palette window a tile at a time hunts for the colors a few
        tiles off the graphics. Ctrl on the button steps one colour entry, for a
        palette that is found but misaligned - one whose rows start a colour or
        two off a tile boundary; Shift steps a full palette, so the panel pages
        on to swatches it has not shown yet. Clamped so a step
        never runs before byte 0 or past the last position a full palette entry
        still fits - holding an arrow at the edge simply stops, without the
        past-EOF alert a typed offset would raise. Reuses each mode's own load,
        so a step is an ordinary undoable palette change either way.
        """
        entry = self._workspace.current
        if self._doc is None or entry is None or not self._palette_mode_has_offset():
            return
        entry_size = pipeline.palette_entry_size(
            self._palette_preset_id(), self._registry
        )
        step = {
            "color": entry_size,
            "tile": self._doc.bytes_per_tile,
            "palette": FULL_PALETTE_COUNT * entry_size,
        }[unit]
        try:
            end = self._palette_offset_end(entry)
        except OSError as exc:
            self._alert(
                f"Cannot read the palette source: {exc}", title="celPix - palette"
            )
            return
        if end is None:
            return
        last = end - entry_size  # last offset a whole entry still fits at
        if last < 0:
            return
        # source.offset is already in the coordinates the load expects, so a step
        # is just arithmetic on it.
        current = self._doc.palette_config.source.offset
        target = min(max(0, current + delta * step), last)
        if target == current:
            return
        if self._palette_mode is PaletteMode.ENTRY:
            source = self._entry_palette_target()
            if source is not None:
                self._load_palette_from_entry(source, target)
            return
        self._load_palette_at_offset(target)

    def _palette_offset_end(self, entry: Entry) -> int | None:
        """One past the last byte the palette window may reach, per mode.

        The owning file's buffer for Offset mode, the source entry's resolved
        data for Entry mode, and ``None`` where there is no source to measure —
        which is what makes a step over a closed one stop rather than raise.
        """
        if self._palette_mode is not PaletteMode.ENTRY:
            return self._offset_palette_space(entry)[1]
        source = self._entry_palette_target(entry)
        if source is None:
            return None
        # The format the source's own buffer is assembled at, which is not this
        # entry's: a never-opened composite is joined at its seed and a length
        # measured at anything else would clamp the steps to the wrong end
        # (:func:`~celpix.project.documents.source_view_preset`).
        preset = documents.source_view_preset(
            source, self._registry, self._pixel_preset_id()
        )
        self._settle_region(source)
        data, _base = entry_view_bytes(source, self._registry, preset, self._workspace)
        return len(data)

    def _palette_offset_owner(self, entry: Entry | None) -> Entry | None:
        """:func:`~celpix.project.documents.palette_offset_owner`."""
        return documents.palette_offset_owner(self._workspace, entry)

    def _offset_palette_files(self, entry: Entry) -> tuple[str, ...]:
        """:func:`~celpix.project.documents.offset_palette_files`."""
        return documents.offset_palette_files(self._workspace, entry)

    def _reordered_view(self, owner: Entry) -> tuple[bytes, int] | None:
        """:func:`~celpix.project.documents.reordered_view`, settling the owner
        first so a dirty parent's unsaved edits are what the palette reads."""
        return documents.reordered_view(
            self._registry,
            self._workspace,
            owner,
            self._settle_region,
            self._pixel_preset_id(),
        )

    def _offset_palette_space(
        self, entry: Entry
    ) -> tuple[tuple[bytes, int] | None, int]:
        """:func:`~celpix.project.documents.offset_palette_space` — the shared
        answer behind both the read window and the step buttons' clamp."""
        return documents.offset_palette_space(
            self._registry,
            self._workspace,
            entry,
            self._settle_region,
            self._pixel_preset_id(),
        )

    def _offset_palette_pixel_owner(self) -> Entry | None:
        """The FILE entry whose view buffer holds the on-screen Offset palette,
        when a color edit should land there — ``None`` for every other palette.

        A buffer-backed Offset palette (its source carries ``data``: the owner
        reorders bytes, so the window was cut from the owner's view buffer) has
        no file span of its own to write, but the *owner's* data pathway writes
        the whole region through ``unshape`` and the container already. So the
        edit is persisted by splicing into that buffer and dirtying the owner's
        data pathway — this answers *which entry that is*, loading its document
        if it isn't yet (a slice's parent may be closed), and ``None`` when the
        owner's own write path can't carry the edit anyway (no ``unshape``, no
        container write half), which keeps those palettes honestly view-only.

        A **tilemap** owner's buffer is its cells (:func:`~celpix.project.
        configs.entry_view_bytes`), never the art its ``pixel_config`` borrows
        from the bound bank, so it is the map's own pathway that has to be able
        to write — and its cells that have to be editable: a sprite object's
        frames are built from its records at load and a re-decoded record list
        would leave them behind.
        """
        if self._palette_mode is not PaletteMode.OFFSET or self._doc is None:
            return None
        if self._doc.palette_config.source.data is None:
            return None  # plain file window: the palette pathway writes itself
        owner = self._palette_offset_owner(self._workspace.current)
        if owner is None:
            return None
        if owner.doc is None and not self._load_entry(owner, quiet=True):
            return None
        doc = owner.doc
        assert doc is not None
        if doc.is_tilemap:
            cfg = doc.tilemap_config
            writable = cfg is not None and cfg.write_enabled and doc.cells_editable
        else:
            writable = doc.pixel_config.write_enabled
        return owner if writable else None

    def _sync_offset_palette_bytes(self, doc: Document, pixel_owner: Entry) -> None:
        """Splice ``doc``'s current palette bytes into ``pixel_owner``'s buffer.

        The persistence half of a buffer-backed Offset palette edit: re-encode
        the edited entries over the splice base (``pipeline.spliced_palette_bytes``)
        and land the window in the owner's view buffer at the offset it was read
        from — the exact bytes the owner's next Write will carry through
        ``unshape`` and the container. Runs on undo as well as redo (the splice
        is recomputed from the palette's state, not diffed), so the buffer
        always mirrors the palette on screen.
        """
        target = pixel_owner.doc
        if target is None:
            return
        # The buffer and anchor the window was read from, recomputed live rather
        # than trusted from the ref's data_base (the owner's document may have
        # been rebuilt since) and through the very function the read went
        # through: a map's cells under their own anchor, a graphic's bytes under
        # its — 0-based under a reshape or a decompressor, the recorded start
        # under a permuting container.
        own, base = entry_view_bytes(
            pixel_owner, self._registry, self._pixel_preset_id(), self._workspace
        )
        start = doc.palette_config.source.offset - base
        # Spliced into the window as the owner holds it now: tiles folded or
        # painted into it since the palette was read must not go back.
        now = own[start : start + len(doc.palette_base_bytes)] if start >= 0 else None
        try:
            window = pipeline.spliced_palette_bytes(doc, self._registry, now)
        except PipelineError as exc:
            self._report(exc)
            return
        if target.is_tilemap:
            self._land_in_cells(pixel_owner, start, window)
        else:
            target.replace_bytes(start, window)

    def _land_in_cells(self, owner: Entry, start: int, window: bytes) -> None:
        """Splice ``window`` into ``owner``'s map bytes at ``start``, and bring
        its cells after them.

        A map's Write encodes its **cells**, not the buffer they were read from
        (``pipeline._save_tilemap``), so bytes landed in the buffer alone would
        show in the hex dump and never reach the file. The spliced buffer is
        decoded back to cells — exact, as the codec's round trip is — and handed
        to the funnel every cell edit lands through, which re-chains anything
        drawing through the map. The revision stays where it is: the colour
        edit stamps the owner's data pathway itself, as it does a graphic's.
        """
        doc = owner.doc
        cfg = doc.tilemap_config if doc is not None else None
        if cfg is None:
            return
        data = doc.tilemap_data
        spliced = data[:start] + window + data[start + len(window) :]
        try:
            cells = pipeline.decode_cells(
                spliced, cfg.interpret_preset_id, self._registry, doc.tilemap_ctx
            )
        except PipelineError as exc:
            self._report(exc)
            return
        # Set first, so the re-encode splices its cells over these bytes and a
        # window reaching past the last cell keeps its tail.
        doc.tilemap_data = spliced
        self._set_cells(owner, cells, owner.pixel_revision)

    def _offset_palette_source(
        self,
        byte_off: int,
        preset_id: str | None = None,
        entry: Entry | None = None,
    ) -> tuple[FileRef | None, bool]:
        """:func:`~celpix.project.documents.offset_palette_source`.

        ``byte_off`` is in the **owning file entry's** view coordinates — the same
        numbers the offset box and the status bar show. ``preset_id`` and
        ``entry`` default to the combo and the live document; both are passed when
        a project restore loads an entry that is not (yet) the one on screen.
        """
        entry = entry if entry is not None else self._workspace.current
        assert entry is not None
        return documents.offset_palette_source(
            self._registry,
            self._workspace,
            entry,
            byte_off,
            preset_id or self._palette_preset_id(),
            self._settle_region,
            self._pixel_preset_id(),
        )

    def _file_palette_source(self, path: str, byte_off: int) -> FileRef | None:
        """A read window of palette colors at ``byte_off`` in the **named file**,
        read as plain bytes.

        The source builder for palette data that lives in a file of its own
        rather than at a position in an entry's coordinate space - an emulator
        save state's CGRAM, and a format change re-flooring such a window. Its
        siblings serve the other palette homes: :meth:`_offset_palette_source`
        for an Offset palette (which honours the owning entry's container and
        reshape),
        :meth:`~...palette_source.PaletteSourceMixin._file_palette_config` for a
        File-mode ``.pal`` (whole file, writable pathway).

        Floored to whole entries - the color codecs reject a partial trailing
        entry, so clamping at EOF alone is not enough. ``None`` when not even one
        entry fits, and capped at a full palette.
        """
        preset_id = self._palette_preset_id()
        avail = Path(path).stat().st_size - byte_off
        colors = min(
            FULL_PALETTE_COUNT,
            pipeline.palette_entry_capacity(avail, preset_id, self._registry),
        )
        if colors == 0:
            return None
        length = pipeline.palette_read_bytes(colors, preset_id, self._registry)
        return FileRef(path, offset=byte_off, length=length)

    def _load_palette_at_offset(self, byte_off: int) -> bool:
        """Load palette data at ``byte_off`` in the owning file's coordinates.

        ``byte_off`` is exactly the number the offset box and the status bar
        show, and it addresses the same bytes the view is built from: past a
        container's header skip, and through a permuting container or a reshape
        (:meth:`_offset_palette_source`). For a **slice** those are the
        *parent's* coordinates - deliberately unbounded by the slice, since a
        graphics block's palette usually lives elsewhere in the ROM.

        The read window is **writable wherever the offset still names a file
        byte**: color edits re-encode into exactly the bytes they were read from
        (the ``FileRef`` is length-bounded, so Write can only ever rewrite the
        palette's own region). That is the point of Offset mode - editing a
        palette where it actually lives in the ROM. The hazard is the user's to
        judge: the window is sized to whatever fits, so pointing it at bytes that
        aren't really a palette and then saving rewrites them
        (``docs/design/palette-editing.md``).
        """
        entry = self._workspace.current
        if self._doc is None or entry is None:
            return False
        refusal = self._offset_palette_refusal(entry)
        if refusal is not None:
            self._alert(refusal, title="celPix - palette")
            return False
        try:
            ref, writable = self._offset_palette_source(byte_off)
        except PipelineError as exc:
            self._report(exc)
            return False
        except OSError as exc:
            # The file the *offset* names, which is the owner's and not
            # necessarily the one this entry draws from
            # (:meth:`_offset_palette_files`).
            files = self._offset_palette_files(entry)
            self._alert(
                f"Cannot read {files[0] if files else entry.path}: {exc}",
                title="celPix - palette",
            )
            return False
        if ref is None:
            self._alert(
                "Not enough data at that offset for a palette entry.",
                title="celPix - palette",
            )
            return False
        # No compression on this pathway: an offset resolves against a *file*
        # entry's coordinates, and where that file decompresses whole (or
        # reorders its bytes) the window is cut from its decoded buffer
        # (``offset_palette_space``), so the bytes arrive already unpacked. The
        # palette itself is never run through a compressor: one sitting next to
        # compressed graphics is not itself compressed, and round-tripping it
        # through one would relocate and corrupt it.
        # Offset mode keeps pixel reloads from restoring the default palette.
        where = self._format_offset(byte_off)
        return self._load_and_commit_palette(
            PathwayConfig(
                source=ref,
                interpret_preset_id=self._palette_preset_id(),
                write_enabled=writable,
            ),
            mode=PaletteMode.OFFSET,
            label=f"load palette from {where}",
            status=lambda n: f"Loaded {n} colors from {where}",
        )

    def _load_palette_from_selection(self) -> None:
        """Palette ▸ Load from Selection: Offset mode at the selected tile.

        The tile's *anchor* offset, since that is what an Offset palette stores
        (:meth:`_initial_palette_offset`).
        """
        if self._doc is None or self._selected_tile is None:
            return
        self._load_palette_at_offset(self._tile_anchor_offset(self._selected_tile))
