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
from functools import lru_cache

from celpix.core.arrangement import BlockLayout

# The index step between one row of a multi-tile cell and the next. **Not** the
# cell's own width: console VRAM behaves as a 16-tile-wide array, so a 16x16 BG
# cell draws tiles N, N+1, N+0x10, N+0x11
# (``docs/graphics-formats-reference/snes-hardware-notes.md`` §5). A sprite
# object's multi-tile subsprite steps through the same array the same way
# (:data:`~celpix.core.sprite.Subsprite.tile_indices`), which is why this is one
# constant rather than a coincidence in two places.
VRAM_ROW_STRIDE = 0x10


def tile_run(
    first: int,
    across: int,
    down: int,
    stride: int,
    *,
    flip_h: bool = False,
    flip_v: bool = False,
) -> list[int]:
    """The tiles a multi-tile thing draws, in the order they appear on screen.

    One index for an ordinary hardware cell; four for a 16x16 metatile or a 16x16
    subsprite, stepped by ``stride`` rather than by ``across``
    (:data:`VRAM_ROW_STRIDE`).

    **A flip reverses the order as well as mirroring each tile.** A mirrored
    metatile shows its right-hand tile on the left, mirrored; toggling the bits
    alone would mirror each tile in place and leave the layout unmirrored, and
    reversing alone would move them without turning them. Both halves are needed
    and neither is sufficient — the same rule :meth:`CellGrid.flipped_h` follows
    over a block of cells. Only the ordering is here; mirroring the tile's pixels
    is the renderer's half.

    Shared by the two things that index a tile array this way, a tilemap cell
    (:meth:`~celpix.core.document.Document.cell_tile_indices`) and a subsprite
    (:meth:`~celpix.core.sprite.Subsprite.tile_indices`), so the rule cannot come out
    different depending on which document is on screen.
    """
    if across == 1 and down == 1:
        # The ordinary hardware cell, and the one a whole map is made of: no
        # stride to step and no order for a flip to reverse. Answered without
        # the walk because a repaint asks this once per cell, thousands of times.
        return [first]
    return [
        first
        + (down - 1 - row if flip_v else row) * stride
        + (across - 1 - col if flip_h else col)
        for row in range(down)
        for col in range(across)
    ]


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
    authoring tool put anything at that position
    (``docs/graphics-formats-reference/scgcad-formats.md`` §4) — a per-position
    visibility celPix has nothing to map onto. Naming such a bit ``priority`` to
    get it round-tripped would be a lie; dropping it would corrupt the file on the
    next write. So it rides here, uninterpreted and intact.
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


def resolve_cell(
    cell: Cell, source: list[Cell], *, carry_rows: bool, at: int | None = None
) -> Cell:
    """The cell ``cell`` names in the tilemap it draws through, or a blank.

    Where an ordinary cell's ``index`` is a tile number, a chained one is a
    position in another tilemap's cells (``docs/design/tilemap-entry.md`` §3.1).
    What comes back is that cell whole — its tile, its palette row, its flips —
    with the referring cell's own attributes composed on top.

    ``at`` overrides which source position is read while leaving the referring
    cell's own attributes in force. That is what a **stamp** needs: one entry
    names a whole block, and the positions inside it read neighbouring source
    cells off the one coordinate (:func:`expand_stamps`). A parameter rather than
    a rebuilt ``Cell`` because a restamp re-resolves every position in the map
    and the copies would be the bulk of the work.

    Composed rather than dropped because the referring format may carry
    attributes of its own, and discarding them would draw a picture neither file
    describes. Both rules reduce to *the source cell untouched* for a format with
    no such field, which is exactly what a stamp layout's coordinate word is:

    - **Flips toggle** rather than overwrite, the same rule :meth:`CellGrid.
      flipped_h` follows — a mirrored reference to an already-mirrored stamp
      faces its original way. A format with no flip bits decodes ``False`` and
      changes nothing.
    - **The palette row is the referrer's only where its format has one to
      state** (``carry_rows``, from
      :meth:`~celpix.plugins.base.TilemapCodecPlugin.has_palette_rows`). A
      decoded 0 from a format without the field means "no row here", not "row
      0", and letting it through would silently black out the source's rows.

    Priority and ``flags`` come from the source cell. Neither is rendered, and a
    referring cell's flags are its own uninterpreted bits — a stamp layout's
    drawn flag among them, which says whether the authoring tool would have put
    anything at this position at all
    (``docs/graphics-formats-reference/scgcad-formats.md`` §4). celPix has no
    per-position visibility to map that onto, so it rides in ``flags`` and a
    restamp writes it back exactly as it was read.
    """
    index = cell.index if at is None else at
    if not 0 <= index < len(source):
        # A reference the source does not have draws blank rather than failing: a
        # layout outliving the panel it was authored against is ordinary, and so
        # is a restamp typed past the end of the panel.
        return BLANK
    found = source[index]
    if not carry_rows and not (cell.flip_h or cell.flip_v):
        # A coordinate-only format has nothing to compose, so the source cell
        # comes back as itself — the same object, which is what keeps a 4096-cell
        # layout from rebuilding every cell it resolves.
        return found
    return replace(
        found,
        palette_row=cell.palette_row if carry_rows else found.palette_row,
        flip_h=found.flip_h != cell.flip_h,
        flip_v=found.flip_v != cell.flip_v,
    )


def stamp_origin(position: int, columns: int, stamp: tuple[int, int]) -> int:
    """Which entry the drawn position ``position`` takes its stamp from.

    A stamped map's entries are **not** one per position: an entry names a whole
    ``stamp``-sized block, and the positions between two entries hold whatever
    the file last had there. So a position reads the entry at its block's
    top-left corner, and the three-quarters of a 2x2 map that were never written
    are never read either
    (``docs/graphics-formats-reference/scgcad-formats.md`` §4).

    The same snap answers both directions, which is why it is one function: it
    picks the entry a position *draws*, and the entry a click on that position
    *restamps* (:meth:`~celpix.core.document.Document.cell_at`). An edit anywhere
    inside a block changes the block.
    """
    across, down = max(1, stamp[0]), max(1, stamp[1])
    x, y = position % columns, position // columns
    return (y - y % down) * columns + (x - x % across)


def expand_stamps(
    cells: list[Cell],
    source: list[Cell],
    columns: int,
    stamp: tuple[int, int],
    source_columns: int,
    *,
    carry_rows: bool,
) -> list[Cell]:
    """Resolve a stamped map into one source cell per **drawn position**.

    The referring map is a grid of blocks and the source is a grid of cells, and
    this is the one place the two shapes meet: the entry at a block's corner
    names the source cell its corner draws, and the rest of the block walks the
    source's *own* rows from there — offset ``x % across + (y % down) *
    source_columns``. That last term is why ``source_columns`` is a parameter and
    not the referrer's width: the block is a rectangle cut out of the source, so
    stepping down a row inside it is a step of the source's width.

    One entry in, ``across * down`` positions out — so the list that comes back
    is the same length as ``cells`` whenever the map divides evenly, and it is in
    **drawn** order rather than file order. Everything that indexes the file
    (a save, the hex dump, a restamp) goes through
    :meth:`~celpix.core.document.Document.cell_at` instead, which is what keeps
    the two orders from being confused for each other.

    **Not** :func:`tile_run`, and the difference is the whole reason both exist.
    Each makes a 2x2 unit out of four tiles and they are otherwise nothing alike:

    - A **hardware metatile** is one cell whose single index names four
      *characters*, stepped by the VRAM row (:data:`VRAM_ROW_STRIDE`) and sharing
      that one cell's palette row and flips. One set of attributes, four tiles.
    - A **stamp** is one coordinate naming four *cells* of another map, stepped by
      that map's row, each carrying its own tile, row, priority and flips. Four
      sets of attributes, four tiles.

    Applying the first rule to the second's data reads four tiles from the wrong
    place and flattens four attribute sets into one. The two can also compose — a
    stamped map whose source has metatile cells — which is why the strides are
    separate parameters rather than one shared notion of "how big a unit is".
    """
    across, down = max(1, stamp[0]), max(1, stamp[1])
    stride = max(1, source_columns)
    out: list[Cell] = []
    for position in range(len(cells)):
        at = stamp_origin(position, columns, (across, down))
        if not 0 <= at < len(cells):
            out.append(BLANK)
            continue
        entry = cells[at]
        offset = position % columns % across + position // columns % down * stride
        out.append(
            resolve_cell(entry, source, carry_rows=carry_rows, at=entry.index + offset)
        )
    return out


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


# -- page assembly ---------------------------------------------------------
#
# Some formats hold several independent maps end to end in one file — a screen
# file is four 32x32 screens — and nothing in the file records how they make up
# a larger picture (``docs/graphics-formats-reference/scgcad-formats.md`` §2).
# Read back to back the four stack in a column, which is a reading no console
# ever used; laid two across they are the 64x64 screen the hardware assembles.
# So the assembly is a **view choice** over pages the container declares, and
# these three functions are the whole of it: which choices exist, which one to
# start on, and the placement itself (``docs/design/tilemap-entry.md`` §6).
#
# Placement is display-only, exactly like the block arrangement it reuses: the
# cells stay in the file's own order and only *where each one is drawn* moves.


def page_assemblies(pages: int) -> tuple[int, ...]:
    """The pages-across values that lay ``pages`` pages out as a whole rectangle.

    The divisors of ``pages``, so every choice fills its last row of pages: a
    partial one would leave holes in the middle of the picture at some other
    width, and there is no reading of a screen file in which a quarter of it is
    absent. Four screens therefore assemble 1x4, 2x2 or 4x1 — the arrangements
    that show all of them.
    """
    if pages <= 0:
        return ()
    return tuple(across for across in range(1, pages + 1) if pages % across == 0)


def default_pages_across(page_columns: int, page_rows: int, pages: int) -> int:
    """The assembly to open a paged file on: the one closest to square in cells.

    A guess either way — the file does not say — so the one to make is the least
    misleading. For the four 32x32 screens this exists for, square is 2x2, which
    is also the console's own multi-screen order and what the one independent
    viewer of the format draws (``scgcad-formats.md`` §2 "Screen assembly"), so
    the general rule and the specific evidence agree rather than having to be
    reconciled.

    Ties go to the **wider** layout: two pages read side by side before they read
    stacked, and the console's own two-screen mode is the horizontal one.
    """
    options = page_assemblies(pages)
    if not options:
        return 1
    width, height = max(1, page_columns), max(1, page_rows)

    def squareness(across: int) -> tuple[float, int]:
        w, h = across * width, (pages // across) * height
        # Aspect as a ratio ≥ 1 either way, so tall and wide are compared by how
        # far from square they are rather than by which axis is longer.
        return max(w, h) / min(w, h), -across

    return min(options, key=squareness)


def resolve_pages_across(
    wanted: int, page_columns: int, page_rows: int, pages: int
) -> int:
    """``wanted`` if it is an assembly this file has, else the default one.

    One place decides, so a project that stored an assembly for a file whose
    format has since been read differently — a different cell size, so a
    different page count — opens on something sensible instead of a layout that
    would shear the picture. 0 means "nothing chosen", which is what a project
    written before assemblies existed says, and lands on the default too.
    """
    if wanted in page_assemblies(pages):
        return wanted
    return default_pages_across(page_columns, page_rows, pages)


@lru_cache(maxsize=16)
def page_order(
    page_columns: int, page_rows: int, pages: int, pages_across: int
) -> tuple[int, ...]:
    """Which cell each drawn position shows, for a ``pages_across`` assembly.

    A **page is a block** and the assembly is a row of blocks, which is exactly
    what :class:`~celpix.core.arrangement.BlockLayout` already maps — so the
    placement is that class's, asked at cell scale instead of tile scale, and the
    one thing this adds is inverting it: the composer wants "position *p* draws
    cell *n*" where the layout answers "cell *n* is drawn at (x, y)".

    Cached because a repaint asks for it and it depends on four numbers that
    change only when the file or the assembly does. The result is a tuple for the
    same reason: a shared cached list a caller could mutate is a bug waiting for
    the second caller.
    """
    width = max(1, pages_across) * max(1, page_columns)
    count = max(0, pages) * max(1, page_columns) * max(1, page_rows)
    if pages <= 0 or pages_across <= 0 or pages % pages_across:
        # Not a whole rectangle of pages: some drawn position would have no cell
        # and the inversion below would leave it pointing at cell 0. The file's
        # own order is the honest fallback (:func:`resolve_pages_across` keeps
        # callers off this path).
        return tuple(range(count))
    layout = BlockLayout(width, page_columns, page_rows)
    order = [0] * count
    for position in range(count):
        x, y = layout.slot_to_cell(position)
        order[y * width + x] = position
    return tuple(order)
