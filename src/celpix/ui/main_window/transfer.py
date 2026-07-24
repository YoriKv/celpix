"""Getting pictures in and out: PNG/raw export, PNG import, drag-and-drop.

Export renders an entry through its own codec and palette, and can act on an
entry that was never shown - the window loads it on demand. Import is the
reverse and shares the clipboard's pathway
(:mod:`celpix.pipeline.importer`), so an image arrives identically whether it
came off disk or through a paste: quantized to the active subpalette, cut on the
view's arrangement, stamped as a block.

Drag-and-drop lives here because a drop is a transfer too, and it decides *what
kind* of file arrived: a project replaces the workspace, a PNG is imported into
the graphic on screen, a ``.pal`` registers as a palette, anything else opens as
pixel data.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import (
    QAction,
    QDragEnterEvent,
    QDropEvent,
    QImage,
)
from PySide6.QtWidgets import (
    QFileDialog,
)

from celpix.core.errors import PipelineError
from celpix.pipeline import importer
from celpix.project import projectfile
from celpix.project.workspace import (
    Entry,
    EntryKind,
    export_basename,
    exportable_entries,
)
from celpix.ui import clipboard, export


class TransferMixin:
    """Image/raw export, PNG import, and drag-and-drop.

    A slice of :class:`~celpix.ui.main_window.window.MainWindow`, not a
    standalone object: it reads and writes the window's own widgets and its
    single live ``_doc``. See the module docstring for what it owns, and the
    package docstring for why these are mixins.
    """

    def _build_export_menu(self, file_menu) -> None:  # noqa: ANN001 - QMenu
        """File ▸ Export ▸ the export targets, rendered from the *current* view.

        Two single-entry exports (the current file or slice → one PNG or one raw
        binary), plus two bulk exports to a folder: every slice of the current
        file, or the whole project. The bulk exports deliberately skip a file that
        has slices - the slices are what's worth exporting - so a sliced file
        leaves as an image only when it is itself the current entry (the
        single-entry PNG). Enabled state depends on the current entry and the
        list contents, so it is refreshed each time the File menu opens.
        """
        export_menu = file_menu.addMenu("Export")

        self._export_png_action = QAction("Export as PNG…", self)
        self._export_png_action.setToolTip(
            "Export as an indexed PNG - index 0 is transparent"
        )
        self._export_png_action.triggered.connect(
            lambda: self._export_png(self._workspace.current)
        )
        export_menu.addAction(self._export_png_action)

        self._export_raw_action = QAction("Export Raw…", self)
        self._export_raw_action.setToolTip("Export decoded bytes as a raw binary")
        self._export_raw_action.triggered.connect(
            lambda: self._export_raw(self._workspace.current)
        )
        export_menu.addAction(self._export_raw_action)

        export_menu.addSeparator()

        self._export_slices_action = QAction("Export File's Slices as PNGs…", self)
        self._export_slices_action.setToolTip("Export each slice of this file as a PNG")
        self._export_slices_action.triggered.connect(
            lambda: self._export_file_slices(self._workspace.current)
        )
        export_menu.addAction(self._export_slices_action)

        self._export_all_action = QAction("Export All as PNGs…", self)
        self._export_all_action.setToolTip(
            "Export every slice and unsliced file as a PNG"
        )
        self._export_all_action.triggered.connect(self._export_project)
        export_menu.addAction(self._export_all_action)

        # Enabled state tracks the current entry and the list, both of which move
        # without a menu rebuild - recompute it whenever the File menu opens.
        file_menu.aboutToShow.connect(self._sync_export_actions)

    def _sync_export_actions(self) -> None:
        """Enable each Export action for what the current state can produce."""
        current = self._workspace.current
        has_doc = current is not None and current.doc is not None
        self._export_png_action.setEnabled(has_doc)
        self._export_raw_action.setEnabled(has_doc)
        self._export_slices_action.setEnabled(
            current is not None
            and current.kind is EntryKind.FILE
            and bool(self._workspace.slices_of(current))
        )
        self._export_all_action.setEnabled(bool(exportable_entries(self._workspace)))

    # -- drag & drop -------------------------------------------------------
    @staticmethod
    def _dropped_paths(event: QDragEnterEvent | QDropEvent) -> list[str]:
        """All local-file paths in a drag payload (empty when it has none)."""
        mime = event.mimeData()
        if not mime.hasUrls():
            return []
        return [path for url in mime.urls() if (path := url.toLocalFile())]

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # Qt override
        # Only offer to accept when the drag carries local files; ignore otherwise
        # so the user gets accurate feedback (no drop cursor for non-file drags).
        if self._dropped_paths(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:  # Qt override
        paths = self._dropped_paths(event)
        if not paths:
            return
        event.acceptProposedAction()
        # A .celpix is a whole session, not a file to add: opening one *replaces*
        # the workspace, so it claims the entire drop rather than racing the other
        # files (anything loaded alongside would either be discarded by the replace
        # or silently land in the new project). First one wins; the rest are named
        # in the status bar so the drop doesn't fail quietly.
        project = next(
            (p for p in paths if p.lower().endswith(projectfile.PROJECT_EXTENSION)),
            None,
        )
        if project is not None:
            self._load_project(project)
            if len(paths) > 1:
                self.statusBar().showMessage(
                    f"Opened project {Path(project).name}; the other "
                    f"{len(paths) - 1} dropped file(s) were ignored."
                )
            return
        # A PNG isn't a binary to read graphics out of - there is nothing to
        # interpret it as - so dropping one *imports* it into the open graphic
        # instead of joining the list. It claims the drop for the same reason a
        # project does: an import lands at one anchor, so a second image could
        # only overwrite the first.
        image = next((p for p in paths if p.lower().endswith(".png")), None)
        if image is not None:
            self._import_dropped_png(image)
            if len(paths) > 1:
                self.statusBar().showMessage(
                    f"Imported {Path(image).name}; the other "
                    f"{len(paths) - 1} dropped file(s) were ignored."
                )
            return
        for path in paths:  # every file becomes an entry; the last one is shown
            # A .pal is palette data, not pixels - it lands in the Palettes
            # section (open it via the dialog to force the pixel reading).
            if path.lower().endswith(".pal"):
                self._add_palette_file(path)
            else:
                self._load_pixel(path)

    # -- export --------------------------------------------------------------
    _PNG_FILTER = "PNG image (*.png)"
    _RAW_FILTER = "Raw binary (*.bin);;All files (*)"

    def _export_dir(self, entry: Entry | None = None) -> str:
        """The directory an export dialog opens in.

        When a project is loaded, exports default beside the **project file** (the
        session's home, and where the user is most likely gathering its output);
        otherwise the dialog opens on the entry's own folder. Only the directory
        is chosen here - the caller adds the suggested filename.
        """
        if self._project_path is not None:
            return str(Path(self._project_path).parent)
        if entry is not None:
            return str(Path(entry.path).parent)
        return ""

    def _ensure_entry_loaded(self, entry: Entry | None) -> bool:
        """True once ``entry`` holds a decoded document, loading it if needed.

        A single-entry export can name any entry in the list - from its context
        menu, one that was never activated and so has no document yet. Loading is
        entry-scoped (it doesn't disturb the current view), and a failure is
        reported with a modal: unlike a bulk run there is nothing else to
        summarize alongside it.
        """
        if entry is None:
            return False
        return entry.doc is not None or self._load_entry(entry)

    def _export_png(self, entry: Entry | None) -> None:
        """Export ``entry`` (the current one, or the one the files list named) as
        a single PNG.

        The entry is the *explicit* selection, so a file with slices is exported
        here even though the bulk exports skip it - choosing it alone is exactly
        the "unless explicitly selected" case."""
        if not self._ensure_entry_loaded(entry):
            return
        default = str(Path(self._export_dir(entry)) / f"{export_basename(entry)}.png")
        path, _ = QFileDialog.getSaveFileName(
            self, "Export as PNG", default, self._PNG_FILTER
        )
        if not path:
            return
        if not path.lower().endswith(".png"):
            path += ".png"
        try:
            image = export.document_image(entry.doc, self._registry)
        except PipelineError as exc:
            self._report(exc)
            return
        if export.save_png(image, path):
            self.statusBar().showMessage(f"Exported {entry.name} to {path}.")
        else:
            self._alert(f"Could not write {path}.", title="Celpix - export")

    def _export_raw(self, entry: Entry | None) -> None:
        """Export ``entry``'s decoded bytes as a raw binary."""
        if not self._ensure_entry_loaded(entry):
            return
        default = str(Path(self._export_dir(entry)) / f"{export_basename(entry)}.bin")
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Raw", default, self._RAW_FILTER
        )
        if not path:
            return
        try:
            export.save_raw(entry.doc, path)
        except OSError as exc:
            self._alert(f"Could not write {path}: {exc}", title="Celpix - export")
            return
        self.statusBar().showMessage(
            f"Exported {len(entry.doc.pixel_data)} bytes of {entry.name} to {path}."
        )

    def _export_file_slices(self, entry: Entry | None) -> None:
        """Export every slice of ``entry``'s file as its own PNG into a folder."""
        if entry is None or entry.kind is not EntryKind.FILE:
            return
        slices = self._workspace.slices_of(entry)
        if not slices:
            return
        folder = QFileDialog.getExistingDirectory(
            self, "Export slices to folder", self._export_dir(entry)
        )
        if folder:
            self._bulk_export_png(slices, folder)

    def _export_project(self) -> None:
        """File ▸ Export ▸ Export All as PNGs: the whole project → a folder.

        Renders every slice and every unsliced file; a file that has slices is
        skipped (its slices are what's worth exporting) - see
        :func:`~celpix.project.workspace.exportable_entries`."""
        entries = exportable_entries(self._workspace)
        if not entries:
            return
        folder = QFileDialog.getExistingDirectory(
            self, "Export project to folder", self._export_dir()
        )
        if folder:
            self._bulk_export_png(entries, folder)

    def _bulk_export_png(self, entries: list[Entry], folder: str) -> None:
        """Render each of ``entries`` to ``folder``/<name>.png and summarize.

        Basenames are de-duplicated within the run (two slices of one file can
        share a name), and each entry is loaded quietly on demand - a
        never-activated one has no document yet. Every failure (won't load, won't
        decode, won't write) is collected and reported once at the end rather than
        as a dialog per bad entry."""
        used: set[str] = set()
        written = 0
        failed: list[str] = []
        for entry in entries:
            if entry.doc is None and not self._load_entry(entry, quiet=True):
                failed.append(entry.name)
                continue
            try:
                image = export.document_image(entry.doc, self._registry)
            except PipelineError:
                failed.append(entry.name)
                continue
            name = self._unique_export_name(export_basename(entry), used)
            if export.save_png(image, str(Path(folder) / f"{name}.png")):
                written += 1
            else:
                failed.append(entry.name)
        message = f"Exported {written} image(s) to {folder}."
        if failed:
            message += f" {len(failed)} could not be exported."
        self.statusBar().showMessage(message)
        if failed:
            self._alert(
                f"{len(failed)} item(s) could not be exported (unreadable, or a "
                "codec that couldn't decode them).",
                title="Celpix - export",
                detail="\n".join(failed),
            )

    @staticmethod
    def _unique_export_name(base: str, used: set[str]) -> str:
        """``base``, suffixed ``_2``/``_3``/… until it is unused; records it."""
        name = base
        counter = 2
        while name in used:
            name = f"{base}_{counter}"
            counter += 1
        used.add(name)
        return name

    # -- image import --------------------------------------------------------
    _IMPORT_FILTER = "PNG image (*.png);;All files (*)"

    def _import_png_into(self, entry: Entry | None) -> None:
        """Files list ▸ Import from PNG…: an image over ``entry`` from its start.

        The entry becomes the current view first - the import is fitted to the
        palette, bit depth and arrangement the view is showing, and the result
        has to be visible and undoable in the same session as any other edit. The
        image then lands as a block anchored at tile 0, so the file reads back
        exactly as the picture looks.
        """
        if entry is None or entry.kind not in (EntryKind.FILE, EntryKind.SLICE):
            return
        if entry is not self._workspace.current:
            self._activate_entry(entry)
            if entry is not self._workspace.current:
                return  # load failed; _activate_entry has already reported it
        if self._doc is None:
            return
        path = self._choose_import_png()
        if path is None:
            return
        self._set_offset(0)  # the destination has to be on screen to land on it
        self._import_png_at(self._offset, path)

    def _import_png_here(self) -> None:
        """Canvas ▸ Import from PNG…: an image over the selection's anchor."""
        if self._doc is None:
            return
        path = self._choose_import_png()
        if path is None:
            return
        self._import_png_at(self._stamp_anchor(), path)

    def _import_dropped_png(self, path: str) -> None:
        """A PNG dropped on the window, imported where the canvas menu would.

        With nothing open there is no view to fit the image to, so the drop is
        refused with the reason rather than falling back to adding the PNG to
        the list - which is the very thing dropping an image no longer does.
        """
        if self._doc is None:
            self.statusBar().showMessage(
                f"Open a file or slice first - {Path(path).name} is imported into "
                "the graphic on screen, not added to the list."
            )
            return
        self._import_png_at(self._stamp_anchor(), path)

    def _choose_import_png(self) -> str | None:
        """Ask for the image to import; None if the dialog was cancelled."""
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Import from PNG",
            self._export_dir(self._workspace.current),
            self._IMPORT_FILTER,
        )
        return path or None

    def _import_png_at(self, anchor: int, path: str) -> None:
        """Fit the image at ``path`` into this view and stamp it from ``anchor``.

        The same import pathway a cross-application paste uses, so an image
        arrives identically whether it came through the clipboard or off disk:
        quantized to the active subpalette, cut on the view's arrangement, and
        written as a block of the image's own width. Pixels the image doesn't
        cover - the remainder of an edge tile whose size isn't a whole number of
        tiles - keep whatever the file already holds.
        """
        assert self._doc is not None
        image = QImage(path)
        if image.isNull():
            self._alert(f"Could not read {path} as an image.", title="Celpix - import")
            return
        incoming = importer.import_argb(
            clipboard.image_to_argb(image), self._import_target()
        )
        if not incoming.tiles:
            self.statusBar().showMessage(f"{Path(path).name} has no pixels to import.")
            return
        written = self._stamp_block(anchor, incoming, "import image")
        if not written:
            self.statusBar().showMessage("Nothing imported - no room at this offset.")
            return
        message = f"Imported {self._tiles_label(written)} from {Path(path).name}"
        if len(incoming.tiles) > written:
            clipped = len(incoming.tiles) - written
            message += f" ({clipped} clipped at the end of the data)"
        note = self._fit_note(incoming.report)
        self.statusBar().showMessage(message + (f" - {note}." if note else "."))
