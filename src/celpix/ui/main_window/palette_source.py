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
from celpix.core.address import format_hex
from celpix.core.capabilities import ContentKind
from celpix.core.context import (
    KEY_PALETTE_ERROR,
    KEY_PALETTE_PRESET,
    KEY_SOURCE_OFFSET,
    PipelineContext,
    hint_info,
)
from celpix.core.document import Document
from celpix.core.errors import Pathway, PipelineError, Stage
from celpix.core.notices import warn
from celpix.core.palette import FULL_PALETTE_COUNT, MISSING_COLOR, Palette
from celpix.pipeline import pipeline
from celpix.pipeline.pathway import PathwayConfig
from celpix.plugins.base import NO_RESHAPE, RAW_CONTAINER, FileRef
from celpix.plugins.detect import detect_container
from celpix.project.workspace import (
    Entry,
    EntryKind,
    PaletteMode,
    PaletteSource,
    data_missing,
    entry_view_bytes,
    export_basename,
    reorders_bytes,
)
from celpix.ui.undo_commands import (
    AddEntryCommand,
    PaletteCommand,
    PaletteConsumerLink,
    PaletteState,
)
from celpix.ui.widgets import (
    ask_save_path,
    select_combo_data,
)

# The session's palette format before any real one has been chosen. RGB888 is
# the plainest, most widely understood encoding, and the right neutral basis for
# a Custom palette forked off the generated default - free ARGB colors with no
# console format behind them. The session default follows the last format
# actually selected from there (:meth:`PaletteSourceMixin._set_session_palette_format`).
# Suffixes that mean "this file is colors, not graphics" — what a dropped file
# is routed by. Deliberately short: a palette is just bytes read through a color
# format, so almost any file *can* hold one, and a long list would start claiming
# files the user meant as pixels. ".col" is the S-CG-CAD palette that ships
# beside the screen and panel files (``scgcad-formats.md``).
# ".tpl" is the one that carries its own color format in a header rather than
# leaving it to be guessed (:mod:`celpix.plugins.builtins.tpl_palette`).
PALETTE_EXTENSIONS = (".pal", ".col", ".tpl")

DEFAULT_SESSION_PALETTE_FORMAT = "preset.palette.rgb888"

# How many sentinel swatches an unreadable palette file opens on: one row of the
# grid - enough to read as "these are not your colors" without pretending to a
# length the file never gave us.
_ERROR_PALETTE_COUNT = 16


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

    def _idle_palette(self) -> Palette:
        """The generated default, shown read-only when nothing at all is open.

        A dock with an empty grid reads as broken rather than idle, and the
        default palette is exactly what a file *would* open on, so it is what
        the panel sits on until something real arrives. Cached because the
        readout re-reads it on every refresh.
        """
        if self._idle_palette_cache is None:
            self._idle_palette_cache = self._fallback_palette()
        return self._idle_palette_cache

    def _shown_palette(self) -> Palette:
        """The colors the dock is showing, whatever is (or isn't) open.

        With a document open this is its palette. With none, it is whichever
        ``.pal`` is being previewed, else the idle default — neither of which
        any edit can reach, since a palette needs a document to be written back
        through. That split is the point of having two accessors: **reads** of
        what is on screen come through here, **writes** through
        :meth:`_palette_doc`, which is ``None`` exactly when nothing on screen
        can hold an edit.
        """
        doc = self._palette_doc()
        if doc is not None:
            return doc.palette
        preview = self._preview_palette
        if preview is not None and preview.doc is not None:
            return preview.doc.palette
        return self._idle_palette()

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
    ) -> None:
        """Point ``graphics`` at the PALETTE entry for ``path``, loading it once.

        Registers the palette entry if the project never had one (a hand-authored
        or older file), builds its live document on first use, then mirrors the
        colors onto ``graphics``. There is no failure return: a bad load raises,
        which the caller (:meth:`_restore_palette_source`) catches and degrades
        to the default palette.
        """
        assert graphics.doc is not None
        entry = self._workspace.find_palette(path)
        if entry is None:
            entry = self._workspace.add_palette(
                path, preset_id, self._detect_palette_container(path)
            )
        if entry.doc is None:
            cfg = self._file_palette_config(
                path, offset, entry.palette_preset_id or preset_id, entry.container_id
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

    # -- previewing a palette file with nothing open ------------------------
    # The dock is never dead: with no document it shows the generated default,
    # or a registered .pal the user opened. Both are read-only - every mode
    # writes an edit back through a document, so with none there is nowhere to
    # put one - and the load modes that need a document are disabled
    # (_sync_palette_mode_items).
    @staticmethod
    def _palette_error(doc: Document | None) -> str | None:
        """Why ``doc`` holds a placeholder palette, or None if it decoded.

        See :data:`~celpix.core.context.KEY_PALETTE_ERROR`: the presence of the
        key is what marks the colors on screen as ours rather than the file's.
        """
        if doc is None:
            return None
        value = doc.palette_ctx.get(KEY_PALETTE_ERROR)
        return value if isinstance(value, str) else None

    def _error_palette(
        self, cfg: PathwayConfig, exc: PipelineError
    ) -> tuple[pipeline.PaletteData, PathwayConfig]:
        """A stand-in for a palette file that won't decode under ``cfg``'s format.

        A ``.pal`` records nothing about its own encoding, so the format is a
        guess until the user says otherwise - and a wrong guess (512 bytes of
        two-byte colors read as three-byte ones) must not be a dead end. Refusing
        the load would leave nothing open, and so no palette on screen whose
        format could be corrected; it opens on a row of the magenta
        missing-colour sentinel instead, and the dock's Import as… dropdown
        re-reads the file under whatever format is named there.

        **Read-only.** These colors are ours, not the file's, and the bytes they
        would be encoded back into were never read - writing them would overwrite
        a palette we failed to understand. The reason rides on the context twice
        over: as a notice, so the files list marks the entry and its tooltip says
        why, and as :data:`~celpix.core.context.KEY_PALETTE_ERROR`, which the
        re-decode reads to hand writability back once the format is right.
        """
        ctx = PipelineContext()
        ctx.set(KEY_PALETTE_ERROR, str(exc))
        warn(
            ctx,
            f"Not readable as {cfg.interpret_preset_id}",
            f"{exc}\nPick the encoding in the palette dock's\nImport as… dropdown.",
            source="host",
        )
        return (
            pipeline.PaletteData(
                Palette([MISSING_COLOR] * _ERROR_PALETTE_COUNT), ctx, b""
            ),
            replace(cfg, write_enabled=False),
        )

    def _load_palette_entry(self, entry: Entry) -> bool:
        """Read a PALETTE entry's file into its own palette-only document.

        The same load :meth:`_link_file_palette` performs for a graphic, minus
        the mirroring. False only when the bytes can't be read at all, which no
        format choice would fix; a decode failure opens on the error palette
        (:meth:`_error_palette`) so the format remains correctable.
        """
        cfg = self._file_palette_config(
            entry.path,
            0,
            entry.palette_preset_id or self._palette_import_preset_id(),
            entry.container_id,
        )
        try:
            loaded = pipeline.load_palette(cfg, self._registry)
        except PipelineError as exc:
            # A format the file itself disagrees with is the *likeliest* way to
            # land here, and it usually fails outright rather than subtly: a
            # 2-byte-entry palette read at three bytes an entry is not a whole
            # number of entries. So the stated format gets its chance before the
            # error palette does, or a file that says what it is would open on a
            # sentinel telling the user to work out what it is.
            retried = self._retry_on_stated_format(entry, cfg)
            if retried is None:
                loaded, cfg = self._error_palette(cfg, exc)
            else:
                loaded, cfg = retried
        except OSError as exc:
            self._alert(f"Cannot read {entry.path}: {exc}", title="celPix - palette")
            return False
        else:
            loaded, cfg = self._apply_palette_preset_hint(entry, loaded, cfg)
        entry.doc = Document.palette_only(loaded.palette, cfg, loaded.ctx, loaded.data)
        return True

    def _apply_palette_preset_hint(self, entry: Entry, loaded, cfg):  # noqa: ANN001
        """Adopt the color format the container read out of the file, if it says.

        The palette-side twin of
        :meth:`~celpix.ui.main_window.session.SessionMixin._apply_pixel_preset_hint`,
        and rarer for the reason :data:`KEY_PALETTE_PRESET` gives: a palette file
        almost never records its own encoding, so the format is normally the
        user's guess and nothing may overrule it. A ``TPL`` names the format in
        its header, and there the file is the better authority — read through the
        wrong one its colors are wrong but never *obviously* so, any bytes being
        some color.

        **Only where the format is still the import default.** That is what
        separates a value nobody chose from a deliberate pick: an entry is
        registered on whatever the dock's Import as… dropdown happens to be on,
        which is a setting about importing in general and not an answer about
        this file. Once the format differs from it, someone has said what they
        want and a stated header does not get to overrule them.
        """
        wanted = str(loaded.ctx.get(KEY_PALETTE_PRESET, "") or "")
        if not wanted or wanted == cfg.interpret_preset_id:
            return loaded, cfg
        adopted = self._adopt_stated_format(entry, cfg, wanted)
        # The header named something this build cannot read, or the format was
        # someone's own pick. What was already decoded stands - a stated format
        # is advice, not a reason to throw away a palette that came out fine.
        return adopted if adopted is not None else (loaded, cfg)

    def _retry_on_stated_format(self, entry: Entry, cfg):  # noqa: ANN001
        """Re-read ``entry`` in the format its container states, or None.

        Reached only from a decode that already failed, which is why the file is
        re-read rather than the hint taken off the failed load: a
        :class:`PipelineError` carries no context, and the container's own
        publication is exactly what is needed to know whether there is anything
        better to try. :func:`~celpix.pipeline.pipeline.inspect_container` runs
        the container stage alone, on a context of its own, and reports a failure
        instead of raising - so the worst case here is that nothing is stated and
        the caller falls through to the error palette as before.
        """
        report = pipeline.inspect_container(cfg, self._registry)
        label = hint_info(KEY_PALETTE_PRESET)[0]
        wanted = next(
            (row.value for row in report.hints if row.name == label),
            "",
        )
        if not wanted or wanted == cfg.interpret_preset_id:
            return None
        return self._adopt_stated_format(entry, cfg, wanted)

    def _adopt_stated_format(self, entry: Entry, cfg, wanted: str):  # noqa: ANN001
        """Re-read ``entry`` as ``wanted``, or None if that is not ours to do.

        The guard both callers share: **only a format nobody chose gives way.**
        An entry is registered on whatever the dock's Import as… dropdown happens
        to be on, which is a setting about importing in general rather than an
        answer about this file, so matching it means nothing has been decided
        here yet. Once the two differ, someone has said what they want and a
        stated header does not get to overrule them.
        """
        if cfg.interpret_preset_id != self._palette_import_preset_id():
            return None
        adopted = self._file_palette_config(entry.path, 0, wanted, entry.container_id)
        try:
            regeared = pipeline.load_palette(adopted, self._registry)
        except (PipelineError, OSError):
            return None
        entry.palette_preset_id = wanted
        return regeared, adopted

    def _preview_palette_file(self, entry: Entry) -> bool:
        """Show a registered ``.pal``'s colors in the dock; False if it won't load.

        **Read-only, and only with no document open.** A palette is written back
        through the document that owns it, so with nothing open there is nowhere
        for an edit to go - the dock shows the file's colors, the readout names
        them and Copy takes them, and every write path stays shut because
        :meth:`_palette_doc` is ``None``. Opening a graphic takes the dock back
        to that graphic's palette.

        Display state, so nothing is pushed onto the undo stack and the entry
        never becomes current: the ``.pal`` is being looked at, not edited.
        """
        if data_missing(entry):
            self._alert(
                f"{entry.name}: file not found - File ▸ Locate missing files "
                "to re-point it.",
                title="celPix - palette",
            )
            return False
        if entry.doc is None and not self._load_palette_entry(entry):
            return False
        self._preview_palette = entry
        self._set_palette_mode(PaletteMode.FILE)
        # Format names the palette on screen, so it follows the file being looked
        # at rather than staying wherever the last graphic left it - and moving it
        # from here re-reads this file (select_combo_data is signal-safe, so
        # landing on it does not).
        select_combo_data(
            self._palette_preset, entry.doc.palette_config.interpret_preset_id
        )
        self._refresh_palette_dock()
        self._files_panel.refresh_entry(entry)  # its row carries the load's notices
        error = self._palette_error(entry.doc)
        self.statusBar().showMessage(
            f"{entry.name}: {error} - pick the encoding in Import as…"
            if error is not None
            else f"{entry.name}: {len(entry.doc.palette)} colors - open pixel data "
            "to edit them."
        )
        return True

    def _clear_palette_preview(self) -> None:
        """Put a previewed ``.pal`` away: back to the generated default.

        Also the hand-off when a document arrives - the dock follows whatever is
        on screen, and a graphic's own palette takes over from a preview that was
        only filling an otherwise empty dock.
        """
        if self._preview_palette is None:
            return
        self._preview_palette = None
        if self._doc is None:
            self._set_palette_mode(PaletteMode.DEFAULT)
            self._refresh_palette_dock()

    def _detect_palette_container(self, path: str) -> str:
        """The container a palette file at ``path`` should be read through.

        The same signature match a graphics file gets when it is opened, over the
        containers that frame a palette rather than the ones that frame graphics
        (:func:`~celpix.plugins.detect.frames`) — so a ``.pal`` still lands on
        plain bytes and an authoring tool's palette lands on the format that
        knows where its colors stop. Correctable afterwards, like any detection.
        """
        return detect_container(self._registry, path, kind=ContentKind.PALETTE)

    @staticmethod
    def _file_palette_config(
        path: str,
        offset: int,
        preset_id: str,
        container_id: str = RAW_CONTAINER,
    ) -> PathwayConfig:
        """The writable pathway a PALETTE entry reads and writes its ``.pal`` with.

        Source and dest are the same file, so a color edit re-encodes into exactly
        the bytes it was read from (the whole file for a plain ``.pal``).

        ``container_id`` is what cuts the colors out of a file that holds more
        than colors — an authoring tool's palette with its metadata block after
        them. It rides on both ends for the same reason a graphic's does: the
        container that unwrapped the file is the one that has to re-wrap it, so
        an edit writes back the colors and leaves that block alone.
        """
        return PathwayConfig(
            source=FileRef(path, offset=offset),
            dest=FileRef(path, offset=offset),
            interpret_preset_id=preset_id,
            container_id=container_id,
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
                self._file_palette_config(
                    palette.path, 0, preset, palette.container_id
                ),
                self._registry,
            )
        except (PipelineError, OSError):
            return []
        return list(loaded.palette.colors)

    def _convert_graphic_to_custom(
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
            entry.doc.palette_base_bytes = b""
            entry.doc.palette_edits = set()
            entry.pending_palette = None
        else:
            # Never loaded: seed the custom colors as the restore a first load reads.
            entry.pending_palette = PaletteSource(colors=list(colors))

    def _relink_graphic_to_file_palette(self, link: PaletteConsumerLink) -> None:
        """Undo of :meth:`_convert_graphic_to_custom`: point the graphic back at the
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
                self._link_file_palette(
                    entry, source.path, source.offset, session.palette_preset_id
                )
                return True
            if session.palette_mode is PaletteMode.EMULATOR and source.path is not None:
                # Re-detect the save state: the palette offset and the console's
                # codec are derived from the file, not carried in the project.
                _fmt, cfg = self._emulator_palette_config(source.path)
            elif source.path is not None:  # an external palette file
                cfg = PathwayConfig(
                    source=FileRef(source.path, offset=source.offset),
                    interpret_preset_id=session.palette_preset_id,
                )
            else:  # palette bytes at an offset in the entry's own coordinates
                ref, writable = self._offset_palette_source(
                    source.offset, session.palette_preset_id, entry=entry
                )
                if ref is None:
                    raise PipelineError(
                        Stage.CONTAINER,
                        Pathway.PALETTE,
                        "not enough data at the palette offset",
                    )
                # Writable exactly as on the interactive Offset load: the bounded
                # ref confines Write to the palette's own bytes, and a reordered
                # source has no file bytes to confine it to.
                cfg = PathwayConfig(
                    source=ref,
                    interpret_preset_id=session.palette_preset_id,
                    write_enabled=writable,
                )
            loaded = pipeline.load_palette(cfg, self._registry)
            doc.palette, doc.palette_ctx = loaded.palette, loaded.ctx
            doc.palette_base_bytes, doc.palette_edits = loaded.data, set()
            doc.palette_config = cfg
            entry.missing_palette = None
            return True
        except (PipelineError, OSError, emustate.StateError) as exc:
            session.palette_mode = PaletteMode.DEFAULT
            entry.missing_palette = None
            self._alert(
                f"{entry.name}: palette not restored, using the default "
                f"palette instead.\n\n{exc}",
                title="celPix - palette",
            )
            return False

    # Shared by the two dialogs that name a .pal - the export that writes one and
    # the open that registers one - so both offer the same filter.
    _PALETTE_FILTER = "Palette files (*.pal *.col);;All files (*)"

    def _prompt_add_palette_file(self) -> None:
        # No .pal filter: palette data is just bytes reinterpreted through the
        # chosen color format, so any file can hold it - a ROM, a save state, a
        # raw dump. Opens any file, like the panel's File source (_open_palette),
        # rather than hiding everything that isn't already named .pal.
        path, _ = QFileDialog.getOpenFileName(self, "Open palette data")
        if path:
            self._open_palette_data(path)

    def _open_palette_data(self, path: str) -> bool:
        """Register ``path`` in Palettes, and preview it when nothing is open.

        The one funnel behind every "open a palette file" gesture - File ▸ Open
        palette data, a dropped ``.pal``, and the dock's File mode when there is
        no document to apply a palette *to*. Returns whether it ended up in the
        dock: with a document open it is registered and nothing more, since the
        Palettes list is then a source of palettes for that view.
        """
        self._add_palette_file(path)
        entry = self._workspace.find_palette(path)
        if entry is None or self._doc is not None:
            return False
        return self._preview_palette_file(entry)

    def _export_palette_file(self) -> None:
        """Palette dock ▸ Export to File…: write the live colors out as a ``.pal``.

        Offered for the modes whose palette exists nowhere else as a file of its
        own - an Offset palette is buried in the pixel file, an Emulator State
        one inside a save state, a Custom one only in the project - so this is
        how those colors become reusable and shareable. The written file is
        registered in the Palettes section straight away, so it is one
        double-click from being re-applied and it travels with the project.

        Written in the format the palette is **read** with - the codec named on
        the document's palette config, which is what the dock's format label is
        showing - and the entry is registered under that same format, so the
        round-trip reads back the colors that went out. A ``.pal`` records
        nothing about its own encoding, so exporting a BGR555 palette as
        anything else would hand the user a file whose bytes don't match the
        format they were just looking at; the status line names what was
        written so it can be told to another tool.
        """
        doc = self._palette_doc()
        if doc is None or not self._palette_mode.is_exportable:
            return
        preset_id = doc.palette_config.interpret_preset_id
        entry = self._workspace.current
        suffix = self._export_offset_suffix()
        suggested = (
            f"{export_basename(entry)}{suffix}.pal" if entry is not None else "palette"
        )
        path = ask_save_path(
            self,
            "Export palette",
            str(Path(self._export_dir(entry)) / suggested),
            self._PALETTE_FILTER,
            ".pal",
        )
        if path is None:
            return
        try:
            pipeline.export_palette(doc, path, self._registry, preset_id)
        except PipelineError as exc:
            self._report(exc)
            return
        except OSError as exc:
            self._alert(f"Cannot write {path}: {exc}", title="celPix - palette")
            return
        added = self._add_palette_file(path, quiet=True, preset_id=preset_id)
        name = Path(path).name
        self.statusBar().showMessage(
            f"Exported palette to {name} as {self._format_label(preset_id)}"
            + (" and added it to Palettes." if added else " (already in Palettes).")
        )

    def _format_label(self, preset_id: str) -> str:
        """A palette format's display name for a message - its id's tail if the
        registry doesn't have it (a project can name a format this build lacks)."""
        try:
            return self._registry.preset(preset_id).name
        except KeyError:
            return preset_id.rsplit(".", 1)[-1]

    def _export_offset_suffix(self) -> str:
        """``_0x001000`` for an Offset-mode palette, else empty.

        Offset is the one exporting mode whose palette bytes live in the very
        file the export basename names, so the offset is what tells the several
        palettes one ROM yields apart - a Custom palette has no offset at all,
        and an Emulator state's belongs to the save state rather than to the
        graphics file the name comes from. Plain hex rather than the address
        box's format: a bank address carries a colon, which Windows won't take
        in a filename.
        """
        if self._doc is None or self._palette_mode is not PaletteMode.OFFSET:
            return ""
        return f"_{format_hex(self._doc.palette_config.source.offset)}"

    def _add_palette_file(
        self, path: str, *, quiet: bool = False, preset_id: str | None = None
    ) -> bool:
        """Register ``path`` in the files list's Palettes section; False if it
        already was.

        The shared entry point for File ▸ Open palette data, a dropped ``.pal``
        and the dock's palette export. Registration only - putting one on screen
        is :meth:`_open_palette_data`, applying it to a graphic is the list's
        double-click. The entry starts on the format the dock's Import as…
        dropdown is on - or ``preset_id``, for a caller that knows the file's
        encoding because it just wrote it - and tracks the format dropdown
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
            container_id=self._detect_palette_container(path),
            palette_preset_id=preset_id or self._palette_import_preset_id(),
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
        codec, so the two agree afterwards. With nothing open there is nothing
        to apply it *to*, so the same gesture previews its colors instead.
        """
        if self._doc is None:
            self._preview_palette_file(entry)
            return
        if data_missing(entry):
            self._alert(
                f"{entry.name}: file not found - File ▸ Locate missing files "
                "to re-point it.",
                title="celPix - palette",
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
        the mode dropdown can revert instead of lying about the source.

        With nothing open the file is registered and previewed read-only rather
        than applied to anything.
        """
        path, _ = QFileDialog.getOpenFileName(self, "Open palette")
        if not path:
            return False
        if self._doc is None:
            return self._open_palette_data(path)
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
                doc.palette, doc.palette_ctx, doc.palette_base_bytes
            )
            cfg = doc.palette_config
            edits = frozenset(doc.palette_edits)
        else:
            cfg = self._file_palette_config(path, 0, preset_id, entry.container_id)
            try:
                loaded = pipeline.load_palette(cfg, self._registry)
            except PipelineError as exc:
                # Same rescue as the standalone load: the graphic renders the
                # sentinel rather than the gesture being refused, and the Format
                # row - live here, because there *is* a document - re-reads the
                # file under the right encoding.
                loaded, cfg = self._error_palette(cfg, exc)
            entry.doc = Document.palette_only(
                loaded.palette, cfg, loaded.ctx, loaded.data
            )
            edits = frozenset()
        error = self._palette_error(entry.doc)
        self._commit_palette(
            cfg,
            loaded,
            mode=PaletteMode.FILE,
            label=label,
            status=(
                f"{entry.name}: {error} - pick the encoding in the Format row"
                if error is not None
                else status(len(loaded.palette))
            ),
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
        fmt, region = emustate.locate_palette(data)
        # A locator extracts the palette out of a container/memory image rather
        # than pointing at a file offset, so those bytes feed the pipeline direct.
        length = min(
            len(region.data),
            pipeline.palette_read_bytes(region.count, region.preset_id, self._registry),
        )
        ref = FileRef(path, offset=0, length=length, data=region.data)
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
            self._alert(str(exc), title="celPix - emulator state")
            return False
        except OSError as exc:
            self._alert(f"Cannot read {path}: {exc}", title="celPix - emulator state")
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
            base_bytes=doc.palette_base_bytes,
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
            self._doc.palette_base_bytes = state.base_bytes
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
            entry = self._workspace.add_palette(
                path, state.preset_id, self._detect_palette_container(path)
            )
        if entry is None:
            return
        if entry.doc is None:
            entry.doc = Document.palette_only(
                state.palette, state.config, state.ctx, state.base_bytes
            )
        else:
            entry.doc.palette = state.palette
            entry.doc.palette_config = state.config
            entry.doc.palette_ctx = state.ctx
            entry.doc.palette_base_bytes = state.base_bytes
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
                    base_bytes=loaded.data,
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
            # Nothing open: File previews a .pal read-only and Default goes back
            # to the generated one. The rest read out of (or write into) a
            # document, and their dropdown items are disabled
            # (_sync_palette_mode_items), so this is only a backstop.
            if mode is PaletteMode.FILE and self._open_palette():
                return
            if mode is PaletteMode.DEFAULT:
                self._clear_palette_preview()
                return
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
        assert entry.doc is not None
        owner = self._palette_offset_owner(entry)
        view = self._reordered_view(owner) if owner is not None else None
        if view is not None:
            data, base = view
            if owner is not None and owner.reshape_id != NO_RESHAPE:
                base = 0
            return (data, base), base + len(data)
        paths = entry.doc.pixel_config.source.paths
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
        assert entry is not None and entry.doc is not None
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
        # The whole file list, as the pixel pathway reads it: the offset
        # addresses the joined buffer, so a several-chip region cannot be
        # answered from its first chip alone.
        paths = entry.doc.pixel_config.source.paths
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
        reshape), :meth:`_file_palette_config` for a File-mode ``.pal`` (whole
        file, writable pathway).

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
        if self._doc is None:
            return False
        src = self._doc.pixel_config.source
        try:
            ref, writable = self._offset_palette_source(byte_off)
        except PipelineError as exc:
            self._report(exc)
            return False
        except OSError as exc:
            self._alert(f"Cannot read {src.path}: {exc}", title="celPix - palette")
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

    def _reload_palette(self) -> None:
        """The palette combo changed: re-express the palette under the new format,
        as one undoable command (a failure reverts the combo).

        The raw-bytes modes re-decode their source. A Custom palette stores its
        colors verbatim and has no source to re-read, so the combo only *relabels*
        it - recording the target format without touching a color. The one-shot
        conversion is the separate Quantize button (:meth:`_quantize_custom_palette`).

        With no graphic open the dock is showing a palette file on its own, which
        has no document to re-decode through - that goes through
        :meth:`_reload_previewed_palette`, which re-reads the file instead.
        """
        if self._applying_undo:
            return
        if self._doc is None:
            self._reload_previewed_palette()
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

    def _reload_previewed_palette(self) -> None:
        """Re-read the previewed palette file under the Format dropdown.

        The document-less half of :meth:`_reload_palette`. A previewed ``.pal``
        has no document behind it, so the ordinary re-decode - which works
        through :meth:`_palette_doc` - has nothing to run on; the format still
        has to be changeable, because a file that decoded wrong (the error
        palette) is otherwise stuck with no graphic to be corrected through.

        Re-read rather than re-decoded: a preview is read-only, so its document
        holds nothing an edit could have put there and nothing is lost by going
        back to the file. Display state, so no undo step - the same reason
        previewing one isn't a history step either.
        """
        entry = self._preview_palette
        if entry is None:
            return
        entry.palette_preset_id = self._palette_preset_id()
        entry.doc = None
        self._preview_palette_file(entry)

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

        A **bounded** read window - Offset mode's length-limited ref - is
        re-floored for the new preset, since the new entry size need not divide
        the old window's byte length; the re-floor goes through the same source
        builder as the original load (:meth:`_offset_palette_source`), so a
        buffer-backed window is re-cut from the owner's buffer with its base
        intact, not re-read from the file. An inline-data ref that is *not* an
        Offset window (an emulator state's extracted CGRAM) carries its own bytes
        and is re-floored over them. A whole palette file is unbounded and needs
        none. ``write_enabled`` carries over untouched: where a Save lands is the
        load mode's decision, not this re-decode's.
        """
        # In File mode the palette lives on its PALETTE entry, so re-decode *its*
        # bytes/config, not the graphic's mirror.
        pal_doc = self._palette_doc()
        assert pal_doc is not None
        old = pal_doc.palette_config
        source = old.source

        def refloored(build: Callable[[], FileRef | None]) -> FileRef | None:
            """Run a window builder, reporting its failures; None ends the redecode."""
            try:
                ref = build()
            except PipelineError as exc:
                self._report(exc)
                return None
            except OSError as exc:
                self._alert(
                    f"Cannot read {old.source.path}: {exc}", title="celPix - palette"
                )
                return None
            if ref is None:
                self._alert(
                    "Not enough data at the palette offset for this format.",
                    title="celPix - palette",
                )
            return ref

        offset = source.offset
        if self._palette_mode is PaletteMode.OFFSET:
            source = refloored(lambda: self._offset_palette_source(offset)[0])
            if source is None:
                return None
        elif source.length is not None and source.data is None:
            path = source.path
            source = refloored(lambda: self._file_palette_source(path, offset))
            if source is None:
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
                    title="celPix - palette",
                )
                return None
            source = FileRef(
                source.path, offset=source.offset, length=length, data=source.data
            )
        cfg = PathwayConfig(
            source=source,
            interpret_preset_id=self._palette_preset_id(),
            # An error palette is read-only because its colors aren't the file's
            # (:meth:`_error_palette`); re-decoding it is precisely the fix, so
            # writability comes back with the read rather than being inherited
            # from the failure.
            write_enabled=old.write_enabled or self._palette_error(pal_doc) is not None,
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
        forking Default → Custom keeps the palette exactly the size it was.
        """
        return Palette.default(FULL_PALETTE_COUNT)
