"""The Help menu's two modal dialogs: the shortcut guide and About.

The guide is **generated from the live menu bar** rather than a hand-written
table. Every keyboard route in the app is already reachable from a menu — either
as a real ``QAction`` shortcut, or (for the bare keys the app-wide event filter
routes) as key text after a tab in the action's label, which Qt renders in the
menu's shortcut column. Walking the menus therefore keeps the guide correct for
free: a new action with a key shows up here without anyone remembering to add it.

Two things menus can't express are appended as static sections: the pixel tools'
number keys (read from :data:`~celpix.ui.tools.TOOL_SPECS`, so they stay in sync)
and the mouse gestures.

An action whose menu label is rebuilt at runtime — Undo/Redo carry the name of
the command they would undo — can set a ``guideLabel`` property to pin the text
the guide shows.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QKeySequence, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMenu,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from celpix import __version__, resources
from celpix.ui.tools import TOOL_SPECS

__all__ = ["AboutDialog", "ShortcutGuide", "shortcut_sections"]

AUTHOR = "Epi"
HOMEPAGE = "https://github.com/YoriKv/celpix"

# Mouse/modifier gestures the canvas answers to. No menu action can express a
# scroll direction or a drag, so these are the one part of the guide that is
# genuinely hand-maintained.
CANVAS_GESTURES: tuple[tuple[str, str], ...] = (
    ("Zoom in / out", "Ctrl + Scroll"),
    ("Pan the view", "Hold Space + drag"),
    ("Pick a color (any tool)", "Right-click"),
    ("Stamp a floating selection", "Esc"),
)


def _key_text(action: QAction) -> str:
    """The key(s) ``action`` advertises, or ``""`` when it has none.

    Label text after a tab wins over a registered shortcut: an action that
    carries both (Zoom In) puts the *gesture* in the label deliberately, and the
    bare-key actions carry only the label form because registering them for real
    would steal the key from focused text inputs.
    """
    label = action.text()
    if "\t" in label:
        return label.split("\t", 1)[1].strip()
    # The primary sequence only. Qt's StandardKey lists carry every historical
    # and media-key alternate for a role (Redo also answers to Ctrl+Y and a bare
    # "Redo" key); showing them all would bury the binding people actually use.
    return action.shortcut().toString(QKeySequence.SequenceFormat.NativeText)


def _label_text(action: QAction) -> str:
    pinned = action.property("guideLabel")
    if pinned:
        return str(pinned)
    # Qt renders "&" as a mnemonic underline, so it is not part of the name.
    return action.text().split("\t", 1)[0].replace("&", "").strip()


def _menu_entries(menu: QMenu) -> list[tuple[str, str]]:
    """Every ``(label, keys)`` pair in ``menu``, submenus flattened in place.

    Actions with no key are dropped — they are reachable by mouse and the menu
    itself is their documentation.
    """
    entries: list[tuple[str, str]] = []
    for action in menu.actions():
        if action.isSeparator():
            continue
        submenu = action.menu()
        if submenu is not None:
            entries.extend(_menu_entries(submenu))
            continue
        keys = _key_text(action)
        if keys:
            entries.append((_label_text(action), keys))
    return entries


def shortcut_sections(window) -> list[tuple[str, list[tuple[str, str]]]]:  # noqa: ANN001 - QMainWindow
    """The guide's contents: one section per menu, then tools and gestures.

    Separated from the dialog so the mapping can be tested without opening a
    modal, which the offscreen platform can never answer.
    """
    sections: list[tuple[str, list[tuple[str, str]]]] = []
    for action in window.menuBar().actions():
        menu = action.menu()
        if menu is None:
            continue
        entries = _menu_entries(menu)
        if entries:
            sections.append((_label_text(action), entries))
    sections.append(
        ("Pixel Tools", [(spec.label, spec.key) for spec in TOOL_SPECS]),
    )
    sections.append(("Canvas", list(CANVAS_GESTURES)))
    return sections


def _section_widget(title: str, entries: list[tuple[str, str]]) -> QWidget:
    """One titled two-column block: names on the left, keys on the right."""
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


def _balanced_columns(
    sections: list[tuple[str, list[tuple[str, str]]]], count: int = 2
) -> list[list[tuple[str, list[tuple[str, str]]]]]:
    """Split sections across ``count`` columns, keeping each column's height even.

    Sections stay whole and in order; each goes to whichever column is shortest
    so far. Navigate alone is longer than most of the others together, so a naive
    halfway split would leave one column nearly empty.
    """
    columns: list[list[tuple[str, list[tuple[str, str]]]]] = [[] for _ in range(count)]
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
        sections: list[tuple[str, list[tuple[str, str]]]],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("celPix - Shortcuts")
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

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)

        layout = QVBoxLayout(self)
        layout.addWidget(scroll)
        layout.addWidget(buttons)
        # Wide enough for two columns without wrapping a key label; the height is
        # a starting size, not a limit.
        self.resize(760, 620)


class AboutDialog(QDialog):
    """Help ▸ About: what this is, which version, who wrote it, and the license."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("About celPix")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

        icon = QLabel()
        pixmap = QPixmap()
        pixmap.loadFromData(resources.read_bytes("icons", "app.png"))
        icon.setPixmap(
            pixmap.scaled(
                64,
                64,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )
        icon.setAlignment(Qt.AlignmentFlag.AlignTop)

        # One rich-text block rather than a stack of labels: it keeps the links
        # clickable and the whole thing selectable for a bug report.
        text = QLabel(
            f"<h2 style='margin-bottom:2px'>celPix {__version__}</h2>"
            "<p style='margin-top:0'>A graphics viewer and editor for romhacking"
            " and research.</p>"
            f"<p>By <b>{AUTHOR}</b><br>"
            f"<a href='{HOMEPAGE}'>{HOMEPAGE}</a></p>"
            "<p>Released under the MIT license. Built on Python and Qt via"
            " PySide6, which is licensed under the LGPLv3.</p>"
        )
        text.setWordWrap(True)
        text.setOpenExternalLinks(True)
        text.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextBrowserInteraction
            | Qt.TextInteractionFlag.TextSelectableByMouse
        )

        top = QHBoxLayout()
        top.setSpacing(14)
        top.addWidget(icon)
        top.addWidget(text, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(buttons)
        self.setMinimumWidth(420)
