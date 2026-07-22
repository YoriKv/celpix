"""Workspace collection semantics: dedupe, close cascade, slice configs, dirty."""

from __future__ import annotations

from celpix.core.context import (
    KEY_COMPRESSED_SIZE,
    KEY_DECOMPRESS_COMPLETE,
    PipelineContext,
)
from celpix.core.document import Document
from celpix.core.palette import Palette
from celpix.pipeline.pathway import PathwayConfig
from celpix.plugins.base import FileRef
from celpix.plugins.registry import default_registry
from celpix.project.workspace import (
    EntryKind,
    Workspace,
    backfill_slice_length,
    pixel_config_for,
)


def _fake_doc() -> Document:
    cfg = PathwayConfig(source=FileRef("x"), interpret_preset_id="p")
    return Document(
        pixel_data=b"\x00" * 32,
        bytes_per_tile=32,
        tile_width=8,
        tile_height=8,
        palette=Palette([0xFF000000]),
        pixel_config=cfg,
        palette_config=cfg,
    )


def test_open_file_dedupes_by_normalized_path(tmp_path) -> None:
    ws = Workspace()
    p = tmp_path / "rom.sfc"
    first = ws.open_file(str(p))
    # Same file through a relative-ish spelling: still the same entry.
    again = ws.open_file(str(tmp_path / "." / "rom.sfc"))
    assert again is first
    assert len(ws.entries) == 1
    # Slices never dedupe — two marks on the same coordinates coexist.
    a = ws.add_slice(str(p), "a", 0x100, 0x40)
    b = ws.add_slice(str(p), "a", 0x100, 0x40)
    assert a is not b


def test_close_parent_cascades_to_slices_and_repoints_current(tmp_path) -> None:
    ws = Workspace()
    rom = ws.open_file(str(tmp_path / "rom.sfc"))
    other = ws.open_file(str(tmp_path / "other.bin"))
    s1 = ws.add_slice(rom.path, "gfx", 0x100, 0x40)
    ws.set_current(s1)

    removed = ws.close(rom)
    assert set(removed) == {rom, s1}
    assert ws.entries == [other]
    assert ws.current is other  # neighbour, not None

    ws.close(other)
    assert ws.current is None and ws.entries == []


def test_pixel_config_for_slice_bounds_source_and_derives_compressor(tmp_path) -> None:
    reg = default_registry()
    ws = Workspace()
    rom = ws.open_file(str(tmp_path / "rom.sfc"))

    lz = ws.add_slice(rom.path, "lz", 0x200, 0x80, "decompress.lz16")
    cfg = pixel_config_for(lz, "preset.pixel.snes-4bpp", 0, reg)
    assert cfg.source == FileRef(rom.path, offset=0x200, length=0x80)
    assert cfg.decompress_id == "decompress.lz16"
    assert cfg.compress_id == "compress.lz16"
    assert cfg.write_enabled

    # A scheme whose compressor isn't registered loads view-only. No built-in
    # scheme is decompress-only today, so a hypothetical id exercises the
    # fallback: pixel_config_for derives the compressor purely by the
    # decompress.X ↔ compress.X id convention, so an unregistered counterpart
    # (compress.view-only-example) disables write-back.
    rle = ws.add_slice(rom.path, "rle", 0x0, None, "decompress.view-only-example")
    cfg = pixel_config_for(rle, "preset.pixel.snes-4bpp", 0, reg)
    assert not cfg.write_enabled
    assert cfg.compress_id == "compress.none"

    # A FILE entry keeps today's behaviour: header offset, unbounded.
    cfg = pixel_config_for(rom, "preset.pixel.snes-4bpp", 512, reg)
    assert cfg.source == FileRef(rom.path, offset=512)
    assert cfg.write_enabled


def test_invalidate_path_spares_the_saver_and_dirty_siblings(tmp_path) -> None:
    ws = Workspace()
    rom = ws.open_file(str(tmp_path / "rom.sfc"))
    s1 = ws.add_slice(rom.path, "a", 0x0, 0x20)
    s2 = ws.add_slice(rom.path, "b", 0x20, 0x20)
    other = ws.open_file(str(tmp_path / "other.bin"))
    for e in (rom, s1, s2, other):
        e.doc = _fake_doc()
    ws.set_dirty(s2)

    ws.invalidate_path(rom.path, keep=s1)  # s1 just saved into rom.sfc
    assert s1.doc is not None  # the saver keeps its cache
    assert rom.doc is None  # clean same-path entries reload lazily
    assert s2.doc is not None  # dirty: dropping it would lose changes
    assert other.doc is not None  # unrelated path untouched


def test_backfill_slice_length_requires_a_complete_decompress(tmp_path) -> None:
    ws = Workspace()
    rom = ws.open_file(str(tmp_path / "rom.sfc"))
    s = ws.add_slice(rom.path, "lz", 0x100, None, "decompress.lz16")

    partial = PipelineContext()
    partial.set(KEY_COMPRESSED_SIZE, 0x40)  # extent of a *truncated* decode
    assert not backfill_slice_length(s, partial)
    assert s.slice_length is None

    complete = PipelineContext()
    complete.set(KEY_COMPRESSED_SIZE, 0x40)
    complete.set(KEY_DECOMPRESS_COMPLETE, True)
    assert backfill_slice_length(s, complete)
    assert s.slice_length == 0x40
    assert not backfill_slice_length(s, complete)  # already bounded: no-op


def test_dirty_flag_fires_callback_only_on_change(tmp_path) -> None:
    ws = Workspace()
    rom = ws.open_file(str(tmp_path / "rom.sfc"))
    events: list[bool] = []
    ws.on_dirty_changed.append(lambda e: events.append(e.dirty))

    ws.set_dirty(rom)
    ws.set_dirty(rom)  # no-op: already dirty
    assert events == [True]
    assert ws.dirty_entries() == [rom]
    ws.set_dirty(rom, False)
    assert events == [True, False]
    assert ws.dirty_entries() == []
    assert rom.kind is EntryKind.FILE
