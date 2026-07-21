"""The default fallback palette's contract: black, white, then distinct colours."""

from __future__ import annotations

import pytest

from celpix.core.palette import Palette


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
