"""The fontmap's alphabet: character codes ⇄ readable text.

A **fontmap** is a tilemap whose cells are character codes rather than tile
numbers — a string of text stored in a ROM as references into a font
(``docs/design/fontmap-entry.md``). Everything about reading those cells is the
tilemap pathway's, unchanged; what this module adds is the one thing a tilemap
has no notion of: **what the codes say**.

A :class:`FontAlphabet` is that lookup, and it is deliberately two things merged:

- the **glyph table**, which belongs to the *font* — it says which tile draws
  which letter, so it is a fact about the art and not about any one string that
  uses it. Ten fontmaps over one font share it and cannot disagree.
- the **control codes**, which belong to the *text format* — how the stream is
  punctuated, where a line ends, where a string does. Two streams in one game
  routinely share a font and differ here, so this half rides on the fontmap's own
  cell format.

Both are :class:`Glyph`\\ s and both reach the decoder through one object, which
is why :meth:`FontAlphabet.merged` exists rather than the reader consulting two
tables and deciding which wins.

**The text form has four cases and no more**, and that is the whole design. A
glyph the font can spell reads as itself; a **line break** reads as an actual
newline, since without it the string's own line structure is invisible; a code
somebody has **named** reads as ``[line-break]``; and **everything else reads as**
``[$FF]`` — its own code, in hex.

A name is never guessed, so the third case costs nothing on a format nobody has
described and the fourth is still what every unnamed code takes. It is spelled to
one word (:func:`spell_name`) because it is what a reader retypes inside the
brackets.

A line break is not always a code. Some formats end a line by setting a **bit on
its last character**, so the character and the break are one cell; that bit
arrives beside the codes (:attr:`~celpix.core.tilemap.Cell.ends_line`) and reads
as a newline *after* whatever the code itself said. It is the same case, spelled
by the format in the only other place it could be.

That last case is what makes this general. There is no markup vocabulary to
learn, none per format, and nothing a game's text format has to be bent into: a
command this build has never heard of, an unmapped icon, a region of padding and
a string terminator all read the same honest way and all type straight back to
the same bytes. A format whose commands are worth naming names them, and the name
reaches the user as a captioned button on the insert row rather than as syntax in
the string.

**Nothing is ever dropped.** That is the same rule the cell model follows for a
priority bit it cannot render: a field dropped on the way in is a field silently
zeroed on the way out.

Qt-free, like the rest of ``core``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from enum import Enum

# ``[`` opens a code, so a literal one is doubled — the only escape in the text
# form, and the only character it costs anything to type.
ESCAPE = "[["

# What a cell the text no longer reaches is filled with, what a piece blanked by
# Backspace becomes, and what a character this font cannot spell costs
# (:attr:`FontAlphabet.blank`). A space is the one character a font drawn for
# words nearly always has; where a font has none there is no honest text for the
# cell and the writer falls back to code zero rather than picking some other
# letter.
BLANK = " "

# A slot of the positional run that draws no character. The run is one code point
# per tile, so absence needs a spelling of its own — a space is a real glyph, and
# leaving the slot out would move every letter after it onto the wrong tile. NUL
# because it is the one code point no font sheet ever draws.
HOLE = "\u0000"

# The editor's starting points, as ``(name, base, chars)``. These two runs are
# what the shipped alphabet presets held, kept as data because what a preset ever
# bought them was a name in a dropdown, and what a table being typed up wants is
# a *first draft* to correct.
#
# Each carries an origin only as a guess. Where a run starts is in the game's
# code and appears in neither the sheet nor the string, so the base is the thing
# to dial afterwards against the text window
# (``docs/graphics-formats-reference/text-formats.md`` §3.2). The uppercase run
# is first because it is the arrangement a sheet drawn for a machine with no
# lower case falls into, and so the one worth trying blind.
TEMPLATES: tuple[tuple[str, int, str], ...] = (
    ("A-Z 0-9, from 0", 0, "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 .,'!?-"),
    (
        "ASCII, from $20",
        0x20,
        " !\"#$%&'()*+,-./0123456789:;<=>?"
        "@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_"
        "`abcdefghijklmnopqrstuvwxyz{|}~",
    ),
)

# What a name may not carry. Brackets would end the token early, and whitespace
# would make ``[wait for input]`` three words where the form is one — a token is
# retyped by hand, and a space in one is a place to lose it.
_UNSPELLABLE_RE = re.compile(r"[\s\[\]]+")

_TOKEN_RE = re.compile(r"\[([^\[\]]*)\]")
_HEX_RE = re.compile(r"^[0-9A-Fa-f]+$")


def spell_name(name: str) -> str:
    """``name`` as it may appear inside ``[...]`` — one word, no brackets.

    Whitespace becomes a hyphen rather than being dropped, so "line break" reads
    as ``line-break`` and not ``linebreak``: a name is retyped by hand and the
    word boundary is what makes it readable at a glance. Normalized rather than
    refused, because the alternative is a format author's table losing a control
    over a space, and hyphenating it costs the reader nothing.
    """
    return _UNSPELLABLE_RE.sub("-", name.strip()).strip("-")


class GlyphRole(str, Enum):
    """What one glyph *does* — which decides how it reads and who declares it.

    Three, and no more, because there are only three answers a general reader can
    give about a code: it spells something, it ends a line, or it is a command
    this build has no business interpreting. A richer set would mean guessing at
    one game's conventions and imposing them on every other.

    ``TEXT`` is the **font's** — a glyph is a glyph whatever stream it appears in.
    ``BREAK`` and ``CONTROL`` are the **text format's**: they say how the stream
    is punctuated, and two streams sharing a font routinely punctuate
    differently.

    ``str``-valued so a preset states a role as itself and the on-disk spelling
    is the enum.
    """

    TEXT = "text"  # types verbatim: "A", or a "th" pair one code stands for
    BREAK = "break"  # ends a line, and reads as a newline
    CONTROL = "control"  # anything the game acts on: reads as its own hex code

    @property
    def spells(self) -> bool:
        """Whether this role puts characters on screen rather than punctuating."""
        return self is GlyphRole.TEXT

    @classmethod
    def parse(cls, value: object) -> GlyphRole:
        """``value`` as a role, falling back to TEXT for anything unknown.

        A hand-authored or newer preset names a role this build has no meaning
        for; reading it as an ordinary character keeps the rest of the table
        usable, which is better than refusing the whole alphabet over one line.
        """
        try:
            return cls(value)
        except ValueError:
            return cls.TEXT


@dataclass(frozen=True, slots=True)
class Glyph:
    """One entry of the alphabet: what a code reads as.

    One code, deliberately. A format that spends several codes on one drawn thing
    is describing its own arrangement of tiles, and there is no reading of that
    which stays true across games — so each code is read on its own and the
    grouping, if it matters, is visible in the picture where it belongs.

    ``text`` is what a ``TEXT`` glyph reads as, and it may be **several
    characters**: that is what a code standing for a common pair is, and dropping
    it would cost the one compression trick fixed-size text regions actually use.
    For the other two roles it is the **name**, and the name *is* what the string
    holds — ``[line-break]`` — so it carries **no spaces**
    (:func:`spell_name`): a token in a string is one word or it is two things to
    tell apart when a user retypes it.

    ``description`` is the sentence behind that name — what the code does, in
    the tooltip on the insert row's button. It never reaches the string and it
    is the one field here a format author writes purely for a reader, which is
    why it is free-form where the name is not.
    """

    code: int
    text: str
    role: GlyphRole = GlyphRole.TEXT
    description: str = ""

    def __post_init__(self) -> None:
        if not self.text:
            raise ValueError(f"glyph at {self.code:#x} has no text")

    @property
    def spells(self) -> bool:
        """Whether this glyph reads as ``text`` rather than as its own code."""
        return self.role.spells


@dataclass(frozen=True)
class Text:
    """A decoded fontmap: the string, and where every character came from.

    ``positions`` has **one entry per character of** ``body`` — the cell index
    that character was decoded from. A hex code five characters wide maps all
    five to the one cell.

    Kept per character rather than per glyph because of what reads it: the text
    window turns a caret into a canvas selection, and a caret sits between
    characters, not between glyphs. Deriving it from a glyph run at every
    keystroke would be the same table built again on each cursor move.
    """

    body: str
    positions: tuple[int, ...]

    def cell_at(self, offset: int) -> int:
        """The cell the character at ``offset`` came from, clamped to the text.

        Clamped because a caret may legitimately sit one past the last character
        — that is where typing appends — and the cell it should select there is
        the last one, not none.
        """
        if not self.positions:
            return 0
        return self.positions[max(0, min(offset, len(self.positions) - 1))]

    def span_of(self, first: int, last: int) -> tuple[int, int]:
        """The cell range ``[first, last)`` of the body covers, as ``(start, stop)``.

        ``stop`` is exclusive and always at least ``start + 1``: a caret with
        nothing selected still names the one cell it sits in, which is what the
        canvas highlights.
        """
        start = self.cell_at(first)
        stop = self.cell_at(max(first, last - 1)) + 1
        return start, max(stop, start + 1)


@dataclass(frozen=True)
class EncodedText:
    """Typed text turned back into codes, plus what could not be turned.

    ``unknown`` holds every character the alphabet has no code for, in order of
    first appearance and without repeats. It is **not** an exception, because the
    text window has to keep showing what the user typed while telling them one
    character of it will not fit — raising would take the edit away instead of
    reporting it.

    Each of those characters still **costs its cell**, filled with
    :attr:`FontAlphabet.blank`. Leaving them out of ``codes`` instead would make
    the result a string nobody typed — everything after the first unspellable
    character slides one cell to the left — so the only safe thing a caller could
    do with it is refuse the whole edit, and one stray character would freeze the
    picture until it was hunted down. A blank keeps the rest of the string on the
    cells the user put it on and leaves the gap visible exactly where the missing
    glyph is, which is the thing they have to see to fix it. ``unknown`` is what
    says so out loud.

    ``ends_line`` has **one entry per code**, saying whether that cell's
    terminator bit is set (:attr:`~celpix.core.tilemap.Cell.ends_line`). All
    False for the formats that punctuate with a code, which is most of them; a
    caller writes it beside the index either way rather than branching, since a
    format with no such bit has nowhere for a True to land and the codec drops it.
    """

    codes: tuple[int, ...]
    unknown: tuple[str, ...]
    ends_line: tuple[bool, ...] = ()

    @property
    def ok(self) -> bool:
        """Whether every character encoded — whether these codes say what was typed."""
        return not self.unknown


class FontAlphabet:
    """The codes ⇄ text lookup a fontmap is read and typed through.

    ``code_digits`` is how wide a code prints — ``[$1F]`` for a one-byte format,
    ``[$FFFE]`` for a two-byte one. It comes from the cell size the tilemap codec
    reports rather than from the table, because it is a fact about the *stream*
    and the same font may be read at either width.

    ``flag_break`` says the stream ends its lines on a **bit the cell carries**
    rather than on a code of its own — the cell format's ``terminator`` field
    (``docs/graphics-formats-reference/text-formats.md`` §4.4). It rides here
    because :meth:`encode` is what has to answer for it: a newline typed into
    such a stream costs **no cell**, and a budget readout that counted one would
    tell the user a string does not fit when it does.

    Text glyphs are matched **longest spelling first** on the way back, so a
    code standing for a pair beats the two letters it stands for.
    """

    __slots__ = (
        "_break",
        "_by_code",
        "_by_text",
        "_glyphs",
        "_names",
        "code_digits",
        "flag_break",
    )

    def __init__(
        self,
        glyphs: Iterable[Glyph] = (),
        *,
        code_digits: int = 2,
        flag_break: bool = False,
    ) -> None:
        self._glyphs: tuple[Glyph, ...] = tuple(glyphs)
        self.code_digits = max(1, code_digits)
        self.flag_break = flag_break
        self._by_code: dict[int, Glyph] = {}
        self._by_text: dict[str, Glyph] = {}
        # A **named** code's name back to its code, for the ``[wait]`` form
        # :meth:`decode` writes and :meth:`encode` reads. First declaration wins,
        # like every other index here — two codes sharing a name would both parse
        # back to this one, so the second keeps its hex and says which byte it is
        # rather than claiming to be the first.
        self._names: dict[str, int] = {}
        self._break: Glyph | None = None
        for glyph in self._glyphs:
            self._by_code.setdefault(glyph.code, glyph)
            if glyph.spells:
                self._by_text.setdefault(glyph.text, glyph)
            else:
                self._names.setdefault(glyph.text, glyph.code)
            if glyph.role is GlyphRole.BREAK and self._break is None:
                self._break = glyph

    # -- shape -------------------------------------------------------------
    @property
    def glyphs(self) -> tuple[Glyph, ...]:
        """Every glyph, in declaration order — the order the insert row lists."""
        return self._glyphs

    @property
    def commands(self) -> tuple[Glyph, ...]:
        """The glyphs that punctuate rather than spell — the insert row's list.

        They read as their own hex code like anything unmapped, so what this is
        for is the *name*: an insert row that offers "line break" is the only
        thing standing between the user and having to remember that this game's
        line break is ``$FE``.
        """
        return tuple(glyph for glyph in self._glyphs if not glyph.spells)

    @property
    def line_break(self) -> Glyph | None:
        """The glyph a typed newline encodes to — the **first** ``BREAK`` declared.

        A format may have several codes that end a line — one that scrolls and
        one that does not, or a line break and a string terminator — and only one
        of them can be what the Enter key means. The first wins because a preset
        lists its codes in the order it wants them offered; the rest stay
        reachable, and unambiguous, as their own hex.

        None where the format declares no break at all, which is an ordinary
        thing — a bare index-only text run has no punctuation, and its line width
        is the view's Cols. A newline typed there cannot be encoded and is
        reported as unknown rather than silently dropped.
        """
        return self._break

    @property
    def blank(self) -> int:
        """The code a cell the text puts nothing in holds — the font's space.

        Two callers ask it and they have to agree, because both are the same
        question: a string typed shorter than its region leaves cells behind it,
        and a character this font cannot spell leaves a cell where it stands
        (:meth:`encode`). A space is the one glyph a font drawn for words nearly
        always has, and the only filler that is not a letter nobody typed.

        **Zero** where the font has no space at all. There is no honest text for
        that cell, and picking some other glyph would put a visible character in
        it — zero at least draws whatever tile 0 is and stays one round trip away
        from what the file holds.
        """
        glyph = self._by_text.get(BLANK)
        return glyph.code if glyph is not None else 0

    def __len__(self) -> int:
        return len(self._glyphs)

    def __repr__(self) -> str:
        return f"FontAlphabet({len(self._glyphs)} glyphs, {self.code_digits} digits)"

    def merged(self, other: FontAlphabet | None) -> FontAlphabet:
        """This alphabet with ``other``'s glyphs laid over it.

        The one place the font's half and the text format's half become one
        table, and the reason the argument wins on a collision: the **controls
        are** ``other``. A code the font claims as a letter and the stream claims
        as a terminator is the stream's — the font's glyph table was authored
        against the tiles, and it has no way of knowing which codes a given
        stream reserves.

        ``code_digits`` comes from ``other`` when it has one, and so does
        ``flag_break``, for the same reason: both are the cell format's, and it is
        the cell format doing the reserving.
        """
        if other is None:
            return self
        keep = {glyph.code for glyph in other.glyphs}
        return FontAlphabet(
            [g for g in self._glyphs if g.code not in keep] + list(other.glyphs),
            code_digits=other.code_digits,
            flag_break=other.flag_break,
        )

    def shifted(self, by: int) -> FontAlphabet:
        """This alphabet with every glyph's code moved by ``by``.

        The answer to the one question a font sheet cannot settle. A table says
        which characters the glyphs are and in what order, and the order is
        nearly always right the moment the sheet is legible — but *where the run
        starts* lives in the game's code, not in the art
        (``docs/graphics-formats-reference/text-formats.md`` §3.2). So the shape
        of the alphabet and its origin are two independent unknowns, and this is
        the second one: the bar's **Base code**, dialled against the string until
        it reads.

        Glyphs shifted outside what a cell of this width can hold are
        **dropped**, not clamped. Such a code is one no stream can contain, so
        keeping it would only give :meth:`encode` something to write that the map
        cannot store — a typed letter landing as a corrupt index — and clamping
        would pile several glyphs onto the end code and let the first one
        silently win.
        """
        if not by:
            return self
        limit = 1 << (4 * self.code_digits)
        return FontAlphabet(
            (
                replace(glyph, code=glyph.code + by)
                for glyph in self._glyphs
                if 0 <= glyph.code + by < limit
            ),
            code_digits=self.code_digits,
            flag_break=self.flag_break,
        )

    # -- matching ----------------------------------------------------------
    def hex_code(self, code: int) -> str:
        """``code`` as the ``[$1F]`` form — the reading that loses nothing."""
        return f"[${code:0{self.code_digits}X}]"

    def token(self, glyph: Glyph) -> str:
        """How ``glyph`` is written in the text — its name, or its hex code.

        The one place that decides, so the insert row's button writes exactly
        what :meth:`decode` would have put there and what :meth:`encode` reads
        back. A name that is not this code's — because another code claimed it
        first — falls back to hex rather than typing to the wrong cell.
        """
        if self._names.get(glyph.text) == glyph.code:
            return f"[{glyph.text}]"
        return self.hex_code(glyph.code)

    def ends_line(self, code: int, flagged: bool = False) -> bool:
        """Whether a cell holding ``code`` finishes a line, by either mechanism.

        Both, because a format may punctuate either way: ``flagged`` is the
        terminator bit the cell carries
        (:attr:`~celpix.core.tilemap.Cell.ends_line`), and the other is the one
        code :attr:`line_break` names. A format with several break codes ends a
        line here only on the canonical one, which is the rule :meth:`decode`
        already follows — the rest stay hex, and marking them would claim a break
        the text does not show.

        Asked here rather than worked out by whoever is drawing, because the
        answer has two readers: the text window gets its newline out of
        :meth:`decode`, and the canvas marks the same cells on the picture. A
        second rule elsewhere would eventually mark a cell the text does not break
        at, and the two disagreeing is worse than neither.
        """
        return flagged or (self._break is not None and code == self._break.code)

    # -- decode ------------------------------------------------------------
    def decode(
        self, codes: Sequence[int], ends_line: Sequence[bool] | None = None
    ) -> Text:
        """``codes`` as readable text, with the cell each character came from.

        Four cases. A glyph that spells reads as itself; a line break reads as a
        newline; a **named** code reads as ``[its name]``; and **everything else
        reads as its own hex code**, which is what keeps this general — a control
        celPix has never heard of, an unmapped icon and a byte of padding all
        come out reversible without anyone having had to describe them.

        The name is the one thing here a *user* supplied, and it is why a named
        code is worth the fourth case: `[wait]` says what the byte does where
        `[$2A]` only says which byte it is, and both type back to the same cell.
        A name is never guessed — a code reads as hex until someone names it.

        ``ends_line`` is the cells' terminator bits, where the format has one
        (:attr:`~celpix.core.tilemap.Cell.ends_line`). A set bit adds the newline
        **after** whatever the code itself read as — the character and the line
        end are one cell, which is exactly what such a format stores — so the
        newline shares that cell in :attr:`Text.positions` and a caret on it is a
        caret on the character it ends.
        """
        body: list[str] = []
        positions: list[int] = []
        for at, code in enumerate(codes):
            glyph = self._by_code.get(code)
            if glyph is None or not glyph.spells:
                if glyph is not None and glyph is self._break:
                    piece = "\n"
                elif glyph is not None and self._names.get(glyph.text) == code:
                    # Named, and the name is still unambiguously this code's —
                    # two codes given the same name would both parse back to the
                    # first, so the second keeps its hex rather than lying.
                    piece = f"[{glyph.text}]"
                else:
                    piece = self.hex_code(code)
            else:
                # ``[`` opens a code, so a font with one as a *letter* doubles it
                # here or the string it decodes to will not parse back.
                piece = glyph.text.replace("[", ESCAPE)
            # Guarded rather than appended blind: a break *code* that also
            # carries the bit already reads as a newline, and a second one would
            # invent a blank line the file has not got.
            if ends_line is not None and at < len(ends_line) and ends_line[at]:
                if not piece.endswith("\n"):
                    piece += "\n"
            body.append(piece)
            positions.extend([at] * len(piece))
        return Text("".join(body), tuple(positions))

    # -- encode ------------------------------------------------------------
    def encode(self, text: str) -> EncodedText:
        """Typed ``text`` back to codes, reporting whatever would not fit.

        The inverse of :meth:`decode`, and tolerant: anything the alphabet *can*
        say is encoded, and anything it cannot spends its cell on :attr:`blank`
        and is named in :attr:`EncodedText.unknown`. It never raises.

        The substitution is not a silent fallback — ``unknown`` exists to say it
        happened, and the text window shows it as a warning beside the string.
        What it buys is that one unspellable character no longer holds the whole
        edit back: the rest of the string lands on the cells the user typed it
        on, and the gap sits on the picture where the missing glyph would be,
        which is a far better account of what is wrong than a canvas that has
        stopped following the text.

        A **newline** encodes to the canonical break (:attr:`line_break`); where
        the format has none, a typed newline is unknown and costs **no cell**. It
        is punctuation the format cannot express rather than a glyph the font is
        missing, so there was never a cell of its own to blank — standing one in
        would push a space into the string that nobody typed.

        Under ``flag_break`` it sets the **terminator bit on the code before it**
        instead, and so costs no cell of its own. Two cases there are not that
        and are reported rather than fudged: a newline with no code before it,
        and one whose code is already flagged — in both, the bit the user is
        asking for is already spoken for, and setting it again would swallow a
        line break silently. A format that has *both* a flag and a break code
        falls back to the code, which is what makes a blank line expressible.
        """
        out: list[int] = []
        ends: list[bool] = []
        unknown: list[str] = []
        seen: set[str] = set()

        def emit(code: int) -> None:
            out.append(code)
            ends.append(False)

        def miss(what: str, *, cell: bool = True) -> None:
            if what not in seen:
                seen.add(what)
                unknown.append(what)
            # A blank stands in so the codes stay one per typed piece and nothing
            # after this slides left. ``cell=False`` is for the one thing that
            # never had a cell to stand in for — punctuation this format cannot
            # express.
            if cell:
                emit(self.blank)

        at = 0
        total = len(text)
        while at < total:
            char = text[at]
            if text.startswith(ESCAPE, at):
                at += len(ESCAPE)
                glyph = self._by_text.get("[")
                if glyph is None:
                    miss("[")
                else:
                    emit(glyph.code)
                continue
            if char == "[":
                found = _TOKEN_RE.match(text, at)
                inside = found.group(1) if found else ""
                digits = inside[1:] if inside.startswith("$") else ""
                if found is not None and inside in self._names:
                    # A named code, written the way :meth:`decode` writes it.
                    # Checked before the hex form so a command named ``$FF``
                    # — which nothing stops a user typing — still reaches its
                    # own code rather than byte 255.
                    at = found.end()
                    emit(self._names[inside])
                    continue
                if found is None or not inside.startswith("$"):
                    # Not a code: an unclosed bracket, brackets around a name
                    # this font has not got, or brackets around something else.
                    # Reported whole rather than encoded letter by letter, which
                    # would silently write the punctuation.
                    miss(found.group(0) if found else text[at:])
                    at = found.end() if found else total
                    continue
                at = found.end()
                if _HEX_RE.match(digits):
                    emit(int(digits, 16))
                else:
                    miss(found.group(0))
                continue
            if char == "\n":
                at += 1
                if self.flag_break and ends and not ends[-1]:
                    ends[-1] = True
                elif self._break is not None:
                    emit(self._break.code)
                else:
                    miss("\\n", cell=False)
                continue
            glyph = self._longest_text(text, at)
            if glyph is None:
                miss(char)
                at += 1
                continue
            emit(glyph.code)
            at += len(glyph.text)
        return EncodedText(tuple(out), tuple(unknown), tuple(ends))

    def _longest_text(self, text: str, at: int) -> Glyph | None:
        """The verbatim glyph at ``text[at]``, preferring the longest spelling.

        Longest first so a format with a code for a common pair uses it rather
        than spending two codes on the letters — which is the whole point of
        having one, and the difference between a string fitting its slot and not.
        """
        best: Glyph | None = None
        for glyph in self._by_text.values():
            if len(glyph.text) > (len(best.text) if best else 0) and text.startswith(
                glyph.text, at
            ):
                best = glyph
        return best


# -- typing over a decoded string -------------------------------------------
#
# The text window types *over* the string rather than into it: a fontmap's cells
# are a fixed run, so one keystroke replaces one cell and the budget never moves
# (``docs/design/fontmap-entry.md`` §5). That needs two questions answered about
# a caret, and neither of them is Qt's: which characters make up the one thing it
# is standing on, and whether it is inside a ``[...]`` — where typing is
# composing a code and must be left alone. Both are string arithmetic over the
# text form, so both live here, beside the form they are about, and are testable
# without a widget.


def unit_spans(text: str) -> list[tuple[int, int]]:
    """``text`` split into the pieces one cell holds one of.

    A ``[[`` escape and a ``[...]`` code are **one piece each**, however many
    characters wide they read; everything else is one character. That is the unit
    an overtype replaces, and the reason it is counted rather than measured: a
    keystroke landing on ``[$FE]`` replaces the code, not its ``[``.

    An unclosed ``[`` runs to the next ``[`` or to the end — mid-composition it
    is exactly one piece being typed.
    """
    spans: list[tuple[int, int]] = []
    at, total = 0, len(text)
    while at < total:
        if text.startswith(ESCAPE, at):
            spans.append((at, at + 2))
            at += 2
            continue
        if text[at] == "[":
            close = text.find("]", at + 1)
            nested = text.find("[", at + 1)
            if close >= 0 and (nested < 0 or close < nested):
                stop = close + 1
            else:
                stop = total if nested < 0 else nested
            spans.append((at, stop))
            at = stop
            continue
        spans.append((at, at + 1))
        at += 1
    return spans


def inside_code(body: str, at: int) -> bool:
    """Whether the caret at ``at`` sits **within** a ``[...]`` rather than beside it.

    The one switch that turns overtyping off. Inside a code the user is spelling
    a number — digit by digit, and for an unclosed ``[`` with nothing yet to
    replace — so the keystroke edits the string and nothing is written to the
    cells until the caret leaves. On the ``[`` itself this is False, which is what
    makes typing there replace the whole pair: the caret is standing on one
    piece, and a piece is a cell.

    ``[[`` is a literal ``[`` and so is never "inside" anything.
    """
    for start, stop in unit_spans(body):
        if start >= at:
            break
        if body[start] != "[" or body.startswith(ESCAPE, start):
            continue
        # One past a *closed* code is beside it; one past an unclosed one is the
        # far end of the number being typed, which is where the digits go.
        closed = body[stop - 1 : stop] == "]"
        if start < at < stop or (not closed and at == stop):
            return True
    return False


def carried_break(body: str, units: Sequence[int], at: int) -> bool:
    """Whether ``body[at]`` is a newline its cell **carries** rather than spends.

    The second thing a caret has to be able to tell apart inside one piece, and
    the mirror of :func:`inside_code`. Where a format ends a line with a bit on
    the last character, that cell decodes to two things — the letter and the
    break — and they are edited separately or not at all: retyping the letter
    must not unend the line, and there must still be a way to unend it.

    True only where the newline **shares its cell with something else**, which is
    exactly what distinguishes the two punctuations. A line-break *code* is a cell
    of its own, so its piece is the newline and nothing more; overtyping that
    replaces a code with a letter, which is an ordinary keystroke and right as it
    stands.
    """
    return (
        0 <= at < len(body)
        and body[at] == "\n"
        and at > 0
        and units[at - 1] == units[at]
    )


def unit_bounds(units: Sequence[int], at: int, count: int = 1) -> tuple[int, int]:
    """The span of ``count`` whole pieces the offset ``at`` starts on.

    ``units`` is one id per character of the body saying which piece it belongs
    to — :attr:`Text.positions` for a string as the file has it, and the same
    shape maintained locally for one being typed. Read from the *start* of the
    piece under the caret, so a caret landing in the middle of a two-character
    glyph still replaces the glyph rather than half of it.

    An empty span at the end of the body, where there is nothing to type over.
    """
    total = len(units)
    at = max(0, min(at, total))
    if at >= total:
        return total, total
    start = at
    while start > 0 and units[start - 1] == units[start]:
        start -= 1
    stop = start
    for _ in range(max(1, count)):
        if stop >= total:
            break
        unit = units[stop]
        while stop < total and units[stop] == unit:
            stop += 1
    return start, stop


def splice(
    body: str,
    units: Sequence[int],
    first: int,
    last: int,
    typed: str,
    *,
    unit: int | None = None,
) -> tuple[str, tuple[int, ...]]:
    """``body[first:last]`` replaced by ``typed``, with the unit map kept in step.

    ``unit`` says what the typed characters belong to. **None** gives each piece
    of ``typed`` a piece of its own, which is a keystroke landing on the cells; an
    existing id folds them all into that one, which is typing *inside* a
    ``[...]`` — however many digits are composed into it, the code is still one
    cell.

    Ids invented here are negative and descending, so they cannot collide with
    the cell numbers :meth:`FontAlphabet.decode` hands back or with each other. They
    live only until the edit is written and the region is decoded again.
    """
    if unit is None:
        fresh = min(units, default=0) - 1
        ids: list[int] = []
        for start, stop in unit_spans(typed):
            ids.extend([fresh] * (stop - start))
            fresh -= 1
    else:
        ids = [unit] * len(typed)
    return (
        body[:first] + typed + body[last:],
        tuple(units[:first]) + tuple(ids) + tuple(units[last:]),
    )


# -- building an alphabet from what a preset says ---------------------------


def sequential(first: int, chars: str) -> list[Glyph]:
    """One glyph per character of ``chars``, numbered from ``first``.

    The simplest statement of an alphabet and the commonest: a font sheet whose
    tiles are its letters in order, so the table is the letters themselves. This
    is the **positional** half of a font alphabet — character *i* is the character
    tile *i* draws — and it is what the editor's sheet is a picture of.

    A space in ``chars`` is a space glyph. A :data:`HOLE` is the slot that draws
    no character and yields no glyph: the run has to keep its length either way,
    or every letter after the gap lands on the wrong tile.
    """
    return [Glyph(first + at, char) for at, char in enumerate(chars) if char != HOLE]


def parse_table(text: str, *, order: str = "code-first") -> list[Glyph]:
    """A table file's lines as glyphs.

    The format is the one the whole scene already writes and both sibling
    projects in this tree keep their fonts in: one ``code=text`` line per glyph,
    hex on the code side. ``order = "text-first"`` reads it the other way round,
    which is how an assembler's own ``table`` directive spells the same thing.
    **Never guessed** — both sides of ``20=A`` parse as hex, so a detector reads
    a whole font backwards on the tables that happen to be all-hex, and being
    wrong here is silent.

    A value in brackets names a **command**: ``FE=[line break]`` says that code
    is not a letter and what to call it. It still reads as ``[$FE]`` in the text
    like any other command — what the name buys is a captioned button on the
    insert row instead of a number to remember.

    Lines that do not parse are **ignored rather than refused**: that is what
    lets a table carry comments, blank lines and an assembler's own directives
    without a syntax of its own.
    """
    out: list[Glyph] = []
    for raw in text.splitlines():
        line = raw.rstrip("\r").rstrip("\n")
        if not line or "=" not in line:
            continue
        if order == "text-first":
            # Split at the **last** ``=`` so the line ``==3D`` reads as the ``=``
            # character rather than as an empty key.
            spelling, _, digits = line.rpartition("=")
        else:
            # And at the **first** for the other order, so ``3D==`` does too.
            digits, _, spelling = line.partition("=")
        digits = digits.strip()
        if not spelling or not _HEX_RE.match(digits or ""):
            continue
        if spelling.startswith("[") and spelling.endswith("]") and len(spelling) > 2:
            name = spell_name(spelling[1:-1])
            if name:
                out.append(Glyph(int(digits, 16), name, GlyphRole.CONTROL))
        else:
            out.append(Glyph(int(digits, 16), spelling))
    return out


def glyphs_from_spec(
    spec: Iterable[dict],
    default_role: GlyphRole = GlyphRole.TEXT,
) -> list[Glyph]:
    """Glyphs from the mapping form a project file or a preset states them in.

    Each entry names ``code`` and either ``name`` (a command) or ``text`` (a
    character), optionally ``role`` and ``description``. This is the
    **absolute** half of a font alphabet, against the positional run
    :func:`sequential` builds: a code named because the game's code says what it
    is — a line break, a terminator, a command worth a caption — or one the run
    cannot spell, a pair standing behind a single code or a glyph outside the
    sheet. Neither kind moves when the run's origin is dialled, because neither
    was read off the sheet.

    ``default_role`` is what an entry that omits ``role`` gets, and it differs by
    who is stating the list: a font's glyphs are letters unless they say
    otherwise, and a cell format's ``controls`` are commands unless they say
    otherwise. Naming the common case per caller is what keeps the *other*
    caller's every line from carrying a ``role`` that could only be one thing.

    A record that does not parse is **skipped, not refused**: this reads a
    project file and a format author's preset, both of them shared,
    hand-editable and untrusted, and one bad line must not cost the user the
    rest of their table.

    A **name** is spelled to one word on the way in (:func:`spell_name`), since
    it is what a reader retypes inside ``[...]``. ``text`` is left exactly as
    written, because a character is whatever the sheet draws.
    """
    out: list[Glyph] = []
    for entry in spec:
        if not isinstance(entry, dict):
            continue
        try:
            code = int(entry.get("code"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        stated = entry.get("role")
        role = default_role if stated is None else GlyphRole.parse(stated)
        named = str(entry.get("name", ""))
        text = spell_name(named) if named else str(entry.get("text", ""))
        if not named and not role.spells:
            # A command stated the older way, with its name under ``text``.
            text = spell_name(text)
        if not text:
            continue
        out.append(Glyph(code, text, role, str(entry.get("description", ""))))
    return out
