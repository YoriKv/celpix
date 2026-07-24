"""Where the palette's colors come from, and how a change to that is committed.

The five load modes (:class:`~celpix.project.workspace.PaletteMode`) and their
loaders: a standalone ``.pal``, raw bytes at an offset in the entry's own pixel
file, an emulator save state, the generated default, and a Custom palette stored
in the project.

Two rules run through it. Every gesture ends in :meth:`_commit_palette`, so a
palette change is always a before/after pair on the session stack and a failed
load can revert the dropdown instead of lying about the source. And a mode with
nowhere to write an edit - the generated default, a save state we never write
back - **forks to Custom** rather than failing, so the edit lands somewhere that
persists (``docs/design/palette-editing.md``).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from PySide6.QtWidgets import (
    QFileDialog,
)

from celpix.core import emustate
from celpix.core.context import (
    PipelineContext,
)
from celpix.core.document import Document
from celpix.core.errors import Pathway, PipelineError, Stage
from celpix.core.palette import FULL_PALETTE_COUNT, Palette
from celpix.pipeline import pipeline
from celpix.pipeline.pathway import PathwayConfig
from celpix.plugins.base import FileRef
from celpix.project.workspace import (
    Entry,
    EntryKind,
    PaletteMode,
    PaletteSource,
    data_missing,
    export_basename,
)
from celpix.ui.undo_commands import (
    AddEntryCommand,
    PaletteCommand,
    PaletteState,
    PaletteUserLink,
)
from celpix.ui.widgets import (
    select_combo_data,
)

# The session's palette format before any real one has been chosen. RGB888 is
# the plainest, most widely understood encoding, and the right neutral basis for
# a Custom palette forked off the generated default - free ARGB colors with no
# console format behind them. The session default follows the last format
# actually selected from there (:meth:`PaletteSourceMixin._set_session_palette_format`).
_DEFAULT_SESSION_PALETTE_FORMAT = "preset.palette.rgb888"


class PaletteSourceMixin:
    """The palette's source: the five load modes, their loaders, and the commit.

    A slice of :class:`~celpix.ui.main_window.window.MainWindow`, not a
    standalone object: it reads and writes the window's own widgets and its
    single live ``_doc``. See the module docstring for what it owns, and the
    package docstring for why these are mixins.
    """

    # -- File-palette ownership --------------------------------------------
    # A File-mode palette is owned by its PALETTE entry, not by the graphic that
    # renders it: the entry holds the live colors, and a color edit dirties *it*
    # and writes back to the .pal. Graphics show the palette **by reference** — a
    # mirror kept on each graphic's own ``doc.palette`` (write-disabled, so a
    # graphic Write never touches the palette). See docs/design/palette-editing.md.
    def _linked_palette_entry(self) -> Entry | None:
        """The registered PALETTE entry backing the current File-mode palette."""
        if self._doc is None or self._palette_mode is not PaletteMode.FILE:
            return None
        path = self._doc.palette_config.source.path
        return self._workspace.find_palette(path) if path else None

    def _palette_doc(self) -> Document | None:
        """The document that *owns* the palette on screen.

        The linked PALETTE entry's document in File mode — so an edit, a format
        re-decode, or a save acts on the palette rather than the graphic — and the
        current graphic's own document in every other mode (offset lives in the
        graphic's bytes; custom/default in the graphic/project).
        """
        entry = self._linked_palette_entry()
        if entry is not None and entry.doc is not None:
            return entry.doc
        return self._doc

    def _palette_owner_entry(self) -> Entry | None:
        """Whose dirt a palette edit belongs to: the PALETTE entry in File mode,
        else the current graphics entry (offset writes the graphic's own bytes)."""
        entry = self._linked_palette_entry()
        return entry if entry is not None else self._workspace.current

    def _mirror_palette(self, palette_entry: Entry) -> None:
        """Copy a PALETTE entry's live colors onto every graphic that renders it.

        The palette entry owns the colors; each graphic shows them through its own
        ``doc.palette`` so the codec/rendering path is unchanged. One color edit
        therefore updates every open graphic using the file at once. The mirrored
        config is write-disabled: a graphic never writes the palette back (that is
        the palette entry's Write).
        """
        src = palette_entry.doc
        if src is None:
            return
        mirror_cfg = replace(src.palette_config, write_enabled=False)
        for entry in self._workspace.palette_render_targets(palette_entry.path):
            entry.doc.palette = src.palette
            entry.doc.palette_ctx = src.palette_ctx
            entry.doc.palette_config = mirror_cfg

    def _link_file_palette(
        self, graphics: Entry, path: str, offset: int, preset_id: str
    ) -> bool:
        """Point ``graphics`` at the PALETTE entry for ``path``, loading it once.

        Registers the palette entry if the project never had one (a hand-authored
        or older file), builds its live document on first use, then mirrors the
        colors onto ``graphics``. Raising on a bad load is deliberate: the caller
        (:meth:`_restore_palette_source`) degrades to the default palette.
        """
        assert graphics.doc is not None
        entry = self._workspace.find_palette(path)
        if entry is None:
            entry = self._workspace.add_palette(path, preset_id)
        if entry.doc is None:
            cfg = self._file_palette_config(
                path, offset, entry.palette_preset_id or preset_id
            )
            loaded = pipeline.load_palette(cfg, self._registry)
            entry.doc = Document.palette_only(
                loaded.palette, cfg, loaded.ctx, loaded.data
            )
        graphics.doc.palette = entry.doc.palette
        graphics.doc.palette_ctx = entry.doc.palette_ctx
        graphics.doc.palette_config = replace(
            entry.doc.palette_config, write_enabled=False
        )
        graphics.missing_palette = None
        return True

    @staticmethod
    def _file_palette_config(path: str, offset: int, preset_id: str) -> PathwayConfig:
        """The writable pathway a PALETTE entry reads and writes its ``.pal`` with.

        Source and dest are the same file, so a color edit re-encodes into exactly
        the bytes it was read from (the whole file for a plain ``.pal``).
        """
        return PathwayConfig(
            source=FileRef(path, offset=offset),
            dest=FileRef(path, offset=offset),
            interpret_preset_id=preset_id,
        )

    def _file_palette_colors(self, palette: Entry) -> list[int]:
        """The colors a removed file palette hands each graphic as a custom copy.

        Its live (possibly edited) colors when the palette is loaded; otherwise the
        file's own, read on demand; an empty list if even that fails, so a removal
        never dead-ends on an unreadable file.
        """
        if palette.doc is not None:
            return list(palette.doc.palette.colors)
        preset = palette.palette_preset_id or self._palette_preset_id()
        try:
            loaded = pipeline.load_palette(
                self._file_palette_config(palette.path, 0, preset), self._registry
            )
        except (PipelineError, OSError):
            return []
        return list(loaded.palette.colors)

    def _convert_user_to_custom(
        self, entry: Entry, colors: list[int], preset_id: str
    ) -> None:
        """Re-home a graphic onto a Custom palette of ``colors`` - what removing a
        file palette leaves behind. This is a **project** change, not a graphic
        edit: only the record of which palette the graphic uses changes, so its
        pixel bytes and dirt are untouched (docs/design/palette-editing.md).
        """
        if entry.session is not None:
            entry.session.palette_mode = PaletteMode.CUSTOM
        entry.missing_palette = None
        if entry.doc is not None:
            entry.doc.palette = Palette(list(colors))
            entry.doc.palette_config = self._placeholder_palette_config(preset_id)
            entry.doc.palette_bytes = b""
            entry.doc.palette_edits = set()
            entry.pending_palette = None
        else:
            # Never loaded: seed the custom colors as the restore a first load reads.
            entry.pending_palette = PaletteSource(colors=list(colors))

    def _relink_user_to_file_palette(self, link: PaletteUserLink) -> None:
        """Undo of :meth:`_convert_user_to_custom`: point the graphic back at the
        (restored) file palette, re-mirroring its colors when it is loaded."""
        entry = link.entry
        if entry.session is not None:
            entry.session.palette_mode = PaletteMode.FILE
        if link.loaded and entry.doc is not None:
            self._link_file_palette(entry, link.path, link.offset, link.preset_id)
        else:
            entry.pending_palette = PaletteSource(path=link.path, offset=link.offset)

    def _restore_palette_source(self, entry: Entry, source: PaletteSource) -> bool:
        """Load ``source`` onto ``entry``'s document palette; True on success.

        Shared by first-load restore and post-relocation reload. An external
        palette whose file is missing degrades **quietly**: the entry keeps its
        palette_mode for display, renders on the default palette, and stashes the
        source on ``missing_palette`` so Locate missing files can re-point it and
        save keeps the reference. Any other failure degrades to the default
        palette with an alert.
        """
        doc, session = entry.doc, entry.session
        assert doc is not None and session is not None
        if source.colors is not None:
            doc.palette = Palette(source.colors)
            entry.missing_palette = None
            return True
        if source.path is not None and not Path(source.path).exists():
            # The file moved: hold this mode on the default palette and remember
            # the source. No alert - the files-list highlight signals it instead.
            entry.missing_palette = source
            doc.palette = self._fallback_palette()
            return False
        try:
            if session.palette_mode is PaletteMode.FILE and source.path is not None:
                # A file palette is owned by its PALETTE entry; register/load it
                # and mirror onto this graphic rather than loading colours here.
                return self._link_file_palette(
                    entry, source.path, source.offset, session.palette_preset_id
                )
            if session.palette_mode is PaletteMode.EMULATOR and source.path is not None:
                # Re-detect the save state: the palette offset and the console's
                # codec are derived from the file, not carried in the project.
                _fmt, cfg = self._emulator_palette_config(source.path)
            elif source.path is not None:  # an external palette file
                cfg = PathwayConfig(
                    source=FileRef(source.path, offset=source.offset),
                    interpret_preset_id=session.palette_preset_id,
                )
            else:  # palette bytes at an offset in the entry's own file
                ref = self._selection_palette_source(
                    doc.pixel_config.source.path,
                    source.offset,
                    session.palette_preset_id,
                )
                if ref is None:
                    raise PipelineError(
                        Stage.READ,
                        Pathway.PALETTE,
                        "not enough data at the palette offset",
                    )
                # Writable, as on the interactive Offset load: the bounded ref
                # confines Write to the palette's own bytes.
                cfg = PathwayConfig(
                    source=ref, interpret_preset_id=session.palette_preset_id
                )
            loaded = pipeline.load_palette(cfg, self._registry)
            doc.palette, doc.palette_ctx = loaded.palette, loaded.ctx
            doc.palette_bytes, doc.palette_edits = loaded.data, set()
            doc.palette_config = cfg
            entry.missing_palette = None
            return True
        except (PipelineError, OSError, emustate.StateError) as exc:
            session.palette_mode = PaletteMode.DEFAULT
            entry.missing_palette = None
            self._alert(
                f"{entry.name}: palette not restored, using the default "
                f"palette instead.\n\n{exc}",
                title="Celpix - palette",
            )
            return False

    # Shared by the two dialogs that name a .pal - the export that writes one and
    # the open that registers one - so both offer the same filter.
    _PALETTE_FILTER = "Palette files (*.pal);;All files (*)"

    # What Export to File writes, always - never the palette's own read format.
    # A .pal carries no marker of its encoding, so the one thing every reader
    # (ours included) has to guess should be the plainest, most widely
    # understood option rather than whichever codec these colors arrived
    # through: three bytes R, G, B per entry, which is what emulator and editor
    # .pal files overwhelmingly are. It is also the only choice that survives
    # every source - the modes that can export include an emulator state, whose
    # console-dictated codec may be an *index* into a fixed table (NES), and
    # writing those index bytes out under a .pal name would export something no
    # other tool could read as color.
    _EXPORT_PRESET_ID = "preset.palette.rgb888"

    def _prompt_add_palette_file(self) -> None:
        # No .pal filter: palette data is just bytes reinterpreted through the
        # chosen color format, so any file can hold it - a ROM, a save state, a
        # raw dump. Opens any file, like the panel's File source (_open_palette),
        # rather than hiding everything that isn't already named .pal.
        path, _ = QFileDialog.getOpenFileName(self, "Open palette data")
        if path:
            self._add_palette_file(path)

    def _export_palette_file(self) -> None:
        """Palette dock ▸ Export to File…: write the live colors out as a ``.pal``.

        Offered for the modes whose palette exists nowhere else as a file of its
        own - an Offset palette is buried in the pixel file, an Emulator State
        one inside a save state, a Custom one only in the project - so this is
        how those colors become reusable and shareable. The written file is
        registered in the Palettes section straight away, so it is one
        double-click from being re-applied and it travels with the project.

        Always written as :data:`_EXPORT_PRESET_ID` (RGB888), and the entry is
        registered under that same format so the round-trip reads back the
        colors that went out. Deliberately *not* the format dropdown's value:
        in two of the three exporting modes that combo is hidden, so it holds
        whatever it was last left on - an invisible setting silently deciding
        the encoding of a file meant to be shared.
        """
        if self._doc is None or not self._palette_mode.is_exportable:
            return
        entry = self._workspace.current
        suggested = f"{export_basename(entry)}.pal" if entry is not None else "palette"
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Export palette",
            str(Path(self._export_dir(entry)) / suggested),
            self._PALETTE_FILTER,
        )
        if not path:
            return
        if not path.lower().endswith(".pal"):
            path += ".pal"
        try:
            pipeline.export_palette(
                self._doc, path, self._registry, self._EXPORT_PRESET_ID
            )
        except PipelineError as exc:
            self._report(exc)
            return
        except OSError as exc:
            self._alert(f"Cannot write {path}: {exc}", title="Celpix - palette")
            return
        added = self._add_palette_file(
            path, quiet=True, preset_id=self._EXPORT_PRESET_ID
        )
        name = Path(path).name
        self.statusBar().showMessage(
            f"Exported palette to {name} as RGB888"
            + (" and added it to Palettes." if added else " (already in Palettes).")
        )

    def _add_palette_file(
        self, path: str, *, quiet: bool = False, preset_id: str | None = None
    ) -> bool:
        """Register ``path`` in the files list's Palettes section; False if it
        already was.

        The shared entry point for File ▸ Open palette data, a dropped ``.pal``
        and the dock's palette export. Registration only - applying it to the
        view is the list's double-click. The entry starts on the palette format
        the dropdown is on right now - or ``preset_id``, for a caller that knows
        the file's encoding because it just wrote it - and tracks the dropdown
        from then on whenever this file is the palette on screen
        (:meth:`_sync_palette_entry_format`); identity is the path, so re-adding
        an already-registered file is a no-op rather than a duplicate. ``quiet``
        leaves the status line to a caller that has its own (larger) outcome to
        report.
        """
        existing = self._workspace.find_palette(path)
        if existing is not None:
            if preset_id is not None and existing.palette_preset_id != preset_id:
                # An export over an already-registered path: the bytes on disk
                # are the ones just written, so the entry's recorded format has
                # to follow them rather than describing the file it replaced.
                existing.palette_preset_id = preset_id
                self._files_panel.refresh_entry(existing)
            if not quiet:
                self.statusBar().showMessage(f"{existing.name} is already in Palettes.")
            return False
        entry = Entry(
            name=Path(path).name,
            kind=EntryKind.PALETTE,
            path=path,
            palette_preset_id=preset_id or self._palette_preset_id(),
        )
        self._push_command(AddEntryCommand(self, entry, f"add palette {entry.name}"))
        if not quiet:
            self.statusBar().showMessage(f"Added {entry.name} to Palettes.")
        return True

    def _use_palette_entry(self, entry: Entry) -> None:
        """Apply a registered palette file to the view (File mode) - the
        Palettes section's double-click / context-menu action.

        Decodes with the codec the *entry* remembers, not wherever the format
        dropdown has moved since; the commit then snaps the dropdown onto that
        codec, so the two agree afterwards.
        """
        if self._doc is None:
            self.statusBar().showMessage("Open pixel data first.")
            return
        if data_missing(entry):
            self._alert(
                f"{entry.name}: file not found - File ▸ Locate missing files "
                "to re-point it.",
                title="Celpix - palette",
            )
            return
        self._apply_file_palette(
            entry.path,
            preset_id=entry.palette_preset_id or self._palette_preset_id(),
            label=f"use palette {entry.name}",
            status=lambda n: f"Loaded {n} colors from {entry.name}",
        )

    def _open_palette(self) -> bool:
        """Load a palette from a separate file; ``False`` on cancel/failure so
        the mode dropdown can revert instead of lying about the source."""
        if self._doc is None:
            self.statusBar().showMessage("Open pixel data first.")
            return False
        path, _ = QFileDialog.getOpenFileName(self, "Open palette")
        if not path:
            return False
        return self._apply_file_palette(
            path,
            preset_id=self._palette_preset_id(),
            label=f"load palette from {Path(path).name}",
            status=lambda n: f"Loaded {n} colors from {path}",
        )

    def _apply_file_palette(
        self,
        path: str,
        *,
        preset_id: str,
        label: str,
        status: Callable[[int], str],
    ) -> bool:
        """Register, load, and switch the graphic to the file palette at ``path``.

        The single path behind both the mode dropdown's *File* pick and a Palettes
        double-click. The file is registered in the Palettes list so it has a
        stable home (a no-op if already there); its live document is the source of
        truth, **reused** when it exists so unsaved edits survive a re-apply, and
        loaded from disk under ``preset_id`` only on first use. Then the graphic is
        switched to File mode pointing at it - one undoable palette change; a bad
        load reports and returns ``False`` so the mode dropdown can revert.
        """
        if self._doc is None:
            self.statusBar().showMessage("Open pixel data first.")
            return False
        self._add_palette_file(path, quiet=True)  # register if the list lacks it
        entry = self._workspace.find_palette(path)
        assert entry is not None
        if entry.doc is not None:
            # Already live (maybe with unsaved edits): reuse rather than re-reading
            # the file, which would discard them. The entry owns the colors now.
            doc = entry.doc
            loaded = pipeline.PaletteData(
                doc.palette, doc.palette_ctx, doc.palette_bytes
            )
            cfg = doc.palette_config
            edits = frozenset(doc.palette_edits)
        else:
            cfg = self._file_palette_config(path, 0, preset_id)
            try:
                loaded = pipeline.load_palette(cfg, self._registry)
            except PipelineError as exc:
                self._report(exc)
                return False
            entry.doc = Document.palette_only(
                loaded.palette, cfg, loaded.ctx, loaded.data
            )
            edits = frozenset()
        self._commit_palette(
            cfg,
            loaded,
            mode=PaletteMode.FILE,
            label=label,
            status=status(len(loaded.palette)),
            edits=edits,
        )
        return True

    def _emulator_palette_config(
        self, path: str
    ) -> tuple[emustate.StateFormat, PathwayConfig]:
        """Detect the emulator state at ``path`` and build its palette config.

        The console is auto-detected from the file's bytes/extension, and the
        palette codec is the one that console dictates (BGR555 for SNES, the NES
        master-palette index table, …) - not whatever the format dropdown was
        on. View-only: the state is a memory dump, never a palette we write back.
        Raises :class:`emustate.StateError` (unrecognised / palette not located)
        or the usual pipeline/OS errors; the read window is floored to what fits.
        """
        data = Path(path).read_bytes()
        fmt, region = emustate.locate_palette(data, Path(path).suffix)
        if region.data is not None:
            # The palette was extracted from a container/memory image, not found
            # at a file offset - feed those bytes straight through the pipeline.
            entry_bytes = pipeline.palette_entry_size(region.preset_id, self._registry)
            length = min(len(region.data), region.count * entry_bytes)
            ref: FileRef | None = FileRef(
                path, offset=0, length=length, data=region.data
            )
        else:
            ref = self._selection_palette_source(
                path, region.offset, region.preset_id, max_entries=region.count
            )
        if ref is None:
            raise emustate.StateError(
                f"{fmt.name} state: no palette data at the detected offset "
                f"({self._format_offset(region.offset)})."
            )
        return fmt, PathwayConfig(
            source=ref, interpret_preset_id=region.preset_id, write_enabled=False
        )

    def _open_emulator_state(self) -> bool:
        """Load a palette from an emulator save state; ``False`` on cancel/failure
        so the mode dropdown can revert instead of lying about the source."""
        if self._doc is None:
            self.statusBar().showMessage("Open pixel data first.")
            return False
        path, _ = QFileDialog.getOpenFileName(self, "Open emulator save state")
        if not path:
            return False
        try:
            fmt, cfg = self._emulator_palette_config(path)
        except emustate.StateError as exc:
            self._alert(str(exc), title="Celpix - emulator state")
            return False
        except OSError as exc:
            self._alert(f"Cannot read {path}: {exc}", title="Celpix - emulator state")
            return False
        return self._load_and_commit_palette(
            cfg,
            mode=PaletteMode.EMULATOR,
            label=f"load {fmt.console} palette from {fmt.name} state",
            status=lambda n: (
                f"Loaded {n} {fmt.console} colors from {fmt.name} state (view-only)"
            ),
        )

    # -- palette load modes ------------------------------------------------
    def _placeholder_palette_config(
        self, preset_id: str | None = None
    ) -> PathwayConfig:
        """The no-palette-loaded config: empty source, never written back.

        ``preset_id`` overrides the combo when loading a non-current entry,
        whose session may name a different palette format.
        """
        return PathwayConfig(
            source=FileRef(""),
            interpret_preset_id=preset_id or self._palette_preset_id(),
            write_enabled=False,
        )

    def _capture_palette_state(self) -> PaletteState:
        """Snapshot the palette pathway + selectors - an undo command's side.

        The preset comes from the document's config, not the format combo: in
        the combo's own change handler the widget has already moved, and only
        the config still holds the outgoing format (the _on_pixel_preset_change
        trick), so undo can restore the combo correctly.
        """
        doc = self._palette_doc()
        assert doc is not None
        return PaletteState(
            preset_id=doc.palette_config.interpret_preset_id,
            mode=self._palette_mode,
            palette=doc.palette,
            config=doc.palette_config,
            ctx=doc.palette_ctx,
            data=doc.palette_bytes,
            edits=frozenset(doc.palette_edits),
        )

    def _apply_palette_state(self, state: PaletteState) -> None:
        """Land a :class:`PaletteState` on the document and its widgets - the
        one application path for palette commands and plugin refreshes; never
        pushes, and stays silent (status messages belong to the gestures).

        In File mode the palette lives on its PALETTE entry and the graphic only
        mirrors it (:meth:`_apply_file_palette_state`); every other mode lands the
        colors straight on the current graphic's document.
        """
        assert self._doc is not None
        select_combo_data(self._palette_preset, state.preset_id)
        if state.mode is PaletteMode.FILE:
            self._apply_file_palette_state(state)
        else:
            self._doc.palette = state.palette
            self._doc.palette_config = state.config
            self._doc.palette_ctx = state.ctx
            # The splice base travels with the colors: a fresh load resets it (no
            # entry is edited yet), and an undo restores whatever it was before.
            self._doc.palette_bytes = state.data
            self._doc.palette_edits = set(state.edits)
        self._set_palette_mode(state.mode)  # already signal-safe
        self._sync_palette_entry_format(state)
        self._refresh_view()

    def _apply_file_palette_state(self, state: PaletteState) -> None:
        """Land a File-mode state: the PALETTE entry owns it, the graphic mirrors.

        The palette entry's document is the source of truth for the colors, splice
        base and touched-entry set, so undo/redo restore *it* exactly; the graphic
        then shows those colors by reference. The entry is registered on the fly if
        the history predates its registration (a project without it).
        """
        assert self._doc is not None
        path = state.config.source.path
        entry = self._workspace.find_palette(path) if path else None
        if entry is None and path:
            entry = self._workspace.add_palette(path, state.preset_id)
        if entry is None:
            return
        if entry.doc is None:
            entry.doc = Document.palette_only(
                state.palette, state.config, state.ctx, state.data
            )
        else:
            entry.doc.palette = state.palette
            entry.doc.palette_config = state.config
            entry.doc.palette_ctx = state.ctx
            entry.doc.palette_bytes = state.data
            entry.doc.palette_edits = set(state.edits)
        self._mirror_palette(entry)
        # Mid-switch the current graphic still names its old palette source, so it
        # isn't a render target yet; point it at the palette entry directly.
        self._doc.palette = entry.doc.palette
        self._doc.palette_ctx = entry.doc.palette_ctx
        self._doc.palette_config = replace(
            entry.doc.palette_config, write_enabled=False
        )

    def _sync_palette_entry_format(self, state: PaletteState) -> None:
        """Write a File-mode palette's format back onto its registered entry.

        A PALETTE entry's ``palette_preset_id`` is the codec its double-click
        decodes with, so re-picking the format dropdown while that file's colors
        are on screen has to update it - otherwise applying the file again would
        silently go back to the format it was registered with, undoing a choice
        the user just made.

        Hooked here, on the state-application path, rather than in the format
        combo's own handler: every palette change lands through here exactly
        once, so undo and redo re-stamp the entry along with the document
        instead of leaving it on the format of a change that was rolled back.
        Only File mode has a registered file behind it; the other modes read
        from the pixel file, a save state or the project, and none of those has
        an entry to record a format on.
        """
        if state.mode is not PaletteMode.FILE:
            return
        entry = self._workspace.find_palette(state.config.source.path)
        if entry is None or entry.palette_preset_id == state.preset_id:
            return
        entry.palette_preset_id = state.preset_id
        self._files_panel.refresh_entry(entry)  # its tooltip names the format

    def _commit_palette(
        self,
        cfg: PathwayConfig,
        loaded: pipeline.PaletteData,
        *,
        mode: PaletteMode,
        label: str,
        status: str | None = None,
        edits: frozenset[int] = frozenset(),
    ) -> None:
        """Push one palette-source change (before→after) and optionally note it.

        The shared tail of every palette gesture - load-from-file, offset,
        emulator state, format re-decode, and back-to-default: snapshot the live
        palette as the undo *before*, land the freshly loaded palette + ``cfg``
        as the *after*, and report ``status`` for the user-initiated loads. Each
        caller keeps its own source-specific load and error reporting; only this
        uniform push/report is shared.

        The new state usually starts with **no edits** - its bytes are what is on
        disk, so a save has nothing to splice until a color changes. Re-applying an
        already-edited file palette passes its live ``edits`` so the switch doesn't
        forget which entries are outstanding.

        A commit that decodes raw bytes (a File/Offset/Emulator import, or a
        format re-decode) is a format being *chosen*, so it advances the session
        default the next Custom-from-default fork will inherit. Default and
        Custom commits carry no such choice and leave it alone. Only forward
        gestures reach here; undo/redo replay through _apply_palette_state, so
        the session default stays put when history is walked.
        """
        if mode.decodes_raw_bytes:
            self._set_session_palette_format(cfg.interpret_preset_id)
        self._push_command(
            PaletteCommand(
                self,
                self._workspace.current,
                label,
                before=self._capture_palette_state(),
                after=PaletteState(
                    cfg.interpret_preset_id,
                    mode,
                    loaded.palette,
                    cfg,
                    loaded.ctx,
                    data=loaded.data,
                    edits=edits,
                ),
            )
        )
        if status:
            self.statusBar().showMessage(status)

    def _load_and_commit_palette(
        self,
        cfg: PathwayConfig,
        *,
        mode: PaletteMode,
        label: str,
        status: Callable[[int], str] | None = None,
    ) -> bool:
        """Decode ``cfg``'s palette and land it as one undoable change.

        The shared tail of every palette-source gesture - open a file, read an
        offset, import a save state, apply a registered ``.pal``. Each of those
        differs only in how it *builds* the config; from there the load, the
        hard-stop report on failure, and the push are identical. ``False`` (with
        the failure already reported) lets the mode dropdown revert instead of
        lying about where the palette came from.

        ``status`` is called with the loaded color count - it isn't known until
        the load succeeds, and the message reads better with it.
        """
        try:
            loaded = pipeline.load_palette(cfg, self._registry)
        except PipelineError as exc:
            self._report(exc)
            return False
        self._commit_palette(
            cfg,
            loaded,
            mode=mode,
            label=label,
            status=status(len(loaded.palette)) if status is not None else None,
        )
        return True

    def _on_palette_mode_change(self) -> None:
        """Act on a user pick in the mode dropdown; revert the combo on failure.

        self._palette_mode still holds the OLD mode here (it is only updated by
        _set_palette_mode on success), so reverting is just re-syncing to it.
        """
        # Parsed back, not read as-is: PaletteMode is a str subclass, and Qt
        # stores item data by value - so currentData() hands back a plain str,
        # never the member. Every ``is`` comparison below depends on this.
        mode = PaletteMode.parse(self._palette_mode_combo.currentData())
        if mode is self._palette_mode or self._applying_undo:
            return
        if self._doc is None:
            self.statusBar().showMessage("Open pixel data first.")
            self._set_palette_mode(self._palette_mode)
            return
        if mode is PaletteMode.DEFAULT:
            self._use_default_palette()
        elif mode is PaletteMode.FILE:
            if not self._open_palette():
                self._set_palette_mode(self._palette_mode)
        elif mode is PaletteMode.OFFSET:
            if not self._load_palette_at_offset(self._initial_palette_offset()):
                self._set_palette_mode(self._palette_mode)
        elif mode is PaletteMode.EMULATOR:
            if not self._open_emulator_state():
                self._set_palette_mode(self._palette_mode)
        elif mode is PaletteMode.CUSTOM:
            # Picking Custom explicitly does what the first edit of an
            # uneditable palette does implicitly: take the colors on screen
            # into the project.
            self._fork_custom_palette()

    def _use_default_palette(self) -> None:
        """Back to the generated default palette (mode "default")."""
        assert self._doc is not None
        self._commit_palette(
            self._placeholder_palette_config(),
            # Generated, not read: no bytes behind it to splice into.
            pipeline.PaletteData(self._fallback_palette(), PipelineContext(), b""),
            mode=PaletteMode.DEFAULT,
            label="use default palette",
            status="Using the default palette.",
        )

    def _set_session_palette_format(self, preset_id: str) -> None:
        """Record ``preset_id`` as the session's default palette format.

        The sticky, global format a Custom-from-default fork inherits when it has
        none of its own. Advanced whenever a format is actually chosen - a
        File/Offset/Emulator import or a format re-decode (via _commit_palette),
        the format dropdown, and, in future, a ROM file hint. Global and
        session-lifetime, so it survives entry switches and is not part of any
        entry's saved session.
        """
        self._session_palette_format = preset_id

    def _fork_custom_palette(self) -> None:
        """Copy the palette on screen into a project-stored Custom one.

        The generated default and an emulator state have nowhere to write a
        color - one is computed from the pixel format, the other is a memory
        dump we never write back - so editing either forks here rather than
        failing, and the edit lands somewhere that persists: the ``.celpix``
        project (``docs/design/palette-editing.md``).

        A fork off the **default** also expands to a full 16 rows: the default
        is only ever generated at the current format's index space (16 colors
        at 4bpp), and a custom palette the user is going to edit should offer
        every subpalette row, not just the one the format happens to index.

        The Custom palette *carries* a color format (shown read-only in the
        dock). A fork off a source that decodes raw bytes keeps that source's
        format - the one on the live dropdown; a fork off the generated default
        has no format to inherit, so it takes the session default instead.
        """
        assert self._doc is not None
        palette = self._doc.palette
        from_default = self._palette_mode is PaletteMode.DEFAULT
        palette = (
            palette.resized(FULL_PALETTE_COUNT) if from_default else palette.copy()
        )
        preset_id = (
            self._session_palette_format if from_default else self._palette_preset_id()
        )
        self._commit_palette(
            # No file behind it: a custom palette is written by saving the
            # project, never by the palette pathway's Write - so no splice base.
            self._placeholder_palette_config(preset_id),
            pipeline.PaletteData(palette, PipelineContext(), b""),
            mode=PaletteMode.CUSTOM,
            label="create custom palette",
            status=(
                f"Custom palette created ({len(palette)} colors) - stored in "
                "the project, not written to a file."
            ),
        )

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
        if self._doc is None or self._palette_mode is not PaletteMode.OFFSET:
            return
        step = self._doc.bytes_per_tile
        path = self._doc.pixel_config.source.path
        entry_size = pipeline.palette_entry_size(
            self._palette_preset_id(), self._registry
        )
        try:
            file_size = Path(path).stat().st_size
        except OSError as exc:
            self._alert(f"Cannot read {path}: {exc}", title="Celpix - palette")
            return
        last = file_size - entry_size  # last offset a whole entry still fits at
        if last < 0:
            return
        # source.offset is the file-absolute palette offset; step there and
        # clamp before handing it back (the load re-adds the header skip, so
        # strip it to keep the absolute value).
        current = self._doc.palette_config.source.offset
        target = min(max(0, current + delta_tiles * step), last)
        if target != current:
            self._load_palette_at_offset(target - self._header_offset())

    def _selection_palette_source(
        self,
        path: str,
        byte_off: int,
        preset_id: str | None = None,
        max_entries: int = 256,
    ) -> FileRef | None:
        """A read window for up to ``max_entries`` palette entries at ``byte_off``.

        Floored to whole entries - the color codecs reject a partial trailing
        entry, so clamping at EOF alone is not enough. ``None`` when not even one
        entry fits. ``preset_id`` overrides the combo when sizing entries for a
        non-current entry's palette format (project restore). ``max_entries``
        caps the window: the 256-entry default suits a free offset read; an
        emulator state passes its console's exact palette size instead.
        """
        bpe = pipeline.palette_entry_size(
            preset_id or self._palette_preset_id(), self._registry
        )
        avail = Path(path).stat().st_size - byte_off
        entries = min(max_entries, max(0, avail) // bpe)
        if entries == 0:
            return None
        return FileRef(path, offset=byte_off, length=entries * bpe)

    def _load_palette_at_offset(self, byte_off: int) -> bool:
        """Load palette data from the pixel source file at ``byte_off`` (Offset mode).

        The offset is in the pixel *source's* coordinate space (the same numbers
        the offset box shows - i.e. after any header skip, which is re-added for
        the file read), and the palette pathway re-reads the raw file - for
        container/compressed pixel sources the bytes at that offset differ from the
        decoded pixel data. Accepted for now; it mirrors the offset box semantics.
        For a **slice**, the source file is the *parent*, so the offset is an
        absolute parent-file offset - deliberately unbounded by the slice, since
        a graphics block's palette usually lives elsewhere in the ROM.

        The read window is **writable**: color edits re-encode into exactly the
        bytes they were read from (the ``FileRef`` is length-bounded, so Write
        can only ever rewrite the palette's own region). That is the point of
        Offset mode - editing a palette where it actually lives in the ROM. The
        hazard is the user's to judge: the window is sized to whatever fits, so
        pointing it at bytes that aren't really a palette and then saving
        rewrites them (``docs/design/palette-editing.md``).
        """
        if self._doc is None:
            return False
        src = self._doc.pixel_config.source
        try:
            ref = self._selection_palette_source(
                src.path, byte_off + self._header_offset()
            )
        except PipelineError as exc:
            self._report(exc)
            return False
        except OSError as exc:
            self._alert(f"Cannot read {src.path}: {exc}", title="Celpix - palette")
            return False
        if ref is None:
            self._alert(
                "Not enough data at that offset for a palette entry.",
                title="Celpix - palette",
            )
            return False
        # Compression is deliberately ignored on this pathway: the config keeps
        # the default decompress.none/compress.none, so the palette is read from
        # - and written back to - the file's raw bytes at this offset whatever
        # the *pixel* pathway is doing. A palette sitting next to compressed
        # graphics is not itself compressed, and round-tripping it through a
        # compressor would relocate and corrupt it.
        # Offset mode keeps pixel reloads from restoring the default palette.
        where = self._format_offset(byte_off)
        return self._load_and_commit_palette(
            PathwayConfig(source=ref, interpret_preset_id=self._palette_preset_id()),
            mode=PaletteMode.OFFSET,
            label=f"load palette from {where}",
            status=lambda n: f"Loaded {n} colors from {where}",
        )

    def _load_palette_from_selection(self) -> None:
        """Palette ▸ Load from Selection: Offset mode at the selected tile."""
        if self._doc is None or self._selected_tile is None:
            return
        self._load_palette_at_offset(self._tile_byte_offset(self._selected_tile))

    def _reload_palette(self) -> None:
        """The palette combo changed: re-express the palette under the new format,
        as one undoable command (a failure reverts the combo).

        The raw-bytes modes re-decode their source. A Custom palette stores its
        colors verbatim and has no source to re-read, so the combo only *relabels*
        it - recording the target format without touching a color. The one-shot
        conversion is the separate Quantize button (:meth:`_quantize_custom_palette`).
        """
        if self._doc is None or self._applying_undo:
            return
        if self._palette_mode is PaletteMode.CUSTOM:
            self._relabel_custom_format()
            return
        if not self._palette_mode.has_source:
            return
        before = self._capture_palette_state()
        result = self._reinterpret_palette()
        if result is None:
            # The load failed (reported): snap the combo back to the live format.
            select_combo_data(self._palette_preset, before.preset_id)
            return
        loaded, cfg = result
        self._commit_palette(
            cfg, loaded, mode=self._palette_mode, label="change palette format"
        )

    def _relabel_custom_format(self) -> None:
        """Record a new target format on a Custom palette, colors untouched.

        A Custom palette holds ARGB verbatim, so a format is only a label here -
        the target the Quantize button snaps colors to, and what the dock shows.
        Committed (so it persists and undoes) but leaving every color exactly as
        it is; converting is the explicit Quantize gesture, not a side effect.
        """
        assert self._doc is not None
        preset_id = self._palette_preset_id()
        self._commit_palette(
            self._placeholder_palette_config(preset_id),
            pipeline.PaletteData(self._doc.palette, PipelineContext(), b""),
            mode=PaletteMode.CUSTOM,
            label="change palette format",
        )

    def _quantize_custom_palette(self) -> None:
        """Snap a Custom palette's stored colors onto the selected format's values.

        The explicit one-shot conversion behind the dock's Quantize button: each
        color is run through the format's round trip (BGR555 drops each channel's
        low bits, an indexed format snaps to its nearest hardware color), so the
        palette lands on values the format can actually hold. Stays Custom - the
        colors remain project-stored ARGB, now merely already-quantized. One
        undoable command; a codec that can't encode is reported and changes
        nothing.
        """
        if self._doc is None or self._palette_mode is not PaletteMode.CUSTOM:
            return
        preset_id = self._palette_preset_id()
        try:
            quantized = pipeline.quantize_palette(
                self._doc.palette, preset_id, self._registry
            )
        except PipelineError as exc:
            self._report(exc)
            return
        format_name = self._palette_preset.currentText()
        if quantized == self._doc.palette:
            # Every color already sits on a value the format can hold - nothing to
            # convert, so leave the undo stack alone rather than push a no-op step.
            self.statusBar().showMessage(
                f"All colors already fit {format_name}; nothing to quantize."
            )
            return
        self._commit_palette(
            self._placeholder_palette_config(preset_id),
            pipeline.PaletteData(quantized, PipelineContext(), b""),
            mode=PaletteMode.CUSTOM,
            label="quantize custom palette",
            status=f"Quantized {len(quantized)} colors to {format_name}.",
        )

    def _reinterpret_palette(
        self,
    ) -> tuple[pipeline.PaletteData, PathwayConfig] | None:
        """Decode the loaded palette source under the format combo's preset;
        ``None`` (reported) on failure, without touching the document.

        A **bounded** read window - Offset mode's length-limited ref into the
        pixel file - is re-floored for the new preset, since the new entry size
        need not divide the old window's byte length. An inline-data ref (an
        emulator state's extracted CGRAM) carries its own bytes rather than being
        re-read from disk, but is re-floored the same way, so a wider/narrower
        format reads a whole number of entries out of it. A whole palette file is
        unbounded and needs none. ``write_enabled`` carries over untouched: where
        a Save lands is the load mode's decision, not this re-decode's.
        """
        # In File mode the palette lives on its PALETTE entry, so re-decode *its*
        # bytes/config, not the graphic's mirror.
        pal_doc = self._palette_doc()
        assert pal_doc is not None
        old = pal_doc.palette_config
        source = old.source
        if source.length is not None and source.data is None:
            try:
                source = self._selection_palette_source(source.path, source.offset)
            except PipelineError as exc:
                self._report(exc)
                return None
            except OSError as exc:
                self._alert(
                    f"Cannot read {old.source.path}: {exc}", title="Celpix - palette"
                )
                return None
            if source is None:
                self._alert(
                    "Not enough data at the palette offset for this format.",
                    title="Celpix - palette",
                )
                return None
        elif source.data is not None:
            # Inline bytes (an emulator state's extracted palette RAM): re-floor
            # the byte length to a whole number of entries under the new format,
            # since the console's own entry size need not divide it. Reading past
            # what the extracted bytes hold makes no sense, so keep the data.
            try:
                entry_size = pipeline.palette_entry_size(
                    self._palette_preset_id(), self._registry
                )
            except PipelineError as exc:
                self._report(exc)
                return None
            avail = len(source.data) - source.offset
            length = avail - (avail % entry_size)
            if length <= 0:
                self._alert(
                    "Not enough palette data for this format.",
                    title="Celpix - palette",
                )
                return None
            source = FileRef(
                source.path, offset=source.offset, length=length, data=source.data
            )
        cfg = PathwayConfig(
            source=source,
            interpret_preset_id=self._palette_preset_id(),
            write_enabled=old.write_enabled,
        )
        try:
            loaded = pipeline.load_palette(cfg, self._registry)
        except PipelineError as exc:
            self._report(exc)
            return None
        return loaded, cfg

    def _fallback_palette(self) -> Palette:
        """The generated palette shown until a real one is loaded — full length.

        Sized to the whole 256 rather than one subpalette's worth: the generator
        puts a contrasting row first, a **grayscale ramp second** and distinct
        colors after, none of which exists at all if only the format's index
        space is asked for (a 4bpp view would stop at 16 — one row, no ramp).
        At full length every subpalette the row spin can reach is populated, so
        single-channel data can be read as a ramp by stepping to row 1, and
        Default → Custom no longer changes the palette's size.
        """
        return Palette.default(FULL_PALETTE_COUNT)
