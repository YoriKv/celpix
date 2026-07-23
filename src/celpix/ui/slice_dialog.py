"""The slice dialog: name + offset + length + decompressor for a file region.

One dialog serves both creating a slice (New Slice) and editing an existing
one's coordinates — the caller sets the ``title`` and prefills the fields.
Offsets and lengths follow the app-wide address-box convention (``parse_hex``):
bare digits are hex, ``$``/``0x`` prefixes accepted — ``10`` must mean the same
thing here as in the navbar. Validation happens on OK and keeps the dialog open
with an inline message, so a typo never silently creates a wrong slice.
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
from celpix.core.errors import Stage
from celpix.plugins.registry import Registry
from celpix.project.workspace import SliceParams, default_slice_name

__all__ = ["SliceDialog", "SliceParams"]


class SliceDialog(QDialog):
    def __init__(
        self,
        registry: Registry,
        *,
        path: str,
        offset: int = 0,
        length: int | None = None,
        decompress_id: str = "decompress.none",
        name: str = "",
        title: str = "New Slice",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"{title} — {basename(path)}")
        self._path = path
        self._params: SliceParams | None = None

        self._name = QLineEdit(name)
        self._offset = QLineEdit(format_hex(offset))
        self._offset.setToolTip("Absolute file offset (hex; $ and 0x accepted)")
        self._length = QLineEdit(format_hex(length) if length is not None else "")
        self._length.setToolTip(
            "Byte length in the file (hex). With a decompressor it may be left "
            "blank — the structure's own end bounds the slice on first load."
        )

        self._decompress = QComboBox()
        for plugin in registry.plugins(Stage.DECOMPRESS):
            self._decompress.addItem(plugin.info.name, plugin.info.id)
        index = self._decompress.findData(decompress_id)
        if index >= 0:
            self._decompress.setCurrentIndex(index)

        self._error = QLabel()
        self._error.setStyleSheet("color: #c04040;")
        self._error.hide()

        # The name placeholder previews the generated default and tracks the
        # coordinate fields, so leaving the name blank never surprises.
        self._offset.textChanged.connect(self._refresh_placeholder)
        self._length.textChanged.connect(self._refresh_placeholder)
        self._decompress.currentIndexChanged.connect(self._refresh_placeholder)
        self._refresh_placeholder()

        form = QFormLayout(self)
        form.addRow("Name:", self._name)
        form.addRow("Offset:", self._offset)
        form.addRow("Length:", self._length)
        form.addRow("Compression:", self._decompress)
        form.addRow(self._error)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._validate_and_accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _refresh_placeholder(self) -> None:
        offset = parse_hex(self._offset.text())
        if offset is None or offset < 0:
            return  # keep the last valid preview while the offset is mid-edit
        length_text = self._length.text().strip()
        length = parse_hex(length_text) if length_text else None
        self._name.setPlaceholderText(
            default_slice_name(offset, length, self._decompress.currentData())
        )

    def _fail(self, message: str) -> None:
        self._error.setText(message)
        self._error.show()

    def _validate_and_accept(self) -> None:
        offset = parse_hex(self._offset.text())
        if offset is None or offset < 0:
            self._fail("Offset is not a valid address.")
            return
        decompress_id = self._decompress.currentData()
        length_text = self._length.text().strip()
        length: int | None = None
        if length_text:
            length = parse_hex(length_text)
            if length is None or length <= 0:
                self._fail("Length is not a valid byte count.")
                return
        elif decompress_id == "decompress.none":
            # A raw slice without an extent is just the file from that offset —
            # require the bound that makes it a slice (and its writes slot-safe).
            self._fail("A raw slice needs a length (compressed ones can discover it).")
            return
        try:
            size = getsize(self._path)
        except OSError as exc:
            self._fail(f"Cannot stat the file: {exc}")
            return
        if offset >= size or (length is not None and offset + length > size):
            self._fail(f"Region runs past the file's end ({format_hex(size)} bytes).")
            return
        # Default name from the *validated* values, not the placeholder text.
        name = self._name.text().strip() or default_slice_name(
            offset, length, decompress_id
        )
        self._params = SliceParams(name, offset, length, decompress_id)
        self.accept()

    @staticmethod
    def get_slice(
        parent: QWidget | None,
        registry: Registry,
        *,
        path: str,
        offset: int = 0,
        length: int | None = None,
        decompress_id: str = "decompress.none",
        name: str = "",
        title: str = "New Slice",
    ) -> SliceParams | None:
        """Run the dialog modally; the validated parameters, or None on cancel."""
        dialog = SliceDialog(
            registry,
            path=path,
            offset=offset,
            length=length,
            decompress_id=decompress_id,
            name=name,
            title=title,
            parent=parent,
        )
        dialog.exec()
        return dialog._params
