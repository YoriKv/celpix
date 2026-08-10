"""Palette files coming in and going out.

The two directions a palette crosses the boundary of a session, and they are one
module because they are the same round trip. A file is **registered** in the
files list's Palettes section, where it has a stable home and travels with the
project; an exported one is registered the moment it is written, so it is one
double-click from being applied again.

Registration is identity by path, so re-adding a file is a no-op rather than a
duplicate, and every gesture that names a palette file - File ▸ Open palette
data, a dropped ``.pal``, the dock's export - funnels through the one entry point
that enforces it.

The **format** is what makes the round trip more than file handling. A ``.pal``
records nothing about its own encoding, so an export writes in the codec the
palette is being *read* with and stamps that codec on the entry it registers;
re-applying the file decodes with the format the entry remembers rather than
wherever the dropdown has moved since. Get that wrong in either direction and the
user is handed a file whose bytes do not match the colours they were just looking
at.

What is not here: applying a registered palette to the graphic on screen, which
is a mode change and ends in the commit
(:mod:`~celpix.ui.main_window.palette_source`); reading a palette out of a
position in an entry's own bytes
(:mod:`~celpix.ui.main_window.palette_offset`); and the codecs themselves, which
are the Qt-free pipeline's.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QFileDialog,
)

from celpix.core.address import format_hex
from celpix.core.errors import PipelineError
from celpix.pipeline import pipeline
from celpix.project.workspace import (
    Entry,
    EntryKind,
    PaletteMode,
    data_missing,
    export_basename,
)
from celpix.ui.undo_commands import (
    AddEntryCommand,
)
from celpix.ui.widgets import (
    ask_save_path,
)


class PaletteTransferMixin:
    """Registering palette files in the session, and writing one back out.

    A slice of :class:`~celpix.ui.main_window.window.MainWindow`, not a
    standalone object: it reads and writes the window's own widgets and its
    single live ``_doc``. See the module docstring for what it owns, and the
    package docstring for why these are mixins.
    """

    # The **export** dialog's filter, and only its own: it is naming a file it is
    # about to write, so suggesting the conventional suffixes is a help. The open
    # side deliberately offers none (see :meth:`_prompt_add_palette_file`) -
    # palette data is bytes read through a codec, so any file could hold some and
    # a filter there would hide the ones that do.
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
        (:meth:`~...palette_source.PaletteSourceMixin._sync_palette_entry_format`);
        identity is the path, so re-adding
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
