"""The text window's window side: what it is handed, and what comes back.

The window is presentation only (:mod:`celpix.ui.text_window`) — it holds a
string, a token list and a budget, and never reads the model. This is the half
that builds those from the live document and turns a committed string back into
cells.

Four things it has to get right, and none of them is the window's to know:

- **The region is always exactly full.** A text region is a fixed run of cells and
  the codes have nowhere else to go, so a string that encodes longer has the
  overrun **taken off the end** and one that encodes shorter has the cells it gave
  up **filled with the blank** (:func:`_text_blank`). Either way what is written is
  the length the file has, which is what makes an edit land on the canvas at all:
  a refusal leaves the picture showing the old string, and a preserved tail leaves
  it showing half of one. Text pushed off the end is **said out loud** when it was
  something rather than trailing space, since it is text the user will look for
  later and not find.
- **A write changes what the text form can say and nothing else.** That is the
  index, plus the terminator bit for the formats that end a line with one
  (:attr:`~celpix.core.tilemap.Cell.ends_line`) — a newline in the field *is*
  that bit. Everything else a cell carries is invisible from here — a palette
  row, a flip, a priority bit — so the cell that was there is kept and those two
  fields replaced on it. Rebuilding cells from the string would silently zero
  every one of the rest.
- **A run of typing is one step.** The window types over the string a piece at a
  time and reports every one, so the canvas and the file follow the caret — but
  the undo stack must not fill with letters. The run number is kept here, handed
  to each command, and bumped whenever the window says the run has ended.
- **A fontmap opens its text by itself.** The canvas can only ever show a string
  as the grid of glyph tiles it is, so the window is not a second look at
  something already legible — it is the reading. Closing it says so and turns
  that off for the session; View ▸ Text turns it back on (:meth:`_sync_text`).
"""

from __future__ import annotations

from dataclasses import replace

from celpix.core.font import BLANK
from celpix.ui.widgets import Badge


class TextMixin:
    """Building, gating and committing the fontmap text window.

    A slice of :class:`~celpix.ui.main_window.window.MainWindow`, not a
    standalone object: it reads the window's own widgets and its single live
    ``_doc``. See the package docstring for why these are mixins.
    """

    def _text_available(self) -> bool:
        """Whether this document has text worth opening a window on.

        The **declaration** and not the alphabet, which is the whole point of the
        format declaring it: a fontmap with no font bound reads as hex, and the
        text window is exactly where the user finds that out and picks one. An
        alphabet check here would hide the window in the one state that most
        needs explaining.
        """
        doc = self._doc
        return bool(doc is not None and doc.is_fontmap)

    def _show_text(self) -> None:
        """View ▸ Text — open the window, and mean it.

        Asking for it is also what puts the automatic opening back: the only way
        to have turned it off is to have closed the window, so the only sensible
        reading of opening it again is that you want it.
        """
        self._text_dismissed = False
        self._refresh_text()

    def _refresh_text(self, *, force: bool = False) -> None:
        """Decode this fontmap's cells and hand them over, showing the window."""
        if not self._text_available():
            self._text.hide_overlay()
            return
        doc = self._doc
        entry = self._workspace.current
        name = entry.name if entry is not None else "text"
        alphabet = doc.alphabet
        # The commands, captioned by name and inserting their own hex code -
        # which is what the text form has in it. The name never appears in the
        # string; it exists so the user is not left remembering that this game's
        # line break is $FE (``docs/design/fontmap-entry.md`` §5).
        commands = [
            (glyph.text, alphabet.hex_code(glyph.code))
            for glyph in (alphabet.commands if alphabet is not None else ())
        ]
        self._text.set_read_only(not doc.cells_editable)
        text = doc.text
        status, badge = self._text_status(text.body)
        self._text.show_text(
            f"Text - {name}",
            text.body,
            text.positions,
            commands,
            status,
            badge,
            # A command applying is the refresh the window must take over a
            # draft, and that includes its own write: the region is always exactly
            # full, so what came back is the typed string with whatever ran off
            # the end taken off and whatever it gave up filled in. An undo is the
            # case that matters most — a draft that outlived it would go on
            # offering to write a string the user had just taken back.
            force=force or self._applying_undo,
            # How this stream ends a line, which decides what Enter costs there
            # (:meth:`~celpix.ui.text_window.TextWindow.put`).
            flag_break=alphabet is not None and alphabet.flag_break,
        )
        # The readout is settled after the window has decided which of the two
        # strings won, and from whatever is actually on screen. Computing it up
        # there from the field would leave an undo showing the budget of the
        # draft it had just taken away.
        if self._text.body != text.body:
            self._text.set_status(*self._text_status(self._text.body))

    def _sync_text(self) -> None:
        """Open the window on a fontmap, and close it on anything else.

        **A fontmap opens its text.** Unlike the animation player, which is one
        reading of a picture that is already on screen, this is the *only* place a
        text run says what it says — the canvas shows a grid of glyph tiles, which
        is a correct picture and not a readable one. Landing on a string and being
        shown letter shapes in a grid, with the words a menu item away, is the one
        state the entry exists to avoid.

        **Closing it turns that off** for the session (:meth:`_show_text` turns it
        back on). A window that came back on the next refresh would be unclosable,
        and there is no reading of a user shutting a window that means "again".

        Called where the document changes rather than on a timer, the rule the
        animation player follows: the window holds its own copy of the string, so
        one left open over a different entry would go on offering to write text
        into a file that is not on screen.
        """
        if not self._text_available():
            self._text.hide_overlay()
            return
        if not self._text.isVisible() and self._text_dismissed:
            return
        self._refresh_text()

    def _on_text_dismissed(self) -> None:
        """The user shut the window: stop opening it for them until they ask."""
        self._text_dismissed = True

    def _text_status(self, typed: str | None) -> tuple[str, Badge | None]:
        """The budget line, and the badge for what the text itself cannot show.

        ``typed`` is what is in the field, or None to report the file as it
        stands. Two different things can be wrong and they are counted
        separately, because the fixes are different: a character with no code in
        this font is a *font* problem, and a string past the end of its region is
        a *length* problem.
        """
        doc = self._doc
        cells = len(doc.cells or []) if doc is not None else 0
        if doc is not None and doc.alphabet is None:
            return (
                f"{cells} cells - no alphabet",
                Badge(
                    "no alphabet",
                    "Nothing says what this font's codes mean, so every\n"
                    "one reads as hex. Pick an alphabet for the entry\n"
                    "supplying the tiles - the Alphabet box on the bar\n"
                    "below the canvas.",
                    warning=True,
                ),
            )
        if typed is None or doc is None or doc.alphabet is None:
            return f"{cells} cells", None
        encoded = doc.alphabet.encode(typed)
        used = len(encoded.codes)
        line = f"{used} / {cells} cells"
        if not encoded.ok:
            shown = ", ".join(repr(item) for item in encoded.unknown[:4])
            return line, Badge(
                f"{len(encoded.unknown)} not in font",
                "These have no code in this font, so the text cannot\n"
                "be written as typed:\n"
                f"  {shown}\n"
                "Remove them, or pick an alphabet that has them.",
                warning=True,
            )
        if used > cells:
            return line, Badge(
                f"{used - cells} over",
                "The text encodes to more cells than this map has,\n"
                "and a text region is a fixed run - there is nowhere\n"
                "for the extra codes to go. Shorten it, or carve a\n"
                "longer slice for the entry.",
                warning=True,
            )
        return line, None

    def _on_text_drafted(self, body: str) -> None:
        """A ``[...]`` still being spelled: the readout keeps up, the file waits.

        Half a code is not a number, so there is nothing here to write — but the
        budget line is what tells the user the code they are typing costs one
        cell like any other, and it would be stale until they finished it.
        """
        self._text.set_status(*self._text_status(body))

    def _on_text_committed(
        self, body: str, fresh: bool = True, label: str = "edit text"
    ) -> None:
        """One edit from the text window, landed on the cells.

        ``fresh`` starts a new undo step; without it this edit merges into the
        run of typing before it, which is what keeps Ctrl+Z taking back a word
        rather than a letter. The window owns that judgement because it is the
        only thing that sees the gesture — what ends a run is a click, an arrow
        key or a button, none of which reaches this far.

        **Length is not a failure.** The region is a fixed run of cells, so it is
        kept exactly full — what ran past the end comes off it, what the string
        gave up is filled with the blank — and the only thing that stops a write is
        a character the font has no code for, which is a *font* problem and cannot
        be fixed by cutting anything. That one is refused with the reason on the
        status bar, and the window keeps showing what the user has.
        """
        doc = self._doc
        if doc is None or not doc.is_fontmap or doc.alphabet is None:
            return
        if fresh:
            self._text_run += 1
        cells = list(doc.cells or [])
        encoded = doc.alphabet.encode(body)
        self._text.set_status(*self._text_status(body))
        if not encoded.ok:
            self.statusBar().showMessage(
                "Not written: "
                + ", ".join(repr(item) for item in encoded.unknown[:4])
                + " has no code in this font."
            )
            return
        codes = list(encoded.codes)
        # The terminator bit rides beside the index for the formats that have one
        # and is False everywhere else, so it is written unconditionally: a format
        # without the field has nowhere to put it and the codec drops it.
        ends = list(encoded.ends_line)
        lost = doc.alphabet.decode(codes[len(cells) :], ends[len(cells) :]).body
        codes = codes[: len(cells)]
        ends = ends[: len(cells)]
        codes += [self._text_blank()] * (len(cells) - len(codes))
        ends += [False] * (len(cells) - len(ends))
        for at, code in enumerate(codes):
            cells[at] = replace(cells[at], index=code, ends_line=ends[at])
        written = self._apply_cells(cells, label, run=self._text_run)
        if lost.strip():
            # Only when something was *said* is this worth interrupting for. A run
            # of trailing spaces pushed off the end is the region doing its job;
            # a word pushed off it is text the user will look for later and not
            # find, and they are owed the chance to undo before typing again.
            self.statusBar().showMessage(
                f"{lost.strip()!r} pushed off the end - the region holds "
                f"{len(cells)} cells and they are all in use."
            )
        elif written:
            self.statusBar().showMessage(f"Wrote {len(cells)} cells of text.")
        if not written:
            # Nothing changed, so no refresh is coming - and the field may be
            # holding text this region cannot: the keystroke that overflowed it
            # has to come back off the screen as well as off the cells.
            self._refresh_text(force=True)

    def _text_blank(self) -> int:
        """The code a cell the text no longer reaches is filled with.

        The font's space, and **zero** where it has none. A fontmap's region is
        always exactly full — a string typed shorter has to leave something in the
        cells it gave up, and there is no third answer that is not a letter celPix
        chose on the user's behalf.
        """
        alphabet = self._doc.alphabet
        encoded = alphabet.encode(BLANK)
        return encoded.codes[0] if encoded.ok and len(encoded.codes) == 1 else 0

    def _on_text_caret(self, offset: int) -> None:
        """Follow the text caret with the canvas selection.

        The two views show the same cells, and a caret in one is a position in
        the other — so finding a word in the text is how a user finds it on the
        canvas, which is the harder direction by far. One way only: the reverse
        would fight the caret while the user is typing, and the selection moves
        on every arrow key.
        """
        doc = self._doc
        if doc is None or not doc.is_fontmap or not doc.cells:
            return
        start, stop = doc.text.span_of(offset, offset)
        self._select_tiles(start, min(stop, len(doc.cells)) - 1)
        self._refresh_view()
