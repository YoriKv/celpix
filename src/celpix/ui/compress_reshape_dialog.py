"""The New Compress & Reshape Plugin dialog: pair a scheme with a second pass.

A game that runs a pass over what its unpacker produced — a running sum over a map
stored as differences is the ordinary one — needs two transforms where a slice has
one Compression slot. The pair is a five-line TOML preset
(:mod:`celpix.plugins.compress_reshape`), and this dialog is what writes it, so the
user picks two rows from the pickers they already know rather than typing two ids.

It writes into the **project's** ``plugins/compression/`` folder. A pair is a fact
about one game's loader, which is what a project's own plugin root is for: the
file then travels with the ``.celpix`` that names it, and being data it opens on
the next machine without a trust prompt.

The dialog only collects and validates; the window writes the file and refreshes
the registry, since those are the two steps that can fail after OK.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QWidget,
)

from celpix.core.errors import Stage
from celpix.plugins.base import NO_COMPRESSION, NO_RESHAPE, writes_back
from celpix.plugins.compress_reshape import (
    ID_PREFIX,
    CompressReshape,
    preset_path,
    slug,
)
from celpix.plugins.registry import Registry
from celpix.ui.searchable_combo import SearchableComboBox, fill_stage_combo
from celpix.ui.theme import ERROR_INK, set_ink
from celpix.ui.widgets import PRESET_COMBO_WIDTH

__all__ = ["CompressReshapeDialog", "CompressReshapeParams"]


@dataclass(frozen=True)
class CompressReshapeParams:
    """What the dialog was OK'd with — everything the preset file states."""

    plugin_id: str
    name: str
    compression_id: str
    reshape_id: str


class CompressReshapeDialog(QDialog):
    def __init__(
        self,
        registry: Registry,
        plugin_root: str | Path,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("celPix - new Compress & Reshape plugin")
        self._registry = registry
        self._plugin_root = Path(plugin_root)
        self._params: CompressReshapeParams | None = None

        self._compression = SearchableComboBox(PRESET_COMBO_WIDTH)
        self._compression.setToolTip(
            "The scheme the data is packed with.\n"
            "It reads the file's bytes, so whether the pair finds its\n"
            "own end, and any inputs it needs, are this half's."
        )
        # Neither pass-through, and no pair: half a pair is a plugin that already
        # exists under its own picker, and the loader refuses all three anyway
        # (`compress_reshape._member`) — offering them would be offering an error.
        fill_stage_combo(
            self._compression,
            [
                plugin
                for plugin in registry.plugins(Stage.COMPRESSION)
                if plugin.info.id != NO_COMPRESSION
                and not isinstance(plugin, CompressReshape)
            ],
            "",
        )
        self._reshape = SearchableComboBox(PRESET_COMBO_WIDTH)
        self._reshape.setToolTip(
            "The pass run over the decompressed bytes on load,\n"
            "and undone before compressing on save.\n"
            "It is handed the whole decompressed stream as its region."
        )
        fill_stage_combo(
            self._reshape,
            [
                plugin
                for plugin in registry.plugins(Stage.RESHAPE)
                if plugin.info.id != NO_RESHAPE
            ],
            "",
        )
        self._name = QLineEdit()
        self._name.setToolTip(
            "What the Compression picker calls the pair.\n"
            "Blank uses the two halves' names."
        )
        self._saved_as = QLabel()
        self._saved_as.setToolTip(
            "The plugin's id, which a slice using it is saved with,\n"
            "and the file it is written to in the project's plugins folder.\n"
            "Both follow from the name."
        )
        self._note = QLabel()
        self._note.hide()
        self._error = QLabel()
        set_ink(self._error, ERROR_INK)
        self._error.hide()

        form = QFormLayout(self)
        form.addRow("Compression:", self._compression)
        form.addRow("Then reshape:", self._reshape)
        form.addRow("Name:", self._name)
        form.addRow("Saved as:", self._saved_as)
        form.addRow(self._note)
        form.addRow(self._error)
        for field in (self._compression, self._reshape, self._name, self._saved_as):
            label = form.labelForField(field)
            if label is not None:
                label.setToolTip(field.toolTip())
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._validate_and_accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

        self._compression.currentIndexChanged.connect(self._sync)
        self._reshape.currentIndexChanged.connect(self._sync)
        self._name.textChanged.connect(self._sync)
        self._sync()

    # -- derived state -----------------------------------------------------
    def _default_name(self) -> str:
        return f"{self._compression.currentText()} + {self._reshape.currentText()}"

    def name(self) -> str:
        return self._name.text().strip() or self._default_name()

    def plugin_id(self) -> str:
        """The id the name implies; bare :data:`ID_PREFIX` when it implies none."""
        return ID_PREFIX + slug(self.name())

    def _sync(self) -> None:
        self._name.setPlaceholderText(self._default_name())
        plugin_id = self.plugin_id()
        path = preset_path(self._plugin_root, plugin_id)
        self._saved_as.setText(
            f"{plugin_id}\n{self._plugin_root.name}/{path.parent.name}/{path.name}"
        )
        self._note.setText(self._view_only_note())
        self._note.setVisible(bool(self._note.text()))
        self._error.hide()

    def _view_only_note(self) -> str:
        """Which half cannot run backwards, said before the choice is committed.

        The container dialog's rule: a pair with a read-only half is worth making
        — it still *shows* the data — but the user should learn that here rather
        than from a greyed-out Write on every slice they then build on it.
        """
        halves = (
            (Stage.COMPRESSION, self._compression, "cannot compress"),
            (Stage.RESHAPE, self._reshape, "has no inverse"),
        )
        missing = [
            f"{combo.currentText()} {why}"
            for stage, combo, why in halves
            if combo.currentData() is not None
            and not writes_back(
                self._registry.plugin(stage, combo.currentData()), stage
            )
        ]
        if not missing:
            return ""
        return (
            f"{' and '.join(missing)}, so data read through\nthis pair opens view-only."
        )

    # -- accept ------------------------------------------------------------
    def _fail(self, message: str) -> None:
        self._error.setText(message)
        self._error.show()

    def _validate_and_accept(self) -> None:
        compression_id = self._compression.currentData()
        reshape_id = self._reshape.currentData()
        if compression_id is None or reshape_id is None:
            self._fail("Pick a compression scheme and a reshape.")
            return
        plugin_id = self.plugin_id()
        if plugin_id == ID_PREFIX:
            self._fail("The name needs at least one letter or digit.")
            return
        # Against the live id only, not through the rename table: a retired name
        # is free for a user's own plugin to take (`Registry.plugin`).
        taken = {p.info.id for p in self._registry.plugins(Stage.COMPRESSION)}
        if plugin_id in taken:
            self._fail(f"{plugin_id} already exists. Choose another name.")
            return
        path = preset_path(self._plugin_root, plugin_id)
        if path.exists():
            self._fail(f"{path.name} is already in the project's plugins folder.")
            return
        self._params = CompressReshapeParams(
            plugin_id, self.name(), compression_id, reshape_id
        )
        self.accept()

    @staticmethod
    def ask(
        parent: QWidget | None, registry: Registry, plugin_root: str | Path
    ) -> CompressReshapeParams | None:
        """Run the dialog modally; what it was OK'd with, or ``None`` if cancelled."""
        dialog = CompressReshapeDialog(registry, plugin_root, parent)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return dialog._params
