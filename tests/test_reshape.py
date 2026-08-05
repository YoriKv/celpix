"""The Reshape stage: plugin round trips, pipeline position, and write rules."""

from __future__ import annotations

import pytest

from celpix.core.context import (
    KEY_COMPRESSED_SIZE,
    KEY_DECOMPRESS_COMPLETE,
    PipelineContext,
)
from celpix.core.errors import PipelineError, Stage
from celpix.pipeline import pipeline
from celpix.pipeline.pathway import PathwayConfig
from celpix.plugins.base import NO_RESHAPE, FileRef, PluginInfo
from celpix.plugins.bitswap import (
    MAX_BITS,
    MAX_PERMUTED_BITS,
    BitswapReshape,
    bitswap_from_spec,
)
from celpix.plugins.builtins.byte_swap import ByteSwapReshape
from celpix.plugins.builtins.konami_rle import KonamiNesRle
from celpix.plugins.builtins.m7_vram import M7VramReshape
from celpix.plugins.builtins.split_planes import (
    PART_COUNTS,
    WORD_PART_COUNTS,
    SplitPartsReshape,
)
from celpix.plugins.data_lut import (
    DATA_LUT_ENGINE,
    MAX_SELECTOR_BIT,
    data_lut_from_spec,
)
from celpix.plugins.registry import default_registry
from celpix.project.workspace import backfill_slice_length, new_slice

# -- SNES Mode 7 VRAM split -------------------------------------------------


def test_m7_vram_split_known_vector() -> None:
    # Interleaved words: map bytes at even offsets, pixel bytes at odd offsets.
    # The split puts the pixels first, then the map.
    interleaved = bytes([0xA0, 0x01, 0xA1, 0x02, 0xA2, 0x03])
    split = M7VramReshape().reshape(interleaved, PipelineContext())
    assert split == bytes([0x01, 0x02, 0x03, 0xA0, 0xA1, 0xA2])
    assert M7VramReshape().unshape(split, PipelineContext()) == interleaved


def test_m7_vram_split_round_trips_odd_length() -> None:
    data = bytes((i * 37 + 5) & 0xFF for i in range(129))
    split = M7VramReshape().reshape(data, PipelineContext())
    assert M7VramReshape().unshape(split, PipelineContext()) == data


# -- Split bitplanes (N equal parts, one per plane) --------------------------


def test_split_planes_join_interleaves_parts_in_order() -> None:
    """Part *k* must land on tile byte ``k + N * y``, which is what makes the
    shipped ``{ base = k, stride = N }`` planar presets read the joined buffer."""
    parts = bytes((0x10, 0x11, 0x12)) + bytes((0x20, 0x21, 0x22))
    joined = SplitPartsReshape(2).reshape(parts, PipelineContext())
    assert joined == bytes((0x10, 0x20, 0x11, 0x21, 0x12, 0x22))
    assert SplitPartsReshape(2).unshape(joined, PipelineContext()) == parts


@pytest.mark.parametrize("parts", PART_COUNTS)
def test_split_planes_round_trips_including_ragged_tail(parts: int) -> None:
    """Both directions are exact for every part count, and a length that isn't a
    whole number of parts keeps its tail rather than shearing the image."""
    plugin = SplitPartsReshape(parts)
    ctx = PipelineContext()
    for length in (0, 1, parts * 8 - 1, parts * 8, parts * 8 + 3):
        data = bytes((i * 37 + 5) & 0xFF for i in range(length))
        assert plugin.unshape(plugin.reshape(data, ctx), ctx) == data
        assert plugin.reshape(plugin.unshape(data, ctx), ctx) == data


# -- Byte-swapped words (ROM_LOAD16_WORD_SWAP) -------------------------------


def test_byte_swap_exchanges_each_pair_and_keeps_an_odd_tail() -> None:
    """The transform is its own inverse, so one vector pins both directions. The
    odd trailing byte cannot form a pair and passes through, as with the joins."""
    plugin = ByteSwapReshape()
    ctx = PipelineContext()
    assert plugin.reshape(bytes((0x10, 0x11, 0x12, 0x13, 0x14)), ctx) == bytes(
        (0x11, 0x10, 0x13, 0x12, 0x14)
    )
    for length in (0, 1, 8, 9):
        data = bytes((i * 37 + 5) & 0xFF for i in range(length))
        assert plugin.unshape(plugin.reshape(data, ctx), ctx) == data


def test_byte_swap_is_not_the_two_chip_interleave() -> None:
    """Both act on byte pairs and neither changes a byte's value, so the pickers
    sit them side by side — but one reads a single dump the other way round and
    the other weaves two dumps together. Confusing them is a silent misread."""
    data = bytes((0x10, 0x11, 0x12, 0x13))
    ctx = PipelineContext()
    assert ByteSwapReshape().reshape(data, ctx) != SplitPartsReshape(2).reshape(
        data, ctx
    )


# -- Chip word interleave (ROM_LOAD32_WORD / ROM_LOAD64_WORD) ----------------


def test_word_interleave_weaves_16bit_words_alternately() -> None:
    """Two chips alternating at 16-bit granularity — the byte pairs must stay
    intact, which is exactly what distinguishes this from split-planes-2."""
    chips = bytes((0x10, 0x11, 0x12, 0x13)) + bytes((0x20, 0x21, 0x22, 0x23))
    joined = SplitPartsReshape(2, unit=2).reshape(chips, PipelineContext())
    assert joined == bytes((0x10, 0x11, 0x20, 0x21, 0x12, 0x13, 0x22, 0x23))
    assert SplitPartsReshape(2, unit=2).unshape(joined, PipelineContext()) == chips


def test_word_interleave_four_chips_fills_the_lanes_in_order() -> None:
    """``ROM_LOAD64_WORD``: four chips, one 16-bit lane each of a 64-bit bus, so
    one 8-byte group is a word from every chip in chip order. The lane order is
    the whole content of the transform — get it wrong and the picture survives
    while the palette indices scramble."""
    chips = b"".join(
        bytes((base, base + 1, base + 2, base + 3)) for base in (0x10, 0x20, 0x30, 0x40)
    )
    joined = SplitPartsReshape(4, unit=2).reshape(chips, PipelineContext())
    assert joined == bytes(
        (0x10, 0x11, 0x20, 0x21, 0x30, 0x31, 0x40, 0x41)
        + (0x12, 0x13, 0x22, 0x23, 0x32, 0x33, 0x42, 0x43)
    )
    assert SplitPartsReshape(4, unit=2).unshape(joined, PipelineContext()) == chips


@pytest.mark.parametrize("parts", WORD_PART_COUNTS)
def test_word_interleave_round_trips_including_ragged_tail(parts: int) -> None:
    plugin = SplitPartsReshape(parts, unit=2)
    ctx = PipelineContext()
    group = parts * 2
    for length in (0, 1, 3, group - 1, group, group * 2 + 2, group * 4 + 1):
        data = bytes((i * 37 + 5) & 0xFF for i in range(length))
        assert plugin.unshape(plugin.reshape(data, ctx), ctx) == data
        assert plugin.reshape(plugin.unshape(data, ctx), ctx) == data
        # Whatever doesn't fill a whole word-aligned set of parts is passed
        # through at the end, not folded into a part.
        aligned = (length // group) * group
        assert plugin.reshape(data, ctx)[aligned:] == data[aligned:]


# -- position in the pipeline ------------------------------------------------


def test_reshape_runs_between_container_read_and_decompress(tmp_path) -> None:
    """A compressed structure inside a split region is only contiguous after
    the join, so the file holds ``unshape(packed)`` and only the order
    read -> reshape -> decompress recovers the payload."""
    ctx = PipelineContext()
    payload = b"\x00" * 40 + bytes(range(24)) + b"\xff" * 16
    packed = KonamiNesRle().compress(payload, ctx)
    scattered = SplitPartsReshape(2).unshape(packed, ctx)
    px = tmp_path / "pair.bin"
    px.write_bytes(scattered)
    cfg = PathwayConfig(
        source=FileRef(str(px)),
        interpret_preset_id="preset.pixel.snes-4bpp",
        reshape_id="reshape.split-planes-2",
        compression_id="compression.konami-nes-rle",
    )
    data = pipeline.load_pixel_data(cfg, default_registry())
    assert data.data == payload


def _make_files(tmp_path):
    # 4 SNES 4bpp tiles (32B each) + 16 BGR555 colors, as test_pipeline builds.
    pixel_bytes = bytes((i * 29 + 5) & 0xFF for i in range(32 * 4))
    pal = bytearray((i * 17 + 3) & 0xFF for i in range(2 * 16))
    for off in range(1, len(pal), 2):
        pal[off] &= 0x7F
    px = tmp_path / "gfx.4bpp.sfc"
    pl = tmp_path / "gfx.4bpp.sfc.pal"
    px.write_bytes(pixel_bytes)
    pl.write_bytes(bytes(pal))
    return px, pl, pixel_bytes


def test_reshaped_file_save_round_trips_byte_identical(tmp_path) -> None:
    reg = default_registry()
    px, pl, pixel_bytes = _make_files(tmp_path)
    pixel_cfg = PathwayConfig(
        source=FileRef(str(px)),
        interpret_preset_id="preset.pixel.snes-4bpp",
        reshape_id="reshape.split-planes-2",
    )
    palette_cfg = PathwayConfig(
        source=FileRef(str(pl)), interpret_preset_id="preset.palette.bgr555"
    )
    doc = pipeline.load(pixel_cfg, palette_cfg, reg)
    # The view sees the joined bytes, not the file's order...
    assert doc.pixel_data == SplitPartsReshape(2).reshape(
        pixel_bytes, PipelineContext()
    )
    # ...and an untouched save puts every byte back where it came from.
    pipeline.save(doc, reg)
    assert px.read_bytes() == pixel_bytes


class _StubCompression:
    """Compression scheme whose *packed* size is dictated by the test."""

    def __init__(self, packed: bytes) -> None:
        self.info = PluginInfo(
            id="compression.stub", name="Stub", stage=Stage.COMPRESSION
        )
        self._packed = packed

    def decompress(self, data: bytes, ctx: PipelineContext) -> bytes:
        return data

    def compress(self, data: bytes, ctx: PipelineContext) -> bytes:
        return self._packed


def _save_reshaped_slice_with_stub(tmp_path, packed: bytes):
    """A 64-byte reshaped slice at offset 32 whose compressor emits ``packed``."""
    reg = default_registry()
    px, pl, pixel_bytes = _make_files(tmp_path)
    pixel_cfg = PathwayConfig(
        source=FileRef(str(px), offset=32, length=64),
        dest=FileRef(str(px), offset=32, length=64),
        interpret_preset_id="preset.pixel.snes-4bpp",
        reshape_id="reshape.split-planes-2",
        compression_id="compression.stub",
    )
    palette_cfg = PathwayConfig(
        source=FileRef(str(pl)),
        interpret_preset_id="preset.palette.bgr555",
        write_enabled=False,
    )
    reg.register(_StubCompression(packed))
    doc = pipeline.load(pixel_cfg, palette_cfg, reg)
    return px, pixel_bytes, lambda: pipeline.save(doc, reg)


def test_reshaped_bounded_write_requires_exact_fill(tmp_path) -> None:
    # Short-of-the-slot is fine for a self-delimiting compressed stream, but a
    # reshape's part boundaries are len/N of the region: writing the front of a
    # slot is writing a *different region*, so it must refuse.
    px, pixel_bytes, save = _save_reshaped_slice_with_stub(tmp_path, bytes(10))
    with pytest.raises(PipelineError) as excinfo:
        save()
    assert (excinfo.value.stage, excinfo.value.action) == (Stage.RESHAPE, "unshape")
    assert px.read_bytes() == pixel_bytes  # nothing partial written


def test_reshaped_bounded_write_accepts_exact_fill(tmp_path) -> None:
    px, pixel_bytes, save = _save_reshaped_slice_with_stub(tmp_path, b"\xab" * 64)
    save()
    out = px.read_bytes()
    assert out[32:96] == b"\xab" * 64  # a fill is its own unshape
    assert out[:32] == pixel_bytes[:32] and out[96:] == pixel_bytes[96:]


# -- the bitswap engine (reshapes as data) -----------------------------------


def _ref_bitswap(i: int, bits_msb: list[int]) -> int:
    """MAME's bitswap<N>(i, ...), written independently of the engine."""
    n = len(bits_msb)
    return sum(((i >> src) & 1) << (n - 1 - k) for k, src in enumerate(bits_msb))


def _bitswap(bits: list[int], gather: bool = False) -> BitswapReshape:
    return BitswapReshape("reshape.test", "test", bits, gather)


def test_bitswap_scatters_through_the_table() -> None:
    # 3-bit table: out bit2 <- in bit2, bit1 <- bit0, bit0 <- bit1. Checked
    # byte-for-byte against an independent bitswap so a chunk-plan bug in the
    # engine can't hide behind a matching round trip.
    bits = [2, 0, 1]
    data = bytes((i * 37 + 5) & 0xFF for i in range(8))
    out = _bitswap(bits).reshape(data, PipelineContext())
    assert all(out[_ref_bitswap(i, bits)] == data[i] for i in range(8))


def test_bitswap_round_trips_across_blocks_and_keeps_the_tail() -> None:
    # A 16-byte block over a 3.5-block region: each block permutes on its own
    # (chosen so the chunk plan is exercised: bits 1..0 identity -> 4-byte
    # chunks), and the half-block tail passes through untouched.
    bits = [2, 3, 1, 0]
    plugin = _bitswap(bits)
    ctx = PipelineContext()
    data = bytes((i * 37 + 5) & 0xFF for i in range(16 * 3 + 8))
    out = plugin.reshape(data, ctx)
    assert plugin.unshape(out, ctx) == data
    assert out[48:] == data[48:]
    for base in (0, 16, 32):
        block = data[base : base + 16]
        assert all(out[base + _ref_bitswap(i, bits)] == block[i] for i in range(16))


def test_bitswap_gather_is_the_inverse_direction() -> None:
    # gather = true reads the table as dst[i] = src[bitswap(i)] — the other
    # MAME idiom — so its reshape must equal the scatter plugin's unshape.
    bits = [0, 2, 1, 3]
    ctx = PipelineContext()
    data = bytes(range(16))
    scatter, gather = _bitswap(bits), _bitswap(bits, gather=True)
    assert gather.reshape(data, ctx) == scatter.unshape(data, ctx)
    assert gather.unshape(gather.reshape(data, ctx), ctx) == data


def test_bitswap_rejects_bad_tables_and_undersized_regions() -> None:
    with pytest.raises(ValueError):
        _bitswap([2, 1, 1])  # not a permutation
    with pytest.raises(ValueError):
        _bitswap([])  # no address lines at all
    with pytest.raises(ValueError):
        bitswap_from_spec(
            {
                "id": "reshape.x",
                "name": "x",
                "engine_id": "reshape.wrong",
                "params": {"bits": [0]},
            }
        )
    with pytest.raises(ValueError):
        # A region smaller than one block would reshape nothing at all — a
        # misconfiguration, not a tail.
        _bitswap([3, 2, 1, 0]).reshape(bytes(8), PipelineContext())
    with pytest.raises(ValueError):
        _bitswap(list(range(MAX_BITS + 1)))  # wider than any real table


def test_bitswap_bounds_span_and_moving_lines_separately() -> None:
    """A table may span 24 address lines but move at most 16 of them.

    Span is free; the chunk plan is sized by the *moving* lines, so the two
    bounds are not interchangeable. An ascending msb-first table moves every
    line, which is what separates them.
    """
    assert (MAX_BITS, MAX_PERMUTED_BITS) == (24, 16)
    _bitswap(list(range(MAX_PERMUTED_BITS)))  # moves 16 — the most allowed
    with pytest.raises(ValueError):
        _bitswap(list(range(MAX_PERMUTED_BITS + 1)))  # moves 17

    # PGM's sprite descramble (igs/pgmprot_igs027a_type1.cpp,
    # pgm_decode_kovlsqh2_sprites) is the widest table in the corpus: 24 lines,
    # but its low 9 are fixed, so only 15 move and the plan stays small. It is
    # what the two bounds have to admit together — neither alone would.
    pgm = [23, 10, 9, 22, 19, 18, 20, 21, 17, 16, 15, 14, 13, 12, 11]
    pgm += [8, 7, 6, 5, 4, 3, 2, 1, 0]
    plugin = _bitswap(pgm)
    block = bytes((i * 61 + 7) & 0xFF for i in range(1 << 16)) * (1 << 8)
    out = plugin.reshape(block, PipelineContext())
    for i in (0, 1 << 9, 1 << 22, (1 << 24) - 1):
        assert out[_ref_bitswap(i, pgm)] == block[i]
    assert plugin.unshape(out, PipelineContext()) == block


def test_no_bitswap_table_ships_active() -> None:
    """A table only means anything for the board it came from, so none ships in
    everyone's Reshape picker — the seeded ``_example.toml`` carries the one
    real table, for a user to copy (see tests/test_discovery.py)."""
    reshapes = default_registry().plugins(Stage.RESHAPE)
    assert not [p for p in reshapes if isinstance(p, BitswapReshape)]


# -- the data-LUT engine (value substitutions as data) -----------------------


def _data_lut(**params):
    return data_lut_from_spec(
        {"id": "reshape.test", "name": "test", "engine_id": DATA_LUT_ENGINE,
         "params": params}
    )  # fmt: skip


def _ref_swap(value: int, bits_msb: list[int]) -> int:
    """MAME's bitswap<N>(value, ...), written independently of the engine."""
    n = len(bits_msb)
    return sum(((value >> bits_msb[n - 1 - k]) & 1) << k for k in range(n))


def test_data_lut_substitutes_values_without_moving_bytes() -> None:
    """The defining contrast with the address engine: a byte's value changes and
    its position does not. Thunder Dragon's bootleg swaps data lines 3 and 4."""
    plugin = _data_lut(bitswaps=[[7, 6, 5, 3, 4, 2, 1, 0]])
    ctx = PipelineContext()
    data = bytes(range(256))
    out = plugin.reshape(data, ctx)
    assert out == bytes(_ref_swap(v, [7, 6, 5, 3, 4, 2, 1, 0]) for v in data)
    # Swapping two data lines is an involution, so it is its own inverse — but
    # `unshape` must be the derived inverse regardless.
    assert plugin.unshape(out, ctx) == data


def test_data_lut_selects_its_table_from_address_bits() -> None:
    """Eight tables picked by three region-offset bits — the part that makes
    this a Reshape rather than a codec, since the selector reads offsets far
    outside any one tile.

    Checked against NMK's decode_gfx loop (nmk16.cpp), whose address pick is
    ``((A & 4) >> 2) | ((A & 0x800) >> 10) | ((A & 0x40000) >> 16)``.
    """
    tables = [[(b + t) % 8 for b in range(8)] for t in range(8)]
    plugin = _data_lut(selector_bits=[2, 11, 18], bitswaps=tables)
    ctx = PipelineContext()
    data = bytes((i * 61 + 7) & 0xFF for i in range(1 << 19))
    out = plugin.reshape(data, ctx)
    for i in (0, 4, 0x800, 0x804, 0x40000, 0x40FFF, (1 << 19) - 1):
        pick = ((i & 4) >> 2) | ((i & 0x800) >> 10) | ((i & 0x40000) >> 16)
        assert out[i] == _ref_swap(data[i], tables[pick])
    assert plugin.unshape(out, ctx) == data


def test_data_lut_word_unit_permutes_across_both_bytes() -> None:
    """A 16-bit table moves bits between the two bytes of a word, which no
    byte-wise table can do — the reason `unit` exists at all.

    The engine decomposes it into per-output-byte lookups; a wrong lane pairing
    still round-trips, so the vector is pinned against a reference.
    """
    table = list(range(16))  # msb-first ascending = reverse every word
    plugin = _data_lut(unit=2, bitswaps=[table])
    ctx = PipelineContext()
    data = bytes([0x80, 0x01, 0x12, 0x34])
    out = plugin.reshape(data, ctx)
    assert out == bytes([0x80, 0x01, 0x2C, 0x48])  # bit-reversed 16-bit words
    for base in (0, 2):
        word = int.from_bytes(data[base : base + 2], "big")
        assert int.from_bytes(out[base : base + 2], "big") == _ref_swap(word, table)
    assert plugin.unshape(out, ctx) == data
    # An odd trailing byte cannot form a word, so it passes through — the same
    # degradation rule as the split-plane joins.
    assert plugin.reshape(data + b"\xa5", ctx) == out + b"\xa5"


def test_data_lut_takes_a_raw_table_for_non_permutations() -> None:
    """`luts` is what makes this a substitution engine rather than a bitswap
    one: an XOR mask is a bijection no permutation of data lines can express."""
    plugin = _data_lut(luts=[[v ^ 0xD2 for v in range(256)]])
    ctx = PipelineContext()
    data = bytes((i * 61 + 7) & 0xFF for i in range(512))
    out = plugin.reshape(data, ctx)
    assert out == bytes(v ^ 0xD2 for v in data)
    assert plugin.unshape(out, ctx) == data


def test_data_lut_remap_reorders_the_table_set() -> None:
    """`selector_remap` covers boards putting a lookup between the address bits
    and the table index (NMK's 215 MCU programs one into the 214)."""
    tables = [[(b + t) % 8 for b in range(8)] for t in range(2)]
    ctx = PipelineContext()
    data = bytes(range(256))
    plain = _data_lut(selector_bits=[0], bitswaps=tables).reshape(data, ctx)
    swapped = _data_lut(
        selector_bits=[0], bitswaps=tables, selector_remap=[1, 0]
    ).reshape(data, ctx)
    # Remapping [1, 0] must give exactly what swapping the two tables gives.
    assert swapped == _data_lut(selector_bits=[0], bitswaps=tables[::-1]).reshape(
        data, ctx
    )
    assert swapped != plain


def test_data_lut_rejects_bad_specs() -> None:
    with pytest.raises(ValueError):
        _data_lut(bitswaps=[[7, 6, 5, 3, 3, 2, 1, 0]])  # not a permutation
    with pytest.raises(ValueError):
        _data_lut(luts=[[0] * 256])  # not a bijection — could not write back
    with pytest.raises(ValueError):
        _data_lut(unit=2, luts=[list(range(256))])  # raw tables are byte-only
    with pytest.raises(ValueError):
        _data_lut(bitswaps=[[7, 6, 5, 4, 3, 2, 1, 0]], luts=[list(range(256))])
    with pytest.raises(ValueError):
        # Three selector bits need eight tables, not two.
        _data_lut(selector_bits=[1, 2, 3], bitswaps=[[7, 6, 5, 4, 3, 2, 1, 0]] * 2)
    with pytest.raises(ValueError):
        # Both bytes of a word must resolve to one table; bit 0 is what differs.
        _data_lut(unit=2, selector_bits=[0], bitswaps=[list(range(16))] * 2)
    with pytest.raises(ValueError):
        _data_lut(selector_bits=[MAX_SELECTOR_BIT + 1], bitswaps=[[0]] * 2)


# -- slice-length discovery is off under a reshape ---------------------------


def test_backfill_slice_length_refuses_under_a_reshape() -> None:
    # The discovered extent is measured in *reshaped* space; recording it would
    # re-bound the window and change the permutation itself.
    ctx = PipelineContext()
    ctx.set(KEY_COMPRESSED_SIZE, 40)
    ctx.set(KEY_DECOMPRESS_COMPLETE, True)
    plain = new_slice("rom.bin", "s", 0, None, "compression.konami-nes-rle")
    assert backfill_slice_length(plain, ctx)
    reshaped = new_slice(
        "rom.bin",
        "s",
        0,
        None,
        "compression.konami-nes-rle",
        reshape_id="reshape.split-planes-2",
    )
    assert not backfill_slice_length(reshaped, ctx)
    assert reshaped.slice_length is None
    assert reshaped.reshape_id != NO_RESHAPE
