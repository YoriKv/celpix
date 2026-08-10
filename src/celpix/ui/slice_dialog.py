"""The slice dialog: what a region holds, where it is, and how it is unpacked.

One dialog serves both creating a slice (New Slice) and editing an existing
one's coordinates — the caller sets the ``title`` and prefills the fields.
Offsets and lengths follow the app-wide address-box convention (``parse_hex``):
bare digits are hex, ``$``/``0x`` prefixes accepted — ``10`` must mean the same
thing here as in the navbar. Validation happens on OK and keeps the dialog open
with an inline message, so a typo never silently creates a wrong slice.

**Content** is the one field only a *new* slice offers (``choose_content``). A
slice inherits its parent's reading by default, which is right for the common
case and wrong for the one this row exists for: a ROM is opened as pixels, and
the map that draws them is a region of that same ROM
(``docs/design/tilemap-entry.md`` §2). Editing an existing slice does not offer
it — that would re-read a live entry as another kind of thing, taking its
binding, its section in the Files list and its session with it — so the value is
carried through unchanged there instead.

**Spare room** appears only once a compression scheme is chosen, because it only
means something there: a re-packed blob is the length its compressor makes it,
and nothing else on this dialog can produce a result shorter than the slot it
goes back into (:class:`~celpix.pipeline.pathway.SlotFill`). Hidden rather than
disabled — a control that can never apply to what is being described is one
question fewer, not a greyed-out one.
"""

from __future__ import annotations

from os.path import basename, getsize

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QWidget,
)

from celpix.core.address import format_hex, parse_hex
from celpix.core.capabilities import ContentKind
from celpix.core.errors import Stage
from celpix.pipeline.pathway import DEFAULT_SLOT_FILL, SlotFill
from celpix.plugins.base import NO_COMPRESSION, NO_RESHAPE
from celpix.plugins.registry import Registry
from celpix.project.workspace import SliceParams, default_slice_name
from celpix.ui.searchable_combo import SearchableComboBox, fill_stage_combo
from celpix.ui.widgets import PRESET_COMBO_WIDTH

__all__ = ["SliceDialog", "SliceParams"]


class SliceDialog(QDialog):
    def __init__(
        self,
        registry: Registry,
        *,
        paths: tuple[str, ...],
        offset: int = 0,
        length: int | None = None,
        compression_id: str = NO_COMPRESSION,
        reshape_id: str = NO_RESHAPE,
        slot_fill: SlotFill = DEFAULT_SLOT_FILL,
        name: str = "",
        title: str = "New Slice",
        content_kind: ContentKind = ContentKind.PIXELS,
        choose_content: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"{title} - {basename(paths[0])}")
        # Every file of the parent's region: offsets address the concatenation
        # (:class:`~celpix.plugins.base.FileRef`), so bounding them against the
        # first chip alone would put most of a several-chip region out of reach.
        self._paths = paths
        self._params: SliceParams | None = None

        # Echoed back untouched when the row is not offered, so an edit round-trips
        # the entry's own kind rather than resetting it to the default.
        self._content_kind = content_kind
        self._content: QComboBox | None = None
        if choose_content:
            self._content = QComboBox()
            self._content.setToolTip(
                "What this region holds:\n"
                "• Pixels - tile graphics, drawn from these bytes\n"
                "• Tilemap - indices into tiles that live somewhere else"
            )
            for label, data in (
                ("Pixels", ContentKind.PIXELS),
                ("Tilemap", ContentKind.TILEMAP),
            ):
                self._content.addItem(label, data)
            at = self._content.findData(content_kind)
            self._content.setCurrentIndex(max(0, at))

        self._name = QLineEdit(name)
        self._name.setToolTip("Name in the Files list; blank uses the placeholder")
        self._offset = QLineEdit(format_hex(offset))
        self._offset.setToolTip("File offset (hex; $ and 0x accepted)")
        self._length = QLineEdit(format_hex(length) if length is not None else "")
        self._length.setToolTip(
            "Byte length (hex); blank lets a decompressor find the end"
        )

        self._reshape = SearchableComboBox(PRESET_COMBO_WIDTH)
        self._reshape.setToolTip(
            "Undo a byte reordering on load: a plane-per-chip\n"
            "split, an interleave\n"
            "Applies to the whole region, before decompression"
        )
        fill_stage_combo(self._reshape, registry.plugins(Stage.RESHAPE), reshape_id)

        self._decompress = SearchableComboBox(PRESET_COMBO_WIDTH)
        self._decompress.setToolTip("Decompress with this codec on load")
        fill_stage_combo(
            self._decompress, registry.plugins(Stage.COMPRESSION), compression_id
        )

        self._slot_fill = QComboBox()
        self._slot_fill.setToolTip(
            "What fills the end of the region when re-packing\n"
            "produces fewer bytes than it replaces:\n"
            "• Keep Bytes - leave the old stream's tail standing\n"
            "• Fill w/ $FF - how erased ROM reads\n"
            "• Fill w/ $00 - for images padded with zeroes"
        )
        for label, data in (
            ("Keep Bytes", SlotFill.KEEP),
            ("Fill w/ $FF", SlotFill.FF),
            ("Fill w/ $00", SlotFill.ZERO),
        ):
            self._slot_fill.addItem(label, data)
        self._slot_fill.setCurrentIndex(max(0, self._slot_fill.findData(slot_fill)))

        self._error = QLabel()
        self._error.setStyleSheet("color: #c04040;")
        self._error.hide()

        # The name placeholder previews the generated default and tracks the
        # coordinate fields, so leaving the name blank never surprises.
        self._offset.textChanged.connect(self._refresh_placeholder)
        self._length.textChanged.connect(self._refresh_placeholder)
        self._reshape.currentIndexChanged.connect(self._refresh_placeholder)
        self._decompress.currentIndexChanged.connect(self._refresh_placeholder)
        self._refresh_placeholder()

        form = QFormLayout(self)
        # First, because it decides what the rest of the dialog is describing —
        # and, unlike the coordinates, it is the one field the parent's answer can
        # be wrong about.
        if self._content is not None:
            form.addRow("Content:", self._content)
        form.addRow("Name:", self._name)
        form.addRow("Offset:", self._offset)
        form.addRow("Length:", self._length)
        form.addRow("Reshape:", self._reshape)
        form.addRow("Compression:", self._decompress)
        form.addRow("Spare room:", self._slot_fill)
        form.addRow(self._error)
        # Connected here rather than beside the other combo signals above,
        # because the row can only be shown or hidden once it is in a layout.
        self._form = form
        self._decompress.currentIndexChanged.connect(self._sync_slot_fill_row)
        self._sync_slot_fill_row()
        # QFormLayout builds the caption widgets itself, so copy each field's
        # tooltip onto its caption - hovering either half then answers the same.
        for field in (
            *((self._content,) if self._content is not None else ()),
            self._name,
            self._offset,
            self._length,
            self._reshape,
            self._decompress,
            self._slot_fill,
        ):
            label = form.labelForField(field)
            if label is not None:
                label.setToolTip(field.toolTip())
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._validate_and_accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _sync_slot_fill_row(self) -> None:
        """Show Spare room only under a compression scheme — see the module docs.

        The value is left alone while hidden, so a slice that had one and is
        switched to raw carries it back out unchanged rather than being reset by
        a row the user was not shown.
        """
        self._form.setRowVisible(
            self._slot_fill, self._decompress.currentData() != NO_COMPRESSION
        )

    def _refresh_placeholder(self) -> None:
        offset = parse_hex(self._offset.text())
        if offset is None or offset < 0:
            return  # keep the last valid preview while the offset is mid-edit
        length_text = self._length.text().strip()
        length = parse_hex(length_text) if length_text else None
        self._name.setPlaceholderText(
            default_slice_name(
                offset,
                length,
                self._decompress.currentData(),
                self._reshape.currentData(),
            )
        )

    def _fail(self, message: str) -> None:
        self._error.setText(message)
        self._error.show()

    def _validate_and_accept(self) -> None:
        offset = parse_hex(self._offset.text())
        if offset is None or offset < 0:
            self._fail("Offset is not a valid address.")
            return
        compression_id = self._decompress.currentData()
        reshape_id = self._reshape.currentData()
        length_text = self._length.text().strip()
        length: int | None = None
        if length_text:
            length = parse_hex(length_text)
            if length is None or length <= 0:
                self._fail("Length is not a valid byte count.")
                return
        elif reshape_id != NO_RESHAPE:
            # A reshape's boundaries are fractions of the region's length, so a
            # decompressor-discovered extent (measured in reshaped space) would
            # re-bound the window and change the permutation itself.
            self._fail(
                "A reshaped slice needs a length — its extent defines "
                "the reshape, so nothing can discover it."
            )
            return
        elif compression_id == NO_COMPRESSION:
            # A raw slice without an extent is just the file from that offset —
            # require the bound that makes it a slice (and its writes slot-safe).
            self._fail("A raw slice needs a length (compressed ones can discover it).")
            return
        try:
            size = sum(getsize(path) for path in self._paths)
        except OSError as exc:
            self._fail(f"Cannot stat the file: {exc}")
            return
        if offset >= size or (length is not None and offset + length > size):
            noun = "region's" if len(self._paths) > 1 else "file's"
            self._fail(f"Region runs past the {noun} end ({format_hex(size)} bytes).")
            return
        # Default name from the *validated* values, not the placeholder text.
        name = self._name.text().strip() or default_slice_name(
            offset, length, compression_id, reshape_id
        )
        # Back through the enum on the way out: ``ContentKind`` is str-valued, and
        # a round trip through a QVariant hands the bare string back — which
        # compares equal to the member but fails every ``is`` test the window
        # gates on.
        kind = (
            self._content_kind
            if self._content is None
            else ContentKind(self._content.currentData())
        )
        self._params = SliceParams(
            name,
            offset,
            length,
            compression_id,
            reshape_id,
            kind,
            # Back through the enum for the same reason ``kind`` is: str-valued,
            # so a QVariant round trip hands back a bare string that compares
            # equal to the member and fails every ``is`` test.
            SlotFill(self._slot_fill.currentData()),
        )
        self.accept()

    @staticmethod
    def get_slice(
        parent: QWidget | None,
        registry: Registry,
        *,
        paths: tuple[str, ...],
        offset: int = 0,
        length: int | None = None,
        compression_id: str = NO_COMPRESSION,
        reshape_id: str = NO_RESHAPE,
        slot_fill: SlotFill = DEFAULT_SLOT_FILL,
        name: str = "",
        title: str = "New Slice",
        content_kind: ContentKind = ContentKind.PIXELS,
        choose_content: bool = False,
    ) -> SliceParams | None:
        """Run the dialog modally; the validated parameters, or None on cancel."""
        dialog = SliceDialog(
            registry,
            paths=paths,
            offset=offset,
            length=length,
            compression_id=compression_id,
            reshape_id=reshape_id,
            slot_fill=slot_fill,
            name=name,
            title=title,
            content_kind=content_kind,
            choose_content=choose_content,
            parent=parent,
        )
        dialog.exec()
        return dialog._params
