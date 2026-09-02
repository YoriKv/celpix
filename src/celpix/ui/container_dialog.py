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

It is also where a region's **file list** is edited. A graphics region is not
always one file — an arcade board's tiles routinely live on several ROM chips
that mean nothing apart (:class:`~celpix.plugins.base.FileRef`) — and nothing in
the files says which chip comes first, so the order is the user's to state. Hence
a list arranged by hand, one file per row, rather than a multi-select that would
hand back whatever order the file picker felt like and interleave a sprite sheet
wrong without ever failing.

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
from functools import partial
from os.path import basename, dirname
from typing import NamedTuple

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from celpix.core.capabilities import ContentKind
from celpix.core.errors import PipelineError, Stage
from celpix.pipeline import pipeline
from celpix.plugins.base import NO_RESHAPE, RAW_CONTAINER, writes_back
from celpix.plugins.detect import (
    container_write_enabled,
    containers_for,
    detect_container,
)
from celpix.plugins.registry import Registry

# The size row counts the same units the New File dialog counts and captions
# them the same way, so it reads both from there rather than keeping a second
# copy that could drift. Only the *tips* are its own: New File states a grid
# across and down, and this states a total (see :meth:`_build_size_row`).
from celpix.ui.new_file_dialog import (
    MAX_COLORS,
    MAX_COLUMNS,
    MAX_ROWS,
    SIZE_CAPTIONS,
)
from celpix.ui.searchable_combo import (
    SearchableComboBox,
    fill_grouped,
    fill_stage_combo,
    info_rows,
)
from celpix.ui.widgets import PRESET_COMBO_WIDTH, value_spin

__all__ = ["ContainerDialog", "ContainerEdit"]

_TIP = (
    "How this file's bytes are unwrapped before decoding:\n"
    "a header to skip, an interleave to undo, a wrapper to strip\n"
    "Raw binary file passes every byte through untouched"
)

_RESHAPE_TIP = (
    "A byte reordering undone after the container:\n"
    "a plane-per-chip split, a ROM-pair word interleave\n"
    "Turns addresses and slice carving off while active"
)

_FILES_TIP = "Every file this entry's bytes come from, in this order"

# A total rather than a grid across and down, which is why these are not the
# New File dialog's tips: see :meth:`ContainerDialog._build_size_row`.
_SIZE_TIPS = {
    ContentKind.PIXELS: "How many tiles the file holds in total\n"
    "Growing it appends blank tiles; shrinking drops the last ones",
    ContentKind.TILEMAP: "How many cells the map holds in total\n"
    "Growing it appends empty cells; shrinking drops the last ones",
    ContentKind.PALETTE: "How many colors the palette holds in total\n"
    "Growing it appends black; shrinking drops the last ones",
}

# Past this many rows the list scrolls instead of the dialog growing: a board
# with sixteen graphics ROMs would otherwise run off the bottom of the screen.
_VISIBLE_ROWS = 4


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


class _FileRow(NamedTuple):
    """One file's row: the path it shows and the four buttons that act on it.

    The buttons wire themselves and no production code reaches back for them,
    but they are what the dialog's tests drive, so the row carries them.
    """

    widget: QWidget
    field: QLineEdit
    up: QToolButton
    down: QToolButton
    browse: QToolButton
    remove: QToolButton


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
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._registry = registry
        self._kind = kind
        self._codec_id = codec_id
        self._units_before = max(0, units)
        # The list is the dialog's model and the rows are rebuilt from it after
        # every edit, rather than widgets being shuffled between positions: the
        # order on screen is then the order that will be applied, by construction,
        # and a move can't leave the two disagreeing about which chip is first.
        self._paths: list[str] = [p for p in paths if p] or [""]
        self._rows: list[_FileRow] = []
        self.setWindowTitle(f"Edit File Container - {basename(self._paths[0])}")

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

        self._build_size_row()

        # A container or reshape with no save half of its own can still be read
        # through, but the entry then opens read-only — worth saying before the
        # user edits, not after.
        self._note = QLabel()
        self._note.setWordWrap(True)
        self._note.setStyleSheet("color: #a08040;")
        # Both answers decide whether there is a write half to put resized bytes
        # back through, so the size row follows them as the note does.
        for combo in (self._container, self._reshape):
            combo.currentIndexChanged.connect(self._refresh_note)
            combo.currentIndexChanged.connect(self._refresh_size)
        self._refresh_note()

        self._rows_layout = QVBoxLayout()
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        rows_host = QWidget()
        rows_host.setLayout(self._rows_layout)
        self._scroll = QScrollArea()
        self._scroll.setWidget(rows_host)
        self._scroll.setWidgetResizable(True)
        self._scroll.setToolTip(_FILES_TIP)

        self._append = QPushButton("Append File")
        self._append.setToolTip("Add another file to the end of the region")
        self._append.clicked.connect(self._append_file)
        # Sized to its own text, left under the list it adds to: stretched across
        # the dialog it would read as the primary action, which OK is.
        append_row = QHBoxLayout()
        append_row.setContentsMargins(0, 0, 0, 0)
        append_row.addWidget(self._append)
        append_row.addStretch(1)

        files_caption = QLabel("Files:")
        files_caption.setToolTip(_FILES_TIP)

        form = QFormLayout(self)
        # The list spans the form's full width rather than sitting in its field
        # column: a path is long, and the caption column would take a quarter of
        # the room the paths need from every row at once.
        form.addRow(files_caption)
        form.addRow(self._scroll)
        form.addRow(append_row)
        form.addRow("Container:", self._container)
        form.addRow("Reshape:", self._reshape)
        # Last, because it is the one row that changes the file rather than how
        # the rows above it are read.
        form.addRow(self._size_caption, self._size_field)
        form.addRow("", self._size)
        for widget, tip in ((self._container, _TIP), (self._reshape, _RESHAPE_TIP)):
            label = form.labelForField(widget)
            if label is not None:
                label.setToolTip(tip)
        form.addRow(self._note)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

        # A row is mostly path, and a path is long. Opening at the width of the
        # widgets' own hints would elide every one of them from the start, so ask
        # for room measured in characters — which follows the font, unlike a
        # pixel count.
        self.setMinimumWidth(self.fontMetrics().averageCharWidth() * 72)
        self._rebuild_rows()

    # -- the size -----------------------------------------------------------
    def _build_size_row(self) -> None:
        """The spin that states how big the region is, seeded from what it holds.

        **A total count, not a grid**, which is the one place this differs from
        the New File dialog's otherwise identical row. A file has a length; a
        grid is the view's way of looking at it, and the two only agree when the
        length happens to be a whole number of rows. Stating a size the file
        cannot have — 100 tiles is not a whole number of 16-wide rows — would
        mean either rounding it or carrying a "the user has not touched this
        yet" exception through every answer this row gives. A count has neither
        problem: it seeds to exactly what the region holds, so opening this
        dialog to change a container asks for no resize at all, by arithmetic
        rather than by special case.

        The ceiling is what the view's grid limits could have expressed between
        them, raised where the file is **already** bigger: an 8 MB ROM holds more
        tiles than any grid this app will draw, and a spin that clamped the seed
        below the true size would read as a shrink nobody asked for.
        """
        ceiling = (
            MAX_COLORS if self._kind is ContentKind.PALETTE else MAX_COLUMNS * MAX_ROWS
        )
        # An empty region seeds 0 and may say so; anything else keeps 1 as the
        # floor, since a file emptied by a spin is a stranger request than a
        # file that already was.
        floor = min(1, self._units_before)
        self._size_units = value_spin(
            floor,
            max(ceiling, self._units_before),
            self._units_before,
            self._refresh_size,
        )
        self._size_field = QWidget()
        row = QHBoxLayout(self._size_field)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self._size_units)
        row.addStretch(1)

        self._size_caption = QLabel(SIZE_CAPTIONS[self._kind])
        self._size = QLabel()
        self._size.setWordWrap(True)
        for widget in (self._size_units, self._size_caption):
            widget.setToolTip(_SIZE_TIPS[self._kind])
        # The length the file has now, to measure a change against. Asked once:
        # it is the codec's arithmetic and cannot move while this dialog is open.
        self._bytes_before = self._byte_size(self._units_before)

    def _byte_size(self, units: int) -> int | None:
        """``units`` in bytes, or ``None`` where the codec would not say.

        A codec is a plugin and may refuse a size — a preset whose params its
        engine rejects, a third-party format that raises. Here that is a reason
        to stop offering the size row, not to stop the dialog: the container and
        the file list are still perfectly answerable questions.
        """
        if not self._codec_id:
            return None
        try:
            return pipeline.blank_size(
                self._kind, self._codec_id, units, self._registry
            )
        except PipelineError:
            return None

    def _resize_blocked(self) -> str:
        """Why this region's size cannot be changed, or ``""`` where it can.

        The multi-file rule is :func:`~celpix.pipeline.pipeline._deposit`'s, said
        here so the spin is dead rather than the user meeting it as a failed
        write: the boundaries between joined files are the lengths those files
        have, and nothing else records which bytes belong to which chip.
        """
        if len(self._paths) > 1:
            return (
                f"A region joined from {len(self._paths)} files has no size to "
                "change here: the boundary between two files is the length of "
                "the first, so moving it would move every byte after it into "
                "the wrong file."
            )
        if self._bytes_before is None:
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
        self._size_units.setEnabled(not blocked)
        if blocked:
            self._size.setText(blocked)
            return
        size = self._byte_size(self.size_units())
        if size is None:
            self._size.setText("This format reports no size for that count.")
            return
        # The current length is worth repeating only when it is not the answer:
        # a spin still on its seed is describing the file as it stands.
        self._size.setText(
            f"{size:,} bytes"
            if size == self._bytes_before
            else f"{size:,} bytes (currently {self._bytes_before:,})"
        )

    def size_units(self) -> int:
        """Tiles, cells or colors the size row is stating."""
        return self._size_units.value()

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
        units = self.size_units()
        return None if self._byte_size(units) == self._bytes_before else units

    # -- the file list -------------------------------------------------------
    def _rebuild_rows(self) -> None:
        """Re-make every row from :attr:`_paths` and re-mark what detection says."""
        while self._rows_layout.count():
            widget = self._rows_layout.takeAt(0).widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        self._rows = [self._make_row(index) for index in range(len(self._paths))]
        for row in self._rows:
            self._rows_layout.addWidget(row.widget)
        self._rows_layout.addStretch(1)
        # Grow to fit up to _VISIBLE_ROWS, then scroll. Measured from a real row
        # rather than a pixel constant so it holds at any font size or DPI.
        unit = self._rows[0].widget.sizeHint().height() + self._rows_layout.spacing()
        rows = min(len(self._rows), _VISIBLE_ROWS)
        self._scroll.setMaximumHeight(rows * unit + 2 * self._scroll.frameWidth())
        self._refresh_detected()
        # Appending or dropping a file is what decides whether there is a size to
        # change at all, so the row is re-stated from here rather than only at
        # construction.
        self._refresh_size()

    def _make_row(self, index: int) -> _FileRow:
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        field = QLineEdit(self._paths[index])
        # Read-only, and Browse is the way to change it: a typed path would need
        # rules for what a not-yet-existing file means, which the Files list
        # already answers its own way (a missing file is highlighted, not refused).
        field.setReadOnly(True)
        field.setToolTip(self._paths[index])
        field.setCursorPosition(0)
        layout.addWidget(field, 1)

        last = len(self._paths) - 1
        specs = (
            (
                "▲",
                "Move this file one place earlier in the join",
                index > 0,
                partial(self._move, index, -1),
            ),
            (
                "▼",
                "Move this file one place later in the join",
                index < last,
                partial(self._move, index, 1),
            ),
            (
                "…",
                "Point this row at a different file",
                True,
                partial(self._browse, index),
            ),
            (
                "✕",
                "Drop this file from the region\n(the list keeps at least one)",
                last > 0,
                partial(self._remove, index),
            ),
        )
        made = []
        for glyph, tip, enabled, handler in specs:
            button = QToolButton()
            button.setText(glyph)
            button.setToolTip(tip)
            button.setEnabled(enabled)
            button.clicked.connect(handler)
            layout.addWidget(button)
            made.append(button)
        return _FileRow(widget, field, *made)

    def _move(self, index: int, delta: int) -> None:
        target = index + delta
        if not 0 <= target < len(self._paths):
            return
        self._paths[index], self._paths[target] = (
            self._paths[target],
            self._paths[index],
        )
        self._rebuild_rows()

    def _remove(self, index: int) -> None:
        if len(self._paths) <= 1:
            return  # a region is at least one file; the button is disabled too
        del self._paths[index]
        self._rebuild_rows()

    def _browse(self, index: int) -> None:
        chosen = self._pick("Select file")
        if chosen:
            self._paths[index] = chosen
            self._rebuild_rows()

    def _append_file(self) -> None:
        chosen = self._pick("Append file")
        if chosen:
            self._paths.append(chosen)
            self._rebuild_rows()

    def _pick(self, title: str) -> str:
        """One file, starting where the last one came from (chips live together).

        Deliberately single-select: a multi-select hands back its own order, and
        an order nothing can verify is exactly what must not be guessed here.
        """
        chosen, _ = QFileDialog.getOpenFileName(self, title, dirname(self._paths[-1]))
        return chosen

    # -- the container -------------------------------------------------------
    def _refresh_detected(self) -> None:
        """Mark the container detection would pick for the *first* file.

        Marking it makes "put it back how it was" a visible option rather than
        something to remember — and it tracks the row it describes, since pointing
        the first row at another file moves what detection would have said.
        """
        detected = detect_container(self._registry, self._paths[0], kind=self._kind)
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
        try:
            plugin = self._registry.plugin(Stage.RESHAPE, self._reshape.currentData())
        except KeyError:
            return True  # an id this registry lacks isn't this note's problem
        return writes_back(plugin, Stage.RESHAPE)

    # -- results -------------------------------------------------------------
    def container_id(self) -> str:
        return self._container.currentData()

    def reshape_id(self) -> str:
        return self._reshape.currentData()

    def paths(self) -> tuple[str, ...]:
        return tuple(self._paths)

    @staticmethod
    def edit_container(
        parent: QWidget | None,
        registry: Registry,
        *,
        paths: tuple[str, ...] | list[str],
        container_id: str = RAW_CONTAINER,
        reshape_id: str = NO_RESHAPE,
        kind: ContentKind = ContentKind.PIXELS,
        codec_id: str = "",
        units: int = 0,
    ) -> ContainerEdit | None:
        """Run the dialog modally; the choices made, or None on cancel."""
        dialog = ContainerDialog(
            registry,
            paths=paths,
            container_id=container_id,
            reshape_id=reshape_id,
            kind=kind,
            codec_id=codec_id,
            units=units,
            parent=parent,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return ContainerEdit(
            dialog.container_id(),
            dialog.paths(),
            dialog.reshape_id(),
            dialog.resize_units(),
        )
