"""An indirect-record map: one byte names a record of a definition table, read as
the coordinate of that record's first cell.

The engine half of a **dense stamped chain** (``docs/design/tilemap-entry.md``
§3.1) whose stored value is a *record number* rather than a cell position. A
side-scroller's level map is the usual shape: one byte per 16x16 metatile, no
filler, naming an entry of a table that holds four tile numbers per metatile.

celPix resolves a stamp as ``source cell = index + dx + dy * source_columns``
(``core/tilemap.py``, ``expand_stamps``), so a referring cell's ``index`` has to
be a **position in the source's cell list**. A record number is not one — fed in
raw, record 20 of a packed table would draw cells 20, 21, 22 and 23, which is
the second half of record 5 and the first half of record 6. So the conversion
belongs to the codec, and no data preset can express it; this engine is the one
place it is written, and a project's preset supplies the numbers:

``record_cells``
    cells per record (4 for a 2x2 metatile). A **packed** table — records end
    to end, stated ``record_columns`` cells wide so a record is a square of
    consecutive cells — puts record *k*'s first cell at ``k * record_cells``.
``record_columns``
    cells across one record (2 for a 2x2). Only read when the table is laid out
    as a grid.
``records_across``
    0 (the default) for a packed table. *N* when a reshape has laid the table
    out as a grid *N* records across, so the table reads as a sheet: record
    *k*'s corner is then ``(k // N) * N * record_cells + (k % N) * record_columns``.

``group_bits`` / ``group_starts`` / ``group_rows``
    for a byte whose **top bits choose a sub-table**. ``group_bits`` of them
    (0, the default, for none) name one of ``2 ** group_bits`` groups, the bits
    below them a record within it, and ``group_starts`` lists the record each
    group begins at in the bound table — so the four tables a byte of
    ``PPRRRRRR`` reaches can sit end to end at whatever sizes they have. With
    ``group_rows`` the group is also the cell's **palette row**, which is how a
    metatile carries its colour where the attribute plane is filled from it: the
    table a record is in *is* its row. The row is then the record's consequence
    and not a field of its own, so an edit that changes only the row writes
    nothing new; choosing a record in another group is what moves it — and
    ``settle_cells`` is what moves it, re-deriving the row from the byte the
    encode will write every time a cell list is settled, so the colour on screen
    is the colour a reload gives back.

The chain's other parameters ride on the preset beside these and are read by
the host: ``indirect`` (the cells are coordinates, so the binding bar offers
tables ahead of banks), ``stamp_cells`` or the source's published stamp,
``stamp_dense`` (one entry per stamp, so the picture is the stamp's size times
the file), and ``column_major`` where the file runs down each column.

**An edit snaps.** ``encode`` divides a coordinate back to the record containing
it, so a reference set to a cell that is not a record's corner writes the record
it falls inside — the same rule ``Document.cell_at`` applies to a click anywhere
within a stamp, and the only honest answer for a file that has no way to name
a quarter of a record.

Among the sample projects reading through it: Alex Kidd in Shinobi World and
The Story of Melroon over packed two-cell-wide tables, Super Mario World's Map16
index maps over a table reshaped eight blocks across, and Super Mario Bros.'
scenery columns over four grouped tables.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import replace
from functools import lru_cache
from typing import Any

from celpix.core.context import PipelineContext
from celpix.core.errors import Stage
from celpix.core.tilemap import Cell
from celpix.plugins._params import flag
from celpix.plugins.base import PluginInfo

MAX_RECORD = 0xFF

#: ``(cells per record, cells across one record, records across the table)`` —
#: the table's shape, read once and passed down (:func:`_geometry`).
Geometry = tuple[int, int, int]


def _geometry(params: dict[str, Any]) -> Geometry:
    cells = int(params.get("record_cells", 4))
    columns = int(params.get("record_columns", 2))
    across = int(params.get("records_across", 0))
    if cells < 1 or columns < 1 or across < 0:
        raise ValueError(
            f"record_cells and record_columns must be positive and records_across "
            f"non-negative, got {cells}, {columns}, {across}"
        )
    return cells, columns, across


def _groups(params: dict[str, Any]) -> tuple[int, list[int]]:
    """``(record bits, group starts)``, or ``(8, [0])`` for an ungrouped byte."""
    bits = int(params.get("group_bits", 0))
    if not bits:
        return 8, [0]
    starts = [int(start) for start in params.get("group_starts", ())]
    if not 0 < bits < 8 or len(starts) != 1 << bits or starts != sorted(starts):
        raise ValueError(
            f"group_bits = {bits} needs {1 << bits} ascending group_starts, "
            f"got {starts}"
        )
    return 8 - bits, starts


# The four conversions below come in pairs: a ``_name_at`` taking the parameters
# **already read**, and the public one that reads them. Every walk here is
# per cell — a decode of a screen, an encode of one, a settle on every edit — so
# the parsing is hoisted out of the loop and the loop calls the inner form. The
# outer form is what a caller with only ``params`` in hand wants, and is the
# one the host and the tests reach for.
def _record_at(byte: int, low: int, starts: Sequence[int]) -> tuple[int, int]:
    """``(record, group)`` a stored byte names."""
    group = byte >> low
    return starts[group] + (byte & ((1 << low) - 1)), group


def _byte_at(record: int, low: int, starts: Sequence[int], stored: int) -> int:
    """The byte to store for ``record`` — :func:`_record_at`'s inverse.

    **The one place that choice is made.** ``encode`` writes this byte,
    ``settle_cells`` takes the cell's palette row from its group, and ``decode``
    is its inverse — so the rule stated a second time anywhere else is a row that
    can disagree with the byte the save produces.

    Where groups overlap in reach, the **last** group starting at or before the
    record is the one it belongs to: a group's table ends where the next
    begins, so an earlier group reaching that far is reaching past its own end.

    That pick alone would not be an inverse, because the record does not always
    say which byte wrote it: two tables can start on the same record, and a
    group's six bits reach past its own end into the next table's. So ``stored``
    — the byte the cell was decoded from — **wins whenever it still names the
    record**, which is what keeps a cell nobody touched going back as the byte
    it came from. The same reason :class:`~celpix.core.tilemap.Cell` carries a
    priority bit this format has no meaning for: a decode that cannot be
    inverted has to keep what it could not express.
    """
    stored &= MAX_RECORD
    if low < 8 and _record_at(stored, low, starts)[0] == record:
        return stored
    # Bisected rather than scanned: this runs per cell, and the starts are
    # ascending by construction (:func:`_groups`). ``bisect_right`` lands past the
    # last group starting *at* the record, which is the group that owns it where
    # two tables start together.
    group = bisect_right(starts, max(record, starts[0])) - 1
    # Masked rather than checked, as the packed engine masks a too-wide field:
    # a record past the byte costs its high bits, not the whole save. With the
    # group in its own high bits the result is always a byte, which is what lets
    # every caller hand it straight to ``bytes()``.
    return (group << low) | ((record - starts[group]) & ((1 << low) - 1))


def _corner_at(record: int, geometry: Geometry) -> int:
    """The source cell a record's first (upper-left) cell sits at."""
    cells, columns, across = geometry
    if not across:
        return record * cells
    row, column = divmod(record, across)
    return row * across * cells + column * columns


def _containing_at(cell: int, geometry: Geometry) -> int:
    """The record a source cell falls inside — ``_corner_at``'s inverse, snapping."""
    cells, columns, across = geometry
    cell = max(0, cell)
    if not across:
        return cell // cells
    # A row of records is `across * cells` cells laid out as `cells // columns`
    # source rows of `across * columns` cells each, so the record's column is
    # read off the position within *its* source row, whichever row of the
    # record the cell sits in.
    row, offset = divmod(cell, across * cells)
    return row * across + (offset % (across * columns)) // columns


def record_of(byte: int, params: dict[str, Any]) -> tuple[int, int]:
    """``(record, group)`` a stored byte names."""
    low, starts = _groups(params)
    return _record_at(byte, low, starts)


def byte_of(record: int, params: dict[str, Any], stored: int = 0) -> int:
    """The byte naming ``record``, ``stored`` being the byte it was read as
    (:func:`_byte_at`)."""
    low, starts = _groups(params)
    return _byte_at(record, low, starts, stored)


def corner(record: int, params: dict[str, Any]) -> int:
    """The source cell a record's first (upper-left) cell sits at."""
    return _corner_at(record, _geometry(params))


def containing_record(cell: int, params: dict[str, Any]) -> int:
    """The record a source cell falls inside — ``corner``'s inverse, snapping."""
    return _containing_at(cell, _geometry(params))


@lru_cache(maxsize=8)
def _byte_tables(
    low: int, starts: tuple[int, ...], geometry: Geometry
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """``(source cell, palette row)`` per stored byte — ``settle_cells``' tables.

    What a cell has to hold to be settled, answered by two lookups instead of two
    divisions and a bisect: a cell is settled exactly when the byte it carries
    still names its index and it is drawn in that byte's group. 256 entries covers
    every byte a grouped map can hold, so the tables are complete rather than a
    cache of answers.

    Cached on the numbers they are built from, so a preset pays for them once a
    session rather than once an edit — the *read* parameters and not the params
    dict, which is unhashable and would key two equal layouts apart besides.
    """
    mask = (1 << low) - 1
    bytes_ = range(MAX_RECORD + 1)
    return (
        tuple(
            _corner_at(starts[byte >> low] + (byte & mask), geometry) for byte in bytes_
        ),
        tuple(byte >> low for byte in bytes_),
    )


class IndirectRecordCodec:
    info = PluginInfo(
        id="codec.tilemap.indirect-record",
        name="Indirect record map (byte -> record of a definition table)",
        stage=Stage.INTERPRET_TILEMAP,
        short_name="IREC",
        category="Generic",
    )

    def decode(
        self, data: bytes, params: dict[str, Any], ctx: PipelineContext
    ) -> list[Cell]:
        rows = flag(params, "group_rows")
        low, starts = _groups(params)
        geometry = _geometry(params)
        grouped = low < 8
        cells = []
        for byte in data:
            record, group = _record_at(byte, low, starts)
            cells.append(
                Cell(
                    index=_corner_at(record, geometry),
                    palette_row=group if rows else 0,
                    # The stored byte rides along where a record can be named by
                    # more than one of them, so `encode` can hand back the one
                    # that was read rather than the one the record picks, and
                    # `settle_cells` can take the row from the same byte
                    # (:func:`_byte_at`). `cell_fields` does not declare `flags`,
                    # so nothing offers it as a field to edit.
                    flags=byte if grouped else 0,
                )
            )
        return cells

    def encode(
        self, cells: list[Cell], params: dict[str, Any], ctx: PipelineContext
    ) -> bytes:
        low, starts = _groups(params)
        geometry = _geometry(params)
        return bytes(
            _byte_at(_containing_at(cell.index, geometry), low, starts, cell.flags)
            for cell in cells
        )

    def settle_cells(self, cells: list[Cell], params: dict[str, Any]) -> list[Cell]:
        """``cells`` with every palette row back in step with the byte it will be.

        Only ``group_rows`` has anything to settle. There the row *is* the group
        of the stored byte (the module docstring), so an edit that moves a cell to
        a record in another table has changed its colour and nothing else would
        have said so: the metatile would draw in the row it came from and come
        back in the row it went to. So the row is re-derived here from
        :func:`_byte_at` — the same call ``encode`` makes — and the byte it
        answered is carried with it, which is what keeps the row, the carried byte
        and what a save writes from ever disagreeing.

        The **index is left alone**, though ``encode`` snaps a coordinate to the
        record containing it: the index is the field the user set, not one derived
        from another, and snapping it here would move a reference out from under
        them mid-edit.

        Cheap on the way it is usually asked — a whole list, on every edit, with
        almost every cell already settled. A cell is settled exactly when the byte
        it carries still names its own index and it is drawn in that byte's group,
        which :func:`_byte_tables` answers with two lookups and no arithmetic at
        all. Only a cell that fails that is measured the long way, and the list is
        copied only once something really differs — the identity the host's
        no-change guard reads.
        """
        low, starts = _groups(params)
        if low == 8 or not flag(params, "group_rows"):
            return cells
        geometry = _geometry(params)
        starts = tuple(starts)
        corners, rows = _byte_tables(low, starts, geometry)
        out: list[Cell] | None = None
        for at, cell in enumerate(cells):
            stored = cell.flags
            index = cell.index
            if 0 <= stored <= MAX_RECORD and corners[stored] == index:
                if cell.palette_row == rows[stored]:
                    continue
            # Not settled by the table — a byte from somewhere else, or an index
            # that is not a record's corner, which is a legitimate place for a
            # reference to sit (``encode`` snaps it). Measured the long way, and
            # it may still turn out to agree.
            byte = _byte_at(_containing_at(index, geometry), low, starts, stored)
            row = rows[byte]
            if cell.palette_row == row and stored == byte:
                continue
            if out is None:
                out = list(cells)
            out[at] = replace(cell, palette_row=row, flags=byte)
        return cells if out is None else out

    def bytes_per_cell(self, params: dict[str, Any]) -> int:
        return 1

    def cell_tiles(self, params: dict[str, Any]) -> tuple[int, int]:
        # One coordinate, one source cell. How many *tiles* get drawn is the
        # stamp's answer and then the source's, neither of which is this format's.
        return (1, 1)

    def index_limit(self, params: dict[str, Any]) -> int:
        """The highest coordinate a stored byte can name — the last record's corner."""
        return corner(record_of(MAX_RECORD, params)[0], params)

    def palette_row_limit(self, params: dict[str, Any]) -> int | None:
        # None even with `group_rows`: the row follows the record (the module
        # docstring), so there is no field an assigned row could be stored in.
        # What keeps it following is `settle_cells`, which re-derives it from the
        # byte the encode will write — a derivation and not an assignment, which
        # is the whole reason this stays None.
        return None

    def has_palette_rows(self, params: dict[str, Any]) -> bool:
        """Only with ``group_rows``; otherwise the row is the source cell's.

        Declared rather than left silent, since the host reads a missing answer
        as *true* and would lay a view-wide row over the table's own.
        """
        return flag(params, "group_rows")

    def palette_row_granularity(self, params: dict[str, Any]) -> tuple[int, int]:
        return (1, 1)

    def has_line_flag(self, params: dict[str, Any]) -> bool:
        return False

    def has_visibility(self, params: dict[str, Any]) -> bool:
        return False

    def cell_fields(self, params: dict[str, Any]) -> dict[str, int]:
        return {"index": self.index_limit(params)}

    # `transform_cell` is deliberately absent: a coordinate has no room for a
    # flip, so the toolbar's mirror buttons are refused rather than set in the
    # model and dropped again by `encode`.
