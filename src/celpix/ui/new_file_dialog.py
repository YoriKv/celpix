"""The New File dialog: what a fresh file holds, how it is framed, and how big.

Everything celPix opens is somebody else's file — a ROM, a dump, a bank pulled
out of a disk image. This is the one gesture that starts from nothing: a blank
tile sheet to draw on, an empty screen to lay out, a palette to fill in. The file
is real and on disk before it is an entry, because that is what every other part
of the editor already assumes — the pipeline reads bytes from a path, a save
writes them back to it, and a project stores the reference. A "new file" that
lived only in memory would be a fourth kind of entry answering to none of that.

**Content is the first row and it drives the rest.** Which containers can frame
the bytes, which codecs can read them, and what a unit of size even *is* all
follow from it, so changing it refills both pickers and reshapes the size row
rather than the user starting over. Same reason it is first on the slice dialog:
it decides what the rest of the dialog is describing.

**Size is stated in the units the user thinks in** — tiles across and down for a
graphic, cells for a map, a plain count of colors for a palette, which is a run
and has no grid to state. The byte length underneath is the codec's arithmetic
(:func:`~celpix.pipeline.pipeline.blank_size`) and moves with every control,
because a file's size in bytes is what the format's own tooling quotes back and
what a hardware slot is measured in — and because it is the one thing that makes
a wrong codec obvious before the file exists.

The cols/rows ranges are this dialog's own, not the format's
(:mod:`celpix.ui.size_row`): the user is picking a size to *start* at, and a
slip of the keyboard should not make a file of hundreds of megabytes.

What this dialog does not do is pick the path. The caller runs the ordinary save
picker afterwards, so a new file is named where every other written file is.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QWidget,
)

from celpix.core.capabilities import ContentKind
from celpix.core.errors import PipelineError, Stage
from celpix.pipeline import pipeline
from celpix.plugins.base import (
    PALETTE_SWATCH_ENGINE,
    RAW_CONTAINER,
    STAGE_DEFAULT_PRESET,
    format_size,
)
from celpix.plugins.detect import container_write_enabled, containers_for
from celpix.plugins.registry import Registry
from celpix.ui.searchable_combo import (
    SearchableComboBox,
    fill_grouped,
    info_rows,
    preset_rows,
    tilemap_codec_label,
)
from celpix.ui.size_row import MAX_COLORS, MAX_COLUMNS, MAX_ROWS, SIZE_CAPTIONS
from celpix.ui.theme import WARNING_INK, set_ink
from celpix.ui.widgets import (
    PRESET_COMBO_WIDTH,
    add_form_row,
    dialog_buttons,
    run_modal,
    signals_blocked,
    size_text,
    value_spin,
)

__all__ = ["SIZE_CAPTIONS", "NewFileDialog", "NewFileParams"]

# A 16x16 sheet is a screen's worth of tiles at every depth, and 256 colors is a
# whole CGRAM dump — the sizes the rest of the app already opens on.
DEFAULT_COLUMNS = 16
DEFAULT_ROWS = 16
DEFAULT_COLORS = 256

_CONTENT_TIP = (
    "Content of the new file:\n"
    "• Pixels - tile graphics\n"
    "• Palette - colors in a color format\n"
    "• Tilemap - indices into tiles stored elsewhere"
)

_CONTAINER_TIP = (
    "Framing written around the payload\n"
    "Raw binary file writes the payload alone; other\n"
    "containers add their own header"
)

_CODEC_TIP = "Format the file is created in and read back through"

_SIZE_TIPS = {
    ContentKind.PIXELS: "Tiles across and down",
    ContentKind.TILEMAP: "Cells across and down",
    ContentKind.PALETTE: "Number of colors",
}

_CODEC_STAGES = {
    ContentKind.PIXELS: Stage.INTERPRET_PIXEL,
    ContentKind.TILEMAP: Stage.INTERPRET_TILEMAP,
    ContentKind.PALETTE: Stage.INTERPRET_PALETTE,
}


@dataclass(frozen=True)
class NewFileParams:
    """The settled answers: what to create, how to frame it, and how big.

    ``columns``/``rows`` are counted in whatever unit ``content_kind`` measures —
    tiles for pixels, cells for a tilemap. A **palette has no grid**, so its whole
    color count is ``columns`` and ``rows`` stays 1; :attr:`units` is the number
    the byte arithmetic actually wants, and is what callers should read.
    """

    content_kind: ContentKind
    container_id: str
    codec_id: str
    columns: int
    rows: int = 1

    @property
    def units(self) -> int:
        """Tiles, cells or colors — the count :func:`blank_size` is asked for."""
        return self.columns * self.rows


class NewFileDialog(QDialog):
    def __init__(
        self,
        registry: Registry,
        *,
        pixel_preset_id: str = "",
        palette_preset_id: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("New File")
        self._registry = registry
        self._params: NewFileParams | None = None
        # The codec each kind is showing, so switching Content back and forth
        # lands where it was rather than on the first row of a hundred-entry
        # list. Seeded from the live toolbar for the two kinds the toolbar has an
        # answer for — a new file starts in the format the user is already
        # working in — and from the stage's own default otherwise, which is what
        # keeps a fresh sheet off whichever preset happens to sort first.
        self._codec_memory = {
            kind: STAGE_DEFAULT_PRESET[stage] for kind, stage in _CODEC_STAGES.items()
        }
        if pixel_preset_id:
            self._codec_memory[ContentKind.PIXELS] = pixel_preset_id
        if palette_preset_id:
            self._codec_memory[ContentKind.PALETTE] = palette_preset_id

        self._content = QComboBox()
        self._content.setToolTip(_CONTENT_TIP)
        for label, data in (
            ("Pixels", ContentKind.PIXELS),
            ("Palette", ContentKind.PALETTE),
            ("Tilemap", ContentKind.TILEMAP),
        ):
            self._content.addItem(label, data)

        self._container = SearchableComboBox(PRESET_COMBO_WIDTH)
        self._container.setToolTip(_CONTAINER_TIP)
        self._codec = SearchableComboBox(PRESET_COMBO_WIDTH)
        self._codec.setToolTip(_CODEC_TIP)

        # Three spins rather than two re-ranged ones: the grid and the color count
        # are different questions with different limits and different sensible
        # starts, and sharing a widget between them would carry 16 colors over
        # from a 16-tile-wide sheet — one hardware row, which nobody asked for.
        self._columns = value_spin(1, MAX_COLUMNS, DEFAULT_COLUMNS, self._refresh)
        self._rows = value_spin(1, MAX_ROWS, DEFAULT_ROWS, self._refresh)
        self._colors = value_spin(1, MAX_COLORS, DEFAULT_COLORS, self._refresh)
        self._times = QLabel("x")
        self._size_field = QWidget()
        size_row = QHBoxLayout(self._size_field)
        size_row.setContentsMargins(0, 0, 0, 0)
        for widget in (self._columns, self._times, self._rows, self._colors):
            size_row.addWidget(widget)
        size_row.addStretch(1)

        # What the answers above work out to on disk, and the one caution this
        # dialog has to give (:meth:`_note_text`).
        self._size = QLabel()
        self._note = QLabel()
        self._note.setWordWrap(True)
        set_ink(self._note, WARNING_INK)

        form = QFormLayout(self)
        add_form_row(form, "Content:", self._content)
        add_form_row(form, "Container:", self._container)
        add_form_row(form, "Codec:", self._codec)
        # Captioned and tipped per content kind (:meth:`_apply_content_kind`).
        self._size_caption = add_form_row(form, "", self._size_field)
        form.addRow("", self._size)
        form.addRow(self._note)
        buttons = dialog_buttons(self, form, self._accept)
        self._ok = buttons.button(QDialogButtonBox.StandardButton.Ok)

        self._content.currentIndexChanged.connect(self._apply_content_kind)
        self._container.currentIndexChanged.connect(self._refresh)
        self._codec.currentIndexChanged.connect(self._on_codec_change)
        self._apply_content_kind()

    # -- content-driven refill ------------------------------------------------
    def _kind(self) -> ContentKind:
        # Back through the enum: ``ContentKind`` is str-valued, so a QVariant
        # round trip hands back a bare string that compares equal to the member
        # and fails every ``is`` test this dialog gates on.
        return ContentKind(self._content.currentData())

    def _on_codec_change(self, _index: int) -> None:
        self._codec_memory[self._kind()] = str(self._codec.currentData())
        self._refresh()

    def _apply_content_kind(self, *_args: object) -> None:
        """Refill both pickers and reshape the size row for the chosen content.

        Only the containers that can *write* are offered: a view-only container
        has no save half, so there is nothing for it to produce a file with —
        a different failure from opening a file that cannot be saved, and one
        there is no reason to let the user reach.

        Signals stay blocked across the refill and one refresh runs after it. A
        fill emits a change per item, and a heading is briefly current before the
        first real row displaces it (:meth:`SearchableComboBox.add_category`), so
        a live handler would be asked to size a file against a category name.
        """
        kind = self._kind()
        offered = [
            info
            for info in containers_for(self._registry, kind)
            if container_write_enabled(self._registry, info.id)
        ]
        # Plain bytes first for every kind: a new file is the one case detection
        # has nothing to go on, and the payload alone always reopens as what was
        # just written.
        with signals_blocked(self._container):
            fill_grouped(self._container, info_rows(offered), RAW_CONTAINER)

        stage = _CODEC_STAGES[kind]
        label = tilemap_codec_label if kind is ContentKind.TILEMAP else None
        # The palette-swatch view is a way of *looking* at bytes, not a format
        # to author a file in — a palette file is made as the Palette kind.
        rows = preset_rows(
            (
                preset
                for preset in self._registry.presets(stage)
                if preset.engine_id != PALETTE_SWATCH_ENGINE
            ),
            label,
        )
        with signals_blocked(self._codec):
            fill_grouped(self._codec, rows, self._codec_memory[kind])
        # Whatever survived the fill is now this kind's remembered choice — the
        # seed may name a preset a plugin refresh has dropped, and the memory
        # must not go on offering it.
        self._codec_memory[kind] = str(self._codec.currentData() or "")

        grid = kind is not ContentKind.PALETTE
        for widget in (self._columns, self._times, self._rows):
            widget.setVisible(grid)
        self._colors.setVisible(not grid)
        self._size_caption.setText(SIZE_CAPTIONS[kind])
        self._size_caption.setBuddy(self._colors if not grid else self._columns)
        tip = _SIZE_TIPS[kind]
        for widget in (
            self._columns,
            self._rows,
            self._colors,
            self._size_field,
            self._size_caption,
        ):
            widget.setToolTip(tip)
        self._refresh()

    # -- the live byte count --------------------------------------------------
    def _units(self) -> int:
        """Tiles, cells or colors the chosen size comes to."""
        if self._kind() is ContentKind.PALETTE:
            return self._colors.value()
        return self._columns.value() * self._rows.value()

    def _refresh(self, *_args: object) -> None:
        """Restate the byte size and the caution, and arm OK on whether they hold.

        Both in one pass, because both are answers only the plugins can give and
        both move with every control. Asking here rather than at the write is
        deliberate: the alternative is a save picker, an overwrite confirmation
        and *then* a failure, with the file already replaced.

        A codec or container is a plugin and may refuse to answer — a preset whose
        params its engine rejects, a third-party format that raises. That is a
        reason not to offer OK, not a reason to close the dialog.
        """
        codec, container_id = self._codec.currentData(), self._container.currentData()
        if not codec or not container_id:
            self._fail("No format is registered for this kind of file.")
            return
        try:
            size = pipeline.blank_size(
                self._kind(), str(codec), self._units(), self._registry
            )
            if size <= 0:
                self._fail("This format reports no size, so there is nothing to write.")
                return
            note = self._note_text(str(container_id), str(codec), size)
        except PipelineError as exc:
            self._fail(str(exc))
            return
        # The round form and the exact count, unless they are the same phrase:
        # a slot is quoted in KiB and a codec's arithmetic in bytes, and a file
        # that is not a round multiple only has the second.
        self._size.setText(size_text(size))
        self._note.setText(note)
        self._note.setVisible(bool(note))
        self._ok.setEnabled(True)

    def _note_text(self, container_id: str, codec_id: str, size: int) -> str:
        """The caution for a container that will not frame this file, or ``""``.

        A container's write is handed the destination as it stands so it can keep
        what it did not decode, and a new file gives it nothing to keep. The
        formats that *build* their framing (a tile bank, a screen) produce a
        proper file anyway; the ones that can only preserve it fall back to
        writing the payload plainly, which will not reopen as that format. Wanting
        the payload alone is reasonable — it is still the bytes asked for — so
        this says so rather than refusing, but it says so before the file exists.

        Plain bytes are exempt: framing nothing is what that container is for.
        """
        if container_id == RAW_CONTAINER:
            return ""
        if pipeline.frames_new_file(
            self._kind(), container_id, codec_id, self._units(), self._registry
        ):
            return ""
        return (
            f"{self._container.currentText()} can preserve this framing but not "
            f"build it, so a new file gets the {format_size(size)} payload on its "
            "own - it will not reopen as that format."
        )

    def _fail(self, message: str) -> None:
        self._size.setText("-")
        self._note.setText(message)
        self._note.show()
        self._ok.setEnabled(False)

    # -- result ---------------------------------------------------------------
    def _accept(self) -> None:
        kind = self._kind()
        palette = kind is ContentKind.PALETTE
        self._params = NewFileParams(
            content_kind=kind,
            container_id=str(self._container.currentData()),
            codec_id=str(self._codec.currentData()),
            columns=self._colors.value() if palette else self._columns.value(),
            rows=1 if palette else self._rows.value(),
        )
        self.accept()

    @staticmethod
    def get_params(
        parent: QWidget | None, registry: Registry, **options: Any
    ) -> NewFileParams | None:
        """Run the dialog modally; the settled parameters, or None on cancel.

        ``options`` are the dialog's own keywords.
        """
        return run_modal(
            NewFileDialog(registry, parent=parent, **options), lambda d: d._params
        )
