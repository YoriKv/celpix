"""Codec engines: round-trips over every registered preset + bit-order vectors."""

from __future__ import annotations

import pytest

from celpix.core.context import PipelineContext
from celpix.core.errors import Stage
from celpix.core.palette import MISSING_COLOR, Palette
from celpix.plugins.registry import default_registry

_REG = default_registry()


def _pixel_ids(engine: str | None = None) -> list[str]:
    return sorted(
        p.id
        for p in _REG.presets(Stage.INTERPRET_PIXEL)
        if engine is None or p.engine_id == engine
    )


def _palette_ids(engine: str | None = None) -> list[str]:
    return sorted(
        p.id
        for p in _REG.presets(Stage.INTERPRET_PALETTE)
        if engine is None or p.engine_id == engine
    )


def _pixel_engine(preset_id: str):
    p = _REG.preset(preset_id)
    return _REG.plugin(Stage.INTERPRET_PIXEL, p.engine_id), p.params


def _color_engine(preset_id: str):
    p = _REG.preset(preset_id)
    return _REG.plugin(Stage.INTERPRET_PALETTE, p.engine_id), p.params


@pytest.mark.parametrize("preset_id", _palette_ids())
def test_palette_preset_reports_entry_size(preset_id: str) -> None:
    """`bytes_per_entry` matches what decode actually consumes, for every palette
    preset — the host relies on it to size "load N entries" byte windows."""
    engine, params = _color_engine(preset_id)
    bpe = engine.bytes_per_entry(params)
    assert bpe > 0
    palette = engine.decode(b"\x00" * (3 * bpe), params, PipelineContext())
    assert len(palette) == 3


# Index-producing pixel presets are bijective on whole buffers; direct-colour is
# lossy at <8bpp/component, so it's round-tripped separately (idempotency).
_INDEX_PIXEL_IDS = [
    p for p in _pixel_ids() if _REG.preset(p).engine_id != "codec.direct-color"
]


@pytest.mark.parametrize("preset_id", _INDEX_PIXEL_IDS)
def test_pixel_preset_round_trips(preset_id: str) -> None:
    """`encode(decode(x)) == x` for every index-producing pixel preset.

    Pins each preset's parameters — a wrong plane count / nibble order / tile size
    breaks the identity or the reported geometry — across planar, packed, chunky,
    bespoke-linear and the wide/odd tile codecs.
    """
    engine, params = _pixel_engine(preset_id)
    tile_bytes = engine.bytes_per_tile(params)
    data = bytes((i * 61 + 7) & 0xFF for i in range(tile_bytes * 3))
    tiles = engine.decode(data, params, PipelineContext())
    assert len(tiles) == 3
    tw, th = engine.tile_size(params)
    assert all(t.width == tw and t.height == th for t in tiles)
    assert engine.encode(tiles, params, PipelineContext()) == data


@pytest.mark.parametrize("preset_id", _pixel_ids(engine="codec.direct-color"))
def test_direct_color_round_trips(preset_id: str) -> None:
    """Direct-colour presets decode to 8×8 ARGB tiles and round-trip idempotently."""
    engine, params = _pixel_engine(preset_id)
    data = bytes((i * 61 + 7) & 0xFF for i in range(engine.bytes_per_tile(params) * 2))
    grids = engine.decode(data, params, PipelineContext())
    assert len(grids) == 2
    assert all(g.width == 8 and g.height == 8 and g.bytes_per_pixel == 4 for g in grids)
    again = engine.decode(
        engine.encode(grids, params, PipelineContext()), params, PipelineContext()
    )
    assert again == grids


def test_direct_color_known_vector() -> None:
    engine, params = _pixel_engine("preset.pixel.dc-rgb555")
    # Pixel 0 = pure blue: RGB555 B field (0x001F), LE u16 -> bytes 1F 00.
    data = b"\x1f\x00" + bytes(engine.bytes_per_tile(params) - 2)
    grid = engine.decode(data, params, PipelineContext())[0]
    assert grid.get(0, 0) == 0xFF0000FF


@pytest.mark.parametrize("preset_id", _palette_ids(engine="codec.color-mask"))
def test_mask_palette_round_trips(preset_id: str) -> None:
    """`decode(encode(pal)) == pal` for every mask-based palette preset.

    Idempotent after the first decode — robust for the lossy 5-/4-/3-bit formats,
    where the raw bits don't round-trip but the decoded canonical value does.
    """
    engine, params = _color_engine(preset_id)
    size = int(params["bytes_per_entry"])
    data = bytes((i * 53 + 11) & 0xFF for i in range(size * 16))
    pal = engine.decode(data, params, PipelineContext())
    assert len(pal) == 16
    again = engine.decode(
        engine.encode(pal, params, PipelineContext()), params, PipelineContext()
    )
    assert again == pal


@pytest.mark.parametrize("preset_id", _palette_ids(engine="codec.color-indexed"))
def test_indexed_palette_round_trips_in_range(preset_id: str) -> None:
    """Indexed decode→encode recovers the same colours for every table index.

    Encode picks the nearest table entry; an exact colour resolves to an index with
    that colour (distance 0), so the decoded palette round-trips even when duplicate
    slots make the *index* differ.
    """
    engine, params = _color_engine(preset_id)
    n = len(params["colors"])
    data = bytes(range(n))  # one of every index
    pal = engine.decode(data, params, PipelineContext())
    assert len(pal) == n
    again = engine.decode(
        engine.encode(pal, params, PipelineContext()), params, PipelineContext()
    )
    assert again == pal


def test_indexed_palette_nearest_encode() -> None:
    engine, params = _color_engine("preset.palette.ega-indexed")
    # An off-table colour encodes to the nearest table entry (EGA index 0 = black).
    from celpix.core.palette import Palette

    assert engine.encode(Palette([0xFF000001]), params, PipelineContext()) == b"\x00"


@pytest.mark.parametrize("preset_id", _pixel_ids(engine="codec.planar"))
def test_planar_plane_maps_to_bit(preset_id: str) -> None:
    """Setting only plane k's MSB at row 0 must light bit k of pixel (0,0).

    Pins the plane→bit assignment and the per-plane byte offsets, which a round trip
    alone cannot (a consistently wrong orientation still round-trips).
    """
    engine, params = _pixel_engine(preset_id)
    tile_bytes = engine.bytes_per_tile(params)
    for k, plane in enumerate(params["planes"]):
        data = bytearray(tile_bytes)
        data[plane["base"]] = 0x80  # row 0, leftmost-pixel bit of plane k
        tiles = engine.decode(bytes(data), params, PipelineContext())
        assert tiles[0].get(0, 0) == (1 << k), f"plane {k} of {preset_id}"
        assert tiles[0].get(1, 0) == 0


def test_packed_nibble_and_bit_order() -> None:
    """The packed order flags place each pixel's field correctly (the tricky part)."""

    def px(preset_id: str, row0: list[int]) -> list[int]:
        engine, params = _pixel_engine(preset_id)
        data = bytes(row0) + bytes(engine.bytes_per_tile(params) - len(row0))
        tile = engine.decode(data, params, PipelineContext())[0]
        return [tile.get(x, 0) for x in range(4)]

    assert px("preset.pixel.gba-4bpp", [0x21]) == [1, 2, 0, 0]  # low nibble = left
    assert px("preset.pixel.genesis-4bpp", [0x21]) == [2, 1, 0, 0]  # high nibble = left
    assert px("preset.pixel.vb-2bpp", [0xE4]) == [0, 1, 2, 3]  # low 2 bits = pixel 0
    assert px("preset.pixel.ngp-2bpp", [0xE4]) == [3, 2, 1, 0]  # high 2 bits = pixel 0
    # YY-CHR byte-swap: the odd (second) row byte drives the left pixels.
    assert px("preset.pixel.ngp-2bpp-swapped", [0x00, 0xE4]) == [3, 2, 1, 0]


def test_chunky_is_row_major_index_per_byte() -> None:
    engine, params = _pixel_engine("preset.pixel.chunky-8bpp")
    tile = engine.decode(bytes(range(64)), params, PipelineContext())[0]
    assert [tile.get(x, 0) for x in range(8)] == list(range(8))  # row 0 = bytes 0..7
    assert tile.get(0, 1) == 8  # row 1 starts at byte 8


def test_planar_rejects_misaligned() -> None:
    engine, params = _pixel_engine("preset.pixel.snes-4bpp")
    with pytest.raises(ValueError):
        engine.decode(b"\x00" * 30, params, PipelineContext())  # not a multiple of 32


def test_bgr555_known_vector() -> None:
    engine, params = _color_engine("preset.palette.bgr555")
    # Pure blue: B field (0x7C00) all set, LE u16 -> bytes 00 7C.
    pal = engine.decode(b"\x00\x7c", params, PipelineContext())
    assert pal.color(0) == 0xFF0000FF


def test_rgb888_known_vector() -> None:
    engine, params = _color_engine("preset.palette.rgb888")
    pal = engine.decode(b"\x12\x34\x56", params, PipelineContext())
    assert pal.color(0) == 0xFF123456


def test_missing_color_sentinel() -> None:
    pal = Palette([0xFF000000])
    assert pal.color(5) == MISSING_COLOR
