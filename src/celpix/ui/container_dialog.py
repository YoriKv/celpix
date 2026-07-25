"""The container dialog: which Read/Write pair a file's bytes travel through.

Opening a file picks its container from the file's own signature
(:mod:`celpix.plugins.detect`), which is right nearly always and wrong in the
cases only a person can settle — an interleaved SNES image looks exactly like a
plain one, a headerless dump is named ``.nes``, a user's own container knows a
wrapper celPix doesn't. This dialog is that override, and it lists *every*
registered container rather than only the plausible ones: the point of reaching
for it is that detection already had its turn.

Changing a container re-reads the file, so the caller is responsible for
handling unsaved edits before applying the answer.
"""

from __future__ import annotations

from os.path import basename

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QWidget,
)

from celpix.core.errors import Stage
from celpix.plugins.base import RAW_READ
from celpix.plugins.detect import container_write_id, detect_container
from celpix.plugins.registry import Registry

__all__ = ["ContainerDialog"]

_TIP = (
    "How this file's bytes are unwrapped before decoding:\n"
    "a header to skip, an interleave to undo, a wrapper to strip.\n"
    "Raw binary file passes every byte through untouched."
)


class ContainerDialog(QDialog):
    def __init__(
        self,
        registry: Registry,
        *,
        path: str,
        container_id: str = RAW_READ,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Change Container - {basename(path)}")

        self._container = QComboBox()
        self._container.setToolTip(_TIP)
        detected = detect_container(registry, path)
        for plugin in registry.plugins(Stage.READ):
            # Marking what detection would have chosen makes "put it back how it
            # was" a visible option rather than something to remember.
            label = plugin.info.name
            if plugin.info.id == detected:
                label = f"{label}  (detected)"
            self._container.addItem(label, plugin.info.id)
        index = self._container.findData(container_id)
        if index >= 0:
            self._container.setCurrentIndex(index)

        # A container with no writer of its own can still be read through, but
        # saving then writes plain bytes and drops the wrapper — worth saying
        # before the user edits, not after.
        self._note = QLabel()
        self._note.setWordWrap(True)
        self._note.setStyleSheet("color: #a08040;")
        self._registry = registry
        self._container.currentIndexChanged.connect(self._refresh_note)
        self._refresh_note()

        form = QFormLayout(self)
        form.addRow("Container:", self._container)
        label = form.labelForField(self._container)
        if label is not None:
            label.setToolTip(_TIP)
        form.addRow(self._note)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _refresh_note(self) -> None:
        chosen = self._container.currentData()
        own_writer = chosen.replace("read.", "write.", 1)
        unwritable = chosen != RAW_READ and (
            container_write_id(self._registry, chosen) != own_writer
        )
        self._note.setVisible(unwritable)
        if unwritable:
            self._note.setText(
                "This container has no writer, so saving writes plain bytes "
                "without re-wrapping them."
            )

    def container_id(self) -> str:
        return self._container.currentData()

    @staticmethod
    def get_container(
        parent: QWidget | None,
        registry: Registry,
        *,
        path: str,
        container_id: str = RAW_READ,
    ) -> str | None:
        """Run the dialog modally; the chosen container id, or None on cancel."""
        dialog = ContainerDialog(
            registry, path=path, container_id=container_id, parent=parent
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return dialog.container_id()
