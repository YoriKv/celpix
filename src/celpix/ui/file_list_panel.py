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

**Several rows can be selected at once** — Shift for a range, Ctrl for one more
— and the *first* of them stays the open document: extending a selection never
switches the view, so the picture on the canvas is still the row the user opened.
Only the operations that mean something over a set of rows act on the whole
selection (Remove, the one-step Move Up/Down, Paste Inputs, and Export to a
folder); everything else here is about one entry, and goes dead while more than
one is picked. The window's own
entry-scoped menu rows do the same (``MainWindow._sync_entry_scope``).

A **filter field** sits under the list, matching the same way the format pickers'
search does (:func:`~celpix.ui.searchable_combo.matches_search`) — every word
typed, in any order. A mapped ROM runs to hundreds of rows, and scrolling for one
slice is what it replaces. Rows are *hidden*, never re-ordered or removed: the
order is the user's (:meth:`FileListPanel.add_entry`) and a filter is a way of
looking at the list, not a change to it.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QItemSelectionModel, QSize, Qt, Signal
from PySide6.QtGui import (
    QIcon,
    QKeySequence,
    QShortcut,
)
from PySide6.QtWidgets import (
    QHeaderView,
    QLineEdit,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from celpix.core.capabilities import ContentKind
from celpix.plugins.registry import Registry
from celpix.project.workspace import (
    Entry,
    EntryKind,
    can_supply_palette,
    section_kind,
)
from celpix.ui.entry_menu import EntryMenuMixin
from celpix.ui.entry_rows import ICON_H, ICON_W, STATUS_COL, STATUS_W, EntryRowsMixin
from celpix.ui.entry_tree import EntryTree
from celpix.ui.icons import Icon
from celpix.ui.searchable_combo import matches_search, search_needles
from celpix.ui.widgets import (
    IconBaker,
    select_only,
    signals_blocked,
)

# The section headings, in the order they appear. Dict order *is* the on-screen
# order, so a header inserts at its own place however the sections were opened:
# what you hold most of the time first, what is applied onto it last.
SECTIONS: dict[ContentKind, str] = {
    ContentKind.PIXELS: "Pixels",
    ContentKind.TILEMAP: "Tilemaps",
    ContentKind.PALETTE: "Palettes",
}

# Jump to the filter field. Bound by the *window* (Navigate ▸ Find Entry), for
# the reason given where the field is built, and named here because the field is
# what it lands on and its tooltip advertises it.
FILTER_KEY = QKeySequence.StandardKey.Find


def _stepped(group: list[Entry], picked: set[Entry], delta: int) -> list[Entry]:
    """``group`` with every row in ``picked`` moved one place ``delta``.

    Each picked row is swapped past its unpicked neighbour, working from the end
    the rows are moving *towards*. Two things fall out of that order rather than
    needing a rule of their own: a run of picked rows travels as one, because
    by the time the second of them is reached the first has already vacated the
    slot behind it; and a run that has run into the end of the group pins the rows
    behind it, because their neighbour is picked too and no swap applies. So a
    selection keeps both its own order and its shape, and a Move Up repeated at
    the top of a list is a no-op rather than a slow collapse.
    """
    order = list(group)
    step = -1 if delta < 0 else 1
    span = range(1, len(order)) if step < 0 else range(len(order) - 2, -1, -1)
    for at in span:
        if order[at] in picked and order[at + step] not in picked:
            order[at], order[at + step] = order[at + step], order[at]
    return order


class FileListPanel(EntryRowsMixin, EntryMenuMixin, IconBaker, QWidget):
    """The Files dock; see the module docs.

    Three halves in three modules: the tree widget and its keys
    (:mod:`celpix.ui.entry_tree`), how a row reads (:mod:`celpix.ui.entry_rows`)
    and the context menu (:mod:`celpix.ui.entry_menu`). What is here mirrors the
    workspace into rows, filters them, and turns selection into signals.
    """

    entry_activated = Signal(object)  # Entry — the user selected it in the list
    # The rows selected changed — the window re-decides its one-entry menu rows.
    selection_changed = Signal()
    remove_requested = Signal(object)  # list[Entry] — take them out of the list
    # Entry, and the row it should land in front of (None — last among its
    # siblings). The drag's signal, and what one row's Move Up/Down comes down to.
    reorder_requested = Signal(object, object)
    # list[Entry], -1 / +1 — step every selected row one place within its group.
    move_requested = Signal(object, int)
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
    # list[Entry], raw — a multi-row selection to a folder, as PNGs or raw dumps
    export_entries_requested = Signal(object, bool)
    import_png_requested = Signal(object)  # Entry (FILE/SLICE) — image over its start
    new_slice_requested = Signal(object)  # Entry (a FILE) — open the slice dialog
    new_slice_from_view_requested = Signal(object)  # Entry — slice the viewport
    new_slice_from_selection_requested = Signal(object)  # Entry — slice the tiles
    new_bookmark_requested = Signal(object)  # Entry (a FILE) — bookmark the view
    change_container_requested = Signal(object)  # Entry (FILE/PALETTE) — its container
    container_info_requested = Signal(object)  # Entry (FILE/PALETTE) — what it read
    use_palette_requested = Signal(object)  # Entry (a PALETTE) — apply to the view
    # Entry (FILE/SLICE/COMPOSITE) — read the current graphic's palette out of
    # this row's bytes (``docs/design/palette-editing.md``).
    use_entry_as_palette_requested = Signal(object)
    edit_slice_requested = Signal(object)  # Entry (a SLICE) — edit its coordinates
    inputs_requested = Signal(object)  # Entry — what its formats need from elsewhere
    copy_inputs_requested = Signal(object)  # Entry — its bindings to the clipboard
    paste_inputs_requested = Signal(object)  # list[Entry] — bindings onto these rows
    edit_composite_requested = Signal(object)  # Entry (a COMPOSITE) — re-list it
    new_composite_requested = Signal(object)  # list[Entry] — assemble one from these
    jump_to_source_requested = Signal(object)  # Entry (a SLICE) — show it in its parent
    # CompositePiece — open the piece's entry at the first byte the piece takes
    jump_to_piece_requested = Signal(object)
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
        # Answered by the window once it is wired up (:meth:`set_inputs_probe`).
        self._inputs_probe: Callable[[Entry], bool] = lambda _entry: False
        # Likewise (:meth:`set_row_order`); appending is the honest stand-in for
        # a panel nobody has told about a list.
        self._row_order: Callable[[Entry], Entry | None] = lambda _entry: None
        self._tree = EntryTree()
        self._tree.setHeaderHidden(True)
        self._tree.setColumnCount(2)
        header = self._tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(STATUS_COL, QHeaderView.ResizeMode.Fixed)
        header.resizeSection(STATUS_COL, STATUS_W)
        self._tree.setIconSize(QSize(ICON_W, ICON_H))  # tighten icon-to-name gap
        self._tree.setRootIsDecorated(True)
        self._tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._show_menu)
        # Selection *is* activation: a single click switches the active view,
        # like every file-switcher sidebar — but only while the click leaves one
        # row picked, so extending a selection never moves the view off the entry
        # it started from. Programmatic syncs (set_current) block signals so only
        # user selection emits.
        self._tree.itemSelectionChanged.connect(self._on_selection_changed)
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
        # Built lazily and theme-colored; cached against the palette and pixel
        # ratio they were rasterized for (see ``EntryRowsMixin._bake_icons``).
        self._icons: dict[Icon, QIcon] = {}
        # One section header per content kind, created with that kind's first
        # entry and removed with its last, so a list holding only one kind shows
        # only its own heading. Ordered by SECTIONS below.
        self._sections: dict[ContentKind, QTreeWidgetItem] = {}
        self._items: dict[Entry, QTreeWidgetItem] = {}
        self._current: Entry | None = None  # mirrors the workspace's pointer
        self._has_selection = False  # mirrors the canvas's tile selection

        # Delete removes the highlighted entry - handled by the tree itself (see
        # EntryTree) rather than a QShortcut, so it wins the key over the
        # canvas's window-wide Clear/Delete instead of overloading with it.
        self._tree.delete_pressed.connect(self._remove_selected)
        self._tree.move_pressed.connect(self._move_selected)
        self._tree.cut_pressed.connect(lambda: self._for_current(self.cut_requested))
        self._tree.copy_pressed.connect(lambda: self._for_current(self.copy_requested))
        self._tree.duplicate_pressed.connect(
            lambda: self._for_current(self.duplicate_requested)
        )
        # The one row action that means something with nothing highlighted, and
        # with the highlight on a section header: a paste with no target lands
        # where the payload itself says it belongs. Still one row's action, so a
        # multi-row selection has no target to offer it (see _single_entry).
        self._tree.paste_pressed.connect(self._paste_at_current)
        self._tree.reorder_dropped.connect(self.reorder_requested)

        # The filter, and the expansion it is holding open. A project's rows run
        # to the hundreds — the melroon sample is 417 — and finding one slice in
        # that by scrolling is the gesture this replaces.
        self._filter = QLineEdit()
        self._filter.setPlaceholderText("Filter")
        self._filter.setClearButtonEnabled(True)
        self._filter.setToolTip(
            "Show only rows whose name contains every word typed,\n"
            "in any order (Ctrl+F)\n"
            "A matching slice keeps its file row. Esc clears"
        )
        self._filter.textChanged.connect(self._apply_filter)
        # Only while a filter is up: what the tree's expansion was before it
        # opened rows to reveal the matches, so clearing it puts back the shape
        # the user had arranged rather than leaving every file spread open.
        self._expanded_before: dict[Entry, bool] | None = None

        # Escape gets out of a filter, which is where a user who has found
        # nothing reaches first. Scoped to the field, so it is inert the moment
        # focus is anywhere else and never competes for the key. The way *in*
        # (Ctrl+F) is the window's, not this panel's: selecting a row hands focus
        # to the canvas, so a shortcut scoped here would be dead exactly when it
        # is wanted - see MainWindow._init_history.
        cancel = QShortcut(QKeySequence.StandardKey.Cancel, self._filter)
        cancel.setContext(Qt.ShortcutContext.WidgetShortcut)
        cancel.activated.connect(self._filter.clear)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._tree)
        layout.addWidget(self._filter)
        # Arms the re-bake on a theme or scale change; the list is empty yet.
        self._bake_if_stale()

    def is_key_navigating(self) -> bool:
        """True while a selection change is being driven by the arrow keys — the
        main window checks this to leave focus on the list rather than handing
        it to the view, so browsing with the keyboard isn't cut short."""
        return self._tree.key_navigating

    # -- model mirroring (driven by workspace callbacks) ---------------------
    def add_entry(
        self, entry: Entry, parent: Entry | None = None, before: Entry | None = None
    ) -> None:
        """Add ``entry``; a slice or bookmark nests under ``parent``'s item —
        a file's or a palette's, or another slice's for a nested slice, to any
        depth.

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
        # being dragged (``EntryTree.startDrag``): a drop lands between rows
        # (see ``EntryTree._drop_before``), and a row that accepted one at any
        # other time would offer a re-parenting the panel has no meaning for.
        item.setFlags(
            (item.flags() | Qt.ItemFlag.ItemIsDragEnabled)
            & ~Qt.ItemFlag.ItemIsDropEnabled
        )
        parent_item = self._items.get(parent) if parent is not None else None
        if parent_item is None:
            parent_item = self._section_root(self.section_of(entry))
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
        # A row added under a live filter has to face it like the rest, or a new
        # slice appears in a list that is supposed to be showing only matches.
        self._refilter()

    def move_item(self, entry: Entry, before: Entry | None) -> None:
        """Re-place ``entry``'s row in front of ``before``'s — the view side of
        :meth:`~celpix.project.workspace.Workspace.reorder`.

        A file's row carries its nested slices and bookmarks with it, since they
        are its item's children. Expansion and the *selection* live in the view
        rather than on the item, so a row taken out comes back collapsed and
        unpicked, and so does every row nested under it — both are put back
        explicitly. Expansion is restored for the whole subtree, to any depth,
        each row as it was: a slice holding slices of its own stays open or
        folded as the user left it. The selection is restored whole,
        and to whatever it was on, which need not be the moved row itself:
        reordering a file while one of its slices is the shown entry takes that
        row out of the tree too, and it must come back current; and a Move Up over
        several picked rows re-places them one at a time, so each step has to hand
        the others' highlight back or the next would find nothing selected.
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
        was_selected = self._tree.selectedItems()
        was_expanded = self._subtree_expansion(item)
        with signals_blocked(self._tree):  # a take/re-insert must not re-activate
            parent_item.takeChild(index)
            parent_item.insertChild(target, item)
            for row, expanded in was_expanded:
                row.setExpanded(expanded)
            # The current row goes back with the selection cleared, and the
            # picked rows after it — so the result is exactly the rows picked
            # before. The command is explicit: a bare setCurrentItem reads the
            # held modifiers as one, and a Ctrl-held move would toggle the row.
            if was_current is not None:
                self._tree.setCurrentItem(
                    was_current, 0, QItemSelectionModel.SelectionFlag.Clear
                )
            for picked in was_selected:
                picked.setSelected(True)

    @staticmethod
    def _subtree_expansion(
        item: QTreeWidgetItem,
    ) -> list[tuple[QTreeWidgetItem, bool]]:
        """``item`` and every item nested under it, each with whether it is
        expanded — read while they are still in the tree, since taking them out
        of it forgets the lot."""
        found: list[tuple[QTreeWidgetItem, bool]] = []
        pending = [item]
        while pending:
            row = pending.pop()
            found.append((row, row.isExpanded()))
            pending += [row.child(i) for i in range(row.childCount())]
        return found

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
        """Whether a file's or palette's item has at least one slice child —
        its bookmark children don't count, holding no bytes to export."""
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
        # The filter named rows that no longer exist. Left standing it would
        # hand the project being swapped in the last one's search — blocked,
        # since there is nothing left for a re-filter to walk.
        with signals_blocked(self._filter):
            self._filter.clear()
        self._expanded_before = None

    def remove_entry(self, entry: Entry) -> None:
        item = self._items.pop(entry, None)
        if item is None:
            return  # its item already went down with its parent file's
        # A row's item takes every item nested under it along, to any depth —
        # drop them from the map now, so their own removal notifications (the
        # workspace removes a row's whole subtree with it) don't touch the
        # deleted items.
        below = [item.child(i) for i in range(item.childCount())]
        while below:
            child = below.pop()
            self._items.pop(child.data(0, Qt.ItemDataRole.UserRole), None)
            below += [child.child(i) for i in range(child.childCount())]
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
        # The removed row may have been the only *visible* one under its
        # section, which the header-drop above cannot see: its siblings are
        # still there, merely filtered out.
        self._refilter()

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
        """Point the highlight at the entry the canvas is showing.

        Setting the current row also narrows the selection to it, which is the
        right answer: the view moved, so whatever set of rows was picked before
        was picked around a different document. The change is announced by hand —
        the tree's own signal is blocked here so a programmatic sync cannot read
        as a user's activation, and the window still has to re-decide the menu
        rows a multi-row selection had switched off.
        """
        previous, self._current = self._current, entry
        with signals_blocked(self._tree):
            select_only(self._tree, self._items.get(entry) if entry else None)
        # The wash marks what the canvas is showing, so it moves with it: repaint
        # the row losing it and the row taking it, and nothing else.
        for changed in (previous, entry):
            item = self._items.get(changed) if changed is not None else None
            if item is not None:
                self._refresh_item(changed, item)
        self.selection_changed.emit()

    # -- what is selected ----------------------------------------------------
    def selected_entries(self) -> list[Entry]:
        """Every selected row's entry, in the order the rows are shown.

        Walked rather than read off ``selectedItems``, which answers in selection
        order and includes rows the **filter** is hiding: a Shift-range spans the
        model, so it picks up whatever the filter took out of the middle of it,
        and a Remove over that would take entries the user could not see it name.
        A hidden row is not part of what was picked.

        Section headers are not selectable and so never appear; a rename in
        flight yields nothing, for the reason :meth:`_current_entry` gives.
        """
        if self._editing is not None:
            return []
        picked: list[Entry] = []

        def visit(item: QTreeWidgetItem) -> None:
            for at in range(item.childCount()):
                child = item.child(at)
                entry = child.data(0, Qt.ItemDataRole.UserRole)
                if entry is not None and child.isSelected() and not child.isHidden():
                    picked.append(entry)
                visit(child)

        for at in range(self._tree.topLevelItemCount()):
            visit(self._tree.topLevelItem(at))
        return picked

    def has_multi_selection(self) -> bool:
        """Whether more than one row is picked — the state that switches off
        every action about a single entry, here and in the window's menus.

        Off the selection rather than out of :meth:`selected_entries`, whose walk
        of the whole tree this is asked far too often to afford: it runs from
        every menu-gating pass, and those run on every view refresh, so on a
        project of several hundred rows it was a scan of all of them per edit.
        The same two rows are skipped for the same reasons — a hidden row is not
        part of what was picked, and a rename in flight yields nothing — and
        section headers cannot be selected in the first place.
        """
        if self._editing is not None:
            return False
        seen = 0
        for item in self._tree.selectedItems():
            if item.isHidden() or item.data(0, Qt.ItemDataRole.UserRole) is None:
                continue
            seen += 1
            if seen > 1:
                return True
        return False

    def move_orders(self, entries: list[Entry], delta: int) -> list[list[Entry]]:
        """The new order of each on-screen group a one-step move of ``entries``
        would rearrange; the groups that cannot move are left out.

        A selection can straddle groups — a palette and a file sit under different
        headings — so the answer is one order per group rather than one list. Each
        is the whole group in its new order, which is what a reorder command
        takes: a move of several rows is not a sequence of independent nudges (the
        rows in front of them have to shuffle the other way), and an order says
        the result without having to describe the shuffling.

        Empty is the honest "nothing to do": every picked row in every group has
        already run into the end it is moving towards.
        """
        picked = set(entries)
        orders: list[list[Entry]] = []
        heads: list[Entry] = []  # the groups already answered for, by first row
        for entry in entries:
            group = self.sibling_entries(entry)
            if not group or any(group[0] is head for head in heads):
                continue
            heads.append(group[0])
            order = _stepped(group, picked, delta)
            if order != group:  # Entry is eq=False, so this compares identity
                orders.append(order)
        return orders

    def set_has_selection(self, active: bool) -> None:
        """Mirror whether the canvas has a tile selection (gates the
        selection-based context-menu action)."""
        self._has_selection = active

    def set_inputs_probe(self, probe: Callable[[Entry], bool]) -> None:
        """Who answers "does this entry have inputs to bind?" for the menu row.

        The window's question, not the panel's: whether a *file* has any depends
        on the codec its compression preview is set to, which only the toolbar
        knows (``main_window/inputs.py``).
        """
        self._inputs_probe = probe

    def set_row_order(self, probe: Callable[[Entry], Entry | None]) -> None:
        """Who answers "which row does this one go in front of?" for a re-file.

        The workspace's question, not the panel's: the order of the rows is the
        user's and only the list knows it (:meth:`add_entry`). The panel needs it
        for the one case where a row that already exists has to be put somewhere
        else — a composite whose format made it a colour table, or stopped making
        it one (:meth:`section_of`).
        """
        self._row_order = probe

    def section_of(self, entry: Entry) -> ContentKind:
        """:func:`~celpix.project.workspace.section_kind` against this panel's
        registry — the one question every placement here is decided by."""
        return section_kind(entry, self._registry)

    def _refile(self, entry: Entry, item: QTreeWidgetItem) -> bool:
        """Move ``entry``'s row into the section it now belongs to; True if it did.

        The **one** deliberate exception to :meth:`refresh_entry`'s rule that a
        row does not move. That rule is about order *within* a group — a slice
        re-pointed to another offset stays where the user put it — and this is
        not a reorder at all: the row has changed which group it is in, and a
        heading that no longer describes the rows under it is worse than a row
        that moved. It happens when a composite's pixel format is switched to or
        from the swatch codec, undo and redo included
        (``docs/design/palette-editing.md``).

        Only a **top-level** row can: the sections hold those, while a slice or a
        bookmark hangs off its own file's item and is not filed by section at all.
        Removing and re-adding is how it is done rather than a reparent, because
        that is the one path that creates and retires the headings, applies the
        live filter and honours the workspace's order for the new position.
        """
        parent = item.parent()
        if parent is None or parent.data(0, Qt.ItemDataRole.UserRole) is not None:
            return False  # nested under its file: not a section's row at all
        if parent is self._sections.get(self.section_of(entry)):
            return False
        was_current = self._current is entry
        was_selected = item.isSelected()
        self.remove_entry(entry)
        self.add_entry(entry, None, self._row_order(entry))
        if was_current:
            self.set_current(entry)
        elif was_selected:
            moved = self._items.get(entry)
            if moved is not None:
                with signals_blocked(self._tree):  # a re-file is not a click
                    moved.setSelected(True)
            # Out of Palettes, selecting a row opens it (_on_selection_changed).
            # A row carried out still picked was only ever selected, and clicking
            # it again changes no selection — so it would sit highlighted over a
            # canvas showing something else, its menu offering it to that
            # entry, until the user clicked away and back. Open it as the click
            # would have.
            selected = self.selected_entries()
            if (
                len(selected) == 1
                and selected[0] is entry
                and self.section_of(entry) is not ContentKind.PALETTE
            ):
                self.entry_activated.emit(entry)
        return True

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
        # A copy: a re-file below removes and re-adds, which rewrites the map.
        for entry, item in list(self._items.items()):
            if not self._refile(entry, item):
                self._refresh_item(entry, item)

    def refresh_entry(self, entry: Entry) -> None:
        """Re-render one entry's label — the dirty marker, a backfilled length, a
        notice its load raised.

        The row does **not** move for it *within* its group. A slice re-pointed
        to another offset stays where the user put it: the order is theirs from
        the moment the row exists (:meth:`add_entry`), and a list that rearranged
        itself under an edit would undo an arrangement nothing asked it to.

        It does move **between** groups, which is the one thing that is not a
        reorder: a row whose section has changed is under a heading that no
        longer describes it (:meth:`_refile`).
        """
        item = self._items.get(entry)
        if item is None:
            return
        if self._refile(entry, item):
            return  # re-added, which rendered and re-filtered it on the way in
        self._refresh_item(entry, item)
        # A rename is the label the filter matches on, so the row may have
        # just stopped matching — or started.
        self._refilter()

    # -- filtering -----------------------------------------------------------
    def focus_filter(self) -> None:
        """Put the cursor in the filter field with its text selected — Ctrl+F.

        Selected rather than appended to, so a second search is typed over the
        first instead of having to be cleared out of the way.
        """
        self._filter.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self._filter.selectAll()

    def is_filtered(self) -> bool:
        """Whether a filter is narrowing the list — so a caller that counts rows
        knows the list is not the whole project."""
        return bool(self._filter.text().strip())

    def _apply_filter(self) -> None:
        """Re-hide every row for the filter as it now stands.

        The whole tree each time rather than a delta, because a filter is edited
        a keystroke at a time and each one both hides and *un*-hides: narrowing
        "obj" to "object" drops rows, and going back adds them, and there is no
        cheaper answer than the walk for a list this size.
        """
        needles = search_needles(self._filter.text())
        if needles and self._expanded_before is None:
            # Entering filter mode. The expansion about to be forced open to
            # reveal matches is the user's own arrangement, and taking it now is
            # the only moment it is still there to take.
            self._expanded_before = {
                entry: item.isExpanded() for entry, item in self._items.items()
            }
        # Hiding the row the highlight sits on makes Qt move the highlight, and
        # that reads as the user picking another entry — which would load it.
        # So the walk is silent and the highlight is put back by hand after it.
        with signals_blocked(self._tree):
            for i in range(self._tree.topLevelItemCount()):
                section = self._tree.topLevelItem(i)
                kept = [
                    self._filter_subtree(section.child(j), needles)
                    for j in range(section.childCount())
                ]
                # A section heading is never matched on its own text: it has no
                # entry behind it, so "Pixels" as a hit would show the user a
                # heading standing over nothing.
                section.setHidden(not any(kept))
            if not needles:
                self._restore_expansion()
            shown = self._items.get(self._current) if self._current else None
            select_only(
                self._tree,
                shown if shown is not None and not shown.isHidden() else None,
            )

    def _filter_subtree(self, item: QTreeWidgetItem, needles: list[str]) -> bool:
        """Hide ``item`` unless it or a descendant matches; True when it stays.

        Both halves of the rule are here. A row is kept for its **own** text, and
        also for a **descendant's** — so a slice found by name brings its file
        along and the answer still says which file each match came from. The
        reverse deliberately does not hold: matching a file does not drag its
        slices back in, which would answer a search with the list it was asked to
        narrow.
        """
        kept = [
            self._filter_subtree(item.child(i), needles)
            for i in range(item.childCount())
        ]
        # Built as a list rather than folded into the ``any`` below: the walk is
        # what *sets* each descendant's hidden flag, and short-circuiting it
        # would leave every row after the first match wearing the last search's.
        descendant = any(kept)
        item.setHidden(not (descendant or matches_search(item.text(0), needles)))
        if descendant:
            item.setExpanded(True)  # or the matches it was kept for stay unseen
        return not item.isHidden()

    def _restore_expansion(self) -> None:
        """Put back the expansion the filter opened — see :attr:`_expanded_before`.

        Rows added while the filter was up aren't in the snapshot and keep what
        they have: they were never part of the arrangement being restored.
        """
        was, self._expanded_before = self._expanded_before, None
        for entry, expanded in (was or {}).items():
            item = self._items.get(entry)
            if item is not None:
                item.setExpanded(expanded)

    def _refilter(self) -> None:
        """Re-run the filter after a change to the rows, when one is up.

        Guarded on the text rather than run unconditionally because the common
        case is no filter at all, and then there is nothing for a walk to do.
        """
        if self._filter.text():
            self._apply_filter()

    # -- interaction ---------------------------------------------------------
    def _on_selection_changed(self) -> None:
        """A row selection the *user* made — activate it, if it names one entry.

        Driven by the selection rather than by the current row, which is what
        keeps the first-picked row on screen while a Shift- or Ctrl-click extends
        around it: Qt moves the current row to whatever was last clicked, so a
        handler reading that would switch the view on every extension. One
        selected row is the whole of what activation means here.

        The Palettes header carries no entry and is not selectable, so it cannot
        arrive as one; a row already on screen re-selecting itself is not an
        activation either, and the window would ignore it anyway.

        **A row filed under Palettes is selected, never opened.** That is what a
        ``.pal`` has always done — it is *applied* onto whatever is on screen
        rather than shown — and a swatch composite filed there behaves the same
        way, because the first click of its double-click would otherwise open it
        and there would be no gesture left for applying it
        (:meth:`_on_double_clicked`).
        """
        selected = self.selected_entries()
        self.selection_changed.emit()
        if len(selected) == 1 and selected[0] is not self._current:
            if self.section_of(selected[0]) is ContentKind.PALETTE:
                return
            self.entry_activated.emit(selected[0])

    def _current_entry(self) -> Entry | None:
        """The highlighted row's entry — None for nothing, a section header, or
        a rename in flight (where every row key belongs to the editor)."""
        item = self._tree.currentItem()
        if item is None or self._editing is not None:
            return None
        return item.data(0, Qt.ItemDataRole.UserRole)

    def _single_entry(self) -> Entry | None:
        """The one selected row's entry, or None while several are — the gate in
        front of every action that has an answer for exactly one entry.

        Read off the *selection* rather than off :meth:`_current_entry`, so the
        row a key acts on is the row the user can see is picked.
        """
        selected = self.selected_entries()
        return selected[0] if len(selected) == 1 else None

    def _for_current(self, signal) -> None:  # noqa: ANN001 — a Signal to emit
        """Fire a one-entry row signal for the selected row, if there is just one."""
        entry = self._single_entry()
        if entry is not None:
            signal.emit(entry)

    def _paste_at_current(self) -> None:
        """The Paste shortcut: land the clipboard beside the one selected row.

        Fires with *nothing* selected too — a paste with no target lands where the
        payload itself says it belongs — but not with several, which name no one
        place to put it.
        """
        if not self.has_multi_selection():
            self.paste_requested.emit(self._current_entry())

    def _remove_selected(self) -> None:
        """The Delete shortcut: request removal of every selected entry."""
        entries = self.selected_entries()
        if entries:
            self.remove_requested.emit(entries)

    def _move_selected(self, delta: int) -> None:
        """The Alt+Up/Down shortcut: step every selected row one place.

        Every kind, unlike the file-only move this grew out of: the order of the
        whole list is the user's, so a slice moves among its parent's children and
        a palette among the palettes, each within the group its row sits in — and
        a selection spanning two groups moves in both.
        """
        entries = self.selected_entries()
        if entries:
            self.move_requested.emit(entries, delta)

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
        elif self.section_of(entry) is ContentKind.PALETTE:
            # A swatch composite: filed with the palettes, so its double-click
            # means what theirs does. Where there is nothing to apply it to —
            # nothing open, or the row is what is open — it opens instead, which
            # is the only other thing a double-click could sensibly mean
            # (``docs/design/palette-editing.md``). Rename stays on the context
            # menu, as it does for a ``.pal``.
            if self._current is not None and can_supply_palette(self._current, entry):
                self.use_entry_as_palette_requested.emit(entry)
            else:
                self.entry_activated.emit(entry)
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
