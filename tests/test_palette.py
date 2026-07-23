"""The default fallback palette's contract: black, white, then distinct colors,
plus the copy-on-edit helpers color editing is built on."""

from __future__ import annotations

import pytest

from celpix.core.palette import FULL_PALETTE_COUNT, Palette


@pytest.mark.parametrize("count", [2, 4, 16, 256])
def test_default_palette_contract(count: int) -> None:
    colors = Palette.default(count).colors
    assert len(colors) == count
    assert colors[0] == 0xFF000000  # black first: index 0 is usually background
    assert colors[1] == 0xFFFFFFFF  # then white
    assert all(c >> 24 == 0xFF for c in colors)  # fully opaque
    # Contrasting: at least the first 16 must be pairwise distinct.
    head = colors[:16]
    assert len(set(head)) == len(head)


def test_default_palette_is_deterministic_and_prefix_stable() -> None:
    assert Palette.default(256).colors == Palette.default(256).colors
    # Smaller counts are prefixes of larger ones (1bpp sees the same black/white).
    assert Palette.default(4).colors == Palette.default(256).colors[:4]
    assert Palette.default(0).colors == []


def test_with_color_leaves_the_original_untouched() -> None:
    # Load-bearing for undo: PaletteState snapshots hold a Palette by reference,
    # so an edit that mutated in place would silently rewrite history.
    original = Palette.default(16)
    edited = original.with_color(3, 0xFF123456)

    assert edited.color(3) == 0xFF123456
    assert original.color(3) == Palette.default(16).color(3)
    assert original.colors is not edited.colors


@pytest.mark.parametrize("index", [-1, 16, 999])
def test_with_color_ignores_out_of_range_entries(index: int) -> None:
    # Growing the palette here would change its byte length under the codec
    # that writes it, so an out-of-range write is dropped instead.
    palette = Palette.default(16)
    assert palette.with_color(index, 0xFF123456).colors == palette.colors


def test_resized_growth_matches_the_generated_default() -> None:
    # The Custom-from-default fork expands a 4bpp default to a full 16 rows;
    # the added entries must come from the same generator, so the expansion is
    # indistinguishable from having generated the big palette up front.
    grown = Palette.default(16).resized(FULL_PALETTE_COUNT)
    assert len(grown) == FULL_PALETTE_COUNT == 256
    assert grown.colors == Palette.default(FULL_PALETTE_COUNT).colors


def test_resized_preserves_edits_and_truncates() -> None:
    edited = Palette.default(16).with_color(2, 0xFF00CCFF)
    grown = edited.resized(64)
    assert grown.color(2) == 0xFF00CCFF  # an edited color survives the growth
    assert len(grown) == 64
    # Shrinking keeps the leading entries and drops the tail.
    assert grown.resized(4).colors == grown.colors[:4]
