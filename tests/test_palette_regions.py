"""Pinned palette regions: the interval algebra and the addressing that feeds it.

The model is a canonical set of disjoint pixel spans, so the risk is concentrated in
one place: ``assigned``/``cleared`` have to split and re-merge correctly for every
way a new span can overlap an old one. That matrix is what most of this file is.
The rest guards what the pixel anchor is for — a span boundary is a real pixel
whatever the codec's byte layout, and a 2D tile's runs address the picture the
reflow really produces.
"""

from __future__ import annotations

import pytest

from celpix.core.arrangement import reflow_2d, tile_first_pixel, tile_pixel_spans
from celpix.core.index_grid import IndexGrid
from celpix.core.paletteregions import PaletteRegions


def _rows(regions: PaletteRegions) -> list[tuple[int, int, int]]:
    return [(r.start, r.length, r.row) for r in regions.regions]


def test_pinning_merges_abutting_spans_and_canonicalizes() -> None:
    """Two halves pinned separately must equal the whole pinned at once.

    Undo compares before/after and the project file diffs its own output, so two
    spellings of one state would show as a change that never happened.
    """
    piecewise = PaletteRegions().assigned([(0, 32)], 3).assigned([(32, 32)], 3)
    whole = PaletteRegions().assigned([(0, 64)], 3)
    assert piecewise == whole
    assert _rows(whole) == [(0, 64, 3)]
    # Unsorted, overlapping input is normalized the same way.
    assert PaletteRegions().assigned([(32, 32), (0, 48)], 3) == whole


def test_abutting_spans_of_different_rows_stay_separate() -> None:
    regions = PaletteRegions().assigned([(0, 32)], 1).assigned([(32, 32)], 2)
    assert _rows(regions) == [(0, 32, 1), (32, 32, 2)]


@pytest.mark.parametrize(
    ("span", "expected"),
    [
        # Identical: straight replacement.
        ((100, 100), [(100, 100, 9)]),
        # Contained: the old row survives on both sides.
        ((120, 20), [(100, 20, 1), (120, 20, 9), (140, 60, 1)]),
        # Containing: the old region disappears entirely.
        ((50, 200), [(50, 200, 9)]),
        # Straddling the leading edge.
        ((80, 40), [(80, 40, 9), (120, 80, 1)]),
        # Straddling the trailing edge.
        ((180, 40), [(100, 80, 1), (180, 40, 9)]),
        # Disjoint before, abutting exactly - must not fuse (different rows).
        ((60, 40), [(60, 40, 9), (100, 100, 1)]),
        # Disjoint after, with a gap.
        ((300, 20), [(100, 100, 1), (300, 20, 9)]),
    ],
)
def test_pinning_over_an_existing_region(span, expected) -> None:
    """The new pin always wins, and leaves no sliver of the old row behind."""
    base = PaletteRegions().assigned([(100, 100)], 1)
    assert _rows(base.assigned([span], 9)) == expected


@pytest.mark.parametrize(
    ("span", "expected"),
    [
        ((100, 100), []),
        ((120, 20), [(100, 20, 1), (140, 60, 1)]),
        ((50, 200), []),
        ((80, 40), [(120, 80, 1)]),
        ((180, 40), [(100, 80, 1)]),
        ((0, 50), [(100, 100, 1)]),
    ],
)
def test_unpinning_splits_the_region_it_cuts(span, expected) -> None:
    base = PaletteRegions().assigned([(100, 100)], 1)
    assert _rows(base.cleared([span])) == expected


def test_one_pass_clears_across_several_regions() -> None:
    """A wide unpin walks both sorted sequences once rather than per span."""
    base = (
        PaletteRegions()
        .assigned([(0, 16)], 1)
        .assigned([(32, 16)], 2)
        .assigned([(64, 16)], 3)
    )
    assert _rows(base.cleared([(8, 64)])) == [(0, 8, 1), (72, 8, 3)]


def test_lookup_is_half_open_at_both_ends() -> None:
    regions = PaletteRegions().assigned([(100, 10)], 5)
    assert regions.row_at(99, 0) == 0
    assert regions.row_at(100, 0) == 5
    assert regions.row_at(109, 0) == 5
    assert regions.row_at(110, 0) == 0  # one past the end is outside


def test_rows_for_matches_row_at_over_a_window() -> None:
    """The bulk path and the point path must not be able to disagree."""
    regions = PaletteRegions().assigned([(32, 32)], 4).assigned([(96, 16)], 7)
    offsets = list(range(0, 160, 8))
    assert regions.rows_for(offsets, 2) == [regions.row_at(o, 2) for o in offsets]


def test_bounded_drops_spans_past_the_picture_and_rows_past_the_palette() -> None:
    regions = (
        PaletteRegions()
        .assigned([(0, 32)], 1)
        .assigned([(64, 64)], 9)  # row outruns a one-row palette
        .assigned([(200, 40)], 2)  # starts past the last pixel
    )
    bounded = regions.bounded(pixel_count=210, max_row=3)
    assert _rows(bounded) == [(0, 32, 1), (200, 10, 2)]


def test_a_region_follows_the_picture_across_a_bit_depth_switch() -> None:
    """A pinned region keeps covering the art it was drawn over.

    Pin tiles 2-3 at 8x8. Switching bit depth re-cuts the *bytes* but not the tile
    grid, so the same 128 pixels are still tiles 2-3 and the user's colouring stays
    where they put it. This is the deliberate trade of a pixel anchor over a byte
    one: the region follows the picture, not the data.
    """
    per_tile = 8 * 8
    regions = PaletteRegions().assigned([(0, 4 * per_tile)], 6)
    assert [regions.row_at(t * per_tile, 0) for t in range(6)] == [6, 6, 6, 6, 0, 0]

    # And if the tile *size* changes, the region still covers the same area of
    # picture - now one 16x16 tile rather than four 8x8 ones.
    big = 16 * 16
    assert [regions.row_at(t * big, 0) for t in range(3)] == [6, 0, 0]


def test_a_tile_is_pinned_only_when_the_region_holds_its_first_pixel() -> None:
    """The membership rule, stated as a test because it is the surprising part.

    A region that overlaps a tile without containing its first pixel does *not*
    pin it. This is what keeps the lookup a single point query under every walk,
    and it only bites when a span is not tile-aligned - which the pin gesture never
    produces, since it builds spans from whole tiles. A hand-edited project, or a
    tile-size change that re-cuts the grid under an existing region, can.
    """
    per_tile = 64
    regions = PaletteRegions().assigned([(per_tile // 2, per_tile)], 4)
    # Tile 0 is half covered but its first pixel is outside; tile 1's first pixel
    # is inside, so tile 1 is the one that renders through row 4.
    assert regions.row_at(0, 0) == 0
    assert regions.row_at(per_tile, 0) == 4


def test_a_2d_tiles_runs_are_the_pixels_the_reflow_produces() -> None:
    """``tile_pixel_spans`` must name exactly the pixels that become that tile.

    Under the wide-bitmap walk a tile owns no contiguous run, so pinning one records
    one span per pixel row. If those named the wrong pixels a pinned 2D region would
    colour its neighbours instead - so this is checked against the real gather, with
    a packed 1-byte-per-pixel geometry that makes byte position *be* pixel position,
    rather than against a re-derivation of the same arithmetic.
    """
    tile_w, tile_h, cols = 2, 4, 3
    per_tile = tile_w * tile_h  # 1 byte per pixel, so bytes_per_tile == per_tile
    window = bytes(range(per_tile * cols))
    flat = reflow_2d(window, per_tile, tile_h, cols)
    for slot in range(cols):
        gathered = flat[slot * per_tile : (slot + 1) * per_tile]
        spans = tile_pixel_spans(slot, tile_w, tile_h, cols, True)
        assert bytes(b for s, n in spans for b in window[s : s + n]) == gathered
        assert tile_first_pixel(slot, tile_w, tile_h, cols, True) == spans[0][0]


def test_a_2d_tiles_runs_never_overlap_its_neighbours() -> None:
    """Every pixel of a 2D window belongs to exactly one tile.

    The failure this guards is silent: overlapping runs would let one pinned tile
    claim part of the tile beside it, and the picture would come out plausibly but
    subtly wrong.
    """
    tile_w, tile_h, cols = 2, 4, 3
    claimed: dict[int, int] = {}
    for slot in range(cols * 2):
        for start, length in tile_pixel_spans(slot, tile_w, tile_h, cols, True):
            for pixel in range(start, start + length):
                assert pixel not in claimed, f"pixel {pixel} claimed twice"
                claimed[pixel] = slot
    assert sorted(claimed) == list(range(cols * 2 * tile_w * tile_h))


def test_tile_first_pixel_is_monotonic_across_a_2d_stripe_boundary() -> None:
    """Lookups walk slots in order, so the positions they produce must not go back."""
    offsets = [tile_first_pixel(slot, 2, 4, 3, True) for slot in range(9)]
    assert offsets == sorted(offsets)
    assert len(set(offsets)) == len(offsets)  # and no two tiles share a first pixel


def test_shifting_a_grid_moves_every_index_and_saturates() -> None:
    grid = IndexGrid(2, 1, bytes([3, 250]))
    assert bytes(grid.shifted(16).data) == bytes([19, 255])
    assert grid.shifted(0) is grid  # the unpinned path allocates nothing
