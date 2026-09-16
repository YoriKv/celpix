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

**Inputs** is the line under Compression, and the badge beside the picker that
edits it. What a codec needs from outside the slice — a code table, an output
size — is as much a part of unpacking these bytes as the codec itself, so the
one screen that describes the rest of the slice reaches it too
(``docs/design/plugin-inputs.md``). The badge opens the Inputs window over this
dialog, on the codec *selected here* rather than the one the entry reads with
today; it comes up application-modal, because a window merely stacked above a
modal dialog is drawn and then ignores every click. The line is re-read whenever
this dialog is activated again, since that apply lands behind its back.

**Spare room** appears only once a compression scheme is chosen, because it only
means something there: a re-packed blob is the length its compressor makes it,
and nothing else on this dialog can produce a result shorter than the slot it
goes back into (:class:`~celpix.pipeline.pathway.SlotFill`). Hidden rather than
disabled — a control that can never apply to what is being described is one
question fewer, not a greyed-out one.
"""

from __future__ import annotations

from collections.abc import Callable
from os.path import basename, getsize

from PySide6.QtCore import QEvent
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QToolButton,
    QWidget,
)

from celpix.core.address import format_hex, parse_hex
from celpix.core.capabilities import ContentKind
from celpix.core.errors import Stage
from celpix.pipeline.pathway import DEFAULT_SLOT_FILL, SlotFill
from celpix.plugins.base import NO_COMPRESSION, NO_RESHAPE
from celpix.plugins.registry import Registry
from celpix.project.workspace import SliceParams, default_slice_name
from celpix.ui.glyphs import Glyph
from celpix.ui.icon_font import glyph_icon
from celpix.ui.searchable_combo import SearchableComboBox, fill_stage_combo
from celpix.ui.theme import ERROR_INK, set_ink
from celpix.ui.widgets import (
    PRESET_COMBO_WIDTH,
    SHORT_COMBO_WIDTH,
    CompactComboBox,
)

__all__ = ["SliceDialog", "SliceParams"]


def _pinned(*widgets: QWidget) -> QWidget:
    """``widgets`` in a row, left-aligned, keeping their own stated widths.

    A :class:`~celpix.ui.widgets.CompactComboBox` only states a *hint*, and a
    ``QFormLayout``'s field column stretches — so a picker dropped straight into
    one is pulled to whatever the widest row made the dialog, and the four
    pickers here end up four different widths. The trailing stretch is what makes
    the stated number the width on screen; the popup still opens at the full
    content width, which is where a long preset name has to be readable anyway.
    """
    holder = QWidget()
    row = QHBoxLayout(holder)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(2)  # no gap: a picker and its badge read as one control
    for widget in widgets:
        row.addWidget(widget)
    row.addStretch(1)
    holder.setToolTip(widgets[0].toolTip())
    return holder


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
        inputs_hint: Callable[[str], str] | None = None,
        edit_inputs: Callable[[SliceDialog, str], None] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        # One line per codec about what it needs from outside the slice, or
        # ``""`` for one that needs nothing — the row below shows or hides on it.
        self._inputs_hint = inputs_hint
        # What the badge beside the Compression picker opens; ``None`` leaves the
        # inputs line read-only, which is what a dialog with no window behind it
        # (a bare construction in a test) gets.
        self._edit_inputs = edit_inputs
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
            self._content = CompactComboBox(SHORT_COMBO_WIDTH)
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

        self._slot_fill = CompactComboBox(SHORT_COMBO_WIDTH)
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

        # The badge the codecs toolbar wears beside its own compression picker,
        # doing the same job here: a codec's inputs are as much a part of "how
        # these bytes are unpacked" as the codec itself, so the one screen that
        # describes the rest of a slice has to be able to reach them. It opens
        # the *dialog's* codec, not the entry's — the picker may have been moved
        # and not yet OK'd, and binding under the codec being chosen is the whole
        # point of the badge being here rather than only on the toolbar.
        self._inputs_badge = QToolButton()
        self._inputs_badge.setAutoRaise(True)
        self._inputs_badge.setIcon(
            glyph_icon(
                Glyph.INPUTS,
                self.palette().color(QPalette.ColorRole.ButtonText),
                ratio=self.devicePixelRatioF(),
            )
        )
        self._inputs_badge.setFixedHeight(self._decompress.sizeHint().height())
        self._inputs_badge.setToolTip(
            "Edit what this codec needs from elsewhere in the file"
        )
        self._inputs_badge.clicked.connect(self._on_edit_inputs)

        # Read-only: what the slice will be created with (a file's preview
        # bindings) or currently binds — the badge above is the editor.
        self._inputs = QLabel()
        self._inputs.setToolTip(
            "What this codec needs from elsewhere in the file, and\n"
            "where the slice binds it. Edit it with the button beside\n"
            "the picker; a required input left unbound opens the slice\n"
            "degraded, with a notice saying what to bind."
        )

        self._error = QLabel()
        set_ink(self._error, ERROR_INK)
        self._error.hide()

        # The name placeholder previews the generated default and tracks the
        # coordinate fields, so leaving the name blank never surprises.
        self._offset.textChanged.connect(self._refresh_placeholder)
        self._length.textChanged.connect(self._refresh_placeholder)
        self._reshape.currentIndexChanged.connect(self._refresh_placeholder)
        self._decompress.currentIndexChanged.connect(self._refresh_placeholder)
        self._refresh_placeholder()

        # Every picker in a holder of its own, so the column of them is one
        # width rather than the form's (see :func:`_pinned`); the rows that hide
        # are addressed through the holder, since that is what the form knows.
        content_row = None if self._content is None else _pinned(self._content)
        reshape_row = _pinned(self._reshape)
        codec_row = _pinned(self._decompress, self._inputs_badge)
        self._slot_fill_row = _pinned(self._slot_fill)

        form = QFormLayout(self)
        # First, because it decides what the rest of the dialog is describing —
        # and, unlike the coordinates, it is the one field the parent's answer can
        # be wrong about.
        if content_row is not None:
            form.addRow("Content:", content_row)
        form.addRow("Name:", self._name)
        form.addRow("Offset:", self._offset)
        form.addRow("Length:", self._length)
        form.addRow("Reshape:", reshape_row)
        form.addRow("Compression:", codec_row)
        form.addRow("Inputs:", self._inputs)
        form.addRow("Spare room:", self._slot_fill_row)
        form.addRow(self._error)
        # Connected here rather than beside the other combo signals above,
        # because the row can only be shown or hidden once it is in a layout.
        self._form = form
        self._decompress.currentIndexChanged.connect(self._sync_slot_fill_row)
        self._decompress.currentIndexChanged.connect(self._sync_inputs_row)
        self._sync_slot_fill_row()
        self._sync_inputs_row()
        # QFormLayout builds the caption widgets itself, so copy each field's
        # tooltip onto its caption - hovering either half then answers the same.
        for field in (
            *((content_row,) if content_row is not None else ()),
            self._name,
            self._offset,
            self._length,
            reshape_row,
            codec_row,
            self._slot_fill_row,
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

    def _sync_inputs_row(self) -> None:
        """Show the inputs line, and the badge that edits it, only under a codec
        that declares any."""
        hint = (
            self._inputs_hint(self._decompress.currentData())
            if self._inputs_hint is not None
            else ""
        )
        self._inputs.setText(hint)
        self._form.setRowVisible(self._inputs, bool(hint))
        # Absent rather than greyed where there is no editor to open — the line
        # it would have edited is read-only either way, and a button that can
        # never do anything is one question more, not one fewer.
        self._inputs_badge.setVisible(bool(hint) and self._edit_inputs is not None)

    def refresh_inputs(self) -> None:
        """Re-read the inputs line — what it describes was edited elsewhere."""
        self._sync_inputs_row()

    def changeEvent(self, event: QEvent) -> None:  # Qt override
        super().changeEvent(event)
        if event.type() == QEvent.Type.ActivationChange and self.isActiveWindow():
            # The editor the badge opens is a separate window applying to the
            # entry behind this dialog's back, so the line is re-read when focus
            # comes back rather than being pushed at us from the main window.
            self.refresh_inputs()

    def _on_edit_inputs(self) -> None:
        if self._edit_inputs is not None:
            self._edit_inputs(self, self._decompress.currentData())

    def _sync_slot_fill_row(self) -> None:
        """Show Spare room only under a compression scheme — see the module docs.

        The value is left alone while hidden, so a slice that had one and is
        switched to raw carries it back out unchanged rather than being reset by
        a row the user was not shown.
        """
        self._form.setRowVisible(
            self._slot_fill_row, self._decompress.currentData() != NO_COMPRESSION
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
        inputs_hint: Callable[[str], str] | None = None,
        edit_inputs: Callable[[SliceDialog, str], None] | None = None,
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
            inputs_hint=inputs_hint,
            edit_inputs=edit_inputs,
            parent=parent,
        )
        dialog.exec()
        return dialog._params
