"""The Font Alphabet window's table: one row per code, and what it takes to type.

The widgetry the window's lower half is built from — the three columns, the Role
column's combo, the Enter key that opens the Text cell, and the column widths,
which are measured rather than left to the header because a kanji font is four
thousand rows (:func:`code_column_width`). What the rows *say* is the working
copy's (:mod:`celpix.ui.font_alphabet_draft`); what a settled cell does is the
window's (:mod:`celpix.ui.font_alphabet_window`).
"""

from __future__ import annotations

from collections.abc import Iterable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QStyledItemDelegate,
    QTableWidget,
    QTableWidgetItem,
)

from celpix.ui.font_alphabet_draft import ROLE_LABELS
from celpix.ui.widgets import select_only

__all__ = [
    "CODE_COLUMN_PADDING",
    "COL_CODE",
    "COL_ROLE",
    "COL_TEXT",
    "AlphabetTable",
    "RoleDelegate",
    "code_column_width",
    "put_cell",
    "role_column_width",
]

# The three columns, in the order a row is read: which code, what it says, and
# what kind of thing it is.
COL_CODE, COL_TEXT, COL_ROLE = 0, 1, 2

# Room around the widest code label in its column (:func:`_code_column_width`) —
# the cell margins a style puts either side of an item's text, which measuring
# the string alone does not account for. Generous rather than exact: a code
# column a few pixels wide of its text costs nothing, and one a few pixels short
# clips the ``$`` off a row.
CODE_COLUMN_PADDING = 16


class RoleDelegate(QStyledItemDelegate):
    """A combo of the :data:`ROLE_LABELS` for the Role column.

    A delegate rather than a combo per row: a sheet is routinely a thousand
    tiles, and a thousand live widgets to offer a choice made on a handful of
    them is a window that takes a second to open.
    """

    def createEditor(self, parent, option, index) -> QComboBox:  # noqa: ANN001, N802
        editor = QComboBox(parent)
        for role, label in ROLE_LABELS:
            editor.addItem(label, role.value)
        return editor

    def setEditorData(self, editor: QComboBox, index) -> None:  # noqa: ANN001, N802
        at = editor.findText(index.data() or "text")
        editor.setCurrentIndex(max(0, at))

    def setModelData(self, editor: QComboBox, model, index) -> None:  # noqa: ANN001, N802
        model.setData(index, editor.currentText())


class AlphabetTable(QTableWidget):
    """The table, with Enter as a second spelling of double-clicking Text.

    Qt's own edit key is F2 (``EditKeyPressed``), which nobody reaches for on a
    row they have just picked off the sheet: the gesture here is click a tile,
    press Enter, type the letter. Enter always opens the **Text** cell whatever
    column the cursor is in, since that is the one a row exists to answer — the
    code is fixed by the tile and the role is the exception.

    Only the closed table sees this key. Once the editor is open it is a child
    widget with the focus, so Enter reaches the delegate and commits, and typing
    never toggles the editor off and on.
    """

    def keyPressEvent(self, event) -> None:  # noqa: ANN001 — QKeyEvent
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            item = self.item(self.currentRow(), COL_TEXT)
            if item is not None:
                # Explicit, not setCurrentItem: Shift+Enter would otherwise
                # extend the row selection from the anchor (``select_only``).
                select_only(self, item)
                self.editItem(item)
                event.accept()
                return
        super().keyPressEvent(event)


def code_column_width(table: QTableWidget, labels: Iterable[str]) -> int:
    """How wide the Code column has to be to hold ``labels`` — and *all* of them.

    Measured off the labels rather than asked of the header, which is the whole
    reason that column is Fixed: `resizeColumnToContents` samples the rows near
    the top and a font's codes get **longer** further down (``$121`` at the first
    tile, ``$1121`` four thousand rows later), so a sampled width clips exactly
    the rows nobody has scrolled to yet.

    The longest label stands in for the widest one. They are hex digits in one
    font, so the two differ by less than the padding either way — which is the
    delegate's own margins, both sides, and is what a measured string alone does
    not cover.
    """
    metrics = table.fontMetrics()
    widest = max(labels, key=len, default="")
    heading = table.horizontalHeaderItem(COL_CODE)
    return (
        max(
            metrics.horizontalAdvance(widest),
            metrics.horizontalAdvance(heading.text() if heading else ""),
        )
        + CODE_COLUMN_PADDING
    )


def role_column_width() -> int:
    """How wide the Role column has to be to hold its editor, plus a little air.

    Measured off a combo with **no parent**, which is dropped on return: parented
    to the table it would outlive the measurement as a stray child, and every
    input in this window is expected to carry a tooltip.
    """
    probe = QComboBox()
    for _role, label in ROLE_LABELS:
        probe.addItem(label)
    return probe.sizeHint().width() + 12


def put_cell(table: QTableWidget, row: int, column: int, text: str) -> None:
    """Set one editable cell's text, reusing the item already there."""
    item = table.item(row, column)
    if item is None:
        table.setItem(row, column, QTableWidgetItem(text))
    elif item.text() != text:
        item.setText(text)
