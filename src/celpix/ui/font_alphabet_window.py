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
code be held by both halves at once (:meth:`FontAlphabetWindow._run_slots`).

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

**Presentation only.** The window holds a working copy of the three values that
make up a font alphabet — the origin, the run and the named codes — and emits
the whole triple whenever one of them moves. It never reads the model, never
encodes anything and owns no undo history of its own
(:mod:`celpix.ui.main_window.font_alphabet`).
"""

from __future__ import annotations

from collections.abc import Iterable
from itertools import count

from PySide6.QtCore import QEvent, QItemSelectionModel, QPoint, Qt, Signal
from PySide6.QtGui import QGuiApplication, QImage, QKeyEvent, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
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
    split_params,
)
from celpix.ui.tile_source_panel import TileSourcePanel
from celpix.ui.widgets import (
    Badge,
    apply_badge,
    hex_spin,
    pan_scroll_area,
    value_spin,
)
from celpix.ui.window_layout import WindowLayout

# The three columns, in the order a row is read: which code, what it says, and
# what kind of thing it is.
COL_CODE, COL_TEXT, COL_ROLE = 0, 1, 2

# How the roles are captioned. The enum's own spellings are the on-disk ones and
# read as jargon in a cell; these are what the column offers.
ROLE_LABELS: tuple[tuple[GlyphRole, str], ...] = (
    (GlyphRole.TEXT, "text"),
    (GlyphRole.DICT, "dict"),
    (GlyphRole.BREAK, "line break"),
    (GlyphRole.CONTROL, "control"),
)

# The most rows either spin will list outside the sheet. A whole byte of code
# space each way, which covers every one-byte format outright and is as much of a
# two-byte one as anybody types into by hand — past that the answer is a paste,
# and an unbounded spin is a window that hangs on a mistyped digit.
MAX_EXTRA_ROWS = 256

# Room around the widest code label in its column (:func:`_code_column_width`) —
# the cell margins a style puts either side of an item's text, which measuring
# the string alone does not account for. Generous rather than exact: a code
# column a few pixels wide of its text costs nothing, and one a few pixels short
# clips the ``$`` off a row.
CODE_COLUMN_PADDING = 16

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

    #: The whole alphabet after an edit — ``(base, prepend, append, chars,
    #: codes)`` — and what to call the undo step it becomes. One report per
    #: gesture: a fill-down or a template sends the finished table, not a row.
    edited = Signal(int, int, int, str, tuple, str)
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
        self._prepend = 0
        self._append = 0
        self._chars = ""
        self._codes: tuple[Glyph, ...] = ()
        self._ids: list[int] = []

        self._sheet = TileSourcePanel(self)
        self._sheet.tile_selected.connect(self._on_tile_selected)
        self._sheet.zoom_requested.connect(self._on_zoom)
        self._sheet.pan_requested.connect(self._pan)
        self._scroll = QScrollArea()
        self._scroll.setWidget(self._sheet)
        self._scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # The backing around the sheet answers both gestures, as it does on the
        # canvas and in the tile source dock: a font sheet is small and centred,
        # so most of what the user is pointing at *is* the grey.
        self._sheet.claim_background(self._scroll)

        self._table = _AlphabetTable(0, 3)
        self._table.setHorizontalHeaderLabels(["Code", "Text", "Role"])
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setItemDelegateForColumn(COL_ROLE, _RoleDelegate(self._table))
        # The one place the two columns' relationship is written down where a user
        # will meet it: that several characters are a name *by default*, and that
        # the Role column is what says otherwise.
        self._table.horizontalHeaderItem(COL_TEXT).setToolTip(
            "What the code says: the character its tile draws, or\n"
            "the name a command reads as inside [brackets]\n"
            "Several characters are taken as a name - set Role to\n"
            "dict where one code spells them all, as a th pair does"
        )
        self._table.horizontalHeaderItem(COL_ROLE).setToolTip(
            "text is one character, the one its tile draws\n"
            "dict is several standing behind the one code, and is\n"
            "drawn as those characters where no tile draws it\n"
            "line break reads as a newline; control is anything\n"
            "else the game acts on, and reads as its own hex code"
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
        self._table.setColumnWidth(COL_ROLE, _role_column_width())
        self._table.itemChanged.connect(self._on_item_changed)
        self._table.itemSelectionChanged.connect(self._on_row_selected)

        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(self._scroll)
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

        # The pan gesture's space key is taken off an application filter rather
        # than a key event of this window, so it answers wherever focus sits in
        # here — see eventFilter.
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

    # -- the sheet's own gestures ------------------------------------------
    def _pan(self, dx: int, dy: int) -> None:
        """Shift the sheet's scroll view by a space-drag delta (device pixels)."""
        pan_scroll_area(self._scroll, dx, dy)

    #: Where the space bar is a character rather than this window's pan: the
    #: cell editor the table opens (a `QLineEdit` over the Text column, which a
    #: font's space glyph is typed into) and the Role column's combo. The spins
    #: are deliberately absent — space does nothing in one, and yielding to them
    #: would kill the pan on the controls a user reaches for it from.
    _SPACE_INPUT_TYPES = (QLineEdit, QComboBox)

    def eventFilter(self, obj, event) -> bool:  # noqa: ANN001 — Qt override
        """Claim the space bar for the pan wherever focus sits in this window.

        Filtered on the application rather than handled in ``keyPressEvent``,
        because a key press goes to the focused widget alone: with focus on the
        table — where reading the sheet against the codes leaves it — the press
        reached a widget that does nothing with it. The subsprite window's rule,
        and the main window's (:meth:`~celpix.ui.main_window.navigation.
        NavigationMixin._handle_space_pan`).

        Any widget of *this* window and nothing outside it: only one window can
        be the one being typed into.
        """
        et = event.type()
        if et == QEvent.Type.WindowDeactivate and obj is self:
            # A hold that outlives the window's activation: the release lands in
            # whatever was raised over it and is never seen here, leaving the
            # sheet holding an open hand and eating the next press.
            self._sheet.set_pan_mode(False)
        elif (
            et in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease)
            and event.key() == Qt.Key.Key_Space
            and isinstance(obj, QWidget)
            and obj.window() is self
            and QApplication.activePopupWidget() is None
            and not isinstance(QApplication.focusWidget(), self._SPACE_INPUT_TYPES)
        ):
            # Auto-repeat is swallowed rather than acted on: holding space fires
            # press after press, and each would re-arm a mode already on.
            if not event.isAutoRepeat():
                self._sheet.set_pan_mode(et == QEvent.Type.KeyPress)
            return True
        return super().eventFilter(obj, event)

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

        # Rows, not codes, so these are plain decimal where Base code is hex:
        # the answer to "how many" is a count, and showing $80 for a hundred and
        # twenty-eight rows would read as a code and be dialled as one.
        row.addWidget(QLabel("Prepend"))
        self._prepend_spin = value_spin(0, MAX_EXTRA_ROWS, 0, self._on_prepend_changed)
        self._prepend_spin.setToolTip(
            "Rows to list before the first tile, for codes\n"
            "below the sheet that the stream still uses\n"
            "Nothing typed into one is on a tile, so it is\n"
            "stored as a named code"
        )
        row.addWidget(self._prepend_spin)

        row.addWidget(QLabel("Append"))
        self._append_spin = value_spin(0, MAX_EXTRA_ROWS, 0, self._on_append_changed)
        self._append_spin.setToolTip(
            "Rows to list after the last tile, which is where\n"
            "a terminator or a command code usually sits\n"
            "Nothing typed into one is on a tile, so it is\n"
            "stored as a named code"
        )
        row.addWidget(self._append_spin)
        row.addStretch(1)

        # What each tile *says*, written into its corner. On by default, because
        # reading the sheet against the codes is the whole of what this window is
        # for — and off for the moment the letter shapes themselves are what is
        # being judged, which a caption sitting over a small glyph is in the way
        # of. Presentation only: nothing is stored and no edit is made.
        self._show_chars = QCheckBox("Characters")
        self._show_chars.setChecked(True)
        # Takes no focus, the subsprite window's rule for its overlay toggles:
        # space is this window's pan gesture and is claimed window-wide
        # (:meth:`eventFilter`), so a focused box could not toggle itself with it
        # anyway — it would only wear a focus ring for nothing.
        self._show_chars.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._show_chars.setToolTip(
            "Caption each tile with the character it says\n"
            "Off shows the letter shapes alone, which is what\n"
            "judging the art against the codes wants\n"
            "Dropped anyway where the tiles are drawn too small\n"
            "to hold the text"
        )
        self._show_chars.toggled.connect(lambda _on: self._apply_labels())
        row.addWidget(self._show_chars)

        self._fill_with = QPushButton("Fill with...")
        self._fill_with.setToolTip(
            "Fill the run with a common arrangement, as a first draft"
        )
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

    def set_pixel_aspect(self, aspect) -> None:  # noqa: ANN001 — a PixelAspect
        """Draw at ``aspect`` — forwarded to the glyph sheet.

        One name on every holder of a pixel surface, so the window applies the
        project's setting with a loop rather than by reaching through each of them
        (:meth:`~celpix.ui.main_window.view_menu.ViewMenuMixin._sync_pixel_aspect`).
        """
        self._sheet.set_pixel_aspect(aspect)

    def show_alphabet(
        self,
        title: str,
        base: int,
        prepend: int,
        append: int,
        chars: str,
        codes: tuple[Glyph, ...],
    ) -> None:
        """Present one font's alphabet, showing the window."""
        self._syncing = True
        try:
            self.setWindowTitle(f"Font Alphabet - {title}")
            self._base, self._chars, self._codes = base, chars, codes
            self._prepend, self._append = prepend, append
            self._base_spin.setValue(base)
            self._prepend_spin.setValue(prepend)
            self._append_spin.setValue(append)
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
        """Hide — the entry on screen has no font alphabet, or was closed.

        Disarms the pan for the same reason :meth:`closeEvent` does: the space
        release lands wherever the window went.
        """
        self._sheet.set_pan_mode(False)
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
        self._select_row(self._sheet_row() + self._ids.index(tile_id))
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

    def _run_slots(self) -> int:
        """How many codes the **positional run** covers — its length or the sheet's.

        Not simply ``len(self._ids)``, and the difference is where a code is
        *stored*. The sheet above is a picture of the tiles; the run is a fact
        about the entry, and a paste or a template can leave it **longer than the
        sheet** — *ASCII, from $20* is 95 characters, and plenty of fonts are
        smaller than that.

        Bounding by the sheet alone splits one run in half: a code the run
        already answers for, but past the last tile, reads out of the run and
        writes back as a *named* code. The two halves then disagree about that
        code, and since named codes do not move with **Base code** and the run
        does, dialling the origin afterwards slides one out from under the other
        — which looks exactly like an entry being lost.

        So the run's own length counts, whether or not a tile draws it.
        """
        return max(len(self._ids), len(self._chars))

    def _first_row_code(self) -> int:
        """The code the table's first row holds — the sheet, less **Prepend**.

        **Not floored at zero**, so the spin always lists the rows it was asked
        for. Below the origin they read as ``-$04``, which is not a code any cell
        can hold — and saying so plainly is the point: Prepend is *how much
        headroom below the sheet to look at*, and it stays put while **Base code**
        is dialled, so those rows come into the code space as the origin rises
        rather than appearing and vanishing under the user's hands.

        Nothing is ever **stored** at a negative code (:meth:`_write`).
        """
        return self._base - self._prepend

    def _sheet_row(self) -> int:
        """Which row the **first tile** sits on — everything above it is prepended.

        Every row ⇄ tile conversion goes through here, since getting it wrong
        points the sheet at the wrong glyph rather than failing outright.
        """
        return self._prepend

    def _rows(self) -> list[int]:
        """Every code the table lists, in order.

        One row per tile, **Prepend** rows before them and **Append** rows after,
        then any named code still outside all of that — appended, in order, since
        a code that has been given an answer must have a row to show it on
        whatever the spins say. A code appears once however many of those reach
        it, and the prepended ones may be negative (:meth:`_first_row_code`).

        Bounded by the spins because the alternative is 65 536 rows on a two-byte
        stream, and they default to none because the ordinary font has nothing
        outside its sheet.
        """
        first = self._first_row_code()
        stop = self._base + self._run_slots() + self._append
        run = list(range(first, max(first, stop)))
        span = set(run)
        return run + sorted(g.code for g in self._codes if g.code not in span)

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
        codes = self._rows()
        merged = self._merged()
        self._table.blockSignals(True)
        try:
            fresh = self._table.rowCount() != len(codes)
            if fresh:
                self._table.setRowCount(len(codes))
            for row, code in enumerate(codes):
                glyph = merged.get(code)
                text = _spelling(glyph)
                label = _label_of(glyph.role if glyph else GlyphRole.TEXT)
                name = self._table.item(row, COL_CODE)
                if name is None:
                    name = QTableWidgetItem()
                    # Everything a cell ordinarily is, minus typing into it: the
                    # code is the tile's position and the only way to move it is
                    # the Base code spin.
                    name.setFlags(name.flags() & ~Qt.ItemFlag.ItemIsEditable)
                    self._table.setItem(row, COL_CODE, name)
                if name.data(Qt.ItemDataRole.UserRole) != code:
                    name.setText(_code_label(code))
                    name.setData(Qt.ItemDataRole.UserRole, code)
                _put(self._table, row, COL_TEXT, text)
                _put(self._table, row, COL_ROLE, label)
        finally:
            self._table.blockSignals(False)
        self._table.setColumnWidth(COL_CODE, _code_column_width(self._table, codes))
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
        merged = self._merged() if merged is None else merged
        self._sheet.set_labels(
            {
                tile: (glyph.text if (glyph := merged.get(self._base + at)) else "")
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

        **Several characters typed in are guessed to be a name**, and the guess is
        made here rather than in :meth:`_write` because it is about the *gesture*:
        it is what somebody typing ``wait`` into a cell meant, and it is wrong for
        somebody typing ``th``. So it applies to a Text edit alone — the Role
        column overrides it, a row that already reads *dict* is left as it is, and
        the status bar names the column that answers it.

        **The spelling settles text ⇄ dict**, whichever of the two was picked, so
        the role a row shows is never a role its text contradicts
        (:class:`~celpix.core.font.GlyphRole`). It is the same rule the model
        holds glyphs to, applied at the gesture as well so the column redraws to
        what was actually stored.

        **A prepended row below zero is shown and not stored** (:meth:`_write`).
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
            if not role.spells:
                self.set_status(
                    f"{_code_label(code)} spells nothing.",
                    Badge(
                        "no text",
                        "A line break or a control reads as its name.\n"
                        "Write one in the Text column first.",
                        warning=True,
                    ),
                )
            return
        guessed = not picked and role is GlyphRole.TEXT and len(text) > 1
        if guessed:
            role = GlyphRole.CONTROL
        elif role.spells:
            # **text** and **dict** are one fact said twice — a code spells one
            # character or it spells several — so the column's word is settled
            # against the spelling rather than trusted over it. Which is also
            # what stops the guess above being made twice: a row that already
            # reads *dict* is no longer a row whose role says ``text``, so
            # correcting ``th`` to ``the`` leaves the role alone.
            role = GlyphRole.DICT if len(text) > 1 else GlyphRole.TEXT
        if not self._write(code, text, role):
            self._rebuild()
            self.set_status(
                f"{_code_label(code)} is below code zero.",
                Badge(
                    "no such code",
                    "No cell can hold a code below zero, so this row\n"
                    "cannot be written to. Raise Base code to bring\n"
                    "these rows into the code space.",
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
            f"set {_code_label(code)} to {_label_of(role)}"
            if picked
            else "edit font alphabet"
        )
        # After the emit, not before it: the step lands through the undo stack and
        # comes back as a refresh, which writes the window's own readout over
        # anything said here first.
        if guessed:
            name, takes = split_params(text)
            self.set_status(
                f"{_code_label(code)} reads as [{spell_name(name)}]"
                + (
                    f", swallowing the {takes} cell"
                    f"{'s' if takes != 1 else ''} after it."
                    if takes
                    else " - set Role to dict if the code spells those characters."
                )
            )
        elif role is GlyphRole.DICT:
            self.set_status(
                f"{_code_label(code)} spells {text!r} - "
                f"one cell for {len(text)} characters."
            )

    def _write(self, code: int, text: str, role: GlyphRole) -> bool:
        """Land one code's answer in whichever half of the storage holds it.

        The run takes it when it can — inside the run's own extent
        (:meth:`_run_slots`), one code point, no role — because that is the half
        a tile's position states, and keeping it there means the run stays the
        thing the sheet is a picture of. Everything else is a named code, and
        either way the other half is cleared of that code so the two can never
        disagree.

        **False, writing nothing, for a code below zero** — the rows Prepend
        lists under the origin (:meth:`_first_row_code`). No cell can hold such a
        value, and a named code is the one half of the storage that is *not*
        range-checked downstream: `FontAlphabet.shifted` drops an out-of-range
        glyph of the run, but a named one goes straight through `merged` and
        `encode` would write it into a cell. So the refusal is here, at the one
        door all three write paths go through, rather than at each of them.

        **More than one character is never positional**, whichever role it is. A
        tile draws one character, so a longer answer is not what the tile *says*:
        it is either what the code is *for* — ``[wait]``, which the string spells
        by name — or a code standing for a pair, which spells all of it at once
        (``docs/design/fontmap-entry.md`` §4). Both are facts about the stream, so
        both are named codes and neither moves when the origin is dialled.

        Storage only. Which of those two a typed spelling *is* is a reading of
        the gesture rather than of the row, so it is decided where the gesture
        arrives (:meth:`_on_item_changed`) — a paste says so in its own form, and
        a fill-down never lands more than one character.

        **The description survives a retyped row.** It is the one field of a
        command no column shows — the sentence on its insert-row button — so
        rebuilding the glyph from what is on screen would quietly drop it every
        time somebody corrected a name or an operand count.
        """
        if code < 0:
            return False
        at = code - self._base
        in_run = 0 <= at < self._run_slots()
        params = 0
        if not role.spells:
            # ``speed, 1`` is a command and the cells it swallows, which is the
            # spelling the table form uses and the one the column shows back
            # (:func:`_spelling`). Only a command can swallow anything, so this
            # is read here rather than off every row: a character with a comma
            # in it is a character.
            text, params = split_params(text)
            # A name is what goes inside the brackets, so it is one word by the
            # time it is stored rather than at the moment it is read back.
            text = spell_name(text)
        positional = in_run and role is GlyphRole.TEXT and len(text) == 1
        if positional:
            self._set_char(at, text)
        elif in_run:
            self._set_char(at, HOLE)
        held = next((g for g in self._codes if g.code == code), None)
        self._codes = tuple(g for g in self._codes if g.code != code)
        if text and not positional:
            self._codes = tuple(
                sorted(
                    [
                        *self._codes,
                        Glyph(
                            code,
                            text,
                            role,
                            held.description if held is not None else "",
                            params=params,
                        ),
                    ],
                    key=lambda g: g.code,
                )
            )
        return True

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
        rather than growing the table, since how far past the sheet this font is
        read is what the two spins say and a paste is not an answer to that.

        A run started on a prepended row below zero **steps over** those rows
        rather than stopping at them: nothing can be stored there
        (:meth:`_write`), and the characters that follow are still meant for the
        codes that come after.
        """
        typed = QGuiApplication.clipboard().text()
        chars = [c for c in typed if c not in "\r\n\t"]
        rows = self._table.selectionModel().selectedRows()
        first = rows[0].row() if rows else 0
        codes = self._rows()
        landed = below = 0
        for at, char in enumerate(chars):
            row = first + at
            if row >= len(codes):
                break
            if self._write(codes[row], char, GlyphRole.TEXT):
                landed += 1
            else:
                below += 1
        past = len(chars) - landed - below
        badge = _dropped_badge(below=below, past=past)
        if not landed:
            self.set_status("Nothing filled in.", badge)
            return
        self._rebuild()
        self._emit(f"paste {landed} character{'s' if landed != 1 else ''}")
        self.set_status(f"{landed} filled in", badge)

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
        self._rebuild()
        self._emit(f"fill with {name}")

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
        written in the bracketed form the same parser reads, operand count
        included — ``7A=[speed, 1]`` — since that is the whole of what the row
        said (:func:`~celpix.core.font.split_params`).

        Codes that say nothing are left out rather than written as blanks: what
        a run has not reached is not a glyph spelling the empty string. So are
        the **negative** ones a base dialled below zero puts the run on: the form
        writes a code as hex and has no spelling for a sign, so such a line would
        come back off the clipboard as no line at all — and nothing can be stored
        there anyway (:meth:`_write`).
        """
        merged = self._merged()
        codes, _bounded = self._span()
        lines = []
        for code in sorted(code for code in codes if code >= 0 and code in merged):
            glyph = merged[code]
            spelling = glyph.text if glyph.spells else f"[{_spelling(glyph)}]"
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
        run = list(self._chars.ljust(self._run_slots(), HOLE))
        for code in span:
            at = code - self._base
            if 0 <= at < len(run):
                run[at] = HOLE
        named = [glyph for glyph in self._codes if glyph.code not in span]

        def place(
            code: int,
            text: str,
            role: GlyphRole = GlyphRole.TEXT,
            params: int = 0,
        ) -> bool:
            # A prepended row below zero is a row and not a code, so it takes
            # nothing here either — the same refusal :meth:`_write` makes for the
            # table's own typing, since both end up in the same two fields.
            if code < 0:
                return False
            at = code - self._base
            if role is GlyphRole.TEXT and len(text) == 1 and 0 <= at < len(run):
                run[at] = text
            else:
                # ``params`` comes with the line: ``7A=[speed, 1]`` states the
                # count as part of the name, and dropping it here would make a
                # pasted table say less than the one it was copied from.
                named.append(Glyph(code, text, role, params=params))
            return True

        landed = below = 0
        if glyphs:
            rows = set(self._rows())
            aimed = 0
            for glyph in glyphs:
                if glyph.code in span or (not bounded and glyph.code not in rows):
                    aimed += 1
                    if place(glyph.code, glyph.text, glyph.role, glyph.params):
                        landed += 1
                    else:
                        below += 1
            past, outside = 0, len(glyphs) - aimed
        else:
            # Uneven on purpose, both ways: a short string fills part of the
            # span and leaves the rest of it cleared, a long one stops at the
            # span's end — and what it stopped short of is counted below rather
            # than dropped quietly.
            for code, char in zip(codes, chars, strict=False):
                if place(code, char):
                    landed += 1
                else:
                    below += 1
            past, outside = len(chars) - landed - below, 0
        badge = _dropped_badge(below=below, past=past, outside=outside)
        if not landed:
            self.set_status("Nothing pasted.", badge)
            return
        self._chars = "".join(run).rstrip(HOLE)
        self._codes = tuple(sorted(named, key=lambda g: g.code))
        self._rebuild()
        self._emit(f"paste {landed} code{'s' if landed != 1 else ''}")
        self.set_status(f"{landed} codes pasted.", badge)

    def _on_base_changed(self, value: int) -> None:
        """Slide the run along the code space — its own step per settled value.

        ``keyboardTracking`` is off on this spin (:func:`~celpix.ui.widgets.
        hex_spin`), so holding the arrow key reports once at the end and the undo
        stack gets the gesture instead of the path it took.
        """
        if self._syncing or value == self._base:
            return
        self._base = value
        self._rebuild()
        self._emit(f"set base code to ${value:X}")

    def _on_prepend_changed(self, value: int) -> None:
        """List ``value`` more rows below the sheet — a step of its own.

        Nothing already typed moves: these rows are outside the run either way,
        so what the spin changes is which of them have somewhere to be shown.
        Dialling it *down* past a named code does not delete the code — it keeps
        its row, appended (:meth:`_rows`), because a code with an answer must
        stay reachable however the table is framed.
        """
        if self._syncing or value == self._prepend:
            return
        self._prepend = value
        self._extra_rows_changed("prepend", value)

    def _on_append_changed(self, value: int) -> None:
        """List ``value`` more rows above the sheet. :meth:`_on_prepend_changed`."""
        if self._syncing or value == self._append:
            return
        self._append = value
        self._extra_rows_changed("append", value)

    def _extra_rows_changed(self, which: str, value: int) -> None:
        """The half the two spins share: redraw, and report the gesture once."""
        self._rebuild()
        self._emit(f"{which} {value} row{'s' if value != 1 else ''}")

    def _emit(self, label: str) -> None:
        self.edited.emit(
            self._base,
            self._prepend,
            self._append,
            self._chars,
            self._codes,
            label,
        )

    # -- the two readings following each other -----------------------------
    def _on_tile_selected(self, tile_id: int) -> None:
        # Only a pick made **on the sheet** moves the table. The sheet reports a
        # selection it was handed the same way it reports a click, so following
        # this one back would answer a row selection with `selectRow` — which
        # takes a stretch of picked rows down to the one that pushed the tile,
        # and the clipboard buttons read that stretch (:meth:`_span`).
        if not self._syncing and tile_id in self._ids:
            self._select_row(self._sheet_row() + self._ids.index(tile_id))
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
        first = self._sheet_row()
        picked = [
            self._ids[at]
            for index in sorted(rows, key=lambda index: index.row())
            if 0 <= (at := index.row() - first) < len(self._ids)
        ]
        if not picked:
            return
        self._syncing = True
        try:
            self._sheet.select_ids(picked)
        finally:
            self._syncing = False

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
        self._syncing = True
        try:
            self._table.setCurrentCell(
                row,
                COL_TEXT,
                QItemSelectionModel.SelectionFlag.ClearAndSelect
                | QItemSelectionModel.SelectionFlag.Rows,
            )
            item = self._table.item(row, COL_TEXT)
            if item is not None:
                self._table.scrollToItem(item)
        finally:
            self._syncing = False

    def _on_zoom(self, steps: int, _at: object) -> None:
        """Ctrl+wheel over the sheet, clamped to what the dock's own spin allows."""
        self._sheet.set_zoom(max(1, min(8, self._sheet.zoom + steps)))

    def closeEvent(self, event) -> None:  # noqa: ANN001 — QCloseEvent
        """Closing says the user does not want this window, which is reported.

        :meth:`hide_overlay` is celPix putting it away and means nothing of the
        sort, which is why only this one speaks up. The pan goes down either way:
        a space release landing anywhere else is one this window never sees, and
        the sheet would come back holding an open hand.
        """
        self._sheet.set_pan_mode(False)
        self.dismissed.emit()
        super().closeEvent(event)


# Why a character or a code the clipboard offered found no home, in the order
# the sentences read. Each names a different fix — Base code, Append or picking
# other rows — so reporting one as another sends the user to the wrong control,
# and a paste that lost characters two ways says both.
_DROP_REASONS: tuple[tuple[str, str], ...] = (
    (
        "below",
        "Some rows are below code zero, so nothing\ncould be written to them.",
    ),
    (
        "past",
        "The paste ran past the last row it could\nreach, so those were not written.",
    ),
    (
        "outside",
        "Some codes fall outside the selected rows,\nso those were not written.",
    ),
)


def _dropped_badge(*, below: int = 0, past: int = 0, outside: int = 0) -> Badge | None:
    """What a paste could not write, and which of :data:`_DROP_REASONS` each was.

    None where everything landed. The counts are kept apart all the way here
    rather than summed at the call site, because the total is the caption and the
    reasons are the sentence — and a paste is routinely refused two ways at once.
    """
    counts = {"below": below, "past": past, "outside": outside}
    why = [sentence for key, sentence in _DROP_REASONS if counts[key]]
    if not why:
        return None
    return Badge(f"{sum(counts.values())} dropped", "\n".join(why), warning=True)


def _code_label(code: int) -> str:
    """A row's code, as the Code column writes it — ``$1A``, or ``-$04`` below zero.

    The sign goes outside the ``$`` because ``$-1A`` reads as a hex digit soup
    where ``-$1A`` reads as "four below the origin", which is the only thing a
    negative row is ever saying.
    """
    return f"-${-code:02X}" if code < 0 else f"${code:02X}"


def _code_column_width(table: QTableWidget, codes: Iterable[int]) -> int:
    """How wide the Code column has to be to hold ``codes`` — and *all* of them.

    Measured off the labels rather than asked of the header, which is the whole
    reason that column is Fixed: `resizeColumnToContents` samples the rows near
    the top and a font's codes get **longer** further down (``$121`` at the first
    tile, ``$1121`` four thousand rows later), so a sampled width clips exactly
    the rows nobody has scrolled to yet.

    The longest label stands in for the widest one. They are hex digits in one
    font, so the two differ by less than the padding either way — which is the
    delegate's own margins, both sides, and is what a measured string alone does
    not cover.
    """
    metrics = table.fontMetrics()
    widest = max((_code_label(code) for code in codes), key=len, default="")
    heading = table.horizontalHeaderItem(COL_CODE)
    return (
        max(
            metrics.horizontalAdvance(widest),
            metrics.horizontalAdvance(heading.text() if heading else ""),
        )
        + CODE_COLUMN_PADDING
    )


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
    (:meth:`~celpix.core.font.FontAlphabet.decode`).
    """
    taken = {glyph.text for glyph in glyphs}
    if BREAK_NAME not in taken:
        return BREAK_NAME
    return next(name for n in count(1) if (name := f"{BREAK_NAME}-{n}") not in taken)


def _spelling(glyph: Glyph | None) -> str:
    """What the Text column shows for ``glyph`` — and what may be typed back in.

    A command that swallows cells shows the count beside its name, ``speed, 1``,
    which is the same spelling the table form writes as ``7A=[speed, 1]``
    (:func:`~celpix.core.font.split_params`). One cell rather than a fourth
    column, because the count is part of *what the code is called* in every other
    place a font table is written down, and a column that is empty on every row
    but four is a column the eye has to skip past on all of them.
    """
    if glyph is None:
        return ""
    if glyph.params and not glyph.spells:
        return f"{glyph.text}, {glyph.params}"
    return glyph.text


def _label_of(role: GlyphRole) -> str:
    """The column's caption for ``role`` — its own spelling where there is none.

    Total on purpose. A role this table has no caption for is a table that has
    fallen behind the model, and the honest cost of that is one row reading as
    its on-disk word; taking the whole window down over it costs the user every
    other row as well.
    """
    return next(
        (label for value, label in ROLE_LABELS if value is role), str(role.value)
    )


def _role_of(label: str) -> GlyphRole:
    return next(
        (value for value, caption in ROLE_LABELS if caption == label), GlyphRole.TEXT
    )
