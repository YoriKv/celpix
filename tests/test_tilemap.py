"""The tilemap pathway: the cell model, the packed codec, and the containers.

The invariant everything leans on is that a cell survives a round trip whole —
index *and* attributes, including the priority bit celPix never renders. A field
dropped at decode time is a field silently zeroed on the next save, which is a
corrupted file rather than a missing feature.
"""

from __future__ import annotations

import pytest

from celpix.core.capabilities import CAPABILITIES, Capability, ContentKind, supports
from celpix.core.context import KEY_SOURCE_OFFSET, PipelineContext
from celpix.core.errors import Stage
from celpix.core.tilemap import Cell, CellGrid
from celpix.pipeline.pathway import PathwayConfig
from celpix.pipeline.pipeline import encode_cells, load_tilemap_data
from celpix.plugins.base import FileRef, ReadSource, WriteTarget
from celpix.plugins.builtins.scgcad import (
    MAP_SIZE,
    PNL_SIZE,
    SCR_PAYLOAD,
    SCR_SIZE,
    SIGNATURE,
    MapContainer,
    PnlContainer,
    ScrContainer,
)
from celpix.plugins.builtins.tilemap_codec import TilemapCodec
from celpix.plugins.detect import detect_container
from celpix.plugins.registry import default_registry

SNES_BG = "preset.tilemap.snes-bg"
SWAPPED = "preset.tilemap.snes-bg-swapped"
PANEL = "preset.tilemap.scgcad-panel"


def _params(registry, preset_id: str) -> dict:
    return registry.preset(preset_id).params


# -- the cell model --------------------------------------------------------
def test_a_grid_is_row_major_and_bounds_checked() -> None:
    grid = CellGrid(4, 3)
    grid.set(1, 2, Cell(index=9))
    assert grid.get(1, 2).index == 9
    assert grid.at(2 * 4 + 1).index == 9
    with pytest.raises(IndexError):
        grid.get(4, 0)
    with pytest.raises(IndexError):
        grid.set(0, 3, Cell())


def test_from_cells_pads_short_input_and_truncates_long() -> None:
    """A byte window need not divide evenly into the grid being shown, and a
    file with a partial final row is a file to display, not one to refuse."""
    short = CellGrid.from_cells(3, 2, [Cell(index=i) for i in range(4)])
    assert [c.index for c in short] == [0, 1, 2, 3, 0, 0]
    long = CellGrid.from_cells(2, 1, [Cell(index=i) for i in range(5)])
    assert [c.index for c in long] == [0, 1]


def test_a_block_flip_reverses_order_and_flips_each_cell() -> None:
    """Both halves are needed: reversing alone mirrors the layout with every tile
    still facing its original way, and toggling alone mirrors in place."""
    grid = CellGrid.from_cells(2, 1, [Cell(index=1), Cell(index=2, flip_h=True)])
    flipped = grid.flipped_h()
    assert [(c.index, c.flip_h) for c in flipped] == [(2, False), (1, True)]
    # Twice is the identity, which is what makes the control safe to press.
    assert flipped.flipped_h() == grid


def test_a_vertical_flip_reverses_rows() -> None:
    grid = CellGrid.from_cells(1, 2, [Cell(index=1), Cell(index=2)])
    flipped = grid.flipped_v()
    assert [(c.index, c.flip_v) for c in flipped] == [(2, True), (1, True)]
    assert flipped.flipped_v() == grid


def test_block_and_paste_clip_to_bounds() -> None:
    """A selection dragged past the edge yields what is actually there."""
    grid = CellGrid.from_cells(3, 2, [Cell(index=i) for i in range(6)])
    cut = grid.block(2, 1, 4, 4)
    assert (cut.width, cut.height) == (1, 1)
    assert cut.get(0, 0).index == 5
    grid.paste(2, 1, CellGrid.from_cells(2, 2, [Cell(index=99)] * 4))
    assert grid.get(2, 1).index == 99  # the rest fell off the edge, harmlessly


# -- the packed codec ------------------------------------------------------
def test_every_field_survives_a_round_trip() -> None:
    """Priority in particular: celPix renders none of it, and dropping it would
    silently zero the bit on the next save of every file that uses it."""
    registry = default_registry()
    codec, params, ctx = TilemapCodec(), _params(registry, SNES_BG), PipelineContext()
    cells = [
        Cell(index=0x123, palette_row=5, priority=1, flip_h=True, flip_v=False),
        Cell(index=0x000, palette_row=0, priority=0, flip_h=False, flip_v=True),
        Cell(index=0x3FF, palette_row=7, priority=1, flip_h=True, flip_v=True),
    ]
    data = codec.encode(cells, params, ctx)
    assert len(data) == 6
    assert codec.decode(data, params, ctx) == cells


def test_the_snes_cell_is_the_hardware_word() -> None:
    """`vhopppcc tttttttt`, little-endian — byte 0 is the low tile bits."""
    registry = default_registry()
    codec, params, ctx = TilemapCodec(), _params(registry, SNES_BG), PipelineContext()
    (cell,) = codec.decode(b"\x34\x9c", params, ctx)
    # 0x9C34 = 1001 1100 0011 0100: vflip=1 hflip=0 pri=0 pal=7 tile=0x034
    assert (cell.index, cell.palette_row, cell.priority) == (0x034, 7, 0)
    assert (cell.flip_h, cell.flip_v) == (False, True)


def test_the_two_byte_orders_disagree_on_the_same_bytes() -> None:
    """The reason endianness is a per-format parameter and not a family
    constant: a screen and a panel come from one authoring tool and store their
    words opposite ways (scgcad-formats.md §5.2)."""
    registry = default_registry()
    codec, ctx = TilemapCodec(), PipelineContext()
    raw = b"\x34\x9c"
    (little,) = codec.decode(raw, _params(registry, SNES_BG), ctx)
    (big,) = codec.decode(raw, _params(registry, SWAPPED), ctx)
    # 0x9C34 -> tile 0x034, palette 7.   0x349C -> tile 0x09C, palette 5.
    assert (little.index, little.palette_row) == (0x034, 7)
    assert (big.index, big.palette_row) == (0x09C, 5)
    assert little != big


def test_an_index_only_format_decodes_and_re_encodes_without_attributes() -> None:
    """A field a preset omits does not exist in that format: it reads as zero
    and is dropped on write, which is how a bare index map is described."""
    registry = default_registry()
    codec, ctx = TilemapCodec(), PipelineContext()
    params = _params(registry, "preset.tilemap.gb-bg")
    cells = codec.decode(b"\x07\x40", params, ctx)
    assert [c.index for c in cells] == [7, 0x40]
    assert all(c.palette_row == 0 and not c.flip_h for c in cells)
    # A cell carrying attributes the format has no room for loses them rather
    # than making the document unsaveable.
    assert (
        codec.encode([Cell(index=3, palette_row=5, flip_v=True)], params, ctx)
        == b"\x03"
    )


def test_a_too_wide_index_is_masked_not_raised() -> None:
    """One cell out of range must not cost the whole save."""
    registry = default_registry()
    codec, params, ctx = TilemapCodec(), _params(registry, SNES_BG), PipelineContext()
    data = codec.encode([Cell(index=0x7FF)], params, ctx)
    assert codec.decode(data, params, ctx)[0].index == 0x3FF


def test_a_trailing_partial_cell_is_dropped() -> None:
    """Unlike a partial tile, which still draws as something, half a cell has no
    meaningful index and would render as a spurious tile 0."""
    registry = default_registry()
    codec, params, ctx = TilemapCodec(), _params(registry, SNES_BG), PipelineContext()
    assert len(codec.decode(b"\x00\x00\x00", params, ctx)) == 1


def test_a_panel_cell_covers_one_tile_by_default() -> None:
    """A panel word is one 8x8 tile: a 16x16 unit is four adjacent words, so a
    preset that expanded each word into a metatile would draw every unit four
    times over. That holds for all 1,172 surveyed panels, including the 80 whose
    header byte claims 16x16 (``scgcad-formats.md`` §3.1)."""
    registry = default_registry()
    codec = TilemapCodec()
    assert codec.cell_tiles(_params(registry, PANEL)) == (1, 1)
    assert codec.cell_tiles(_params(registry, SNES_BG)) == (1, 1)
    assert codec.bytes_per_cell(_params(registry, SNES_BG)) == 2


def test_every_shipped_tilemap_preset_resolves_to_a_working_engine() -> None:
    registry = default_registry()
    presets = registry.presets(Stage.INTERPRET_TILEMAP)
    assert presets, "no tilemap presets registered"
    for preset in presets:
        engine, _ = registry.engine_for(preset.id)
        ctx = PipelineContext()
        size = engine.bytes_per_cell(preset.params)
        cells = engine.decode(bytes(size * 4), preset.params, ctx)
        assert len(cells) == 4
        assert engine.encode(cells, preset.params, ctx) == bytes(size * 4)


# -- the containers --------------------------------------------------------
def _scr_bytes(payload: bytes = b"") -> bytes:
    out = bytearray(SCR_SIZE)
    out[: len(payload)] = payload
    out[0x2000 : 0x2000 + len(SIGNATURE)] = SIGNATURE
    out[0x2100:] = b"\xff" * 0x200
    return bytes(out)


def test_a_screen_reads_its_payload_from_the_front_and_reports_the_offset() -> None:
    data = _scr_bytes(b"\x11\x22")
    ctx = PipelineContext()
    payload = ScrContainer().read(ReadSource(data=data, path="x.SCR"), ctx)
    assert len(payload) == SCR_PAYLOAD
    assert payload[:2] == b"\x11\x22"
    assert ctx.get(KEY_SOURCE_OFFSET) == 0


def test_writing_a_screen_preserves_its_header_and_trailer() -> None:
    """The metadata block is carried verbatim rather than regenerated — it holds
    fields celPix does not interpret, and inventing them would rewrite the
    file's provenance."""
    existing = _scr_bytes(b"\x11\x22")
    out = ScrContainer().write(
        b"\x99" * SCR_PAYLOAD,
        WriteTarget(existing=existing, path="x.SCR"),
        PipelineContext(),
    )
    assert len(out) == SCR_SIZE
    assert out[:SCR_PAYLOAD] == b"\x99" * SCR_PAYLOAD
    assert out[0x2000:] == existing[0x2000:]


def test_a_panel_carries_its_flag_table_through_untouched() -> None:
    """Bit 15 of a flag word does not track occupancy, so anything written there
    would be a guess written into the user's file (scgcad-formats.md §3)."""
    existing = bytearray(PNL_SIZE)
    existing[: len(SIGNATURE)] = SIGNATURE
    existing[0x8100:] = b"\x5a" * 0x8000
    ctx = PipelineContext()
    payload = PnlContainer().read(ReadSource(data=bytes(existing)), ctx)
    assert len(payload) == 0x8000
    assert ctx.get(KEY_SOURCE_OFFSET) == 0x100
    out = PnlContainer().write(
        b"\x77" * 0x8000, WriteTarget(existing=bytes(existing)), PipelineContext()
    )
    assert out[0x8100:] == b"\x5a" * 0x8000
    assert out[:0x100] == bytes(existing[:0x100])


def test_a_stamp_layout_reads_past_its_leading_header() -> None:
    data = bytearray(MAP_SIZE)
    data[: len(SIGNATURE)] = SIGNATURE
    data[0x100:0x102] = b"\x40\x01"
    ctx = PipelineContext()
    payload = MapContainer().read(ReadSource(data=bytes(data)), ctx)
    assert payload[:2] == b"\x40\x01"
    assert ctx.get(KEY_SOURCE_OFFSET) == 0x100


def test_detection_tells_the_three_apart(tmp_path) -> None:
    """They share a signature; a panel and a stamp layout share its *offset*
    too, so only the file length separates those two. A screen's signature sits
    at 0x2000, past where detection used to look at all."""
    registry = default_registry()
    cases = {
        "s.SCR": (_scr_bytes(), "container.scgcad-scr"),
        "p.PNL": (_sig_file(PNL_SIZE), "container.scgcad-pnl"),
        "m.MAP": (_sig_file(MAP_SIZE), "container.scgcad-map"),
    }
    for name, (data, want) in cases.items():
        path = tmp_path / name
        path.write_bytes(data)
        assert detect_container(registry, str(path)) == want, name


def test_an_assembler_listing_named_map_is_not_claimed(tmp_path) -> None:
    """`.map` is a common extension for linker output. Magic decides, so a file
    without the signature keeps its plain-bytes reading whatever it is called."""
    path = tmp_path / "ys_main.map"
    path.write_bytes(b"symbol table\n" * 100)
    assert detect_container(default_registry(), str(path)) == "container.raw-file"


def _sig_file(size: int) -> bytes:
    out = bytearray(size)
    out[: len(SIGNATURE)] = SIGNATURE
    return bytes(out)


# -- end to end ------------------------------------------------------------
def test_a_screen_loads_through_the_pipeline_and_re_encodes_identically(
    tmp_path,
) -> None:
    """The whole pathway on a real-shaped file: container cuts the payload out,
    the codec reads cells, and encoding them back reproduces the bytes."""
    cells = [
        Cell(index=0x101, palette_row=6, priority=1, flip_h=True),
        Cell(index=0x002, palette_row=0),
    ]
    registry = default_registry()
    ctx = PipelineContext()
    body = TilemapCodec().encode(cells, _params(registry, SNES_BG), ctx)
    path = tmp_path / "B2-MORI.SCR"
    path.write_bytes(_scr_bytes(body))

    loaded = load_tilemap_data(
        PathwayConfig(
            source=FileRef(str(path)),
            interpret_preset_id=SNES_BG,
            container_id="container.scgcad-scr",
        ),
        registry,
    )
    assert loaded.cell_bytes == 2
    assert loaded.cell_tiles == (1, 1)
    assert len(loaded.cells) == SCR_PAYLOAD // 2  # four 32x32 screens
    assert loaded.cells[:2] == cells
    assert encode_cells(loaded.cells, SNES_BG, registry) == loaded.data


# -- capabilities ----------------------------------------------------------
def test_a_tilemap_flips_but_does_not_rotate() -> None:
    """The case that justifies splitting the transforms: a hardware cell carries
    mirror bits and no transpose bit, so one CELL_TRANSFORM capability would
    have had to lie about one of the two."""
    assert supports(ContentKind.TILEMAP, Capability.CELL_FLIP)
    assert not supports(ContentKind.TILEMAP, Capability.CELL_ROTATE)
    assert supports(ContentKind.PIXELS, Capability.CELL_ROTATE)


def test_the_kinds_differ_where_the_design_says_they_do() -> None:
    pixels = CAPABILITIES[ContentKind.PIXELS]
    tilemap = CAPABILITIES[ContentKind.TILEMAP]
    # A tilemap has no pixels to paint and no display-only permutation: moving a
    # cell *is* the byte edit, which is exactly what a rearrangement is not.
    assert Capability.PIXEL_EDIT not in tilemap
    assert Capability.TILE_REARRANGE not in tilemap
    # A cell already names its own palette row, so pinning one over a span would
    # be a second, conflicting answer to a question the file has answered.
    assert Capability.PALETTE_REGIONS not in tilemap
    # ...but it carries its own palette, like every other entry.
    assert Capability.PALETTE_EDIT in tilemap and Capability.PALETTE_EDIT in pixels
    assert Capability.STAMP in tilemap and Capability.STAMP not in pixels
    # A palette entry is applied rather than activated: no view to navigate.
    assert Capability.NAVIGATION not in CAPABILITIES[ContentKind.PALETTE]


def test_cells_expand_into_tiles_a_block_layout_can_place() -> None:
    """The whole of why a tilemap renders through the pixel view: cells become
    an ordinary tile list plus per-tile palette shifts, and the cell becomes the
    arrangement's *block* — which is what places a metatile's four tiles as a
    square without a second composer."""
    from celpix.core.document import Document
    from celpix.core.index_grid import IndexGrid
    from celpix.pipeline.pipeline import tilemap_tiles

    registry = default_registry()
    # Four distinct 8x8 tiles, so a flip or a wrong index is visible.
    source = bytearray()
    for value in (1, 2, 3, 4):
        source += bytes([value]) * 32
    doc = Document(
        pixel_data=bytes(source),
        bytes_per_tile=32,
        tile_width=8,
        tile_height=8,
        palette=None,
        pixel_config=PathwayConfig(
            source=FileRef("x"), interpret_preset_id="preset.pixel.snes-4bpp"
        ),
        palette_config=None,
        cells=[Cell(index=1, palette_row=2), Cell(index=0, flip_h=True)],
    )
    tiles, biases, layout = tilemap_tiles(doc, registry, columns=2)
    assert len(tiles) == 2 and all(isinstance(t, IndexGrid) for t in tiles)
    # 4bpp: the row is folded into the indices as row * 16, the same shift a
    # pinned palette region uses.
    assert biases == [2 * 16, 0]
    assert (layout.columns, layout.block_columns, layout.block_rows) == (2, 1, 1)


def test_a_metatile_cell_becomes_a_block_of_four() -> None:
    from celpix.core.document import Document
    from celpix.pipeline.pipeline import tilemap_tiles

    registry = default_registry()
    doc = Document(
        pixel_data=bytes(32 * 64),
        bytes_per_tile=32,
        tile_width=8,
        tile_height=8,
        palette=None,
        pixel_config=PathwayConfig(
            source=FileRef("x"), interpret_preset_id="preset.pixel.snes-4bpp"
        ),
        palette_config=None,
        cells=[Cell(index=0), Cell(index=1)],
        cell_tiles=(2, 2),
        cell_row_stride=16,
    )
    tiles, _biases, layout = tilemap_tiles(doc, registry, columns=2)
    assert len(tiles) == 8  # two cells of four tiles each
    # Columns are in *tiles*, so a 2-cell-wide map of 2-wide cells is 4 across,
    # and the block is the cell.
    assert (layout.columns, layout.block_columns, layout.block_rows) == (4, 2, 2)
    # A flip reverses the order as well as mirroring each tile: the right-hand
    # tile of the metatile has to appear on the left, mirrored. Toggling the bits
    # alone would mirror each tile in place and leave the pair the wrong way
    # round, which is the half of a block flip that is easy to miss.
    assert doc.cell_tile_indices(Cell(index=0)) == [0, 1, 16, 17]
    assert doc.cell_tile_indices(Cell(index=0, flip_h=True)) == [1, 0, 17, 16]
    assert doc.cell_tile_indices(Cell(index=0, flip_v=True)) == [16, 17, 0, 1]


def test_a_cell_naming_a_tile_the_source_lacks_renders_blank() -> None:
    """Tilemaps are routinely authored against a bank loaded from elsewhere, and
    half a picture beats an error."""
    from celpix.core.document import Document
    from celpix.pipeline.pipeline import tilemap_tiles

    doc = Document(
        pixel_data=bytes(32),  # one tile
        bytes_per_tile=32,
        tile_width=8,
        tile_height=8,
        palette=None,
        pixel_config=PathwayConfig(
            source=FileRef("x"), interpret_preset_id="preset.pixel.snes-4bpp"
        ),
        palette_config=None,
        cells=[Cell(index=999)],
    )
    tiles, _biases, _layout = tilemap_tiles(doc, default_registry(), columns=1)
    assert len(tiles) == 1
    assert set(tiles[0].data) == {0}


def test_a_format_answers_for_itself_which_transforms_it_can_do() -> None:
    """Which transforms a cell supports is a property of the *format*, and only
    the codec knows which bits say one. A preset that declares no flip field
    describes a format with nowhere to put one, so the flip is refused rather
    than set in the model and dropped again on the next save."""
    from celpix.core.tilemap import CellOp

    registry = default_registry()
    codec = TilemapCodec()
    snes = _params(registry, SNES_BG)
    flipped = codec.transform_cell(Cell(index=7), CellOp.FLIP_H, snes)
    assert flipped == Cell(index=7, flip_h=True)
    assert codec.transform_cell(flipped, CellOp.FLIP_H, snes).flip_h is False

    # An index-only map has no bit for it, and a stamp layout's word is a
    # coordinate with no room for one either.
    for preset in ("preset.tilemap.gb-bg", "preset.tilemap.scgcad-map"):
        params = _params(registry, preset)
        assert codec.transform_cell(Cell(), CellOp.FLIP_H, params) is None
        assert codec.transform_cell(Cell(), CellOp.FLIP_V, params) is None

    # No format in hand has a rotation bit, and a Cell has no field to hold one.
    assert codec.transform_cell(Cell(), CellOp.ROTATE_CW, snes) is None
    assert codec.transform_cell(Cell(), CellOp.ROTATE_CCW, snes) is None


# -- sprite objects --------------------------------------------------------
OBJECT = "preset.tilemap.scgcad-object"


def _record(
    *, x=0, y=0, tile=0, palette=0, large=False, drawn=True, group=0, order="big"
) -> bytes:
    """One 6-byte sprite part record, built the way the file stores it."""
    attr = (tile & 0x1FF) | ((palette & 0x7) << 9)
    head = bytes(
        ((0x80 if drawn else 0) | (1 if large else 0), group, y & 0xFF, x & 0xFF)
    )
    return head + attr.to_bytes(2, order)


def _obj_bytes(records: bytes, *, marker: bytes = b"Ver1.23 901226  ") -> bytes:
    from celpix.plugins.builtins.scgcad import OBJ_PAYLOADS, OBJ_SIZE

    out = bytearray(OBJ_SIZE)
    out[: len(records)] = records
    at = OBJ_PAYLOADS[0]
    out[at : at + len(SIGNATURE)] = SIGNATURE
    out[at + 0x10 : at + 0x20] = marker
    return bytes(out)


def _sprite_doc(cells, frames, size_pair=(8, 16), bank=None):
    """A sprite document bound to ``bank`` (64 blank 4bpp tiles by default)."""
    from celpix.core.document import Document

    source = bank if bank is not None else bytes(32 * 64)
    return Document(
        pixel_data=source,
        bytes_per_tile=32,
        tile_width=8,
        tile_height=8,
        palette=None,
        pixel_config=PathwayConfig(
            source=FileRef("x"), interpret_preset_id="preset.pixel.snes-4bpp"
        ),
        palette_config=None,
        cells=cells,
        sprite_frames=frames,
        sprite_size_pair=size_pair,
    )


def test_a_part_record_round_trips_including_what_celpix_has_no_field_for() -> None:
    """Position, size bit and the tool's group byte have no home in a Cell, so
    they ride in ``flags``. Dropping them would zero a sprite's whole geometry on
    the next save."""
    from celpix.plugins.builtins.object_codec import ObjectCodec

    registry = default_registry()
    codec, params = ObjectCodec(), _params(registry, OBJECT)
    raw = _record(x=-8, y=100, tile=0x1FF, palette=5, large=True, group=3)
    ctx = PipelineContext()
    (cell,) = codec.decode(raw, params, ctx)
    assert (cell.index, cell.palette_row) == (0x1FF, 5)
    assert codec.encode([cell], params, ctx) == raw


def test_the_later_build_stores_its_attribute_word_the_other_way_round() -> None:
    """26 of the 1,341 objects in the corpus are that build and every one says so
    in its own marker, so the container reads it and the codec obeys — the preset
    only says what to assume when nothing did."""
    from celpix.core.context import KEY_TILEMAP_ENDIAN
    from celpix.plugins.builtins.object_codec import ObjectCodec
    from celpix.plugins.builtins.scgcad import ObjContainer

    registry = default_registry()
    raw = _record(tile=0x1FF, palette=0, order="little")
    ctx = PipelineContext()
    payload = ObjContainer().read(
        ReadSource(data=_obj_bytes(raw, marker=b"Ver1.11 930511 F")), ctx
    )
    assert ctx.get(KEY_TILEMAP_ENDIAN) == "little"
    (cell,) = ObjectCodec().decode(payload[:6], _params(registry, OBJECT), ctx)
    assert cell.index == 0x1FF  # read big-endian it would be 0x1FE, palette 7


def test_undrawn_slots_are_dropped_and_trailing_empty_frames_with_them() -> None:
    """A file has room for a fixed 32 frames of 64 parts and the artist filled
    almost none of it — 94% of the corpus's slots are empty — so a view that drew
    every slot would bury the sprite at the top of a blank sheet."""
    from celpix.core.sprite import drawn_frames
    from celpix.plugins.builtins.object_codec import ObjectCodec

    registry = default_registry()
    params = dict(_params(registry, OBJECT))
    params["parts_per_frame"] = 2
    cells = ObjectCodec().decode(
        _record(tile=1) + _record(drawn=False) + _record(drawn=False) * 2,
        params,
        PipelineContext(),
    )
    frames = ObjectCodec().frames(cells, params)
    assert [len(frame) for frame in frames] == [1, 0]
    assert len(drawn_frames(frames)) == 1


def test_a_flipped_part_reverses_its_tile_order_as_well_as_each_tile() -> None:
    """A mirrored 16x16 part shows its right-hand tile on the left, mirrored.
    Both halves are needed and neither is sufficient."""
    from celpix.core.sprite import Part

    pair = (8, 16)
    assert Part(index=0x20, large=True).tile_indices(pair) == [0x20, 0x21, 0x30, 0x31]
    assert Part(index=0x20, large=True, flip_h=True).tile_indices(pair) == [
        0x21,
        0x20,
        0x31,
        0x30,
    ]
    assert Part(index=0x20, large=True, flip_v=True).tile_indices(pair) == [
        0x30,
        0x31,
        0x20,
        0x21,
    ]


def test_a_sprite_draws_its_parts_at_pixel_offsets_and_keeps_index_0_clear() -> None:
    """The whole reason a sprite object is not a cell grid: 66% of the corpus's Y
    offsets are not multiples of 8, so a part lands between tiles. Index 0 has to
    stay transparent too — parts overlap, and an opaque one would erase whatever
    it was meant to sit in front of."""
    from celpix.core.sprite import Part
    from celpix.pipeline.pipeline import sprite_image

    # 4bpp planar: tile 1 is every bitplane set (index 15 throughout); tile 2 has
    # its left four pixels clear on every plane and its right four set.
    bank = bytes(32) + bytes([0xFF]) * 32 + bytes([0x0F]) * 32
    front = Part(x=0, y=0, index=1)
    behind = Part(x=3, y=5, index=2)
    doc = _sprite_doc([], [(front, behind)], bank=bank)
    image, drawn = sprite_image(doc, default_registry(), columns=1)

    assert drawn == 2
    assert (image.width, image.height) == (16, 16)  # the box, rounded out to tiles
    assert image.get(0, 0) == 15  # the front part, at the origin
    assert image.get(10, 12) == 15  # the one behind it, at its odd offset
    assert image.get(4, 12) == 0  # ...and its transparent half stayed clear


def test_a_sprite_object_is_view_only() -> None:
    """A canvas position resolves to a *part* through an overlap order rather
    than to a cell through a grid, so what an edit would change is not settled
    (``docs/design/tilemap-entry.md`` §9)."""
    from celpix.core.sprite import Part

    doc = _sprite_doc([Cell(index=1)], [(Part(index=1),)])
    assert doc.is_sprite and doc.is_tilemap
    assert not doc.cells_editable
    # An ordinary tilemap is the control: same class, same cells, editable.
    assert _sprite_doc([Cell(index=1)], None).cells_editable


def test_an_unknown_content_kind_reads_as_pixels() -> None:
    """Tolerant like the rest of the project reader — a newer build's kind, or a
    hand-edited typo, opens as pixels rather than failing the load."""
    assert ContentKind.parse("nonsense") is ContentKind.PIXELS
    assert ContentKind.parse(None) is ContentKind.PIXELS
    assert ContentKind.parse("tilemap") is ContentKind.TILEMAP


# -- saving ----------------------------------------------------------------
def test_saving_a_tilemap_writes_its_cells_through_its_own_container(tmp_path) -> None:
    """The entry's own data is its cells, and the container's write half keeps
    the metadata block around them."""
    from celpix.core.document import Document
    from celpix.pipeline.pipeline import load_tilemap_data, save

    registry = default_registry()
    path = tmp_path / "s.SCR"
    path.write_bytes(_scr_bytes(b"\x00" * 8))
    cfg = PathwayConfig(
        source=FileRef(str(path)),
        interpret_preset_id=SNES_BG,
        container_id="container.scgcad-scr",
    )
    loaded = load_tilemap_data(cfg, registry)
    doc = Document(
        pixel_data=b"",
        bytes_per_tile=32,
        tile_width=8,
        tile_height=8,
        palette=None,
        pixel_config=PathwayConfig(
            source=FileRef(""), interpret_preset_id=SNES_BG, write_enabled=False
        ),
        palette_config=PathwayConfig(
            source=FileRef(""), interpret_preset_id="", write_enabled=False
        ),
        cells=loaded.cells,
        tilemap_config=cfg,
        tilemap_ctx=loaded.ctx,
        tilemap_data=loaded.data,
    )
    doc.cells[0] = Cell(index=0x2AB, palette_row=5, priority=1, flip_v=True)
    save(doc, registry, palette=False)

    written = path.read_bytes()
    assert len(written) == SCR_SIZE
    assert written[0x2000:] == _scr_bytes()[0x2000:]  # header + trailer intact
    again = load_tilemap_data(cfg, registry)
    assert again.cells[0] == Cell(index=0x2AB, palette_row=5, priority=1, flip_v=True)


def test_saving_a_tilemap_never_writes_the_tiles_it_is_bound_to(tmp_path) -> None:
    """The bound entry owns its own bytes and saves them itself. A map's Write
    reaching into a second file is the kind of thing nothing else would catch."""
    from celpix.core.document import Document
    from celpix.pipeline.pipeline import load_tilemap_data, save

    registry = default_registry()
    scr = tmp_path / "s.SCR"
    scr.write_bytes(_scr_bytes(b"\x00" * 8))
    tiles = tmp_path / "tiles.bin"
    original = bytes(range(256)) * 4
    tiles.write_bytes(original)

    cfg = PathwayConfig(
        source=FileRef(str(scr)),
        interpret_preset_id=SNES_BG,
        container_id="container.scgcad-scr",
    )
    loaded = load_tilemap_data(cfg, registry)
    doc = Document(
        pixel_data=b"\xff" * len(original),  # as if the tiles had been edited
        bytes_per_tile=32,
        tile_width=8,
        tile_height=8,
        palette=None,
        # A writable config pointed at the tile file — what an ENTRY binding
        # would produce without the guard.
        pixel_config=PathwayConfig(
            source=FileRef(str(tiles)), interpret_preset_id="preset.pixel.snes-4bpp"
        ),
        palette_config=PathwayConfig(
            source=FileRef(""), interpret_preset_id="", write_enabled=False
        ),
        cells=loaded.cells,
        tilemap_config=cfg,
        tilemap_ctx=loaded.ctx,
    )
    save(doc, registry, palette=False)
    assert tiles.read_bytes() == original


# -- stamp layouts ---------------------------------------------------------
def test_a_stamp_entry_reads_as_one_panel_cell_index() -> None:
    """The two coordinate fields are adjacent and a panel is 32 wide, so the low
    14 bits read as one number *are* panelY * 32 + panelX — which is why the
    codec needs no way to compose two fields (scgcad-formats.md §4)."""
    registry = default_registry()
    codec, ctx = TilemapCodec(), PipelineContext()
    params = _params(registry, "preset.tilemap.scgcad-map")
    # panelX = 5, panelY = 9  ->  index 9*32 + 5 = 293, attribute-source set.
    word = (1 << 14) | (9 << 5) | 5
    (cell,) = codec.decode(word.to_bytes(2, "big"), params, ctx)
    assert cell.index == 9 * 32 + 5
    assert cell.flags == 1  # bit 14, carried and not interpreted


def test_a_stamp_entrys_unused_bits_survive_a_round_trip() -> None:
    """Bits 14-15 mean something to the authoring tool and nothing to celPix.
    Naming one `priority` to get it carried would be a lie; dropping it would
    corrupt the file on the next write."""
    registry = default_registry()
    codec, ctx = TilemapCodec(), PipelineContext()
    params = _params(registry, "preset.tilemap.scgcad-map")
    raw = bytes([0xC0, 0x45, 0x00, 0x11])  # both high bits set, then neither
    assert codec.encode(codec.decode(raw, params, ctx), params, ctx) == raw


def test_the_stamp_preset_declares_itself_indirect() -> None:
    """The host reads this to know the entry binds to another *tilemap* — a
    property of the format, not of whether a binding is resolved yet."""
    registry = default_registry()
    assert _params(registry, "preset.tilemap.scgcad-map").get("indirect") is True
    assert not _params(registry, SNES_BG).get("indirect")


def test_a_stamp_layout_resolves_through_the_panel_it_names() -> None:
    from celpix.ui.main_window.session import _resolve_stamp

    panel = [
        Cell(index=10, palette_row=3, flip_h=True),
        Cell(index=20, palette_row=1),
    ]
    # The panel's cell comes back whole: its tile *and* its attributes.
    assert _resolve_stamp(Cell(index=1), panel) == Cell(index=20, palette_row=1)
    assert _resolve_stamp(Cell(index=0), panel).flip_h
    # An entry naming a cell the panel does not have draws blank rather than
    # failing: a layout outliving the panel it was authored against is ordinary.
    assert _resolve_stamp(Cell(index=999), panel) == Cell()


# -- panels ----------------------------------------------------------------
def _pnl_bytes(*, tile_size=0, width_exp=1, height_exp=1, body=b"") -> bytes:
    """A panel whose three cell-size decoys are set as asked. The offsets are
    spelled out here rather than imported because the container does not read
    them at all — naming them in the module would be a constant nothing uses."""
    from celpix.plugins.builtins.scgcad import HEADER, PNL_SIZE

    out = bytearray(PNL_SIZE)
    out[: len(SIGNATURE)] = SIGNATURE
    out[0x62] = tile_size
    out[0x69] = width_exp
    out[0x6A] = height_exp
    out[HEADER : HEADER + len(body)] = body
    return bytes(out)


def test_a_panel_word_is_one_tile_whatever_its_header_says() -> None:
    """A panel has three header bytes that look like a cell size — 0x62 and the
    metatile exponents at 0x69/0x6A — and none of them is one. A 16x16 unit is
    stored as four adjacent words, in every panel of the corpus and under either
    setting of 0x62, so reading any of the three would draw the panel at four
    times its content (``scgcad-formats.md`` §3.1)."""
    from celpix.core.context import KEY_TILEMAP_CELL_TILES, KEY_TILEMAP_COLUMNS

    for header in (
        _pnl_bytes(tile_size=0, width_exp=1, height_exp=1),  # the common panel
        _pnl_bytes(tile_size=1, width_exp=0, height_exp=0),  # the 80 that differ
    ):
        ctx = PipelineContext()
        PnlContainer().read(ReadSource(data=header), ctx)
        assert ctx.get(KEY_TILEMAP_CELL_TILES) is None
        assert ctx.get(KEY_TILEMAP_COLUMNS) == 32


def test_a_screen_states_its_own_cell_size() -> None:
    """Where a panel's byte is a decoy, a screen's is real: of the
    cells the 949 screens that set it actually draw, 76.1% carry a
    metatile-aligned index against 30.0% for the 8x8 ones, where chance alone
    gives 25% (``scgcad-formats.md`` §2.3). Read at 8x8 a 16x16 screen draws one
    quarter of every cell and drops the rest."""
    from celpix.core.context import KEY_TILEMAP_CELL_TILES
    from celpix.plugins.builtins.scgcad import SCR_HEADER_AT, SCR_TILE_SIZE

    for value, want in ((0, (1, 1)), (1, (2, 2)), (0xFF, (1, 1))):
        data = bytearray(_scr_bytes())
        data[SCR_HEADER_AT + SCR_TILE_SIZE] = value
        ctx = PipelineContext()
        ScrContainer().read(ReadSource(data=bytes(data)), ctx)
        assert ctx.get(KEY_TILEMAP_CELL_TILES) == want, value


def test_a_screen_publishes_nothing_that_would_shift_its_cells() -> None:
    """The word at +0x47 looks like a base character index and is not one: it is
    non-zero in 83% of real screens, so adding it would send the whole screen off
    the end of a 1024-tile bank (``scgcad-formats.md`` §2, "Unresolved"). The
    container reads it into nothing, and a base index is the binding's to work
    out from the cells (``docs/design/tilemap-entry.md`` §3)."""
    data = bytearray(_scr_bytes())
    data[0x2047:0x2049] = (0x03EE).to_bytes(2, "big")  # the commonest value

    ctx = PipelineContext()
    ScrContainer().read(ReadSource(data=bytes(data)), ctx)
    assert 0x03EE not in ctx._entries.values()


def test_the_other_two_formats_state_their_widths() -> None:
    """A wrong width shears the picture into diagonal stripes rather than
    failing, so a format that knows should say."""
    from celpix.core.context import KEY_TILEMAP_COLUMNS

    ctx = PipelineContext()
    ScrContainer().read(ReadSource(data=_scr_bytes()), ctx)
    assert ctx.get(KEY_TILEMAP_COLUMNS) == 32

    data = bytearray(MAP_SIZE)
    data[: len(SIGNATURE)] = SIGNATURE
    ctx = PipelineContext()
    MapContainer().read(ReadSource(data=bytes(data)), ctx)
    assert ctx.get(KEY_TILEMAP_COLUMNS) == 128


# -- tile banks ------------------------------------------------------------
def _cgx_bytes(size: int, rows: bytes = b"") -> bytes:
    from celpix.plugins.builtins.scgcad import CGX_BANKS, HEADER

    payload = CGX_BANKS[size][0]
    out = bytearray(size)
    out[payload : payload + len(SIGNATURE)] = SIGNATURE
    if rows:
        at = payload + HEADER
        out[at : at + len(rows)] = rows
    return bytes(out)


def test_a_tile_bank_cuts_its_payload_and_names_its_own_depth() -> None:
    """Reading a bank raw almost works, which is the problem: the trailing
    header and table decode as convincing noise, and the three depths all look
    plausible. Both facts are in the file."""
    from celpix.core.context import KEY_PIXEL_PRESET
    from celpix.plugins.builtins.scgcad import CgxContainer

    for size, (payload, preset, _) in _cgx_banks().items():
        ctx = PipelineContext()
        out = CgxContainer().read(ReadSource(data=_cgx_bytes(size)), ctx)
        assert len(out) == payload, hex(size)
        assert ctx.get(KEY_PIXEL_PRESET) == preset, hex(size)


def _cgx_banks():
    from celpix.plugins.builtins.scgcad import CGX_BANKS

    return CGX_BANKS


def test_a_tile_banks_row_table_comes_out_as_a_hint() -> None:
    """One byte per tile, each the subpalette row that tile is meant to be read
    under - which is what pinned regions otherwise have to be told by hand."""
    from celpix.core.context import KEY_TILE_PALETTE_ROWS
    from celpix.plugins.builtins.scgcad import CgxContainer

    rows = bytes([2, 2, 5, 0]) + bytes(0x3FC)
    ctx = PipelineContext()
    CgxContainer().read(ReadSource(data=_cgx_bytes(0x8500, rows)), ctx)
    assert ctx.get(KEY_TILE_PALETTE_ROWS)[:4] == bytes([2, 2, 5, 0])
    # 8bpp banks carry no table, so nothing is claimed for them.
    ctx = PipelineContext()
    CgxContainer().read(ReadSource(data=_cgx_bytes(0x10100)), ctx)
    assert ctx.get(KEY_TILE_PALETTE_ROWS) is None


def test_a_tile_bank_of_an_unknown_size_is_passed_through_whole() -> None:
    """Better a few trailing junk tiles than a silently truncated bank."""
    from celpix.plugins.builtins.scgcad import CgxContainer

    data = bytearray(0x1234)
    data[0x1000 : 0x1000 + len(SIGNATURE)] = SIGNATURE
    out = CgxContainer().read(ReadSource(data=bytes(data)), PipelineContext())
    assert len(out) == 0x1234


def test_writing_a_tile_bank_keeps_its_header_and_row_table() -> None:
    """The rows are the file's own statement about its tiles; a pixel edit is no
    reason to rewrite them."""
    from celpix.plugins.builtins.scgcad import CgxContainer

    existing = _cgx_bytes(0x8500, bytes([7]) * 0x400)
    out = CgxContainer().write(
        b"\xab" * 0x8000, WriteTarget(existing=existing), PipelineContext()
    )
    assert len(out) == 0x8500
    assert out[:0x8000] == b"\xab" * 0x8000
    assert out[0x8000:] == existing[0x8000:]


# -- palettes (COL) --------------------------------------------------------
def _col_bytes(colors: bytes = b"") -> bytes:
    from celpix.plugins.builtins.scgcad import COL_HEADER_AT, COL_SIZE

    out = bytearray(COL_SIZE)
    out[: len(colors)] = colors
    out[COL_HEADER_AT : COL_HEADER_AT + len(SIGNATURE)] = SIGNATURE
    return bytes(out)


def test_a_palette_file_stops_at_its_metadata_block() -> None:
    """The colors are the first 0x200 and the tool's own block follows them. Read
    whole, that block decodes as 128 more BGR555 entries — junk rows 16-31 of a
    4bpp palette, which look like colors because any two bytes do."""
    from celpix.plugins.builtins.scgcad import COL_PAYLOAD, ColContainer

    ctx = PipelineContext()
    payload = ColContainer().read(ReadSource(data=_col_bytes(b"\x1f\x00")), ctx)
    assert len(payload) == COL_PAYLOAD
    assert payload[:2] == b"\x1f\x00"
    assert SIGNATURE not in payload
    assert ctx.get(KEY_SOURCE_OFFSET) == 0


def test_writing_a_palette_leaves_the_tools_metadata_block_alone() -> None:
    """celPix did not write that block and has no business regenerating it: it
    names the version that made the file."""
    from celpix.plugins.builtins.scgcad import COL_PAYLOAD, COL_SIZE, ColContainer

    existing = _col_bytes(b"\xff\x7f")
    out = ColContainer().write(
        b"\x00\x00" * (COL_PAYLOAD // 2),
        WriteTarget(path="p.COL", existing=existing),
        PipelineContext(),
    )
    assert len(out) == COL_SIZE
    assert out[:2] == b"\x00\x00"
    assert out[COL_PAYLOAD:] == existing[COL_PAYLOAD:]


def test_a_palette_container_is_offered_only_to_palettes(tmp_path) -> None:
    """The filter's point: a palette and a graphic are framed by disjoint sets of
    formats, so neither is ever offered the other's — which is what would let a
    user read a palette as a ROM, or a screen through a palette's framing."""
    from celpix.plugins.detect import containers_for

    registry = default_registry()
    for_palette = {info.id for info in containers_for(registry, ContentKind.PALETTE)}
    for_pixels = {info.id for info in containers_for(registry, ContentKind.PIXELS)}
    assert "container.scgcad-col" in for_palette
    assert "container.scgcad-col" not in for_pixels
    assert "container.scgcad-scr" in for_pixels
    assert "container.scgcad-scr" not in for_palette
    # Plain bytes is the fallback both kinds land on, so it belongs to both.
    assert "container.raw-file" in for_palette & for_pixels


def test_detection_respects_the_kind_it_is_asked_about(tmp_path) -> None:
    """A COL opened as a graphic must not be claimed by the palette container:
    detection and the dialog's list read the same declaration, so a container the
    dialog would not offer is one detection will not pick either."""
    path = tmp_path / "score.COL"
    path.write_bytes(_col_bytes())
    registry = default_registry()
    assert (
        detect_container(registry, str(path), kind=ContentKind.PALETTE)
        == "container.scgcad-col"
    )
    assert detect_container(registry, str(path)) == "container.raw-file"


def test_a_plain_pal_is_still_read_whole(tmp_path) -> None:
    """The COL container claims by signature and exact size, so an ordinary
    palette file keeps its plain-bytes reading and every byte of its colors."""
    path = tmp_path / "colors.pal"
    path.write_bytes(bytes(0x400))  # right size, no signature
    assert (
        detect_container(default_registry(), str(path), kind=ContentKind.PALETTE)
        == "container.raw-file"
    )
