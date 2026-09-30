"""The Help menu's two modal dialogs: the shortcut guide and About.

The guide lays out what :func:`~celpix.ui.shortcut_table.shortcut_sections`
reads off the live menu bar and the static tables beside it; About is
:class:`~celpix.ui.about_dialog.AboutDialog`, re-exported here so the Help menu
imports both from one place.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from celpix.ui.about_dialog import AboutDialog
from celpix.ui.shortcut_table import Section, shortcut_sections
from celpix.ui.widgets import dialog_buttons

__all__ = ["AboutDialog", "ShortcutGuide", "shortcut_sections"]


def _section_widget(title: str, entries: list[tuple[str, str]]) -> QWidget:
    """One titled two-column section: names on the left, keys on the right."""
    box = QWidget()
    layout = QVBoxLayout(box)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(2)
    heading = QLabel(title)
    font = heading.font()
    font.setBold(True)
    heading.setFont(font)
    layout.addWidget(heading)
    rule = QFrame()
    rule.setFrameShape(QFrame.Shape.HLine)
    rule.setFrameShadow(QFrame.Shadow.Sunken)
    layout.addWidget(rule)
    grid = QGridLayout()
    grid.setContentsMargins(0, 0, 0, 0)
    grid.setHorizontalSpacing(18)
    grid.setVerticalSpacing(1)
    grid.setColumnStretch(0, 1)
    for row, (name, keys) in enumerate(entries):
        grid.addWidget(QLabel(name), row, 0)
        key_label = QLabel(keys)
        key_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        key_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        grid.addWidget(key_label, row, 1)
    layout.addLayout(grid)
    return box


def _balanced_columns(sections: list[Section], count: int = 2) -> list[list[Section]]:
    """Split sections across ``count`` columns, keeping each column's height even.

    Sections stay whole and in order; each goes to whichever column is shortest
    so far. Navigate alone is longer than most of the others together, so a naive
    halfway split would leave one column nearly empty.
    """
    columns: list[list[Section]] = [[] for _ in range(count)]
    heights = [0] * count
    for section in sections:
        target = heights.index(min(heights))
        columns[target].append(section)
        heights[target] += len(section[1]) + 2  # rows plus the heading and rule
    return columns


class ShortcutGuide(QDialog):
    """Help ▸ Shortcuts: every key the app answers to, in one modal page."""

    def __init__(
        self,
        sections: list[Section],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Shortcuts")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

        body = QWidget()
        columns = QHBoxLayout(body)
        columns.setContentsMargins(12, 12, 12, 12)
        columns.setSpacing(28)
        for column in _balanced_columns(sections):
            lane = QVBoxLayout()
            lane.setSpacing(14)
            for title, entries in column:
                lane.addWidget(_section_widget(title, entries))
            lane.addStretch(1)
            columns.addLayout(lane)

        # Scrolled rather than sized to fit: the list grows with the app, and a
        # short screen must still be able to reach the buttons.
        scroll = QScrollArea()
        scroll.setWidget(body)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)

        layout = QVBoxLayout(self)
        layout.addWidget(scroll)
        dialog_buttons(self, layout, close_only=True)
        # Sized to the width the two columns actually need rather than to a
        # remembered number: the guide is generated from the live menu bar, so an
        # action added anywhere can push a column past a hardcoded width and
        # leave the dialog opening with a horizontal scrollbar over its own text.
        # The scrollbar allowance is for the vertical one, which the tall list
        # will have and which would otherwise eat that much of the columns. The
        # height stays a starting size, not a limit -- the list grows with the
        # app, and a short screen must still be able to reach the buttons.
        margins = layout.contentsMargins()
        width = (
            body.sizeHint().width()
            + scroll.verticalScrollBar().sizeHint().width()
            + 2 * scroll.frameWidth()
            + margins.left()
            + margins.right()
        )
        self.resize(min(width, self.screen().availableGeometry().width()), 620)
