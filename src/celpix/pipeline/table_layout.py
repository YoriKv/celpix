"""What a tilemap offers the maps drawn through it: its stamp and its records.

A map in a chain plays up to two roles (``docs/design/tilemap-entry.md`` §3.1).
It **draws from** the table below it — its coordinates name that table's cells,
a stamp at a time — and it may **offer** stamps to a map above it, which needs
to know how this table's cells group. The first is the referrer's own business
and the host reads it off the referrer's preset (``stamp_cells``,
``stamp_dense``). The second is a fact about *this* table, stated by its preset
and no cell of it, and this module is where the host reads it — for every
engine alike, so no engine has to know that chains exist:

``offered_stamp_cells``
    ``[across, down]``: how many of this table's cells one coordinate of a map
    above names. **Falls back to** ``stamp_cells`` where a preset states no offer
    of its own, which is what every preset written before the two were told apart
    means by it — a stamp table's ``stamp_cells`` is its offer, a referrer's is
    what it draws, and a table in the middle of a chain drawing and offering the
    same size needs only the one. The two differ where a middle table's stamp is
    not its source's: a chunk of 8x8 blocks drawing 2x2 of the block table below.
``stamp_stride``
    Cells between a stamp's rows in this table's cell list: 2 for 2x2 records
    packed end to end, whatever width the table is shown at.
``stamp_order``
    ``"row"`` (default) or ``"column"``: a stamp's cells stored down each column,
    the stride then stepping between its columns.
``record_shape`` / ``record_order``
    The record the table's cells come in, so each draws as its own rectangle.
    Stated outright for a table nothing stamps from (a sprite frame); otherwise
    it follows from an offered stamp whose rows sit one stamp apart.

**What the file states wins.** A container that reads the stamp from a header
(an S-CG-CAD panel) or a code format that works it out while decoding has
already put it on the context, and the preset only fills what is still unset —
the rule page geometry keeps too. Qt-free.
"""

from __future__ import annotations

from typing import Any

from celpix.core.context import (
    KEY_TILEMAP_PAGE_ROWS,
    KEY_TILEMAP_RECORD_COLUMN_MAJOR,
    KEY_TILEMAP_RECORD_SHAPE,
    KEY_TILEMAP_STAMP_CELLS,
    KEY_TILEMAP_STAMP_COLUMN_MAJOR,
    KEY_TILEMAP_STAMP_STRIDE,
    PipelineContext,
)

#: The preset parameter naming the stamp a table offers the maps above it.
OFFERED_STAMP = "offered_stamp_cells"


def _pair(value: object, name: str) -> tuple[int, int]:
    """``[across, down]`` as two positive ints, or a message naming ``name``."""
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f"{name} must be [across, down], got {value!r}")
    try:
        return max(1, int(value[0])), max(1, int(value[1]))
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be [across, down], got {value!r}") from None


def offered_stamp(params: dict[str, Any]) -> tuple[int, int] | None:
    """The stamp ``params`` offers a map above, or None where it offers none."""
    if OFFERED_STAMP in params:
        return _pair(params[OFFERED_STAMP], OFFERED_STAMP)
    if "stamp_cells" in params:
        return _pair(params["stamp_cells"], "stamp_cells")
    return None


def _unset(ctx: PipelineContext, key: str) -> bool:
    return ctx.get(key) is None


def publish_table_layout(
    cells: int, params: dict[str, Any], ctx: PipelineContext
) -> None:
    """Put what ``params`` states about the table's layout on ``ctx``.

    Called by the host after the tilemap codec has decoded ``cells`` cells, so a
    code format's own answer — and a container's before it — is already there
    and kept (the module docstring). Raises ``ValueError`` on a declaration the
    readers would otherwise misread.
    """
    stamp = offered_stamp(params)
    stated = params.get("stamp_stride")
    stride: int | None = None
    if stated is not None:
        try:
            stride = max(1, int(stated))
        except (TypeError, ValueError):
            raise ValueError(f"stamp_stride must be a number, got {stated!r}") from None
    order = params.get("stamp_order", "row")
    if order not in ("row", "column"):
        raise ValueError(f"stamp_order must be 'row' or 'column', got {order!r}")
    if order == "column" and stride is None:
        # Required rather than defaulted: the fallback stride is the width the
        # table is viewed at, which is a row step and means nothing down a column.
        raise ValueError('stamp_order = "column" needs a stamp_stride')

    if stamp is not None and _unset(ctx, KEY_TILEMAP_STAMP_CELLS):
        ctx.set(KEY_TILEMAP_STAMP_CELLS, stamp)
    if stride is not None and _unset(ctx, KEY_TILEMAP_STAMP_STRIDE):
        ctx.set(KEY_TILEMAP_STAMP_STRIDE, stride)
    if order == "column" and _unset(ctx, KEY_TILEMAP_STAMP_COLUMN_MAJOR):
        ctx.set(KEY_TILEMAP_STAMP_COLUMN_MAJOR, True)

    if _unset(ctx, KEY_TILEMAP_RECORD_SHAPE):
        _publish_records(cells, params, stamp, stride, order, ctx)


def _publish_records(
    cells: int,
    params: dict[str, Any],
    stamp: tuple[int, int] | None,
    stride: int | None,
    order: str,
    ctx: PipelineContext,
) -> None:
    """State the record a table's cells come in, so each draws as a rectangle.

    Stated outright by ``record_shape = [across, down]`` (with ``record_order``)
    for a table nothing stamps from — a sprite frame kept as its tile numbers.
    Otherwise it follows from the offered stamp wherever that describes
    **contiguous** records: a stamp whose rows (or, stored down each column,
    whose columns) sit exactly one stamp apart is a run of consecutive cells. A
    stride wider than that means the stamps are windows on a real map, and a map
    is drawn as the map it is.

    Not claimed on a paged file, whose assembly is already the layout, nor where
    the cell count is not a whole number of records — the same honesty a page
    count keeps: a table read under the wrong cell size is left looking wrong.
    """
    shape = params.get("record_shape")
    if shape is None:
        if stamp is None or stride is None:
            return
        across, down = stamp
        if stride != (down if order == "column" else across):
            return
        record_order = order
    else:
        across, down = _pair(shape, "record_shape")
        record_order = params.get("record_order", "row")
        if record_order not in ("row", "column"):
            raise ValueError(
                f"record_order must be 'row' or 'column', got {record_order!r}"
            )
    size = across * down
    if size == 1 or not cells or cells % size or ctx.get(KEY_TILEMAP_PAGE_ROWS):
        return
    ctx.set(KEY_TILEMAP_RECORD_SHAPE, (across, down))
    ctx.set(KEY_TILEMAP_RECORD_COLUMN_MAJOR, record_order == "column")
