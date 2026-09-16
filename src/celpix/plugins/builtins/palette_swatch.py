"""Palette swatch codec — a byte stream read as palette colors, one tile each.

The pixel-side view of palette data: every palette entry in the buffer becomes
one solid 8×8 :class:`~celpix.core.argb_grid.ArgbGrid` tile, so a region of
color words reads on the canvas as a row of swatches — sixteen columns, one
palette row per line — with the tile grid marking the entries apart. The colors
themselves are decoded by a **color codec**, the same engine the palette dock
reads a palette with; which one is the ``palette_preset_id`` param, and the
toolbar's palette-format picker is what sets it per entry
(``PathwayConfig.interpret_params``, ``docs/design/plugin-system.md`` §5).

That param is a preset id, so this engine is handed the **registry** at
construction — the one built-in that resolves another stage's plugin, because
its whole job is to lend a color format a tile shape. A preset naming a color
format this build hasn't got fails the load like any other unusable preset.

**A tile is one read unit** of the color format: one entry for every byte-sized
format, and every entry packed into the unit for the handheld registers (four
shades in a Game Boy palette byte), which is what keeps ``bytes_per_tile`` a
whole number. A packed unit is therefore one tile *several swatches wide*, and
that width is the format's — the bitmap-width probe (``tile_width`` /
``tile_height`` params) is deliberately ignored.

Encoding takes each swatch's **most common** color, so a whole-swatch paint (the
only kind the pixel tools make here — :meth:`~celpix.ui.main_window.pixel_edit.
PixelEditMixin._paint_pixels`) rewrites the entry exactly, and an imported image
whose swatches carry a stray edge pixel or two still lands on the color that
fills them. The color codec's own encode then quantizes to what the format can
hold, so a painted color reads back as the format's nearest.
"""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING, Any

from celpix.core.argb_grid import ArgbGrid
from celpix.core.context import PipelineContext
from celpix.core.errors import Stage
from celpix.core.palette import Palette
from celpix.plugins.base import (
    PALETTE_PRESET_PARAM,
    PALETTE_SWATCH_ENGINE,
    STAGE_DEFAULT_PRESET,
    ColorCodecPlugin,
    PluginInfo,
)
from celpix.plugins.builtins._tile import check_tile_size, require_whole_tiles

if TYPE_CHECKING:
    from celpix.plugins.registry import Registry

# Each entry's square, in pixels. Eight because it is the tile every other
# format draws at, so the zoom, the grid and the tile-size readout all mean what
# they mean everywhere else.
SWATCH = 8


class PaletteSwatchCodec:
    """Palette entries as solid direct-color tiles; the color format is a param."""

    info = PluginInfo(
        id=PALETTE_SWATCH_ENGINE,
        name="Palette swatches (each entry as a solid tile)",
        stage=Stage.INTERPRET_PIXEL,
    )

    def __init__(self, registry: Registry) -> None:
        self._registry = registry

    def _color_codec(
        self, params: dict[str, Any]
    ) -> tuple[ColorCodecPlugin, dict[str, Any], int, int]:
        """``(engine, its params, bytes per unit, entries per unit)`` for the
        color format ``params`` names — the stage default when it names none."""
        preset_id = (
            params.get(PALETTE_PRESET_PARAM)
            or STAGE_DEFAULT_PRESET[Stage.INTERPRET_PALETTE]
        )
        try:
            engine, preset = self._registry.engine_for(preset_id, ColorCodecPlugin)
        except KeyError:
            raise ValueError(f"no palette format with id {preset_id!r}") from None
        if preset.stage is not Stage.INTERPRET_PALETTE:
            raise ValueError(f"{preset_id!r} is not a palette format")
        unit_bytes = engine.bytes_per_entry(preset.params)
        per_unit = getattr(engine, "entries_per_unit", None)
        entries = max(1, per_unit(preset.params)) if per_unit is not None else 1
        if unit_bytes <= 0:
            raise ValueError(f"{preset_id!r} reports no bytes per entry")
        return engine, preset.params, unit_bytes, entries

    def bytes_per_tile(self, params: dict[str, Any]) -> int:
        return self._color_codec(params)[2]

    def tile_size(self, params: dict[str, Any]) -> tuple[int, int]:
        return SWATCH * self._color_codec(params)[3], SWATCH

    def decode(
        self, data: bytes, params: dict[str, Any], ctx: PipelineContext
    ) -> list[ArgbGrid]:
        engine, color_params, unit_bytes, per_unit = self._color_codec(params)
        require_whole_tiles(len(data), unit_bytes)
        if not data:
            return []
        colors = engine.decode(data, color_params, ctx)
        width = SWATCH * per_unit
        tiles = []
        for unit in range(len(data) // unit_bytes):
            # One pixel row of the tile: each swatch's color repeated across its
            # eight pixels, the unit's swatches side by side; then that row
            # stacked eight high. Built from bytes rather than set() per pixel
            # because a wide palette region is thousands of entries.
            row = b"".join(
                (colors.color(unit * per_unit + i) & 0xFFFFFFFF).to_bytes(4, "little")
                * SWATCH
                for i in range(per_unit)
            )
            tiles.append(ArgbGrid(width, SWATCH, row * SWATCH))
        return tiles

    def encode(
        self, tiles: list[ArgbGrid], params: dict[str, Any], ctx: PipelineContext
    ) -> bytes:
        engine, color_params, _unit_bytes, per_unit = self._color_codec(params)
        width = SWATCH * per_unit
        colors: list[int] = []
        for index, tile in enumerate(tiles):
            check_tile_size(tile, width, SWATCH, index)
            for swatch in range(per_unit):
                left = swatch * SWATCH
                seen = Counter(
                    tile.get(x, y)
                    for y in range(SWATCH)
                    for x in range(left, left + SWATCH)
                )
                colors.append(seen.most_common(1)[0][0])
        if not colors:
            return b""
        return engine.encode(Palette(colors), color_params, ctx)
