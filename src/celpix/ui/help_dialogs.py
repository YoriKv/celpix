"""The Help menu's two modal dialogs: the shortcut guide and About.

The guide is **generated from the live menu bar** rather than a hand-written
table. Every keyboard route in the app is already reachable from a menu — either
as a real ``QAction`` shortcut, or (for the bare keys the app-wide event filter
routes) as key text after a tab in the action's label, which Qt renders in the
menu's shortcut column. Walking the menus therefore keeps the guide correct for
free: a new action with a key shows up here without anyone remembering to add it.

What no menu holds is appended as static sections, read from the same tables the
widgets are built from so they stay in sync: the pixel tools' number keys
(:data:`~celpix.ui.tools.TOOL_SPECS`) and the transform bar's flip/rotate letters
(:data:`~celpix.ui.tools.TRANSFORM_SPECS`) — those buttons are glyphs on a toolbar
that swaps with the mode, which is not something a menu row can say. The canvas
gestures, the panel keys and the floating windows' own keys follow, and are the
genuinely hand-maintained part: a drag, a held modifier and a key that belongs to
a window with no menu bar of its own have no other place to be written down.

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
from celpix.ui.tools import TOOL_SPECS, TRANSFORM_SPECS

__all__ = ["AboutDialog", "ShortcutGuide", "shortcut_sections"]

AUTHOR = "Epi"
HOMEPAGE = "https://github.com/YoriKv/celpix"

# Mouse/modifier gestures the canvas answers to. No menu action can express a
# drag or a held modifier, so these and the two tables below are the part of the
# guide that is genuinely hand-maintained. Zoom is not here: it is a menu action,
# whose entry already advertises the wheel gesture alongside its keys.
#
# Ordered by the mode the gesture belongs to, and each row that is not universal
# names its mode: the same button means four different things across tile mode,
# pixel editing, Rearrange and Edit Tiles, and which one it is right now is the
# question this section exists to answer.
CANVAS_GESTURES: tuple[tuple[str, str], ...] = (
    ("Pan the view", "Hold Space + drag"),
    ("Select a range of tiles", "Drag"),
    ("Actions for the selection", "Right-click"),
    ("Square selection (Select tool)", "Shift + drag"),
    ("Select the whole tile (Select tool)", "Double-click"),
    # One key, two answers, in the order the second only happens without the
    # first: Esc lands a float if one is in the air and clears the marquee if not.
    ("Set a floating selection down", "Esc"),
    ("Clear the pixel selection", "Esc"),
    ("Pick a color (while pixel editing)", "Right-click or right-drag"),
    ("Move tiles (Rearrange)", "Drag"),
    ("Select tiles (Rearrange)", "Right-drag"),
    ("Abandon a rearrange drag", "Esc or right-click"),
    ("Lay the picked tile down (Edit Tiles)", "Click or drag"),
    ("Pick the tile a cell names (Edit Tiles)", "Right-click"),
    ("Pick an area of cells as the stamp (Edit Tiles)", "Right-drag"),
    # Not the canvas's own, but a mouse gesture the whole window answers to, and
    # this is where someone looks for one. Its keys are on the Navigate menu.
    ("Back / forward through visited entries", "Mouse 4 / Mouse 5"),
)

# Keys a focused panel claims for itself, which is why they are not on the menu
# bar: while one has focus the canvas's own editing shortcuts yield to it
# (``widgets.take_editing_shortcut``), so Ctrl+C means the color under the
# palette's cursor rather than the tile selection behind the dock. The Tile
# Source rows are the same story told about a key that is *also* bound
# window-wide: Shift+Left/Right is the view's Cols on the Navigate menu, and the
# sheet's own width while the sheet is the thing being typed into.
PANEL_KEYS: tuple[tuple[str, str], ...] = (
    ("Copy / paste a color", "Ctrl+C / Ctrl+V"),
    ("Copy / paste a palette row", "Ctrl+Shift+C / Ctrl+Shift+V"),
    ("Move the color selection", "Arrow keys"),
    ("Step the tile pick", "Arrow keys"),
    ("Sweep tiles as one stamp (Tile Source)", "Right-drag"),
    ("Tile Source columns", "Shift+Left / Shift+Right"),
    ("Zoom / pan the tile sheet", "Ctrl + Scroll / Space + drag"),
    ("Extend the Files selection", "Shift+click / Shift+Up / Shift+Down"),
    ("Add or drop one Files row", "Ctrl+click"),
    ("Reorder the selected Files rows", "Alt+Up / Alt+Down"),
    ("Cut / copy / paste a Files row", "Ctrl+X / Ctrl+C / Ctrl+V"),
    ("Duplicate a Files row", "Ctrl+D"),
    ("Remove the selected Files entries", "Del"),
    ("Filter the Files list", "Ctrl+F"),
)

# The floating windows — Text, Font Alphabet, Subsprites, Animation. They carry
# no menu bar of their own, so nothing above can reach them, and the keys are
# theirs only while the window is the active one: the main window's app-wide
# filters are all gated on that. Undo names its two windows rather than being
# written as a universal, because it is one: the Edit menu's Ctrl+Z cannot fire
# from a separate top-level window, so it reaches the session's single stack
# (``docs/design/undo-redo.md``) only from the two that claim the key back.
TOOL_WINDOW_KEYS: tuple[tuple[str, str], ...] = (
    ("Undo / redo (Text, Font Alphabet)", "Ctrl+Z / Ctrl+Shift+Z"),
    ("Zoom the sheet", "Ctrl + Scroll"),
    ("Pan the sheet (Subsprites, Animation)", "Hold Space + drag"),
    ("Sheet columns (Subsprites)", "Shift+Left / Shift+Right"),
    ("Edit the row's text (Font Alphabet)", "Enter"),
    ("Fill the character down (Font Alphabet)", "Ctrl+V"),
    ("Write the code being typed (Text)", "Ctrl+Return"),
)


def _key_text(action: QAction) -> str:
    """The key(s) ``action`` advertises, or ``""`` when it has none.

    Two routes, and an action can take both. A registered shortcut is the plain
    case; text after a tab in the label is what no ``QKeySequence`` can hold — a
    scroll direction (Zoom In), a list of alternates ("+ / Ctrl+Right"), or a bare
    key that must not be registered because it would be stolen from focused text
    inputs. The menu's shortcut column has room for only one of the two, so this
    is the only place the pair is ever seen together.
    """
    # The primary sequence only. Qt's StandardKey lists carry every historical
    # and media-key alternate for a role (Redo also answers to Ctrl+Y and a bare
    # "Redo" key); showing them all would bury the binding people actually use.
    registered = action.shortcut().toString(QKeySequence.SequenceFormat.NativeText)
    label = action.text()
    advertised = label.split("\t", 1)[1].strip() if "\t" in label else ""
    return " / ".join(part for part in (registered, advertised) if part)


def _label_text(action: QAction) -> str:
    pinned = action.property("guideLabel")
    if pinned:
        return str(pinned)
    # Qt renders "&" as a mnemonic underline, so it is not part of the name.
    return action.text().split("\t", 1)[0].replace("&", "").strip()


def _menu_entries(menu: QMenu) -> list[tuple[str, str]]:
    """Every ``(label, keys)`` pair in ``menu``, submenus flattened in place.

    Actions with no key are dropped — they are reachable by mouse and the menu
    itself is their documentation. A submenu's *own* key is kept where it has
    one: a key that cycles a group of radios (View ▸ Grid) belongs to the group
    rather than to any one of its entries, so the parent is where it is written.
    """
    entries: list[tuple[str, str]] = []
    for action in menu.actions():
        if action.isSeparator():
            continue
        submenu = action.menu()
        if submenu is not None:
            keys = _key_text(action)
            if keys:
                entries.append((_label_text(action), keys))
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
    # One row per letter, plus the Shift axis: the letters act on whichever group
    # the transform bar is showing, so naming the groups here would be four times
    # the rows to say the same thing.
    sections.append(
        (
            "Transform",
            [(spec.label, spec.key) for spec in TRANSFORM_SPECS]
            + [("The block, not each tile", "Shift + the above")],
        ),
    )
    sections.append(("Canvas", list(CANVAS_GESTURES)))
    sections.append(("Panels (while focused)", list(PANEL_KEYS)))
    sections.append(("Floating Windows", list(TOOL_WINDOW_KEYS)))
    return sections


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

        # One rich-text label rather than a stack of them: it keeps the links
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
