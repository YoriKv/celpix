"""The interpreted tilemap model: a grid of cells, each naming a tile.

A tilemap is a file of tile *indices* drawn as a layout — a screen, a level's
background, a stamp sheet. Where an :class:`~celpix.core.index_grid.IndexGrid`
holds one palette index per pixel, a :class:`CellGrid` holds one :class:`Cell`
per grid position, and the tile that cell names lives in a *different* document
(``docs/design/tilemap-entry.md`` §3). Nothing here resolves that reference:
this is the codec-neutral model the tilemap pathway decodes into and encodes
back out of, exactly as the index grid is for pixels. Qt-free.

A cell is not a bare index. Every hardware tilemap format worth reading carries
per-cell **attributes** alongside the index — which palette row to draw through,
whether to mirror the tile, and a priority bit that orders it against other
layers. celPix renders none of the priority and only some of the mirroring, and
carries all of it anyway: a field dropped at decode time is a field silently
zeroed on write, which corrupts every file that used it.

Cells are frozen. A tilemap edit replaces a cell rather than mutating one, which
is what lets an undo command hold a plain before/after pair of cells without
copying, and what makes two equal grids compare equal.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, replace
from enum import Enum


class CellOp(str, Enum):
    """A transform a tool asks of one cell, for the *format* to answer.

    Which of these a tilemap can do is a property of the format, not of tilemaps:
    a console BG entry has both mirror bits, a Game Boy map entry has neither and
    a stamp layout's word is a coordinate with no room for any. So the tool names
    the operation and the codec decides — it is the only thing that knows which
    bits, if any, say it (``docs/design/tilemap-entry.md`` §4).

    The rotations are here because the question is the same one and a format that
    has a rotation bit should be able to answer yes. Nothing shipped does, so
    every built-in codec refuses them; what supporting one would take is written
    up in :meth:`~celpix.plugins.base.TilemapCodecPlugin.transform_cell`.

    ``value`` is the toolbar's own field name for the button, so the two cannot
    drift apart (:data:`~celpix.ui.main_window.transform.OP_BY_FIELD`).
    """

    FLIP_H = "flip_h"
    FLIP_V = "flip_v"
    ROTATE_CW = "rotate_cw"
    ROTATE_CCW = "rotate_ccw"

    @property
    def label(self) -> str:
        """How the operation reads in a sentence: "no horizontal flip"."""
        return {
            CellOp.FLIP_H: "horizontal flip",
            CellOp.FLIP_V: "vertical flip",
            CellOp.ROTATE_CW: "rotation",
            CellOp.ROTATE_CCW: "rotation",
        }[self]


@dataclass(frozen=True, slots=True)
class Cell:
    """One tilemap position: which tile, and how to show it.

    ``index`` is the format's own tile number, *before* any base offset the tile
    source applies (``docs/design/tilemap-entry.md`` §3) — the file's number, so
    what is written back is what was read.

    ``priority`` is carried but never rendered. It orders a cell against other
    background layers on hardware, which celPix does not simulate; storing it is
    what keeps a round-trip byte-exact.

    ``flags`` is the same idea generalised: bits a format has that celPix has no
    meaning for at all. A stamp layout's entry carries one saying whether the
    panel cell's own attributes travel with it
    (``docs/graphics-formats-reference/scgcad-formats.md`` §4), which is a
    question about a tool celPix is not. Naming such a bit ``priority`` to get it
    round-tripped would be a lie; dropping it would corrupt the file on the next
    write. So it rides here, uninterpreted and intact.
    """

    index: int = 0
    palette_row: int = 0
    priority: int = 0
    flip_h: bool = False
    flip_v: bool = False
    flags: int = 0

    def flipped_h(self) -> Cell:
        """This cell mirrored horizontally — the attribute toggled, not the tile.

        A tilemap flip costs one bit and no pixels, which is the whole reason
        hardware has the bit. The pixel-side equivalent rewrites the tile
        (``celpix.core.transform``) or stores a display orientation
        (``celpix.core.tilerearrangement``); all three are the same gesture over
        different documents (``docs/design/tilemap-entry.md`` §4).
        """
        return replace(self, flip_h=not self.flip_h)

    def flipped_v(self) -> Cell:
        """This cell mirrored vertically."""
        return replace(self, flip_v=not self.flip_v)


BLANK = Cell()


class CellGrid:
    """A row-major grid of :class:`Cell`, the tilemap pathway's decoded form.

    Sized in **cells**, not pixels or tiles: how many tiles a cell covers is the
    codec's parameter (a panel cell is 2x2 tiles) and how big a tile is belongs
    to the pixel document this grid indexes into. Neither is knowable from the
    grid alone, and neither is needed to hold one.

    Mutable, like the pixel model and unlike :class:`Cell`: an edit sets a
    position, and the positions are the document.
    """

    __slots__ = ("_cells", "_height", "_width")

    def __init__(self, width: int, height: int, fill: Cell = BLANK) -> None:
        if width < 0 or height < 0:
            raise ValueError(f"negative grid size: {width}x{height}")
        self._width = width
        self._height = height
        self._cells: list[Cell] = [fill] * (width * height)

    # -- shape -------------------------------------------------------------
    @property
    def width(self) -> int:
        return self._width

    @property
    def height(self) -> int:
        return self._height

    def __len__(self) -> int:
        return len(self._cells)

    def __iter__(self) -> Iterator[Cell]:
        """Cells in row-major order — the order a codec encodes them back in."""
        return iter(self._cells)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, CellGrid):
            return NotImplemented
        return (
            self._width == other._width
            and self._height == other._height
            and self._cells == other._cells
        )

    def __repr__(self) -> str:
        return f"CellGrid({self._width}x{self._height})"

    # -- access ------------------------------------------------------------
    def contains(self, x: int, y: int) -> bool:
        return 0 <= x < self._width and 0 <= y < self._height

    def get(self, x: int, y: int) -> Cell:
        if not self.contains(x, y):
            raise IndexError(f"({x}, {y}) outside {self._width}x{self._height}")
        return self._cells[y * self._width + x]

    def set(self, x: int, y: int, cell: Cell) -> None:
        if not self.contains(x, y):
            raise IndexError(f"({x}, {y}) outside {self._width}x{self._height}")
        self._cells[y * self._width + x] = cell

    def at(self, position: int) -> Cell:
        """The cell at a flat row-major position — the codec's own coordinate."""
        return self._cells[position]

    # -- construction ------------------------------------------------------
    @classmethod
    def from_cells(cls, width: int, height: int, cells: Iterable[Cell]) -> CellGrid:
        """A grid of ``width`` x ``height`` filled from ``cells``, row-major.

        Short input is padded with blanks and long input is truncated, because a
        decode is fed a byte window that need not divide evenly: a file holding
        a partial final row is a file to show, not one to refuse. The pixel
        pathway takes the same position on a trailing partial tile.
        """
        grid = cls(width, height)
        for i, cell in enumerate(cells):
            if i >= len(grid._cells):
                break
            grid._cells[i] = cell
        return grid

    def copy(self) -> CellGrid:
        grid = CellGrid(self._width, self._height)
        grid._cells = list(self._cells)
        return grid

    # -- regions -----------------------------------------------------------
    def block(self, x: int, y: int, width: int, height: int) -> CellGrid:
        """The ``width`` x ``height`` rectangle at ``(x, y)``, clipped to bounds.

        Clipped rather than refused so a selection dragged past the edge yields
        what is actually there — the same rule the pixel selection follows.
        """
        w = max(0, min(width, self._width - x))
        h = max(0, min(height, self._height - y))
        out = CellGrid(w, h)
        for row in range(h):
            start = (y + row) * self._width + x
            out._cells[row * w : (row + 1) * w] = self._cells[start : start + w]
        return out

    def paste(self, x: int, y: int, block: CellGrid) -> None:
        """Lay ``block`` over this grid at ``(x, y)``, clipped to bounds."""
        for row in range(block.height):
            ty = y + row
            if not 0 <= ty < self._height:
                continue
            for col in range(block.width):
                tx = x + col
                if 0 <= tx < self._width:
                    self._cells[ty * self._width + tx] = block.at(
                        row * block.width + col
                    )

    # -- transforms --------------------------------------------------------
    def flipped_h(self) -> CellGrid:
        """This grid mirrored horizontally: cell order reversed per row, **and**
        every cell's own H-flip toggled.

        Both halves are needed and neither is sufficient. Reversing the order
        alone mirrors the layout while leaving each tile facing its original way;
        toggling the bits alone mirrors each tile in place. A block flip on the
        pixel side is the same compound operation over tiles rather than cells
        (``docs/design/tilemap-entry.md`` §4).
        """
        out = CellGrid(self._width, self._height)
        for y in range(self._height):
            row = self._cells[y * self._width : (y + 1) * self._width]
            out._cells[y * self._width : (y + 1) * self._width] = [
                cell.flipped_h() for cell in reversed(row)
            ]
        return out

    def flipped_v(self) -> CellGrid:
        """This grid mirrored vertically — row order reversed, V-flip toggled."""
        out = CellGrid(self._width, self._height)
        for y in range(self._height):
            src = self._height - 1 - y
            out._cells[y * self._width : (y + 1) * self._width] = [
                cell.flipped_v()
                for cell in self._cells[src * self._width : (src + 1) * self._width]
            ]
        return out
