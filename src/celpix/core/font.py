"""The fontmap's alphabet: character codes ⇄ readable text.

A **fontmap** is a tilemap whose cells are character codes rather than tile
numbers — a string of text stored in a ROM as references into a font
(``docs/design/fontmap-entry.md``). Everything about reading those cells is the
tilemap pathway's, unchanged; what this module adds is the one thing a tilemap
has no notion of: **what the codes say**.

A :class:`FontAlphabet` is that lookup, and **all of it belongs to the font
entry** — the letters and the punctuation alike. It is stored as two halves,
which differ in how they were *read* rather than in who owns them:

- the **positional run**, one character per tile in tile order, which is the
  sheet read straight off and moves with the origin.
- the **named codes**, absolute, which is the stream read straight off: a line
  break, a terminator, a command worth a caption, a pair standing behind one
  code, a glyph the sheet has not got.

Both are :class:`Glyph`\\ s and both reach the decoder through one object, which
is why :meth:`FontAlphabet.merged` exists rather than the reader consulting two
tables and deciding which wins.

A cell format states **no codes at all**. It says how a cell's bits are laid out
and nothing about what any value means, so two streams punctuated differently
are two font entries over the same tiles — each with its own reading of the same
glyphs (``docs/design/fontmap-entry.md`` §3).

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

A code that spells **several characters** is a ``DICT`` glyph — a game's own
compression table, a hundred codes standing for ``the``, ``you`` and ``ing ``
above a font of 128 tiles. It reads and types exactly as the characters it
spells, so it is not a case of its own here; what it adds is
:meth:`FontAlphabet.spelling`, which is how the *picture* answers for a code the
sheet has no tile for (``docs/design/fontmap-entry.md`` §5).

A named code may also **swallow the cells after it** (:attr:`Glyph.params`),
which is how a command with an argument stops reading as a command followed by a
stray letter: ``[speed, $00]`` rather than ``[speed]A``. It is the same third
case with its operands inside the brackets, opt-in per command, and a format that
has said nothing about its commands is unchanged — the operand goes on reading as
its own ``[$00]``, which is still correct and still types back to the same byte.

**Nothing is ever dropped.** That is the same rule the cell model follows for a
priority bit it cannot render: a field dropped on the way in is a field silently
zeroed on the way out.

Qt-free, like the rest of ``core``.
"""

from __future__ import annotations

import re
from bisect import bisect_left
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


def split_params(stated: str) -> tuple[str, int]:
    """``speed, 1`` as the name and the cell count behind it.

    The one spelling a command's operand count is written in, wherever a person
    writes one: the table form's ``7A=[speed, 1]``, and the Text cell of the
    alphabet editor's own table. Kept here beside :func:`spell_name` because
    both are about what a *reader* types, and both have to be undone the same
    way when it is written back.

    ``(stated, 0)`` for anything that is not a name followed by a whole number
    of cells — including a name that simply has a comma in it, which
    :func:`spell_name` then hyphenates. A count is the only thing that may sit
    there, so anything else is a name somebody wrote loosely rather than a
    declaration, and reading it as one would silently make the command eat the
    cell after it.
    """
    head, sep, tail = stated.rpartition(",")
    if not sep:
        return stated, 0
    digits = tail.strip()
    if not digits.isdigit():
        return stated, 0
    return head, int(digits)


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

    Still only three answers a general reader can give about a code: it spells
    something, it ends a line, or it is a command this build has no business
    interpreting. A richer set would mean guessing at one game's conventions and
    imposing them on every other. ``DICT`` is not a fourth answer — it is the
    first one, said of a code that spells **more than one character**.

    ``TEXT`` and ``DICT`` say what the *sheet* draws, which is legible off the
    art. ``BREAK`` and ``CONTROL`` say how the *stream* is punctuated, which is
    in the game's code and in nobody's picture. All of them sit on the font
    entry, so a font read by two differently-punctuated streams is two entries
    over the same tiles.

    ``str``-valued so a preset states a role as itself and the on-disk spelling
    is the enum.
    """

    TEXT = "text"  # types verbatim, one character: "A"
    DICT = "dict"  # types verbatim, several: a "th" pair behind one code
    BREAK = "break"  # ends a line, and reads as a newline
    CONTROL = "control"  # anything the game acts on: reads as its own hex code

    @property
    def spells(self) -> bool:
        """Whether this role puts characters on screen rather than punctuating."""
        return self is GlyphRole.TEXT or self is GlyphRole.DICT

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

    ``text`` is what a glyph that spells reads as, and it may be **several
    characters**: that is what a code standing for a common pair is, and dropping
    it would cost the one compression trick fixed-size text regions actually use.
    For the punctuating roles it is the **name**, and the name *is* what the
    string holds — ``[line-break]`` — so it carries **no spaces**
    (:func:`spell_name`): a token in a string is one word or it is two things to
    tell apart when a user retypes it.

    **Spelling several characters is** ``DICT``, and the role is settled here
    rather than trusted from the caller. The two are the same fact said twice —
    a ``TEXT`` glyph is one character and a ``DICT`` glyph is a run of them — so
    letting them disagree would mean a table that says one thing and a picture
    that draws another. Normalizing at the one door every glyph comes through
    (a project file, a table file, the alphabet editor, :func:`sequential`,
    :meth:`FontAlphabet.shifted`'s ``replace``) is what makes
    ``role is GlyphRole.DICT`` a question anything downstream may ask.

    ``description`` is the sentence behind that name — what the code does, in
    the tooltip on the insert row's button. It never reaches the string and it
    is the one field here a format author writes purely for a reader, which is
    why it is free-form where the name is not.

    ``params`` is how many cells after this one the command **swallows** as its
    own — ``[speed, $00]`` for a code that reads the next cell as a speed. Zero,
    and every code stands alone, which is what a format that has said nothing
    about its commands gets and is what most of them are. Declared per command
    because that is where the fact lives: the count is a property of the one
    code, not of the format, and a reader who has not worked one out leaves it
    at zero and gets the operand as its own ``[$00]``, exactly as before.

    It is meaningless on a glyph that spells, and ignored there: a character
    consumes nothing.
    """

    code: int
    text: str
    role: GlyphRole = GlyphRole.TEXT
    description: str = ""
    params: int = 0

    def __post_init__(self) -> None:
        if not self.text:
            raise ValueError(f"glyph at {self.code:#x} has no text")
        if self.role.spells:
            spelled = GlyphRole.DICT if len(self.text) > 1 else GlyphRole.TEXT
            if spelled is not self.role:
                object.__setattr__(self, "role", spelled)

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

    Kept per character rather than per glyph because of what reads it: the two
    views mirror each other's selection, and a caret sits between characters,
    not between glyphs. Deriving it from a glyph run at every keystroke would be
    the same table built again on each cursor move.

    It is **nondecreasing** — the decode walks the cells in order and each one
    contributes its characters in a run — which is what lets :meth:`offsets_of`
    invert it by bisection rather than by scanning.
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

    def offsets_of(self, start: int, stop: int) -> tuple[int, int]:
        """The body range ``[first, last)`` the cells ``[start, stop)`` decoded to.

        The inverse of :meth:`span_of`, and the direction a canvas selection
        travels in: the user picked cells and the text has to show which
        characters those are. A cell that read as a five-character hex code
        contributes all five, which is what makes the two selections cover the
        same thing rather than the same *length*.

        Empty where the cells decoded to nothing — a range past the end of the
        text — since :attr:`positions` is nondecreasing and both bisections land
        in the same place.
        """
        return (
            bisect_left(self.positions, start),
            bisect_left(self.positions, stop),
        )


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
        "_dictionary",
        "_encoded",
        "_glyphs",
        "_names",
        "_params",
        "_pieces",
        "_spellings",
        "_text_sizes",
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
        # How many cells each **command** swallows after itself, for the codes
        # that swallow any (:attr:`Glyph.params`). Its own index rather than a
        # field read off ``_by_code``, because :meth:`decode` asks it of every
        # cell in the region and the answer is nothing for nearly all of them —
        # an empty dict says so in one lookup.
        self._params: dict[int, int] = {}
        self._break: Glyph | None = None
        # Whether any code here stands for **several** characters at all, so the
        # one caller that expands them can leave without a pass over the region
        # (:meth:`spelling`). Nearly every font has no dictionary, and the
        # question is asked of every fontmap drawn.
        self._dictionary = False
        for glyph in self._glyphs:
            self._by_code.setdefault(glyph.code, glyph)
            if glyph.spells:
                self._by_text.setdefault(glyph.text, glyph)
                self._dictionary = self._dictionary or glyph.role is GlyphRole.DICT
            else:
                self._names.setdefault(glyph.text, glyph.code)
                if glyph.params > 0:
                    self._params.setdefault(glyph.code, glyph.params)
            if glyph.role is GlyphRole.BREAK and self._break is None:
                self._break = glyph
        # The distinct spelling *widths*, longest first — what :meth:`_longest_text`
        # probes instead of walking every glyph. A font is a hundred-odd glyphs and
        # a text region is tens of thousands of characters, so a scan per character
        # is the whole cost of encoding one; nearly every font has exactly one width
        # and the probe is then a single dict lookup.
        self._text_sizes: tuple[int, ...] = tuple(
            sorted({len(text) for text in self._by_text}, reverse=True)
        )
        # One code's reading, remembered (:meth:`decode`). A text region draws tens
        # of thousands of cells out of a hundred-odd codes, so the four-case
        # decision below is the same handful of answers over and over. Safe to keep
        # because an alphabet never changes after it is built — every operation on
        # one (:meth:`merged`, :meth:`shifted`) returns a new object.
        self._pieces: dict[int, str] = {}
        # And one dictionary code's characters as codes of their own
        # (:meth:`spelling`), remembered for the same reason: the answer depends
        # on the code alone, and the caller asks it once per cell of a region.
        self._spellings: dict[int, tuple[int, ...]] = {}
        # And the last string encoded, for the same reason one reading over
        # (:meth:`encode`). One entry: the callers are all asking about the string
        # on screen right now, and a second would only ever hold the one before it.
        self._encoded: tuple[str, EncodedText] | None = None

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

    @property
    def has_dictionary(self) -> bool:
        """Whether any code here spells **several** characters — a ``DICT`` glyph.

        The cheap guard in front of :meth:`spelling`, for the caller that has to
        ask it of every cell on a map (:attr:`~celpix.core.document.Document.
        laid_out_cells`). A font with no dictionary is nearly every font, and it
        answers here in one attribute read rather than in a pass over the region.
        """
        return self._dictionary

    def spelling(self, code: int) -> tuple[int, ...]:
        """The codes that spell out what ``code`` says, one per character.

        ``$E3 = "you"`` comes back as the codes for ``y``, ``o`` and ``u``. This
        is the one thing a dictionary code needs that an ordinary one does not:
        the sheet has no tile for it — a hundred-odd codes above a 128-tile font
        are exactly the compression table, drawn by nothing — so the only honest
        picture of that cell is the characters it stands for, each on its own
        glyph (``docs/design/fontmap-entry.md`` §5).

        **Empty for anything else**, which covers three cases the caller does not
        have to tell apart: a code that is not a dictionary entry, one whose
        characters this font cannot spell singly, and one nothing has named. In
        all three there is nothing to draw it *as*, and the cell stays the one
        cell it is — showing whatever tile its own code names, which is the
        picture the file actually describes.

        Nothing here recurses. A dictionary entry spells several characters and
        each of them is looked up whole, so a piece can only ever be a one-
        character glyph — which is by construction not a ``DICT``.
        """
        spelled = self._spellings.get(code)
        if spelled is None:
            spelled = self._spellings[code] = self._spell(code)
        return spelled

    def _spell(self, code: int) -> tuple[int, ...]:
        """:meth:`spelling` without the memo — the lookup itself."""
        glyph = self._by_code.get(code)
        if glyph is None or glyph.role is not GlyphRole.DICT:
            return ()
        out: list[int] = []
        for char in glyph.text:
            piece = self._by_text.get(char)
            if piece is None:
                # One character the font cannot draw is the whole spelling gone:
                # a partial run would put a word on the map with a letter missing
                # from the middle of it and nothing saying which.
                return ()
            out.append(piece.code)
        return tuple(out)

    def __len__(self) -> int:
        return len(self._glyphs)

    def __repr__(self) -> str:
        return f"FontAlphabet({len(self._glyphs)} glyphs, {self.code_digits} digits)"

    def merged(self, other: FontAlphabet | None) -> FontAlphabet:
        """This alphabet with ``other``'s glyphs laid over it.

        The one place the two halves of a font's own table become one, and the
        reason the argument wins on a collision: the **named codes are**
        ``other``. A code the positional run spells as a letter and the named
        codes call a terminator is the terminator's — the run was read off the
        sheet, in tile order, and the named codes were read off the stream at the
        value it actually holds (``docs/design/fontmap-entry.md`` §4).

        ``code_digits`` and ``flag_break`` come from ``other``, which is what
        makes the merged alphabet answer at the *stream's* measure rather than at
        whatever the half being laid over was built with.
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
    def _hex(self, value: int) -> str:
        """``value`` as it is written inside a token — ``$1F``, at this width."""
        return f"${value:0{self.code_digits}X}"

    def hex_code(self, code: int) -> str:
        """``code`` as the ``[$1F]`` form — the reading that loses nothing."""
        return f"[{self._hex(code)}]"

    def _head(self, code: int) -> str:
        """What a token calls ``code`` — its name where that name is its own.

        Hex otherwise, which covers both the code nobody has named and the one
        whose name another code claimed first: a token has to type back to the
        cell it came out of, and a borrowed name would type back to somebody
        else's.
        """
        glyph = self._by_code.get(code)
        if glyph is not None and self._names.get(glyph.text) == code:
            return glyph.text
        return self._hex(code)

    def token(self, glyph: Glyph) -> str:
        """How ``glyph`` is written in the text — its name, or its hex code.

        The one place that decides, so the insert row's button writes exactly
        what :meth:`decode` would have put there and what :meth:`encode` reads
        back. A name that is not this code's — because another code claimed it
        first — falls back to hex rather than typing to the wrong cell.

        A command that swallows cells is written **with them**, zeroed:
        ``[speed, $00]``. The button has no way to know what the user wants in
        an operand, and writing the command without its operands would leave a
        code eating whatever character followed it.
        """
        head = self._head(glyph.code)
        takes = 0 if glyph.spells else self._params.get(glyph.code, 0)
        if not takes:
            return f"[{head}]"
        return "[" + ", ".join([head, *([self._hex(0)] * takes)]) + "]"

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

        Each code's reading is worked out once and remembered
        (:attr:`_pieces`): the four cases below depend on the code alone, and a
        text region spends tens of thousands of cells on a hundred-odd of them.
        """
        body: list[str] = []
        positions: list[int] = []
        pieces = self._pieces
        params = self._params
        reading = self._reading
        flags = () if ends_line is None else ends_line
        flagged = len(flags)
        total = len(codes)
        at = 0
        while at < total:
            code = codes[at]
            stop = at + 1
            takes = params.get(code, 0)
            if takes:
                # Cut short at the end of the region rather than reaching past
                # it: a command with nothing left to swallow is what a run of
                # padding after a terminator looks like, and it still has to
                # read as something that types back.
                stop = min(total, stop + takes)
                piece = self._commanding(code, codes[at + 1 : stop])
            else:
                piece = pieces.get(code)
                if piece is None:
                    piece = pieces[code] = reading(code)
            # Guarded rather than appended blind: a break *code* that also
            # carries the bit already reads as a newline, and a second one would
            # invent a blank line the file has not got.
            ends = at < flagged and flags[at]
            if takes and not ends:
                # A swallowed cell's own bit is the command's, since the command
                # is what the string shows: the line ends after the whole token.
                ends = any(flags[k] for k in range(at + 1, min(stop, flagged)))
            if ends and not piece.endswith("\n"):
                piece += "\n"
            body.append(piece)
            # Every character of a command's token belongs to the **command's**
            # cell, operands included. The token is one thing to type over and
            # one thing to delete, and a caret that could stand between a command
            # and its operand would be a caret standing on half a piece.
            positions.extend([at] * len(piece))
            at = stop
        return Text("".join(body), tuple(positions))

    def _commanding(self, code: int, operands: Sequence[int]) -> str:
        """One command and the cells it swallowed, as ``[speed, $00]``.

        Not memoized where the other four cases are (:attr:`_pieces`), because
        this is the one reading that depends on cells other than its own.
        """
        return (
            "[" + ", ".join([self._head(code), *(self._hex(v) for v in operands)]) + "]"
        )

    def _reading(self, code: int) -> str:
        """What one code reads as, before its cell's terminator bit is applied.

        The four cases of :meth:`decode`, split out so they can be memoized per
        code rather than re-decided per cell.

        **Whichever it is, only the code that owns the spelling keeps it.** A
        name and a run of characters are the same promise — that what is written
        types back to the cell it came out of — so two codes claiming one
        answer is settled the same way for both: the first keeps it and the
        second reads as its own hex.
        """
        glyph = self._by_code.get(code)
        if glyph is None or not glyph.spells:
            if glyph is not None and glyph is self._break:
                return "\n"
            if glyph is not None and self._names.get(glyph.text) == code:
                # Named, and the name is still unambiguously this code's — two
                # codes given the same name would both parse back to the first,
                # so the second keeps its hex rather than lying.
                return f"[{glyph.text}]"
            return self.hex_code(code)
        # And the same rule for a **spelling**, which a font really does state
        # twice: A Link to the Past's sheet draws a second `I`, `i` and `!` at
        # $5F-$61 for the name-entry charset, above the ones at $08, $22 and $3E
        # (``docs/design/fontmap-entry.md`` §5). Both would read as `I` and both
        # would type back to $08, which is the one thing this form never does —
        # so the code that did not claim the spelling keeps its hex.
        owner = self._by_text.get(glyph.text)
        if owner is None or owner.code != code:
            return self.hex_code(code)
        # ``[`` opens a code, so a font with one as a *letter* doubles it here or
        # the string it decodes to will not parse back.
        return glyph.text.replace("[", ESCAPE)

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

        **The last answer is kept** (:attr:`_encoded`), because one edit asks
        this of the same string several times over: the budget readout, the write
        itself, and the readout again once the cells have come back. They are one
        question with one answer, and on a region of tens of thousands of cells
        each asking of it is a pass over the whole string. Safe to keep for the
        reason :attr:`_pieces` is — an alphabet never changes after it is built —
        and the hit costs one string comparison, which is a memcmp against a pass.
        """
        cached = self._encoded
        if cached is not None and cached[0] == text:
            return cached[1]
        encoded = self._encode(text)
        self._encoded = (text, encoded)
        return encoded

    def _encode(self, text: str) -> EncodedText:
        """:meth:`encode` without the memo — the pass itself."""
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
        # An ordinary character is nearly the whole of a text region — tens of
        # thousands of them against a handful of codes and breaks — so it is
        # answered first and without leaving this frame. ``lookup`` is the whole
        # match where every spelling is one code point, which is every font that
        # has no pair glyph; the general probe is only reached when one has.
        # Safe before the two branches below because both test the character this
        # one has already ruled out.
        by_text = self._by_text
        lookup = by_text.get if self._text_sizes in ((), (1,)) else None
        while at < total:
            char = text[at]
            if char != "[" and char != "\n":
                glyph = lookup(char) if lookup else self._longest_text(text, at)
                if glyph is None:
                    miss(char)
                    at += 1
                    continue
                out.append(glyph.code)
                ends.append(False)
                at += len(glyph.text)
                continue
            if text.startswith(ESCAPE, at):
                at += len(ESCAPE)
                glyph = by_text.get("[")
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
                if found is not None and "," in inside:
                    # A command and the cells it swallows. Read whole or not at
                    # all: the operands are cells of their own, so a token half
                    # of which parses would write a command with somebody else's
                    # byte behind it.
                    written = self._commanded(inside)
                    if written is not None:
                        at = found.end()
                        for value in written:
                            emit(value)
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
        return EncodedText(tuple(out), tuple(unknown), tuple(ends))

    def _commanded(self, inside: str) -> list[int] | None:
        """``speed, $00`` as the cells it writes, or None where it is not that.

        The inverse of :meth:`_commanding`, and deliberately strict: **None** for
        anything that is not a head this alphabet can place followed by operands
        that are all plain values. A token half of which parsed would put a
        command in the stream with whatever came after it as its operand, which
        is the one mistake a text format cannot show — so the whole token is
        handed back to :meth:`_encode` to be reported instead.

        The head may be a **name or a hex code**, since that is what
        :meth:`_commanding` writes: a command whose name another code claimed
        first still reads and types back, as ``[$7A, $00]``.

        The operand count is **not** checked against what the command declared.
        A user editing one is mid-thought, the declaration is somebody's reading
        of the format rather than a fact the file states, and the honest thing is
        to write the cells the string actually says.
        """
        head, _, rest = inside.partition(",")
        head = head.strip()
        code = self._names.get(head)
        if code is None:
            if not head.startswith("$") or not _HEX_RE.match(head[1:]):
                return None
            code = int(head[1:], 16)
        out = [code]
        for part in rest.split(","):
            value = part.strip()
            if not value.startswith("$") or not _HEX_RE.match(value[1:]):
                return None
            out.append(int(value[1:], 16))
        return out

    def _longest_text(self, text: str, at: int) -> Glyph | None:
        """The verbatim glyph at ``text[at]``, preferring the longest spelling.

        Longest first so a format with a code for a common pair uses it rather
        than spending two codes on the letters — which is the whole point of
        having one, and the difference between a string fitting its slot and not.

        Probed by the **widths a font actually spells** (:attr:`_text_sizes`)
        rather than by walking every glyph, because this is asked once per
        character of the string: a scan over a hundred-odd glyphs per character
        is what a region of thirty thousand cells pays it in, and a slice
        lookup per distinct width is the same answer for a fraction of it.

        A probe near the end of the string is **truncated** by the slice and can
        answer at a wider size than it asked for — but only with a glyph whose
        whole spelling is there, which is the same glyph the scan would have
        found, since no longer one could have matched in the characters left.
        """
        for size in self._text_sizes:
            glyph = self._by_text.get(text[at : at + size])
            if glyph is not None:
                return glyph
        return None


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

    Answered from the **nearest opener behind the caret** rather than by splitting
    the whole string into pieces (:func:`unit_spans`). The pieces are disjoint and
    in order, so only the last one can contain the caret or end on it — and this
    is asked several times per keystroke, where a walk of the string ahead of the
    caret is a cost that grows with the region while what it decides does not.
    """
    if at <= 0:
        return False
    opener = body.rfind("[", 0, at)
    if opener < 0:
        return False
    # ``[[`` pairs off from the *start* of a run of brackets, so which of them is
    # a real opener is decided by the whole run and not by the one found above: an
    # even run is escapes end to end, and an odd one opens a code on its last
    # bracket. Read in both directions for that reason — the run may continue past
    # the caret, and a real opener there is not behind it.
    start = opener
    while start > 0 and body[start - 1] == "[":
        start -= 1
    end = opener
    while end + 1 < len(body) and body[end + 1] == "[":
        end += 1
    if (end - start) % 2 or end >= at:
        return False
    close = body.find("]", end + 1)
    nested = body.find("[", end + 1)
    if close >= 0 and (nested < 0 or close < nested):
        return end < at < close + 1
    # Unclosed: one past it is the far end of the number being typed, which is
    # where the digits go — where one past a *closed* code is beside it.
    stop = len(body) if nested < 0 else nested
    return end < at <= stop


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

    A number after the name says how many cells it **swallows**:
    ``7A=[speed, 1]`` is a command that reads the cell after it as its operand
    (:attr:`Glyph.params`), and the string then shows the pair together as
    ``[speed, $00]``. Only a count belongs there — what the operand *is* varies
    per occurrence and is in the stream, not in the table.

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
            name, params = split_params(spelling[1:-1])
            name = spell_name(name)
            if name:
                out.append(
                    Glyph(int(digits, 16), name, GlyphRole.CONTROL, params=params)
                )
        else:
            out.append(Glyph(int(digits, 16), spelling))
    return out


def glyphs_from_spec(spec: Iterable[dict]) -> list[Glyph]:
    """Glyphs from the mapping form a project file states them in.

    Each entry names ``code`` and either ``name`` (a command) or ``text`` (a
    character), optionally ``role`` and ``description``. This is the
    **absolute** half of a font alphabet, against the positional run
    :func:`sequential` builds: a code named because the game's code says what it
    is — a line break, a terminator, a command worth a caption — or one the run
    cannot spell, a pair standing behind a single code or a glyph outside the
    sheet. Neither kind moves when the run's origin is dialled, because neither
    was read off the sheet.

    A line with no ``role`` is a **letter**, which is what the common line is:
    the ones that punctuate say so, since what they are is the whole of what the
    reader has to be told.

    A record that does not parse is **skipped, not refused**: this reads a
    project file, which is shared, hand-editable and untrusted, and one bad line
    must not cost the user the rest of their table.

    A **name** is spelled to one word on the way in (:func:`spell_name`), since
    it is what a reader retypes inside ``[...]``. ``text`` is left exactly as
    written, because a character is whatever the sheet draws.

    ``params`` is how many cells the command swallows (:attr:`Glyph.params`),
    and a value that is not a whole number of them reads as none: an operand
    count is a small integer or it is a line somebody mistyped, and a command
    that swallows nothing is what every format gets before anybody says
    otherwise.
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
        role = GlyphRole.TEXT if stated is None else GlyphRole.parse(stated)
        named = str(entry.get("name", ""))
        text = spell_name(named) if named else str(entry.get("text", ""))
        if not named and not role.spells:
            # A command stated the older way, with its name under ``text``.
            text = spell_name(text)
        if not text:
            continue
        try:
            params = max(0, int(entry.get("params", 0)))
        except (TypeError, ValueError):
            params = 0
        out.append(
            Glyph(code, text, role, str(entry.get("description", "")), params=params)
        )
    return out
