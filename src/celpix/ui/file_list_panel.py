"""The open-files dock panel: every open file, with its slices and bookmarks
nested under it, grouped into sections by what the files hold.

A thin Qt view over the workspace model — the main window forwards workspace
callbacks into the ``add_entry``/``remove_entry``/``set_current``/``refresh_entry``
slots and listens to the signals; the panel itself never mutates the workspace.
Built on QTreeWidget rather than a hand-painted widget: unlike the palette
swatches or the canvas, a document list has no custom pixel presentation — it
wants exactly the selection, nesting, keyboard and context-menu behaviour the
framework already provides.

Entries live under non-selectable section headers — Pixels, Tilemaps, Palettes
— in that fixed order. A header exists only while its section has entries and
carries no entry of its own, so every handler that reads an item's entry data
must tolerate ``None``.

Sections group by :class:`~celpix.core.capabilities.ContentKind`, which is what
an entry *holds*; the tree's nesting stays the other question, "a window into
that file's bytes" (``docs/design/tilemap-entry.md`` §2). The two are allowed to
disagree — a tilemap slice of a ROM appears under Tilemaps, nested beneath a
pixel file that sits under Pixels.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QSize, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QIcon,
    QImage,
    QKeySequence,
    QPalette,
    QPixmap,
)
from PySide6.QtWidgets import (
    QHeaderView,
    QMenu,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from celpix import resources
from celpix.core.address import format_hex
from celpix.core.capabilities import Capability, ContentKind
from celpix.core.notices import Notice
from celpix.plugins.detect import container_label
from celpix.plugins.registry import Registry
from celpix.project.workspace import (
    Entry,
    EntryKind,
    SortKey,
    data_missing,
    entry_notices,
    entry_palette_path,
    palette_missing,
)
from celpix.ui import clipboard
from celpix.ui.widgets import (
    ShortcutIsland,
    icon_cache_key,
    signals_blocked,
    tinted_icon,
)

# Translucent amber behind an entry that needs the user's attention — a missing
# referenced file, or a container that had to assume something. Reads as a
# warning over either light or dark row backgrounds without fighting the
# selection highlight. Which of the two it is, is what the status icon says.
_MISSING_HIGHLIGHT = QBrush(QColor(255, 193, 7, 70))

# Alpha for the tint behind the entry currently on the canvas (the theme's
# Highlight, see _open_entry_wash). Deliberately fainter than the amber above:
# that one is asking to be dealt with, this one only says "here".
_OPEN_ENTRY_ALPHA = 45

# The status icons' own color: opaque amber, matching the row wash it sits on.
# Fixed rather than a palette role, because a warning that took the theme's text
# color would stop reading as a warning.
_WARNING_INK = QColor(200, 137, 10)

# The section headings, in the order they appear. Dict order *is* the on-screen
# order, so a header inserts at its own place however the sections were opened:
# what you hold most of the time first, what is applied onto it last.
SECTIONS: dict[ContentKind, str] = {
    ContentKind.PIXELS: "Pixels",
    ContentKind.TILEMAP: "Tilemaps",
    ContentKind.PALETTE: "Palettes",
}

# The slice/bookmark icon box. Narrower than the default 16px decoration so a
# centred icon sits close to the entry name rather than across a wide gap; the
# icons are painted at exactly this size so nothing is scaled.
_ICON_W = 13
_ICON_H = 16

# The status column, right of the name: the *kind* marker already owns column 0's
# icon slot (a slice's picture, a bookmark's ribbon), and a row can be both a
# slice and in trouble — so the two cannot share one slot.
_STATUS_COL = 1
_STATUS_W = _ICON_W + 8  # the icon box plus breathing room from the name

# Duplicate a row. Not a QKeySequence.StandardKey: Qt has no standard for it, and
# Ctrl+D is what every list-of-things editor spells it as. Named here because the
# tree matches the key and the context menu labels it, and the two must agree.
DUPLICATE_KEY = QKeySequence("Ctrl+D")


class _EntryTree(ShortcutIsland, QTreeWidget):
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

    Shift+arrows reach it the same way the editing keys do, and it claims the
    vertical pair to reorder rows (they resize the view window everywhere else) —
    the keyboard spelling of the drag. Selection is single-item here, so nothing
    is lost: Shift+Up/Down would otherwise be a second way to spell the bare
    arrows.
    """

    delete_pressed = Signal()  # Delete with the list focused - remove the entry
    move_pressed = Signal(int)  # Shift+Up/Down - reorder by -1 / +1
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
        if event.modifiers() == Qt.KeyboardModifier.ShiftModifier and event.key() in (
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

    # -- reordering by drag ---------------------------------------------------
    def startDrag(self, actions) -> None:  # noqa: ANN001, N802 — Qt override
        # Which row is being dragged is read here rather than off the drop's mime
        # data: the drag never leaves this widget, so the item itself is the
        # honest handle, and Qt's default encoding would only give it back as a
        # row number in a tree that is about to change shape.
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


class FileListPanel(QWidget):
    entry_activated = Signal(object)  # Entry — the user selected it in the list
    remove_requested = Signal(object)  # Entry — take it out of the list
    # Entry, and the row it should land in front of (None — last among its
    # siblings). One signal behind both the drag and Shift+Up/Down.
    reorder_requested = Signal(object, object)
    # Entry, SortKey — put the group this row sits in into that order.
    sort_requested = Signal(object, object)
    copy_requested = Signal(object)  # Entry — put it and its children on the clipboard
    cut_requested = Signal(object)  # Entry — copy, then take it out of the list
    paste_requested = Signal(object)  # Entry | None — paste, targeting this row
    duplicate_requested = Signal(object)  # Entry — a second copy of it in the project
    write_requested = Signal(object)  # Entry
    export_png_requested = Signal(object)  # Entry (FILE/SLICE) — render to one PNG
    export_raw_requested = Signal(object)  # Entry (FILE/SLICE) — decoded bytes out
    export_slices_requested = Signal(object)  # Entry (a FILE) — its slices to a folder
    import_png_requested = Signal(object)  # Entry (FILE/SLICE) — image over its start
    new_slice_requested = Signal(object)  # Entry (a FILE) — open the slice dialog
    new_slice_from_view_requested = Signal(object)  # Entry — slice the viewport
    new_slice_from_selection_requested = Signal(object)  # Entry — slice the tiles
    new_bookmark_requested = Signal(object)  # Entry (a FILE) — bookmark the view
    change_container_requested = Signal(object)  # Entry (FILE/PALETTE) — its container
    container_info_requested = Signal(object)  # Entry (FILE/PALETTE) — what it read
    use_palette_requested = Signal(object)  # Entry (a PALETTE) — apply to the view
    edit_slice_requested = Signal(object)  # Entry (a SLICE) — edit its coordinates
    jump_to_source_requested = Signal(object)  # Entry (a SLICE) — show it in its parent
    jump_to_bookmark_requested = Signal(object)  # Entry (a BOOKMARK) — apply + jump
    bookmark_as_palette_requested = Signal(object)  # Entry (BOOKMARK) — offset palette
    show_in_manager_requested = Signal(object)  # Entry — reveal its file on disk
    rename_committed = Signal(object, str)  # Entry, new name — a finished rename

    def __init__(
        self, registry: Registry | None = None, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        # Only to name a file's container in its label; None simply omits the
        # hint, which keeps the panel constructible on its own.
        self._registry = registry
        self._tree = _EntryTree()
        self._tree.setHeaderHidden(True)
        self._tree.setColumnCount(2)
        header = self._tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(_STATUS_COL, QHeaderView.ResizeMode.Fixed)
        header.resizeSection(_STATUS_COL, _STATUS_W)
        self._tree.setIconSize(QSize(_ICON_W, _ICON_H))  # tighten icon-to-name gap
        self._tree.setRootIsDecorated(True)
        self._tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._show_menu)
        # Selection *is* activation: a single click switches the active view,
        # like every file-switcher sidebar. Programmatic syncs (set_current)
        # block signals so only user selection emits.
        self._tree.currentItemChanged.connect(self._on_current_item_changed)
        # Inline rename (slices only): double-click or the context menu opens
        # the tree's item editor. The editable flag is set just for the edit —
        # a permanently editable item would also open on stray clicks.
        self._tree.itemDoubleClicked.connect(self._on_double_clicked)
        self._tree.itemChanged.connect(self._on_item_changed)
        # Keep the delegate wrapper referenced: a connection made through a
        # temporary PySide wrapper is lost when the wrapper is collected.
        self._delegate = self._tree.itemDelegate()
        self._delegate.closeEditor.connect(self._on_editor_closed)
        self._editing: Entry | None = None
        # Both built lazily and theme-colored; cached against the palette and
        # pixel ratio they were rasterized for (see _drop_stale_icons).
        self._icons: dict[str, QIcon] = {}
        self._icon_key: tuple[int, float] | None = None
        # One section header per content kind, created with that kind's first
        # entry and removed with its last, so a list holding only one kind shows
        # only its own heading. Ordered by SECTIONS below.
        self._sections: dict[ContentKind, QTreeWidgetItem] = {}
        self._items: dict[Entry, QTreeWidgetItem] = {}
        self._current: Entry | None = None  # mirrors the workspace's pointer
        self._has_selection = False  # mirrors the canvas's tile selection

        # Delete removes the highlighted entry - handled by the tree itself (see
        # _EntryTree) rather than a QShortcut, so it wins the key over the
        # canvas's window-wide Clear/Delete instead of overloading with it.
        self._tree.delete_pressed.connect(self._remove_current)
        self._tree.move_pressed.connect(self._move_current)
        self._tree.cut_pressed.connect(lambda: self._for_current(self.cut_requested))
        self._tree.copy_pressed.connect(lambda: self._for_current(self.copy_requested))
        self._tree.duplicate_pressed.connect(
            lambda: self._for_current(self.duplicate_requested)
        )
        # The one row action that means something with nothing highlighted, and
        # with the highlight on a section header: a paste with no target lands
        # where the payload itself says it belongs.
        self._tree.paste_pressed.connect(
            lambda: self.paste_requested.emit(self._current_entry())
        )
        self._tree.reorder_dropped.connect(self.reorder_requested)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._tree)

    def is_key_navigating(self) -> bool:
        """True while a selection change is being driven by the arrow keys — the
        main window checks this to leave focus on the list rather than handing
        it to the view, so browsing with the keyboard isn't cut short."""
        return self._tree.key_navigating

    # -- model mirroring (driven by workspace callbacks) ---------------------
    def add_entry(
        self, entry: Entry, parent: Entry | None = None, before: Entry | None = None
    ) -> None:
        """Add ``entry``; a slice or bookmark nests under ``parent``'s item.

        A top-level entry goes under the section header for what it *holds* —
        Pixels, Tilemaps or Palettes (``docs/design/tilemap-entry.md`` §2) — so
        the tree's nesting keeps meaning "a window into that file's bytes" and
        the sections carry the other question.

        ``before`` is the sibling row this one goes in front of, appended when it
        is None or names a row that isn't there. **The order is the workspace's**,
        not this panel's: rows are the user's to arrange, so the caller reads the
        position out of the list rather than the panel deriving one — which is
        also what makes a project reload and an undone removal put a row back
        exactly where it was. Where offsets still decide anything is one step
        earlier, in what position the *workspace* gives a newly carved slice
        (:meth:`~celpix.project.workspace.Workspace.add_index_for`).
        """
        item = QTreeWidgetItem()
        item.setData(0, Qt.ItemDataRole.UserRole, entry)
        # Draggable, and a drop target only while one of its own children is
        # being dragged (``_EntryTree.startDrag``): a drop lands between rows
        # (see ``_EntryTree._drop_before``), and a row that accepted one at any
        # other time would offer a re-parenting the panel has no meaning for.
        item.setFlags(
            (item.flags() | Qt.ItemFlag.ItemIsDragEnabled)
            & ~Qt.ItemFlag.ItemIsDropEnabled
        )
        parent_item = self._items.get(parent) if parent is not None else None
        if parent_item is None:
            parent_item = self._section_root(entry.content_kind)
        anchor = self._items.get(before) if before is not None else None
        at = (
            parent_item.indexOfChild(anchor)
            if anchor is not None and anchor.parent() is parent_item
            else parent_item.childCount()
        )
        parent_item.insertChild(at, item)
        parent_item.setExpanded(True)
        self._items[entry] = item
        self._refresh_item(entry, item)

    def move_item(self, entry: Entry, before: Entry | None) -> None:
        """Re-place ``entry``'s row in front of ``before``'s — the view side of
        :meth:`~celpix.project.workspace.Workspace.reorder`.

        A file's row carries its nested slices and bookmarks with it, since they
        are its item's children. Expansion and the highlight live in the *view*
        rather than on the item, so a row taken out comes back collapsed and
        unhighlighted — both are put back explicitly. The highlight is restored to
        whatever it was on, which need not be the moved row itself: reordering a
        file while one of its slices is the shown entry takes that row out of the
        tree too, and it must come back current.
        """
        found = self._placed(entry)
        if found is None:
            return
        item, parent_item = found
        index = parent_item.indexOfChild(item)
        anchor = self._items.get(before) if before is not None else None
        if anchor is not None and anchor.parent() is not parent_item:
            return
        target = (
            parent_item.indexOfChild(anchor)
            if anchor is not None
            else parent_item.childCount()
        )
        # Read before the removal, so it has to be corrected for it: everything
        # after the row being lifted out slides one place up.
        if index < target:
            target -= 1
        if target == index:
            return
        was_current = self._tree.currentItem()
        was_expanded = item.isExpanded()
        with signals_blocked(self._tree):  # a take/re-insert must not re-activate
            parent_item.takeChild(index)
            parent_item.insertChild(target, item)
            item.setExpanded(was_expanded)
            if was_current is not None:
                self._tree.setCurrentItem(was_current)

    def _section_root(self, kind: ContentKind) -> QTreeWidgetItem:
        """The section header for ``kind``, created on first use.

        A header, not an entry: it carries no UserRole data and is not
        selectable, so clicking it can never read as an activation. Inserted at
        the position :data:`SECTIONS` gives it rather than appended, so the
        order on screen is fixed regardless of which kind is opened first.
        """
        existing = self._sections.get(kind)
        if existing is not None:
            return existing
        item = QTreeWidgetItem([SECTIONS[kind]])
        item.setFlags(Qt.ItemFlag.ItemIsEnabled)
        font = item.font(0)
        font.setBold(True)
        item.setFont(0, font)
        order = list(SECTIONS)
        # The first section already present that sorts after this one; appended
        # when there is none.
        at = self._tree.topLevelItemCount()
        for later in order[order.index(kind) + 1 :]:
            sibling = self._sections.get(later)
            if sibling is not None:
                at = self._tree.indexOfTopLevelItem(sibling)
                break
        self._tree.insertTopLevelItem(at, item)
        self._sections[kind] = item
        return item

    @staticmethod
    def _has_slices(item: QTreeWidgetItem) -> bool:
        """Whether a file item has at least one slice child — its bookmark
        children don't count, holding no bytes to export."""
        return any(
            item.child(i).data(0, Qt.ItemDataRole.UserRole).kind is EntryKind.SLICE
            for i in range(item.childCount())
        )

    def clear_entries(self) -> None:
        """Drop every row at once — the workspace's ``on_reset``.

        Not the same shape as removing each entry in turn: the section headers
        go with the rows rather than being torn down one by one as each kind
        empties, and an inline rename in flight is abandoned rather than left
        pointing at an entry the swap discarded.
        """
        with signals_blocked(self._tree):  # clearing must not emit an activation
            self._tree.clear()
        self._sections.clear()
        self._items.clear()
        self._current = None
        self._editing = None

    def remove_entry(self, entry: Entry) -> None:
        item = self._items.pop(entry, None)
        if item is None:
            return  # its item already went down with its parent file's
        # A file's item takes its nested slice items with it — drop them from
        # the map now, so the slices' own removal notifications (the workspace
        # removes a file's slices with it) don't touch the deleted items.
        for i in range(item.childCount()):
            self._items.pop(item.child(i).data(0, Qt.ItemDataRole.UserRole), None)
        with signals_blocked(self._tree):  # removal must not emit an activation
            parent = item.parent()
            if parent is not None:
                parent.removeChild(item)
                # A section's last entry takes its header with it, so a list
                # that no longer holds a kind stops advertising it.
                if parent.childCount() == 0:
                    for kind, header in list(self._sections.items()):
                        if header is parent:
                            self._tree.takeTopLevelItem(
                                self._tree.indexOfTopLevelItem(parent)
                            )
                            del self._sections[kind]
            else:
                self._tree.takeTopLevelItem(self._tree.indexOfTopLevelItem(item))

    def next_sibling(self, entry: Entry) -> Entry | None:
        """The row after ``entry``'s among its own siblings — where a reorder has
        to put it back, and so what an undo step captures before moving it."""
        return self._step_sibling(entry, 1)

    def _step_sibling(self, entry: Entry, offset: int) -> Entry | None:
        """The sibling ``offset`` places from ``entry``'s row, or None past
        either end of its group."""
        found = self._placed(entry)
        if found is None:
            return None
        item, parent_item = found
        at = parent_item.indexOfChild(item) + offset
        if not 0 <= at < parent_item.childCount():
            return None
        return parent_item.child(at).data(0, Qt.ItemDataRole.UserRole)

    def sibling_entries(self, entry: Entry) -> list[Entry]:
        """Every row in ``entry``'s group, in the order they are shown — what a
        sort rearranges, and the reason it is asked of the panel.

        A group is what sits under one parent row *on screen*: a file's slices and
        bookmarks, or the files of one section. The workspace's list is flat and
        holds every kind at once, so the group is only knowable here — the same
        reason :meth:`next_sibling` is (see ``MainWindow._reorder_entry``).
        """
        found = self._placed(entry)
        if found is None:
            return []
        _item, parent_item = found
        return [
            parent_item.child(i).data(0, Qt.ItemDataRole.UserRole)
            for i in range(parent_item.childCount())
        ]

    def _placed(self, entry: Entry) -> tuple[QTreeWidgetItem, QTreeWidgetItem] | None:
        """``entry``'s row and the row it is a child of, or None when it has no
        row (never added, or already removed) — the one None-check the three
        order-aware methods below would otherwise each spell out."""
        item = self._items.get(entry)
        parent_item = item.parent() if item is not None else None
        return None if item is None or parent_item is None else (item, parent_item)

    def move_target(self, entry: Entry, delta: int) -> tuple[bool, Entry | None]:
        """Whether ``entry`` can move ``delta`` places among its siblings, and
        the row it would then sit in front of.

        The keyboard's translation into the same "land in front of this" the drop
        handler produces, so both gestures reach one model operation. Moving
        **up** lands in front of the row it passes; moving **down** lands in front
        of the one *after* it, which is None at the end of the group.
        """
        found = self._placed(entry)
        if found is None:
            return False, None
        item, parent_item = found
        at = parent_item.indexOfChild(item) + delta
        if not 0 <= at < parent_item.childCount():
            return False, None
        return True, self._step_sibling(entry, delta + (1 if delta > 0 else 0))

    def set_current(self, entry: Entry | None) -> None:
        previous, self._current = self._current, entry
        with signals_blocked(self._tree):
            self._tree.setCurrentItem(self._items.get(entry) if entry else None)
        # The wash marks what the canvas is showing, so it moves with it: repaint
        # the row losing it and the row taking it, and nothing else.
        for changed in (previous, entry):
            item = self._items.get(changed) if changed is not None else None
            if item is not None:
                self._refresh_item(changed, item)

    def set_has_selection(self, active: bool) -> None:
        """Mirror whether the canvas has a tile selection (gates the
        selection-based context-menu action)."""
        self._has_selection = active

    def set_registry(self, registry: Registry | None) -> None:
        """Point the panel at a rebuilt registry, and re-render what reads it.

        Opening or closing a project **replaces** the window's registry rather
        than adding to one (``_load_project_plugins``), so a panel holding the
        object it was constructed with is holding the one from before the
        project. Everything the rows take from it then falls back: a map whose
        cell format lives in the project's own ``plugins/`` folder is looked up
        in a registry that has never heard of it, and
        :meth:`_tilemap_layout` reports no layout — so a fontmap and a sprite map
        both draw the plain grid icon, and only the shipped formats look right.

        Every row is re-rendered rather than only the tilemaps, because the same
        registry names a file's container in its label and tooltip. Both
        directions matter: opening a project has to pick its formats up, and
        closing one has to let them go again.
        """
        self._registry = registry
        for entry, item in self._items.items():
            self._refresh_item(entry, item)

    def refresh_entry(self, entry: Entry) -> None:
        """Re-render one entry's label — the dirty marker, a backfilled length, a
        notice its load raised.

        The row does **not** move for it. A slice re-pointed to another offset
        stays where the user put it: the order is theirs from the moment the row
        exists (:meth:`add_entry`), and a list that rearranged itself under an
        edit would undo an arrangement nothing asked it to.
        """
        item = self._items.get(entry)
        if item is not None:
            self._refresh_item(entry, item)

    # -- presentation --------------------------------------------------------
    def _container_hint(self, entry: Entry) -> str:
        """`` (TIFF)`` for a file read through a container; ``""`` otherwise.

        Files and palettes (a slice and a bookmark have no container of their
        own), and only when one is actually in use: tagging the great majority of
        rows "(Raw binary file)" would cost every row width to say nothing. So the
        hint's presence is itself the signal that this file is not being read
        literally — which on a palette is exactly the thing worth seeing, since
        the framing decides how many of its colors are real.
        """
        if self._registry is None or entry.kind not in (
            EntryKind.FILE,
            EntryKind.PALETTE,
        ):
            return ""
        label = container_label(self._registry, entry.container_id)
        return f" ({label})" if label else ""

    @staticmethod
    def _files_hint(entry: Entry) -> str:
        """`` (3)`` for a region joined from several files; ``""`` for one file.

        A row is named after its *first* file, which alone would read as the whole
        of what the entry holds — so how many files it really is goes beside the
        name, where a name is being read. The whole count rather than how many
        *extra* there are: "(3)" answers the question the row raises without the
        reader having to add one to it. The paths themselves are long and belong
        in the tooltip. Whole files only: a slice carries its parent's list but is
        one region cut out of the join, so counting the parent's chips on its row
        would answer a question the row isn't asking.
        """
        if entry.kind is not EntryKind.FILE or not entry.extra_paths:
            return ""
        return f" ({len(entry.paths)})"

    def _refresh_item(self, entry: Entry, item: QTreeWidgetItem) -> None:
        # The label is just the name (default slice names already read as
        # "offset (length) compression"), plus how many files join onto it and the
        # container hint on a file; coordinates live in the tooltip so a
        # custom-named slice stays inspectable without cluttering the list.
        unsaved = entry.pixel_dirty or entry.palette_dirty
        name = f"{entry.name}{self._files_hint(entry)}{self._container_hint(entry)}"
        item.setText(0, f"● {name}" if unsaved else name)
        tip = entry.path
        if entry.kind is EntryKind.FILE and entry.extra_paths:
            # Numbered, because the order is what the join uses and is the one
            # thing about a multi-file region a user cannot check anywhere else.
            tip = f"{len(entry.paths)} files joined end to end:\n" + "\n".join(
                f"{n}. {path}" for n, path in enumerate(entry.paths, 1)
            )
        if entry.kind is EntryKind.FILE and self._registry is not None:
            # The full container name, since the list column only had room for a tag.
            full = container_label(self._registry, entry.container_id, short=False)
            if full:
                tip += f"\nContainer {full}"
        marker, what = self._entry_marker(entry)
        # Always set, empty included: a row keeps whatever icon it was last given,
        # so a kind that no longer wears one has to say so rather than leaving the
        # previous icon behind it.
        item.setIcon(0, marker)
        if what:
            tip += f"\n{what}"
        if entry.kind is EntryKind.SLICE:
            tip += f"\nOffset {format_hex(entry.slice_offset)}\nLength " + (
                format_hex(entry.slice_length)
                if entry.slice_length is not None
                else "to be discovered"
            )
        elif entry.kind is EntryKind.BOOKMARK:
            tip += (
                f"\nBookmark at {format_hex(entry.slice_offset)}\nDouble-click to jump"
            )
        elif entry.kind is EntryKind.PALETTE:
            if entry.palette_preset_id is not None:
                tip += f"\nFormat {entry.palette_preset_id.rsplit('.', 1)[-1]}"
            tip += "\nDouble-click to use as the current palette"
        if unsaved:
            # Name which pathway is pending: a palette edit writes to a different
            # file than the entry's own data, so "unsaved changes" alone would
            # misreport a color tweak as a change to the graphic.
            what = (
                "changes"
                if entry.pixel_dirty and not entry.palette_dirty
                else "palette changes"
                if entry.palette_dirty and not entry.pixel_dirty
                else "changes (data + palette)"
            )
            tip += f"\nUnsaved {what}"
        # Two conditions that leave an entry working but not on the bytes the user
        # thinks: a file it references (its own, or its palette) has moved, or a
        # container had to drop, assume or substitute something. Both wash the row
        # amber, and each has its own icon, because the fixes differ — go and find
        # the file, versus read what the container did. Missing wins when both
        # apply: a file that isn't there cannot have been read, so any notice on
        # the entry is from an older load.
        #
        # The whole explanation goes in the tooltip. It is the one place a user
        # already looks to ask "what is wrong with this row", so a notice belongs
        # there rather than somewhere else they have to be told to look.
        notes = entry_notices(entry)
        warnings = [n for n in notes if n.is_warning]
        status: QIcon | None = None
        gone = self._missing_lines(entry)
        if gone:
            tip += gone + "\nFile ▸ Locate missing files"
            status = self._missing_icon()
        else:
            # Every notice, not only the warnings that earn the icon: an info one
            # raises no marker of its own but is still worth reading once here.
            tip += "".join(self._notice_lines(n) for n in notes)
            if warnings:
                status = self._notice_icon()
        # Which row the canvas is showing, kept visible after the *selection*
        # has moved off it - clicking a palette to apply it, or a bookmark to
        # read its offset, leaves the list highlighting something that is not
        # what is on screen. A problem wash outranks it: it is rarer, it is
        # actionable, and the shown entry is still the one the tooltip and title
        # name.
        if status is not None:
            wash = _MISSING_HIGHLIGHT
        elif entry is self._current:
            wash = self._open_entry_wash()
        else:
            wash = QBrush()
        item.setBackground(0, wash)
        item.setBackground(_STATUS_COL, wash)
        item.setIcon(_STATUS_COL, status if status is not None else QIcon())
        item.setToolTip(0, tip)
        # The icon is the thing a user points at to ask what is wrong with this
        # one, so it has to answer rather than showing nothing.
        item.setToolTip(_STATUS_COL, tip)

    def _open_entry_wash(self) -> QBrush:
        """The tint behind the entry the canvas is showing.

        The theme's own Highlight, at an alpha low enough to read as a tint
        rather than a second selection - the real selection sits on top of it
        whenever the two are the same row, and must stay the stronger of the
        two. Taken from the palette rather than fixed like the amber warning,
        since this one is a *neutral* marker and should follow the theme's accent
        (:meth:`changeEvent` repaints every row when that changes).
        """
        color = QColor(
            self.palette().color(
                QPalette.ColorGroup.Active, QPalette.ColorRole.Highlight
            )
        )
        color.setAlpha(_OPEN_ENTRY_ALPHA)
        return QBrush(color)

    @staticmethod
    def _missing_lines(entry: Entry) -> str:
        """The tooltip lines for whichever of the entry's files is gone; ``""``
        when both are there.

        Which one is named matters more than that something is: a graphic whose
        *palette* file moved still opens and still draws (on the default
        palette), so a row flagged with the same wording as one whose own bytes
        are gone sends the user looking for a file that never went anywhere. The
        palette line carries its path too — unlike the data file, it is nowhere
        else in the tooltip.
        """
        lines = []
        if data_missing(entry):
            # A slice/bookmark has no file of its own: what's missing is the
            # parent it reads through, which is what the user has to go and find.
            lines.append(
                {
                    EntryKind.PALETTE: "Palette file is missing",
                    EntryKind.SLICE: "Parent file is missing",
                    EntryKind.BOOKMARK: "Parent file is missing",
                }.get(entry.kind, "File is missing")
            )
        if palette_missing(entry):
            lines.append(f"Palette file is missing:\n  {entry_palette_path(entry)}")
        return "".join(f"\n{line}" for line in lines)

    @staticmethod
    def _notice_lines(notice: Notice) -> str:
        """One notice as tooltip lines: the summary flush, its detail indented.

        The indent is what keeps a list of several readable — without it the
        detail of one runs straight into the summary of the next. Both are
        already hard-wrapped by whoever wrote them, per the tooltip rule (Qt
        never wraps a plain-text tooltip), and two extra columns keep them inside
        it.
        """
        lines = f"\n{notice.summary}"
        if notice.detail:
            lines += "".join(f"\n  {line}" for line in notice.detail.split("\n"))
        return lines

    def _ribbon_icon(self) -> QIcon:
        """The bookmark marker: a flag icon in the theme's accent color."""
        return self._icon("bookmark.png", role=QPalette.ColorRole.Highlight)

    def _entry_marker(self, entry: Entry) -> tuple[QIcon, str]:
        """The icon a row wears, and what the tooltip calls what it holds.

        Keyed on the entry's **content kind** first and its bounding second,
        because that is the order the icons answer in. A **map wears the icon of
        its layout whatever it is a window onto** — a whole file as much as a
        slice of a ROM — since which of the three layouts it holds settles what
        the entry can even do: a sprite map is placed by coordinate rather than
        laid into a grid, and a fontmap's cells read as words. The section header
        says *tilemap*; only the row can say *which*. Gating this on
        ``EntryKind.SLICE`` left every map opened as its own file — which is most
        of them — with no icon at all.

        The maps share one motif and differ in how its cells sit: an even
        lattice, the same cells loose at free offsets and sizes, and the lattice's
        columns fused into lines of text. Three slight variants read as a family;
        the tooltip carries the name, since an icon that subtle is a reminder
        rather than an introduction.

        The two exceptions come first and last. A **bookmark** keeps the ribbon
        even on a map: it marks a position rather than content, and the ribbon is
        what tells it from the slices it sits among. A **pixel slice** is its own
        little graphic, and the framed-picture icon is the universal symbol for
        that — while a pixel *file* wears none, its name and its section being the
        whole of what there is to say.

        The variant is the **format's** declaration, exactly as the window's
        ``_tilemap_is_sprite`` / ``_tilemap_is_fontmap`` read it: a row has to draw
        before its entry is loaded, and a map with nothing bound yet is still an
        object, or still a string.
        """
        if entry.kind is EntryKind.BOOKMARK:
            return self._ribbon_icon(), ""
        if entry.content_kind is ContentKind.TILEMAP:
            filename, what = {
                "sprite": ("spritemap.png", "Sprite map"),
                "text": ("fontmap.png", "Fontmap"),
            }.get(self._tilemap_layout(entry), ("tilemap.png", "Tilemap"))
            return self._icon(filename, role=QPalette.ColorRole.Text), what
        if entry.kind is EntryKind.SLICE:
            return self._icon("slice.png", role=QPalette.ColorRole.Text), ""
        return QIcon(), ""

    def _tilemap_layout(self, entry: Entry) -> str:
        """What ``entry``'s cell format calls its layout — ``""`` for a plain grid.

        Empty for a format this build hasn't got, and for the panel built with no
        registry at all: an unrecognised map is still a map, and the grid icon is
        the honest thing to draw for one.
        """
        if self._registry is None or not entry.tilemap_preset_id:
            return ""
        try:
            preset = self._registry.preset(entry.tilemap_preset_id)
        except KeyError:
            return ""
        return str(preset.params.get("layout") or "")

    def _missing_icon(self) -> QIcon:
        """A question mark: this entry's file is unaccounted for, and the fix is
        to go and find it (File ▸ Locate missing files). Deliberately not a cross
        — at this size, next to rows the user can close, a cross reads as a close
        button rather than a state."""
        return self._icon("missing.png", tint=_WARNING_INK)

    def _notice_icon(self) -> QIcon:
        """An exclamation mark: the entry opened, but a stage had to drop, assume
        or substitute something on the way in, and the row's own tooltip spells
        out what."""
        return self._icon("notice.png", tint=_WARNING_INK)

    def _icon(self, filename: str, *, role=None, tint: QColor | None = None) -> QIcon:  # noqa: ANN001
        """A baked icon, kept until what it was rasterized from moves.

        Keyed on the file, since each one is baked in exactly one color — either
        a theme ``role`` (which follows the palette) or a fixed ``tint``.
        """
        self._drop_stale_icons()
        icon = self._icons.get(filename)
        if icon is None:
            icon = (
                self._role_icon(filename, role)
                if role is not None
                else self._tinted_icon(filename, tint)
            )
            self._icons[filename] = icon
        return icon

    def _drop_stale_icons(self) -> None:
        """Discard the cached icons when what they were rasterized *from* has
        moved: the palette (a theme switch) or the screen's device pixel ratio
        (the window dragged to a differently scaled monitor). Both are baked
        into the finished pixmap, so a cache kept across either change would
        show yesterday's color at the wrong resolution."""
        key = icon_cache_key(self)
        if key != self._icon_key:
            self._icon_key = key
            self._icons.clear()

    def changeEvent(self, event) -> None:  # Qt override
        # A theme switch repaints the rows, but the icons are pixmaps baked in
        # the old colors — re-render every label so they are rebuilt against the
        # new palette rather than persisting until the panel is.
        super().changeEvent(event)
        if event.type() is QEvent.Type.PaletteChange:
            self._drop_stale_icons()
            for entry, item in self._items.items():
                self._refresh_item(entry, item)

    def _role_icon(self, filename: str, role: QPalette.ColorRole) -> QIcon:
        """A bundled icon in a **theme** color — what the kind markers use.

        The color comes from the **Active** group explicitly: an entry's marker
        shouldn't wear the dimmed inactive variant (on Windows the inactive
        Highlight is a flat gray) merely because the window happened to be
        unfocused when the icon was first built and cached.
        """
        return self._tinted_icon(
            filename, self.palette().color(QPalette.ColorGroup.Active, role)
        )

    def _tinted_icon(self, filename: str, color: QColor) -> QIcon:
        """A bundled ``icons/<filename>`` recolored to ``color``.

        The art ships as white silhouettes, pre-cropped to their opaque bounds (no
        baked-in margin to widen the gap to the entry name). We recolor to the
        given color — which is a palette role for the kind markers, keeping them
        theme-aware in light and dark, and the fixed warning amber for the status
        icons, whose whole job is to read as a warning in either theme — then
        fit the art, centred, into the icon box.

        Rasterized at the screen's **device** resolution, not the logical 13x16:
        a QIcon built from a single 1x pixmap has nothing better to offer a
        scaled display, so Qt would stretch that bitmap — and these icons are
        thin enough that the smear reads as a washed-out gray rather than the
        tint. The pixmap carries its ratio, so the icon still measures 13x16 in
        layout units.

        Two pixmaps, not one: a **selected** row is painted in the highlight
        color, and any ink chosen to read against the ordinary row background
        turns muddy on top of it — the warning amber worst of all. Qt asks a
        QIcon for its ``Selected`` variant when it draws the decoration of a
        selected item, so the second pixmap is the same icon in the highlighted
        text color. The shape still says *which* condition it is; only the ink
        follows the row.
        """
        source = QImage.fromData(resources.read_bytes("icons", filename))
        image = source.convertToFormat(QImage.Format.Format_ARGB32)
        icon = QIcon(self._icon_pixmap(image, color))
        icon.addPixmap(
            self._icon_pixmap(
                image,
                self.palette().color(
                    QPalette.ColorGroup.Active, QPalette.ColorRole.HighlightedText
                ),
            ),
            QIcon.Mode.Selected,
        )
        return icon

    def _icon_pixmap(self, image: QImage, color: QColor) -> QPixmap:
        """``image`` tinted ``color`` and centred in the icon box, at device scale."""
        return tinted_icon(
            image, color, QSize(_ICON_W, _ICON_H), self.devicePixelRatioF()
        )

    # -- interaction ---------------------------------------------------------
    def _on_current_item_changed(self, item: QTreeWidgetItem | None, _prev) -> None:
        # The Palettes header carries no entry — keyboard navigation can still
        # land on it, and that must not read as an activation.
        entry = item.data(0, Qt.ItemDataRole.UserRole) if item is not None else None
        if entry is not None:
            self.entry_activated.emit(entry)

    def _current_entry(self) -> Entry | None:
        """The highlighted row's entry — None for nothing, a section header, or
        a rename in flight (where every row key belongs to the editor)."""
        item = self._tree.currentItem()
        if item is None or self._editing is not None:
            return None
        return item.data(0, Qt.ItemDataRole.UserRole)

    def _for_current(self, signal) -> None:  # noqa: ANN001 — a Signal to emit
        """Fire a one-entry row signal for the highlighted row, if there is one."""
        entry = self._current_entry()
        if entry is not None:
            signal.emit(entry)

    def _remove_current(self) -> None:
        """The Delete shortcut: request removal of the highlighted entry."""
        self._for_current(self.remove_requested)

    def _move_current(self, delta: int) -> None:
        """The Shift+Up/Down shortcut: reorder the highlighted row.

        Every kind, unlike the file-only move this replaced: the order of the
        whole list is the user's now, so a slice moves among its parent's
        children and a palette among the palettes, each within the group its row
        sits in.
        """
        entry = self._current_entry()
        if entry is None:
            return
        can_move, before = self.move_target(entry, delta)
        if can_move:
            self.reorder_requested.emit(entry, before)

    def _on_double_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        # A bookmark's or palette's double-click is its primary action — jump
        # to it / apply it (rename stays on the context menu); a file's or
        # slice's double-click opens the renamer, single-click having already
        # done the only other thing a row can do.
        entry: Entry | None = item.data(0, Qt.ItemDataRole.UserRole)
        if entry is None:  # the Palettes header
            return
        if entry.kind is EntryKind.BOOKMARK:
            self.jump_to_bookmark_requested.emit(entry)
        elif entry.kind is EntryKind.PALETTE:
            self.use_palette_requested.emit(entry)
        else:
            self._begin_rename(entry)

    # -- rename --------------------------------------------------------------
    def _begin_rename(self, entry: Entry) -> None:
        """Open the inline editor on ``entry``'s item.

        Every kind of entry: a row opens under the basename of the file it points
        at, and that name rarely says what the user is editing. A ROM is not named
        after the sprite sheet inside it, a region joined from several chips is
        named after only the first of them, and a project holding dozens of
        palette files sorts them out by which scene they colour rather than by
        which numbered ``.pal`` they are. So the row is free text, and the path it
        was named from stays in the tooltip.
        """
        item = self._items.get(entry)
        if item is None:
            return
        self._editing = entry
        with signals_blocked(self._tree):  # marker strip must not read as an edit
            item.setText(0, entry.name)  # edit the bare name, not the ● marker
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
        self._tree.editItem(item, 0)

    def _on_item_changed(self, item: QTreeWidgetItem, _column: int) -> None:
        # Only a commit of the active inline edit counts; every other setText
        # (label refreshes) either arrives with signals blocked or lands here
        # with no edit in progress and falls through.
        entry = self._editing
        if entry is None or self._items.get(entry) is not item:
            return
        self._editing = None
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        name = item.text(0).strip()
        if name and name != entry.name:
            self.rename_committed.emit(entry, name)
        else:
            self._refresh_item(entry, item)  # empty or unchanged: revert

    def _on_editor_closed(self, _editor, _hint) -> None:
        # A cancelled edit (Escape / focus loss without commit) never fires
        # itemChanged — restore the display label and editability here.
        entry, self._editing = self._editing, None
        item = self._items.get(entry) if entry is not None else None
        if item is not None:
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self._refresh_item(entry, item)

    @staticmethod
    def _entry_action(
        menu: QMenu,
        text: str,
        handler,  # noqa: ANN001 — Callable, usually a signal's ``emit``
        *args: object,
        enabled: bool = True,
        shortcut=None,  # noqa: ANN001 — QKeySequence | StandardKey
    ) -> None:
        """One row of an entry's context menu: label, what it does, whether it is
        live.

        Every row in :meth:`_show_menu` is the same three statements around a
        signal carrying the right-clicked entry, so a call site says only which
        signal and with what — ``handler`` being that signal's ``emit`` (or, for
        the rows this panel answers itself, the method). The arguments are passed
        here rather than closed over at the call site because a lambda built in a
        loop-free run of twenty rows is exactly where a captured name goes stale.

        A ``shortcut`` here is always **display-only**: a closed menu's action
        never fires, so it labels the key in the shortcut column while the working
        binding lives elsewhere (the tree's own key handling, or the File menu's
        row for the current entry).
        """
        action = menu.addAction(text)
        action.triggered.connect(lambda: handler(*args))
        if shortcut is not None:
            action.setShortcut(shortcut)
        action.setEnabled(enabled)

    def _add_container_info_action(self, menu: QMenu, entry: Entry) -> None:
        """What the container read, under the action that chooses which one.

        On both kinds that have a container of their own, from one builder so the
        two cannot drift apart. Needs no document and no successful load — a file
        that came out looking wrong is exactly when the report is worth reading,
        which is why it is always offered.
        """
        # "C" rather than the File menu's "I", which "Import from PNG…" holds
        # here — a mnemonic only has to be unique within the menu it appears in.
        self._entry_action(
            menu, "&Container Info…", self.container_info_requested.emit, entry
        )

    def _add_write_action(self, menu: QMenu, entry: Entry) -> None:
        """Write, sitting under the entry's own settings (a file's container, a
        slice's definition) rather than down by the import/export group: it is
        what commits the edits those dialogs and the canvas make. One builder so
        the two kinds that write bytes cannot disagree about when it is live.
        """
        # Writing needs a loaded, write-capable document; a never-activated or
        # view-only entry has nothing to write.
        self._entry_action(
            menu,
            "&Write",
            self.write_requested.emit,
            entry,
            enabled=entry.doc is not None and entry.doc.pixel_config.write_enabled,
        )

    def _add_order_actions(self, menu: QMenu, entry: Entry) -> None:
        """Move Up / Move Down and the Sort submenu, on every kind of row.

        Every row's place in the list is the user's, so every row can be moved —
        within its own group, which is what all of these are gated on. The keys go
        in the label after a tab, which Qt renders in the shortcut column, rather
        than being registered as the actions' shortcuts: Shift+Up/Down resize the
        view window window-wide, and a real binding here would fire from anywhere
        in the app. The working one is the tree's own key handling, which the
        navigation filter already defers to while the list has focus.

        Sorting rearranges the **group** the clicked row is in rather than the
        whole list — the files of one section, or one file's children — since that
        is the span the user is looking at as an ordered thing. Nothing latches:
        it is one rearrangement of rows whose order stays theirs afterwards, so a
        row moved by hand next stays moved, and a re-pointed slice stays put.

        The orders live in a **submenu**: they are one question asked three ways,
        and three more siblings of Move Up would be most of the menu. It also
        gives them a mnemonic space of their own, where out here the obvious
        letters are taken — N by Rename in every branch, S by New Slice where a
        file's menu offers one — so inside it each order is its own first letter.
        The submenu itself takes b, which is why a file's New Bookmark spells
        itself with a k.

        **By offset only on the child kinds**, where the offset is what the row
        is: a file and a palette are the whole of their bytes, so a group of them
        would sort on a column of zeros. Left out rather than shown dead, because
        it is not a thing those rows could do under other circumstances — the
        distinction the rest of this menu draws between an absent row and a
        disabled one.
        """
        menu.addSeparator()
        for label, delta in (("M&ove Up\tShift+Up", -1), ("Move &Down\tShift+Down", 1)):
            can_move, before = self.move_target(entry, delta)
            self._entry_action(
                menu,
                label,
                self.reorder_requested.emit,
                entry,
                before,
                enabled=can_move,
            )
        # A group of one has no order to put right — a dead submenu, not a missing
        # one: the same rows with a second sibling would sort.
        sort = menu.addMenu("Sort &by")
        sort.setEnabled(len(self.sibling_entries(entry)) > 1)
        self._entry_action(sort, "&Name", self.sort_requested.emit, entry, SortKey.NAME)
        self._entry_action(sort, "&Type", self.sort_requested.emit, entry, SortKey.TYPE)
        if entry.kind in (EntryKind.SLICE, EntryKind.BOOKMARK):
            self._entry_action(
                sort, "&Offset", self.sort_requested.emit, entry, SortKey.OFFSET
            )

    def _add_clipboard_actions(self, menu: QMenu, entry: Entry) -> None:
        """Cut / Copy / Paste / Duplicate, on every kind of row.

        What travels is the **entry** — a reference plus the settings it is read
        through — and never the file behind it: cutting a row takes it out of the
        project, and duplicating one gives the project a second way in to bytes
        that were only ever written once.

        **Duplicate is for the kinds that can appear twice**, which is a slice and
        a bookmark. A file and a palette are identified by their path
        (:meth:`~celpix.project.workspace.Workspace.find_file`), and a second row
        over one file would be a second document over one buffer — two sets of
        unsaved edits with one file underneath them. Cut and Copy stay live on all
        four, since pasting *elsewhere* is exactly what that identity permits.

        The shortcuts are display-only here, like Remove's: a closed menu's action
        never fires, so these label the keys while the working bindings are the
        tree's own (:class:`_EntryTree`).
        """
        menu.addSeparator()
        self._entry_action(
            menu,
            "Cu&t",
            self.cut_requested.emit,
            entry,
            shortcut=QKeySequence.StandardKey.Cut,
        )
        self._entry_action(
            menu,
            "Cop&y",
            self.copy_requested.emit,
            entry,
            shortcut=QKeySequence.StandardKey.Copy,
        )
        self._entry_action(
            menu,
            "&Paste",
            self.paste_requested.emit,
            entry,
            enabled=clipboard.has_entries(),
            shortcut=QKeySequence.StandardKey.Paste,
        )
        self._entry_action(
            menu,
            "Dup&licate",
            self.duplicate_requested.emit,
            entry,
            enabled=entry.kind in (EntryKind.SLICE, EntryKind.BOOKMARK),
            shortcut=DUPLICATE_KEY,
        )

    def _show_menu(self, pos) -> None:
        item = self._tree.itemAt(pos)
        if item is None:
            return
        entry: Entry | None = item.data(0, Qt.ItemDataRole.UserRole)
        if entry is None:  # the Palettes header has no actions
            return
        # "&" marks the keyboard mnemonic - the letter that picks the entry once
        # the menu is open. It matches the action's shortcut letter where one is
        # free (Write/Ctrl+W, Edit File Container/Ctrl+E). Each entry kind builds
        # its own menu below, so the letters only need to be unique per branch.
        menu = QMenu(self)
        if entry.kind is EntryKind.FILE:
            # Only files spawn slices and bookmarks (neither nests), so the
            # menu shows these on files alone. All but the plain dialog
            # additionally need the file on screen — the viewport, selection
            # and settings snapshot live only there.
            sliceable = entry is self._current and entry.doc is not None
            self._entry_action(
                menu, "New &Slice…", self.new_slice_requested.emit, entry
            )
            # ...and a view to read: an entry shown entire has no window for
            # this to cover, which is the entry's own answer to give
            # (the File menu's row is gated on the same capability).
            self._entry_action(
                menu,
                "New Slice from &View",
                self.new_slice_from_view_requested.emit,
                entry,
                enabled=sliceable and entry.can(Capability.NAVIGATION),
            )
            self._entry_action(
                menu,
                "New Slice &from Selection",
                self.new_slice_from_selection_requested.emit,
                entry,
                enabled=sliceable and self._has_selection,
            )
            # "k" rather than the File menu's "B", which Sort by holds here: the
            # letters only have to be unique within one menu, and every other one
            # in "Sort by" is spoken for on a file's row (S by New Slice, o by
            # Move Up, r by Remove, t by Cut, y by Copy).
            self._entry_action(
                menu,
                "New Boo&kmark",
                self.new_bookmark_requested.emit,
                entry,
                enabled=sliceable,
            )
            menu.addSeparator()
            self._entry_action(menu, "Re&name…", self._begin_rename, entry)
            # Always offered, and needs no document: correcting the container is
            # exactly what a file that failed to make sense needs. Its shortcut is
            # display-only, like Remove's below: the working binding is the File
            # menu's action, which acts on the *current* entry rather than the
            # right-clicked one.
            self._entry_action(
                menu,
                "&Edit File Container…",
                self.change_container_requested.emit,
                entry,
                shortcut=QKeySequence("Ctrl+E"),
            )
            self._add_container_info_action(menu, entry)
            self._add_write_action(menu, entry)
            menu.addSeparator()
        elif entry.kind is EntryKind.SLICE:
            # A slice's primary navigation action: reopen its region in the
            # parent file, decoded the slice's way, at the slice's offset.
            self._entry_action(
                menu, "&Jump to Source", self.jump_to_source_requested.emit, entry
            )
            menu.addSeparator()
            self._entry_action(menu, "Re&name…", lambda: self._begin_rename(entry))
            self._entry_action(menu, "&Edit…", self.edit_slice_requested.emit, entry)
            self._add_write_action(menu, entry)
            menu.addSeparator()
        elif entry.kind is EntryKind.PALETTE:
            # The double-click action, discoverable.
            self._entry_action(
                menu, "&Use as Current Palette", self.use_palette_requested.emit, entry
            )
            menu.addSeparator()
            self._entry_action(menu, "Re&name…", lambda: self._begin_rename(entry))
            # Same override a file gets, over the containers that frame a palette:
            # a palette whose colors stop before its bytes do needs one, and
            # detection can be as wrong here as anywhere.
            self._entry_action(
                menu,
                "&Edit File Container…",
                self.change_container_requested.emit,
                entry,
            )
            self._add_container_info_action(menu, entry)
            # A file palette owns its colors and is edited in place, so it Writes
            # back to its own .pal from here — offered only with unsaved edits (the
            # graphic that renders it is never dirtied by a color change).
            self._entry_action(
                menu,
                "&Write",
                self.write_requested.emit,
                entry,
                enabled=entry.doc is not None and entry.palette_dirty,
            )
            menu.addSeparator()
        else:
            # The double-click action, discoverable; a bookmark holds no bytes
            # of its own, so there is no Write here.
            self._entry_action(
                menu, "&Jump to Bookmark", self.jump_to_bookmark_requested.emit, entry
            )
            # Reuse the bookmarked offset as a palette offset — the graphics
            # position often marks where the palette sits.
            self._entry_action(
                menu, "&Use as Palette", self.bookmark_as_palette_requested.emit, entry
            )
            menu.addSeparator()
            self._entry_action(menu, "Re&name…", lambda: self._begin_rename(entry))
            menu.addSeparator()
        self._add_order_actions(menu, entry)
        self._add_clipboard_actions(menu, entry)
        if entry.kind in (EntryKind.FILE, EntryKind.SLICE):
            # Import is the mirror of Export ▸ As PNG…, and lands the image at
            # the start of the entry. Unlike export it needs the entry on screen
            # (it is fitted to the view's palette and arrangement), so the window
            # activates it first.
            self._entry_action(
                menu, "&Import from PNG…", self.import_png_requested.emit, entry
            )
            # Export targets the entry the menu was opened on, not the current
            # view, so an entry can leave as an image without being activated
            # first — the window loads it on demand. Always offered: whether the
            # bytes decode is only knowable by trying.
            export = menu.addMenu("E&xport")
            self._entry_action(
                export, "As &PNG…", self.export_png_requested.emit, entry
            )
            self._entry_action(export, "&Raw…", self.export_raw_requested.emit, entry)
            if entry.kind is EntryKind.FILE and self._has_slices(item):
                export.addSeparator()
                self._entry_action(
                    export, "&Slices as PNGs…", self.export_slices_requested.emit, entry
                )
            menu.addSeparator()
        if entry.kind in (EntryKind.FILE, EntryKind.PALETTE):
            # The two kinds that *are* a file on disk - a slice or bookmark only
            # borrows its parent's, so revealing one would point at a file the
            # row isn't. Named for the job rather than for any one desktop's
            # file manager, since the same item opens Explorer, Finder or
            # whatever the session runs.
            self._entry_action(
                menu,
                "Show in File &Manager",
                self.show_in_manager_requested.emit,
                entry,
            )
            menu.addSeparator()
        # The Delete hint is display-only: the working binding is the
        # tree-focused QShortcut.
        self._entry_action(
            menu,
            "&Remove",
            self.remove_requested.emit,
            entry,
            shortcut=QKeySequence.StandardKey.Delete,
        )
        menu.exec(self._tree.viewport().mapToGlobal(pos))
