"""Chained tilemaps: a map whose cells are coordinates into another map.

A chained map's cells name cells of a *source* map rather than tiles — a layout
over a panel, a field map over a table of metatiles — and a source may itself be
chained, so a chain is a list of hops ending at a map of tile numbers
(``docs/design/tilemap-entry.md`` §3.1). This module owns that: the
:class:`CellChain` a document holds, snapshotting each hop's source and how its
coordinates read it, and :func:`resolve_chain`, the one walk that resolves a
list of cells through every hop into what is drawn. The walk's per-hop pieces —
stamp expansion, corner numbering, attribute composition — are
:mod:`celpix.core.tilemap`'s. Qt-free.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, replace
from itertools import islice

from celpix.core.tilemap import (
    Cell,
    Geometry,
    expand_stamps,
    index_corner,
    resolve_cell,
)


@dataclass(frozen=True)
class CellChain:
    """The tilemap a chained map's cells are coordinates into, and how to read it.

    Held on the document rather than looked up per edit, which is what makes
    resolution a **model** operation: a restamp rebuilds
    :attr:`~celpix.core.document.Document.resolved_cells` from what is already
    here (:meth:`~celpix.core.document.Document.resolve`), with no workspace and
    no reload, so the new stamp is on screen as soon as the cell changes.

    ``source`` is the other map's cell list as it stood when this one was bound.
    Editing *that* map replaces its list rather than mutating it, so the host
    re-points the chain of anything drawing through it
    (``docs/design/tilemap-entry.md`` §3.1) — a snapshot that silently aged would
    be worse than one that is refreshed on the one event that invalidates it.

    ``carry_rows`` is the *referring* format's answer to
    :meth:`~celpix.plugins.base.TilemapCodecPlugin.has_palette_rows`, which is
    not the same question as
    :attr:`~celpix.core.document.Document.cells_carry_palette_rows` — that one
    is true if either side of the chain states rows, because it gates the view's
    palette row. This one says whose row wins per cell.

    ``stamp`` is how many source cells one coordinate names, and it is
    **whichever side states it, the referrer first**: a format whose coordinates
    always name a fixed stamp declares one, and so does a format whose own cell
    covers several units, which over a map can only be several of its cells
    (``project/documents.py``, ``chain_stamp_cells``). Otherwise it is the
    source's answer — a panel states its stamp size in its own header and the
    layout's file does not know it, so the same layout draws differently against
    a differently divided panel. ``(1, 1)`` is the ordinary chain, where one
    coordinate names one cell and there is no stamp to expand.

    ``source_columns`` and ``stamp_column_major`` are the **source's** alone,
    whoever sized the stamp: a stamp is a rectangle cut out of the source as the
    source lays its cells out. ``source_columns`` is the step between a stamp's
    rows (:func:`~celpix.core.tilemap.expand_stamps`) — the stride the source
    publishes for its records, else its width — and where its stamps are stored
    down each column, the step between a stamp's columns instead
    (:func:`~celpix.core.tilemap.stamp_offset`).

    ``dense`` is the **referrer's** alone, set from the referring format rather
    than read off the source's context. It says whether this
    file holds one entry per *stamp* or one per drawn position, and no source
    could know: the same panel is stamped by a layout with a slot per position,
    and would be stamped by a metatile map with a slot per stamp, and the panel's
    header says nothing about either. It is a constant of the referring format,
    so the format's preset declares it (``docs/design/tilemap-entry.md`` §3.1).

    ``base`` is the **binding's**, the chained reading of
    :attr:`~celpix.core.document.Document.tile_base_index`, and it counts what
    this hop's coordinates count: coordinate N names source cell ``base + N``
    where N is a corner, and the corner of stamp ``base + N`` where it counts
    stamps (:func:`~celpix.core.tilemap.index_corner`). A table of 32x32 records
    numbering its 16x16s from partway into their own table needs it for the
    reason a map numbering its tiles from partway into a bank does. Signed, like
    the tile base, and a coordinate it pushes out of the source draws blank.

    ``through`` is the **source's own** chain, where the source is itself a map
    drawing through a map — a field map whose byte names a 2x2 of 16x16 stamps,
    each of those a 2x2 of tiles. Snapshotted with ``source`` and re-pointed with
    it, so a chain is a list of hops (:attr:`hops`) held entirely on the
    referrer, and resolving it (:func:`resolve_chain`) needs no document but this
    one. None for the ordinary chain, whose source's cells are tile numbers.

    ``geometry`` is how this hop's coordinates **number** the source: None where
    a coordinate is the source cell at its stamp's corner, and a
    :data:`~celpix.core.tilemap.Geometry` where it counts stamps — record 20 of
    a packed 2x2 table is cell 80
    (:class:`~celpix.core.tilemap.IndexAddressing`). Converted after the base is
    added and before the stamp walk, and still converted where the stamp
    degrades to one cell (:attr:`~celpix.core.document.Document.stamp_cells`):
    the entry names its stamp's corner cell either way, never the cell its
    number happens to be. The binding's and the referrer's to state, the
    source's to shape (``project/documents.py``, ``index_reading``).
    """

    source: list[Cell]
    carry_rows: bool = True
    stamp: tuple[int, int] = (1, 1)
    source_columns: int = 0
    dense: bool = False
    stamp_column_major: bool = False
    base: int = 0
    through: CellChain | None = None
    geometry: Geometry | None = None

    def source_cell(self, index: int) -> int:
        """The source cell coordinate ``index`` names first — its stamp's
        corner once the base is added in the index's own count
        (:func:`~celpix.core.tilemap.index_corner`). Where it lands may be
        outside the source."""
        return index_corner(index, self.geometry, self.base)

    @property
    def hops(self) -> Iterator[CellChain]:
        """This hop, then each source's own, down to the one whose cells are tiles."""
        hop: CellChain | None = self
        while hop is not None:
            yield hop
            hop = hop.through

    @property
    def growth(self) -> tuple[int, int]:
        """How much wider and taller the hops **after** this one make the picture.

        A dense hop draws one entry as a whole stamp of positions, so each one
        multiplies the picture by its stamp; a sparse hop has an entry per
        position already and multiplies nothing (:func:`resolve_chain`). ``(1,
        1)`` for a chain one hop deep.
        """
        across = down = 1
        hop = self.through
        while hop is not None:
            if hop.dense:
                across *= max(1, hop.stamp[0])
                down *= max(1, hop.stamp[1])
            hop = hop.through
        return across, down

    @property
    def sparse_below(self) -> bool:
        """Whether a hop **after** this one is a sparse stamped map.

        A sparse map holds an entry per drawn position with only its stamps'
        corners meaningful, and which positions those are depends on the width
        the map itself is resolved at. Handed a referrer's cells instead, a
        sparse hop snaps them to corners the source never wrote, so the entry a
        position draws and the entry a click on it edits part company. A sparse
        map is an end product; a chain that draws through one is refused
        (:attr:`~celpix.core.document.Document.stamp_refusal`).
        """
        return any(
            not hop.dense and hop.stamp != (1, 1) for hop in islice(self.hops, 1, None)
        )

    @property
    def drawn_stamp(self) -> tuple[int, int]:
        """How many drawn positions one entry covers once every hop is resolved.

        This hop's stamp grown by the later ones (:attr:`growth`) — a field-map
        byte naming a 2x2 of stamps, each a 2x2 of tiles, is a 4x4 on screen.
        What the chain *states*; whether it can be laid out is the document's
        question (:attr:`~celpix.core.document.Document.stamp_cells`).
        """
        (across, down), (grow_across, grow_down) = self.stamp, self.growth
        return max(1, across) * grow_across, max(1, down) * grow_down


def resolve_chain(
    cells: list[Cell],
    chain: CellChain,
    columns: int,
    *,
    stamped: bool = True,
    carry_rows: bool | None = None,
    dense: bool | None = None,
) -> list[Cell]:
    """``cells`` resolved through every hop of ``chain``, in drawn order.

    **The** resolution walk: the map
    (:meth:`~celpix.core.document.Document.resolve`) and every single-stamp
    preview — the tile source sheet, the stamp tool's ghost, the tile readout —
    go through it, so a preview and the map cannot resolve one coordinate two
    different ways (``docs/design/tilemap-entry.md`` §3.1).

    Each hop takes a list of cells in drawn order at ``columns`` entries across
    and hands the next hop the same, which is what makes depth free: the output
    of a hop is the *source's* cells with the entry's attributes composed on
    (:func:`~celpix.core.tilemap.resolve_cell`), and a source's cells are the
    next hop's entries — coordinates again until the last hop, whose cells are
    tile numbers. A dense hop grows the grid by its stamp, so the width the next
    hop reads its entries at grows with it. A sparse stamped hop after the first
    has no width of its own to snap at here, so a chain holding one is never
    resolved stamped (:attr:`CellChain.sparse_below`,
    :attr:`~celpix.core.document.Document.stamp_cells`).

    ``stamped`` False is the degraded chain
    (:attr:`~celpix.core.document.Document.stamp_cells`): every hop resolves one
    coordinate to one cell — the corner of the stamp it counts to, where the hop
    is ordinal-addressed (:attr:`CellChain.geometry`), since that is the cell
    the entry names. ``carry_rows`` and ``dense`` override the **first** hop's,
    for a preview whose referrer is synthetic (no row of its own to carry) and a
    single entry (dense whatever the file is).

    A row carries **down** the chain: once a referrer with a palette-row field
    has stated one, a later hop whose own format has none must not replace it
    with the bottom table's — so a hop carries rows if it or any hop before it
    does.
    """
    if carry_rows is not None:
        chain = replace(chain, carry_rows=carry_rows)
    if dense is not None:
        chain = replace(chain, dense=dense)
    carry = False
    for hop in chain.hops:
        carry = carry or hop.carry_rows
        stamp = hop.stamp if stamped else (1, 1)
        geometry = hop.geometry
        if stamp == (1, 1):
            # One cell per entry, and under ordinal addressing — degraded or
            # one-cell — still its stamp's corner: entry n draws that cell,
            # never cell n.
            cells = [
                resolve_cell(
                    cell, hop.source, carry_rows=carry, at=hop.source_cell(cell.index)
                )
                for cell in cells
            ]
            continue
        cells = expand_stamps(
            cells,
            hop.source,
            columns,
            stamp,
            hop.source_columns,
            carry_rows=carry,
            dense=hop.dense,
            column_major=hop.stamp_column_major,
            base=hop.base,
            geometry=geometry,
        )
        if hop.dense:
            columns = max(1, columns) * max(1, stamp[0])
    return cells
