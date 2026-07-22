"""The open-files dock panel: every open file, with its slices nested under it.

A thin Qt view over the workspace model — the main window forwards workspace
callbacks into the ``add_entry``/``remove_entry``/``set_current``/``refresh_entry``
slots and listens to the signals; the panel itself never mutates the workspace.
Built on QTreeWidget rather than a hand-painted widget: unlike the palette
swatches or the canvas, a document list has no custom pixel presentation — it
wants exactly the selection, nesting, keyboard and context-menu behaviour the
framework already provides.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import QMenu, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget

from celpix.core.address import format_hex
from celpix.project.workspace import Entry, EntryKind


class FileListPanel(QWidget):
    entry_activated = Signal(object)  # Entry — the user selected it in the list
    remove_requested = Signal(object)  # Entry — take it out of the list
    write_requested = Signal(object)  # Entry
    new_slice_requested = Signal(object)  # Entry (a FILE) — open the slice dialog
    new_slice_from_view_requested = Signal(object)  # Entry — slice the viewport
    new_slice_from_selection_requested = Signal(object)  # Entry — slice the tiles
    edit_slice_requested = Signal(object)  # Entry (a SLICE) — edit its coordinates
    rename_committed = Signal(object, str)  # Entry, new name — a finished rename

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._tree = QTreeWidget()
        self._tree.setHeaderHidden(True)
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
        self._items: dict[Entry, QTreeWidgetItem] = {}
        self._current: Entry | None = None  # mirrors the workspace's pointer
        self._has_selection = False  # mirrors the canvas's tile selection

        # Delete removes the highlighted entry — active only while the tree
        # itself has focus, so the key can't fire from the canvas or a field.
        self._remove_shortcut = QShortcut(QKeySequence.StandardKey.Delete, self._tree)
        self._remove_shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
        self._remove_shortcut.activated.connect(self._remove_current)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._tree)

    # -- model mirroring (driven by workspace callbacks) ---------------------
    def add_entry(self, entry: Entry, parent: Entry | None = None) -> None:
        """Append ``entry``; a slice nests under ``parent``'s item when given."""
        item = QTreeWidgetItem()
        item.setData(0, Qt.ItemDataRole.UserRole, entry)
        parent_item = self._items.get(parent) if parent is not None else None
        if parent_item is not None:
            parent_item.addChild(item)
            parent_item.setExpanded(True)
        else:
            self._tree.addTopLevelItem(item)
        self._items[entry] = item
        self._refresh_item(entry, item)

    def remove_entry(self, entry: Entry) -> None:
        item = self._items.pop(entry, None)
        if item is None:
            return
        self._tree.blockSignals(True)  # removal must not emit a stray activation
        parent = item.parent()
        if parent is not None:
            parent.removeChild(item)
        else:
            self._tree.takeTopLevelItem(self._tree.indexOfTopLevelItem(item))
        self._tree.blockSignals(False)

    def set_current(self, entry: Entry | None) -> None:
        self._current = entry
        self._tree.blockSignals(True)
        self._tree.setCurrentItem(self._items.get(entry) if entry else None)
        self._tree.blockSignals(False)

    def set_has_selection(self, active: bool) -> None:
        """Mirror whether the canvas has a tile selection (gates the
        selection-based context-menu action)."""
        self._has_selection = active

    def refresh_entry(self, entry: Entry) -> None:
        """Re-render one entry's label (dirty marker, backfilled length, …)."""
        item = self._items.get(entry)
        if item is not None:
            self._refresh_item(entry, item)

    # -- presentation --------------------------------------------------------
    def _refresh_item(self, entry: Entry, item: QTreeWidgetItem) -> None:
        # The label is just the name (default slice names already read as
        # "offset (length) compression"); coordinates live in the tooltip so a
        # custom-named slice stays inspectable without cluttering the list.
        item.setText(0, f"● {entry.name}" if entry.dirty else entry.name)
        tip = entry.path
        if entry.kind is EntryKind.SLICE:
            tip += f"\nOffset {format_hex(entry.slice_offset)}\nLength " + (
                format_hex(entry.slice_length)
                if entry.slice_length is not None
                else "to be discovered"
            )
        if entry.dirty:
            tip += "\nUnsaved changes"
        item.setToolTip(0, tip)

    # -- interaction ---------------------------------------------------------
    def _on_current_item_changed(self, item: QTreeWidgetItem | None, _prev) -> None:
        if item is not None:
            self.entry_activated.emit(item.data(0, Qt.ItemDataRole.UserRole))

    def _remove_current(self) -> None:
        """The Delete shortcut: request removal of the highlighted entry."""
        item = self._tree.currentItem()
        if item is not None and self._editing is None:  # not mid-rename
            self.remove_requested.emit(item.data(0, Qt.ItemDataRole.UserRole))

    # -- rename --------------------------------------------------------------
    def _on_double_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        self._begin_rename(item.data(0, Qt.ItemDataRole.UserRole))

    def _begin_rename(self, entry: Entry) -> None:
        """Open the inline editor on ``entry``'s item (slices only — a file's
        name is its on-disk basename, not free text)."""
        item = self._items.get(entry)
        if item is None or entry.kind is not EntryKind.SLICE:
            return
        self._editing = entry
        self._tree.blockSignals(True)  # marker strip must not read as an edit
        item.setText(0, entry.name)  # edit the bare name, not the ● marker
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
        self._tree.blockSignals(False)
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

    def _show_menu(self, pos) -> None:
        item = self._tree.itemAt(pos)
        if item is None:
            return
        entry: Entry = item.data(0, Qt.ItemDataRole.UserRole)
        menu = QMenu(self)
        if entry.kind is EntryKind.FILE:
            # The viewport/selection only exist for the entry on screen, and
            # slicing them only works while its byte stream maps to file
            # offsets (the same gate the File-menu actions live behind).
            sliceable = (
                entry is self._current
                and entry.doc is not None
                and entry.doc.pixel_config.decompress_id == "decompress.none"
            )
            new_slice = menu.addAction("New Slice…")
            new_slice.triggered.connect(lambda: self.new_slice_requested.emit(entry))
            here = menu.addAction("New Slice from View")
            here.triggered.connect(
                lambda: self.new_slice_from_view_requested.emit(entry)
            )
            here.setEnabled(sliceable)
            from_sel = menu.addAction("New Slice from Selection")
            from_sel.triggered.connect(
                lambda: self.new_slice_from_selection_requested.emit(entry)
            )
            from_sel.setEnabled(sliceable and self._has_selection)
            menu.addSeparator()
        else:
            rename = menu.addAction("Rename…")
            rename.triggered.connect(lambda: self._begin_rename(entry))
            edit = menu.addAction("Edit…")
            edit.triggered.connect(lambda: self.edit_slice_requested.emit(entry))
            menu.addSeparator()
        write = menu.addAction("Write")
        write.triggered.connect(lambda: self.write_requested.emit(entry))
        # Writing needs a loaded, write-capable document; a never-activated or
        # view-only entry has nothing to write.
        write.setEnabled(entry.doc is not None and entry.doc.pixel_config.write_enabled)
        menu.addSeparator()
        remove = menu.addAction("Remove")
        # Display-only shortcut hint: the working binding is the tree-focused
        # QShortcut; a menu action's own shortcut is inert while the menu is
        # closed, so this just labels the key in the shortcut column.
        remove.setShortcut(QKeySequence.StandardKey.Delete)
        remove.triggered.connect(lambda: self.remove_requested.emit(entry))
        menu.exec(self._tree.viewport().mapToGlobal(pos))
