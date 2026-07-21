"""The interpreted session model the UI binds to and Write serializes.

A :class:`Document` is the point where the two pathways converge (overview.md §2):
the **pixel bytes** (decompressed, decoded on demand a window at a time), the
**palette**, the **view options**, and the two pathway configs + contexts needed to
round-trip. It is Qt-free and mutable — the editing tools (later) act on it in
place; for the view-only MVP the UI reads it and never mutates.

**Deferred decoding.** Large files are never decoded whole: the document holds the
raw pixel bytes plus the codec's atomic geometry (bytes/tile, tile pixel size), and
the view decodes only the visible window of tiles on demand (via
``pipeline.decode_window``). The bytes are the source of truth; an editing path will
re-encode changed tiles back into them.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from celpix.core.context import PipelineContext
from celpix.core.palette import Palette
from celpix.pipeline.pathway import PathwayConfig


@dataclass
class ViewOptions:
    """How the tiles are laid out and rendered — pure display state.

    ``subpalette_row`` selects which ``2^bpp`` window of a larger palette a tile
    renders through (``base = row * 2**bpp``); the sample ``.pal``s are 256-colour
    CGRAM dumps, so this matters even for viewing.

    Large files are viewed through a fixed **window**: ``rows`` tile-rows starting
    at tile ``offset`` (the top-left corner of the view). Navigation moves
    ``offset`` — ±``columns`` for a row step, ±1 for a tile step — instead of
    free-scrolling the whole file, so only the window is ever decoded/rendered.
    """

    columns: int = 16
    rows: int = 16
    zoom: int = 4
    show_grid: bool = False
    subpalette_row: int = 0
    offset: int = 0  # top-left tile index into the pixel bytes


@dataclass
class Document:
    pixel_data: bytes  # raw, decompressed pixel bytes — the whole file
    bytes_per_tile: int  # codec geometry, for slicing/indexing the bytes by tile
    tile_width: int
    tile_height: int
    palette: Palette
    pixel_config: PathwayConfig
    palette_config: PathwayConfig
    pixel_ctx: PipelineContext = field(default_factory=PipelineContext)
    palette_ctx: PipelineContext = field(default_factory=PipelineContext)
    view: ViewOptions = field(default_factory=ViewOptions)

    @property
    def tile_count(self) -> int:
        return len(self.pixel_data) // self.bytes_per_tile if self.bytes_per_tile else 0

    def window_bytes(self, first_tile: int, count: int) -> bytes:
        """The raw byte slice for ``count`` tiles starting at tile ``first_tile``.

        Clamped to the data, so a partial window at the file's end yields fewer
        tiles' worth of bytes (and an out-of-range request yields ``b""``). The
        codec decodes exactly the tiles in this slice — see the module docstring.
        """
        tb = self.bytes_per_tile
        start = max(0, first_tile) * tb
        end = min(len(self.pixel_data), max(0, first_tile + count) * tb)
        return self.pixel_data[start:end] if end > start else b""

    def clamp_offset(self, offset: int, columns: int, rows: int) -> int:
        """A valid top-left tile offset for a ``columns`` × ``rows`` window.

        Bounded so the last reachable window is exactly the final page of tiles
        (the view never scrolls into all-blank space), mirroring how tile viewers
        stop at ``file_size - one_page``.
        """
        page = max(1, columns) * max(1, rows)
        return max(0, min(offset, max(0, self.tile_count - page)))
