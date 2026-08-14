"""The text window — a fontmap's cells read, and typed, as words.

A fontmap draws on the canvas as what it is: a grid of glyph tiles. That picture
is correct and nearly useless for the one thing the entry exists for, because
reading a sentence off a tile grid means reading it a character at a time in
whatever width the view happens to be set to. This window is the second reading —
the same cells as text — and the place they are edited by typing.

It is a `Qt.Tool` window on the decompression overlay's pattern
(:mod:`celpix.ui.decompress_overlay`): floats above the main window, takes no
taskbar slot, placed beside it on the first show and left where the user drags it
after. Below the text sits the same status bar and the same
:class:`~celpix.ui.widgets.Badge` those two carry, and for the same reason — the
text cannot show that one character of it has no code in this font, so it is said
in words.

**Presentation only.** It holds a string, the unit map that came with it, a
command list and a budget; it never reads the model and never encodes anything.
The main window hands it decoded text and takes back an edited string
(:mod:`celpix.ui.main_window.text`).

Four rules the window itself enforces, each of which is about not lying:

- **The field is typed over, not typed into.** A text region is a fixed run of
  cells, so one keystroke replaces one *piece* of the string — a letter, a
  newline, or a whole ``[$FE]`` code, since each is one cell — and Backspace
  blanks a piece to a space rather than closing the gap, so the budget never
  moves in either direction. **Insert** on the status bar turns that off and
  gives back an ordinary text field, which is the mode that has to be asked for
  because every key in it spends a cell the region may not have — and asked for
  *again* next session, since unlike **Wrap** it is not remembered. Either way
  every key goes through :meth:`TextWindow.put` and friends rather than through
  ``QPlainTextEdit``; the widget's own editing is switched off entirely, undo
  included, because a field editing behind celPix's back would be a second
  editor disagreeing with this one.
- **Inside a ``[...]`` the overtyping stops.** There the user is spelling a
  number, digit by digit, and there is nothing to replace until it is finished —
  so the keystroke edits the string, nothing is written, and the write happens
  when the caret leaves (:func:`~celpix.core.font.inside_code`). On the ``[``
  itself the caret is standing beside a whole piece, so typing there replaces the
  pair.
- **A step is a run of typing, not a keystroke.** Each edit is reported at once
  so the canvas and the file follow the caret, but consecutive keys merge into
  one undo step; a click, an arrow key, a command button or leaving the field
  breaks the run, and the next key starts a fresh one. **The caret is part of
  the step**, which is what makes a key that changes nothing — the letter
  already there, typed over itself — still one: the cells stand still, the
  caret does not, and an undo owes the user the place in the string they were
  working in as much as the string itself (:meth:`TextWindow.set_caret`).
- **The budget is characters of the file, not of the string.** A text region is a
  fixed run of cells, and the main window keeps it exactly full — cutting off what
  runs past the end and filling what a string gives up. The readout here is live
  so the cost of what is being typed is visible while it is being typed, and it
  says out loud when a word has been pushed off the end to pay for it.
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import (
    QFontDatabase,
    QGuiApplication,
    QKeySequence,
    QShortcut,
    QTextCursor,
)
from PySide6.QtWidgets import (
    QCheckBox,
    QGridLayout,
    QLabel,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from celpix.core.font import (
    BLANK,
    carried_break,
    inside_code,
    splice,
    unit_bounds,
    unit_spans,
)
from celpix.ui.widgets import (
    Badge,
    apply_badge,
    load_bool_setting,
    save_bool_setting,
)
from celpix.ui.window_layout import WindowLayout

# How many command buttons the insert row lays out before it stops. A format with
# a hundred named commands would otherwise build a hundred buttons, and the grid
# they fold into would take the window over; past this every one is still typeable
# as its own hex, which is the form the text uses anyway. Chosen to comfortably
# hold a full control-code table, which is what the row is really for.
MAX_COMMAND_BUTTONS = 48

# QSettings key for the wrap switch. A local preference like the grid's
# (``main_window/window.py``): whether you want a long string folded to the
# window or laid out as the file's own lines says how you are reading it right
# now, and carrying it in the .celpix would mean opening someone else's project
# rewrapped your text. Off by default, because a fontmap's lines are content —
# the breaks in it are codes in the file, and wrapping hides which is which.
WORD_WRAP_KEY = "view/text_word_wrap"

# The typing mode is deliberately **not** remembered, unlike the switch above it.
# Overtyping is the rule that suits what a text region *is* — a fixed run of
# cells — and insertion is the one that has to be asked for, since every key in
# it moves the string closer to the end of a region it may not pass. A mode that
# dangerous should be a thing you turned on, not a thing a session last week left
# on: the cost of it being wrong is text pushed off the end, and the cost of
# re-ticking a box is one click.


class _CommandGrid(QWidget):
    """The insert row, folded to the window's width.

    A grid rather than a strip that scrolls sideways: the commands are the
    format's whole vocabulary for punctuating this string, and one that has
    scrolled out of sight is one the user has to go hunting for — a hidden name
    is no better than an unlisted one. So every button is on screen at once and
    the window grows a line at a time instead.

    Columns are uniform and as wide as the widest caption needs, which keeps a
    control table reading as a table; how many of them fit is the width divided
    by that, recomputed as the window is dragged. The buttons stretch to fill,
    so the last row of a short list lines up with the ones above it rather than
    ending in a ragged edge.
    """

    def __init__(self, spacing: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setSpacing(spacing)
        self._spacing = spacing
        self._buttons: list[QPushButton] = []
        self._columns = 0
        # Vertically Minimum: the height is whatever the rows come to, and the
        # text field above keeps the rest. Nothing here is worth a pixel the
        # string could have had.
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)

    @property
    def layout_(self) -> QGridLayout:
        """The grid itself — the buttons in the order they were given."""
        return self._grid

    def set_buttons(self, buttons: list[QPushButton]) -> None:
        for button in self._buttons:
            self._grid.removeWidget(button)
            button.setParent(None)
            button.deleteLater()
        self._buttons = buttons
        for button in buttons:
            # Expanding, so a column wider than the caption is filled rather than
            # leaving the button floating in the middle of its cell.
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._columns = 0  # nothing is placed yet, whatever the count was before
        self._reflow()

    def resizeEvent(self, event) -> None:  # noqa: ANN001, N802 — Qt override
        super().resizeEvent(event)
        self._reflow()

    def _reflow(self) -> None:
        """Lay the buttons out in as many columns as the current width holds."""
        columns = self._fits()
        if columns == self._columns:
            return
        self._columns = columns
        # Taken out before being put back: adding a widget the layout already
        # manages to a second cell leaves it in both, and the row it came from
        # keeps its old height.
        for button in self._buttons:
            self._grid.removeWidget(button)
        for at, button in enumerate(self._buttons):
            self._grid.addWidget(button, *divmod(at, columns))
        for column in range(self._grid.columnCount()):
            self._grid.setColumnStretch(column, 1 if column < columns else 0)

    def _fits(self) -> int:
        """How many uniform columns the width holds — at least one, at most all."""
        if not self._buttons:
            return 1
        widest = max(button.sizeHint().width() for button in self._buttons)
        step = widest + self._spacing
        return max(1, min(len(self._buttons), (self.width() + self._spacing) // step))


class TextWindow(QWidget):
    """Presentation-only floating view of a fontmap's text, typed over in place."""

    #: The body after an edit, with whether it starts a new undo step, what to
    #: call that step, and where the caret stood before and after it. The caret
    #: travels with the edit because it is part of it: an undo of typing has to
    #: put the caret back where the typing started, or the string comes back
    #: without the place in it the user was working (:meth:`set_caret`).
    committed = Signal(str, bool, str, int, int)
    #: The body mid-composition — a ``[...]`` still being spelled. Nothing may be
    #: written from it; it exists so the budget readout keeps up with the keys.
    drafted = Signal(str)
    #: What is selected, as ``(first, last)`` character offsets into the body the
    #: window was given — ``last`` exclusive, and equal to ``first`` for a bare
    #: caret. The canvas mirrors it, so a phrase picked out here is the run of
    #: cells highlighted there and not just the one the caret sits in.
    caret_moved = Signal(int, int)
    #: Ctrl+Z / Ctrl+Y arrived here rather than at the main window, because a
    #: `Qt.Tool` window is the active one and window-context shortcuts follow it.
    undo_requested = Signal()
    redo_requested = Signal()
    #: The user shut the window from its own frame. Distinct from being hidden
    #: because the entry stopped being a fontmap, which is celPix's decision and
    #: says nothing about whether they want to see text again.
    dismissed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent, Qt.WindowType.Tool)
        self.setWindowTitle("Text")
        self._positioned = False
        self._syncing = False
        self._title = ""
        # What the file says, what the field says, and which piece each character
        # of it belongs to. They agree except while a code is being composed or a
        # write has been refused, which is the one state ``show_text`` must not
        # overwrite (see :attr:`_drafting`).
        #
        # The **committed** string keeps its own unit map beside it, because a
        # draft is taken back by putting both of them back at once
        # (:meth:`_revert`): the map is one id per character of the body, and a
        # body restored under the draft's map is a field where every offset past
        # the edit names the wrong piece — one Backspace then reads two cells as
        # one and blanks a letter nobody touched.
        self._committed = ""
        self._committed_units: tuple[int, ...] = ()
        self._body = ""
        self._units: tuple[int, ...] = ()
        self._drafting = False
        self._fresh = True
        # Whether this stream ends a line with a bit on the last character rather
        # than with a code (:attr:`~celpix.core.font.FontAlphabet.flag_break`). The one
        # thing about the alphabet this window is told, and it earns it: it is what
        # decides whether Enter costs a cell (:meth:`put`).
        self._flag_break = False

        self._edit = _TextEdit(self)
        # A fixed-width font, which is the one presentation decision here and not
        # a cosmetic one: a fontmap's budget is counted in cells, and a
        # proportional face makes two lines of equal cost look unequal. It also
        # keeps a column of `[$FF]` padding reading as a column.
        self._edit.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self._edit.cursorPositionChanged.connect(self._on_caret)
        # Both, because neither alone is "what is selected": a Select All from a
        # caret already at the end moves the anchor and not the position, and a
        # plain click that only moves the caret changes no selection. They also
        # both fire for one gesture — a click that clears a selection — which is
        # what :attr:`_reported` is for.
        self._edit.selectionChanged.connect(self._on_caret)
        # The last span handed out, so one gesture is reported once. The canvas
        # re-renders on each of them, and a draft lands on the first.
        self._reported: tuple[int, int] = (0, 0)
        self._edit.left.connect(self._on_focus_out)

        self._guide = _CommandGrid(3, self)
        self._guide_row = self._guide.layout_
        # The captions, tokens and descriptions the row was last built from, so a
        # refresh that says the same thing leaves the buttons standing
        # (:meth:`_build_guide`).
        self._commands: list[tuple[str, str, str]] = []

        self._wrap = QCheckBox("Wrap")
        self._wrap.setToolTip(
            "Fold long lines to the window's width\n"
            "Off, a line ends where the string's own line-break\n"
            "code says it does, and nowhere else"
        )
        self._wrap.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._wrap.setChecked(load_bool_setting(WORD_WRAP_KEY, False))
        self._wrap.toggled.connect(self._on_wrap)
        self._apply_wrap(self._wrap.isChecked())

        # Named apart from :meth:`_insert`, which puts a command in the string:
        # an attribute and a method of one name is the attribute, so the row of
        # command buttons would bind its clicks to a checkbox and raise on every
        # one of them.
        self._insert_mode = QCheckBox("Insert")
        self._insert_mode.setToolTip(
            "Type into the string instead of over it\n"
            "Off, a key replaces the character it lands on and\n"
            "Backspace blanks one to a space, so the text always\n"
            "costs the cells its region has\n"
            "On, a run longer than its region cannot be written\n"
            "Always starts off"
        )
        self._insert_mode.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        self._status = QStatusBar()
        self._status.setSizeGripEnabled(False)
        self._badge = QLabel()
        self._badge.hide()
        self._status.addPermanentWidget(self._insert_mode)
        self._status.addPermanentWidget(self._wrap)
        self._status.addPermanentWidget(self._badge)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 0)
        layout.addWidget(self._edit, 1)
        layout.addWidget(self._guide)
        layout.addWidget(self._status)
        self.resize(460, 380)
        # A size the user set is worth keeping: this is the window a fontmap is
        # actually typed in, and the default is a starting point rather than an
        # answer. Its own key - a window is remembered by what it is for, not by
        # which instance of it is on screen.
        self._layout_memory = WindowLayout(self, "layout/text-window")
        # A remembered position counts as already placed (the tool windows'
        # shared rule, :mod:`celpix.ui.decompress_overlay`).
        self._positioned = self._layout_memory.restore()

        # Ctrl+Return writes a code still being spelled without leaving the field,
        # because Return itself is a line break in the text and a fontmap's line
        # breaks are part of what is being edited — there is no key here to spare.
        commit = QShortcut(QKeySequence("Ctrl+Return"), self)
        commit.activated.connect(self._commit)

    # -- presentation ------------------------------------------------------
    def show_text(
        self,
        title: str,
        body: str,
        units: tuple[int, ...],
        commands: list[tuple[str, str, str]],
        status: str,
        badge: Badge | None = None,
        *,
        force: bool = False,
        flag_break: bool = False,
    ) -> None:
        """Present a fontmap's decoded text (showing the window if hidden).

        ``units`` is the decode's cell per character
        (:attr:`~celpix.core.font.Text.positions`) — what says where one piece of
        the string ends and the next begins, and so what a keystroke replaces.

        The body is written **only when it differs from what the field has**, so
        the refresh this window's own edit just triggered does not yank the caret
        back through a string the user is still in. Where they disagree it is the
        *field* that wins: a code half spelled, or a write the budget refused, is
        the user's work in progress and re-reading the file over it would throw it
        away without saying so.

        ``force`` is the exception, and it is the one the caller can see and this
        window cannot — the cells changed for a reason that was not this field.
        An undo is the case that matters: it must land on the text as it lands on
        the canvas, and a draft that outlived it would go on offering to write a
        string the user just took back. A new entry forces for the same reason.

        ``flag_break`` says how this stream ends a line — see :attr:`_flag_break`.
        Set on every refresh rather than at binding time because it comes off the
        cell format, and the format is a picker the user can change under the open
        window.
        """
        self._flag_break = flag_break
        self.setWindowTitle(title)
        self._status.showMessage(status)
        apply_badge(self._badge, badge)
        self._build_guide(commands)
        force = force or title != self._title
        self._title = title
        if body == self._edit.toPlainText():
            self._body, self._units, self._drafting = body, units, False
        elif self._drafting and not force:
            pass
        else:
            self._body, self._units, self._drafting = body, units, False
            self._syncing = True
            try:
                at = self._edit.textCursor().position()
                self._edit.setPlainText(body)
                cursor = self._edit.textCursor()
                cursor.setPosition(min(at, len(body)))
                self._edit.setTextCursor(cursor)
            finally:
                self._syncing = False
            self._note_span()
            self._fresh = True
        self._committed, self._committed_units = body, units
        if not self.isVisible():
            if not self._positioned and self.parentWidget() is not None:
                anchor = self.parentWidget().frameGeometry().topRight()
                self.move(anchor + QPoint(12, 40))
                self._positioned = True
            self.show()

    def set_status(self, status: str, badge: Badge | None = None) -> None:
        """Update the budget line alone — what a keystroke changes."""
        self._status.showMessage(status)
        apply_badge(self._badge, badge)

    def set_read_only(self, read_only: bool) -> None:
        """Lock the field where the entry's cells cannot be written back."""
        self._edit.setReadOnly(read_only)

    @property
    def body(self) -> str:
        """What is in the field right now, written back or not."""
        return self._edit.toPlainText()

    def select_range(self, first: int, last: int) -> None:
        """Put the selection over ``[first, last)`` of the body, without echoing.

        Guarded against the caret signal so a selection arriving *from* the
        canvas does not bounce straight back out as a fresh caret position and
        move the canvas again.

        Scrolled to, because the canvas is a whole string at once and this field
        is not: picking a cell out of the picture is how a user asks *what does
        this say*, and an answer that is still six screens down has not been
        given. The run of typing ends for the same reason :meth:`set_caret` ends
        it — the caret has been moved by something that was not a keystroke, and
        the next one starts a step of its own.
        """
        self._syncing = True
        try:
            cursor = self._edit.textCursor()
            cursor.setPosition(max(0, min(first, len(self.body))))
            cursor.setPosition(
                max(0, min(last, len(self.body))),
                QTextCursor.MoveMode.KeepAnchor,
            )
            self._edit.setTextCursor(cursor)
            self._edit.ensureCursorVisible()
        finally:
            self._syncing = False
        self._note_span()
        self._fresh = True

    def set_caret(self, at: int) -> None:
        """Put the caret at ``at`` of the body — an undo step landing, not a move.

        The other half of the caret travelling with the edit (:attr:`committed`):
        a step made in this field puts it back where it was made, so Ctrl+Z hands
        back the place in the string as well as the string. Silent, like
        :meth:`select_range`, because the caller is mid-apply and moving the
        canvas is its own business.

        The run of typing ends here. What the undo took back is on the stack
        below, and a key pressed after it must start a step of its own rather
        than merge into the one the caret has just been pulled out of.
        """
        self._syncing = True
        try:
            cursor = self._edit.textCursor()
            cursor.setPosition(max(0, min(at, len(self.body))))
            self._edit.setTextCursor(cursor)
        finally:
            self._syncing = False
        self._note_span()
        self._fresh = True

    def hide_overlay(self) -> None:
        """Hide — the entry on screen is no longer a fontmap, or was closed."""
        if self.isVisible():
            self._commit()
            self.hide()

    # -- editing -----------------------------------------------------------
    def put(self, typed: str, *, label: str = "edit text") -> None:
        """Type ``typed`` at the caret — the one road every edit comes in by.

        Three behaviours, and which one applies is decided before anything is
        looked up:

        - **Inside a** ``[...]`` **with no selection**, the characters go in as
          themselves and join the code being spelled, whatever the mode says
          (:func:`~celpix.core.font.inside_code`). Nothing is written; the field
          is ahead of the file until the caret leaves.
        - **Overtyping** (the default), they replace whole pieces of the string —
          as many pieces as ``typed`` is worth, so one key costs one cell and a
          pasted word costs its length. Over a **selection** the whole of it is
          blanked to spaces first and the typing restarts at its head, which is
          the same gesture Backspace makes and for the same reason: the phrase
          that was there costs the cells it always did, however much of it is
          typed back.
        - **Inserting**, they go in beside what is there and the string grows,
          which is an ordinary text field and is why it has to be asked for: a
          region is a fixed run of cells and every key spends one it may not have.

        A selection is replaced piece-wise in **both** modes. Half a ``[$FE]`` is
        not something the string can hold, and that is a fact about the text form
        rather than about how the field is being typed in.

        **Enter on a flag-break format inserts, in either mode.** There a line
        break is a bit on the character before it and costs no cell at all, so
        overtyping one would spend a cell on something free — eating the letter
        under the caret and pulling the whole rest of the string a cell left. The
        rule overtyping exists to keep is that the length never moves, and
        inserting something weightless does not move it. It lands past the piece
        the caret is standing *inside*, since the bit is a whole cell's and a
        pair has no half to end a line on.

        **A bracket that could not close is dropped** (:meth:`_bracketing`), so
        the field cannot be typed into a shape that reads back as something else.
        """
        if self._edit.isReadOnly():
            return
        cursor = self._edit.textCursor()
        first, last = cursor.selectionStart(), cursor.selectionEnd()
        typed = self._bracketing(typed, first, first != last)
        if not typed:
            return
        if first == last and inside_code(self._body, first):
            self._splice(first, last, typed, label, unit=self._units[first - 1])
            return
        if first == last and typed == "\n" and self._flag_break:
            # Past the piece the caret stands **inside**, which is only ever more
            # than the caret itself where a piece reads wider than a character —
            # a code standing for a pair. The bit belongs to a whole cell and
            # there is no half of one for a line to end on, so putting it where
            # the caret literally is would split the pair into the two letters it
            # stands for and spend a second cell on them.
            start, stop = unit_bounds(self._units, first)
            at = stop if start < first < stop else first
            # Joined to the cell before it, which is the cell whose bit it will
            # become - so it reads back as one piece the moment the file is
            # re-decoded, and Backspace finds it as the carried break it is.
            self._splice(
                at,
                at,
                typed,
                label,
                unit=self._units[at - 1] if at else None,
            )
            return
        caret = None
        if first != last:
            start = unit_bounds(self._units, first)[0]
            stop = self._keep_break(start, unit_bounds(self._units, last - 1)[1])
            if not self.inserting:
                # The caret is named because the blanks are part of the edit and
                # not part of what was typed: it belongs after the letter, on the
                # first cell still to be replaced.
                spare = self._pieces(start, stop) - len(unit_spans(typed))
                caret = start + len(typed)
                typed += BLANK * max(0, spare)
        elif self.inserting:
            start = stop = first
        else:
            start, stop = unit_bounds(self._units, first, len(unit_spans(typed)))
            stop = self._keep_break(start, stop)
        self._splice(start, stop, typed, label, caret=caret)

    def _bracketing(self, typed: str, at: int, selecting: bool) -> str:
        """``typed`` with the brackets that could not close taken out of it.

        There are exactly two of those, and both are keys a user presses by
        accident on the way to something else:

        - **A** ``[`` **where one is already open**. The caret is spelling a code
          already; a second opener cannot start another inside it, and left in it
          turns the code being typed into a run of characters the font almost
          certainly has no glyphs for.
        - **A** ``]`` **where none is open**. There is nothing for it to close, so
          it is a literal ``]`` — which the text form has no way to write, since
          ``]`` only ever means "the code ends here".

        Walked rather than filtered, because whether a bracket can close depends
        on the ones before it: a whole ``[line-break]`` arriving from the insert
        row starts outside a code and is kept entire, while the ``]`` of it typed
        on its own is not. The walk starts from the caret's own context, so a
        ``]`` finishing a code the field already holds is kept too.

        Dropped rather than refused with a message: a stray bracket is a slip,
        and the honest response to a slip is for nothing to happen. Only the
        bracket goes — a paste carrying one keeps everything else, which is what
        makes pasting a sentence out of a disassembly work.

        A selection is being replaced, so there is no open code to be inside: the
        span is spent whole and what lands starts a fresh piece.
        """
        if not typed or self._edit.isReadOnly():
            return typed
        inside = not selecting and inside_code(self._body, at)
        kept: list[str] = []
        for char in typed:
            if char == "[":
                if inside:
                    continue
                inside = True
            elif char == "]":
                if not inside:
                    continue
                inside = False
            kept.append(char)
        return "".join(kept)

    def _keep_break(self, start: int, stop: int) -> int:
        """``stop`` pulled back off a line break the last cell in the span carries.

        The bit belongs to the **cell**, not to the letter that happens to carry
        it, so retyping or blanking that letter must not unend the line: the span
        an edit replaces stops before the newline, and the newline stays. Enter
        and Backspace are how a break comes and goes
        (:func:`~celpix.core.font.carried_break`).

        Left alone where the newline is the whole span — that is a break *code*,
        a cell of its own, and replacing it with a letter is an ordinary edit.
        """
        if stop - 1 > start and carried_break(self._body, self._units, stop - 1):
            return stop - 1
        return stop

    def erase(self, *, forward: bool) -> None:
        """Backspace or Delete, over the same pieces :meth:`put` types over.

        A whole ``[$FE]`` goes in one press, because it is one cell and half a
        code is not a thing the string can hold — except inside one, where the
        digits are being spelled and are deleted as themselves. The same holds
        for the other piece that reads wider than one character: a code standing
        for a pair goes whole, from either side and from between its letters.

        **Overtyping, a piece is blanked to a space rather than removed.** That is
        the half of the model a removal would break: the point of typing over a
        text region is that its length never moves, and a Backspace that closed
        the gap would pull the rest of the string one cell left and leave the tail
        cell holding whatever the file had there. Blanking edits the one cell the
        user was looking at, exactly as typing over it does.

        **A line break the cell carries is erased on its own**, and *removed*
        rather than blanked, in either mode. It costs no cell, so taking it back
        frees none — the letter that ended the line stays exactly where it was and
        only stops ending it. This is the one way to unend such a line, which is
        why it is answered before the piece rules below: those would blank the
        letter and the break together, which is two edits for one keypress.
        """
        if self._edit.isReadOnly():
            return
        cursor = self._edit.textCursor()
        first, last = cursor.selectionStart(), cursor.selectionEnd()
        caret = None
        at = first - 1 if not forward else first
        if first == last and carried_break(self._body, self._units, at):
            self._splice(at, at + 1, "", "delete text", caret=at)
            return
        if first != last:
            start = unit_bounds(self._units, first)[0]
            stop = self._keep_break(start, unit_bounds(self._units, last - 1)[1])
        elif inside_code(self._body, first):
            # Composing: one character at a time, as typed - except the opening
            # `[`, which takes the code with it. Deleting only the bracket would
            # leave the digits loose in the string as letters nobody typed.
            if not forward and self._body[first - 1] == "[":
                # Backspacing off the front of a code takes the code, which is a
                # whole piece - so overtyping blanks it like any other rather than
                # letting the one gesture inside a `[...]` shorten the string.
                start, stop = unit_bounds(self._units, first - 1)
                blank = "" if self.inserting else BLANK
            elif forward:
                start, stop, blank = first, min(first + 1, len(self._body)), ""
            else:
                start, stop, blank = first - 1, first, ""
            if start != stop:
                self._splice(start, stop, blank, "delete text", caret=start)
            return
        elif forward:
            start, stop = unit_bounds(self._units, first)
            stop = self._keep_break(start, stop)
        else:
            if not first:
                return  # nothing behind the caret to take back
            # The **whole** piece behind the caret, even where the caret is
            # standing inside it: a code that stands for a pair reads as two
            # characters and is one cell, and blanking only the half in front of
            # the caret left the other half in the string as a letter of its own
            # — which costs a second cell the region has not got and pushes the
            # tail of it off the end. The one thing held back is the line break
            # the piece carries, exactly as on the way forward.
            start, stop = unit_bounds(self._units, first - 1)
            stop = self._keep_break(start, stop)
            caret = start
        if start == stop:
            return
        if self.inserting:
            self._splice(start, stop, "", "delete text", caret=start)
            return
        # One space per piece blanked, so a selection costs the cells it covered
        # and the string still ends where the region does.
        blanks = BLANK * self._pieces(start, stop)
        self._splice(start, stop, blanks, "blank text", caret=caret)

    @property
    def inserting(self) -> bool:
        """Whether the field types into the string rather than over it."""
        return self._insert_mode.isChecked()

    def cut(self) -> None:
        """Take the selection to the clipboard and erase it as one piece-wise step."""
        cursor = self._edit.textCursor()
        if not cursor.hasSelection() or self._edit.isReadOnly():
            return
        # QTextCursor hands a selection back with U+2029 where the line breaks
        # are; the clipboard wants the newline the rest of the world writes.
        QGuiApplication.clipboard().setText(
            cursor.selectedText().replace("\u2029", "\n")
        )
        self.erase(forward=True)

    def paste(self) -> None:
        """Type the clipboard in, which is :meth:`put` over as many pieces as it is."""
        text = QGuiApplication.clipboard().text()
        if text:
            self.put(text, label="paste text")

    def break_run(self) -> None:
        """End the current run of typing, so the next key starts a fresh undo step.

        Called for every gesture that is *not* more typing — a click, an arrow
        key, a command button, focus leaving. Ctrl+Z should take back the word
        that was typed, and the thing that says where a word ended is the user
        having done something else.
        """
        self._fresh = True

    # -- internals ---------------------------------------------------------
    def _note_span(self) -> None:
        """Take what is selected as already reported — a move nobody made.

        Every place this window moves the cursor itself does so behind
        ``_syncing``, so no signal comes out of it; without this the *next*
        genuine move back to the span before it would look like no change at all
        and be swallowed by :meth:`_on_caret`'s guard. Read back off the widget
        rather than from what was asked for, since both setters clamp.
        """
        cursor = self._edit.textCursor()
        self._reported = (cursor.selectionStart(), cursor.selectionEnd())

    def _pieces(self, start: int, stop: int) -> int:
        """How many cells ``body[start:stop]`` occupies — its runs of one unit."""
        run = self._units[start:stop]
        return sum(1 for at, unit in enumerate(run) if not at or unit != run[at - 1])

    def _splice(
        self,
        first: int,
        last: int,
        typed: str,
        label: str,
        *,
        unit: int | None = None,
        caret: int | None = None,
    ) -> None:
        """Land one edit: into the field, into the unit map, and out as a signal.

        Through the cursor rather than by rewriting the whole field, so the scroll
        position and the rest of the string are left alone on every keystroke.

        ``caret`` defaults to the far end of what was typed, which is where a key
        leaves it. Backspace is the exception and names the near end, so that
        holding it walks left over the string rather than standing still on the
        space it just made.

        Where the caret stood *before* is read first and reported with the edit:
        it is where an undo of this step has to leave it.
        """
        was = self._edit.textCursor().position()
        self._body, self._units = splice(
            self._body, self._units, first, last, typed, unit=unit
        )
        caret = first + len(typed) if caret is None else caret
        self._syncing = True
        try:
            cursor = self._edit.textCursor()
            cursor.setPosition(first)
            cursor.setPosition(last, QTextCursor.MoveMode.KeepAnchor)
            cursor.insertText(typed)
            cursor.setPosition(caret)
            self._edit.setTextCursor(cursor)
        finally:
            self._syncing = False
        self._note_span()
        self._report(was, caret, label)

    def _report(self, was: int, caret: int, label: str) -> None:
        """Say what the edit was — a write, or a code still being spelled."""
        self._drafting = self._body != self._committed
        if inside_code(self._body, caret):
            self.drafted.emit(self._body)
        else:
            self.committed.emit(self._body, self._fresh, label, was, caret)
            self._fresh = False
        # A splice leaves the caret standing, never a selection: what was
        # selected has just been replaced by what was typed.
        self.caret_moved.emit(caret, caret)

    def _build_guide(self, commands: list[tuple[str, str, str]]) -> None:
        """Rebuild the insert row, or leave it alone when it already matches.

        Compared before rebuilding because this runs on every refresh of the
        entry underneath — once per edit — and tearing down a row of buttons the
        user may be reaching for is worse than the comparison costs.
        """
        wanted = commands[:MAX_COMMAND_BUTTONS]
        if wanted == self._commands:
            return
        self._commands = wanted
        buttons = []
        for name, code, description in wanted:
            button = QPushButton(name)
            # The tooltip carries what lands in the string, then whatever the
            # format author said the code *does* — the one thing about a command
            # that neither the caption nor the token can show, and the reason a
            # format states a description at all.
            button.setToolTip(
                f"Insert {code}" + (f"\n{description}" if description else "")
            )
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.clicked.connect(lambda _checked=False, text=code: self._insert(text))
            buttons.append(button)
        self._guide.set_buttons(buttons)

    def undo(self) -> None:
        """Ctrl+Z — take back the draft if there is one, the session step if not.

        A **draft** is the field being ahead of the file: a ``[...]`` half
        spelled, or a character this font has no code for. Nothing has been
        written for it, so it is not on the session's stack at all — and reaching
        past it into the stack undoes something the user was not looking at while
        leaving the half-typed code standing. That is what a backspace inside a
        code and one Ctrl+Z used to do: the code stayed broken and the *binding*
        came undone.

        So the draft goes first. It is the most recent thing the user did, which
        is what an undo means, and the step underneath is still one more Ctrl+Z
        away.
        """
        if self.body != self._committed:
            self._revert()
            return
        self.undo_requested.emit()

    def _revert(self) -> None:
        """Put the file's own string back in the field, discarding the draft.

        **The unit map goes back with it.** It is one id per character, so a
        committed body left under the draft's map is a field whose every offset
        past the edit names the wrong piece: the next Backspace reads two cells
        as one and blanks a letter nobody touched
        (:attr:`_committed_units`).
        """
        at = self._edit.textCursor().position()
        self._syncing = True
        try:
            self._edit.setPlainText(self._committed)
            cursor = self._edit.textCursor()
            cursor.setPosition(min(at, len(self._committed)))
            self._edit.setTextCursor(cursor)
        finally:
            self._syncing = False
        self._note_span()
        self._body, self._units = self._committed, self._committed_units
        self._drafting, self._fresh = False, True

    def _insert(self, code: str) -> None:
        """Put a named command at the caret, as a step of its own.

        A step of its own on both sides: the run of typing before it ends here,
        and the next key starts another. Clicking a button is a gesture the user
        will expect one Ctrl+Z to take back, whatever they were typing around it.

        **Refused inside a** ``[...]``. The caret is mid-way through spelling a
        code there, and a whole ``[wait]`` dropped into the middle of one makes a
        bracketed thing no reader can parse — where the button's entire promise
        is that what it writes is exactly what the string holds.
        """
        cursor = self._edit.textCursor()
        if cursor.selectionStart() == cursor.selectionEnd() and inside_code(
            self._body, cursor.selectionStart()
        ):
            self._edit.setFocus()
            return
        self.break_run()
        self.put(code, label=f"insert {code}")
        self.break_run()
        self._edit.setFocus()

    def _on_wrap(self, wrapped: bool) -> None:
        save_bool_setting(WORD_WRAP_KEY, wrapped)
        self._apply_wrap(wrapped)

    def _apply_wrap(self, wrapped: bool) -> None:
        self._edit.setLineWrapMode(
            QPlainTextEdit.LineWrapMode.WidgetWidth
            if wrapped
            else QPlainTextEdit.LineWrapMode.NoWrap
        )

    def _on_caret(self) -> None:
        if self._syncing:
            return
        cursor = self._edit.textCursor()
        span = (cursor.selectionStart(), cursor.selectionEnd())
        if span == self._reported:
            return
        self._reported = span
        # Leaving a half-spelled code is what finishes it: nothing was written
        # while the caret was inside, so this is where the string catches up.
        if self._drafting and not inside_code(self._body, cursor.position()):
            self._commit()
        self.caret_moved.emit(*span)

    def _on_focus_out(self) -> None:
        """Attention has moved off the field: the run ends and the draft lands."""
        self.break_run()
        self._commit()

    def _commit(self) -> None:
        """Write the body out, if the field is ahead of the file.

        The caret is reported unmoved, because from here it is: what lands is a
        ``[...]`` the user finished typing some keystrokes ago, and the place to
        come back to is the one the caret is standing in now.
        """
        if self._body != self._committed:
            at = self._edit.textCursor().position()
            self.committed.emit(self._body, True, "edit text", at, at)
            self._fresh = True

    def closeEvent(self, event) -> None:  # noqa: ANN001 — QCloseEvent
        """Closing is a write: a code left half-spelled is still an edit.

        It is also the one gesture that says the user does not want this window,
        which is why it is reported — :meth:`hide_overlay` is celPix putting it
        away and means nothing of the sort.
        """
        self._commit()
        self.dismissed.emit()
        super().closeEvent(event)


class _TextEdit(QPlainTextEdit):
    """The field, with its own editing taken out and handed to the window.

    Every key that would change the text is routed to :class:`TextWindow`
    instead, because celPix types *over* the string and ``QPlainTextEdit`` types
    into it — leaving both live would mean two editors with different rules
    writing to one buffer. Undo goes with them: the widget's history would record
    keystrokes celPix never applied, so it is switched off and Ctrl+Z is passed to
    the session's own stack (``docs/design/undo-redo.md``).

    Navigation, selection and copy are untouched — none of them changes the text.
    """

    #: Focus has gone elsewhere: the run of typing ends, and a code left half
    #: spelled is written out.
    left = Signal()

    def __init__(self, owner: TextWindow) -> None:
        super().__init__(owner)
        self._owner = owner
        self.setUndoRedoEnabled(False)

    def keyPressEvent(self, event) -> None:  # noqa: ANN001 — QKeyEvent
        if self.isReadOnly():
            super().keyPressEvent(event)
            return
        if event.matches(QKeySequence.StandardKey.Undo):
            self._owner.undo()
            return
        if event.matches(QKeySequence.StandardKey.Redo):
            self._owner.redo_requested.emit()
            return
        if event.matches(QKeySequence.StandardKey.Paste):
            self._owner.paste()
            return
        if event.matches(QKeySequence.StandardKey.Cut):
            self._owner.cut()
            return
        key = event.key()
        if key in (Qt.Key.Key_Backspace, Qt.Key.Key_Delete):
            self._owner.erase(forward=key == Qt.Key.Key_Delete)
            return
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            # Ctrl+Return is the window's "write it now" shortcut, so it must
            # reach the window rather than land in the string as a line break.
            if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                event.ignore()
                return
            self._owner.put("\n")
            return
        typed = event.text()
        if typed and typed.isprintable():
            self._owner.put(typed)
            return
        super().keyPressEvent(event)
        self._owner.break_run()

    def insertFromMimeData(self, source) -> None:  # noqa: ANN001 — QMimeData
        """Every other way text arrives: a drop, a middle click, the menu's Paste.

        One override covers all three because ``QPlainTextEdit`` funnels them
        here — and all three would otherwise write to the buffer behind the
        window's back, leaving its idea of the string one edit out of date and
        every offset after that pointing at the wrong character.
        """
        text = source.text()
        if text:
            self._owner.put(text, label="paste text")

    def contextMenuEvent(self, event) -> None:  # noqa: ANN001 — QContextMenuEvent
        """A menu of this window's own operations, not the widget's.

        The standard one offers Cut, Delete and an Undo that would each edit the
        buffer directly — the same desync :meth:`insertFromMimeData` guards
        against, and here there is no funnel to catch them in.
        """
        menu = QMenu(self)
        selected = self.textCursor().hasSelection()
        editable = not self.isReadOnly()
        for caption, slot, enabled in (
            ("Cu&t", self._owner.cut, selected and editable),
            ("&Copy", self.copy, selected),
            ("&Paste", self._owner.paste, editable),
            ("Select &All", self.selectAll, bool(self.toPlainText())),
        ):
            action = menu.addAction(caption)
            action.setEnabled(enabled)
            action.triggered.connect(slot)
        menu.exec(event.globalPos())

    def mousePressEvent(self, event) -> None:  # noqa: ANN001 — QMouseEvent
        super().mousePressEvent(event)
        self._owner.break_run()

    def focusOutEvent(self, event) -> None:  # noqa: ANN001 — QFocusEvent
        super().focusOutEvent(event)
        # Through a signal and not a call, unlike everything else here: focus
        # leaves during teardown too, and by then the window this field belongs
        # to may already be gone. A connection is dropped with its receiver; a
        # method call on it is not.
        self.left.emit()
