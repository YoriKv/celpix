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
The top is what the sheet looks like; the bottom is what it says. A selection is
one thing across both, however wide: picking a stretch of rows outlines that
stretch of tiles, so what the clipboard buttons are about to act on is visible as
a shape on the sheet and not only as a band of highlighted rows.

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
tile it was typed against. *Copy alphabet* / *Paste alphabet* carry the table in
the ``20=A`` form a font table is kept in everywhere else — and the paste also
takes a plain string of characters, one per code, since that is the other form a
font is quoted in. Both act on the **selection**: the picked rows when there are
several, that row to the end when there is one. The table's own Ctrl+V is the
different gesture, filling characters down from a row without clearing anything.

**Presentation only.** The window holds a working copy of the three values that
make up a font alphabet — the origin, the run and the named codes — and emits
the whole triple whenever one of them moves. It never reads the model, never
encodes anything and owns no undo history of its own
(:mod:`celpix.ui.main_window.font_alphabet`).
"""

from __future__ import annotations

from collections.abc import Iterable
from itertools import count

from PySide6.QtCore import QEvent, QPoint, Qt, Signal
from PySide6.QtGui import QGuiApplication, QImage, QKeyEvent, QKeySequence
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

# What an unnamed break is called when the Role column makes it one. A break has
# to read as *something* — a name is how the text spells the code back
# (``docs/design/fontmap-entry.md`` §5) — and there is only one word anyone wants
# for it, so it is written rather than demanded.
BREAK_NAME = "br"


class _RoleDelegate(QStyledItemDelegate):
    """A three-way combo for the Role column.

    A delegate rather than a combo per row: a sheet is routinely a thousand
    tiles, and a thousand live widgets to offer a choice made on a handful of
    them is a window that takes a second to open.
    """

    def createEditor(self, parent, option, index) -> QComboBox:  # noqa: ANN001, N802
        editor = QComboBox(parent)
        for role, label in ROLE_LABELS:
            editor.addItem(label, role.value)
        return editor

    def setEditorData(self, editor: QComboBox, index) -> None:  # noqa: ANN001, N802
        at = editor.findText(index.data() or "text")
        editor.setCurrentIndex(max(0, at))

    def setModelData(self, editor: QComboBox, model, index) -> None:  # noqa: ANN001, N802
        model.setData(index, editor.currentText())


def _claim_override(event: QEvent) -> bool:
    """Take a ``ShortcutOverride`` for a key this window answers itself.

    A `Qt.Tool` window is a top-level of its own, but Qt keeps the **parent's**
    window shortcuts alive while it is active — that is what lets the menu bar's
    keys work from a floating window. So Ctrl+Z here is claimed by two things at
    once, the main window's Undo action and whatever this window binds, and Qt's
    answer to a tie is to fire *neither*: the key does nothing at all until the
    user clicks back onto the main window.

    Accepting the override is the way out. It says the focused widget wants the
    key as an ordinary keystroke, so no shortcut runs anywhere and the press
    arrives at :meth:`FontAlphabetWindow.keyPressEvent` to be answered once. A
    live cell editor still wins: `QLineEdit` accepts the override first, which is
    why Ctrl+Z inside a half-typed cell is still that cell's own undo.
    """
    return isinstance(event, QKeyEvent) and any(
        event.matches(keys)
        for keys in (
            QKeySequence.StandardKey.Undo,
            QKeySequence.StandardKey.Redo,
            QKeySequence.StandardKey.Paste,
        )
    )


class _AlphabetTable(QTableWidget):
    """The table, with Enter as a second spelling of double-clicking Text.

    Qt's own edit key is F2 (``EditKeyPressed``), which nobody reaches for on a
    row they have just picked off the sheet: the gesture here is click a tile,
    press Enter, type the letter. Enter always opens the **Text** cell whatever
    column the cursor is in, since that is the one a row exists to answer — the
    code is fixed by the tile and the role is the exception.

    Only the closed table sees this key. Once the editor is open it is a child
    widget with the focus, so Enter reaches the delegate and commits, and typing
    never toggles the editor off and on.
    """

    def keyPressEvent(self, event) -> None:  # noqa: ANN001 — QKeyEvent
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            item = self.item(self.currentRow(), COL_TEXT)
            if item is not None:
                self.setCurrentItem(item)
                self.editItem(item)
                event.accept()
                return
        super().keyPressEvent(event)


class FontAlphabetWindow(QWidget):
    """Floating editor for one font's alphabet: its origin, run and named codes."""

    #: The whole alphabet after an edit — ``(base, chars, codes)`` — with whether
    #: it starts a new undo step and what to call that step.
    edited = Signal(int, str, tuple, bool, str)
    #: A tile was picked, by its ID, so the canvas and the dock can follow.
    tile_selected = Signal(int)
    #: Ctrl+Z / Ctrl+Y arrived here rather than at the main window, because this
    #: window is the active one and answers the key itself (:func:`_claim_override`).
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

        self._table = _AlphabetTable(0, 3)
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

    # -- the keys this window answers itself -------------------------------
    def event(self, event) -> bool:  # noqa: ANN001 — QEvent
        """Claim Ctrl+Z / Ctrl+Y / Ctrl+V off the main window's actions.

        The override arrives here after the focused widget has passed on it, so
        an open cell editor keeps its own undo and paste and only the window's
        own keys are taken. Why an override rather than a `QShortcut`:
        :func:`_claim_override`.
        """
        if event.type() == QEvent.Type.ShortcutOverride and _claim_override(event):
            event.accept()
            return True
        return super().event(event)

    def keyPressEvent(self, event) -> None:  # noqa: ANN001 — QKeyEvent
        """Answer the three claimed keys, once the widgets under them have not.

        Undo and redo are the session's, not this window's: it owns no history,
        so they are passed up and land on the one stack every edit shares
        (``docs/design/undo-redo.md``). Paste is the table's fill-down, and it is
        answered here rather than on the table so that it works from the sheet
        and the buttons too — the row it starts at is the selected one either way.
        """
        if event.matches(QKeySequence.StandardKey.Undo):
            self.undo_requested.emit()
        elif event.matches(QKeySequence.StandardKey.Redo):
            self.redo_requested.emit()
        elif event.matches(QKeySequence.StandardKey.Paste):
            self._fill_down()
        else:
            super().keyPressEvent(event)
            return
        event.accept()

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
            "Fill the run with a common arrangement, as a first draft"
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
            "The character on the first tile falls off"
        )
        self._shift_up.clicked.connect(lambda: self._shift(-1))
        row.addWidget(self._shift_up)

        self._shift_down = QPushButton("Shift down")
        self._shift_down.setToolTip(
            "Move every character one tile later\n"
            "The first tile is left spelling nothing"
        )
        self._shift_down.clicked.connect(lambda: self._shift(1))
        row.addWidget(self._shift_down)
        row.addStretch(1)

        self._copy = QPushButton("Copy alphabet")
        self._copy.setToolTip(
            "Put the table on the clipboard, one 20=A line per code\n"
            "Selected rows only, or from the selected row to the\n"
            "end when one is picked\n"
            "Ctrl+C in the table copies cells instead"
        )
        self._copy.clicked.connect(self._copy_alphabet)
        row.addWidget(self._copy)

        self._paste = QPushButton("Paste alphabet")
        self._paste.setToolTip(
            "Replace the table from the clipboard: 20=A lines, or\n"
            "a plain string of characters, one per code\n"
            "Into the selected rows only, or from the selected row\n"
            "to the end when one is picked\n"
            "Ctrl+V in the table fills characters down instead"
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
        """Pick the tile the canvas's selected cell names, as a click would.

        A **selection** in both readings and not a mark of its own: the row the
        clipboard buttons act on and Enter opens to type into, and the picked
        tile on the sheet above it. Clicking a glyph in the string and clicking
        it on the sheet are the same question asked from two places, so they
        must not leave the window in two different states.

        Ignored for a tile the sheet does not show, which is a cell naming a
        code past the bank the binding reaches.
        """
        if tile_id not in self._ids:
            return
        self._select_row(self._ids.index(tile_id))
        # Guarded, so the sheet's own report of the pick does not answer this
        # with `selectRow` — the reason a click on the sheet is the only pick
        # that moves the table (:meth:`_on_tile_selected`).
        self._syncing = True
        try:
            self._sheet.select_id(tile_id)
        finally:
            self._syncing = False

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
                    # Everything a cell ordinarily is, minus typing into it: the
                    # code is the tile's position and the only way to move it is
                    # the Base code spin.
                    name.setFlags(name.flags() & ~Qt.ItemFlag.ItemIsEditable)
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
        """One cell typed or picked: settle it into the run or the named codes.

        **A role is its own step**, where a run of typing down the Text column is
        one. It is a pick from a list rather than a keystroke, and it says
        something different about the code than the letters around it did — so it
        ends the run on both sides, the rule a paste and a template follow.

        **A role needs something to be the role of.** What a non-text code reads
        as is its *name*, and a glyph with no text is not a thing the model can
        hold at all — so the pick is put back and the row says what it wants
        first, rather than being silently dropped on the next redraw.
        """
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
        picked = item.column() == COL_ROLE
        if picked and not text and role is GlyphRole.BREAK:
            # The one role that names itself: a format's second and third break
            # (one that scrolls, one that does not) are told apart by the code
            # beside them rather than by what they are called, so a number is a
            # better answer than a prompt. Only a *control* still has to be said
            # out loud - what it does is the whole of what its name carries.
            text = _fresh_break_name(self._merged().values())
        if picked and not text:
            self._rebuild()
            self.set_status(
                f"${code:02X} spells nothing.",
                Badge(
                    "no text",
                    "A line break or a control reads as its name.\n"
                    "Write one in the Text column first.",
                    warning=True,
                ),
            )
            return
        self._write(code, text, role)
        if picked:
            # A name written *for* the user has to appear in the column it was
            # written into - the round trip through the undo stack redraws the
            # table only once the edit has been applied to the entry.
            self._rebuild()
            self._fresh = True
        self._emit(
            f"set ${code:02X} to {_label_of(role)}" if picked else "edit font alphabet"
        )
        if picked:
            self._fresh = True

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
                "The paste ran past the last tile of the sheet,\n"
                "so those characters were not written.",
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

    def _span(self) -> tuple[list[int], bool]:
        """Which codes the two clipboard buttons act on, and whether that is all.

        The table is long and the answer is usually a stretch of it, so the
        selection scopes both: **several rows picked** is exactly those rows and
        nothing else, and **one row** — or none — is that row to the end, which
        is what makes "the whole table" still the ordinary case rather than a
        separate mode.

        The second value says whether the far end is closed. An open span still
        takes a pasted code the sheet does not number, since a code past the
        tiles is the kind that gets named; a closed one is the user pointing at
        rows, and a code outside them is not one of the rows they pointed at.
        """
        rows = self._rows()
        picked = sorted(
            index.row() for index in self._table.selectionModel().selectedRows()
        )
        if len(picked) > 1:
            return [rows[at] for at in picked if at < len(rows)], True
        return rows[picked[0] if picked else 0 :], False

    def _copy_alphabet(self) -> None:
        """Put the table on the clipboard as ``20=A`` lines, the selection's part.

        The form a font table is kept in everywhere outside celPix
        (``docs/graphics-formats-reference/text-formats.md`` §3.3), so what comes
        out of here pastes into a disassembly and back again. Named codes are
        written in the bracketed form the same parser reads.

        Codes that say nothing are left out rather than written as blanks: what
        a run has not reached is not a glyph spelling the empty string.
        """
        merged = self._merged()
        codes, _bounded = self._span()
        lines = []
        for code in sorted(code for code in codes if code in merged):
            glyph = merged[code]
            spelling = glyph.text if glyph.spells else f"[{glyph.text}]"
            lines.append(f"{code:02X}={spelling}\n")
        QGuiApplication.clipboard().setText("".join(lines))
        self.set_status(f"{len(lines)} codes copied.")

    def _paste_alphabet(self) -> None:
        """Replace the selection's codes from either form on the clipboard.

        **Two forms, told apart by whether the first reads.** ``20=A`` lines
        state their own codes, so they land where they say and the origin is left
        alone — moving it would answer a question the table just answered. A
        plain **string of characters** states none, so it lands one character per
        code down the span, which is the form a font is quoted in when it is
        quoted at all: a row of letters read off the sheet.

        **Replaces**, where the table's own Ctrl+V fills down: a table arriving
        from somewhere else is the whole answer for the codes it covers, so the
        span is cleared first and leftover codes inside it come out blank. The
        span is the selection (:meth:`_span`), which is what keeps that from
        meaning the whole font every time.

        A paste that lands nothing changes nothing — a table whose codes all fall
        outside the picked rows is a paste aimed at the wrong place, and wiping
        the rows would be the one reading of it nobody wants.
        """
        typed = QGuiApplication.clipboard().text()
        glyphs = parse_table(typed)
        chars = [] if glyphs else [c for c in typed if c not in "\r\n\t"]
        if not glyphs and not chars:
            self.set_status(
                "Nothing to paste.",
                Badge(
                    "empty",
                    "The clipboard holds neither 20=A lines nor\n"
                    "characters to spell the codes with.",
                    warning=True,
                ),
            )
            return
        codes, bounded = self._span()
        span = set(codes)
        # Everything outside the span is kept exactly as it stands; inside it the
        # old answers go before the new ones land, which is what makes this a
        # replace and not a merge.
        run = list(self._chars.ljust(len(self._ids), HOLE))
        for code in span:
            at = code - self._base
            if 0 <= at < len(run):
                run[at] = HOLE
        named = [glyph for glyph in self._codes if glyph.code not in span]

        def place(code: int, text: str, role: GlyphRole = GlyphRole.TEXT) -> None:
            at = code - self._base
            if role is GlyphRole.TEXT and len(text) == 1 and 0 <= at < len(self._ids):
                run[at] = text
            else:
                named.append(Glyph(code, text, role))

        landed = 0
        if glyphs:
            rows = set(self._rows())
            for glyph in glyphs:
                if glyph.code in span or (not bounded and glyph.code not in rows):
                    place(glyph.code, glyph.text, glyph.role)
                    landed += 1
            lost, outside = 0, len(glyphs) - landed
        else:
            for code, char in zip(codes, chars):
                place(code, char)
                landed += 1
            lost, outside = len(chars) - landed, 0
        if not landed:
            self.set_status(
                "Nothing pasted.",
                Badge(
                    "outside",
                    "Every code on the clipboard falls outside the\n"
                    "selected rows, so none of them was written.",
                    warning=True,
                ),
            )
            return
        self._chars = "".join(run).rstrip(HOLE)
        self._codes = tuple(sorted(named, key=lambda g: g.code))
        self._fresh = True
        self._rebuild()
        self._emit(f"paste {landed} code{'s' if landed != 1 else ''}")
        self._fresh = True
        dropped = lost or outside
        self.set_status(
            f"{landed} codes pasted.",
            Badge(
                f"{dropped} dropped",
                "The paste ran past the last row of the selection,\n"
                "so those characters were not written."
                if lost
                else "Those codes fall outside the selected rows,\n"
                "so they were not written.",
                warning=True,
            )
            if dropped
            else None,
        )

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
        # Only a pick made **on the sheet** moves the table. The sheet reports a
        # selection it was handed the same way it reports a click, so following
        # this one back would answer a row selection with `selectRow` — which
        # takes a stretch of picked rows down to the one that pushed the tile,
        # and the clipboard buttons read that stretch (:meth:`_span`).
        if not self._syncing and tile_id in self._ids:
            self._select_row(self._ids.index(tile_id))
        self.tile_selected.emit(tile_id)

    def _on_row_selected(self) -> None:
        """Show the picked rows on the sheet — all of them, not just the first.

        The two readings are one list, so a stretch of rows is a stretch of
        tiles: the rows the clipboard buttons act on (:meth:`_span`) are the
        tiles ringed above them, and there is never a moment where the window
        says one thing at the top and another at the bottom. Rows past the last
        tile — named codes the sheet cannot draw — carry no tile and are simply
        not in the answer.
        """
        rows = self._table.selectionModel().selectedRows()
        if self._syncing or not rows:
            return
        picked = [
            self._ids[index.row()]
            for index in sorted(rows, key=lambda index: index.row())
            if index.row() < len(self._ids)
        ]
        if not picked:
            return
        self._syncing = True
        try:
            self._sheet.select_ids(picked)
        finally:
            self._syncing = False

    def _select_row(self, row: int) -> None:
        self._syncing = True
        try:
            self._table.selectRow(row)
            item = self._table.item(row, COL_TEXT)
            if item is not None:
                self._table.scrollToItem(item)
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


def _fresh_break_name(glyphs: Iterable[Glyph]) -> str:
    """``br``, or ``br-1``, ``br-2``… where codes already spell that.

    Numbered rather than shared: a name is what the text writes the code as and
    what the user types to put it back, so two codes answering to one name would
    leave whichever came second unreachable — the reader takes the first
    (:class:`~celpix.core.font.Font`).
    """
    taken = {glyph.text for glyph in glyphs}
    if BREAK_NAME not in taken:
        return BREAK_NAME
    return next(name for n in count(1) if (name := f"{BREAK_NAME}-{n}") not in taken)


def _label_of(role: GlyphRole) -> str:
    return next(label for value, label in ROLE_LABELS if value is role)


def _role_of(label: str) -> GlyphRole:
    return next(
        (value for value, caption in ROLE_LABELS if caption == label), GlyphRole.TEXT
    )
