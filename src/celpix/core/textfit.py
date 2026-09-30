"""Fitting typed text to a fontmap's region: cut, fill, and say what came off.

A fontmap's region is fixed in the file, so a string typed into it has to come
out **exactly** that size — the overrun cut off the end, the shortfall filled —
and the text window has to be able to tell the user what was lost
(``docs/design/fontmap-entry.md`` §5). This module owns that arithmetic:
:func:`fit_text` over the region's cells and their byte widths, and the
:class:`TextFit` it answers with. It is pure over its arguments;
:meth:`~celpix.core.document.Document.fit_text` is the document's delegate,
handing in its own cells, widths and record size. Qt-free.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from celpix.core.tilemap import Cell


@dataclass(frozen=True)
class TextFit:
    """A typed string's codes cut and filled to exactly the region it lands in.

    What :meth:`~celpix.core.document.Document.fit_text` hands back, and the
    text window's two readers of it — the write and the budget line — take it
    from one place so they cannot disagree about where the region ends
    (``docs/design/fontmap-entry.md`` §5). ``codes`` and ``ends`` are what the
    cells become; ``lost_codes`` and ``lost_ends`` are what came off, kept so
    they can be read back as text and said out loud. ``used``, ``room`` and
    ``over`` are counted in ``unit``: ``"cells"`` on a fixed-width region,
    ``"bytes"`` on a mixed-width one, where a two-byte letter costs two.
    """

    codes: list[int]
    ends: list[bool]
    lost_codes: list[int]
    lost_ends: list[bool]
    used: int
    room: int
    over: int
    unit: str


def _cut(widths: Sequence[int], start: int, stop: int, room: int) -> tuple[int, int]:
    """How many of ``widths[start:stop]`` fit in ``room`` bytes, and their bytes.

    Whole cells off the front and none after the first that does not fit: a
    letter cannot be half written, and one skipped for a narrower one behind it
    would move text the user placed.
    """
    spent = 0
    at = start
    while at < stop and spent + widths[at] <= room:
        spent += widths[at]
        at += 1
    return at - start, spent


def fit_text(
    cells: list[Cell],
    codes: Sequence[int],
    ends: Sequence[bool],
    blank: int,
    *,
    widths_of: Callable[[list[Cell]], list[int] | None],
    line_bytes: int,
) -> TextFit:
    """``codes`` cut and filled to exactly the fontmap region ``cells`` is.

    **The region is always exactly full** (``docs/design/fontmap-entry.md`` §5):
    the overrun comes off the end and whatever the string gave up is filled with
    ``blank``. Counted in cells on a fixed-width region — the cell count is the
    slot there. Counted in **bytes** where the format says its cells differ
    (``widths_of`` answers, as
    :attr:`~celpix.core.document.Document.cell_widths` does), since that is what
    the slot is fixed in: a string that trades one-byte letters for two-byte
    ones keeps fewer cells, one that trades back keeps more, and either way the
    save writes exactly the bytes the slot has and a reload reads back exactly
    these cells. The cell count moves; nothing past the region does.

    On a mixed-width region the **fill is one byte**: ``blank`` where it
    is, and code zero where it is not — a font whose space is a two-byte
    letter, or a lead byte that would pair with whatever followed it. A
    remainder can be one byte, and only a one-byte fill can meet every
    remainder exactly.

    A **name table** (``line_bytes``, the record size
    :attr:`~celpix.core.document.Document.line_bytes` states) keeps each record
    full instead of the region: typed line *k* is fitted to record *k*, so a
    name run long loses its own tail rather than pushing into the next name, and
    one typed short is padded where it stands. Lines past the last record are
    lost whole, and records no line reached are filled blank.

    ``widths_of`` is the region's byte-width reading of a cell list — None for a
    fixed-width format — asked of the typed cells as well as the region's own,
    since a cell's width can depend on what follows it.
    """
    codes, ends = list(codes), list(ends)
    have = widths_of(cells)
    widths = None
    if have is not None:
        pairs = zip(codes, ends, strict=True)
        widths = widths_of([Cell(index=c, ends_line=e) for c, e in pairs])
    if have is None or widths is None:
        count = len(cells)
        return TextFit(
            codes[:count] + [blank] * (count - len(codes)),
            ends[:count] + [False] * (count - len(ends)),
            codes[count:],
            ends[count:],
            len(codes),
            count,
            max(0, len(codes) - count),
            "cells",
        )
    pair = widths_of([Cell(index=blank), Cell(index=blank)])
    fill = blank if pair is not None and pair[0] == 1 else 0
    size = line_bytes
    room = sum(have)
    # The typed cells as (first, stop) spans, each fitted to a slot of
    # ``size`` bytes: one span and one slot for a stream, a span per typed
    # line and a slot per record for a name table.
    if size:
        lines: list[tuple[int, int]] = []
        start = 0
        for at, end in enumerate(ends):
            if end:
                lines.append((start, at + 1))
                start = at + 1
        if start < len(codes):
            lines.append((start, len(codes)))
        records = sum(1 for cell in cells if cell.ends_line)
        room = records * size
    else:
        lines, records, size = [(0, len(codes))], 1, room
    out_codes: list[int] = []
    out_ends: list[bool] = []
    lost_codes: list[int] = []
    lost_ends: list[bool] = []
    over = 0
    for record in range(max(records, len(lines))):
        first, stop = lines[record] if record < len(lines) else (0, 0)
        if record >= records:
            kept = spent = 0
        else:
            kept, spent = _cut(widths, first, stop, size)
            out_codes += codes[first : first + kept] + [fill] * (size - spent)
            if line_bytes:
                # A record's line ends where it fills, whichever cell that
                # now is: the typed break may have gone with a cut tail, or
                # stand before the fill that pads the record out.
                cut = kept + size - spent
                out_ends += [False] * (cut - 1) + [True]
            else:
                out_ends += ends[first : first + kept] + [False] * (size - spent)
        lost_codes += codes[first + kept : stop]
        lost_ends += ends[first + kept : stop]
        over += sum(widths[first + kept : stop])
    return TextFit(
        out_codes,
        out_ends,
        lost_codes,
        lost_ends,
        sum(widths),
        room,
        over,
        "bytes",
    )
