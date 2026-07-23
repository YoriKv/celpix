"""Workspace collection semantics: dedupe, close cascade, slice configs, dirty."""

from __future__ import annotations

from celpix.core.context import (
    KEY_COMPRESSED_SIZE,
    KEY_DECOMPRESS_COMPLETE,
    PipelineContext,
)
from celpix.core.document import Document
from celpix.core.palette import Palette
from celpix.pipeline import pipeline
from celpix.pipeline.pathway import PathwayConfig
from celpix.plugins.base import FileRef
from celpix.plugins.registry import default_registry
from celpix.project.workspace import (
    Entry,
    EntryKind,
    EntrySession,
    PaletteSource,
    Workspace,
    backfill_slice_length,
    data_missing,
    entry_palette_path,
    export_basename,
    exportable_entries,
    missing_paths,
    palette_source_for,
    pixel_config_for,
    relocate_path,
)


def _session(mode: str = "default") -> EntrySession:
    return EntrySession(
        pixel_preset_id="preset.pixel.snes-4bpp",
        palette_preset_id="preset.palette.bgr555",
        palette_mode=mode,
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


def test_close_cascades_bookmarks_and_repointing_skips_them(tmp_path) -> None:
    ws = Workspace()
    file_a = ws.open_file(str(tmp_path / "a.sfc"))
    file_b = ws.open_file(str(tmp_path / "b.sfc"))
    # A bookmark of B parked *before* B in the flat list. Closing A (which has no
    # children) leaves this bookmark sitting exactly at the removal anchor index,
    # so a naive "take the entry now at that index" would land current on a
    # bookmark — which can never be shown. The repoint must skip it to B.
    bookmark_b = Entry(
        name="mark", kind=EntryKind.BOOKMARK, path=file_b.path, slice_offset=0x40
    )
    ws.insert(bookmark_b, 1)
    assert ws.entries == [file_a, bookmark_b, file_b]
    ws.set_current(file_a)

    removed = ws.close(file_a)
    assert removed == [file_a]  # A's close doesn't drag B's bookmark along
    assert ws.current is file_b  # the bookmark at the anchor index was skipped

    # Closing B cascades to its bookmark, and with nothing showable left current
    # falls to None rather than to the just-removed bookmark.
    removed = ws.close(file_b)
    assert set(removed) == {file_b, bookmark_b}
    assert ws.entries == [] and ws.current is None


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


def test_slice_of_dirty_parent_reads_the_unsaved_bytes_past_a_header(tmp_path) -> None:
    """A slice of an edited parent sees the edits, rebased across the header skip.

    The parent's buffer starts at its header skip, so serving a slice from it is
    an offset rebase — the case that silently reads the wrong region if the base
    is dropped. Write must stay file-absolute regardless.
    """
    reg = default_registry()
    rom = tmp_path / "rom.sfc"
    rom.write_bytes(bytes(512) + bytes(range(256)))  # 512-byte copier header
    ws = Workspace()
    parent = ws.open_file(str(rom))
    sl = ws.add_slice(parent.path, "gfx", 0x220, 0x10)  # absolute file offset

    # Clean parent: the slice reads the file, and reads it correctly.
    cfg = pixel_config_for(sl, "preset.pixel.snes-4bpp", 0, reg, ws)
    assert cfg.source.data is None
    assert pipeline.load_pixel_data(cfg, reg).data == bytes(range(0x20, 0x30))

    # The parent is loaded past its header and edited in memory but not written.
    parent.doc = _fake_doc()
    parent.doc.pixel_config = PathwayConfig(
        source=FileRef(parent.path, offset=512), interpret_preset_id="p"
    )
    parent.doc.pixel_data = bytes(range(256))  # file bytes 512.. as loaded
    parent.doc.replace_bytes(0x20, b"\xaa" * 0x10)  # edit at file offset 0x220
    ws.set_pixel_revision(parent, ws.next_revision())
    assert parent.pixel_dirty

    cfg = pixel_config_for(sl, "preset.pixel.snes-4bpp", 0, reg, ws)
    assert cfg.source.data_base == 512  # rebased onto the parent's window
    assert pipeline.load_pixel_data(cfg, reg).data == b"\xaa" * 0x10
    # Reading from memory must not move where a save lands.
    assert cfg.write_target() == FileRef(parent.path, offset=0x220, length=0x10)

    # Without the workspace the factory can't know about the parent - it reads
    # the file, which is the pre-edit truth rather than a wrong region.
    cfg = pixel_config_for(sl, "preset.pixel.snes-4bpp", 0, reg)
    assert pipeline.load_pixel_data(cfg, reg).data == bytes(range(0x20, 0x30))


def test_invalidate_path_spares_the_saver_and_dirty_siblings(tmp_path) -> None:
    ws = Workspace()
    rom = ws.open_file(str(tmp_path / "rom.sfc"))
    s1 = ws.add_slice(rom.path, "a", 0x0, 0x20)
    s2 = ws.add_slice(rom.path, "b", 0x20, 0x20)
    other = ws.open_file(str(tmp_path / "other.bin"))
    for e in (rom, s1, s2, other):
        e.doc = _fake_doc()
    ws.set_pixel_revision(s2, ws.next_revision())

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


def test_data_missing_tracks_the_entrys_file_or_parent(tmp_path) -> None:
    rom = tmp_path / "rom.sfc"
    rom.write_bytes(b"\x00" * 32)
    ws = Workspace()

    present = ws.open_file(str(rom))
    assert not data_missing(present)
    gone = ws.open_file(str(tmp_path / "gone.sfc"))
    assert data_missing(gone)

    # A slice or bookmark has no file of its own: it reports on its parent's path.
    sl = ws.add_slice(str(rom), "gfx", 0x0, 0x20)
    assert not data_missing(sl)
    bookmark = Entry(
        name="mark", kind=EntryKind.BOOKMARK, path=str(tmp_path / "gone.sfc")
    )
    assert data_missing(bookmark)


def test_entry_palette_path_resolves_across_states(tmp_path) -> None:
    # Unloaded file-mode entry: the external palette lives on pending_palette.
    unloaded = Entry(
        name="a",
        kind=EntryKind.FILE,
        path=str(tmp_path / "rom.sfc"),
        session=_session("file"),
        pending_palette=PaletteSource(path="ext.pal", offset=4),
    )
    assert entry_palette_path(unloaded) == "ext.pal"

    # Degraded entry: missing_palette takes precedence over any pending source.
    degraded = Entry(
        name="b",
        kind=EntryKind.FILE,
        path=str(tmp_path / "rom.sfc"),
        session=_session("file"),
        missing_palette=PaletteSource(path="moved.pal"),
        pending_palette=PaletteSource(path="ignored.pal"),
    )
    assert entry_palette_path(degraded) == "moved.pal"

    # Default and offset modes have no external palette file at all.
    for mode in ("default", "offset"):
        entry = Entry(
            name="c",
            kind=EntryKind.FILE,
            path=str(tmp_path / "rom.sfc"),
            session=_session(mode),
            pending_palette=PaletteSource(path="ignored.pal"),
        )
        assert entry_palette_path(entry) is None


def test_missing_paths_dedupes_shared_rom_and_includes_palette(tmp_path) -> None:
    rom = str(tmp_path / "gone.sfc")  # never created on disk → missing
    pal = str(tmp_path / "gone.pal")  # missing external palette
    ws = Workspace()
    file_entry = ws.open_file(rom)
    file_entry.session = _session("file")
    file_entry.pending_palette = PaletteSource(path=pal)
    ws.add_slice(rom, "gfx", 0x0, 0x20)  # the same missing ROM, once more

    # The shared ROM collapses to a single worklist item; the palette is unioned in.
    assert missing_paths(ws) == [rom, pal]


def test_relocate_path_repoints_shared_rom_and_palette_sources(tmp_path) -> None:
    old = str(tmp_path / "old.sfc")
    new = str(tmp_path / "moved.sfc")
    other = str(tmp_path / "other.sfc")
    ws = Workspace()

    # A file whose degraded palette is read from the same ROM, and a slice of it
    # carrying a pending palette from that ROM too — both source fields must move.
    file_entry = ws.open_file(old)
    file_entry.missing_palette = PaletteSource(path=old, offset=0x100)
    sl = ws.add_slice(old, "gfx", 0x0, 0x20)
    sl.pending_palette = PaletteSource(path=old)
    # An unrelated entry on a different file stays put.
    unrelated = ws.open_file(other)
    unrelated.pending_palette = PaletteSource(path=other)

    touched = relocate_path(ws, old, new)
    assert touched == [file_entry, sl]
    assert file_entry.path == new
    assert file_entry.missing_palette.path == new
    assert sl.path == new
    assert sl.pending_palette.path == new
    # A FILE's display name follows its new on-disk basename (the located file was
    # renamed old.sfc → moved.sfc); the slice keeps the user-given name it was made
    # with, since a slice's name is a label, not a filename.
    assert file_entry.name == "moved.sfc"
    assert sl.name == "gfx"
    # The non-matching entry is untouched — path and palette both unchanged.
    assert unrelated.path == other
    assert unrelated.pending_palette.path == other

    # Re-extensioning counts as a rename too: the FILE name reflects the new suffix,
    # the slice's is still untouched.
    renamed = str(tmp_path / "moved.smc")
    relocate_path(ws, new, renamed)
    assert file_entry.path == renamed
    assert file_entry.name == "moved.smc"
    assert sl.name == "gfx"


def test_palette_source_for_prefers_missing_palette(tmp_path) -> None:
    # A degraded palette keeps its intended source on missing_palette, so save
    # and new-slice seeding carry the reference forward, default palette or not.
    src = PaletteSource(path=str(tmp_path / "moved.pal"), offset=8)
    entry = Entry(
        name="a",
        kind=EntryKind.FILE,
        path=str(tmp_path / "rom.sfc"),
        session=_session("file"),
        missing_palette=src,
    )
    assert palette_source_for(entry) is src


def test_palette_entry_is_not_a_child_of_a_same_path_file(tmp_path) -> None:
    # A palette file registered with the same path as an open FILE (an odd but
    # legal case — e.g. a .pal opened both ways) must stay a top-level entry,
    # never nest under the file, so closing the file leaves it alone.
    ws = Workspace()
    shared = str(tmp_path / "thing.bin")
    rom = ws.open_file(shared)
    pal = Entry(name="thing.bin", kind=EntryKind.PALETTE, path=shared)
    ws.insert(pal, len(ws.entries))
    assert ws.find_palette(shared) is pal
    assert ws.children_of(rom) == []  # the palette is not a child
    assert ws.parent_of(pal) is None
    removed = ws.close(rom)
    assert removed == [rom]  # the palette did not go with it
    assert ws.entries == [pal]


def test_close_repoints_current_past_a_palette_entry(tmp_path) -> None:
    # A palette entry can never be current, so the neighbour search that repoints
    # after a close must skip it exactly as it skips bookmarks.
    ws = Workspace()
    file_a = ws.open_file(str(tmp_path / "a.sfc"))
    pal = Entry(name="p.pal", kind=EntryKind.PALETTE, path=str(tmp_path / "p.pal"))
    ws.insert(pal, len(ws.entries))
    file_b = ws.open_file(str(tmp_path / "b.sfc"))
    assert ws.entries == [file_a, pal, file_b]
    ws.set_current(file_a)

    ws.close(file_a)
    assert ws.current is file_b  # the palette at the anchor index was skipped


def test_dirty_flag_fires_callback_only_on_change(tmp_path) -> None:
    ws = Workspace()
    rom = ws.open_file(str(tmp_path / "rom.sfc"))
    events: list[bool] = []
    ws.on_dirty_changed.append(lambda e: events.append(e.pixel_dirty))

    edit = ws.next_revision()
    ws.set_pixel_revision(rom, edit)
    ws.set_pixel_revision(rom, edit)  # no-op: same revision, still dirty
    assert events == [True]
    assert ws.dirty_entries() == [rom]
    ws.mark_saved(rom)
    assert events == [True, False]
    assert ws.dirty_entries() == []


def test_undo_back_to_the_saved_revision_reports_clean() -> None:
    """The point of revision *tokens*: an undo is only clean if it lands on the
    state that was written, not merely one edit earlier."""
    ws = Workspace()
    rom = ws.open_file("/tmp/rom.sfc")

    clean, first = rom.pixel_revision, ws.next_revision()
    ws.set_pixel_revision(rom, first)
    assert rom.pixel_dirty

    ws.set_pixel_revision(rom, clean)  # undo of the only edit
    assert not rom.pixel_dirty

    ws.set_pixel_revision(rom, first)  # redo, then write
    ws.mark_saved(rom)
    assert not rom.pixel_dirty

    ws.set_pixel_revision(rom, clean)  # undo *past* the save point
    assert rom.pixel_dirty  # disk holds the edit, memory doesn't
    ws.set_pixel_revision(rom, first)
    assert not rom.pixel_dirty  # redo lands back on what was written

    # A different edit is never mistaken for the saved state, even though it
    # sits at the same depth in the history as the one that was written.
    ws.set_pixel_revision(rom, clean)
    ws.set_pixel_revision(rom, ws.next_revision())
    assert rom.pixel_dirty


def test_drop_document_preserves_a_custom_palette() -> None:
    """A custom palette lives only in the document — dropping it must not lose it.

    Saving one entry invalidates the cached documents of its siblings on the
    same file; without capturing the palette source first, an in-memory custom
    palette would silently revert to the generated default on reload.
    """
    from celpix.core.document import Document
    from celpix.core.palette import Palette
    from celpix.pipeline.pathway import PathwayConfig
    from celpix.plugins.base import FileRef

    ws = Workspace()
    entry = ws.open_file("/tmp/rom.sfc")
    colors = [0xFF102030, 0xFF405060]
    entry.doc = Document(
        pixel_data=b"",
        bytes_per_tile=32,
        tile_width=8,
        tile_height=8,
        palette=Palette(colors),
        pixel_config=PathwayConfig(
            source=FileRef("/tmp/rom.sfc"), interpret_preset_id="p"
        ),
        palette_config=PathwayConfig(
            source=FileRef(""), interpret_preset_id="q", write_enabled=False
        ),
    )
    entry.session = EntrySession(
        pixel_preset_id="p", palette_preset_id="q", palette_mode="custom"
    )

    ws.drop_document(entry)

    assert entry.doc is None
    assert entry.pending_palette is not None
    assert entry.pending_palette.colors == colors


def test_palette_dirt_is_tracked_apart_from_data_dirt() -> None:
    ws = Workspace()
    entry = ws.open_file("/tmp/rom.sfc")
    seen: list[Entry] = []
    ws.on_dirty_changed.append(seen.append)

    ws.set_palette_revision(entry, ws.next_revision())
    assert entry.palette_dirty and not entry.pixel_dirty
    assert ws.dirty_entries() == [entry]  # "anything unsaved?" covers both
    assert seen == [entry]

    ws.set_pixel_revision(entry, ws.next_revision())
    assert entry.pixel_dirty and entry.palette_dirty

    ws.mark_saved(entry, pixel=False)  # a palette-only write
    assert entry.pixel_dirty and not entry.palette_dirty
    assert ws.dirty_entries() == [entry]


def test_invalidate_path_keeps_documents_with_unsaved_palette_edits() -> None:
    # Dropping a document with pending palette changes would discard them.
    ws = Workspace()
    a = ws.open_file("/tmp/rom.sfc")
    b = ws.add_slice("/tmp/rom.sfc", "s", 0, 16)
    a.doc = object()  # stand-in: invalidate only clears the reference
    b.doc = object()
    ws.set_palette_revision(a, ws.next_revision())

    ws.invalidate_path("/tmp/rom.sfc", keep=b)

    assert a.doc is not None  # protected by its unsaved palette edits


# -- export selection & naming (docs/design/export.md) ---------------------
def test_exportable_entries_skips_sliced_files_bookmarks_palettes() -> None:
    ws = Workspace()
    plain = ws.open_file("/rom/plain.chr")  # no slices -> exported
    sliced = ws.open_file("/rom/sheet.sfc")  # has slices -> skipped in bulk
    sl1 = ws.add_slice("/rom/sheet.sfc", "hero", 0x100, 0x80)
    sl2 = ws.add_slice("/rom/sheet.sfc", "enemy", 0x200, 0x80)
    mark = Entry(name="mark", kind=EntryKind.BOOKMARK, path="/rom/sheet.sfc")
    ws.entries.append(mark)
    ws.entries.append(Entry(name="pal", kind=EntryKind.PALETTE, path="/rom/pal.pal"))

    result = exportable_entries(ws)

    # The unsliced file and both slices; never the sliced file, bookmark, palette.
    assert result == [plain, sl1, sl2]
    assert sliced not in result


def test_export_basename_prefixes_slices_and_sanitizes() -> None:
    file_entry = Entry(name="Foo Bar.chr", kind=EntryKind.FILE, path="/rom/Foo Bar.chr")
    slice_entry = Entry(name="1000 (800)", kind=EntryKind.SLICE, path="/rom/sheet.sfc")
    # A file keeps its own stem (spaces preserved as an unsafe->'_' would only hit
    # forbidden chars; the space is replaced to stay portable).
    assert export_basename(file_entry) == "Foo_Bar"
    # A slice is prefixed with the parent stem so cross-file slices don't collide,
    # and its punctuation is flattened to underscores (trailing ones trimmed).
    assert export_basename(slice_entry) == "sheet_1000__800"
