"""The interpreted session model the UI binds to and Write serializes.

A :class:`Document` is the point where the two pathways converge (overview.md §2):
the decoded pixel **tiles**, the **palette**, the **view options**, and the two
pathway configs + contexts needed to round-trip. It is Qt-free and mutable — the
editing tools (later) act on it in place; for the view-only MVP the UI reads it and
never mutates.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from celpix.core.arrangement import compose_linear
from celpix.core.context import PipelineContext
from celpix.core.index_grid import IndexGrid
from celpix.core.palette import Palette
from celpix.pipeline.pathway import PathwayConfig


@dataclass
class ViewOptions:
    """How the tiles are laid out and rendered — pure display state.

    ``subpalette_row`` selects which ``2^bpp`` window of a larger palette a tile
    renders through (``base = row * 2**bpp``); the sample ``.pal``s are 256-colour
    CGRAM dumps, so this matters even for viewing.
    """

    columns: int = 16
    zoom: int = 4
    show_grid: bool = False
    subpalette_row: int = 0


@dataclass
class Document:
    pixel_tiles: list[IndexGrid]
    palette: Palette
    pixel_config: PathwayConfig
    palette_config: PathwayConfig
    pixel_ctx: PipelineContext = field(default_factory=PipelineContext)
    palette_ctx: PipelineContext = field(default_factory=PipelineContext)
    view: ViewOptions = field(default_factory=ViewOptions)

    def compose_image(self) -> IndexGrid:
        """The tiles laid out per the current view options, as one image grid."""
        return compose_linear(self.pixel_tiles, self.view.columns)
