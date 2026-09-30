"""The cell transforms the tilemap-stage codecs answer the same way.

Every built-in format that can mirror a cell does it with the cell's own two
mirror bits, and none can turn one: no format these codecs read has a rotation
bit, and a :class:`~celpix.core.tilemap.Cell` has no field for one to live in.
What differs is only *whether* a given format has the bit, which each codec
settles from its own layout and hands to :func:`mirror`.
"""

from __future__ import annotations

from collections.abc import Callable, Container

from celpix.core.tilemap import Cell, CellOp

_MIRRORS: dict[CellOp, Callable[[Cell], Cell]] = {
    CellOp.FLIP_H: Cell.flipped_h,
    CellOp.FLIP_V: Cell.flipped_v,
}


def mirror(cell: Cell, op: CellOp, placed: Container[str] | None = None) -> Cell | None:
    """``cell`` mirrored by ``op``, or None where the format cannot store it.

    ``placed`` names the fields the format's layout has, spelled as the op's own
    value (``flip_h``, ``flip_v``) — which is what lets "can it" be answered by
    looking the field up rather than by a second table that could disagree with
    the first. None means a format with both mirror bits in every layout.
    """
    toggle = _MIRRORS.get(op)
    if toggle is None or (placed is not None and op.value not in placed):
        return None
    return toggle(cell)
