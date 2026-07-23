"""The windowed view: composing only a band of tiles and clamping the offset."""

from __future__ import annotations

from celpix.core.arrangement import compose_window
from celpix.core.document import Document
from celpix.core.index_grid import IndexGrid
from celpix.core.palette import Palette
from celpix.pipeline.pathway import PathwayConfig
from celpix.plugins.base import FileRef


def _tiles(n: int, size: int = 2) -> list[IndexGrid]:
    # Tile i is filled entirely with the byte value i, so each tile is trivially
    # identifiable in the composed image.
    return [IndexGrid(size, size, bytes([i] * size * size)) for i in range(n)]


def test_compose_window_lays_out_only_the_band() -> None:
    # 10 tiles, window of 2 rows × 3 cols starting at tile 3 -> tiles 3..8.
    image = compose_window(_tiles(10), columns=3, first_tile=3, rows=2)
    assert (image.width, image.height) == (6, 4)  # 3*2 x 2*2
    # Top-left pixel is tile 3; the tile at grid slot (col=2,row=1) is tile 8.
    assert image.get(0, 0) == 3
    assert image.get(4, 2) == 8  # col 2 -> x=4, row 1 -> y=2


def test_compose_window_pads_partial_window_at_end() -> None:
    # 5 tiles, but a 2x3 window from tile 3 wants 6: slots for tiles 5..8 are blank.
    image = compose_window(_tiles(5), columns=3, first_tile=3, rows=2)
    assert (image.width, image.height) == (6, 4)
    assert image.get(0, 0) == 3  # tile 3 present
    assert image.get(2, 0) == 4  # tile 4 present
    assert image.get(4, 0) == 0  # tile 5 absent -> blank
    assert image.get(0, 2) == 0  # whole second row absent -> blank


def test_compose_window_empty_tiles_is_empty_grid() -> None:
    image = compose_window([], columns=4, first_tile=0, rows=4)
    assert (image.width, image.height) == (0, 0)


def test_compose_window_preserves_grid_type_for_argb() -> None:
    # Direct-color tiles compose into an ArgbGrid (4 bytes/pixel), not an IndexGrid.
    from celpix.core.argb_grid import ArgbGrid

    tiles = [ArgbGrid(2, 2, bytes([i]) * 16) for i in range(4)]
    image = compose_window(tiles, columns=2, first_tile=0, rows=2)
    assert isinstance(image, ArgbGrid)
    assert (image.width, image.height) == (4, 4)
    assert image.get(0, 0) == tiles[0].get(0, 0)


_BPT = 4  # bytes per tile in these documents


def _doc(n_tiles: int) -> Document:
    # Distinguishable bytes so window_bytes slices are checkable.
    data = bytes(i & 0xFF for i in range(n_tiles * _BPT))
    return Document(
        pixel_data=data,
        bytes_per_tile=_BPT,
        tile_width=2,
        tile_height=2,
        palette=Palette.default(4),
        pixel_config=PathwayConfig(source=FileRef("x"), interpret_preset_id="p"),
        palette_config=PathwayConfig(source=FileRef(""), interpret_preset_id="q"),
    )


def test_tile_count_derives_from_bytes() -> None:
    assert _doc(10).tile_count == 10
    assert _doc(0).tile_count == 0


def test_tile_count_counts_a_trailing_partial_tile() -> None:
    doc = _doc(10)
    doc.pixel_data = doc.pixel_data[:-1]  # 39 bytes: 9 whole tiles + 3 spare bytes
    assert doc.tile_count == 10


def test_window_bytes_slices_the_requested_tiles() -> None:
    doc = _doc(10)  # 40 bytes, 4 per tile
    # 3 tiles from tile 2 -> bytes [8:20].
    assert doc.window_bytes(2, 3) == bytes(range(8, 20))
    # A partial window at the end returns only the bytes that exist (tiles 8, 9).
    assert doc.window_bytes(8, 5) == bytes(range(32, 40))
    # Entirely past the end -> empty.
    assert doc.window_bytes(20, 3) == b""


def test_window_bytes_zero_pads_a_trailing_partial_tile() -> None:
    doc = _doc(10)
    doc.pixel_data = doc.pixel_data[:-3]  # 37 bytes: tile 9 has only 1 byte
    assert doc.window_bytes(8, 2) == bytes(range(32, 37)) + bytes(3)


def test_clamp_offset_stops_at_the_last_full_page() -> None:
    doc = _doc(100)
    # 4 cols x 4 rows = 16-tile page; last valid top-left is 100 - 16 = 84.
    assert doc.clamp_offset(0, 4, 4) == 0
    assert doc.clamp_offset(84, 4, 4) == 84
    assert doc.clamp_offset(999, 4, 4) == 84
    assert doc.clamp_offset(-5, 4, 4) == 0


def test_clamp_offset_small_file_pins_to_zero() -> None:
    doc = _doc(10)
    # A window bigger than the file can only sit at the top.
    assert doc.clamp_offset(5, 8, 8) == 0


def test_window_bytes_nudge_shifts_the_grid_and_pads_the_tail() -> None:
    doc = _doc(10)  # 40 bytes, 4 per tile
    # The whole grid shifts forward: 3 tiles from tile 2 at nudge 1 -> bytes [9:21].
    assert doc.window_bytes(2, 3, nudge=1) == bytes(range(9, 21))
    # Near the end the nudged grid's trailing partial tile is zero-padded to a
    # whole tile — codecs decode whole tiles only.
    assert doc.window_bytes(8, 5, nudge=1) == bytes(range(33, 40)) + bytes(1)


def test_clamp_offset_accounts_for_the_nudge() -> None:
    doc = _doc(100)
    # The nudged grid's trailing partial tile still counts (it renders padded):
    # 100 usable tiles, last top-left 100 - 16.
    assert doc.clamp_offset(999, 4, 4, nudge=1) == 84
