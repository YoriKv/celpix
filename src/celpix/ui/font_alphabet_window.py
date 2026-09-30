"""The Font Alphabet window — a font sheet's tiles, and what each of them says.

A font sheet draws on the canvas as what it is: a grid of letter shapes. That
picture is correct and says nothing about the one fact a fontmap needs from it,
which is **which code draws which letter** — a fact that lives in the game's
code and in nobody's art. This window is where that is written down.

It is a floating tool window (:class:`~celpix.ui.tool_window.ToolWindow`), like
the text window (:mod:`celpix.ui.text_window`): floats above the main window,
takes no taskbar slot, placed beside it on the first show and left where the
user drags it after.
The two open together on a fontmap and are meant to be read together — the
alphabet is judged against the string, never on its own.

**One list, shown twice.** The tiles across the top and the table underneath are
the same run in two readings, and either can be clicked to select in the other.
The top is what the sheet looks like; the bottom is what it says. A selection is
one thing across both, however wide: picking a stretch of rows outlines that
stretch of tiles, so what the clipboard buttons are about to act on is visible as
a shape on the sheet and not only as a band of highlighted rows. Each tile is
captioned with what it says, that being the reading the picture cannot give;
**Characters** takes the captions off for the moment the letter shapes
themselves are what is being judged.

**The sheet magnifies and pans like every other one.** Ctrl+wheel zooms it and a
space-drag moves it, over the grey around it as much as over the tiles
(:class:`~celpix.ui.widgets.PanZoomSurface`) — a font sheet is small, and a user
who has just magnified one past its half of the splitter wants it moved rather
than resized. Space is the window's while the table is not being typed into: the
cell editor and the Role combo keep it, a font's own space glyph being a
character somebody has to be able to type.

**Two halves of the storage, one table on screen.** A row is written back to the
positional run when its code is inside the run, its role is text and its text is
one code point; anything else — a role, a pair standing behind one code, a code
past the end of the run — is written as a named code
(``docs/design/fontmap-entry.md`` §4). That split is a storage rule and not a
thing to make the user think about, so there is one table and no mode.

**Several characters typed into Text are read as a command's name**, because
that is what the common one is: ``wait``, ``end``, a code worth a caption. It is
a *guess*, and the Role column is where it is corrected — picking **dict** on
such a row keeps the spelling as a spelling, which is a code standing for a
**pair**: ``th`` behind one byte, the one compression trick a fixed-size text
region actually has (``docs/design/fontmap-entry.md`` §4). A row that already
reads *dict* is not guessed at again, so its text can be corrected without the
role falling back out from under it.

**text and dict are one column entry, not two.** A code spells one character or
it spells several, and the row says which it is doing rather than being asked:
whichever of the two is picked, the spelling settles it. What that buys is
downstream — a dictionary code past the end of the sheet has no tile of its own,
and the fontmap draws it as the characters it stands for, which is a question
only a role that never lies can be asked (``docs/design/fontmap-entry.md`` §5).

**A command may say how many cells it swallows**, written beside its name in the
same cell: ``speed, 1`` for a code whose argument is the cell after it, which the
string then reads as ``[speed, $00]`` instead of a command followed by a letter
that is not a letter (``docs/design/fontmap-entry.md`` §5). One cell rather than
a fourth column, because that is how the count is written in every other place a
font table is kept — ``7A=[speed, 1]`` — and a column empty on all but four rows
is a column the eye skips past on all of them.

*Inside the **run***, which is not always inside the sheet: a run can be longer
than the tiles that draw it, and bounding by the picture instead would let one
code be held by both halves at once
(:meth:`~celpix.ui.font_alphabet_draft.AlphabetDraft.run_slots`).

**Prepend and Append are how a code off the sheet gets a row.** The table is one
row per tile, and a font routinely has to answer for codes no tile draws: a
terminator at ``$FF`` above a 128-tile sheet, a letter the sheet uploaded beside
this one draws. Both spins are counts of *rows*, before the first tile and after
the last, and every row they add is outside the run — so what is typed into one
is stored as a named code, at the code the row's header says. They default to
none, because the ordinary font has nothing outside its sheet and 65 536 rows is
not a table anybody can read.

**Prepend lists what it was asked for, including below zero.** Under an origin of
0 those rows read as ``-$04``, and they are shown rather than swallowed because
the spin is *how much headroom to look at*: it holds still while **Base code** is
dialled, so the rows come into the code space as the origin rises instead of
appearing and vanishing under the user's hands. Nothing is stored at a negative
code — no cell can hold one — and a row that is typed into says so and names the
spin that would fix it.

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

**Presentation only.** The window holds a working copy of the values that make
up a font alphabet — the origin, the rows listed either side, the run and the
named codes (:class:`~celpix.ui.font_alphabet_draft.AlphabetDraft`, where the
rules for changing them live) — and emits the whole of it whenever one of them
moves. It never reads the model, never
encodes anything and owns no undo history of its own
(:mod:`celpix.ui.main_window.font_alphabet`).
"""

from __future__ import annotations

from PySide6.QtCore import QItemSelectionModel, Qt, Signal
from PySide6.QtGui import QGuiApplication, QImage, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemDelegate,
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QMenu,
    QPushButton,
    QSplitter,
    QTableWidgetItem,
    QWidget,
)

from celpix.core.font import (
    TEMPLATES,
    Glyph,
    GlyphRole,
    format_code,
    parse_table,
    spell_name,
    split_params,
)
from celpix.ui.font_alphabet_draft import (
    ROLE_LABELS,
    AlphabetDraft,
    Placed,
    fresh_break_name,
    label_of,
    role_of,
    spelling,
)

# The table's widgetry (:mod:`celpix.ui.font_alphabet_table`); the column numbers
# are re-exported because a caller addressing a cell of this window's table needs
# them from where the table lives on screen.
from celpix.ui.font_alphabet_table import (
    COL_CODE,
    COL_ROLE,
    COL_TEXT,
    AlphabetTable,
    RoleDelegate,
    code_column_width,
    put_cell,
    role_column_width,
)
from celpix.ui.panzoom import (
    PanZoomSurface,
    SpacePanFilter,
    mount_surface,
)
from celpix.ui.tile_source_panel import TileSourcePanel
from celpix.ui.tool_window import ToolWindow
from celpix.ui.widgets import (
    Badge,
    SyncGuard,
    add_labelled,
    counted,
    hex_spin,
    signals_blocked,
    take_shortcuts,
    value_spin,
)

__all__ = ["COL_CODE", "COL_ROLE", "COL_TEXT", "ROLE_LABELS", "FontAlphabetWindow"]

# The most rows either spin will list outside the sheet. A whole byte of code
# space each way, which covers every one-byte format outright and is as much of a
# two-byte one as anybody types into by hand — past that the answer is a paste,
# and an unbounded spin is a window that hangs on a mistyped digit.
MAX_EXTRA_ROWS = 256

# The keys this window answers itself (:meth:`FontAlphabetWindow.event`). A
# `Qt.Tool` window is a top-level of its own, but Qt keeps the **parent's** window
# shortcuts alive while it is active — that is what lets the menu bar's keys work
# from a floating window. So Ctrl+Z here is claimed by two things at once, the
# main window's Undo action and this window, and Qt's answer to a tie is to fire
# *neither*: the key would do nothing at all until the user clicked back onto the
# main window. Claiming the override (:func:`~celpix.ui.widgets.take_shortcuts`)
# routes the press to :meth:`FontAlphabetWindow.keyPressEvent` to be answered
# once. A live cell editor still wins: `QLineEdit` accepts the override first,
# which is why Ctrl+Z inside a half-typed cell is still that cell's own undo.
_WINDOW_KEYS = (
    QKeySequence.StandardKey.Undo,
    QKeySequence.StandardKey.Redo,
    QKeySequence.StandardKey.Paste,
)


class FontAlphabetWindow(ToolWindow):
    """Floating editor for one font's alphabet: its origin, run and named codes."""

    #: The whole alphabet after an edit — ``(base, prepend, append, chars,
    #: codes)`` — and what to call the undo step it becomes. One report per
    #: gesture: a fill-down or a template sends the finished table, not a row.
    edited = Signal(int, int, int, str, tuple, str)
    #: A tile was picked, by its ID, so the canvas and the dock can follow.
    tile_selected = Signal(int)
    #: Ctrl+Z / Ctrl+Y arrived here rather than at the main window, because this
    #: window is the active one and answers the key itself (:data:`_WINDOW_KEYS`).
    undo_requested = Signal()
    redo_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        # Beside the lower half of the main window, where the text window —
        # which opens with it, beside the upper half — leaves room.
        super().__init__(
            parent,
            "Font Alphabet",
            "layout/font-alphabet-window",
            (520, 560),
            from_bottom=True,
        )
        self._syncing = SyncGuard()
        # Held while :meth:`commit_pending` is landing an open cell editor.
        self._committing = SyncGuard()
        # The working copy: what the file says. Held rather than re-read because
        # every edit is expressed as a change to it and the window is what knows
        # which row was touched.
        self._draft = AlphabetDraft()
        self._ids: list[int] = []

        self._sheet = TileSourcePanel(self)
        self._sheet.tile_selected.connect(self._on_tile_selected)
        self._sheet.zoom_requested.connect(self._on_zoom)
        # The backing around the sheet answers both gestures, as it does on the
        # canvas and in the tile source dock: a font sheet is small and centred,
        # so most of what the user is pointing at *is* the grey.
        self._scroll = mount_surface(self._sheet, align=Qt.AlignmentFlag.AlignCenter)

        self._table = AlphabetTable(0, 3)
        self._table.setHorizontalHeaderLabels(["Code", "Text", "Role"])
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setItemDelegateForColumn(COL_ROLE, RoleDelegate(self._table))
        # The one place the two columns' relationship is written down where a user
        # will meet it: that several characters are a name *by default*, and that
        # the Role column is what says otherwise.
        self._table.horizontalHeaderItem(COL_TEXT).setToolTip(
            "Character the code's tile draws, or a command name\n"
            "shown in [brackets]\n"
            "Several characters read as a name unless Role is dict"
        )
        self._table.horizontalHeaderItem(COL_ROLE).setToolTip(
            "• text - one character, drawn by the code's tile\n"
            "• dict - several characters spelled by one code\n"
            "• line break - reads as a newline\n"
            "• control - any other command; reads as its hex code"
        )
        header = self._table.horizontalHeader()
        # Sized to its contents, but **when the table is rebuilt** rather than by
        # the header itself (:meth:`_rebuild`). Left on ResizeToContents, Qt
        # re-measures the column on every single cell written into it — and a
        # kanji font is four thousand rows, each measurement scanning a thousand
        # of them, which turned one press of the Base code spin into ten seconds.
        # The width is the same either way; what changes is that it is computed
        # once per redraw instead of once per row.
        header.setSectionResizeMode(COL_CODE, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(COL_TEXT, QHeaderView.ResizeMode.Stretch)
        # Wide enough for the **combo** the delegate opens in it, not for the word
        # the cell shows: sized to contents the column fits "line break" exactly
        # and clips the dropdown arrow and frame that replace it on a click.
        # Measured off a real combo rather than guessed, since that chrome is the
        # style's and differs by platform.
        header.setSectionResizeMode(COL_ROLE, QHeaderView.ResizeMode.Fixed)
        self._table.setColumnWidth(COL_ROLE, role_column_width())
        self._table.itemChanged.connect(self._on_item_changed)
        self._table.itemSelectionChanged.connect(self._on_row_selected)

        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(self._scroll)
        split.addWidget(self._table)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 1)

        self.content.addLayout(self._build_header())
        self.content.addWidget(split)
        self.content.addLayout(self._build_actions())

        # Space pans wherever focus sits in here — with focus on the table, where
        # reading the sheet against the codes leaves it, as much as on the sheet.
        self._space_pan = SpacePanFilter(self, self._sheet, yields=_space_is_typed)

    def _pixel_surface(self) -> PanZoomSurface:
        return self._sheet

    # -- the keys this window answers itself -------------------------------
    def event(self, event) -> bool:  # noqa: ANN001 — QEvent
        """Claim Ctrl+Z / Ctrl+Y / Ctrl+V off the main window's actions.

        The override arrives here after the focused widget has passed on it, so
        an open cell editor keeps its own undo and paste and only the window's
        own keys are taken. Why an override rather than a `QShortcut`:
        :data:`_WINDOW_KEYS`.
        """
        if take_shortcuts(event, _WINDOW_KEYS):
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
        """The origin, how far past the sheet to list, and the first drafts.

        The three spins are one question asked twice over. **Base code** says
        which codes the tiles occupy; **Prepend** and **Append** say how many
        codes *outside* them the table still has to be able to answer for — a
        terminator no tile draws, a letter drawn by the sheet uploaded next to
        this one. So they sit together, and they are told apart by the fact that
        only the first moves anything already typed.
        """
        row = QHBoxLayout()
        row.setSpacing(6)
        # Where the run starts. Its own control and not a column of the table for
        # the reason it always was: the run's *shape* is legible off the sheet,
        # its *origin* is in the game's code and appears nowhere
        # (``docs/graphics-formats-reference/text-formats.md`` §3.2). So it is a
        # thing to dial against the text window next door, and moving it moves
        # every row at once.
        self._base_spin = hex_spin(
            -0xFFFF,
            0xFFFF,
            "The code the first tile draws, shifting what the text\n"
            "says without touching what the cells draw\n"
            "Dial it when the text reads as near-words; when the\n"
            "picture does instead, it is Base tile that is off",
        )
        self._base_spin.valueChanged.connect(self._on_base_changed)
        add_labelled(row, "Base code", self._base_spin, self._base_spin.toolTip())

        # Rows, not codes, so these are plain decimal where Base code is hex:
        # the answer to "how many" is a count, and showing $80 for a hundred and
        # twenty-eight rows would read as a code and be dialled as one.
        self._prepend_spin = value_spin(0, MAX_EXTRA_ROWS, 0, self._on_prepend_changed)
        add_labelled(
            row,
            "Prepend",
            self._prepend_spin,
            "Rows listed before the first tile, for codes\n"
            "below the sheet. Their text is stored as named codes",
        )
        self._append_spin = value_spin(0, MAX_EXTRA_ROWS, 0, self._on_append_changed)
        add_labelled(
            row,
            "Append",
            self._append_spin,
            "Rows listed after the last tile, for terminators and\n"
            "command codes. Their text is stored as named codes",
        )
        row.addStretch(1)

        # What each tile *says*, written into its corner. On by default, because
        # reading the sheet against the codes is the whole of what this window is
        # for — and off for the moment the letter shapes themselves are what is
        # being judged, which a caption sitting over a small letter shape is in
        # the way of. Presentation only: nothing is stored and no edit is made.
        self._show_chars = QCheckBox("Characters")
        self._show_chars.setChecked(True)
        # Takes no focus, the subsprite window's rule for its overlay toggles:
        # space is this window's pan gesture and is claimed window-wide
        # (:func:`_space_is_typed`), so a focused box could not toggle itself with it
        # anyway — it would only wear a focus ring for nothing.
        self._show_chars.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._show_chars.setToolTip(
            "Label each tile with its character\nDropped at zooms the text does not fit"
        )
        self._show_chars.toggled.connect(lambda _on: self._apply_labels())
        row.addWidget(self._show_chars)

        self._fill_with = QPushButton("Fill with...")
        self._fill_with.setToolTip("Fill the table from a common character order")
        menu = QMenu(self._fill_with)
        for name, base, chars in TEMPLATES:
            menu.addAction(name).triggered.connect(
                lambda _checked=False, b=base, c=chars, n=name: self._apply_template(
                    b, c, n
                )
            )
        self._fill_with.setMenu(menu)
        row.addWidget(self._fill_with)

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
            "The first tile's character is dropped"
        )
        self._shift_up.clicked.connect(lambda: self._shift(-1))
        row.addWidget(self._shift_up)

        self._shift_down = QPushButton("Shift down")
        self._shift_down.setToolTip(
            "Move every character one tile later\nThe first tile is left blank"
        )
        self._shift_down.clicked.connect(lambda: self._shift(1))
        row.addWidget(self._shift_down)
        row.addStretch(1)

        self._copy = QPushButton("Copy alphabet")
        self._copy.setToolTip(
            "Copy the table as 20=A lines, one per code\n"
            "Selected rows only, or from a single selected row\n"
            "to the end"
        )
        self._copy.clicked.connect(self._copy_alphabet)
        row.addWidget(self._copy)

        self._paste = QPushButton("Paste alphabet")
        self._paste.setToolTip(
            "Replace the table from the clipboard: 20=A lines,\n"
            "or a plain string with one character per code\n"
            "Selected rows only, or from a single selected row\n"
            "to the end. Ctrl+V in the table fills down instead"
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
        self._draft.tiles = len(self._ids)
        self._sheet.set_sheet(sheet, ids, cell_px, columns)

    def show_alphabet(
        self,
        title: str,
        base: int,
        prepend: int,
        append: int,
        chars: str,
        codes: tuple[Glyph, ...],
        *,
        code_digits: int = 2,
    ) -> None:
        """Present one font's alphabet, showing the window.

        ``code_digits`` is how wide the format prints a code
        (:attr:`~celpix.core.font.FontAlphabet.code_digits`), so the Code column
        writes ``$0121`` where the text window does.
        """
        with self._syncing:
            self.setWindowTitle(f"Font Alphabet - {title}")
            self._draft = AlphabetDraft(
                base, prepend, append, chars, codes, len(self._ids), code_digits
            )
            self._base_spin.setValue(base)
            self._prepend_spin.setValue(prepend)
            self._append_spin.setValue(append)
            self._rebuild()
        self.present()

    def hide_overlay(self) -> None:
        """Hide — the entry on screen has no font alphabet, or was closed.

        A cell still open for typing is settled first, for the reason
        :meth:`commit_pending` gives.
        """
        if self.isVisible():
            self.commit_pending()
        super().hide_overlay()

    def commit_pending(self) -> None:
        """Settle a table cell still open for typing, as the edit it is.

        An open editor writes its value back when it closes, and by then the
        table may have been refilled for another font — so the value would land
        on that font's row of the same number, or on no font at all. The host
        calls this before the table under the editor changes (an entry switch),
        and :meth:`hide_overlay` calls it before putting the window away.

        The editor is found through the window's own focus rather than asked of
        the view, which has no public handle on it: an open cell editor holds
        this window's focus, and a table not in its editing state has none.
        """
        if self._committing or (
            self._table.state() != QAbstractItemView.State.EditingState
        ):
            return
        viewport = self._table.viewport()
        editor = self.focusWidget()
        # Up to the editor itself: an editable combo keeps its focus on the line
        # edit inside it, and the view knows only the outer widget.
        while editor is not None and editor.parentWidget() is not viewport:
            editor = editor.parentWidget()
        if editor is None:
            return
        # Guarded because the commit is an edit, and the host's answer to an
        # edit can refill this table — which would ask the same editor again.
        with self._committing:
            self._table.commitData(editor)
            self._table.closeEditor(editor, QAbstractItemDelegate.EndEditHint.NoHint)

    def select_tile(self, tile_id: int) -> None:
        """Pick the tile the canvas's selected cell names, as a click would.

        A **selection** in both readings and not a mark of its own: the row the
        clipboard buttons act on and Enter opens to type into, and the picked
        tile on the sheet above it. Clicking a character in the string and
        clicking its tile on the sheet are the same question asked from two
        places, so they must not leave the window in two different states.

        Ignored for a tile the sheet does not show, which is a cell naming a
        code past the bank the binding reaches.
        """
        if tile_id not in self._ids:
            return
        self._select_row(self._draft.sheet_row() + self._ids.index(tile_id))
        # Guarded, so the sheet's own report of the pick does not answer this
        # with `selectRow` — the reason a click on the sheet is the only pick
        # that moves the table (:meth:`_on_tile_selected`).
        with self._syncing:
            self._sheet.select_id(tile_id)

    # -- the two readings --------------------------------------------------
    def _rebuild(self) -> None:
        """Redraw both readings from the working copy.

        Items are **reused** wherever there is one to reuse — the Code cells
        included, which change on every redraw that follows the origin: an item
        is *made* only for a row the table has never had. Replacing three
        thousand `QTableWidgetItem`s per keystroke is what made typing here feel
        slow; writing over the ones already there does not.

        The Code column's width is settled here, once, for the reason the header
        is Fixed (:meth:`__init__`).
        """
        draft = self._draft
        codes = draft.rows()
        merged = draft.merged()
        with signals_blocked(self._table):
            fresh = self._table.rowCount() != len(codes)
            if fresh:
                self._table.setRowCount(len(codes))
            for row, code in enumerate(codes):
                glyph = merged.get(code)
                text = spelling(glyph)
                label = label_of(glyph.role if glyph else GlyphRole.TEXT)
                name = self._table.item(row, COL_CODE)
                if name is None:
                    name = QTableWidgetItem()
                    # Everything a cell ordinarily is, minus typing into it: the
                    # code is the tile's position and the only way to move it is
                    # the Base code spin.
                    name.setFlags(name.flags() & ~Qt.ItemFlag.ItemIsEditable)
                    self._table.setItem(row, COL_CODE, name)
                if name.data(Qt.ItemDataRole.UserRole) != code:
                    name.setText(draft.code_label(code))
                    name.setData(Qt.ItemDataRole.UserRole, code)
                put_cell(self._table, row, COL_TEXT, text)
                put_cell(self._table, row, COL_ROLE, label)
        self._table.setColumnWidth(
            COL_CODE, code_column_width(self._table, map(draft.code_label, codes))
        )
        self._apply_labels(merged)

    def _apply_labels(self, merged: dict[int, Glyph] | None = None) -> None:
        """Caption the sheet's tiles with what they say — or leave them bare.

        The **Characters** box is a view of the same merge the table shows, so it
        is answered by re-captioning rather than by redrawing: the toggle changes
        nothing about the rows, and rebuilding a thousand of them to hide a
        caption is the cost the reuse in :meth:`_rebuild` exists to avoid. The
        merge is handed in where the caller already built one, for that same
        reason.
        """
        merged = self._draft.merged() if merged is None else merged
        base = self._draft.base
        self._sheet.set_labels(
            {
                tile: (glyph.text if (glyph := merged.get(base + at)) else "")
                for at, tile in enumerate(self._ids)
            }
            if self._show_chars.isChecked()
            else {}
        )

    # -- editing -----------------------------------------------------------
    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        """One cell typed or picked: settle it into the run or the named codes.

        **One settled row is one undo step**, whichever column settled it: a row
        is a code's whole answer, so the next row the user reaches for is the
        next thing they would take back.

        **A role needs something to be the role of.** What a non-text code reads
        as is its *name*, and a glyph with no text is not a thing the model can
        hold at all — so the pick is put back and the row says what it wants
        first, rather than being silently dropped on the next redraw. A **text**
        role wants nothing, so an empty row picking it is simply a row that still
        spells nothing.

        **Several characters typed in are guessed to be a name**, and **the
        spelling settles text ⇄ dict** — both rules of the gesture rather than of
        the storage (:meth:`~celpix.ui.font_alphabet_draft.AlphabetDraft.
        settle_role`); the status bar names the column that answers a guess.

        **A prepended row below zero is shown and not stored**
        (:meth:`~celpix.ui.font_alphabet_draft.AlphabetDraft.write`).
        Same shape of answer, and the badge names the control that fixes it: no
        cell can hold a negative code, so what such a row is waiting for is Base
        code to come up under it.
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
        role = role_of(role_item.text() if role_item else "")
        picked = item.column() == COL_ROLE
        if picked and not text and role is GlyphRole.BREAK:
            # The one role that names itself: a format's second and third break
            # (one that scrolls, one that does not) are told apart by the code
            # beside them rather than by what they are called, so a number is a
            # better answer than a prompt. Only a *control* still has to be said
            # out loud - what it does is the whole of what its name carries.
            text = fresh_break_name(self._draft.merged().values())
        if picked and not text:
            self._rebuild()
            if not role.spells:
                self.set_status(
                    f"{self._draft.code_label(code)} spells nothing.",
                    Badge(
                        "no text",
                        "A line break or control reads as its name\n"
                        "Fill the Text column first",
                        warning=True,
                    ),
                )
            return
        role, guessed = self._draft.settle_role(text, role, picked=picked)
        if not self._draft.write(code, text, role):
            self._rebuild()
            self.set_status(
                f"{self._draft.code_label(code)} is below code zero.",
                Badge(
                    "no such code",
                    "Codes below zero cannot be written to a cell\n"
                    "Raise Base code to bring these rows into range",
                    warning=True,
                ),
            )
            return
        if picked:
            # A name written *for* the user has to appear in the column it was
            # written into - the round trip through the undo stack redraws the
            # table only once the edit has been applied to the entry.
            self._rebuild()
        self._emit(
            f"set {self._draft.code_label(code)} to {label_of(role)}"
            if picked
            else "edit font alphabet"
        )
        # After the emit, not before it: the step lands through the undo stack and
        # comes back as a refresh, which writes the window's own readout over
        # anything said here first.
        if guessed:
            name, takes = split_params(text)
            self.set_status(
                f"{self._draft.code_label(code)} reads as [{spell_name(name)}]"
                + (
                    f", swallowing the {counted(takes, 'cell')} after it."
                    if takes
                    else " - set Role to dict if the code spells those characters."
                )
            )
        elif role is GlyphRole.DICT:
            self.set_status(
                f"{self._draft.code_label(code)} spells {text!r} - "
                f"one cell for {len(text)} characters."
            )

    def _fill_down(self) -> None:
        """Fill down from the selected row, one code point per code.

        The gesture the whole window is arranged around: a font sheet is a run of
        letters in order, and the run is the thing a user already has in a
        clipboard. Newlines and tabs are skipped rather than written, since they
        describe the shape of whatever the text was copied out of.

        One undo step, because it is one gesture — and stopping at the last row
        rather than growing the table, since how far past the sheet this font is
        read is what the two spins say and a paste is not an answer to that.

        A run started on a prepended row below zero **steps over** those rows
        rather than stopping at them: nothing can be stored there
        (:meth:`~celpix.ui.font_alphabet_draft.AlphabetDraft.write`), and the
        characters that follow are still meant for the codes that come after.
        """
        typed = QGuiApplication.clipboard().text()
        chars = [c for c in typed if c not in "\r\n\t"]
        rows = self._table.selectionModel().selectedRows()
        placed = self._draft.fill(rows[0].row() if rows else 0, chars)
        badge = _dropped_badge(placed)
        if not placed.landed:
            self.set_status("Nothing filled in.", badge)
            return
        self._rebuild()
        self._emit(f"paste {counted(placed.landed, 'character')}")
        self.set_status(f"{placed.landed} filled in", badge)

    def _apply_template(self, base: int, chars: str, name: str) -> None:
        """Replace the run wholesale with one of the shipped arrangements.

        The run only. Named codes are the user's own reading of the stream and
        have nothing to do with which letters the sheet draws, so a first draft
        of the second must not take the first away.
        """
        self._draft.base, self._draft.chars = base, chars
        with self._syncing:
            self._base_spin.setValue(base)
        self._rebuild()
        self._emit(f"fill with {name}")

    def _shift(self, by: int) -> None:
        """Move the run ``by`` tiles along the sheet, one gesture, one step.

        What moves is the draft's (:meth:`~celpix.ui.font_alphabet_draft.
        AlphabetDraft.shift`); nothing to move, no step.
        """
        if not self._draft.shift(by):
            return
        self._rebuild()
        self._emit("shift the run down" if by > 0 else "shift the run up")

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
        rows = self._draft.rows()
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
        written in the bracketed form the same parser reads, operand count
        included — ``7A=[speed, 1]`` — since that is the whole of what the row
        said (:func:`~celpix.core.font.split_params`).

        Codes that say nothing are left out rather than written as blanks: what
        a run has not reached is not a glyph spelling the empty string. So are
        the **negative** ones a base dialled below zero puts the run on: the form
        writes a code as hex and has no spelling for a sign, so such a line would
        come back off the clipboard as no line at all — and nothing can be stored
        there anyway (:meth:`~celpix.ui.font_alphabet_draft.AlphabetDraft.write`).
        """
        codes, _bounded = self._span()
        lines = self._draft.copy_lines(codes)
        QGuiApplication.clipboard().setText("".join(lines))
        self.set_status(f"{counted(len(lines), 'code')} copied.")

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
                    "a string of characters",
                    warning=True,
                ),
            )
            return
        codes, bounded = self._span()
        placed = self._draft.replace(codes, bounded, glyphs, chars)
        badge = _dropped_badge(placed)
        if not placed.landed:
            self.set_status("Nothing pasted.", badge)
            return
        self._rebuild()
        self._emit(f"paste {counted(placed.landed, 'code')}")
        self.set_status(f"{counted(placed.landed, 'code')} pasted.", badge)

    def _on_base_changed(self, value: int) -> None:
        """Slide the run along the code space — its own step per settled value.

        ``keyboardTracking`` is off on this spin (:func:`~celpix.ui.widgets.
        hex_spin`), so holding the arrow key reports once at the end and the undo
        stack gets the gesture instead of the path it took.
        """
        if self._syncing or value == self._draft.base:
            return
        self._draft.base = value
        self._rebuild()
        self._emit(f"set base code to {format_code(value, self._draft.digits)}")

    def _on_prepend_changed(self, value: int) -> None:
        """List ``value`` more rows below the sheet — a step of its own.

        Nothing already typed moves: these rows are outside the run either way,
        so what the spin changes is which of them have somewhere to be shown.
        Dialling it *down* past a named code does not delete the code — it keeps
        its row, appended (:meth:`~celpix.ui.font_alphabet_draft.AlphabetDraft.
        rows`), because a code with an answer must
        stay reachable however the table is framed.
        """
        if self._syncing or value == self._draft.prepend:
            return
        self._draft.prepend = value
        self._extra_rows_changed("prepend", value)

    def _on_append_changed(self, value: int) -> None:
        """List ``value`` more rows above the sheet. :meth:`_on_prepend_changed`."""
        if self._syncing or value == self._draft.append:
            return
        self._draft.append = value
        self._extra_rows_changed("append", value)

    def _extra_rows_changed(self, which: str, value: int) -> None:
        """The half the two spins share: redraw, and report the gesture once."""
        self._rebuild()
        self._emit(f"{which} {counted(value, 'row')}")

    def _emit(self, label: str) -> None:
        draft = self._draft
        self.edited.emit(
            draft.base, draft.prepend, draft.append, draft.chars, draft.codes, label
        )

    # -- the two readings following each other -----------------------------
    def _on_tile_selected(self, tile_id: int) -> None:
        # Only a pick made **on the sheet** moves the table. The sheet reports a
        # selection it was handed the same way it reports a click, so following
        # this one back would answer a row selection with `selectRow` — which
        # takes a stretch of picked rows down to the one that pushed the tile,
        # and the clipboard buttons read that stretch (:meth:`_span`).
        if not self._syncing and tile_id in self._ids:
            self._select_row(self._draft.sheet_row() + self._ids.index(tile_id))
        self.tile_selected.emit(tile_id)

    def _on_row_selected(self) -> None:
        """Show the picked rows on the sheet — all of them, not just the first.

        The two readings are one list, so a stretch of rows is a stretch of
        tiles: the rows the clipboard buttons act on (:meth:`_span`) are the
        tiles ringed above them, and there is never a moment where the window
        says one thing at the top and another at the bottom. Rows outside the
        sheet — the prepended and appended ones, and named codes past both —
        carry no tile and are simply not in the answer.
        """
        rows = self._table.selectionModel().selectedRows()
        if self._syncing or not rows:
            return
        first = self._draft.sheet_row()
        picked = [
            self._ids[at]
            for index in sorted(rows, key=lambda index: index.row())
            if 0 <= (at := index.row() - first) < len(self._ids)
        ]
        if not picked:
            return
        with self._syncing:
            self._sheet.select_ids(picked)

    def _select_row(self, row: int) -> None:
        """Make ``row`` **the** selection — one row, not one more row.

        Spelled as an explicit ``ClearAndSelect | Rows`` rather than as
        ``selectRow``, which derives its command from the **live keyboard
        modifiers**: with Shift held it extends from the anchor instead of
        replacing, and Shift held is exactly the state an arrow key stepping the
        pick across the sheet arrives in. The table then showed a stretch of rows
        the sheet was ringing one tile of — the two readings saying different
        things, which is the one thing this window must never do. A stray row is
        not cosmetic either: the clipboard buttons read the selection
        (:meth:`_span`), so a *Paste alphabet* aimed at one row would land as a
        two-row replace.

        The **current cell** moves with it, and to the Text column, since that is
        the one Enter opens (:meth:`_AlphabetTable.keyPressEvent`) — and because
        the anchor a later Shift+Down extends from is the current index, so a
        stretch picked in the table starts where the sheet last pointed.
        """
        with self._syncing:
            self._table.setCurrentCell(
                row,
                COL_TEXT,
                QItemSelectionModel.SelectionFlag.ClearAndSelect
                | QItemSelectionModel.SelectionFlag.Rows,
            )
            item = self._table.item(row, COL_TEXT)
            if item is not None:
                self._table.scrollToItem(item)

    def _on_zoom(self, steps: int, _at: object) -> None:
        """Ctrl+wheel over the sheet — the sheet clamps it to its own range
        (:data:`~celpix.ui.tile_source_panel.ZOOM_RANGE`). No spin to drive, so
        nothing to anchor: the window has only the wheel."""
        self._sheet.set_zoom(self._sheet.zoom + steps)


def _space_is_typed(_obj: object) -> bool:
    """Whether space is a character here rather than this window's pan.

    The cell editor the table opens (a `QLineEdit` over the Text column, which a
    font's space character is typed into), the Role column's combo, and any
    popup open over the window. The spins are deliberately absent — space does
    nothing in one, and yielding to them would kill the pan on the controls a
    user reaches for it from.
    """
    return QApplication.activePopupWidget() is not None or isinstance(
        QApplication.focusWidget(), (QLineEdit, QComboBox)
    )


# Why a character or a code the clipboard offered found no home, in the order
# the sentences read. Each names a different fix — Base code, Append or picking
# other rows — so reporting one as another sends the user to the wrong control,
# and a paste that lost characters two ways says both.
_DROP_REASONS: tuple[tuple[str, str], ...] = (
    (
        "below",
        "Rows below code zero were not written",
    ),
    (
        "past",
        "Codes past the last row were not written",
    ),
    (
        "outside",
        "Codes outside the selected rows were not written",
    ),
)


def _dropped_badge(placed: Placed) -> Badge | None:
    """What a paste could not write, and which of :data:`_DROP_REASONS` each was.

    None where everything landed. The counts are kept apart all the way here
    rather than summed at the call site, because the total is the caption and the
    reasons are the sentence — and a paste is routinely refused two ways at once.
    """
    counts = {"below": placed.below, "past": placed.past, "outside": placed.outside}
    why = [sentence for key, sentence in _DROP_REASONS if counts[key]]
    if not why:
        return None
    return Badge(f"{sum(counts.values())} dropped", "\n".join(why), warning=True)
