"""The Font Alphabet window — a font sheet's tiles, and what each of them says.

A font sheet draws on the canvas as what it is: a grid of letter shapes. That
picture is correct and says nothing about the one fact a fontmap needs from it,
which is **which code draws which letter** — a fact that lives in the game's
code and in nobody's art. This window is where that is written down.

It is a `Qt.Tool` window on the text window's pattern
(:mod:`celpix.ui.text_window`): floats above the main window, takes no taskbar
slot, placed beside it on the first show and left where the user drags it after.
The two open together on a fontmap and are meant to be read together — the
alphabet is judged against the string, never on its own.

**One list, shown twice.** The tiles across the top and the table underneath are
the same run in two readings, and either can be clicked to select in the other.
The top is what the sheet looks like; the bottom is what it says.

**Two halves of the storage, one table on screen.** A row is written back to the
positional run when its code is inside the run, its role is text and its text is
one code point; anything else — a role, a pair standing behind one code, a code
past the end of the sheet — is written as a named code
(``docs/design/fontmap-entry.md`` §4). That split is a storage rule and not a
thing to make the user think about, so there is one table and no mode.

**Pasting a string fills down.** Select the row under the first tile, paste the
alphabet, and each code point lands on one consecutive code. It is the fastest
honest way to state a font sheet, and it is why there is no "characters" field
separate from the table. Newlines and tabs in the pasted text are skipped: they
are the layout of wherever the string was copied from, not glyphs.

**The bottom row is the run against the sheet, and the clipboard.** *Shift up* /
*Shift down* move the characters one tile along, which is the correction a paste
that started one tile out needs and is **not** the Base code spin one reading
over — that moves which codes the run occupies and leaves every character on the
tile it was typed against. *Copy alphabet* / *Paste alphabet* carry the whole
table in the ``20=A`` form a font table is kept in everywhere else, where the
table's own Ctrl+V fills characters down from a row.

**Presentation only.** The window holds a working copy of the three values that
make up a font alphabet — the origin, the run and the named codes — and emits
the whole triple whenever one of them moves. It never reads the model, never
encodes anything and owns no undo history of its own
(:mod:`celpix.ui.main_window.font_alphabet`).
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QGuiApplication, QImage, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QPushButton,
    QScrollArea,
    QSplitter,
    QStatusBar,
    QStyledItemDelegate,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from celpix.core.font import (
    HOLE,
    TEMPLATES,
    Glyph,
    GlyphRole,
    parse_table,
    spell_name,
)
from celpix.ui.tile_source_panel import TileSourcePanel
from celpix.ui.widgets import Badge, apply_badge, hex_spin
from celpix.ui.window_layout import WindowLayout

# The three columns, in the order a row is read: which code, what it says, and
# what kind of thing it is.
COL_CODE, COL_TEXT, COL_ROLE = 0, 1, 2

# How the roles are captioned. The enum's own spellings are the on-disk ones and
# read as jargon in a cell; these are what the column offers.
ROLE_LABELS: tuple[tuple[GlyphRole, str], ...] = (
    (GlyphRole.TEXT, "text"),
    (GlyphRole.BREAK, "line break"),
    (GlyphRole.CONTROL, "control"),
)


class _RoleDelegate(QStyledItemDelegate):
    """A three-way combo for the Role column.

    A delegate rather than a combo per row: a sheet is routinely a thousand
    tiles, and a thousand live widgets to offer a choice made on a handful of
    them is a window that takes a second to open.
    """

    def createEditor(self, parent, option, index):  # noqa: ANN001, ANN201, N802
        editor = QComboBox(parent)
        for role, label in ROLE_LABELS:
            editor.addItem(label, role.value)
        return editor

    def setEditorData(self, editor, index):  # noqa: ANN001, N802
        at = editor.findText(index.data() or "text")
        editor.setCurrentIndex(max(0, at))

    def setModelData(self, editor, model, index):  # noqa: ANN001, N802
        model.setData(index, editor.currentText())


class FontAlphabetWindow(QWidget):
    """Floating editor for one font's alphabet: its origin, run and named codes."""

    #: The whole alphabet after an edit — ``(base, chars, codes)`` — with whether
    #: it starts a new undo step and what to call that step.
    edited = Signal(int, str, tuple, bool, str)
    #: A tile was picked, by its ID, so the canvas and the dock can follow.
    tile_selected = Signal(int)
    #: Ctrl+Z / Ctrl+Y arrived here rather than at the main window, because a
    #: `Qt.Tool` window is the active one and window-context shortcuts follow it.
    undo_requested = Signal()
    redo_requested = Signal()
    #: The user shut the window from its own frame. Distinct from being hidden
    #: because the entry stopped being a fontmap, which is celPix's decision.
    dismissed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent, Qt.WindowType.Tool)
        self.setWindowTitle("Font Alphabet")
        self._positioned = False
        self._syncing = False
        # The working copy: what the file says, as three values. Held rather than
        # re-read because every edit is expressed as a change to one of them and
        # the window is what knows which row was touched.
        self._base = 0
        self._chars = ""
        self._codes: tuple[Glyph, ...] = ()
        self._ids: list[int] = []
        # Whether the next edit opens a new undo step. A run of typing down the
        # table is one step; a paste, a template, a shift or the origin moving
        # ends it, the same rule the text window's typing follows.
        self._fresh = True

        self._sheet = TileSourcePanel(self)
        self._sheet.tile_selected.connect(self._on_tile_selected)
        self._sheet.zoom_requested.connect(self._on_zoom)
        scroller = QScrollArea()
        scroller.setWidget(self._sheet)
        scroller.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._table = QTableWidget(0, 3)
        self._table.setHorizontalHeaderLabels(["Code", "Text", "Role"])
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setItemDelegateForColumn(COL_ROLE, _RoleDelegate(self._table))
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(COL_CODE, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COL_TEXT, QHeaderView.ResizeMode.Stretch)
        # Wide enough for the **combo** the delegate opens in it, not for the word
        # the cell shows: sized to contents the column fits "line break" exactly
        # and clips the dropdown arrow and frame that replace it on a click.
        # Measured off a real combo rather than guessed, since that chrome is the
        # style's and differs by platform.
        header.setSectionResizeMode(COL_ROLE, QHeaderView.ResizeMode.Fixed)
        self._table.setColumnWidth(COL_ROLE, _role_column_width())
        self._table.itemChanged.connect(self._on_item_changed)
        self._table.itemSelectionChanged.connect(self._on_row_selected)

        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(scroller)
        split.addWidget(self._table)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 0)
        layout.addLayout(self._build_header())
        layout.addWidget(split)
        layout.addLayout(self._build_actions())
        self._status = QStatusBar()
        self._status.setSizeGripEnabled(False)
        self._badge = QLabel()
        self._status.addPermanentWidget(self._badge)
        layout.addWidget(self._status)

        self.resize(520, 560)
        self._layout_memory = WindowLayout(self, "layout/font-alphabet-window")
        # A remembered position counts as already placed (the tool windows'
        # shared rule, :mod:`celpix.ui.decompress_overlay`).
        self._positioned = self._layout_memory.restore()

        paste = QShortcut(QKeySequence.StandardKey.Paste, self._table)
        paste.activated.connect(self._fill_down)
        for keys, signal in (
            (QKeySequence.StandardKey.Undo, self.undo_requested),
            (QKeySequence.StandardKey.Redo, self.redo_requested),
        ):
            shortcut = QShortcut(keys, self)
            shortcut.activated.connect(signal.emit)

    def _build_header(self) -> QHBoxLayout:
        """The origin, and the way to fill a table without typing it."""
        row = QHBoxLayout()
        row.setSpacing(6)
        # Where the run starts. Its own control and not a column of the table for
        # the reason it always was: the run's *shape* is legible off the sheet,
        # its *origin* is in the game's code and appears nowhere
        # (``docs/graphics-formats-reference/text-formats.md`` §3.2). So it is a
        # thing to dial against the text window next door, and moving it moves
        # every row at once.
        row.addWidget(QLabel("Base code"))
        self._base_spin = hex_spin(
            -0xFFFF,
            0xFFFF,
            "The code the first tile draws, shifting what the text\n"
            "says without touching what the cells draw\n"
            "Dial it when the text reads as near-words; when the\n"
            "picture does instead, it is Base tile that is off",
        )
        self._base_spin.valueChanged.connect(self._on_base_changed)
        row.addWidget(self._base_spin)
        row.addStretch(1)

        self._start_from = QPushButton("Start from...")
        self._start_from.setToolTip(
            "Fill the run with a common arrangement, as a first draft\n"
            "Both are guesses at the shape; the origin is still yours\n"
            "to dial afterwards"
        )
        menu = QMenu(self._start_from)
        for name, base, chars in TEMPLATES:
            menu.addAction(name).triggered.connect(
                lambda _checked=False, b=base, c=chars, n=name: self._apply_template(
                    b, c, n
                )
            )
        self._start_from.setMenu(menu)
        row.addWidget(self._start_from)

        return row

    def _build_actions(self) -> QHBoxLayout:
        """The bottom row: nudging the run against the sheet, and the clipboard.

        **Shift** is the correction a paste that started one tile out needs, and
        it is not the Base code spin one reading over: the spin moves which
        *codes* the run occupies and leaves every character on the tile it was
        typed against, while these move the characters **along the tiles** and
        leave the codes alone. The two look alike on the text and are told apart
        on the sheet, which is why they sit at opposite ends of the window.

        They move the run only. A named code was read out of the stream at the
        value it has, so it no more follows a nudge than it follows the origin.
        """
        row = QHBoxLayout()
        row.setSpacing(6)
        self._shift_up = QPushButton("Shift up")
        self._shift_up.setToolTip(
            "Move every character one tile earlier\n"
            "For a run pasted one tile too late; the character\n"
            "on the first tile falls off"
        )
        self._shift_up.clicked.connect(lambda: self._shift(-1))
        row.addWidget(self._shift_up)

        self._shift_down = QPushButton("Shift down")
        self._shift_down.setToolTip(
            "Move every character one tile later\n"
            "For a run pasted one tile too early; the first tile\n"
            "is left spelling nothing"
        )
        self._shift_down.clicked.connect(lambda: self._shift(1))
        row.addWidget(self._shift_down)
        row.addStretch(1)

        self._copy = QPushButton("Copy alphabet")
        self._copy.setToolTip(
            "Put the whole table on the clipboard, one 20=A line\n"
            "per code - the form a font table is kept in\n"
            "Unlike Ctrl+C in the table, which copies cells"
        )
        self._copy.clicked.connect(self._copy_alphabet)
        row.addWidget(self._copy)

        self._paste = QPushButton("Paste alphabet")
        self._paste.setToolTip(
            "Replace the whole table from 20=A lines on the\n"
            "clipboard, as Copy alphabet writes them\n"
            "Unlike Ctrl+V in the table, which fills characters\n"
            "down from the selected row"
        )
        self._paste.clicked.connect(self._paste_alphabet)
        row.addWidget(self._paste)
        return row

    # -- presentation ------------------------------------------------------
    def set_sheet(
        self,
        sheet: QImage,
        ids: list[int],
        cell_px: tuple[int, int],
        columns: int,
    ) -> None:
        """Show the font's tiles. Its own call because it is the expensive half.

        A bank decoded, laid out and rasterized — and none of it moves when a
        character is typed into the table below, so an edit refreshes the table
        alone and leaves this standing (``main_window/font_alphabet.py``).
        """
        self._ids = list(ids)
        self._sheet.set_sheet(sheet, ids, cell_px, columns)

    def show_alphabet(
        self,
        title: str,
        base: int,
        chars: str,
        codes: tuple[Glyph, ...],
    ) -> None:
        """Present one font's alphabet, showing the window."""
        self._syncing = True
        try:
            self.setWindowTitle(f"Font Alphabet - {title}")
            self._base, self._chars, self._codes = base, chars, codes
            self._base_spin.setValue(base)
            self._rebuild()
        finally:
            self._syncing = False
        if not self.isVisible():
            if not self._positioned and self.parentWidget() is not None:
                anchor = self.parentWidget().frameGeometry().bottomRight()
                self.move(anchor + QPoint(12, -560))
                self._positioned = True
            self.show()

    def hide_overlay(self) -> None:
        """Hide — the entry on screen has no font alphabet, or was closed."""
        if self.isVisible():
            self.hide()

    def set_status(self, status: str, badge: Badge | None = None) -> None:
        self._status.showMessage(status)
        apply_badge(self._badge, badge)

    def select_tile(self, tile_id: int) -> None:
        """Mark the tile the canvas's selected cell names."""
        self._sheet.set_marked_id(tile_id)

    # -- the two readings --------------------------------------------------
    def _merged(self) -> dict[int, Glyph]:
        """Every code that says something, named codes winning over the run.

        The merge the pipeline performs, done here so the table shows what the
        text will. Built once per redraw and handed down rather than asked per
        row: a sheet is routinely a thousand tiles and a font a hundred named
        codes, and asking each row to scan the list made a keystroke quadratic.
        """
        merged = {
            self._base + at: Glyph(self._base + at, char)
            for at, char in enumerate(self._chars)
            if char != HOLE
        }
        merged.update({glyph.code: glyph for glyph in self._codes})
        return merged

    def _rows(self) -> list[int]:
        """Every code the table lists: one per tile, plus named codes past them.

        Bounded to the sheet because the alternative is 65 536 rows on a two-byte
        stream. A named code outside it is still reachable — it is appended, in
        order — since a code the sheet cannot draw is exactly the kind that gets
        named.
        """
        run = [self._base + at for at in range(len(self._ids))]
        span = set(run)
        return run + sorted(g.code for g in self._codes if g.code not in span)

    def _rebuild(self) -> None:
        """Redraw both readings from the working copy.

        Items are **reused** where the row set has not moved, which is every
        redraw but the ones that follow the origin or a new sheet. Replacing
        three thousand `QTableWidgetItem`s per keystroke is what made typing here
        feel slow; setting text on the ones already there does not.
        """
        codes = self._rows()
        merged = self._merged()
        self._table.blockSignals(True)
        try:
            fresh = self._table.rowCount() != len(codes)
            if fresh:
                self._table.setRowCount(len(codes))
            for row, code in enumerate(codes):
                glyph = merged.get(code)
                text = glyph.text if glyph else ""
                label = _label_of(glyph.role if glyph else GlyphRole.TEXT)
                name = self._table.item(row, COL_CODE)
                if name is None or name.data(Qt.ItemDataRole.UserRole) != code:
                    name = QTableWidgetItem(f"${code:02X}")
                    name.setFlags(
                        Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
                    )
                    name.setData(Qt.ItemDataRole.UserRole, code)
                    self._table.setItem(row, COL_CODE, name)
                _put(self._table, row, COL_TEXT, text)
                _put(self._table, row, COL_ROLE, label)
        finally:
            self._table.blockSignals(False)
        self._sheet.set_labels(
            {
                tile: (glyph.text if (glyph := merged.get(self._base + at)) else "")
                for at, tile in enumerate(self._ids)
            }
        )

    # -- editing -----------------------------------------------------------
    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        """One cell typed: settle it into the run or into the named codes."""
        if self._syncing:
            return
        row = item.row()
        code_item = self._table.item(row, COL_CODE)
        if code_item is None:
            return
        code = int(code_item.data(Qt.ItemDataRole.UserRole))
        text_item = self._table.item(row, COL_TEXT)
        role_item = self._table.item(row, COL_ROLE)
        text = text_item.text() if text_item else ""
        role = _role_of(role_item.text() if role_item else "")
        self._write(code, text, role)
        self._emit("edit font alphabet")

    def _write(self, code: int, text: str, role: GlyphRole) -> None:
        """Land one code's answer in whichever half of the storage holds it.

        The run takes it when it can — inside the sheet, one code point, no role
        — because that is the half a tile's position states, and keeping it there
        means the run stays the thing the sheet is a picture of. Everything else
        is a named code, and either way the other half is cleared of that code so
        the two can never disagree.

        **More than one character names the code.** A tile draws one character,
        so a longer answer is not what the tile *says* — it is what the code is
        *for*, and the string shows it as ``[wait]`` and types straight back
        (``docs/design/fontmap-entry.md`` §5). Done here rather than left to the
        Role column so the rule holds however the row was reached, including a
        paste; the column is then how a one-character code is made a command, and
        how a break is picked out from the rest.
        """
        at = code - self._base
        in_run = 0 <= at < len(self._ids)
        if len(text) > 1 and role is GlyphRole.TEXT:
            role = GlyphRole.CONTROL
        if not role.spells:
            # A name is what goes inside the brackets, so it is one word by the
            # time it is stored rather than at the moment it is read back.
            text = spell_name(text)
        positional = in_run and role is GlyphRole.TEXT and len(text) == 1
        if positional:
            self._set_char(at, text)
        elif in_run:
            self._set_char(at, HOLE)
        self._codes = tuple(g for g in self._codes if g.code != code)
        if text and not positional:
            self._codes = tuple(
                sorted([*self._codes, Glyph(code, text, role)], key=lambda g: g.code)
            )

    def _set_char(self, at: int, char: str) -> None:
        """Put ``char`` at slot ``at``, padding the run out with holes to reach it."""
        run = self._chars.ljust(at + 1, HOLE)
        self._chars = run[:at] + char + run[at + 1 :]

    def _fill_down(self) -> None:
        """Fill down from the selected row, one code point per code.

        The gesture the whole window is arranged around: a font sheet is a run of
        letters in order, and the run is the thing a user already has in a
        clipboard. Newlines and tabs are skipped rather than written, since they
        describe the shape of whatever the text was copied out of.

        One undo step, because it is one gesture — and stopping at the last row
        rather than growing the table, since a code past the sheet is a code no
        tile draws and would be a glyph nobody could see.
        """
        typed = QGuiApplication.clipboard().text()
        chars = [c for c in typed if c not in "\r\n\t"]
        rows = self._table.selectionModel().selectedRows()
        first = rows[0].row() if rows else 0
        codes = self._rows()
        landed = 0
        for at, char in enumerate(chars):
            row = first + at
            if row >= len(codes):
                break
            self._write(codes[row], char, GlyphRole.TEXT)
            landed += 1
        if not landed:
            return
        self._fresh = True
        self._rebuild()
        self._emit(f"paste {landed} character{'s' if landed != 1 else ''}")
        self._fresh = True
        lost = len(chars) - landed
        self.set_status(
            f"{landed} filled in",
            Badge(
                f"{lost} dropped",
                "The paste ran past the last tile of the sheet.\n"
                "A code no tile draws would be a glyph nobody\n"
                "can see, so those characters were not written.",
                warning=True,
            )
            if lost
            else None,
        )

    def _apply_template(self, base: int, chars: str, name: str) -> None:
        """Replace the run wholesale with one of the shipped arrangements.

        The run only. Named codes are the user's own reading of the stream and
        have nothing to do with which letters the sheet draws, so a first draft
        of the second must not take the first away.
        """
        self._base, self._chars = base, chars
        self._syncing = True
        try:
            self._base_spin.setValue(base)
        finally:
            self._syncing = False
        self._fresh = True
        self._rebuild()
        self._emit(f"start from {name}")
        self._fresh = True

    def _shift(self, by: int) -> None:
        """Move the run ``by`` tiles along the sheet, one gesture, one step.

        The **characters** move and the codes do not, which is what makes this a
        different control from Base code rather than a second spelling of it: a
        run pasted one tile out is corrected here, a run whose *origin* is out is
        corrected there, and the sheet is what tells the two apart.

        Shifting up drops the first character off the top rather than wrapping
        it round to the bottom: a run and a ring are not the same thing, and the
        character that falls off is the one the user can see fall off.
        """
        if not self._chars:
            return
        self._chars = (HOLE * by + self._chars if by > 0 else self._chars[-by:]).rstrip(
            HOLE
        )
        self._fresh = True
        self._rebuild()
        self._emit("shift the run down" if by > 0 else "shift the run up")
        self._fresh = True

    def _copy_alphabet(self) -> None:
        """Put the whole table on the clipboard as ``20=A`` lines.

        The form a font table is kept in everywhere outside celPix
        (``docs/graphics-formats-reference/text-formats.md`` §3.3), so what comes
        out of here pastes into a disassembly and back again. Named codes are
        written in the bracketed form the same parser reads.
        """
        merged = self._merged()
        lines = []
        for code in sorted(merged):
            glyph = merged[code]
            spelling = glyph.text if glyph.spells else f"[{glyph.text}]"
            lines.append(f"{code:02X}={spelling}")
        QGuiApplication.clipboard().setText("\n".join(lines) + "\n")
        self.set_status(f"{len(lines)} codes copied.")

    def _paste_alphabet(self) -> None:
        """Replace the whole table from ``20=A`` lines on the clipboard.

        **Replaces**, where the table's own Ctrl+V fills down: this is a table
        arriving from somewhere else and it is the whole answer, so leaving the
        old one underneath would merge two fonts into a third that is neither.

        The codes are read as they are written, so what lands is the run this
        sheet already numbers plus named codes for anything outside it — and the
        origin is left alone, because the pasted codes are absolute and moving
        them would be answering a question the table just answered.
        """
        glyphs = parse_table(QGuiApplication.clipboard().text())
        if not glyphs:
            self.set_status(
                "Nothing to paste.",
                Badge(
                    "no table",
                    "The clipboard holds no 20=A lines. Copy alphabet\n"
                    "writes them in the form this reads.",
                    warning=True,
                ),
            )
            return
        span = range(self._base, self._base + len(self._ids))
        run = [HOLE] * len(self._ids)
        named: list[Glyph] = []
        for glyph in glyphs:
            if glyph.code in span and glyph.spells and len(glyph.text) == 1:
                run[glyph.code - self._base] = glyph.text
            else:
                named.append(glyph)
        self._chars = "".join(run).rstrip(HOLE)
        self._codes = tuple(sorted(named, key=lambda g: g.code))
        self._fresh = True
        self._rebuild()
        self._emit(f"paste {len(glyphs)} codes")
        self._fresh = True
        self.set_status(f"{len(glyphs)} codes pasted.")

    def _on_base_changed(self, value: int) -> None:
        """Slide the run along the code space — its own step per settled value.

        ``keyboardTracking`` is off on this spin (:func:`~celpix.ui.widgets.
        hex_spin`), so holding the arrow key reports once at the end and the undo
        stack gets the gesture instead of the path it took.
        """
        if self._syncing or value == self._base:
            return
        self._base = value
        self._fresh = True
        self._rebuild()
        self._emit(f"set base code to ${value:X}")
        self._fresh = True

    def _emit(self, label: str) -> None:
        self.edited.emit(self._base, self._chars, self._codes, self._fresh, label)
        self._fresh = False

    # -- the two readings following each other -----------------------------
    def _on_tile_selected(self, tile_id: int) -> None:
        if tile_id in self._ids:
            self._select_row(self._ids.index(tile_id))
        self.tile_selected.emit(tile_id)

    def _on_row_selected(self) -> None:
        rows = self._table.selectionModel().selectedRows()
        if self._syncing or not rows:
            return
        row = rows[0].row()
        if row < len(self._ids):
            self._sheet.select_id(self._ids[row])

    def _select_row(self, row: int) -> None:
        self._syncing = True
        try:
            self._table.selectRow(row)
            self._table.scrollToItem(self._table.item(row, COL_TEXT))
        finally:
            self._syncing = False

    def _on_zoom(self, steps: int, _at: object) -> None:
        self._sheet.set_zoom(max(1, min(8, self._sheet_zoom() + steps)))

    def _sheet_zoom(self) -> int:
        return getattr(self._sheet, "_zoom", 2)

    def closeEvent(self, event) -> None:  # noqa: ANN001 — QCloseEvent
        """Closing says the user does not want this window, which is reported.

        :meth:`hide_overlay` is celPix putting it away and means nothing of the
        sort, which is why only this one speaks up.
        """
        self.dismissed.emit()
        super().closeEvent(event)


def _role_column_width() -> int:
    """How wide the Role column has to be to hold its editor, plus a little air.

    Measured off a combo with **no parent**, which is dropped on return: parented
    to the table it would outlive the measurement as a stray child, and every
    input in this window is expected to carry a tooltip.
    """
    probe = QComboBox()
    for _role, label in ROLE_LABELS:
        probe.addItem(label)
    return probe.sizeHint().width() + 12


def _put(table: QTableWidget, row: int, column: int, text: str) -> None:
    """Set one editable cell's text, reusing the item already there."""
    item = table.item(row, column)
    if item is None:
        table.setItem(row, column, QTableWidgetItem(text))
    elif item.text() != text:
        item.setText(text)


def _label_of(role: GlyphRole) -> str:
    return next(label for value, label in ROLE_LABELS if value is role)


def _role_of(label: str) -> GlyphRole:
    return next(
        (value for value, caption in ROLE_LABELS if caption == label), GlyphRole.TEXT
    )
