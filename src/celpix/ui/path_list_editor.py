"""An ordered list of file paths, edited one row at a time.

A graphics region is not always one file — an arcade board's tiles routinely
live on several ROM chips that mean nothing apart
(:class:`~celpix.plugins.base.FileRef`) — and nothing in the files says which
chip comes first, so the order is the user's to state. Hence a list arranged by
hand, one file per row, rather than a multi-select that would hand back whatever
order the file picker felt like and interleave a sprite sheet wrong without ever
failing. The container dialog is where a region's list is edited
(:class:`~celpix.ui.container_dialog.ContainerDialog`).
"""

from __future__ import annotations

from functools import partial
from os.path import dirname
from typing import NamedTuple

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

__all__ = ["PathListEditor"]

# Past this many rows the list scrolls instead of the editor growing: a board
# with sixteen graphics ROMs would otherwise run off the bottom of the screen.
_VISIBLE_ROWS = 4


class _FileRow(NamedTuple):
    """One file's row: the path it shows and the four buttons that act on it.

    The buttons wire themselves and no production code reaches back for them,
    but they are what the editor's tests drive, so the row carries them.
    """

    widget: QWidget
    field: QLineEdit
    up: QToolButton
    down: QToolButton
    browse: QToolButton
    remove: QToolButton


class PathListEditor(QWidget):
    """The rows of paths, and an Append File button under them.

    The list is the editor's model and the rows are rebuilt from it after every
    edit, rather than widgets being shuffled between positions: the order on
    screen is then the order that will be applied, by construction, and a move
    can't leave the two disagreeing about which chip is first. A region is at
    least one file, so the last row cannot be removed.
    """

    #: The list changed — a move, a browse, an append or a removal.
    paths_changed = Signal()

    def __init__(
        self,
        paths: tuple[str, ...] | list[str],
        tip: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._paths: list[str] = [p for p in paths if p] or [""]
        self._rows: list[_FileRow] = []

        self._rows_layout = QVBoxLayout()
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        rows_host = QWidget()
        rows_host.setLayout(self._rows_layout)
        self._scroll = QScrollArea()
        self._scroll.setWidget(rows_host)
        self._scroll.setWidgetResizable(True)
        self._scroll.setToolTip(tip)

        self._append = QPushButton("Append File")
        self._append.setToolTip("Append another file to the region")
        self._append.clicked.connect(self._append_file)
        # Sized to its own text, left under the list it adds to: stretched across
        # the dialog it would read as the primary action, which OK is.
        append_row = QHBoxLayout()
        append_row.setContentsMargins(0, 0, 0, 0)
        append_row.addWidget(self._append)
        append_row.addStretch(1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._scroll)
        layout.addLayout(append_row)
        self._rebuild_rows(announce=False)

    def paths(self) -> tuple[str, ...]:
        """The files, in join order."""
        return tuple(self._paths)

    def set_paths(self, paths: tuple[str, ...] | list[str]) -> None:
        """Replace the whole list."""
        self._paths = [p for p in paths if p] or [""]
        self._rebuild_rows()

    def _rebuild_rows(self, *, announce: bool = True) -> None:
        """Re-make every row from :attr:`_paths`."""
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
        if announce:
            self.paths_changed.emit()

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
                "Remove this file from the region",
                last > 0,
                partial(self._remove, index),
            ),
        )
        made = []
        for mark, tip, enabled, handler in specs:
            button = QToolButton()
            button.setText(mark)
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
