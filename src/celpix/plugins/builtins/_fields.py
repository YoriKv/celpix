"""A packed value's bit layout, written the way its documentation writes it.

A tilemap cell, a palette entry and a direct-color pixel are one problem: an
integer whose bits are parcelled out to named fields. Hardware notes draw that
parcelling as a **letter per bit, most significant first** — the SNES BG entry is
``vhopppcc tttttttt``, the VDP cell is ``PCCVHAAA AAAAAAAA``, the 68000 arcade
colour word is ``RRRRGGGGBBBBRGBx`` — and this module reads exactly that, so a
preset is a transcription of the paragraph above it rather than a translation of
it.

Three things fall out of the notation that a table of positions cannot state:

- **A split field needs no convention.** Hardware that grew a tile number past
  the room left for it parks the extra bits wherever there was space, and a
  Game Boy Color index is bits 0-7 plus bit 8 stranded in the attribute byte.
  Written ``ovh.ippp iiiiiiii`` the two runs of ``i`` are one field in the order
  they are read, so which chunk holds the high bits is carried by where it sits
  rather than by a rule the author has to remember and the reader has to trust.
- **Every bit is accounted for.** A field this engine does not place is dropped
  on write, so a bit nobody named is a byte-exactness bug that no amount of
  re-reading the preset would show. A layout is checked against the value's
  width, which turns that omission into a load error — and makes the choice
  explicit: ``.`` for a bit nothing reads, against a field that carries it.
- **The width is stated once.** ``bytes``/``bytes_per_entry`` and friends stay
  optional; where a preset gives one anyway the two are cross-checked.

Whitespace groups the diagram for counting and means nothing — group in nibbles
and each group is one hex digit. Letters are case-insensitive, since the same
layout is written ``BBGGGRRR`` in one note and ``bbgggrrr`` in the next. ``.``,
``-``, ``0`` and ``x`` all mean a bit no field claims; every diagram in
circulation spells it one of those four ways, so all four are read rather than a
winner being picked.

Each engine supplies its own legend — :mod:`~celpix.plugins.builtins._mask` for
colour components, :mod:`~celpix.plugins.builtins.tilemap_codec` for cell fields
— and a preset may override it, which is what lets a layout keep the mnemonics
of the note it was copied from (``c`` and ``t`` for the two chunks of an SNES
tile number). The result is the chunk masks and their ``(shift, width)`` pairs
that :func:`~celpix.plugins.builtins._mask.gather` and
:func:`~celpix.plugins.builtins._mask.scatter` take, most significant chunk
first.
"""

from __future__ import annotations

from collections.abc import Mapping
from functools import cache

# Ignored entirely: a diagram is grouped so its bits can be counted, and where
# the groups fall is the author's business.
_SEPARATORS = " \t\n\r|"

# A bit no field claims. It decodes to nothing and is written as zero.
_UNUSED = ".-0xX"

# One field's placement: the chunk masks it occupies and their (shift, width)
# pairs, both most significant chunk first.
Field = tuple[tuple[int, ...], tuple[tuple[int, int], ...]]


def bit_width(text: str) -> int:
    """How many bits the layout describes, grouping ignored."""
    return sum(1 for char in text if char not in _SEPARATORS)


def resolve_legend(
    default: Mapping[str, str],
    override: Mapping[str, object] | None,
    known: frozenset[str],
) -> dict[str, str]:
    """``default`` with a preset's own letters merged over it, both checked.

    A letter naming a field this engine does not have is refused rather than
    ignored: silently dropping it would place none of the bits it covers, and a
    layout whose bits are all spoken for is the whole point.
    """
    legend = dict(default)
    for letter, name in (override or {}).items():
        if len(letter) != 1:
            raise ValueError(f"a legend entry names one letter, got {letter!r}")
        if name not in known:
            raise ValueError(
                f"{letter!r} names {name!r}, which is not a field here - "
                f"this layout has {', '.join(sorted(known))}"
            )
        legend[letter.lower()] = str(name)
    return legend


def parse_layout(
    text: str, legend: Mapping[str, str], width: int | None = None
) -> dict[str, Field]:
    """Read a bit layout into ``{field name: placement}``.

    ``legend`` maps a diagram letter to the field it names, case-insensitively.
    ``width`` is how many bits the value really has, checked against the diagram
    when the caller knows it; a field absent from the diagram is absent from the
    result rather than present and empty.
    """
    return dict(_parse(text, tuple(sorted(legend.items())), width))


@cache
def _parse(
    text: str, legend_items: tuple[tuple[str, str], ...], width: int | None
) -> tuple[tuple[str, Field], ...]:
    """:func:`parse_layout` behind a cache — a view refresh re-enters per window.

    Keyed on the legend as sorted items so it hashes, and returning pairs rather
    than the dict so a caller cannot mutate what the next call gets back.
    """
    legend = {letter.lower(): name for letter, name in legend_items}
    bits = [char for char in text if char not in _SEPARATORS]
    if not bits:
        raise ValueError("a bit layout has to name at least one bit")
    if width is not None and len(bits) != width:
        raise ValueError(
            f"the layout describes {len(bits)} bits and the format is {width} - "
            "every bit needs a letter, '.' for one no field claims"
        )

    # Walked most significant bit first, which is how the diagram is written, so
    # a field's chunks come out in that order too — the order gather/scatter
    # want, and the reason a split field needs no ordering rule.
    runs: list[tuple[str, int, int]] = []
    for position, char in enumerate(bits):
        # The legend is consulted first, so a preset can claim one of the
        # spare-bit characters for a real field - which is what a format whose
        # own notes write an attribute as `x` needs.
        name = legend.get(char.lower())
        if name is None:
            if char in _UNUSED:
                continue
            raise ValueError(
                f"{char!r} names no field - this layout takes "
                f"{', '.join(sorted(set(legend)))}, or '.' for a bit no field claims"
            )
        bit = len(bits) - 1 - position
        # Contiguous same-letter bits are one chunk; the same letter picked up
        # again after anything else - another field, or a gap - is a new one.
        if runs and runs[-1][0] == name and runs[-1][1] == bit + 1:
            _, _, run_width = runs[-1]
            runs[-1] = (name, bit, run_width + 1)
        else:
            runs.append((name, bit, 1))

    placed: dict[str, list[tuple[int, int]]] = {}
    for name, shift, run_width in runs:
        placed.setdefault(name, []).append((shift, run_width))
    return tuple(
        (name, (tuple(((1 << w) - 1) << s for s, w in sw), tuple(sw)))
        for name, sw in placed.items()
    )
