"""The generic alphabet engine: a font's character lookup, stated as data.

Every ordinary font's code ⇄ letter mapping is a table, so it is a **preset**
rather than code (``docs/design/plugin-system.md``). One engine reads all four
ways the same table gets written down, because which one is convenient depends
entirely on the font:

- ``first`` + ``chars`` — a sheet whose tiles are its letters in order, which is
  most of them. ``first = 0, chars = "ABC…"`` is the whole alphabet.
- ``lines`` — a table file's text inline, one ``20=A`` line per glyph. This is
  the form the scene already writes fonts in and the one a table pasted from
  somewhere else arrives as.
- ``table`` — the same, in a file beside the preset. What a font with a few
  hundred glyphs wants, and what keeps a hand-maintained table diffable.
- ``glyphs`` — an explicit list. The escape hatch within the data tier: a gap in
  the numbering, a line break, a command worth naming.

They **compose**, in that order, later ones overriding earlier: a font that is
mostly a plain run with four oddities in it says the run once and lists the four,
rather than writing out ninety-six lines to get to them.

One more param shapes the reading rather than adding to it: ``order`` —
``"code-first"`` (``20=A``, the default) or ``"text-first"`` (``A=20``, which is
how an assembler's own table directive spells it). Never detected, because both
sides of ``20=A`` parse as hex: a guess reads an all-hex table backwards and does
it silently.

**A container may state the whole thing instead.** Where the pixel pathway
carries :data:`~celpix.core.context.KEY_ALPHABET`, those glyphs are laid over
whatever the params said — the mapping a game-specific loader computed beats a
table nobody could have written by hand, and it reaches the user through this
same data-tier preset rather than needing a code plugin
(``docs/design/fontmap-entry.md`` §4).
"""

from __future__ import annotations

from typing import Any

from celpix.core.context import KEY_ALPHABET, PipelineContext
from celpix.core.errors import Stage
from celpix.core.font import Glyph, glyphs_from_spec, parse_table, sequential
from celpix.plugins.base import ALPHABET_TABLE_ENGINE, PluginInfo


class AlphabetTable:
    """The data-tier alphabet: glyphs assembled from what a preset states."""

    info = PluginInfo(
        id=ALPHABET_TABLE_ENGINE,
        name="Character table",
        stage=Stage.ALPHABET,
    )

    def glyphs(self, params: dict[str, Any], ctx: PipelineContext) -> list[Glyph]:
        """Every glyph this preset describes, in the order they are offered in.

        Later sources override earlier ones **by code**, not by position: a
        glyph list naming ``0x1A`` replaces the ``0x1A`` a character run
        produced, and leaves the rest of the run alone. That is what makes the
        four forms compose into one table instead of four tables that have to be
        kept apart.
        """
        order = str(params.get("order", "code-first"))
        out: list[Glyph] = []
        chars = params.get("chars")
        if chars:
            out += sequential(int(params.get("first", 0) or 0), str(chars))
        lines = params.get("lines")
        if lines:
            out += parse_table(str(lines), order=order)
        table = params.get("table_text")
        if table:
            out += parse_table(str(table), order=order)
        spec = params.get("glyphs")
        if spec:
            out += glyphs_from_spec(spec)
        stated = ctx.get(KEY_ALPHABET)
        if isinstance(stated, str):
            out += parse_table(stated, order=order)
        elif stated:
            out += [g for g in stated if isinstance(g, Glyph)]
        return _last_wins(out)


def _last_wins(glyphs: list[Glyph]) -> list[Glyph]:
    """``glyphs`` with earlier entries for a code dropped, order preserved.

    Order is preserved *at the overridden position* rather than at the
    overriding one, so a preset that fixes four glyphs of a hundred-letter run
    leaves the run reading alphabetically in the picker instead of moving
    its four corrections to the end.
    """
    first: dict[int, int] = {}
    latest: dict[int, Glyph] = {}
    for index, glyph in enumerate(glyphs):
        first.setdefault(glyph.code, index)
        latest[glyph.code] = glyph
    return sorted(latest.values(), key=lambda g: first[g.code])
