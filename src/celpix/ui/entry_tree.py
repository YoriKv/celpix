"""The Files dock's tree: selection, row keys and drag-to-reorder.

The widget half of :class:`~celpix.ui.file_list_panel.FileListPanel` — what the
list does with the keyboard and the mouse before the panel decides what any of
it *means*. It knows rows and reports entries; it never reads the workspace.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QAbstractItemView, QTreeWidget, QTreeWidgetItem, QWidget

from celpix.ui.widgets import ShortcutIsland

__all__ = ["DUPLICATE_KEY", "EntryTree"]

# Duplicate a row. Not a QKeySequence.StandardKey: Qt has no standard for it, and
# Ctrl+D is what every list-of-things editor spells it as. Named here because the
# tree matches the key and the context menu labels it, and the two must agree.
DUPLICATE_KEY = QKeySequence("Ctrl+D")


class EntryTree(ShortcutIsland, QTreeWidget):
    """A tree that records when a selection change is driven by the keyboard,
    and owns the Delete key while it has focus.

    Selecting a row loads it into the view, which normally hands focus to the
    canvas so arrow keys drive the pixels. But while the user is *browsing* the
    list with the arrow keys, stealing focus mid-scroll would break the very
    keys they are navigating with. The flag is true only for the duration of a
    key-driven selection change (it wraps the base handler that emits
    ``currentItemChanged``), so the panel can tell an arrow-key move apart from a
    click and keep focus on the list for the former.

    While it has focus it is a :class:`~celpix.ui.widgets.ShortcutIsland`, so the
    canvas editing shortcuts don't act on the canvas selection from here. That is
    also what disambiguates Delete, which the list binds too: left to compete with
    the canvas's Clear, Qt sees two claims on the key and fires neither, so it
    silently does nothing. Delete removes the entry and Cut/Copy/Paste act on the
    rows rather than on the tiles behind them; the arrow keys reach the tree's own
    navigation through the app-wide filter that already yields to this widget.

    Duplicate is the one key here that is *not* claimed from anywhere — Ctrl+D is
    bound nowhere else in the window, so it arrives as an ordinary press and only
    has to be recognised before the base class sees it.

    Selection is **extended**: Shift for a range, Ctrl for one more row, and
    Shift+arrows for a range from the keyboard — which is what those keys mean in
    every list, and so is what they mean here. Reordering rows has no such
    convention to borrow, and takes **Alt+Up/Down**. That reaches this widget for
    the same reason Alt+Left/Right reach the window's Back and Forward: the
    app-wide navigation filter declines anything carrying Alt
    (``NavigationMixin._handle_nav_key``), so the key arrives here as an ordinary
    press.
    """

    delete_pressed = Signal()  # Delete with the list focused - remove the entries
    move_pressed = Signal(int)  # Alt+Up/Down - reorder by -1 / +1
    cut_pressed = Signal()
    copy_pressed = Signal()
    paste_pressed = Signal()
    duplicate_pressed = Signal()  # Ctrl+D
    # A finished internal drag: the dragged entry, and the entry whose row it
    # should land in front of (None for last among its siblings).
    reorder_dropped = Signal(object, object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.key_navigating = False
        # Shift for a range, Ctrl for one more row - what a user reaches for to
        # remove or move a handful of entries in one gesture. What a multi-row
        # selection then *means* is the panel's question, not the tree's.
        self.setSelectionMode(QTreeWidget.SelectionMode.ExtendedSelection)
        # Rows are dragged to reorder them and nothing else, so the drag never
        # leaves the widget and never carries a payload of its own: the drop
        # handler below reads the dragged row off the tree and reports a position
        # for the *model* to move, rather than letting the view move an item the
        # workspace still believes is somewhere else.
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QTreeWidget.DragDropMode.InternalMove)
        self._dragged: QTreeWidgetItem | None = None

    def keyPressEvent(self, event) -> None:
        if event.matches(QKeySequence.StandardKey.Delete):
            self.delete_pressed.emit()
            event.accept()
            return
        for sequence, signal in (
            (QKeySequence.StandardKey.Cut, self.cut_pressed),
            (QKeySequence.StandardKey.Copy, self.copy_pressed),
            (QKeySequence.StandardKey.Paste, self.paste_pressed),
        ):
            if event.matches(sequence):
                signal.emit()
                event.accept()
                return
        # Compared as a sequence, not with ``matches``, which only speaks
        # StandardKey — and Qt has no standard key for Duplicate.
        if QKeySequence(event.keyCombination()) == DUPLICATE_KEY:
            self.duplicate_pressed.emit()
            event.accept()
            return
        if event.modifiers() == Qt.KeyboardModifier.AltModifier and event.key() in (
            Qt.Key.Key_Up,
            Qt.Key.Key_Down,
        ):
            self.move_pressed.emit(-1 if event.key() == Qt.Key.Key_Up else 1)
            event.accept()
            return
        self.key_navigating = True
        try:
            super().keyPressEvent(event)  # emits currentItemChanged synchronously
        finally:
            self.key_navigating = False

    def step_row(self, delta: int) -> None:
        """Move to the row above (``delta < 0``) or below, exactly as Up/Down do
        with the list focused — the same cursor walk, so a collapsed group, a row
        the filter hid and the Palettes header are passed over the same way.

        Flagged as a keyboard move for the reason a real arrow press is: the
        activation it causes leaves focus wherever the user is, rather than
        handing it to the canvas between one step and the next.
        """
        action = (
            QAbstractItemView.CursorAction.MoveDown
            if delta > 0
            else QAbstractItemView.CursorAction.MoveUp
        )
        index = self.moveCursor(action, Qt.KeyboardModifier.NoModifier)
        if not index.isValid() or index == self.currentIndex():
            return
        self.key_navigating = True
        try:
            self.setCurrentIndex(index)  # clears and selects, as the arrow does
        finally:
            self.key_navigating = False
        self.scrollTo(index)

    # -- reordering by drag ---------------------------------------------------
    def startDrag(self, actions) -> None:  # noqa: ANN001, N802 — Qt override
        # Which row is being dragged is read here rather than off the drop's mime
        # data: the drag never leaves this widget, so the item itself is the
        # honest handle, and Qt's default encoding would only give it back as a
        # row number in a tree that is about to change shape.
        # A drag moves the one row it started on, so it has nothing to say about
        # a set of them: refused outright rather than quietly moving the row
        # under the cursor and leaving the rest behind. Move Up/Down is what
        # reorders a multi-row selection.
        if len(self.selectedItems()) > 1:
            return
        self._dragged = self.currentItem()
        # Qt accepts a drop *between* two rows only when their **parent** is a
        # drop target, so the group being rearranged is opened for the length of
        # the drag and closed again after — without this a drag over a sibling
        # shows the "no drop" cursor and no indicator line, which is every
        # reorder this panel offers. It is opened only for the drag because the
        # rest of the time nothing here is a drop target at all: a row that is
        # one would also offer a drop *onto* it (see ``_drop_before``).
        group = self._dragged.parent() if self._dragged is not None else None
        if group is not None:
            group.setFlags(group.flags() | Qt.ItemFlag.ItemIsDropEnabled)
        try:
            super().startDrag(actions)
        finally:
            if group is not None:
                group.setFlags(group.flags() & ~Qt.ItemFlag.ItemIsDropEnabled)
            self._dragged = None

    def _drop_before(
        self,
        event,  # noqa: ANN001 — QDragMoveEvent / QDropEvent
    ) -> tuple[QTreeWidgetItem, QTreeWidgetItem | None] | None:
        """The dragged item and the sibling it would land in front of, or None
        when this drop is not a reorder we allow.

        Two rules, and both are about keeping a drag from meaning more than it
        says. The drop must land **between** rows, never *on* one: a row taken
        into another would be a re-pointing — a slice reading a different file's
        offsets — which is a decision to make in a dialog, not by aiming. And the
        two rows must be **siblings**, so a drag stays inside the group whose
        order it is changing rather than moving an entry between a section and a
        file's children.
        """
        source = self._dragged
        if source is None:
            return None
        target = self.itemAt(event.position().toPoint())
        if target is None or target is source:
            return None
        # A section header has no parent and carries no entry, so a drag from one
        # (or onto one) fails the sibling test rather than needing its own check.
        parent = source.parent()
        if parent is None or target.parent() is not parent:
            return None
        position = self.dropIndicatorPosition()
        if position is QTreeWidget.DropIndicatorPosition.AboveItem:
            return source, target
        if position is QTreeWidget.DropIndicatorPosition.BelowItem:
            index = parent.indexOfChild(target) + 1
            after = parent.child(index) if index < parent.childCount() else None
            return source, after
        return None

    def dragMoveEvent(self, event) -> None:  # noqa: ANN001, N802 — Qt override
        # The base class decides where the indicator is drawn, so it runs first;
        # what it accepted is then overruled for anything the rules above refuse,
        # which is what makes an illegal target show the "no drop" cursor rather
        # than accepting and quietly doing nothing.
        super().dragMoveEvent(event)
        if self._drop_before(event) is None:
            event.ignore()

    def dropEvent(self, event) -> None:  # noqa: ANN001, N802 — Qt override
        landing = self._drop_before(event)
        if landing is None:
            event.ignore()
            return
        source, before = landing
        # Accepted, but as **IgnoreAction** and without the base class: Qt's own
        # internal move would take the row out and put it back on its own, leaving
        # the view holding an order the workspace never agreed to (and no undo
        # step for it). The signal is the whole of what a drop does; the row moves
        # when the model has.
        event.setDropAction(Qt.DropAction.IgnoreAction)
        event.accept()
        self.reorder_dropped.emit(
            source.data(0, Qt.ItemDataRole.UserRole),
            before.data(0, Qt.ItemDataRole.UserRole) if before is not None else None,
        )
