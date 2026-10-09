"""The container dialog: which files an entry reads, and how they are unwrapped.

Opening a file picks its container from the file's own signature
(:mod:`celpix.plugins.detect`), which is right nearly always and wrong in the
cases only a person can settle — an interleaved SNES image looks exactly like a
plain one, a headerless dump is named ``.nes``, a user's own container knows a
wrapper celPix doesn't. This dialog is that override, and it lists *every*
registered container rather than only the plausible ones: the point of reaching
for it is that detection already had its turn.

The file-level **Reshape** choice lives here too: it is the region-scoped byte
reordering the joined bytes go through after the container (a plane-per-chip
split, a ROM-pair word interleave), and this dialog already owns how the
region's bytes are assembled — which files, in what order, through what
wrapper. It has no signature to detect, so unlike the container there is no
"(detected)" marker to show.

**Compression** follows it, for the file that is one compressed blob lifted
out of a ROM whole: rather than slicing a region that is the entire file, the
file itself decompresses on load and is re-packed on save. The choice is the
same stage a slice's dialog offers, and so is its cost — the view's positions
are then positions in the unpacked stream, which no byte of the file has.

The three rows are the ones New File… asks about a file that does not exist
yet, and are the same component (:class:`~celpix.ui.file_stages.FileStageRows`):
here every registered plugin is offered and a stage with no save half opens the
file read-only, where a new file can only be made through stages that write.

It is also where a region's **file list** is edited: a graphics region is not
always one file, and only the user can state the order of the chips it is joined
from (:class:`~celpix.ui.path_list_editor.PathListEditor`).

The region's **size** is the last thing here, and the one answer that changes
the file rather than how it is read. It counts the units the New File dialog
counts — tiles, cells, a palette's colours — because it is the same question
asked of a file that already exists: a bank that was carved two tiles short, a
map that needs another screen's worth of room. What it does to the file is
:func:`~celpix.pipeline.pipeline.resize_file`.

A **total**, where New File states a grid across and down. A file has a length;
a grid is the view's way of looking at one, and the two only agree when the
length is a whole number of rows. Seeding a grid from a region that is not
(100 tiles is not a whole number of 16-wide rows) would round the file up
merely by opening this dialog — and since a resize **writes**, that is not
something a dialog reached for the container can be allowed to do on the way
past. A count seeds to exactly what the region holds, so it asks for nothing
until it is changed (:meth:`ContainerDialog.resize_units`).

A region joined from several files has no size to change here at all — its
boundaries are the lengths its files have.

Applying either answer re-reads the entry, so the caller is responsible for
handling unsaved edits before applying it.
"""

from __future__ import annotations

from dataclasses import dataclass
from os.path import basename
from typing import Any

from PySide6.QtWidgets import QDialog, QFormLayout, QLabel, QWidget

from celpix.core.capabilities import ContentKind
from celpix.plugins.detect import detect_container
from celpix.plugins.registry import Registry
from celpix.project.entry import FileStages
from celpix.ui.file_stages import FileStageRows
from celpix.ui.path_list_editor import PathListEditor
from celpix.ui.size_row import GROWTH_TIPS, UnitCountRow
from celpix.ui.theme import WARNING_INK, set_ink
from celpix.ui.widgets import (
    dialog_buttons,
    run_modal,
)

__all__ = ["ContainerDialog", "ContainerEdit"]

_FILES_TIP = "Files joined end to end to form this entry, in order"

# A total rather than a grid across and down, which is why these are not the
# New File dialog's tips: see :meth:`ContainerDialog._build_size_row`.
_SIZE_TIPS = {
    kind: f"Total {what}\n{GROWTH_TIPS[kind]}"
    for kind, what in (
        (ContentKind.PIXELS, "tiles in the file"),
        (ContentKind.TILEMAP, "cells in the map"),
        (ContentKind.PALETTE, "colors in the palette"),
    )
}


@dataclass(frozen=True)
class ContainerEdit:
    """What the dialog was left holding: the file's stages and its files.

    Together, because the dialog settles them and the caller applies them as
    one change — the file list decides which bytes there are, the stages how
    they are unwrapped, reordered and unpacked
    (:class:`~celpix.project.entry.FileStages`), and any one alone leaves the
    entry re-read.

    ``units`` is the odd one out and is **not** part of that change: the above
    only decide how bytes are read and so can be undone by putting them back,
    while a resize rewrites the file. It is carried here because the dialog is
    where it was asked for, and it is ``None`` — the ordinary case — wherever
    no size was asked for at all, so a caller applying the rest does nothing to
    the file (:meth:`ContainerDialog.resize_units`).
    """

    stages: FileStages
    paths: tuple[str, ...]
    units: int | None = None


class ContainerDialog(QDialog):
    def __init__(
        self,
        registry: Registry,
        *,
        paths: tuple[str, ...] | list[str],
        stages: FileStages | None = None,
        kind: ContentKind = ContentKind.PIXELS,
        codec_id: str = "",
        units: int = 0,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._registry = registry
        self._kind = kind
        self._files = PathListEditor(paths, _FILES_TIP)
        self.setWindowTitle(f"Edit File Container - {basename(self.paths()[0])}")

        # Every registered plugin, not only the plausible ones: the point of
        # reaching for this dialog is that detection already had its turn. A
        # stage with no save half is allowed and opens the file read-only, which
        # the note below says before the user edits rather than after. Both
        # answers decide whether there is a write half to put resized bytes
        # back through, so the size row follows them as the note does.
        self._stages = FileStageRows(
            registry, writable_only=False, on_change=self._on_stage_change
        )
        self._stages.fill(kind, stages or FileStages())
        self._container = self._stages.container  # what detection is marked on
        self._detected = ""
        # What the size row's count was measured under. A different scheme
        # unpacks to a different length, so until it is applied the count
        # describes a reading the file is not being given (``_resize_blocked``).
        self._compression_before = self._stages.stages().compression_id

        # **A total count, not a grid**, which is the one place this differs
        # from the New File dialog's otherwise identical row. A file has a
        # length; a grid is the view's way of looking at it, and the two only
        # agree when the length happens to be a whole number of rows. A count
        # seeds to exactly what the region holds, so opening this dialog to
        # change a container asks for no resize at all, by arithmetic rather
        # than by special case (:class:`~celpix.ui.size_row.UnitCountRow`).
        self._count = UnitCountRow(
            kind, codec_id, registry, units, _SIZE_TIPS[kind], self._refresh_size
        )
        self._size_units = self._count.spin
        self._size_caption = self._count.caption
        self._size = self._count.note

        self._note = QLabel()
        self._note.setWordWrap(True)
        set_ink(self._note, WARNING_INK)
        self._refresh_note()

        files_caption = QLabel("Files:")
        files_caption.setToolTip(_FILES_TIP)

        form = QFormLayout(self)
        # The list spans the form's full width rather than sitting in its field
        # column: a path is long, and the caption column would take a quarter of
        # the room the paths need from every row at once.
        form.addRow(files_caption)
        form.addRow(self._files)
        # A palette file gets the container row alone (``FileStageRows``).
        self._stages.add_to(form)
        self._stages.fill(kind, self._stages.stages())  # hides a palette's rows
        # Last, because it is the one row that changes the file rather than how
        # the rows above it are read.
        self._size_caption.setBuddy(self._size_units)
        form.addRow(self._size_caption, self._count.field)
        form.addRow("", self._size)
        form.addRow(self._note)
        dialog_buttons(self, form)

        # A row is mostly path, and a path is long. Opening at the width of the
        # widgets' own hints would elide every one of them from the start, so ask
        # for room measured in characters — which follows the font, unlike a
        # pixel count.
        self.setMinimumWidth(self.fontMetrics().averageCharWidth() * 72)
        # Appending or dropping a file is what decides whether there is a size
        # to change at all, and the first file is what detection is marked for.
        self._files.paths_changed.connect(self._refresh_files)
        self._refresh_files()

    # -- the size -----------------------------------------------------------
    def _resize_blocked(self) -> str:
        """Why this region's size cannot be changed, or ``""`` where it can.

        The multi-file rule is :func:`~celpix.pipeline.pipeline._deposit`'s, said
        here so the spin is dead rather than the user meeting it as a failed
        write: the boundaries between joined files are the lengths those files
        have, and nothing else records which bytes belong to which chip.
        """
        count = len(self.paths())
        if count > 1:
            return (
                f"A region joined from {count} files has no size to "
                "change here: the boundary between two files is the length of "
                "the first, so moving it would move every byte after it into "
                "the wrong file."
            )
        if self._count.bytes_before is None:
            return "No format is registered to measure this file in."
        fixed = self._stages.size_fixed_reason()
        if fixed:
            return fixed
        if self._stages.stages().compression_id != self._compression_before:
            # The count was measured under the scheme the file opened with, and
            # another unpacks to another length — the slice dialog's rule.
            return "Apply the new compression first."
        return ""

    def _refresh_size(self, *_args: object) -> None:
        """Restate what the spin comes to in bytes, and grey it where it cannot
        be acted on."""
        blocked = self._resize_blocked()
        if blocked:
            self._count.show_reason(blocked)
        else:
            self._count.show_bytes()

    def size_units(self) -> int:
        """Tiles, cells or colors the size row is stating."""
        return self._count.units()

    def resize_units(self) -> int | None:
        """The count to resize to, or ``None`` where no resize was asked for.

        ``None`` for a region that cannot be resized at all, and for a size that
        works out to the length the file already has — which is what the spin
        opens on, so a dialog reached for the container alone asks for nothing
        here. Compared in **bytes** rather than units, because a packed palette
        rounds up to a whole read unit and several counts share one length.
        """
        if self._resize_blocked():
            return None
        return self._count.resize_units()

    def _refresh_files(self) -> None:
        self._refresh_detected()
        self._refresh_size()

    def _on_stage_change(self, *_args: object) -> None:
        self._refresh_note()
        self._refresh_size()

    # -- the container -------------------------------------------------------
    def _refresh_detected(self) -> None:
        """Mark the container detection would pick for the *first* file.

        Marking it makes "put it back how it was" a visible option rather than
        something to remember — and it tracks the row it describes, since pointing
        the first row at another file moves what detection would have said.
        """
        detected = detect_container(self._registry, self.paths()[0], kind=self._kind)
        if detected == self._detected:
            return
        self._detected = detected
        names = self._stages.container_names
        for index in range(self._container.count()):
            if self._container.is_heading(index):
                continue
            plugin_id = self._container.itemData(index)
            name = names[plugin_id]
            self._container.setItemText(
                index, f"{name}  (detected)" if plugin_id == detected else name
            )

    def _refresh_note(self) -> None:
        # A stage with no save half of its own can still be read through, but
        # the entry then opens read-only — worth saying before the user edits.
        note = self._stages.view_only_note()
        self._note.setText(note)
        self._note.setVisible(bool(note))

    # -- results -------------------------------------------------------------
    def stages(self) -> FileStages:
        return self._stages.stages()

    def paths(self) -> tuple[str, ...]:
        return self._files.paths()

    @staticmethod
    def edit_container(
        parent: QWidget | None, registry: Registry, **options: Any
    ) -> ContainerEdit | None:
        """Run the dialog modally; the choices made, or None on cancel.

        ``options`` are the dialog's own keywords.
        """
        return run_modal(
            ContainerDialog(registry, parent=parent, **options),
            lambda d: ContainerEdit(d.stages(), d.paths(), d.resize_units()),
        )
