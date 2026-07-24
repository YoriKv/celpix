"""Pixel geometry of the drawing tools (core, Qt-free).

Guards the rasterization math that turns a mouse gesture into pixels: line
endpoints, rect/ellipse extents, the flood-fill region walk, and the region
copy/blit the floating selection rides on. Colors and clipping to a live grid
are the UI's job — here we only check which pixels each tool touches.
"""

from __future__ import annotations

from celpix.core import draw
from celpix.core.index_grid import IndexGrid


def _grid(rows: list[list[int]]) -> IndexGrid:
    h, w = len(rows), len(rows[0])
    g = IndexGrid(w, h)
    for y, row in enumerate(rows):
        for x, v in enumerate(row):
            g.set(x, y, v)
    return g


def _rows(g) -> list[list[int]]:
    return [[g.get(x, y) for x in range(g.width)] for y in range(g.height)]


# -- line ------------------------------------------------------------------
def test_line_is_inclusive_and_connected() -> None:
    pts = draw.line(0, 0, 3, 0)
    assert pts == [(0, 0), (1, 0), (2, 0), (3, 0)]


def test_line_single_point() -> None:
    assert draw.line(2, 5, 2, 5) == [(2, 5)]


def test_line_diagonal_and_reversed_match() -> None:
    forward = draw.line(0, 0, 3, 3)
    assert forward == [(0, 0), (1, 1), (2, 2), (3, 3)]
    # Same set of pixels regardless of drag direction.
    assert set(draw.line(3, 3, 0, 0)) == set(forward)


def test_line_steep_has_no_gaps() -> None:
    # A steep line steps one row per pixel — one pixel per y, none skipped.
    ys = [y for _, y in draw.line(0, 0, 1, 5)]
    assert ys == [0, 1, 2, 3, 4, 5]


# -- rectangle -------------------------------------------------------------
def test_rect_outline_is_the_border_only() -> None:
    pts = set(draw.rect_outline(0, 0, 2, 2))
    assert pts == {
        (0, 0),
        (1, 0),
        (2, 0),
        (0, 1),
        (2, 1),
        (0, 2),
        (1, 2),
        (2, 2),
    }  # the centre (1, 1) is absent
    assert (1, 1) not in pts


def test_rect_outline_normalises_corner_order() -> None:
    assert set(draw.rect_outline(2, 2, 0, 0)) == set(draw.rect_outline(0, 0, 2, 2))


def test_rect_outline_degenerate_line() -> None:
    # A zero-height box is a horizontal run with no duplicate top/bottom row.
    assert draw.rect_outline(0, 0, 3, 0) == [(0, 0), (1, 0), (2, 0), (3, 0)]


def test_rect_filled_covers_every_cell() -> None:
    assert set(draw.rect_filled(0, 0, 1, 1)) == {(0, 0), (1, 0), (0, 1), (1, 1)}


# -- ellipse ---------------------------------------------------------------
def test_ellipse_outline_stays_in_bounding_box() -> None:
    pts = draw.ellipse_outline(0, 0, 6, 4)
    xs = [x for x, _ in pts]
    ys = [y for _, y in pts]
    assert min(xs) == 0 and max(xs) == 6
    assert min(ys) == 0 and max(ys) == 4


def test_ellipse_outline_is_symmetric() -> None:
    pts = set(draw.ellipse_outline(0, 0, 6, 4))
    # Mirror across both axes of the box: the curve is four-way symmetric.
    assert all((6 - x, y) in pts and (x, 4 - y) in pts for x, y in pts)


def test_ellipse_filled_includes_centre_and_outline() -> None:
    outline = set(draw.ellipse_outline(0, 0, 6, 4))
    filled = set(draw.ellipse_filled(0, 0, 6, 4))
    assert outline <= filled
    assert (3, 2) in filled  # the centre is filled


# -- flood fill ------------------------------------------------------------
def test_flood_fill_takes_the_contiguous_region() -> None:
    grid = _grid(
        [
            [1, 1, 2],
            [1, 2, 2],
            [3, 2, 2],
        ]
    )
    region = set(draw.flood_fill(grid, 0, 0))
    assert region == {(0, 0), (1, 0), (0, 1)}  # the connected 1s, not the lone 3


def test_flood_fill_is_four_connected_not_diagonal() -> None:
    grid = _grid(
        [
            [1, 0],
            [0, 1],
        ]
    )
    # The two 1s touch only at a corner, so a fill from one never reaches the other.
    assert set(draw.flood_fill(grid, 0, 0)) == {(0, 0)}


def test_flood_fill_whole_uniform_grid() -> None:
    grid = IndexGrid(3, 3)  # all zero
    assert len(draw.flood_fill(grid, 1, 1)) == 9


def test_flood_fill_out_of_bounds_is_empty() -> None:
    assert draw.flood_fill(IndexGrid(2, 2), 5, 5) == []


# -- region copy / blit ----------------------------------------------------
def test_extract_region_copies_the_block() -> None:
    grid = _grid(
        [
            [1, 2, 3],
            [4, 5, 6],
        ]
    )
    region = draw.extract_region(grid, 1, 0, 2, 2)
    assert _rows(region) == [[2, 3], [5, 6]]


def test_extract_region_off_edge_pads_with_zero() -> None:
    grid = _grid([[1, 2], [3, 4]])
    region = draw.extract_region(grid, 1, 1, 2, 2)  # bottom-right corner
    assert _rows(region) == [[4, 0], [0, 0]]


def test_blit_region_round_trips_and_clips() -> None:
    dst = IndexGrid(3, 3)
    draw.blit_region(dst, _grid([[7, 8], [9, 1]]), 2, 2)  # only (2,2) lands
    assert dst.get(2, 2) == 7
    assert _rows(dst) == [[0, 0, 0], [0, 0, 0], [0, 0, 7]]


def test_blit_region_transparent_skips_matching_pixels() -> None:
    dst = _grid([[5, 5], [5, 5]])
    draw.blit_region(dst, _grid([[0, 1], [1, 0]]), 0, 0, transparent=0)
    # The 0s are skipped, so the dst's original 5s show through under them.
    assert _rows(dst) == [[5, 1], [1, 5]]
