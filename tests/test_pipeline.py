"""End-to-end pipeline: byte-identical round trip + hard-stop failures."""

from __future__ import annotations

import pytest

from celpix.core.context import KEY_SOURCE_FILES, KEY_SOURCE_PATH, PipelineContext
from celpix.core.errors import PipelineError, Stage
from celpix.core.index_grid import IndexGrid
from celpix.pipeline import pipeline
from celpix.pipeline.pathway import PathwayConfig
from celpix.plugins.base import FileRef
from celpix.plugins.registry import default_registry


def _make_files(tmp_path):
    # 4 SNES 4bpp tiles (32B each) of deterministic bytes.
    pixel_bytes = bytes((i * 29 + 5) & 0xFF for i in range(32 * 4))
    # 16 BGR555 colors (32B), unused bit 15 cleared for an exact round trip.
    pal = bytearray((i * 17 + 3) & 0xFF for i in range(2 * 16))
    for off in range(1, len(pal), 2):
        pal[off] &= 0x7F
    px = tmp_path / "gfx.4bpp.sfc"
    pl = tmp_path / "gfx.4bpp.sfc.pal"
    px.write_bytes(pixel_bytes)
    pl.write_bytes(bytes(pal))
    return px, pl, pixel_bytes, bytes(pal)


def _configs(px, pl):
    pixel = PathwayConfig(
        source=FileRef(str(px)), interpret_preset_id="preset.pixel.snes-4bpp"
    )
    palette = PathwayConfig(
        source=FileRef(str(pl)), interpret_preset_id="preset.palette.bgr555"
    )
    return pixel, palette


def test_load_then_save_is_byte_identical(tmp_path) -> None:
    reg = default_registry()
    px, pl, pixel_bytes, pal_bytes = _make_files(tmp_path)
    pixel_cfg, palette_cfg = _configs(px, pl)

    doc = pipeline.load(pixel_cfg, palette_cfg, reg)
    assert doc.tile_count == 4  # 128 bytes / 32 bytes-per-tile, decoded on demand
    assert len(doc.palette) == 16

    pipeline.save(doc, reg)
    assert px.read_bytes() == pixel_bytes
    assert pl.read_bytes() == pal_bytes


def test_decode_window_matches_full_decode(tmp_path) -> None:
    reg = default_registry()
    px, pl, pixel_bytes, _ = _make_files(tmp_path)  # 4 SNES 4bpp tiles
    pixel_cfg, palette_cfg = _configs(px, pl)
    doc = pipeline.load(pixel_cfg, palette_cfg, reg)

    preset = reg.preset(pixel_cfg.interpret_preset_id)
    engine = reg.plugin(Stage.INTERPRET_PIXEL, preset.engine_id)
    all_tiles = engine.decode(pixel_bytes, preset.params, PipelineContext())

    # A windowed decode returns exactly the same tiles as slicing a full decode.
    assert pipeline.decode_window(doc, reg, 1, 2) == all_tiles[1:3]
    # A window running past the end yields only the tiles that exist.
    assert pipeline.decode_window(doc, reg, 3, 5) == all_tiles[3:4]


def test_decode_window_2d_reflows_the_window_before_decode(tmp_path) -> None:
    from celpix.core.arrangement import reflow_2d

    reg = default_registry()
    px, pl, _pixel_bytes, _ = _make_files(tmp_path)  # 4 SNES 4bpp tiles
    pixel_cfg, palette_cfg = _configs(px, pl)
    doc = pipeline.load(pixel_cfg, palette_cfg, reg)
    preset = reg.preset(pixel_cfg.interpret_preset_id)
    engine = reg.plugin(Stage.INTERPRET_PIXEL, preset.engine_id)

    cols = 2  # a 2×2-tile window read as a wide bitmap 2 tiles across
    window = doc.window_bytes(0, cols * 2)
    expected = engine.decode(
        reflow_2d(window, doc.bytes_per_tile, doc.tile_height, cols),
        preset.params,
        PipelineContext(),
    )
    got = pipeline.decode_window(
        doc, reg, 0, cols * 2, columns=cols, two_dimensional=True
    )
    # 2D decode is exactly the codec run over the reflowed window …
    assert got == expected
    # … and a different picture from the 1D walk (proves the flag is applied).
    assert got != pipeline.decode_window(doc, reg, 0, cols * 2)


def test_provenance_recorded(tmp_path) -> None:
    from celpix.core.context import KEY_SOURCE_PATH

    reg = default_registry()
    px, pl, *_ = _make_files(tmp_path)
    pixel_cfg, palette_cfg = _configs(px, pl)
    doc = pipeline.load(pixel_cfg, palette_cfg, reg)
    assert doc.pixel_ctx.get(KEY_SOURCE_PATH) == str(px)


def test_palette_write_optional(tmp_path) -> None:
    reg = default_registry()
    px, pl, _, pal_bytes = _make_files(tmp_path)
    pixel_cfg, palette_cfg = _configs(px, pl)
    palette_cfg.write_enabled = False
    doc = pipeline.load(pixel_cfg, palette_cfg, reg)
    # Corrupt the palette in memory, then save: the file must be untouched.
    doc.palette.colors[0] = 0xFFFFFFFF
    pipeline.save(doc, reg)
    assert pl.read_bytes() == pal_bytes


def test_misaligned_pixel_buffer_pads_the_last_tile(tmp_path) -> None:
    # 1.5 tiles' worth of data: the partial tile counts and decodes zero-padded.
    reg = default_registry()
    px = tmp_path / "odd.4bpp.sfc"
    pixel_bytes = bytes((i * 29 + 5) & 0xFF for i in range(48))
    px.write_bytes(pixel_bytes)
    pl = tmp_path / "p.pal"
    pl.write_bytes(b"\x00" * 32)
    pixel_cfg, palette_cfg = _configs(px, pl)

    doc = pipeline.load(pixel_cfg, palette_cfg, reg)
    assert doc.tile_count == 2
    assert doc.pixel_data == pixel_bytes  # padding is decode-only, never stored

    preset = reg.preset(pixel_cfg.interpret_preset_id)
    engine = reg.plugin(Stage.INTERPRET_PIXEL, preset.engine_id)
    padded = engine.decode(pixel_bytes + bytes(16), preset.params, PipelineContext())
    assert pipeline.decode_window(doc, reg, 0, 2) == padded


def _slice_configs(px, pl, offset, length):
    pixel = PathwayConfig(
        source=FileRef(str(px), offset=offset, length=length),
        interpret_preset_id="preset.pixel.snes-4bpp",
    )
    palette = PathwayConfig(
        source=FileRef(str(pl)), interpret_preset_id="preset.palette.bgr555"
    )
    return pixel, palette


def test_slice_round_trip_touches_only_the_slice(tmp_path) -> None:
    # A bounded source (a slice of the parent) loads just that window and saves
    # back in place: bytes outside [offset, offset+length) stay byte-identical.
    reg = default_registry()
    px, pl, pixel_bytes, _ = _make_files(tmp_path)
    pixel_cfg, palette_cfg = _slice_configs(px, pl, offset=32, length=64)

    doc = pipeline.load(pixel_cfg, palette_cfg, reg)
    assert doc.tile_count == 2
    assert doc.pixel_data == pixel_bytes[32:96]

    pipeline.save(doc, reg)
    assert px.read_bytes() == pixel_bytes


class _StubCompression:
    """Compression scheme whose *packed* size is dictated by the test.

    Both directions on one plugin, as every scheme is: the load side has to work
    for the save side to be reachable at all, so decompress passes through.
    """

    def __init__(self, packed: bytes) -> None:
        from celpix.plugins.base import PluginInfo

        self.info = PluginInfo(
            id="compression.stub", name="Stub", stage=Stage.COMPRESSION
        )
        self._packed = packed

    def decompress(self, data: bytes, ctx: PipelineContext) -> bytes:
        return data

    def compress(self, data: bytes, ctx: PipelineContext) -> bytes:
        return self._packed


def _save_slice_with_stub(tmp_path, packed: bytes):
    """Save a 64-byte slice at offset 32 through a stub compressor emitting
    ``packed``; returns (path, original bytes, save thunk)."""
    reg = default_registry()
    px, pl, pixel_bytes, _ = _make_files(tmp_path)
    pixel_cfg, palette_cfg = _slice_configs(px, pl, offset=32, length=64)
    pixel_cfg.compression_id = "compression.stub"
    palette_cfg.write_enabled = False
    reg.register(_StubCompression(packed))
    doc = pipeline.load(pixel_cfg, palette_cfg, reg)
    return px, pixel_bytes, lambda: pipeline.save(doc, reg)


def test_bounded_write_refuses_oversized_result(tmp_path) -> None:
    px, pixel_bytes, save = _save_slice_with_stub(tmp_path, packed=bytes(65))
    with pytest.raises(PipelineError) as excinfo:
        save()
    # The stage covers both directions, so the sub-label is what says this was a
    # save that failed rather than the load that preceded it.
    assert (excinfo.value.stage, excinfo.value.action) == (Stage.CONTAINER, "write")
    assert "container:write" in str(excinfo.value)
    assert px.read_bytes() == pixel_bytes  # nothing partial written


def test_bounded_write_accepts_exact_fit(tmp_path) -> None:
    px, pixel_bytes, save = _save_slice_with_stub(tmp_path, packed=b"\xab" * 64)
    save()
    out = px.read_bytes()
    assert out[32:96] == b"\xab" * 64
    assert out[:32] == pixel_bytes[:32] and out[96:] == pixel_bytes[96:]


def test_bounded_write_leaves_slot_tail_on_short_result(tmp_path) -> None:
    px, pixel_bytes, save = _save_slice_with_stub(tmp_path, packed=b"\xab" * 10)
    save()
    out = px.read_bytes()
    assert out[32:42] == b"\xab" * 10
    assert out[42:96] == pixel_bytes[42:96]  # stale tail deliberately untouched


def test_pixel_write_optional(tmp_path) -> None:
    reg = default_registry()
    px, pl, pixel_bytes, _ = _make_files(tmp_path)
    pixel_cfg, palette_cfg = _configs(px, pl)
    pixel_cfg.write_enabled = False
    doc = pipeline.load(pixel_cfg, palette_cfg, reg)
    doc.pixel_data = bytes(len(doc.pixel_data))  # zero it; save must not land
    pipeline.save(doc, reg)
    assert px.read_bytes() == pixel_bytes


@pytest.mark.parametrize(
    "preset_id, expected_bpp",
    [
        # Wide/odd-tile codecs fix their geometry intrinsically and carry NO bpp
        # param — reading params["bpp"] used to KeyError. Deriving from the tile
        # geometry is the fix these guard, and their bpp isn't the naive tile
        # width either (e.g. pce-sg is 4bpp over a 16-wide tile).
        ("preset.pixel.pce-sg-4bpp", 4),
        ("preset.pixel.pce-2bpp16", 2),
        ("preset.pixel.1bpp16", 1),
        # Ordinary param-driven codecs: the derived value must equal declared bpp.
        ("preset.pixel.snes-4bpp", 4),
        ("preset.pixel.chunky-8bpp", 8),
        # Direct-color storage: params declare bpp=15 but 16 bits are stored per
        # pixel, so the geometry-derived value pins the "storage bits" semantics.
        ("preset.pixel.dc-rgb555", 16),
    ],
)
def test_pixel_bpp_derived_from_geometry(preset_id, expected_bpp) -> None:
    reg = default_registry()
    assert pipeline.pixel_bpp(preset_id, reg) == expected_bpp


def test_bitmap_params_only_re_cuts_codecs_that_take_a_tile_size() -> None:
    # A bitmap width re-cuts the tile grid so whole tiles span the width - but
    # only where that means anything. Direct-color addresses whole bytes per
    # pixel, so 306 px becomes 6x6 tiles; a planar codec's row *is* eight pixels
    # of bitplane, so it must come back untouched rather than have the view
    # claim a 6-px tile its decode would never produce. Support is probed
    # through tile_size(), so a codec gaining the parameter needs no change here.
    reg = default_registry()
    direct, direct_preset = reg.engine_for("preset.pixel.dc-rgb888-be")
    recut = pipeline.bitmap_params(direct, direct_preset.params, 306)
    assert direct.tile_size(recut) == (6, 6)
    assert direct.bytes_per_tile(recut) == 6 * 6 * 3
    # Untouched when the natural tile already spans the width, and when off.
    assert pipeline.bitmap_params(direct, direct_preset.params, 320) is (
        direct_preset.params
    )
    assert pipeline.bitmap_params(direct, direct_preset.params, 0) is (
        direct_preset.params
    )

    planar, planar_preset = reg.engine_for("preset.pixel.snes-4bpp")
    assert pipeline.bitmap_params(planar, planar_preset.params, 306) is (
        planar_preset.params
    )


def test_pixel_bpp_covers_code_formats() -> None:
    # A code format has empty preset params: any params["bpp"] read would fail.
    # 1 byte over a 2x2 tile = 8 bits / 4 pixels = 2bpp.
    from celpix.plugins import FormatInfo
    from celpix.plugins.formats import adapt_format

    class _Fmt:
        info = FormatInfo(id="format.pixel.t", name="t")

        def decode(self, data, ctx): ...
        def encode(self, tiles, ctx): ...
        def bytes_per_tile(self):
            return 1

        def tile_size(self):
            return (2, 2)

    reg = default_registry()
    engine, preset = adapt_format(_Fmt(), Stage.INTERPRET_PIXEL)
    reg.register(engine)
    reg.register_preset(preset)

    assert pipeline.pixel_bpp("format.pixel.t", reg) == 2


def test_missing_source_file_hard_stops(tmp_path) -> None:
    reg = default_registry()
    pixel_cfg = PathwayConfig(
        source=FileRef(str(tmp_path / "nope.sfc")),
        interpret_preset_id="preset.pixel.snes-4bpp",
    )
    palette_cfg = PathwayConfig(
        source=FileRef(str(tmp_path / "nope.pal")),
        interpret_preset_id="preset.palette.bgr555",
    )
    with pytest.raises(PipelineError) as excinfo:
        pipeline.load(pixel_cfg, palette_cfg, reg)
    assert excinfo.value.stage == Stage.CONTAINER


def test_find_next_structure_locates_reports_and_aborts() -> None:
    """The Qt-free Scan core: walks past undecodable bytes to a real structure,
    reports no-match at end-of-data, and honours an on_tick abort."""
    from celpix.plugins.builtins import lz_command

    plugin = default_registry().plugin(Stage.COMPRESSION, "compression.lz2")
    tiles = bytes((i * 31 + 7) & 0xFF for i in range(32 * 4))
    packed = lz_command.compress(tiles, big_endian_offsets=True)
    # A junk lead-in no scheme accepts (backrefs into nothing), then a structure.
    junk = (b"\x83\xff\xff" * 40)[:120]
    probe_bytes = 512

    hit = pipeline.find_next_structure(
        junk + packed + bytes(64), plugin, probe_bytes, 0
    )
    assert hit.found == len(junk)
    assert not hit.stopped

    miss = pipeline.find_next_structure(junk, plugin, probe_bytes, 0)
    assert miss.found is None
    assert miss.end == len(junk)
    assert not miss.stopped

    ticks: list[int] = []

    def _stop(pos: int) -> bool:
        ticks.append(pos)
        return True  # abort on the first progress tick

    aborted = pipeline.find_next_structure(
        junk + packed, plugin, probe_bytes, 0, progress_every=1, on_tick=_stop
    )
    assert aborted.found is None
    assert aborted.stopped
    assert ticks  # the callback actually ran


def test_quantize_color_reports_what_a_format_can_store() -> None:
    """The color editor's "Stored as" preview: encode+decode through a preset.

    This is the number the user is warned by, so the loss has to be the codec's
    real loss — not an approximation computed alongside it.
    """
    reg = default_registry()

    # BGR555 keeps 5 bits per channel: the low 3 bits are dropped, and the
    # surviving value scales back up by high-bit replication (0xF8 -> 0xFF).
    stored = pipeline.quantize_color(0xFFFFFFFF, "preset.palette.bgr555", reg)
    assert stored == 0xFFFFFFFF  # white survives exactly
    lossy = pipeline.quantize_color(0xFF010203, "preset.palette.bgr555", reg)
    assert lossy == 0xFF000000  # all three channels quantize away to black

    # A color already on the format's grid round-trips unchanged, which is what
    # makes "approximated" a trustworthy signal rather than constant noise.
    assert (
        pipeline.quantize_color(0xFF080808, "preset.palette.bgr555", reg) == 0xFF080808
    )

    # An indexed format has no grid at all — it snaps to its nearest hardware
    # color, so an arbitrary RGB comes back as some *table* entry.
    nes = pipeline.quantize_color(0xFF123456, "preset.palette.nes-indexed", reg)
    assert nes >> 24 == 0xFF
    assert nes != 0xFF123456


def _pal_doc(tmp_path, raw: bytes, preset="preset.palette.bgr555"):
    """A Document whose palette pathway is a writable .pal file of ``raw``."""
    from celpix.core.document import Document

    pal = tmp_path / "p.pal"
    pal.write_bytes(raw)
    reg = default_registry()
    cfg = PathwayConfig(source=FileRef(str(pal)), interpret_preset_id=preset)
    loaded = pipeline.load_palette(cfg, reg)
    doc = Document(
        pixel_data=b"",
        bytes_per_tile=32,
        tile_width=8,
        tile_height=8,
        palette=loaded.palette,
        pixel_config=PathwayConfig(
            source=FileRef(str(tmp_path / "none")),
            interpret_preset_id="preset.pixel.snes-4bpp",
            write_enabled=False,
        ),
        palette_config=cfg,
        palette_ctx=loaded.ctx,
        palette_base_bytes=loaded.data,
    )
    return doc, pal, reg


def test_palette_save_leaves_untouched_entries_byte_identical(tmp_path) -> None:
    """A color codec doesn't round-trip *bytes*, so a save must splice.

    BGR555 ignores bit 15: re-encoding a whole palette to persist one edit
    would clear that bit on every other entry — silent corruption of data the
    user never touched.
    """
    raw = bytes([0x21, 0xC3, 0x45, 0xE6, 0x67, 0x8A, 0x9B, 0xFC])  # bit 15 set in 2
    doc, pal, reg = _pal_doc(tmp_path, raw)

    # Edit entry 1 only.
    doc.palette = doc.palette.with_color(1, 0xFFFFFFFF)
    doc.palette_edits.add(1)
    pipeline.save(doc, reg, pixel=False)

    written = pal.read_bytes()
    assert written[2:4] == b"\xff\x7f"  # entry 1 is the new white
    # Every other entry survived *bit for bit*, high bit included.
    assert written[0:2] == raw[0:2]
    assert written[4:8] == raw[4:8]


def test_palette_save_without_edits_is_a_no_op(tmp_path) -> None:
    # Saving a palette nobody edited must not rewrite it — the round-trip test
    # above shows a full re-encode would change half of all BGR555 values.
    raw = bytes([0x21, 0xC3, 0x45, 0xE6, 0x67, 0x8A, 0x9B, 0xFC])
    doc, pal, reg = _pal_doc(tmp_path, raw)

    pipeline.save(doc, reg, pixel=False)

    assert pal.read_bytes() == raw


def test_indexed_palette_save_preserves_out_of_range_bytes(tmp_path) -> None:
    # An indexed codec has no inverse: a byte past the hardware table decodes to
    # the missing-color sentinel and would encode back as a *different* index.
    raw = bytes([0x01, 0xF0, 0x02, 0xC8])  # 0xF0/0xC8 are past the 64-entry table
    doc, pal, reg = _pal_doc(tmp_path, raw, preset="preset.palette.nes-indexed")

    doc.palette = doc.palette.with_color(0, doc.palette.color(2))
    doc.palette_edits.add(0)
    pipeline.save(doc, reg, pixel=False)

    written = pal.read_bytes()
    assert written[0] == raw[2]  # the edited entry took entry 2's color
    assert written[1:] == raw[1:]  # the junk bytes are untouched


def test_second_palette_save_keeps_the_first_edit(tmp_path) -> None:
    # After a write the document must re-baseline on what it wrote; otherwise
    # the next splice runs against pre-save bytes and reverts the earlier edit.
    raw = bytes([0x21, 0xC3, 0x45, 0xE6, 0x67, 0x8A, 0x9B, 0xFC])
    doc, pal, reg = _pal_doc(tmp_path, raw)

    doc.palette = doc.palette.with_color(0, 0xFFFFFFFF)
    doc.palette_edits.add(0)
    pipeline.save(doc, reg, pixel=False)
    first = pal.read_bytes()

    doc.palette = doc.palette.with_color(3, 0xFFFFFFFF)
    doc.palette_edits.add(3)
    pipeline.save(doc, reg, pixel=False)

    written = pal.read_bytes()
    assert written[0:2] == first[0:2] == b"\xff\x7f"  # edit 1 survived edit 2
    assert written[6:8] == b"\xff\x7f"
    assert written[2:6] == raw[2:6]


def test_save_can_skip_the_pixel_pathway(tmp_path) -> None:
    # A palette-only write must not touch the graphic (pipeline-level guarantee
    # behind the window's palette-only Write).
    px = tmp_path / "g.bin"
    px.write_bytes(b"\xaa" * 64)
    doc, pal, reg = _pal_doc(tmp_path, bytes(8))
    doc.pixel_data = b"\x00" * 64
    doc.pixel_config = PathwayConfig(
        source=FileRef(str(px)), interpret_preset_id="preset.pixel.snes-4bpp"
    )

    pipeline.save(doc, reg, pixel=False)
    assert px.read_bytes() == b"\xaa" * 64  # untouched

    pipeline.save(doc, reg)
    assert px.read_bytes() == b"\x00" * 64  # written when asked


def test_decode_and_compose_sizes_to_all_tiles_when_uncapped() -> None:
    # The export path: max_rows=None must lay out *every* tile (the whole file),
    # unlike the live view which caps to its window height.
    from celpix.core.arrangement import BlockLayout

    reg = default_registry()
    engine, preset = reg.engine_for("preset.pixel.snes-4bpp")  # 8x8 tiles, 32B each
    five_tiles = bytes((i * 7) & 0xFF for i in range(32 * 5))
    layout = BlockLayout(2)  # 2 columns -> 5 tiles need 3 rows

    grid, filled = pipeline.decode_and_compose(
        five_tiles, engine, preset.params, layout, two_dimensional=False, max_rows=None
    )
    assert filled == 5
    assert (grid.width, grid.height) == (16, 24)  # 2*8 wide, 3*8 tall (all 5 tiles)

    # The live-view cap still applies when max_rows is set: only 2 rows compose.
    capped, _ = pipeline.decode_and_compose(
        five_tiles, engine, preset.params, layout, two_dimensional=False, max_rows=2
    )
    assert (capped.width, capped.height) == (16, 16)


def test_encode_tiles_replaces_only_the_run_it_covers(tmp_path) -> None:
    # The paste primitive: tiles 1-2 of a 4-tile file are rewritten and every
    # other byte stays exactly as loaded.
    reg = default_registry()
    px, pl, pixel_bytes, _ = _make_files(tmp_path)
    doc = pipeline.load(*_configs(px, pl), reg)

    blank = [IndexGrid(8, 8) for _ in range(2)]
    start, data = pipeline.encode_tiles(doc, reg, 1, blank)
    assert (start, len(data)) == (32, 64)
    doc.replace_bytes(start, data)
    assert doc.pixel_data[:32] == pixel_bytes[:32]
    assert doc.pixel_data[32:96] == bytes(64)
    assert doc.pixel_data[96:] == pixel_bytes[96:]


def test_encode_tiles_round_trips_decoded_tiles(tmp_path) -> None:
    # Decode a run and write it straight back: the codec is a faithful round
    # trip for its own output, so copy→paste onto itself must be a no-op.
    reg = default_registry()
    px, pl, pixel_bytes, _ = _make_files(tmp_path)
    doc = pipeline.load(*_configs(px, pl), reg)

    tiles = pipeline.decode_tiles(doc, reg, 1, 2)
    start, data = pipeline.encode_tiles(doc, reg, 1, tiles)
    assert data == pixel_bytes[start : start + len(data)]


def test_encode_tiles_is_clipped_at_the_end_of_the_data(tmp_path) -> None:
    # Editing never grows a file: a run overrunning the data writes only what fits.
    reg = default_registry()
    px, pl, pixel_bytes, _ = _make_files(tmp_path)  # 4 tiles
    doc = pipeline.load(*_configs(px, pl), reg)

    blanks = [IndexGrid(8, 8) for _ in range(3)]
    start, data = pipeline.encode_tiles(doc, reg, 3, blanks)
    assert (start, len(data)) == (96, 32)  # only the last tile's worth fits
    doc.replace_bytes(start, data)
    assert len(doc.pixel_data) == len(pixel_bytes)


def test_2d_edit_scatters_one_tile_and_spares_its_stripe_mates(tmp_path) -> None:
    # Under the wide-bitmap walk a tile's bytes are strided across the whole
    # bitmap-row, so writing one tile must scatter it back exactly — and leave
    # every other tile in that row byte-identical.
    reg = default_registry()
    px, pl, pixel_bytes, _ = _make_files(tmp_path)  # 4 SNES 4bpp tiles
    doc = pipeline.load(*_configs(px, pl), reg)
    cols = 2  # a bitmap 2 tiles wide: tiles 0+1 share a stripe, 2+3 the next

    kwargs = {"columns": cols, "two_dimensional": True}
    before = pipeline.decode_tiles(doc, reg, 0, 4, **kwargs)
    start, data = pipeline.encode_tiles(doc, reg, 1, [IndexGrid(8, 8)], **kwargs)
    # The region widens to the whole stripe, but only tile 1's bytes change.
    assert (start, len(data)) == (0, 64)
    doc.replace_bytes(start, data)

    after = pipeline.decode_tiles(doc, reg, 0, 4, **kwargs)
    assert after[1] == IndexGrid(8, 8)  # the tile we wrote
    assert [after[i] for i in (0, 2, 3)] == [before[i] for i in (0, 2, 3)]
    # Untouched stripes aren't rewritten at all.
    assert doc.pixel_data[64:] == pixel_bytes[64:]


def test_2d_decode_tiles_matches_the_view_it_was_copied_from(tmp_path) -> None:
    # decode_tiles reads an arbitrary run in the *view's* stripe frame, so a
    # selection starting mid-stripe decodes to what the canvas actually shows.
    reg = default_registry()
    px, pl, _, _ = _make_files(tmp_path)
    doc = pipeline.load(*_configs(px, pl), reg)
    cols = 2

    window = pipeline.decode_window(doc, reg, 0, 4, columns=cols, two_dimensional=True)
    run = pipeline.decode_tiles(
        doc, reg, 1, 2, columns=cols, two_dimensional=True, anchor=0
    )
    assert run == window[1:3]


def test_pixel_is_direct_color_distinguishes_the_codecs() -> None:
    reg = default_registry()
    assert not pipeline.pixel_is_direct_color("preset.pixel.snes-4bpp", reg)
    assert pipeline.pixel_is_direct_color("preset.pixel.dc-argb8888", reg)


# -- several files as one region -------------------------------------------


def _chip_files(tmp_path, sizes: tuple[int, ...]) -> tuple[list, list[bytes]]:
    """One file per size, each filled with a distinguishable byte pattern."""
    paths, blobs = [], []
    for i, size in enumerate(sizes):
        blob = bytes(((n * 7) + i * 101) & 0xFF for n in range(size))
        path = tmp_path / f"chip{i}.bin"
        path.write_bytes(blob)
        paths.append(path)
        blobs.append(blob)
    return paths, blobs


def _joined_configs(tmp_path, paths):
    """A pixel pathway over the joined files, plus an inert palette beside it."""
    pal = tmp_path / "colors.pal"
    pal.write_bytes(bytes(32))
    pixel = PathwayConfig(
        source=FileRef(tuple(str(p) for p in paths)),
        interpret_preset_id="preset.pixel.chunky-8bpp",
    )
    palette = PathwayConfig(
        source=FileRef(str(pal)),
        interpret_preset_id="preset.palette.bgr555",
        write_enabled=False,
    )
    return pixel, palette


def test_several_files_load_as_one_joined_region(tmp_path) -> None:
    """An arcade region spread over its board's ROM chips is one document.

    The container is handed the concatenation and is never told there was more
    than one file — which is what lets every container already written work on a
    joined region unchanged.
    """
    paths, blobs = _chip_files(tmp_path, (64, 64, 32))
    reg = default_registry()

    pixel_cfg, _palette_cfg = _joined_configs(tmp_path, paths)
    data = pipeline.load_pixel_data(pixel_cfg, reg)
    assert data.data == b"".join(blobs)

    # The pieces are on the context for a container that wants them: which file
    # supplied which range of the buffer, in order.
    files = data.ctx.get(KEY_SOURCE_FILES)
    assert [(f.start, f.length) for f in files] == [(0, 64), (64, 64), (128, 32)]
    assert [f.path for f in files] == [str(p) for p in paths]
    # Provenance still names one file — the region's identity, and the first.
    assert data.ctx.get(KEY_SOURCE_PATH) == str(paths[0])


def test_an_edit_writes_back_to_the_file_that_owns_those_bytes(tmp_path) -> None:
    """The joined buffer is cut apart again at the boundaries the files have, so
    an edit lands in whichever chip actually holds it — and only that one."""
    paths, blobs = _chip_files(tmp_path, (64, 64, 32))
    reg = default_registry()
    pixel_cfg, palette_cfg = _joined_configs(tmp_path, paths)

    doc = pipeline.load(pixel_cfg, palette_cfg, reg)
    # A byte inside the *second* file (buffer offset 64..127).
    doc.pixel_data = doc.pixel_data[:70] + b"\xaa" + doc.pixel_data[71:]
    mtimes = [p.stat().st_mtime_ns for p in paths]
    pipeline.save(doc, reg, pixel=True, palette=False)

    assert paths[0].read_bytes() == blobs[0]  # untouched
    assert paths[2].read_bytes() == blobs[2]  # untouched
    assert paths[1].read_bytes() == blobs[1][:6] + b"\xaa" + blobs[1][7:]
    # ...and the two that did not change were not rewritten at all.
    assert [p.stat().st_mtime_ns for p in (paths[0], paths[2])] == [
        mtimes[0],
        mtimes[2],
    ]


def test_a_resized_result_is_refused_rather_than_split_wrong(tmp_path) -> None:
    """File boundaries are the only thing saying which bytes belong to which
    chip, so a result that changed length has moved every boundary after it.

    Refusing is the safe answer — splitting anyway would write most of the region
    into the wrong files, and the ROM the bytes came off cannot change size.
    """
    paths, blobs = _chip_files(tmp_path, (64, 64))
    reg = default_registry()
    pixel_cfg, palette_cfg = _joined_configs(tmp_path, paths)
    pixel_cfg.compression_id = "compression.stub"
    reg.register(_StubCompression(b"\x00" * 100))  # 100 != the 128 on disk

    doc = pipeline.load(pixel_cfg, palette_cfg, reg)
    with pytest.raises(PipelineError) as excinfo:
        pipeline.save(doc, reg, pixel=True, palette=False)
    assert (excinfo.value.stage, excinfo.value.action) == (Stage.CONTAINER, "write")
    assert "file boundaries" in str(excinfo.value)
    # Nothing partial written: both files are exactly as they were.
    assert [p.read_bytes() for p in paths] == blobs


def test_one_file_in_the_list_behaves_exactly_as_before(tmp_path) -> None:
    # The single-file case is a list of one and keeps every freedom it had —
    # including growing the file, which a fresh palette export relies on.
    paths, blobs = _chip_files(tmp_path, (64,))
    reg = default_registry()
    pixel_cfg, palette_cfg = _joined_configs(tmp_path, paths)

    doc = pipeline.load(pixel_cfg, palette_cfg, reg)
    assert doc.pixel_data == blobs[0]
    doc.pixel_data = blobs[0] + b"\xff" * 32  # longer than the file on disk
    pipeline.save(doc, reg, pixel=True, palette=False)
    assert paths[0].read_bytes() == blobs[0] + b"\xff" * 32
