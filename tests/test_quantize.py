"""Nearest-color matching: the rules an import depends on."""

from __future__ import annotations

from celpix.core.argb_grid import ArgbGrid
from celpix.core.index_grid import IndexGrid
from celpix.core.quantize import ColorMatcher, color_distance
from celpix.pipeline import importer

BLACK = 0xFF000000
WHITE = 0xFFFFFFFF
RED = 0xFFFF0000
GREEN = 0xFF00FF00


def test_exact_color_wins_over_a_nearer_looking_neighbour() -> None:
    # An exact hit short-circuits, so the reported index is the palette's own
    # entry and the match is flagged lossless.
    matcher = ColorMatcher([BLACK, 0xFFFE0000, RED])
    assert matcher.match(RED) == (2, True)
    assert matcher.match(0xFFFD0000) == (1, False)  # nearest, not exact


def test_alpha_is_ignored_when_comparing_colors() -> None:
    # A half-transparent red still reads as red: only the RGB is compared, so
    # source art with soft edges lands on the right hue.
    matcher = ColorMatcher([BLACK, RED], transparent_index=None)
    assert matcher.index_of(0x80FF0000) == 1


def test_transparent_source_snaps_to_the_transparent_index() -> None:
    matcher = ColorMatcher([0x00000000, WHITE, RED], transparent_index=0)
    # Fully transparent white must not be matched as *white*.
    assert matcher.match(0x00FFFFFF) == (0, True)
    # Index 0 holding an opaque color means the hole gained one: not exact.
    opaque_zero = ColorMatcher([BLACK, WHITE], transparent_index=0)
    assert opaque_zero.match(0x00FFFFFF) == (0, False)


def test_fully_transparent_always_snaps_to_index_zero() -> None:
    # alpha 0 is the hole unconditionally — even in "no-hole" mode, where a
    # partly-transparent pixel would be matched by color instead. A pasted
    # image's transparent background must never bleed onto a real color.
    matcher = ColorMatcher([WHITE, RED], transparent_index=None)
    assert matcher.match(0x00FF0000) == (0, False)  # clear red → index 0, gained
    assert matcher.match(0x40FF0000) == (1, True)  # faint red still reads as red


def test_only_alpha_zero_is_transparent() -> None:
    # The opacity cut is >0: a pixel with even a sliver of alpha is a drawn
    # color, not a hole. Barely-there red still matches red rather than snapping
    # to the transparent index at 0.
    matcher = ColorMatcher([0x00000000, RED], transparent_index=0)
    assert matcher.match(0x01FF0000) == (1, True)  # alpha 1 → opaque, matched
    assert matcher.match(0x00FF0000) == (0, True)  # alpha 0 → the clear hole


def test_opaque_pixels_never_land_on_a_transparent_entry() -> None:
    # Entry 0 stores black but is transparent; an opaque black has to take the
    # opaque black at index 2, not the invisible slot.
    matcher = ColorMatcher([0x00000000, WHITE, BLACK])
    assert matcher.match(BLACK) == (2, True)


def test_all_transparent_palette_still_matches() -> None:
    # Degenerate palette: with no opaque candidate the whole set is fair game
    # rather than an empty search.
    matcher = ColorMatcher([0x00000000, 0x00FF0000], transparent_index=None)
    assert matcher.index_of(RED) == 1


def test_distance_weights_green_over_blue() -> None:
    # The perceptual weighting is the point of the metric: an equal-magnitude
    # green error must cost more than a blue one.
    assert color_distance(BLACK, 0xFF002000) > color_distance(BLACK, 0xFF000020)


def _argb(width: int, height: int, colors: list[int]) -> ArgbGrid:
    grid = ArgbGrid(width, height)
    for i, argb in enumerate(colors):
        grid.set(i % width, i // width, argb)
    return grid


def test_import_argb_quantizes_and_cuts_into_tiles() -> None:
    # A 2x1-tile image of solid colors: each 2x2 tile takes one palette index.
    target = importer.ImportTarget(2, 2, colors=(BLACK, RED, GREEN))
    source = _argb(4, 2, [RED, RED, GREEN, GREEN, RED, RED, GREEN, GREEN])
    result = importer.import_argb(source, target)
    assert (result.columns, result.rows) == (2, 1)
    assert [bytes(t.data) for t in result.tiles] == [bytes([1] * 4), bytes([2] * 4)]
    assert result.report.lossless
    assert result.report.source_colors == 2


def test_import_argb_ignores_alpha_when_no_pixel_has_any() -> None:
    # An editor that never writes alpha hands over an all-clear image. Read
    # literally, every pixel would snap to index 0; instead the colors survive
    # because a whole image without alpha is taken as opaque and matched by RGB.
    target = importer.ImportTarget(2, 2, colors=(BLACK, RED, GREEN))
    source = _argb(2, 2, [0x00FF0000, 0x00FF0000, 0x0000FF00, 0x0000FF00])
    result = importer.import_argb(source, target)
    assert bytes(result.tiles[0].data) == bytes([1, 1, 2, 2])
    # One genuinely transparent pixel means alpha *is* meaningful again: the
    # clear pixels then go to the hole, only the opaque one keeps its color.
    mixed = _argb(2, 2, [0x00FF0000, 0x00FF0000, 0x00FF0000, GREEN])
    assert bytes(importer.import_argb(mixed, target).tiles[0].data) == bytes(
        [0, 0, 0, 2]
    )


def test_import_argb_reports_approximated_colors() -> None:
    target = importer.ImportTarget(2, 2, colors=(BLACK, WHITE))
    source = _argb(2, 2, [BLACK, BLACK, 0xFF808080, 0xFF808080])
    result = importer.import_argb(source, target)
    assert not result.report.lossless
    assert (result.report.source_colors, result.report.exact_colors) == (2, 1)
    assert result.report.approximated_colors == 1


def test_import_argb_pads_a_partial_tile() -> None:
    # A 3x3 image into 2x2 tiles: four tiles, edges zero-filled rather than
    # dropped, so a paste of an odd-sized image still lands whole.
    target = importer.ImportTarget(2, 2, colors=(BLACK, WHITE))
    source = _argb(3, 3, [WHITE] * 9)
    result = importer.import_argb(source, target)
    assert len(result.tiles) == 4
    assert bytes(result.tiles[0].data) == bytes([1, 1, 1, 1])
    assert bytes(result.tiles[1].data) == bytes([1, 0, 1, 0])  # right edge padded
    assert bytes(result.tiles[3].data) == bytes([1, 0, 0, 0])  # corner


def test_import_argb_reports_which_tiles_are_only_partly_covered() -> None:
    # The padding above is a placeholder, not data — the caller has to be able
    # to tell it apart so it can leave the file's own pixels there instead.
    target = importer.ImportTarget(2, 2, colors=(BLACK, WHITE))
    result = importer.import_argb(_argb(3, 3, [WHITE] * 9), target)
    assert result.partial
    assert result.covered(0) is None  # whole tile — nothing to merge
    assert (result.covered(1), result.covered(2), result.covered(3)) == (
        (1, 2),
        (2, 1),
        (1, 1),
    )
    # A whole-tile image has nothing to merge anywhere.
    assert not importer.import_argb(_argb(2, 2, [WHITE] * 4), target).partial


def test_merge_uncovered_keeps_the_pixels_the_source_never_reached() -> None:
    # The heart of the partial-tile rule: only the covered rectangle changes, so
    # importing a 3-pixel-wide image can't blank the 4th column of its tiles.
    source = IndexGrid(2, 2, bytearray([1, 0, 1, 0]))
    base = IndexGrid(2, 2, bytearray([7, 7, 7, 7]))
    merged = importer.merge_uncovered(source, base, (1, 2))
    assert bytes(merged.data) == bytes([1, 7, 1, 7])
    # A fully covered tile is the source itself; an uncovered one is untouched.
    assert importer.merge_uncovered(source, base, None) is source
    assert importer.merge_uncovered(source, base, (0, 0)) is base
    # Nothing to merge against (past the end of the data) leaves the source.
    assert importer.merge_uncovered(source, None, (1, 2)) is source


def test_import_argb_direct_color_keeps_pixels() -> None:
    target = importer.ImportTarget(2, 2, direct_color=True)
    source = _argb(2, 2, [RED, GREEN, BLACK, WHITE])
    result = importer.import_argb(source, target)
    assert isinstance(result.tiles[0], ArgbGrid)
    assert result.tiles[0].get(1, 0) == GREEN


def test_import_indexed_remaps_through_the_source_palette() -> None:
    # Indices that don't fit the target are re-fitted by color: source index 3
    # is green, which exists in the target at index 1.
    source_tile = IndexGrid(2, 1, bytearray([3, 0]))
    target = importer.ImportTarget(2, 1, colors=(BLACK, GREEN))
    tiles, report = importer.import_indexed(
        [source_tile], (BLACK, WHITE, RED, GREEN), target
    )
    assert bytes(tiles[0].data) == bytes([1, 0])
    assert report.lossless
