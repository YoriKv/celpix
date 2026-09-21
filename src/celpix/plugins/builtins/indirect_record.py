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
    nothing new; choosing a record in another group is what moves it.

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

Four projects read through it: Alex Kidd in Shinobi World and The Story of
Melroon over packed two-cell-wide tables, Super Mario World's Map16 index maps
over a table reshaped eight blocks across, and Super Mario Bros.' scenery
columns over four grouped tables.
"""

from __future__ import annotations

from typing import Any

from celpix.core.context import PipelineContext
from celpix.core.errors import Stage
from celpix.core.tilemap import Cell
from celpix.plugins.base import PluginInfo

MAX_RECORD = 0xFF


def _geometry(params: dict[str, Any]) -> tuple[int, int, int]:
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


def record_of(byte: int, params: dict[str, Any]) -> tuple[int, int]:
    """``(record, group)`` a stored byte names."""
    low, starts = _groups(params)
    group = byte >> low
    return starts[group] + (byte & ((1 << low) - 1)), group


def byte_of(record: int, params: dict[str, Any]) -> int:
    """The byte naming ``record`` — :func:`record_of`'s inverse.

    Where groups overlap in reach, the **last** group starting at or before the
    record is the one it belongs to: a group's table ends where the next
    begins, so an earlier group reaching that far is reaching past its own end.
    """
    low, starts = _groups(params)
    group = max(g for g, start in enumerate(starts) if start <= max(record, starts[0]))
    # Masked rather than checked, as the packed engine masks a too-wide field:
    # a record past the byte costs its high bits, not the whole save.
    return (group << low) | ((record - starts[group]) & ((1 << low) - 1))


def corner(record: int, params: dict[str, Any]) -> int:
    """The source cell a record's first (upper-left) cell sits at."""
    cells, columns, across = _geometry(params)
    if not across:
        return record * cells
    row, column = divmod(record, across)
    return row * across * cells + column * columns


def containing_record(cell: int, params: dict[str, Any]) -> int:
    """The record a source cell falls inside — ``corner``'s inverse, snapping."""
    cells, columns, across = _geometry(params)
    cell = max(0, cell)
    if not across:
        return cell // cells
    # A row of records is `across * cells` cells laid out as `cells // columns`
    # source rows of `across * columns` cells each, so the record's column is
    # read off the position within *its* source row, whichever row of the
    # record the cell sits in.
    row, offset = divmod(cell, across * cells)
    return row * across + (offset % (across * columns)) // columns


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
        rows = bool(params.get("group_rows"))
        cells = []
        for byte in data:
            record, group = record_of(byte, params)
            cells.append(
                Cell(index=corner(record, params), palette_row=group if rows else 0)
            )
        return cells

    def encode(
        self, cells: list[Cell], params: dict[str, Any], ctx: PipelineContext
    ) -> bytes:
        return bytes(
            byte_of(containing_record(cell.index, params), params) & MAX_RECORD
            for cell in cells
        )

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
        return None

    def has_palette_rows(self, params: dict[str, Any]) -> bool:
        """Only with ``group_rows``; otherwise the row is the source cell's.

        Declared rather than left silent, since the host reads a missing answer
        as *true* and would lay a view-wide row over the table's own.
        """
        return bool(params.get("group_rows"))

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
