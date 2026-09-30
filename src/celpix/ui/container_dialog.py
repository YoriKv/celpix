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
from celpix.core.errors import Stage
from celpix.plugins.base import NO_RESHAPE, RAW_CONTAINER
from celpix.plugins.detect import (
    container_write_enabled,
    containers_for,
    detect_container,
    stage_write_enabled,
)
from celpix.plugins.registry import Registry
from celpix.ui.path_list_editor import PathListEditor
from celpix.ui.searchable_combo import (
    SearchableComboBox,
    fill_grouped,
    fill_stage_combo,
    info_rows,
)
from celpix.ui.size_row import GROWTH_TIPS, UnitCountRow
from celpix.ui.theme import WARNING_INK, set_ink
from celpix.ui.widgets import (
    PRESET_COMBO_WIDTH,
    add_form_row,
    dialog_buttons,
    run_modal,
)

__all__ = ["ContainerDialog", "ContainerEdit"]

_TIP = (
    "How the file is unwrapped before decoding: a header\n"
    "to skip, an interleave to undo, a wrapper to strip\n"
    "Raw binary file passes every byte through"
)

_RESHAPE_TIP = (
    "Byte reordering undone after the container, e.g. a\n"
    "plane-per-chip split or a ROM-pair word interleave\n"
    "Addresses and slices then count in the reordered bytes"
)

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
    """What the dialog was left holding: files, container, and reshape.

    All together, because the dialog settles them and the caller applies them as
    one change — the file list decides which bytes there are, the container how
    they are unwrapped, the reshape how the region is reordered, and any one
    alone leaves the entry re-read.

    ``units`` is the odd one out and is **not** part of that change: the three
    above only decide how bytes are read and so can be undone by putting them
    back, while a resize rewrites the file. It is carried here because the dialog
    is where it was asked for, and it is ``None`` — the ordinary case — wherever
    no size was asked for at all, so a caller applying the rest does nothing to
    the file (:meth:`ContainerDialog.resize_units`).
    """

    container_id: str
    paths: tuple[str, ...]
    reshape_id: str = NO_RESHAPE
    units: int | None = None


class ContainerDialog(QDialog):
    def __init__(
        self,
        registry: Registry,
        *,
        paths: tuple[str, ...] | list[str],
        container_id: str = RAW_CONTAINER,
        reshape_id: str = NO_RESHAPE,
        kind: ContentKind = ContentKind.PIXELS,
        codec_id: str = "",
        units: int = 0,
        offer_reshape: bool = True,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._registry = registry
        self._kind = kind
        self._files = PathListEditor(paths, _FILES_TIP)
        self.setWindowTitle(f"Edit File Container - {basename(self.paths()[0])}")

        self._container = SearchableComboBox(PRESET_COMBO_WIDTH)
        self._container.setToolTip(_TIP)
        # Kept unadorned so the "(detected)" marker can be re-applied to a
        # different entry when the first file changes.
        # Only the containers that frame this kind of entry: offering a palette
        # the wrappers that unwrap ROMs would be inviting a choice that cannot
        # come out well, and the two sets do not overlap.
        offered = containers_for(registry, kind)
        self._names = {info.id: info.name for info in offered}
        self._detected = ""
        fill_grouped(self._container, info_rows(offered), container_id)

        self._reshape = SearchableComboBox(PRESET_COMBO_WIDTH)
        self._reshape.setToolTip(_RESHAPE_TIP)
        fill_stage_combo(self._reshape, registry.plugins(Stage.RESHAPE), reshape_id)

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

        # A container or reshape with no save half of its own can still be read
        # through, but the entry then opens read-only — worth saying before the
        # user edits, not after.
        self._note = QLabel()
        self._note.setWordWrap(True)
        set_ink(self._note, WARNING_INK)
        # Both answers decide whether there is a write half to put resized bytes
        # back through, so the size row follows them as the note does.
        for combo in (self._container, self._reshape):
            combo.currentIndexChanged.connect(self._refresh_note)
            combo.currentIndexChanged.connect(self._refresh_size)
        self._refresh_note()

        files_caption = QLabel("Files:")
        files_caption.setToolTip(_FILES_TIP)

        form = QFormLayout(self)
        # The list spans the form's full width rather than sitting in its field
        # column: a path is long, and the caption column would take a quarter of
        # the room the paths need from every row at once.
        form.addRow(files_caption)
        form.addRow(self._files)
        add_form_row(form, "Container:", self._container)
        # Left out where the caller cannot apply one (a palette file: see
        # ``_change_container_for``). The combo stays, hidden but parented so it
        # never stands alone as a window, holding the value it was given, so the
        # results and the note read it as they would a shown one.
        if offer_reshape:
            add_form_row(form, "Reshape:", self._reshape)
        else:
            self._reshape.setParent(self)
            self._reshape.hide()
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
        if not container_write_enabled(self._registry, self._container.currentData()):
            return "This container cannot write, so the file's size is fixed."
        if not self._reshape_writes_back():
            return "This reshape cannot be undone, so the file's size is fixed."
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
        for index in range(self._container.count()):
            if self._container.is_heading(index):
                continue
            plugin_id = self._container.itemData(index)
            name = self._names[plugin_id]
            self._container.setItemText(
                index, f"{name}  (detected)" if plugin_id == detected else name
            )

    def _refresh_note(self) -> None:
        if not container_write_enabled(self._registry, self._container.currentData()):
            self._note.setText(
                "This container has no writer, so the file opens read-only.\n"
                "Saving plain bytes back would undo the unwrapping rather\n"
                "than reverse it, leaving the file corrupt."
            )
            self._note.setVisible(True)
        elif not self._reshape_writes_back():
            self._note.setText(
                "This reshape has no unshape half, so the file opens\n"
                "read-only: without the inverse, saved bytes could not be\n"
                "returned to the places they came from."
            )
            self._note.setVisible(True)
        else:
            self._note.setVisible(False)

    def _reshape_writes_back(self) -> bool:
        # A missing reshape answers True here, where a missing *container*
        # (:func:`container_write_enabled`) answers False: an id this registry
        # lacks isn't this note's problem, and the entry's open reports it.
        return stage_write_enabled(
            self._registry, Stage.RESHAPE, self._reshape.currentData(), missing=True
        )

    # -- results -------------------------------------------------------------
    def container_id(self) -> str:
        return self._container.currentData()

    def reshape_id(self) -> str:
        return self._reshape.currentData()

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
            lambda d: ContainerEdit(
                d.container_id(), d.paths(), d.reshape_id(), d.resize_units()
            ),
        )
