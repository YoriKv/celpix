"""Pixel-geometry of the flip/rotate transforms (core, Qt-free)."""

from __future__ import annotations

from celpix.core import transform
from celpix.core.argb_grid import ArgbGrid
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


def test_flip_horizontal_reverses_columns() -> None:
    assert _rows(transform.flip_horizontal(_grid([[1, 2, 3], [4, 5, 6]]))) == [
        [3, 2, 1],
        [6, 5, 4],
    ]


def test_flip_vertical_reverses_rows() -> None:
    assert _rows(transform.flip_vertical(_grid([[1, 2, 3], [4, 5, 6]]))) == [
        [4, 5, 6],
        [1, 2, 3],
    ]


def test_rotate_cw_turns_top_row_into_right_column() -> None:
    # 3×2 → 2×3; the top row (1,2,3) ends up as the right-hand column.
    assert _rows(transform.rotate_cw(_grid([[1, 2, 3], [4, 5, 6]]))) == [
        [4, 1],
        [5, 2],
        [6, 3],
    ]


def test_rotate_ccw_turns_top_row_into_left_column() -> None:
    assert _rows(transform.rotate_ccw(_grid([[1, 2, 3], [4, 5, 6]]))) == [
        [3, 6],
        [2, 5],
        [1, 4],
    ]


def test_flip_is_its_own_inverse() -> None:
    g = _grid([[1, 2, 3], [4, 5, 6]])
    assert _rows(transform.flip_horizontal(transform.flip_horizontal(g))) == _rows(g)
    assert _rows(transform.flip_vertical(transform.flip_vertical(g))) == _rows(g)


def test_rotations_compose_to_identity() -> None:
    g = _grid([[1, 2], [3, 4]])
    # cw ∘ ccw is the identity, and four cw turns come back to the start.
    assert _rows(transform.rotate_ccw(transform.rotate_cw(g))) == _rows(g)
    turned = g
    for _ in range(4):
        turned = transform.rotate_cw(turned)
    assert _rows(turned) == _rows(g)


def test_rotate_swaps_dimensions() -> None:
    g = _grid([[1, 2, 3], [4, 5, 6]])  # 3 wide, 2 tall
    out = transform.rotate_cw(g)
    assert (out.width, out.height) == (2, 3)


def test_transforms_preserve_grid_type_for_argb() -> None:
    # Direct-color grids go through the same code path via type(grid).
    g = ArgbGrid(2, 1)
    g.set(0, 0, 0xFF112233)
    g.set(1, 0, 0xFF445566)
    flipped = transform.flip_horizontal(g)
    assert isinstance(flipped, ArgbGrid)
    assert flipped.get(0, 0) == 0xFF445566
    assert flipped.get(1, 0) == 0xFF112233
