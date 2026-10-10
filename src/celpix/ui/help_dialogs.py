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
    QLineEdit,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from celpix.ui.about_dialog import AboutDialog
from celpix.ui.searchable_combo import matches_search
from celpix.ui.shortcut_table import Section, shortcut_sections
from celpix.ui.widgets import dialog_buttons

__all__ = ["AboutDialog", "ShortcutGuide", "filtered_sections", "shortcut_sections"]


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


def filtered_sections(sections: list[Section], query: str) -> list[Section]:
    """The rows of ``sections`` that carry every word of ``query``, in any order.

    A row is read together with its section's title and its keys, so "palette"
    keeps the whole Palette section and "ctrl shift" every Ctrl+Shift key — what
    the guide is opened to answer is as often "what does this key do" as "which
    key does this". The files dock's matching rule
    (:func:`~celpix.ui.searchable_combo.matches_search`); a section left with no
    rows is dropped, heading and all.
    """
    needles = query.lower().split()
    if not needles:
        return sections
    kept: list[Section] = []
    for title, entries in sections:
        rows = [
            (name, keys)
            for name, keys in entries
            if matches_search(f"{title} {name} {keys}", needles)
        ]
        if rows:
            kept.append((title, rows))
    return kept


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
        self._sections = sections

        # Typed into the moment the guide opens: it is a long page, and the
        # question it is opened for is usually one key or one action.
        self._filter = QLineEdit()
        self._filter.setPlaceholderText("Filter")
        self._filter.setClearButtonEnabled(True)
        self._filter.setToolTip(
            "Show the shortcuts whose name, keys or section\n"
            "carry every word typed, in any order"
        )
        self._filter.textChanged.connect(self._apply_filter)

        # Scrolled rather than sized to fit: the list grows with the app, and a
        # short screen must still be able to reach the buttons.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll = scroll
        body = self._build_body(sections)

        layout = QVBoxLayout(self)
        layout.addWidget(self._filter)
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
        self._filter.setFocus()

    def _build_body(self, sections: list[Section]) -> QWidget:
        """Lay ``sections`` out in balanced columns as the scroll area's page.

        Rebuilt whole on every filter change rather than hiding rows in place,
        so what is left is re-balanced across the columns instead of standing in
        the gaps the hidden rows leave.
        """
        body = QWidget()
        columns = QHBoxLayout(body)
        columns.setContentsMargins(12, 12, 12, 12)
        columns.setSpacing(28)
        if not sections:
            columns.addWidget(
                QLabel("No shortcut matches."), 0, Qt.AlignmentFlag.AlignTop
            )
        for column in _balanced_columns(sections):
            lane = QVBoxLayout()
            lane.setSpacing(14)
            for title, entries in column:
                lane.addWidget(_section_widget(title, entries))
            lane.addStretch(1)
            columns.addLayout(lane)
        self._scroll.setWidget(body)  # takes ownership; the old page is deleted
        return body

    def _apply_filter(self, text: str) -> None:
        self._build_body(filtered_sections(self._sections, text))
