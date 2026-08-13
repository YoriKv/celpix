"""The composite dialog: which entries a view is assembled from, and in what order.

One dialog serves both creating a composite view (New Composite View) and
editing an existing one's pieces — the caller sets the ``title`` and prefills
the list.

The list **is** the entry (``docs/design/composite-entry.md``): an order and a
running length, and nothing else. No formats — a composite carries no container,
reshape or compression of its own because every piece carries its own, and the
*pixel* format is set on the view bar like any other entry's, since a tile window
is read at whatever depth its consumer wants rather than its sources'.

**Runs are measured in bytes**, and the two position columns say the same place
twice on purpose. *At* is the byte the run starts on, which is what a VRAM upload
table is written in and what a piece's own range addresses; *Tile* is that same
place in the index space a tilemap sees, which is what a cell means. Both are
derived from the list above them, so a move or a removal re-answers every row.

**A blank row is the only editable length.** A source row's is its entry's own
size, or a range a project stated; neither is this dialog's to invent, and it
reads no bytes to check. A hole has no entry to speak for it — the tile windows
this exists to reproduce have gaps (a console loads one span, skips the next),
and that length is what keeps every piece after it on the index the maps expect.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QSpinBox,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from celpix.core.address import format_hex
from celpix.project.workspace import CompositePiece, Entry, can_compose

__all__ = ["CompositeDialog", "CompositeParams"]

# What a blank run may be dialled to, in bytes. The ceiling is a whole console
# tile window several times over — a composite view is an index space, and every
# index space in hand is far smaller — so it is a guard against a typo costing
# gigabytes rather than a claim about what is reasonable.
MAX_PAD_BYTES = 0x100000


class CompositeParams:
    """What the dialog returns: a name and an ordered list of pieces.

    A plain object rather than a dataclass of two fields so it reads the same way
    :class:`~celpix.project.workspace.SliceParams` does at the call site — the
    dialog's result flows straight into an undo command without a field-by-field
    copy.
    """

    def __init__(self, name: str, pieces: tuple[CompositePiece, ...]) -> None:
        self.name = name
        self.pieces = pieces

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, CompositeParams)
            and other.name == self.name
            and other.pieces == self.pieces
        )


class CompositeDialog(QDialog):
    def __init__(
        self,
        *,
        entry: Entry,
        candidates: list[Entry],
        tile_bytes: int,
        name: str = "",
        pieces: tuple[CompositePiece, ...] = (),
        title: str = "New Composite View",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self._entry = entry
        # Filtered by the same rule the assembly applies, so what is offered and
        # what is accepted cannot disagree (``can_compose``): no composites, no
        # maps, no palettes, and never this entry itself.
        self._candidates = [e for e in candidates if can_compose(entry, e)]
        self._tile_bytes = max(1, tile_bytes)
        self._params: CompositeParams | None = None

        self._name = QLineEdit(name)
        self._name.setToolTip("Name in the Files list")

        self._list = QTreeWidget()
        self._list.setHeaderLabels(["At", "Tile", "Source", "Bytes"])
        self._list.setRootIsDecorated(False)
        self._list.setUniformRowHeights(True)
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._list.setToolTip(
            "The runs this view is assembled from, in order.\n"
            "Byte N of the view is byte N of whichever run covers it,\n"
            "so the order here is the index space a tilemap sees."
        )
        header = self._list.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)

        self._total = QLabel()
        self._error = QLabel()
        self._error.setStyleSheet("color: #c04040;")
        self._error.hide()

        self._add_source = QPushButton("Add source…")
        self._add_source.setToolTip(
            "Append a run holding the whole of another open entry.\n"
            "A run may be narrowed to a byte range of that entry's\n"
            "resolved data, which is how part of a compressed blob\n"
            "is reached - but only the project file states one today."
        )
        self._add_source.setMenu(self._source_menu())
        self._add_blank = QPushButton("Add blank")
        self._add_blank.setToolTip(
            "Append a run of blank bytes, for a hole in the window\n"
            "being reproduced. Nothing owns those bytes, so they\n"
            "cannot be painted on."
        )
        self._add_blank.clicked.connect(
            lambda: self._append(CompositePiece(length=self._tile_bytes))
        )
        self._up = QPushButton("Move up")
        self._up.clicked.connect(lambda: self._move(-1))
        self._down = QPushButton("Move down")
        self._down.clicked.connect(lambda: self._move(1))
        self._remove = QPushButton("Remove")
        self._remove.clicked.connect(self._remove_selected)

        layout = QVBoxLayout(self)
        naming = QHBoxLayout()
        naming.addWidget(QLabel("Name:"))
        naming.addWidget(self._name)
        layout.addLayout(naming)
        layout.addWidget(self._list)
        layout.addWidget(self._total)
        buttons_row = QHBoxLayout()
        for button in (
            self._add_source,
            self._add_blank,
            self._up,
            self._down,
            self._remove,
        ):
            buttons_row.addWidget(button)
        buttons_row.addStretch(1)
        layout.addLayout(buttons_row)
        layout.addWidget(self._error)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._validate_and_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        for piece in pieces:
            self._append(piece)
        self._list.currentItemChanged.connect(lambda *_: self._sync_buttons())
        self._sync_buttons()

    # -- building the list -------------------------------------------------
    def _source_menu(self) -> QMenu:
        """The Add source menu: every entry this composite may read, in list order.

        Disabled-looking-but-present is not offered — an entry that cannot supply
        bytes is simply absent, because the reason it cannot (it is a map, a
        palette, another composite) is a property of that entry rather than
        something the user can fix from here.
        """
        menu = QMenu(self)
        if not self._candidates:
            action = menu.addAction("No other pixel entries are open")
            action.setEnabled(False)
            return menu
        for candidate in self._candidates:
            menu.addAction(
                candidate.name, lambda e=candidate: self._append(CompositePiece(e))
            )
        return menu

    def _append(self, piece: CompositePiece) -> None:
        item = QTreeWidgetItem(self._list)
        item.setData(0, Qt.ItemDataRole.UserRole, piece.entry)
        item.setData(1, Qt.ItemDataRole.UserRole, piece)
        if piece.is_pad:
            spin = QSpinBox()
            spin.setRange(1, MAX_PAD_BYTES)
            spin.setSingleStep(max(1, self._tile_bytes))  # one tile a click
            spin.setValue(max(1, piece.extent))
            spin.setToolTip("How many blank bytes this run stands for")
            spin.valueChanged.connect(self._refresh)
            self._list.setItemWidget(item, 3, spin)
            item.setText(2, "(blank)")
        else:
            name = piece.entry.name if piece.entry is not None else "(missing)"
            # A range says so in the Source column, because the row is otherwise
            # indistinguishable from the whole entry and the difference is the
            # whole reason the run is where it is.
            if piece.is_ranged:
                last = piece.offset + piece.extent
                name += f"  [{format_hex(piece.offset)}\u2013{format_hex(last)}]"
            item.setText(2, name)
        self._list.setCurrentItem(item)
        self._refresh()

    def _items(self) -> list[QTreeWidgetItem]:
        root = self._list.invisibleRootItem()
        return [root.child(i) for i in range(root.childCount())]

    def _piece_of(self, item: QTreeWidgetItem) -> CompositePiece:
        """One row as a piece — the inverse of :meth:`_append`.

        A source row is carried through **whole**, range and measurement
        included: this dialog reads no bytes, so it is in no position to
        re-answer either, and rebuilding the piece from the columns would lose
        the range a project stated.

        A blank row is the one thing it does own — its spin *is* the answer.
        """
        entry = item.data(0, Qt.ItemDataRole.UserRole)
        if entry is None:
            spin = self._list.itemWidget(item, 3)
            return CompositePiece(length=spin.value() if spin is not None else 1)
        return item.data(1, Qt.ItemDataRole.UserRole)

    def pieces(self) -> tuple[CompositePiece, ...]:
        """The rows as pieces, in order — what OK returns."""
        return tuple(self._piece_of(item) for item in self._items())

    # -- reacting -----------------------------------------------------------
    def _refresh(self) -> None:
        """Re-run the two position columns and the notes under the list.

        Every row's position depends on every row before it, so this recomputes
        the whole column rather than patching one cell: a move or a removal
        changes the answer for everything after it, which is precisely the thing
        the columns exist to show.
        """
        at = 0
        for item in self._items():
            piece = self._piece_of(item)
            # Hex for the byte, because that is how an upload table is written;
            # decimal for the tile, because that is how a cell counts. A run that
            # does not land on a tile boundary says so with a "+", which is the
            # only warning the dialog can give without reading anything.
            item.setText(0, format_hex(at))
            tile, over = divmod(at, self._tile_bytes)
            item.setText(1, f"{tile}+" if over else str(tile))
            if not piece.is_pad:
                item.setText(3, format_hex(piece.extent))
            at += piece.extent
        tiles = at // self._tile_bytes
        self._total.setText(f"{format_hex(at)} bytes ({tiles} tiles)")
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        item = self._list.currentItem()
        rows = self._items()
        at = rows.index(item) if item in rows else -1
        self._up.setEnabled(at > 0)
        self._down.setEnabled(0 <= at < len(rows) - 1)
        self._remove.setEnabled(at >= 0)

    def _current_row(self) -> int:
        """Which row is selected, or -1 — the index every gesture below works in."""
        rows = self._items()
        item = self._list.currentItem()
        return rows.index(item) if item in rows else -1

    def _rebuild(self, pieces: list[CompositePiece], select: int) -> None:
        """Replace every row with ``pieces`` and land the selection on ``select``.

        Reordering and removal both go through this rather than shuffling
        :class:`QTreeWidgetItem`\\ s in place, because a blank row's spin box is a
        *widget attached to a position* — Qt does not carry it along when the item
        under it moves. Rebuilding makes the rows a plain function of the list,
        which is one behaviour to get right instead of one per gesture.

        **The scroll position is held still across it.** Every ``setCurrentItem``
        below asks the view to scroll that row into sight, and a rebuild makes one
        per row — so nudging a row one place with Move up threw the list to the
        end and back. What the gesture is for is comparing a row against its
        neighbours, which needs the list exactly where it was left.
        """
        scrolled = self._list.verticalScrollBar().value()
        self._list.clear()
        for piece in pieces:
            self._append(piece)
        rows = self._items()
        if rows:
            self._list.setCurrentItem(rows[min(max(select, 0), len(rows) - 1)])
        self._refresh()
        self._list.verticalScrollBar().setValue(scrolled)

    def _move(self, step: int) -> None:
        at = self._current_row()
        target = at + step
        pieces = list(self.pieces())
        if at < 0 or not 0 <= target < len(pieces):
            return
        pieces[at], pieces[target] = pieces[target], pieces[at]
        self._rebuild(pieces, target)

    def _remove_selected(self) -> None:
        at = self._current_row()
        if at < 0:
            return
        pieces = list(self.pieces())
        del pieces[at]
        self._rebuild(pieces, at)

    def _validate_and_accept(self) -> None:
        name = self._name.text().strip()
        if not name:
            self._error.setText("A composite needs a name — it has no file to borrow.")
            self._error.show()
            return
        self._params = CompositeParams(name, self.pieces())
        self.accept()

    @staticmethod
    def get_composite(
        parent: QWidget | None,
        *,
        entry: Entry,
        candidates: list[Entry],
        tile_bytes: int,
        name: str = "",
        pieces: tuple[CompositePiece, ...] = (),
        title: str = "New Composite View",
    ) -> CompositeParams | None:
        """Run the dialog modally; the validated parameters, or None on cancel."""
        dialog = CompositeDialog(
            entry=entry,
            candidates=candidates,
            tile_bytes=tile_bytes,
            name=name,
            pieces=pieces,
            title=title,
            parent=parent,
        )
        dialog.exec()
        return dialog._params
