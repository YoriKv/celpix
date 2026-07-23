"""The open-files dock panel: every open file, with its slices and bookmarks
nested under it.

A thin Qt view over the workspace model — the main window forwards workspace
callbacks into the ``add_entry``/``remove_entry``/``set_current``/``refresh_entry``
slots and listens to the signals; the panel itself never mutates the workspace.
Built on QTreeWidget rather than a hand-painted widget: unlike the palette
swatches or the canvas, a document list has no custom pixel presentation — it
wants exactly the selection, nesting, keyboard and context-menu behaviour the
framework already provides.
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QIcon,
    QImage,
    QKeySequence,
    QPainter,
    QPalette,
    QPixmap,
    QShortcut,
)
from PySide6.QtWidgets import QMenu, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget

from celpix import resources
from celpix.core.address import format_hex
from celpix.project.workspace import Entry, EntryKind, entry_reference_missing

# Translucent amber behind an entry whose referenced file (or palette) is
# missing: reads as a warning over either light or dark row backgrounds without
# fighting the selection highlight.
_MISSING_HIGHLIGHT = QBrush(QColor(255, 193, 7, 70))

# The slice/bookmark icon box. Narrower than the default 16px decoration so a
# centred glyph sits close to the entry name rather than across a wide gap; the
# glyphs are painted at exactly this size so nothing is scaled.
_ICON_W = 13
_ICON_H = 16


class _EntryTree(QTreeWidget):
    """A tree that records when a selection change is driven by the keyboard.

    Selecting a row loads it into the view, which normally hands focus to the
    canvas so arrow keys drive the pixels. But while the user is *browsing* the
    list with the arrow keys, stealing focus mid-scroll would break the very
    keys they are navigating with. The flag is true only for the duration of a
    key-driven selection change (it wraps the base handler that emits
    ``currentItemChanged``), so the panel can tell an arrow-key move apart from a
    click and keep focus on the list for the former.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.key_navigating = False

    def keyPressEvent(self, event) -> None:
        self.key_navigating = True
        try:
            super().keyPressEvent(event)  # emits currentItemChanged synchronously
        finally:
            self.key_navigating = False


class FileListPanel(QWidget):
    entry_activated = Signal(object)  # Entry — the user selected it in the list
    remove_requested = Signal(object)  # Entry — take it out of the list
    write_requested = Signal(object)  # Entry
    new_slice_requested = Signal(object)  # Entry (a FILE) — open the slice dialog
    new_slice_from_view_requested = Signal(object)  # Entry — slice the viewport
    new_slice_from_selection_requested = Signal(object)  # Entry — slice the tiles
    new_bookmark_requested = Signal(object)  # Entry (a FILE) — bookmark the view
    edit_slice_requested = Signal(object)  # Entry (a SLICE) — edit its coordinates
    jump_to_source_requested = Signal(object)  # Entry (a SLICE) — show it in its parent
    jump_to_bookmark_requested = Signal(object)  # Entry (a BOOKMARK) — apply + jump
    rename_committed = Signal(object, str)  # Entry, new name — a finished rename

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._tree = _EntryTree()
        self._tree.setHeaderHidden(True)
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
        # Both built lazily and theme-coloured; cached until the panel is rebuilt.
        self._bookmark_icon: QIcon | None = None
        self._slice_icon: QIcon | None = None
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

    def is_key_navigating(self) -> bool:
        """True while a selection change is being driven by the arrow keys — the
        main window checks this to leave focus on the list rather than handing
        it to the view, so browsing with the keyboard isn't cut short."""
        return self._tree.key_navigating

    # -- model mirroring (driven by workspace callbacks) ---------------------
    def add_entry(self, entry: Entry, parent: Entry | None = None) -> None:
        """Add ``entry``; a slice or bookmark nests under ``parent``'s item,
        inserted so children stay ordered by offset (files keep open order)."""
        item = QTreeWidgetItem()
        item.setData(0, Qt.ItemDataRole.UserRole, entry)
        parent_item = self._items.get(parent) if parent is not None else None
        if parent_item is not None:
            parent_item.insertChild(
                self._sorted_index(parent_item, entry.slice_offset), item
            )
            parent_item.setExpanded(True)
        else:
            self._tree.addTopLevelItem(item)
        self._items[entry] = item
        self._refresh_item(entry, item)

    def _sorted_index(self, parent_item: QTreeWidgetItem, offset: int) -> int:
        """The child index at which an entry of ``offset`` belongs — the first
        child whose offset is greater, keeping equal offsets in arrival order."""
        for i in range(parent_item.childCount()):
            if self._offset_of(parent_item.child(i)) > offset:
                return i
        return parent_item.childCount()

    @staticmethod
    def _offset_of(item: QTreeWidgetItem) -> int:
        entry: Entry = item.data(0, Qt.ItemDataRole.UserRole)
        return entry.slice_offset

    def remove_entry(self, entry: Entry) -> None:
        item = self._items.pop(entry, None)
        if item is None:
            return  # its item already went down with its parent file's
        # A file's item takes its nested slice items with it — drop them from
        # the map now, so the slices' own removal notifications (the workspace
        # removes a file's slices with it) don't touch the deleted items.
        for i in range(item.childCount()):
            self._items.pop(item.child(i).data(0, Qt.ItemDataRole.UserRole), None)
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
        """Re-render one entry's label (dirty marker, backfilled length, …) and
        re-sort it if an edit moved its offset."""
        item = self._items.get(entry)
        if item is not None:
            self._refresh_item(entry, item)
            self._reorder_child(entry, item)

    def _reorder_child(self, entry: Entry, item: QTreeWidgetItem) -> None:
        """Move ``item`` back into offset order among its siblings if an offset
        edit misplaced it. The list was sorted before the change, so only an
        immediate neighbour can be out of order — check those and skip the
        take/re-insert (which would disturb selection) when already in place."""
        parent_item = item.parent()
        if parent_item is None:
            return  # a file: top-level items keep open order
        index = parent_item.indexOfChild(item)
        offset = entry.slice_offset
        last = parent_item.childCount() - 1
        prev_ok = index == 0 or self._offset_of(parent_item.child(index - 1)) <= offset
        next_ok = (
            index == last or self._offset_of(parent_item.child(index + 1)) >= offset
        )
        if prev_ok and next_ok:
            return
        was_current = self._tree.currentItem() is item
        self._tree.blockSignals(True)  # a take/re-insert must not re-activate
        parent_item.takeChild(index)
        parent_item.insertChild(self._sorted_index(parent_item, offset), item)
        if was_current:
            self._tree.setCurrentItem(item)
        self._tree.blockSignals(False)

    # -- presentation --------------------------------------------------------
    def _refresh_item(self, entry: Entry, item: QTreeWidgetItem) -> None:
        # The label is just the name (default slice names already read as
        # "offset (length) compression"); coordinates live in the tooltip so a
        # custom-named slice stays inspectable without cluttering the list.
        item.setText(0, f"● {entry.name}" if entry.dirty else entry.name)
        tip = entry.path
        if entry.kind is EntryKind.SLICE:
            # A picture glyph marks a slice as its own little graphic, telling it
            # apart from the ribbon-marked bookmarks it sits among.
            item.setIcon(0, self._picture_icon())
            tip += f"\nOffset {format_hex(entry.slice_offset)}\nLength " + (
                format_hex(entry.slice_length)
                if entry.slice_length is not None
                else "to be discovered"
            )
        elif entry.kind is EntryKind.BOOKMARK:
            # The ribbon icon is what tells a bookmark from its slice siblings
            # in the list; the tooltip spells it out.
            item.setIcon(0, self._ribbon_icon())
            tip += (
                f"\nBookmark at {format_hex(entry.slice_offset)}\nDouble-click to jump"
            )
        if entry.dirty:
            tip += "\nUnsaved changes"
        # A moved/missing referenced file leaves the entry partially working —
        # flag it amber so it stands out as needing Locate missing files.
        if entry_reference_missing(entry):
            tip += "\nReferenced file is missing — File ▸ Locate missing files"
            item.setBackground(0, _MISSING_HIGHLIGHT)
        else:
            item.setBackground(0, QBrush())
        item.setToolTip(0, tip)

    def _ribbon_icon(self) -> QIcon:
        """The bookmark marker: a flag glyph in the theme's accent colour."""
        if self._bookmark_icon is None:
            self._bookmark_icon = self._tinted_icon(
                "bookmark.png", QPalette.ColorRole.Highlight
            )
        return self._bookmark_icon

    def _picture_icon(self) -> QIcon:
        """The slice marker: a framed-picture glyph in the theme's text
        colour — the universal "this is a graphic" symbol."""
        if self._slice_icon is None:
            self._slice_icon = self._tinted_icon("slice.png", QPalette.ColorRole.Text)
        return self._slice_icon

    def _tinted_icon(self, filename: str, role: QPalette.ColorRole) -> QIcon:
        """A bundled ``icons/<filename>`` recoloured to a theme role.

        The art ships as white glyphs, pre-cropped to their opaque bounds (no
        baked-in margin to widen the gap to the entry name). We recolour to the
        palette role — keeping the icons theme-aware in light and dark — then
        fit the glyph, centred, into the icon box.
        """
        color = self.palette().color(role)
        source = QImage.fromData(resources.read_bytes("icons", filename))
        glyph = source.convertToFormat(QImage.Format.Format_ARGB32)
        # SourceIn keeps the glyph's alpha but replaces its colour with the tint.
        tinting = QPainter(glyph)
        tinting.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
        tinting.fillRect(glyph.rect(), color)
        tinting.end()
        scaled = QPixmap.fromImage(glyph).scaled(
            _ICON_W,
            _ICON_H,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        canvas = QPixmap(_ICON_W, _ICON_H)
        canvas.fill(Qt.GlobalColor.transparent)
        placing = QPainter(canvas)
        placing.drawPixmap(
            (_ICON_W - scaled.width()) // 2,
            (_ICON_H - scaled.height()) // 2,
            scaled,
        )
        placing.end()
        return QIcon(canvas)

    # -- interaction ---------------------------------------------------------
    def _on_current_item_changed(self, item: QTreeWidgetItem | None, _prev) -> None:
        if item is not None:
            self.entry_activated.emit(item.data(0, Qt.ItemDataRole.UserRole))

    def _remove_current(self) -> None:
        """The Delete shortcut: request removal of the highlighted entry."""
        item = self._tree.currentItem()
        if item is not None and self._editing is None:  # not mid-rename
            self.remove_requested.emit(item.data(0, Qt.ItemDataRole.UserRole))

    def _on_double_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        # A bookmark's double-click is its primary action — jump to it (rename
        # stays on the context menu); a slice's double-click opens the renamer.
        entry: Entry = item.data(0, Qt.ItemDataRole.UserRole)
        if entry.kind is EntryKind.BOOKMARK:
            self.jump_to_bookmark_requested.emit(entry)
        else:
            self._begin_rename(entry)

    # -- rename --------------------------------------------------------------
    def _begin_rename(self, entry: Entry) -> None:
        """Open the inline editor on ``entry``'s item (slices and bookmarks —
        a file's name is its on-disk basename, not free text)."""
        item = self._items.get(entry)
        if item is None or entry.kind is EntryKind.FILE:
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
            # Only files spawn slices and bookmarks (neither nests), so the
            # menu shows these on files alone. All but the plain dialog
            # additionally need the file on screen — the viewport, selection
            # and settings snapshot live only there.
            sliceable = entry is self._current and entry.doc is not None
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
            bookmark = menu.addAction("New Bookmark")
            bookmark.triggered.connect(lambda: self.new_bookmark_requested.emit(entry))
            bookmark.setEnabled(sliceable)
            menu.addSeparator()
        elif entry.kind is EntryKind.SLICE:
            # A slice's primary navigation action: reopen its region in the
            # parent file, decoded the slice's way, at the slice's offset.
            jump = menu.addAction("Jump to Source")
            jump.triggered.connect(lambda: self.jump_to_source_requested.emit(entry))
            menu.addSeparator()
            rename = menu.addAction("Rename…")
            rename.triggered.connect(lambda: self._begin_rename(entry))
            edit = menu.addAction("Edit…")
            edit.triggered.connect(lambda: self.edit_slice_requested.emit(entry))
            menu.addSeparator()
        else:
            # The double-click action, discoverable; a bookmark holds no bytes
            # of its own, so there is no Write here.
            jump = menu.addAction("Jump to Bookmark")
            jump.triggered.connect(lambda: self.jump_to_bookmark_requested.emit(entry))
            menu.addSeparator()
            rename = menu.addAction("Rename…")
            rename.triggered.connect(lambda: self._begin_rename(entry))
            menu.addSeparator()
        if entry.kind is not EntryKind.BOOKMARK:
            write = menu.addAction("Write")
            write.triggered.connect(lambda: self.write_requested.emit(entry))
            # Writing needs a loaded, write-capable document; a never-activated
            # or view-only entry has nothing to write.
            write.setEnabled(
                entry.doc is not None and entry.doc.pixel_config.write_enabled
            )
            menu.addSeparator()
        remove = menu.addAction("Remove")
        # Display-only shortcut hint: the working binding is the tree-focused
        # QShortcut; a menu action's own shortcut is inert while the menu is
        # closed, so this just labels the key in the shortcut column.
        remove.setShortcut(QKeySequence.StandardKey.Delete)
        remove.triggered.connect(lambda: self.remove_requested.emit(entry))
        menu.exec(self._tree.viewport().mapToGlobal(pos))
