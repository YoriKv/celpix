"""The Font Alphabet window's working copy: an alphabet being edited, Qt-free.

The window (:mod:`celpix.ui.font_alphabet_window`) holds one font's alphabet
while the user edits it — the origin, how far past the sheet the table lists, the
positional run and the named codes — and every gesture it offers is a change to
those five values. This is them, and the rules every change follows: which half
of the storage a code's answer lands in, which rows the table lists, what a
fill-down or a paste did with each character it was handed. None of it needs a
widget, so none of it is written against one; the window turns clicks into these
calls and the results back into a table and a status line.

**Two halves of the storage, one table on screen.** A row is written back to the
positional run when its code is inside the run, its role is text and its text is
one code point; anything else is written as a named code
(``docs/design/fontmap-entry.md`` §4).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from itertools import count

from celpix.core.font import (
    HOLE,
    Glyph,
    GlyphRole,
    format_code,
    spell_name,
    split_params,
)

__all__ = [
    "BREAK_NAME",
    "ROLE_LABELS",
    "AlphabetDraft",
    "Placed",
    "fresh_break_name",
    "label_of",
    "role_of",
    "spelling",
]

# How the roles are captioned. The enum's own spellings are the on-disk ones and
# read as jargon in a cell; these are what the column offers.
ROLE_LABELS: tuple[tuple[GlyphRole, str], ...] = (
    (GlyphRole.TEXT, "text"),
    (GlyphRole.DICT, "dict"),
    (GlyphRole.BREAK, "line break"),
    (GlyphRole.CONTROL, "control"),
)

# What an unnamed break is called when the Role column makes it one. A break has
# to read as *something* — a name is how the text spells the code back
# (``docs/design/fontmap-entry.md`` §5) — and there is only one word anyone wants
# for it, so it is written rather than demanded.
BREAK_NAME = "br"


@dataclass(frozen=True)
class Placed:
    """What a fill or a paste did with what it was handed, counted by fate.

    ``landed`` was written. The rest were not, and each for a reason with its
    own fix — which is why they are kept apart rather than summed: ``below``
    aimed at a row under code zero (Base code), ``past`` ran off the last row
    (Append), ``outside`` named a code outside the picked rows (pick others).
    """

    landed: int = 0
    below: int = 0
    past: int = 0
    outside: int = 0


@dataclass
class AlphabetDraft:
    """One font's alphabet as the window is editing it.

    ``base`` is the code the first tile draws; ``prepend`` and ``append`` how
    many rows the table lists before the first tile and after the last;
    ``chars`` the positional run (:data:`~celpix.core.font.HOLE` for a slot that
    says nothing) and ``codes`` the named codes. ``tiles`` is how many tiles the
    sheet shows and ``digits`` how wide a code prints
    (:attr:`~celpix.core.font.FontAlphabet.code_digits`) — the two facts about
    the entry a working copy has to be told rather than holding itself.
    """

    base: int = 0
    prepend: int = 0
    append: int = 0
    chars: str = ""
    codes: tuple[Glyph, ...] = ()
    tiles: int = 0
    digits: int = 2

    def code_label(self, code: int) -> str:
        """A row's code, as the Code column writes it (:func:`format_code`)."""
        return format_code(code, self.digits)

    def merged(self) -> dict[int, Glyph]:
        """Every code that says something, named codes winning over the run.

        The merge the pipeline performs, done here so the table shows what the
        text will. Built once per redraw and handed down rather than asked per
        row: a sheet is routinely a thousand tiles and a font a hundred named
        codes, and asking each row to scan the list made a keystroke quadratic.
        """
        merged = {
            self.base + at: Glyph(self.base + at, char)
            for at, char in enumerate(self.chars)
            if char != HOLE
        }
        merged.update({glyph.code: glyph for glyph in self.codes})
        return merged

    def run_slots(self) -> int:
        """How many codes the **positional run** covers — its length or the sheet's.

        Not simply the sheet's :attr:`tiles`, and the difference is where a code is
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
        return max(self.tiles, len(self.chars))

    def first_row_code(self) -> int:
        """The code the table's first row holds — the sheet, less **Prepend**.

        **Not floored at zero**, so the spin always lists the rows it was asked
        for. Below the origin they read as ``-$04``, which is not a code any cell
        can hold — and saying so plainly is the point: Prepend is *how much
        headroom below the sheet to look at*, and it stays put while **Base code**
        is dialled, so those rows come into the code space as the origin rises
        rather than appearing and vanishing under the user's hands.

        Nothing is ever **stored** at a negative code (:meth:`write`).
        """
        return self.base - self.prepend

    def sheet_row(self) -> int:
        """Which row the **first tile** sits on — everything above it is prepended.

        Every row ⇄ tile conversion goes through here, since getting it wrong
        points the sheet at the wrong tile rather than failing outright.
        """
        return self.prepend

    def rows(self) -> list[int]:
        """Every code the table lists, in order.

        One row per tile, **Prepend** rows before them and **Append** rows after,
        then any named code still outside all of that — appended, in order, since
        a code that has been given an answer must have a row to show it on
        whatever the spins say. A code appears once however many of those reach
        it, and the prepended ones may be negative (:meth:`first_row_code`).

        Bounded by the spins because the alternative is 65 536 rows on a two-byte
        stream, and they default to none because the ordinary font has nothing
        outside its sheet.
        """
        first = self.first_row_code()
        stop = self.base + self.run_slots() + self.append
        run = list(range(first, max(first, stop)))
        span = set(run)
        return run + sorted(g.code for g in self.codes if g.code not in span)

    # -- editing -------------------------------------------------------------
    def write(self, code: int, text: str, role: GlyphRole) -> bool:
        """Land one code's answer in whichever half of the storage holds it.

        The run takes it when it can — inside the run's own extent
        (:meth:`run_slots`), one code point, no role — because that is the half
        a tile's position states, and keeping it there means the run stays the
        thing the sheet is a picture of. Everything else is a named code, and
        either way the other half is cleared of that code so the two can never
        disagree.

        **False, writing nothing, for a code below zero** — the rows Prepend
        lists under the origin (:meth:`first_row_code`). No cell can hold such a
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
        arrives (:meth:`settle_role`, from the window's cell edit) — a paste
        says so in its own form, and a fill-down never lands more than one
        character.

        **The description survives a retyped row.** It is the one field of a
        command no column shows — the sentence on its insert-row button — so
        rebuilding the glyph from what is on screen would quietly drop it every
        time somebody corrected a name or an operand count.
        """
        if code < 0:
            return False
        at = code - self.base
        in_run = 0 <= at < self.run_slots()
        params = 0
        if not role.spells:
            # ``speed, 1`` is a command and the cells it swallows, which is the
            # spelling the table form uses and the one the column shows back
            # (:func:`spelling`). Only a command can swallow anything, so this
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
        held = next((g for g in self.codes if g.code == code), None)
        self.codes = tuple(g for g in self.codes if g.code != code)
        if text and not positional:
            self.codes = tuple(
                sorted(
                    [
                        *self.codes,
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
        run = self.chars.ljust(at + 1, HOLE)
        self.chars = run[:at] + char + run[at + 1 :]

    def settle_role(
        self, text: str, role: GlyphRole, *, picked: bool
    ) -> tuple[GlyphRole, bool]:
        """The role one settled row stores, and whether it was guessed.

        **Several characters typed in are guessed to be a name** — it is what
        somebody typing ``wait`` into a cell meant, and it is wrong for somebody
        typing ``th``. So it applies to a Text edit alone (``picked`` is False):
        the Role column overrides it, and a row that already reads *dict* is left
        as it is.

        **The spelling settles text ⇄ dict**, whichever of the two was picked, so
        the role a row shows is never a role its text contradicts
        (:class:`~celpix.core.font.GlyphRole`) — which is also what stops the
        guess being made twice: a row that already reads *dict* is no longer a
        row whose role says ``text``, so correcting ``th`` to ``the`` leaves the
        role alone.
        """
        if not picked and role is GlyphRole.TEXT and len(text) > 1:
            return GlyphRole.CONTROL, True
        if role.spells:
            return (GlyphRole.DICT if len(text) > 1 else GlyphRole.TEXT), False
        return role, False

    def fill(self, first_row: int, chars: Sequence[str]) -> Placed:
        """Fill down from row ``first_row``, one character per code.

        Stops at the last row rather than growing the table: how far past the
        sheet this font is read is what the two spins say, and a paste is not an
        answer to that. A run started on a prepended row below zero **steps
        over** those rows rather than stopping at them: nothing can be stored
        there (:meth:`write`), and the characters that follow are still meant
        for the codes that come after.
        """
        codes = self.rows()
        landed = below = 0
        for at, char in enumerate(chars):
            row = first_row + at
            if row >= len(codes):
                break
            if self.write(codes[row], char, GlyphRole.TEXT):
                landed += 1
            else:
                below += 1
        return Placed(landed, below, len(chars) - landed - below)

    def shift(self, by: int) -> bool:
        """Move the run ``by`` tiles along the sheet; False where there is none.

        The **characters** move and the codes do not, which is what makes this a
        different control from Base code rather than a second spelling of it: a
        run pasted one tile out is corrected here, a run whose *origin* is out is
        corrected there, and the sheet is what tells the two apart.

        Shifting up drops the first character off the top rather than wrapping
        it round to the bottom: a run and a ring are not the same thing, and the
        character that falls off is the one the user can see fall off. The run
        moves only — a named code was read out of the stream at the value it
        has, so it no more follows a nudge than it follows the origin.
        """
        if not self.chars:
            return False
        moved = HOLE * by + self.chars if by > 0 else self.chars[-by:]
        self.chars = moved.rstrip(HOLE)
        return True

    def copy_lines(self, codes: Iterable[int]) -> list[str]:
        """``codes`` as ``20=A`` lines, the form a font table is kept in.

        Named codes are written in the bracketed form the same parser reads,
        operand count included — ``7A=[speed, 1]`` — since that is the whole of
        what the row said (:func:`~celpix.core.font.split_params`). Codes that
        say nothing are left out rather than written as blanks: what a run has
        not reached is not a glyph spelling the empty string. So are the
        **negative** ones: the form writes a code as hex and has no spelling for
        a sign, so such a line would come back as no line at all.
        """
        merged = self.merged()
        lines = []
        for code in sorted(code for code in codes if code >= 0 and code in merged):
            glyph = merged[code]
            text = glyph.text if glyph.spells else f"[{spelling(glyph)}]"
            lines.append(f"{code:0{self.digits}X}={text}\n")
        return lines

    def replace(
        self,
        codes: Sequence[int],
        bounded: bool,
        glyphs: Sequence[Glyph],
        chars: Sequence[str],
    ) -> Placed:
        """Replace ``codes`` from a pasted table — ``glyphs`` or a plain string.

        **Two forms.** ``20=A`` lines (``glyphs``) state their own codes, so they
        land where they say and the origin is left alone. A plain string of
        characters (``chars``, read when ``glyphs`` is empty) states none, so it
        lands one character per code down ``codes``.

        **Replaces**: the span is cleared first and leftover codes inside it come
        out blank. ``bounded`` says whether the span's far end is closed; an open
        one still takes a pasted code the table does not list, since a code past
        the tiles is the kind that gets named, where a closed one is the user
        pointing at rows. A paste that lands nothing changes nothing — wiping the
        rows would be the one reading of it nobody wants.
        """
        span = set(codes)
        # Everything outside the span is kept exactly as it stands; inside it the
        # old answers go before the new ones land, which is what makes this a
        # replace and not a merge.
        run = list(self.chars.ljust(self.run_slots(), HOLE))
        for code in span:
            at = code - self.base
            if 0 <= at < len(run):
                run[at] = HOLE
        named = [glyph for glyph in self.codes if glyph.code not in span]

        def place(
            code: int,
            text: str,
            role: GlyphRole = GlyphRole.TEXT,
            params: int = 0,
        ) -> bool:
            # A prepended row below zero is a row and not a code, so it takes
            # nothing here either — the same refusal :meth:`write` makes for the
            # table's own typing, since both end up in the same two fields.
            if code < 0:
                return False
            at = code - self.base
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
            rows = set(self.rows())
            aimed = 0
            for glyph in glyphs:
                if glyph.code in span or (not bounded and glyph.code not in rows):
                    aimed += 1
                    if place(glyph.code, glyph.text, glyph.role, glyph.params):
                        landed += 1
                    else:
                        below += 1
            placed = Placed(landed, below, 0, len(glyphs) - aimed)
        else:
            # Uneven on purpose, both ways: a short string fills part of the
            # span and leaves the rest of it cleared, a long one stops at the
            # span's end — and what it stopped short of is counted rather than
            # dropped quietly.
            for code, char in zip(codes, chars, strict=False):
                if place(code, char):
                    landed += 1
                else:
                    below += 1
            placed = Placed(landed, below, len(chars) - landed - below)
        if landed:
            self.chars = "".join(run).rstrip(HOLE)
            self.codes = tuple(sorted(named, key=lambda g: g.code))
        return placed


def fresh_break_name(glyphs: Iterable[Glyph]) -> str:
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


def spelling(glyph: Glyph | None) -> str:
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


def label_of(role: GlyphRole) -> str:
    """The column's caption for ``role`` — its own spelling where there is none.

    Total on purpose. A role this table has no caption for is a table that has
    fallen behind the model, and the honest cost of that is one row reading as
    its on-disk word; taking the whole window down over it costs the user every
    other row as well.
    """
    return next(
        (label for value, label in ROLE_LABELS if value is role), str(role.value)
    )


def role_of(label: str) -> GlyphRole:
    return next(
        (value for value, caption in ROLE_LABELS if caption == label), GlyphRole.TEXT
    )
