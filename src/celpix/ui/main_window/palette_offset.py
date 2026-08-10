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
owner's **pixel** pathway (:meth:`~PaletteOffsetMixin.
_offset_palette_pixel_owner`), whose Write carries the whole region back through
``unshape`` and the container.

**How far it may run.** Every window is floored to whole entries, because the
colour codecs reject a partial trailing one, and capped at a full palette. The
step buttons clamp against the same end the read window is sized from, so holding
an arrow at the edge stops rather than raising the past-EOF alert a typed offset
would.

What is not here: the other four load modes and the commit every one of them ends
in (:mod:`~celpix.ui.main_window.palette_source`), the dock widgets this drives
(:mod:`~celpix.ui.main_window.palette_dock`), and what a colour edit does once it
has an owner (:mod:`~celpix.ui.main_window.color_editing`).
"""

from __future__ import annotations

from pathlib import Path

from celpix.core.context import (
    KEY_SOURCE_OFFSET,
)
from celpix.core.document import Document
from celpix.core.errors import PipelineError
from celpix.core.palette import FULL_PALETTE_COUNT
from celpix.pipeline import pipeline
from celpix.pipeline.pathway import PathwayConfig
from celpix.plugins.base import NO_RESHAPE, FileRef
from celpix.project.workspace import (
    Entry,
    EntryKind,
    PaletteMode,
    entry_view_bytes,
    reorders_bytes,
)


class PaletteOffsetMixin:
    """Where an Offset-mode palette is read from, and how far it may reach.

    A slice of :class:`~celpix.ui.main_window.window.MainWindow`, not a
    standalone object: it reads and writes the window's own widgets and its
    single live ``_doc``. See the module docstring for what it owns, and the
    package docstring for why these are mixins.
    """

    def _initial_palette_offset(self) -> int:
        """Where Offset mode starts: the selected tile, else the window top-left
        - the same byte numbers the offset box and status bar already show."""
        assert self._doc is not None
        # No stamp here, so no on-screen snap - this only reads a byte offset.
        return self._tile_byte_offset(self._anchor_tile())

    def _palette_offset_text(self) -> str:
        """The palette offset field's text provider; safe with no document."""
        if self._doc is None or self._palette_mode is not PaletteMode.OFFSET:
            return ""
        return self._format_offset(self._doc.palette_config.source.offset)

    def _on_palette_offset_committed(self, byte_off: int) -> None:
        # On failure the commit's own unconditional refresh reverts the text.
        if self._doc is not None:
            self._load_palette_at_offset(byte_off)

    def _step_palette_offset(self, delta_tiles: int) -> None:
        """Nudge the Offset-mode palette by ``delta_tiles`` whole tiles.

        The ◄/► buttons: one tile of the current pixel format is the step, so
        walking the palette window a tile at a time hunts for the colors a few
        tiles off the graphics. Clamped so a step never runs before byte 0 or
        past the last position a full palette entry still fits - holding an
        arrow at the edge simply stops, without the past-EOF alert a typed
        offset would raise. Reuses the Offset-mode load, so each step is an
        ordinary undoable palette change.
        """
        entry = self._workspace.current
        if (
            self._doc is None
            or entry is None
            or self._palette_mode is not PaletteMode.OFFSET
        ):
            return
        step = self._doc.bytes_per_tile
        entry_size = pipeline.palette_entry_size(
            self._palette_preset_id(), self._registry
        )
        try:
            end = self._offset_palette_space(entry)[1]
        except OSError as exc:
            self._alert(
                f"Cannot read the palette source: {exc}", title="celPix - palette"
            )
            return
        last = end - entry_size  # last offset a whole entry still fits at
        if last < 0:
            return
        # source.offset is already in the coordinates the load expects, so a step
        # is just arithmetic on it.
        current = self._doc.palette_config.source.offset
        target = min(max(0, current + delta_tiles * step), last)
        if target != current:
            self._load_palette_at_offset(target)

    def _palette_offset_owner(self, entry: Entry | None) -> Entry | None:
        """The FILE entry whose coordinates ``entry``'s Offset palette is in.

        ``entry`` itself when it is a whole file; its **parent** when it is a
        slice, because a slice's palette offsets are parent-absolute and
        deliberately reach outside its own window - a graphics block's palette
        usually lives elsewhere in the ROM (``docs/design/palette-editing.md``
        §2). ``None`` when the parent is not open: with no entry there is no
        container or reshape to honour, and the file's own bytes are what the
        offset means.
        """
        if entry is None:
            return None
        if entry.kind is EntryKind.FILE:
            return entry
        return self._workspace.find_file(entry.path)

    def _offset_palette_files(self, entry: Entry) -> tuple[str, ...]:
        """The files ``entry``'s Offset palette offsets address.

        The **owner's** (:meth:`_palette_offset_owner`), which for a slice is the
        parent whose coordinates its offsets are already in; its own list when
        the parent is not open, which is that same list — a slice carries the
        parent's files precisely so an offset into a joined region keeps meaning
        one thing (:func:`~celpix.project.workspace.slice_of`).

        Asked of the *entry* rather than read off ``doc.pixel_config``, which is
        the same answer for a pixel document and the wrong file entirely for a
        **tilemap**: a map's pixel pathway is the bound bank's, so an unbound one
        offers no file at all and a bank in another file offers the wrong one.
        The map's own bytes are its ``tilemap_config``, and its palette offsets
        are in that lineage's coordinates like any other entry's.
        """
        owner = self._palette_offset_owner(entry)
        return owner.paths if owner is not None else entry.paths

    def _reordered_view(self, owner: Entry) -> tuple[bytes, int] | None:
        """``owner``'s view buffer plus the offset its first byte sits at, or
        ``None`` when reading the file would give the same bytes anyway.

        A container that merely skips a header leaves every remaining byte where
        it was, so offsets still name file bytes and the palette keeps reading
        the file - which is also what keeps its write half. A **permuting**
        container or an active reshape makes the buffer a different address space
        from the file, and then the buffer is the only place the offset means
        anything at all.

        The owner's live bytes are used when it is loaded, so an Offset palette
        sees a dirty parent's unsaved edits exactly as a slice of it would;
        otherwise the region is read fresh (``workspace.entry_view_bytes`` — the
        same read a slice of the owner performs, so the two can never disagree).
        """
        if not reorders_bytes(owner, self._registry):
            return None
        self._settle_region(owner)
        return entry_view_bytes(
            owner,
            self._registry,
            owner.session.pixel_preset_id
            if owner.session is not None
            else self._pixel_preset_id(),
            self._workspace,
        )

    def _offset_palette_space(
        self, entry: Entry
    ) -> tuple[tuple[bytes, int] | None, int]:
        """The address space ``entry``'s Offset palette reads: ``(view, end)``.

        ``view`` is the owner's ``(buffer, base)`` when it reorders bytes —
        offsets then index the buffer at ``offset - base`` — else ``None``,
        meaning offsets are file offsets into the joined files. ``end`` is one
        past the highest offset addressable in that space, whichever it is: the
        shared answer behind both the read window
        (:meth:`_offset_palette_source`) and the step buttons' clamp.

        ``base`` mirrors what the *address display* uses for the owner, because
        a palette offset is one of the numbers on screen: under an active
        reshape the display falls back to 0-based buffer positions
        (``_display_base``), so the base is 0; under a permuting container the
        display keeps the recorded start (those are the ROM's real addresses —
        an ``.smd`` body begins past its copier header), so the base is that
        start.
        """
        owner = self._palette_offset_owner(entry)
        view = self._reordered_view(owner) if owner is not None else None
        if view is not None:
            data, base = view
            if owner is not None and owner.reshape_id != NO_RESHAPE:
                base = 0
            return (data, base), base + len(data)
        paths = self._offset_palette_files(entry)
        return None, sum(Path(p).stat().st_size for p in paths)

    def _offset_palette_pixel_owner(self) -> Entry | None:
        """The FILE entry whose pixel buffer holds the on-screen Offset palette,
        when a color edit should land there — ``None`` for every other palette.

        A buffer-backed Offset palette (its source carries ``data``: the owner
        reorders bytes, so the window was cut from the owner's view buffer) has
        no file span of its own to write, but the *owner's* pixel pathway writes
        the whole region through ``unshape`` and the container already. So the
        edit is persisted by splicing into that buffer and dirtying the owner's
        pixel pathway — this answers *which entry that is*, loading its document
        if it isn't yet (a slice's parent may be closed), and ``None`` when the
        owner's own write path can't carry the edit anyway (no ``unshape``, no
        container write half), which keeps those palettes honestly view-only.
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
        assert owner.doc is not None
        if not owner.doc.pixel_config.write_enabled:
            return None
        return owner

    def _sync_offset_palette_bytes(self, doc: Document, pixel_owner: Entry) -> None:
        """Splice ``doc``'s current palette bytes into ``pixel_owner``'s buffer.

        The persistence half of a buffer-backed Offset palette edit: re-encode
        the edited entries over the splice base (``pipeline.spliced_palette_bytes``)
        and land the window in the owner's ``pixel_data`` at the offset it was
        read from — the exact bytes the owner's next pixel Write will carry
        through ``unshape`` and the container. Runs on undo as well as redo (the
        splice is recomputed from the palette's state, not diffed), so the buffer
        always mirrors the palette on screen.
        """
        target = pixel_owner.doc
        if target is None:
            return
        try:
            window = pipeline.spliced_palette_bytes(doc, self._registry)
        except PipelineError as exc:
            self._report(exc)
            return
        # The same addressing rule as _offset_palette_space, recomputed live
        # rather than trusted from the ref's data_base (the owner's document may
        # have been rebuilt since): 0-based under a reshape, the recorded start
        # under a permuting container.
        base = (
            0
            if pixel_owner.reshape_id != NO_RESHAPE
            else target.pixel_ctx.get(KEY_SOURCE_OFFSET, 0)
        )
        target.replace_bytes(doc.palette_config.source.offset - base, window)

    def _offset_palette_source(
        self,
        byte_off: int,
        preset_id: str | None = None,
        entry: Entry | None = None,
    ) -> tuple[FileRef | None, bool]:
        """The read window for an Offset palette at ``byte_off``, and whether a
        color edit can be written back through it.

        ``byte_off`` is in the **owning file entry's** view coordinates - the same
        numbers the offset box and the status bar show. Where the file is its own
        buffer that is a file offset, and the window is read (and written)
        straight from disk. Where the owner reorders bytes the window is cut from
        its view buffer instead, and the *palette pathway* comes back write-off:
        a length-bounded ``FileRef`` into the file cannot say where a permuted
        splice belongs. Color edits still persist - through the buffer owner's
        **pixel** pathway instead (:meth:`_offset_palette_pixel_owner`), whose
        Write carries the whole region through ``unshape`` and the container
        (``docs/design/palette-editing.md`` §2).

        Floored to whole entries - the color codecs reject a partial trailing
        one, so clamping at the end alone is not enough. ``(None, ...)`` when not
        even one entry fits. ``preset_id`` overrides the combo when sizing
        colors for a non-current entry's palette format, and ``entry`` names
        whose palette is being resolved - both default to the live document, and
        both are passed when a project restore loads an entry that is not (yet)
        the one on screen.
        """
        entry = entry if entry is not None else self._workspace.current
        assert entry is not None
        fmt = preset_id or self._palette_preset_id()
        view, end = self._offset_palette_space(entry)
        writable = view is None
        base = 0 if view is None else view[1]
        avail = end - byte_off if byte_off >= base else 0
        colors = min(
            FULL_PALETTE_COUNT,
            pipeline.palette_entry_capacity(avail, fmt, self._registry),
        )
        if colors <= 0:
            return None, writable
        length = pipeline.palette_read_bytes(colors, fmt, self._registry)
        # The owner's whole file list: the offset addresses the joined region, so
        # a several-chip one cannot be answered from its first chip alone.
        paths = self._offset_palette_files(entry)
        if view is None:
            return FileRef(paths, offset=byte_off, length=length), True
        data, base = view
        return (
            FileRef(paths, offset=byte_off, length=length, data=data, data_base=base),
            False,
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
        # No compression on this pathway, and none is reachable: an offset
        # resolves against a *file* entry's buffer, and a file entry's pathway
        # never carries a scheme - only a slice's does. Which agrees with the
        # intent anyway: a palette sitting next to compressed graphics is not
        # itself compressed, and round-tripping it through a compressor would
        # relocate and corrupt it.
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
        """Palette ▸ Load from Selection: Offset mode at the selected tile."""
        if self._doc is None or self._selected_tile is None:
            return
        self._load_palette_at_offset(self._tile_byte_offset(self._selected_tile))
