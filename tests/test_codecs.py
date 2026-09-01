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


def _entries_per_unit(engine, params) -> int:  # noqa: ANN001
    """What the host assumes of a codec that doesn't declare it: one per unit."""
    per_unit = getattr(engine, "entries_per_unit", None)
    return per_unit(params) if per_unit else 1


@pytest.mark.parametrize("preset_id", _palette_ids())
def test_palette_preset_reports_entry_size(preset_id: str) -> None:
    """`bytes_per_entry` matches what decode actually consumes, for every palette
    preset — the host relies on it to size "load N entries" byte windows.

    Stated per *unit* rather than per entry, because the handheld grayscale
    registers pack four entries into one: three units of a Game Boy palette byte
    decode to twelve shades, and a host that read the pair the other way round
    would size every window a quarter too small."""
    engine, params = _color_engine(preset_id)
    bpe = engine.bytes_per_entry(params)
    per_unit = _entries_per_unit(engine, params)
    assert bpe > 0 and per_unit > 0
    palette = engine.decode(b"\x00" * (3 * bpe), params, PipelineContext())
    assert len(palette) == 3 * per_unit


# Index-producing pixel presets are bijective on whole buffers; direct-color is
# lossy at <8bpp/component, so it's round-tripped separately (idempotency).
_INDEX_PIXEL_IDS = [
    p for p in _pixel_ids() if _REG.preset(p).engine_id != "codec.pixel.direct-color"
]


@pytest.mark.parametrize("preset_id", _INDEX_PIXEL_IDS)
def test_pixel_preset_round_trips(preset_id: str) -> None:
    """`encode(decode(x)) == x` for every index-producing pixel preset.

    Pins each preset's parameters — a wrong plane count / nibble order / tile size
    breaks the identity or the reported geometry — across planar, packed, chunky,
    straddling-field and the wide/odd tile codecs.
    """
    engine, params = _pixel_engine(preset_id)
    tile_bytes = engine.bytes_per_tile(params)
    data = bytes((i * 61 + 7) & 0xFF for i in range(tile_bytes * 3))
    tiles = engine.decode(data, params, PipelineContext())
    assert len(tiles) == 3
    tw, th = engine.tile_size(params)
    assert all(t.width == tw and t.height == th for t in tiles)
    assert engine.encode(tiles, params, PipelineContext()) == data


@pytest.mark.parametrize("preset_id", _pixel_ids(engine="codec.pixel.direct-color"))
def test_direct_color_round_trips(preset_id: str) -> None:
    """Direct-color presets decode to 8×8 ARGB tiles and round-trip idempotently."""
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


def test_intensity_presets_share_one_mask_across_rgb() -> None:
    """The I/IA presets give R, G and B the same mask on purpose, so a stored value
    decodes to grey and re-encodes to the very same byte — not merely idempotently.

    The shared mask is the part worth pinning: give the components separate masks
    and the image comes out tinted instead of grey.
    """

    def first_pixels(preset_id: str, probe: bytes) -> list[int]:
        engine, params = _pixel_engine(preset_id)
        data = probe + bytes(engine.bytes_per_tile(params) - len(probe))
        grid = engine.decode(data, params, PipelineContext())[0]
        count = len(probe) // params["bytes_per_pixel"]
        assert engine.encode([grid], params, PipelineContext()) == data
        return [grid.get(i, 0) for i in range(count)]

    # I8: the byte is the grey level, always opaque.
    assert first_pixels("preset.pixel.dc-i8", bytes([0x00, 0x7F, 0xFF])) == [
        0xFF000000,
        0xFF7F7F7F,
        0xFFFFFFFF,
    ]
    # IA8: high nibble grey, low nibble alpha (so 0xF0 is *transparent* white).
    assert first_pixels("preset.pixel.dc-ia8", bytes([0xF0, 0x0F])) == [
        0x00FFFFFF,
        0xFF000000,
    ]
    # IA16 big-endian: intensity byte then alpha byte.
    assert first_pixels("preset.pixel.dc-ia16-be", bytes([0x80, 0x40])) == [0x40808080]


@pytest.mark.parametrize("preset_id", _palette_ids(engine="codec.palette.mask"))
def test_mask_palette_round_trips(preset_id: str) -> None:
    """`decode(encode(pal)) == pal` for every mask-based palette preset.

    Idempotent after the first decode — robust for the lossy 5-/4-/3-bit formats,
    where the raw bits don't round-trip but the decoded canonical value does.
    """
    engine, params = _color_engine(preset_id)
    size = engine.bytes_per_entry(params)
    data = bytes((i * 53 + 11) & 0xFF for i in range(size * 16))
    pal = engine.decode(data, params, PipelineContext())
    assert len(pal) == 16 * _entries_per_unit(engine, params)
    again = engine.decode(
        engine.encode(pal, params, PipelineContext()), params, PipelineContext()
    )
    assert again == pal


@pytest.mark.parametrize("preset_id", _palette_ids(engine="codec.palette.indexed"))
def test_indexed_palette_round_trips_in_range(preset_id: str) -> None:
    """Indexed decode→encode recovers the same colors for every table index.

    Encode picks the nearest table entry; an exact color resolves to an index with
    that color (distance 0), so the decoded palette round-trips even when duplicate
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
    # An off-table color encodes to the nearest table entry (EGA index 0 = black).
    from celpix.core.palette import Palette

    assert engine.encode(Palette([0xFF000001]), params, PipelineContext()) == b"\x00"


@pytest.mark.parametrize("preset_id", _pixel_ids(engine="codec.pixel.planar"))
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


# Arcade boards commonly wire one bitplane per ROM chip, so a tile's planes sit in
# N equal *parts* of the graphics region instead of inside the tile. celPix reads
# that by joining the parts first (`reshape.split-planes-N`) and then using the
# ordinary interleaved-planar presets. The pairing is only right if the join's
# ordering and the preset's plane offsets agree, so it is checked here against a
# reference that indexes the unjoined region directly.
def _region_split_reference(region: bytes, planes: int, code: int) -> list[list[int]]:
    """Tile ``code`` of an 8×8 region-split planar layout, decoded independently.

    Bit *k* of every pixel comes from part *k*; inside a part a tile is 8 bytes,
    one per row, MSB first. See ``docs/graphics-formats-reference/mame-formats.md``
    §1.1 (region fractions) and §4.1 (the layout family).
    """
    part = len(region) // planes
    return [
        [
            sum(
                ((region[k * part + code * 8 + y] >> (7 - x)) & 1) << k
                for k in range(planes)
            )
            for x in range(8)
        ]
        for y in range(8)
    ]


@pytest.mark.parametrize(
    ("planes", "preset_id"),
    [
        (2, "preset.pixel.snes-2bpp"),
        (3, "preset.pixel.3bpp-planar"),
        (4, "preset.pixel.sms-4bpp"),
    ],
)
def test_split_plane_join_feeds_interleaved_planar_presets(
    planes: int, preset_id: str
) -> None:
    """Joining N parts then decoding must equal the region-split layout, and the
    write path must return every byte to the part it came from."""
    tiles_wanted = 3
    region = bytes((i * 61 + 7) & 0xFF for i in range(8 * tiles_wanted * planes))
    ctx = PipelineContext()
    joined = _REG.plugin(Stage.RESHAPE, f"reshape.split-planes-{planes}").reshape(
        region, ctx
    )

    engine, params = _pixel_engine(preset_id)
    grids = engine.decode(joined, params, ctx)
    assert len(grids) == tiles_wanted
    for code, grid in enumerate(grids):
        actual = [[grid.get(x, y) for x in range(8)] for y in range(8)]
        assert actual == _region_split_reference(region, planes, code)

    back = _REG.plugin(Stage.RESHAPE, f"reshape.split-planes-{planes}")
    assert back.unshape(engine.encode(grids, params, ctx), ctx) == region


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


@pytest.mark.parametrize("stride", [1, 4, 32])
def test_packed_nibble_stride_splits_the_index_across_two_bytes(stride: int) -> None:
    """With ``nibble_stride`` a pixel's index is assembled from two bytes that far
    apart, the nearer one carrying its *high* half.

    The stride is also the run length, so 1 alternates the two halves byte by
    byte, 4 alternates whole rows and 32 puts the tile's high halves before all of
    its low ones. Getting the halves the wrong way round still round-trips, so the
    order is pinned directly.
    """
    engine = _REG.plugin(Stage.INTERPRET_PIXEL, "codec.pixel.packed")
    params = {"bpp": 8, "msb_first": True, "nibble_stride": stride}
    assert engine.bytes_per_tile(params) == 64  # an 8x8 8bpp tile, split or not

    data = bytearray(64)
    data[0] = 0x12  # first high-half byte: pixels 0 and 1 -> index bits 7..4
    data[stride] = 0x34  # its partner: the same two pixels' index bits 3..0
    tile = engine.decode(bytes(data), params, PipelineContext())[0]
    assert [tile.get(x, 0) for x in range(3)] == [0x13, 0x24, 0x00]

    body = bytes((i * 61 + 7) & 0xFF for i in range(128))
    tiles = engine.decode(body, params, PipelineContext())
    assert engine.encode(tiles, params, PipelineContext()) == body


def test_packed_nibble_stride_1_reads_a_joined_pair_of_4bpp_halves() -> None:
    """A board wiring each half-index to its own ROM lands on ``nibble_stride=1``
    once ``reshape.split-planes-2`` has joined the two halves.

    Cross-checked against the shipped 4bpp preset rather than a restatement of the
    kernel: decode each half alone, and the joined 8bpp index must be the first
    half's index in its top nibble and the second's in its bottom.
    """
    half_engine, half_params = _pixel_engine("preset.pixel.genesis-4bpp")
    ctx = PipelineContext()
    high = bytes((i * 37 + 11) & 0xFF for i in range(32))
    low = bytes((i * 53 + 5) & 0xFF for i in range(32))
    hi_tile = half_engine.decode(high, half_params, ctx)[0]
    lo_tile = half_engine.decode(low, half_params, ctx)[0]

    joined = _REG.plugin(Stage.RESHAPE, "reshape.split-planes-2").reshape(
        high + low, ctx
    )
    engine = _REG.plugin(Stage.INTERPRET_PIXEL, "codec.pixel.packed")
    params = {"bpp": 8, "msb_first": True, "nibble_stride": 1}
    tile = engine.decode(joined, params, ctx)[0]

    assert [[tile.get(x, y) for x in range(8)] for y in range(8)] == [
        [hi_tile.get(x, y) << 4 | lo_tile.get(x, y) for x in range(8)] for y in range(8)
    ]


def test_nibble_planar_plane_pair_and_nibble_order() -> None:
    """A nibble-planar byte's high nibble is the *more* significant of the two
    planes it carries and bit 3 of a nibble is the leftmost of its four pixels;
    within a group, byte 0 carries the top two index bits.

    All four of those can be wrong while still round-tripping, so they are pinned
    directly (``docs/graphics-formats-reference/mame-formats.md`` §3).
    """

    def tile(preset_id: str, head: list[int]):
        engine, params = _pixel_engine(preset_id)
        data = bytes(head) + bytes(engine.bytes_per_tile(params) - len(head))
        return engine.decode(data, params, PipelineContext())[0]

    # 2bpp: a single byte is four pixels. 0xB4 = high nibble 1011 (index bit 1),
    # low nibble 0100 (bit 0), leftmost pixel first.
    chars = tile("preset.pixel.atari-sy2-chars-2bpp", [0xB4])
    assert [chars.get(x, 0) for x in range(4)] == [2, 1, 2, 2]
    assert chars.get(4, 0) == 0  # pixels 4..7 come from the next byte

    # 4bpp: two bytes per group, most significant plane pair first.
    tiles_4 = "preset.pixel.atari-sy2-tiles-4bpp"
    assert tile(tiles_4, [0x80]).get(0, 0) == 0b1000  # byte 0 high nibble
    assert tile(tiles_4, [0x08]).get(0, 0) == 0b0100  # byte 0 low nibble
    assert tile(tiles_4, [0x00, 0x80]).get(0, 0) == 0b0010  # byte 1 high nibble
    assert tile(tiles_4, [0x00, 0x08]).get(0, 0) == 0b0001  # byte 1 low nibble
    # Groups run left to right, so the second group drives pixels 4..7.
    assert tile(tiles_4, [0, 0, 0x80]).get(4, 0) == 0b1000

    # 16 pixels wide is four groups, so a row is eight bytes and the rightmost
    # group starts at byte 6 — the check that row stride follows tile width.
    sprite = tile("preset.pixel.atari-sy2-sprites-4bpp", [0] * 6 + [0x80])
    assert sprite.get(12, 0) == 0b1000


def test_chunky_is_row_major_index_per_byte() -> None:
    """One byte per pixel, row-major — the 8bpp end of the packed engine.

    There is no separate chunky codec: at ``bpp = 8`` a packed field fills the
    byte, so this pins that the degenerate depth really is a straight copy rather
    than something the field machinery reorders.
    """
    engine, params = _pixel_engine("preset.pixel.8bpp-linear")
    assert params["bpp"] == 8
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


def test_a_low_bit_channel_reaches_full_white() -> None:
    """A field's maximum must scale to 0xFF, however few bits it has.

    Replicating the pattern once leaves a 3-bit `7` at 0xE7 and a 2-bit `3` at
    0xC3 — plausible enough to pass unnoticed, and wrong by the whole top of the
    range for the Genesis, the PC Engine and the handheld greys, which are all
    2-3 bits a channel.
    """
    for preset_id, data, expect in [
        ("preset.palette.genesis-9bpp", b"\x0e\xee", 0xFFFFFFFF),  # 3 bits
        ("preset.palette.pce-grb333", b"\xff\x01", 0xFFFFFFFF),  # 3 bits, packed
        ("preset.palette.sms-6bpp", b"\x3f", 0xFFFFFFFF),  # 2 bits
    ]:
        engine, params = _color_engine(preset_id)
        assert engine.decode(data, params, PipelineContext()).color(0) == expect


def test_the_game_boy_palette_register_is_four_shades_in_one_byte() -> None:
    """`BGP` = 0xE4 is the identity palette: white, light, dark, black.

    Pins all three things that make a register a palette — entries packed from
    the low bits up, a value that counts darkness rather than brightness, and one
    field driving R, G and B — in the one byte every Game Boy programmer knows
    the expected output of.
    """
    engine, params = _color_engine("preset.palette.gb-bgp")
    assert engine.bytes_per_entry(params) == 1
    assert engine.entries_per_unit(params) == 4
    pal = engine.decode(b"\xe4", params, PipelineContext())
    assert [pal.color(i) for i in range(4)] == [
        0xFFFFFFFF,
        0xFFAAAAAA,
        0xFF555555,
        0xFF000000,
    ]
    assert engine.encode(pal, params, PipelineContext()) == b"\xe4"


def test_a_grey_format_reduces_a_colour_by_luma() -> None:
    """One field for three channels means a colour has to become a shade.

    Naming those bits `r`, `g` and `b` instead would put each channel's own value
    through the field and OR the results, which turns a pure red into black on an
    inverted format — so the reduction has to be stated, not fallen into.
    """
    engine, params = _color_engine("preset.palette.ngp-gray")
    raw = engine.encode(Palette([0xFFFF0000]), params, PipelineContext())
    # Rec. 601 luma of pure red is 0.299 -> 2/7 of the way up an inverted 3-bit
    # ramp, so the stored darkness is 5 and it reads back as a dark grey.
    assert raw == b"\x05"
    assert engine.decode(raw, params, PipelineContext()).color(0) == 0xFF494949


def test_a_palette_stated_component_by_component_still_reads() -> None:
    """A user's plugins folder is not ours to rewrite.

    Presets out there give each component its own hex mask, a split channel as an
    ordered list of chunks, and a shade as one mask plus `gray`. All three keep
    reading, and have to agree with the layout that says the same thing.
    """
    engine, params = _color_engine("preset.palette.rgb555-split-be")
    per_component = {
        "bytes_per_entry": 2,
        "byte_order": "big",
        "masks": {"r": [0xF000, 0x0008], "g": [0x0F00, 0x0004], "b": [0x00F0, 0x0002]},
    }
    raw = b"\xf0\x04"
    assert engine.decode(raw, per_component, PipelineContext()).color(
        0
    ) == engine.decode(raw, params, PipelineContext()).color(0)

    shade_engine, shade_params = _color_engine("preset.palette.ngp-gray")
    stated = {
        "bytes_per_entry": 1,
        "byte_order": "little",
        "invert": True,
        "gray": True,
        "masks": {"r": 0x07},
    }
    assert shade_engine.encode(
        Palette([0xFFFF0000]), stated, PipelineContext()
    ) == shade_engine.encode(Palette([0xFFFF0000]), shade_params, PipelineContext())


def test_a_layout_must_account_for_every_bit_of_its_entry() -> None:
    """A component the layout does not place is written back as zero, so a bit
    nobody named loses a channel's precision on the first save. Checking the
    layout against the entry width turns that into a load error rather than a
    palette that quietly comes back slightly wrong."""
    engine, _ = _color_engine("preset.palette.rgb565")
    short = {"bytes_per_entry": 2, "fields": "rrrr rggg gggb"}
    with pytest.raises(ValueError, match="describes 12 bits and the format is 16"):
        engine.decode(b"\x00\x00", short, PipelineContext())


def test_split_field_palette_gathers_the_stray_low_bits() -> None:
    """A channel whose low bit sits away from its nibble must read as one field,
    high chunk first. Pinning the *low* bits is the point: drop them and the
    colour is still plausible (off by 1/32), so only an exact vector catches it.
    """
    engine, params = _color_engine("preset.palette.rgb555-split-be")
    # R nibble full, R low bit clear -> 5-bit 0b11110; G low bit alone -> 0b00001.
    pal = engine.decode(b"\xf0\x04", params, PipelineContext())
    assert pal.color(0) == 0xFFF70800


def test_split_mask_palette_write_back_keeps_every_defined_bit() -> None:
    """5 bits a channel survive a decode/encode round trip exactly, so editing one
    colour cannot quietly clear the low bits of the fifteen beside it. Bit 0 is
    unused by the hardware and is the only bit allowed to come back zeroed."""
    engine, params = _color_engine("preset.palette.rgb555-split-be")
    raw = bytes.fromhex("f008 0f04 00f2 ffff 1234 aa55 0000 8001")
    back = engine.encode(
        engine.decode(raw, params, PipelineContext()), params, PipelineContext()
    )
    assert back == bytes(b & (0xFE if i % 2 else 0xFF) for i, b in enumerate(raw))


def test_split_field_direct_color_matches_the_palette_path() -> None:
    """Direct colour converts through precomputed byte-plane tables rather than
    per value, so it is a second implementation of the same gather. Given the same
    layout the two must agree pixel for pixel."""
    palette_engine, palette_params = _color_engine("preset.palette.rgb555-split-be")
    pixel_engine = _REG.plugin(Stage.INTERPRET_PIXEL, "codec.pixel.direct-color")
    params = {
        "bytes_per_pixel": 2,
        "byte_order": "big",
        "fields": palette_params["fields"],
        "tile_width": 2,
        "tile_height": 2,
    }
    data = bytes.fromhex("f008 0f04 00f2 a55a")
    grid = pixel_engine.decode(data, params, PipelineContext())[0]
    expected = palette_engine.decode(data, palette_params, PipelineContext())
    assert [grid.get(x, y) for y in range(2) for x in range(2)] == [
        expected.color(i) for i in range(4)
    ]
    assert pixel_engine.encode([grid], params, PipelineContext()) == bytes(
        b & (0xFE if i % 2 else 0xFF) for i, b in enumerate(data)
    )


def test_rgb888_known_vector() -> None:
    engine, params = _color_engine("preset.palette.rgb888")
    pal = engine.decode(b"\x12\x34\x56", params, PipelineContext())
    assert pal.color(0) == 0xFF123456


def test_missing_color_sentinel() -> None:
    pal = Palette([0xFF000000])
    assert pal.color(5) == MISSING_COLOR


# The shipped `reshape/` tables are pixel-order *conventions* rather than
# board-specific scrambles: each converts a widespread bit ordering into the one
# a codec here already reads. That pairing is the whole content of the preset, so
# it is checked against a reference decode of the layout it claims to unlock.
def _mame_decode(
    data: bytes,
    layout: tuple[int, int, list[int], list[int], list[int], int],
    code: int,
) -> list[list[int]]:
    """Tile ``code`` of a MAME ``gfx_layout``, by a port of ``gfx_element::decode``.

    All four offset arrays are counted in bits, MSB-first inside a byte, and
    ``planeoffset[0]`` supplies the *most* significant index bit — the two
    conventions that make an otherwise plausible decode wrong
    (``docs/graphics-formats-reference/mame-formats.md`` §1.1).
    """
    width, height, planeoffset, xoffset, yoffset, charincrement = layout
    planes = len(planeoffset)

    def bit(n: int) -> int:
        return (data[n // 8] >> (7 - n % 8)) & 1

    return [
        [
            sum(
                bit(code * charincrement + planeoffset[p] + yoffset[y] + xoffset[x])
                << (planes - 1 - p)
                for p in range(planes)
            )
            for x in range(width)
        ]
        for y in range(height)
    ]


def _step(count: int, start: int, step: int) -> list[int]:
    return [start + i * step for i in range(count)]


@pytest.mark.parametrize(
    ("reshape_id", "preset_id", "layout"),
    [
        # Two 4bpp pixels woven bit by bit through a byte: planes two bits apart,
        # pixels one. De-interleaved, the odd bits become the high nibble.
        (
            "reshape.deinterleave-nibbles",
            "preset.pixel.genesis-4bpp",
            (8, 8, [0, 2, 4, 6], [0, 1, 8, 9, 16, 17, 24, 25], _step(8, 0, 32), 8 * 32),
        ),
        # The other weave phase — left pixel in the even bits. It needs no second
        # table: the phases differ by a nibble swap, which is `msb_first`.
        (
            "reshape.deinterleave-nibbles",
            "preset.pixel.gba-4bpp",
            (8, 8, [0, 2, 4, 6], [1, 0, 9, 8, 17, 16, 25, 24], _step(8, 0, 32), 8 * 32),
        ),
        # Nibble-planar with the four-pixel groups counted the other way round:
        # bit 0 of a nibble is the leftmost pixel instead of bit 3.
        (
            "reshape.reverse-nibble-bits",
            "preset.pixel.atari-sy2-chars-2bpp",
            (8, 8, [0, 4], [3, 2, 1, 0, 11, 10, 9, 8], _step(8, 0, 16), 8 * 16),
        ),
        # A plain interleaved-planar tile whose rows are read lsb-first, which is
        # what MAME spells as the GFXENTRY_REVERSE flag.
        (
            "reshape.reverse-byte-bits",
            "preset.pixel.snes-2bpp",
            (8, 8, [8, 0], _step(8, 7, -1), _step(8, 0, 16), 8 * 16),
        ),
    ],
)
def test_shipped_reshape_table_unlocks_its_layout(
    reshape_id: str, preset_id: str, layout: tuple
) -> None:
    """Reshaping then decoding must equal the layout the preset names, and the
    write path must put every byte back exactly as it was."""
    engine, params = _pixel_engine(preset_id)
    tiles_wanted = 3
    charincrement = layout[5]
    region = bytes(
        (i * 61 + 7) & 0xFF for i in range(tiles_wanted * charincrement // 8)
    )
    ctx = PipelineContext()
    reshape = _REG.plugin(Stage.RESHAPE, reshape_id)

    grids = engine.decode(reshape.reshape(region, ctx), params, ctx)
    assert len(grids) == tiles_wanted
    for code, grid in enumerate(grids):
        actual = [[grid.get(x, y) for x in range(8)] for y in range(8)]
        assert actual == _mame_decode(region, layout, code), f"tile {code}"

    assert reshape.unshape(engine.encode(grids, params, ctx), ctx) == region


@pytest.mark.parametrize(
    ("preset_id", "layout"),
    [
        # gfx_16x16x4_packed_msb / _lsb (src/emu/video/generic.cpp) — the two
        # nibble orders of the most-used 16-wide arcade tile. The _lsb form is
        # written as a permuted xoffset rather than as a flag, which is what
        # `msb_first` recovers.
        (
            "preset.pixel.4bpp-linear-16x16-msb",
            (16, 16, [0, 1, 2, 3], _step(16, 0, 4), _step(16, 0, 64), 16 * 16 * 4),
        ),
        (
            "preset.pixel.4bpp-linear-16x16-lsb",
            (
                16,
                16,
                [0, 1, 2, 3],
                [4, 0, 12, 8, 20, 16, 28, 24, 36, 32, 44, 40, 52, 48, 60, 56],
                _step(16, 0, 64),
                16 * 16 * 4,
            ),
        ),
        (
            "preset.pixel.4bpp-linear-32x32-msb",
            (32, 32, [0, 1, 2, 3], _step(32, 0, 4), _step(32, 0, 128), 32 * 32 * 4),
        ),
    ],
)
def test_wide_packed_preset_matches_its_layout(preset_id: str, layout: tuple) -> None:
    """The wide packed presets decode exactly as MAME's layout of the same name.

    Tile size is a codec parameter rather than a fixed 8×8, so what needs pinning
    is that a row runs the *full* width — a 16-wide tile is eight bytes across,
    not two 8×8 tiles side by side, and reading it the other way shears the image
    without failing.
    """
    engine, params = _pixel_engine(preset_id)
    width, height = layout[0], layout[1]
    assert engine.tile_size(params) == (width, height)
    tiles_wanted = 2
    data = bytes(
        (i * 61 + 7) & 0xFF for i in range(tiles_wanted * engine.bytes_per_tile(params))
    )
    ctx = PipelineContext()
    grids = engine.decode(data, params, ctx)
    assert len(grids) == tiles_wanted
    for code, grid in enumerate(grids):
        actual = [[grid.get(x, y) for x in range(width)] for y in range(height)]
        assert actual == _mame_decode(data, layout, code), f"tile {code}"
    assert engine.encode(grids, params, ctx) == data


# Presets that decode identically on purpose, because the *name* is the point:
# a reader looking for their hardware should find it without knowing which other
# machine shares the encoding. Three symmetries generate all of them — reversing
# both the byte order and the channel order of a direct-color format cancels, the
# interleaved-planar 2bpp layout is shared hardware to hardware, and a packed
# depth is only nibble order plus a tile size, which several machines land on.
_INTENTIONAL_PIXEL_ALIASES = frozenset(
    {
        frozenset({"preset.pixel.dc-abgr8888", "preset.pixel.dc-rgba8888-be"}),
        frozenset({"preset.pixel.dc-abgr8888-be", "preset.pixel.dc-rgba8888"}),
        frozenset({"preset.pixel.dc-argb8888", "preset.pixel.dc-bgra8888-be"}),
        frozenset({"preset.pixel.dc-argb8888-be", "preset.pixel.dc-bgra8888"}),
        frozenset({"preset.pixel.dc-bgr888", "preset.pixel.dc-rgb888-be"}),
        frozenset({"preset.pixel.dc-bgr888-be", "preset.pixel.dc-rgb888"}),
        frozenset({"preset.pixel.dc-bbgggrrr", "preset.pixel.dc-snes-direct"}),
        frozenset({"preset.pixel.pce-4bpp", "preset.pixel.snes-4bpp"}),
        frozenset(
            {"preset.pixel.gb-2bpp", "preset.pixel.snes-2bpp", "preset.pixel.ws-2bpp"}
        ),
        # The TIM container names the format its header states, and a PlayStation
        # texture labelled with a handheld's name reads as the wrong file.
        frozenset({"preset.pixel.gba-4bpp", "preset.pixel.psx-4bpp"}),
        frozenset({"preset.pixel.8bpp-linear", "preset.pixel.psx-8bpp"}),
    }
)


def test_pixel_presets_duplicate_only_where_intended() -> None:
    """No preset may restate another except the documented aliases above.

    Engines overlap in ways parameters don't show — a 16-wide 1bpp packed tile is
    byte for byte the wide-1bpp `halves` mode, across two different engines — so a
    new preset can silently duplicate a shipped one. Comparing *decodes* is what
    catches that; pinning the alias set rather than forbidding aliases keeps the
    deliberate ones without letting an accidental one through.
    """
    ctx = PipelineContext()
    groups: dict[tuple, set[str]] = {}
    for preset_id in _pixel_ids():
        engine, params = _pixel_engine(preset_id)
        data = bytes(
            (i * 61 + 7) & 0xFF for i in range(engine.bytes_per_tile(params) * 2)
        )
        grids = engine.decode(data, params, ctx)
        key = (engine.tile_size(params), tuple(bytes(g.data) for g in grids))
        groups.setdefault(key, set()).add(preset_id)
    found = {frozenset(ids) for ids in groups.values() if len(ids) > 1}
    assert found == _INTENTIONAL_PIXEL_ALIASES


@pytest.mark.parametrize(
    ("preset_id", "width", "height"),
    [
        ("preset.pixel.4bpp-planar-16x16", 16, 16),
        ("preset.pixel.4bpp-planar-32x32", 32, 32),
    ],
)
def test_wide_planar_preset_matches_the_region_split_layout(
    preset_id: str, width: int, height: int
) -> None:
    """A wide planar preset over a 4-way join equals MAME's `gfx_16x16x4_planar`.

    The 8×8 pairing is checked by ``test_split_plane_join_feeds_interleaved_planar
    _presets``; what is new here is the *group* term, so this pins that pixel
    ``x`` reads byte ``x // 8`` of its row across the whole width. Getting the
    group stride wrong still round-trips — it just shows the halves swapped or
    sheared — so the round-trip test cannot catch it.
    """
    planes, count = 4, 2
    per_tile = width * height // 8  # one fraction's bytes for one tile
    region = bytes((i * 61 + 7) & 0xFF for i in range(per_tile * count * planes))
    ctx = PipelineContext()
    join = _REG.plugin(Stage.RESHAPE, f"reshape.split-planes-{planes}")

    engine, params = _pixel_engine(preset_id)
    assert engine.tile_size(params) == (width, height)
    grids = engine.decode(join.reshape(region, ctx), params, ctx)
    assert len(grids) == count

    part, row_bytes = len(region) // planes, width // 8
    for code, grid in enumerate(grids):
        for y in range(height):
            for x in range(width):
                byte = code * per_tile + y * row_bytes + x // 8
                assert grid.get(x, y) == sum(
                    ((region[k * part + byte] >> (7 - x % 8)) & 1) << k
                    for k in range(planes)
                ), f"tile {code} pixel ({x},{y})"

    assert join.unshape(engine.encode(grids, params, ctx), ctx) == region
