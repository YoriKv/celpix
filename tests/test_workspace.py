"""Workspace collection semantics: dedupe, close cascade, slice configs, dirty."""

from __future__ import annotations

import pytest

from celpix.core.capabilities import ContentKind
from celpix.core.context import (
    KEY_COMPRESSED_SIZE,
    KEY_DECOMPRESS_COMPLETE,
    KEY_SOURCE_OFFSET,
    PipelineContext,
)
from celpix.core.document import Document
from celpix.core.errors import PipelineError, Stage
from celpix.core.notices import notices
from celpix.core.palette import Palette
from celpix.pipeline import pipeline
from celpix.pipeline.pathway import PathwayConfig
from celpix.plugins.base import NO_COMPRESSION, RAW_CONTAINER, FileRef
from celpix.plugins.registry import default_registry
from celpix.project.workspace import (
    Entry,
    EntryKind,
    EntrySession,
    PaletteSource,
    SortKey,
    Workspace,
    backfill_slice_length,
    data_missing,
    entry_palette_path,
    export_basename,
    exportable_entries,
    missing_paths,
    new_slice,
    palette_source_for,
    path_is_palette_only,
    pixel_config_for,
    relocate_path,
    reorders_bytes,
    repair_presets,
    retarget_files,
    sorted_entries,
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

    lz = ws.add_slice(rom.path, "lz", 0x200, 0x80, "compression.lz16")
    cfg = pixel_config_for(lz, "preset.pixel.snes-4bpp", reg)
    assert cfg.source == FileRef(rom.path, offset=0x200, length=0x80)
    assert cfg.compression_id == "compression.lz16"
    assert cfg.compression_id == "compression.lz16"
    assert cfg.write_enabled

    # A scheme whose compressor isn't registered loads view-only. No built-in
    # scheme is decompress-only today, so a hypothetical id exercises the
    # fallback: pixel_config_for derives the compressor purely by the
    # decompress.X ↔ compress.X id convention, so an unregistered counterpart
    # (compress.view-only-example) disables write-back.
    rle = ws.add_slice(rom.path, "rle", 0x0, None, "compression.view-only-example")
    cfg = pixel_config_for(rle, "preset.pixel.snes-4bpp", reg)
    assert not cfg.write_enabled
    assert cfg.compression_id == "compression.none"

    # A FILE entry is unbounded and starts at the file: any header skip is the
    # container's to make and to record, not something the config asks for.
    cfg = pixel_config_for(rom, "preset.pixel.snes-4bpp", reg)
    assert cfg.source == FileRef(rom.path)
    assert cfg.write_enabled


def test_slice_of_dirty_parent_reads_the_unsaved_bytes_past_a_header(tmp_path) -> None:
    """A slice of an edited parent sees the edits, rebased across the container's skip.

    The parent's buffer starts past whatever its container skipped, so serving a
    slice from it is an offset rebase — the case that silently reads the wrong
    region if the base is dropped. Write must stay file-absolute regardless.
    """
    reg = default_registry()
    rom = tmp_path / "rom.sfc"
    rom.write_bytes(bytes(512) + bytes(range(256)))  # 512-byte copier header
    ws = Workspace()
    parent = ws.open_file(str(rom))
    sl = ws.add_slice(parent.path, "gfx", 0x220, 0x10)  # absolute file offset

    # Clean parent: the slice reads the file, and reads it correctly.
    cfg = pixel_config_for(sl, "preset.pixel.snes-4bpp", reg, ws)
    assert cfg.source.data is None
    assert pipeline.load_pixel_data(cfg, reg).data == bytes(range(0x20, 0x30))

    # The parent is loaded past its header and edited in memory but not written.
    parent.doc = _fake_doc()
    parent.doc.pixel_config = PathwayConfig(
        source=FileRef(parent.path), interpret_preset_id="p"
    )
    # What Read recorded is the rebase base — the container's own start, which
    # the config never names.
    parent.doc.pixel_ctx.set(KEY_SOURCE_OFFSET, 512)
    parent.doc.pixel_data = bytes(range(256))  # file bytes 512.. as loaded
    parent.doc.replace_bytes(0x20, b"\xaa" * 0x10)  # edit at file offset 0x220
    ws.set_pixel_revision(parent, ws.next_revision())
    assert parent.pixel_dirty

    cfg = pixel_config_for(sl, "preset.pixel.snes-4bpp", reg, ws)
    assert cfg.source.data_base == 512  # rebased onto the parent's window
    assert pipeline.load_pixel_data(cfg, reg).data == b"\xaa" * 0x10
    # Reading from memory must not move where a save lands.
    assert cfg.write_target() == FileRef(parent.path, offset=0x220, length=0x10)

    # Without the workspace the factory can't know about the parent - it reads
    # the file, which is the pre-edit truth rather than a wrong region.
    cfg = pixel_config_for(sl, "preset.pixel.snes-4bpp", reg)
    assert pipeline.load_pixel_data(cfg, reg).data == bytes(range(0x20, 0x30))


def test_slice_of_a_permuting_parent_reads_its_buffer_and_writes_through_it(
    tmp_path,
) -> None:
    """A parent whose container *reorders* bytes makes its offsets positions in
    the deinterleaved ROM, not in the file — so a slice has to read through the
    parent's buffer, closed parent or not, and its write has to go back the same
    way.

    Reading the file instead is silently wrong (a scrambled region that still
    looks like tiles), and depositing there is worse: the splice lands on bytes
    the offset never named. So the slice stays writable but is flagged
    ``writes_through_parent``, and the pipeline refuses to deposit it itself —
    a `save` that quietly wrote it to `dest` is the regression to fear.
    """
    reg = default_registry()
    # .smd: 512-byte header, then a 16 KB block holding all the odd bytes and
    # then all the even ones. Deinterleaved, byte 0 is 0x22 and byte 1 is 0x11.
    body = bytearray(16384)
    body[0], body[8192] = 0x11, 0x22
    smd = tmp_path / "rom.smd"
    smd.write_bytes(bytes(512) + bytes(body))

    ws = Workspace()
    parent = ws.open_file(str(smd))
    parent.container_id = "container.smd"  # what opening it detects by suffix
    assert reorders_bytes(parent, reg)
    sl = ws.add_slice(parent.path, "gfx", 512, 0x10)  # the buffer's first bytes

    # The parent is closed: the region is read fresh rather than falling back to
    # the file, which at 512 holds the odd half rather than the joined bytes.
    assert parent.doc is None
    cfg = pixel_config_for(sl, "preset.pixel.snes-4bpp", reg, ws)
    assert cfg.source.data is not None
    assert cfg.source.data_base == 512
    assert pipeline.load_pixel_data(cfg, reg).data[:2] == b"\x22\x11"
    # Writable, but not by depositing at `dest` — the host has to route it
    # through the parent, and the pipeline says so rather than guessing.
    assert (cfg.write_enabled, cfg.writes_through_parent) == (True, True)
    doc = _fake_doc()
    doc.pixel_config = cfg
    with pytest.raises(PipelineError):
        pipeline.save(doc, reg, palette=False)
    assert smd.read_bytes()[512:514] == bytes(body[:2])  # nothing was written


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
    s = ws.add_slice(rom.path, "lz", 0x100, None, "compression.lz16")

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

    # And the two are told apart by what reads them: the ROM is pixel data, the
    # .pal is only ever a palette, so the prompts can name which is which.
    assert not path_is_palette_only(ws, rom)
    assert path_is_palette_only(ws, pal)
    # A .pal registered as its own row is still a palette file, not pixel data.
    ws.add_palette(pal, "preset.palette.bgr555")
    assert path_is_palette_only(ws, pal)


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

    # A name the user typed over the basename is not a stale one to correct:
    # the next relocate moves the path and leaves the row's name alone.
    file_entry.name = "tileset ROM"
    relocate_path(ws, renamed, str(tmp_path / "elsewhere.sfc"))
    assert file_entry.path == str(tmp_path / "elsewhere.sfc")
    assert file_entry.name == "tileset ROM"


def test_retarget_files_carries_children_and_renames(tmp_path) -> None:
    """A file's new list reaches its slices and bookmarks, keyed off the old path."""
    first = str(tmp_path / "chip1.bin")
    second = str(tmp_path / "chip2.bin")
    ws = Workspace()
    file_entry = ws.open_file(first)
    sl = ws.add_slice(first, "gfx", 0x10, 0x20)
    mark = Entry(name="here", kind=EntryKind.BOOKMARK, path=first, slice_offset=0x40)
    ws.entries.append(mark)
    other = ws.open_file(str(tmp_path / "unrelated.bin"))

    touched = retarget_files(ws, file_entry, (first, second))
    assert touched == [file_entry, sl, mark]
    # A child's offset addresses the parent's *joined* buffer, so it has to be
    # joined the same way — the extras reach every child, not just the file.
    for entry in (file_entry, sl, mark):
        assert entry.paths == (first, second)
    assert other.paths == (str(tmp_path / "unrelated.bin"),)

    # Reordering moves the entry's identity: the children follow onto the new
    # first file (or they would no longer find their parent at all), and the
    # FILE's name follows the basename it now leads with.
    retarget_files(ws, file_entry, (second, first))
    assert file_entry.name == "chip2.bin"
    for entry in (file_entry, sl, mark):
        assert entry.path == second and entry.extra_paths == (first,)
    assert sl.name == "gfx"  # a slice keeps the name the user gave it

    # A slice is never the thing being re-pointed — its list is its parent's.
    assert retarget_files(ws, sl, (first,)) == []
    assert sl.path == second

    # The basename follows the list only while the row is still showing it: a
    # name the user typed survives the list being re-ordered under it.
    file_entry.name = "tileset ROM"
    retarget_files(ws, file_entry, (first, second))
    assert file_entry.path == first and file_entry.name == "tileset ROM"


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


# -- regions spread over several files -------------------------------------


def _region(tmp_path, ws):
    """Two 64-byte "ROM chips" opened as one region, plus their bytes."""
    lo = bytes(range(64))
    hi = bytes(range(64, 128))
    first, second = tmp_path / "lo.bin", tmp_path / "hi.bin"
    first.write_bytes(lo)
    second.write_bytes(hi)
    entry = ws.open_file(str(first), extra_paths=(str(second),))
    return entry, first, second, lo + hi


def test_a_slice_reads_its_parents_whole_file_list(tmp_path) -> None:
    """A slice's offset is into the parent's *joined* region.

    Reading only the file the slice is named after would put every offset past
    the first chip somewhere else entirely — and silently, since the bytes it
    landed on are still plausible graphics.
    """
    reg = default_registry()
    ws = Workspace()
    parent, first, second, joined = _region(tmp_path, ws)

    # A slice straddling the boundary: 32 bytes from each chip.
    sliced = ws.add_slice(parent.path, "gfx", 32, 64)
    assert sliced.paths == (str(first), str(second))

    cfg = pixel_config_for(sliced, "preset.pixel.8bpp-linear", reg, ws)
    assert cfg.source.paths == (str(first), str(second))
    assert pipeline.load_pixel_data(cfg, reg).data == joined[32:96]


def test_a_region_is_missing_when_any_of_its_files_is(tmp_path) -> None:
    # One absent chip does not shorten a region, it moves every byte after the
    # gap — so the region is unloadable, and it is the chip that has to be found.
    ws = Workspace()
    parent, _first, second, _joined = _region(tmp_path, ws)
    assert missing_paths(ws) == []

    second.unlink()
    assert data_missing(parent)
    assert missing_paths(ws) == [str(second)]  # only the one that moved

    moved = tmp_path / "moved.bin"
    moved.write_bytes(bytes(64))
    relocate_path(ws, str(second), str(moved))
    assert parent.paths == (parent.path, str(moved))  # repointed, order kept
    assert missing_paths(ws) == []


def test_saving_a_shared_chip_invalidates_the_entries_reading_it(tmp_path) -> None:
    # Cache invalidation follows every file of a region, not just the one it is
    # named after: an entry that only borrows a chip as its *second* file goes
    # just as stale when something rewrites it.
    ws = Workspace()
    parent, _first, second, _joined = _region(tmp_path, ws)
    other = ws.open_file(str(second))
    other.doc = _fake_doc()

    ws.invalidate_path(str(second), keep=parent)
    assert other.doc is None


def test_reordering_a_file_carries_its_children_with_it(tmp_path) -> None:
    # Reordering a file is a block move: its slices and bookmarks are matched by
    # path rather than position, but the panel can only nest them under a parent
    # that precedes them, so they travel with it. The block lands in front of the
    # named row and the row's *own* children stay behind it, which is what stops
    # the two groups from interleaving.
    ws = Workspace()
    a = ws.open_file(str(tmp_path / "a.sfc"))
    b = ws.open_file(str(tmp_path / "b.sfc"))
    c = ws.open_file(str(tmp_path / "c.sfc"))
    a_slice = ws.add_slice(str(tmp_path / "a.sfc"), "cut", 0, 64)
    b_slice = ws.add_slice(str(tmp_path / "b.sfc"), "cut", 0, 64)
    assert ws.entries == [a, a_slice, b, b_slice, c]

    assert ws.reorder(a, c)  # a goes between b and c
    assert ws.entries == [b, b_slice, a, a_slice, c]
    assert ws.children_of(a) == [a_slice]

    assert ws.reorder(a, b)  # and back, which is the same operation
    assert ws.entries == [a, a_slice, b, b_slice, c]

    assert ws.reorder(a, None)  # last: the group goes to the end of the list
    assert ws.entries == [b, b_slice, c, a, a_slice]

    assert not ws.reorder(a, a_slice)  # a file cannot land inside its own group
    assert not ws.reorder(a, None)  # already there


def test_reordering_a_child_stays_inside_its_parents_group(tmp_path) -> None:
    # "Last" for a slice means after its last sibling, not at the end of the
    # list: a child that drifted past unrelated entries would still be its
    # parent's child but would break the contiguity a file move relies on.
    ws = Workspace()
    rom = ws.open_file(str(tmp_path / "rom.sfc"))
    first = ws.add_slice(str(tmp_path / "rom.sfc"), "first", 0, 64)
    second = ws.add_slice(str(tmp_path / "rom.sfc"), "second", 0x40, 64)
    other = ws.open_file(str(tmp_path / "other.sfc"))
    assert ws.entries == [rom, first, second, other]

    assert ws.reorder(first, None)
    assert ws.entries == [rom, second, first, other]
    assert ws.reorder(first, second)
    assert ws.entries == [rom, first, second, other]


def test_sorting_reads_digit_runs_as_numbers_and_ties_by_name(tmp_path) -> None:
    # Plain string order is wrong for the names this list holds: default slice
    # names lead with a hex offset of whatever width the offset needs, and
    # hand-typed ones number their variants. Both are decided by the digit runs.
    ws = Workspace()
    ws.open_file(str(tmp_path / "rom.sfc"))
    rom = str(tmp_path / "rom.sfc")
    named = [
        ws.add_slice(rom, name, offset, 64)
        for name, offset in (
            ("tile10", 0x200),
            ("0x800", 0x800),
            ("Tile2", 0x200),
            ("0x1000", 0x1000),
        )
    ]
    by_name = sorted_entries(named, SortKey.NAME)
    assert [e.name for e in by_name] == ["0x800", "0x1000", "Tile2", "tile10"]

    # By offset the name breaks the tie, so two slices on one position still land
    # in a readable order rather than whichever the list happened to hold first.
    by_offset = sorted_entries(named, SortKey.OFFSET)
    assert [e.name for e in by_offset] == ["Tile2", "tile10", "0x800", "0x1000"]

    # Equal on both counts: the sort is stable, so a group it cannot tell apart
    # keeps the order the user left it in.
    twins = [ws.add_slice(rom, "same", 0x40, 64) for _ in range(2)]
    assert sorted_entries(twins, SortKey.NAME) == twins
    assert sorted_entries(list(reversed(twins)), SortKey.OFFSET) == twins[::-1]


def test_only_a_pixels_entry_counts_as_a_font_sheet(tmp_path) -> None:
    # A map reads its cells *through* a font and has no tiles of its own to
    # spell, so one carrying the tick — an older project, a hand-edited file —
    # must not be offered as the font another map reads through.
    ws = Workspace()
    rom = str(tmp_path / "rom.sfc")
    ws.open_file(rom)
    sheet = ws.add_slice(rom, "font", 0x100, 64)
    sheet.use_as_font = True
    assert sheet.is_font_sheet

    string = ws.add_slice(rom, "text", 0x200, 64)
    string.content_kind = ContentKind.TILEMAP
    string.use_as_font = True  # stale: the tick is offered on pixels alone
    assert not string.is_font_sheet


def test_sorting_by_type_ranks_the_map_readings_and_leaves_ties_alone(tmp_path) -> None:
    # The picture first, then the three readings of a map, and the palettes last.
    # Nothing breaks a tie on purpose: sorts compose, so a group put in name order
    # and then in type order reads as names within each type — which is the whole
    # of how "fontmaps last, alphabetically" is asked for.
    ws = Workspace()
    rom = str(tmp_path / "rom.sfc")
    ws.open_file(rom)
    layouts = {}

    def carve(name: str, kind: ContentKind, layout: str = "") -> Entry:
        entry = ws.add_slice(rom, name, 0x100, 64)
        entry.content_kind = kind
        layouts[entry] = layout
        return entry

    art = carve("art", ContentKind.PIXELS)
    colors = carve("colors", ContentKind.PALETTE)
    screen = carve("screen", ContentKind.TILEMAP)
    objects = carve("objects", ContentKind.TILEMAP, "sprite")
    words = carve("words", ContentKind.TILEMAP, "text")
    aardvark = carve("aardvark", ContentKind.TILEMAP, "text")

    group = [colors, words, objects, aardvark, screen, art]
    by_type = sorted_entries(group, SortKey.TYPE, layout=lambda e: layouts[e])
    assert by_type == [art, screen, objects, words, aardvark, colors]

    # The two fontmaps kept the order they were handed in above; put the group in
    # name order first and they come out alphabetical, still last.
    by_name = sorted_entries(group, SortKey.NAME)
    then_type = sorted_entries(by_name, SortKey.TYPE, layout=lambda e: layouts[e])
    assert then_type == [art, screen, objects, aardvark, words, colors]

    # No layout to ask: every map is the plain reading of one, which is what an
    # unrecognised format is anyway.
    assert sorted_entries(group, SortKey.TYPE) == [
        art,
        words,
        objects,
        aardvark,
        screen,
        colors,
    ]


def test_a_new_slice_is_seeded_into_offset_order(tmp_path) -> None:
    # The offsets decide where a freshly carved slice *arrives* and nothing more:
    # a list nobody has arranged reads low-to-high, and from then on the order is
    # the user's. Only children of an open file are seeded - anything else, and a
    # child whose parent is closed, goes to the end.
    ws = Workspace()
    rom = ws.open_file(str(tmp_path / "rom.sfc"))
    low = ws.add_slice(str(tmp_path / "rom.sfc"), "low", 0x100, 64)
    high = ws.add_slice(str(tmp_path / "rom.sfc"), "high", 0x900, 64)

    middle = new_slice(str(tmp_path / "rom.sfc"), "middle", 0x500, 64)
    assert ws.add_index_for(middle) == ws.entries.index(high)

    later = new_slice(str(tmp_path / "rom.sfc"), "later", 0xF00, 64)
    assert ws.add_index_for(later) == len(ws.entries)

    # Not re-sorted afterwards: an offset edit leaves the row where it was put.
    low.slice_offset = 0xFFF
    assert ws.entries == [rom, low, high]
    assert ws.add_index_for(ws.open_file(str(tmp_path / "b.sfc"))) == len(ws.entries)


def test_a_missing_plugin_opens_view_only_and_names_itself(tmp_path) -> None:
    """A stored plugin this build hasn't got degrades to the pass-through so the
    file still opens, but the entry is view-only and says which plugin is gone.

    The pair matters together: degrading silently would leave the user with a
    file that looks wrong and a greyed-out Write with no reason given, and
    letting the save through would put untransformed bytes back over the real
    ones — for a compressed slice, raw bytes over the compressed structure.
    Choosing a plugin the registry *does* have clears both, with nothing to
    reset: the config is rebuilt from the entry's id.
    """
    reg = default_registry()
    rom = tmp_path / "rom.bin"
    rom.write_bytes(bytes(256))
    ws = Workspace()
    entry = ws.open_file(str(rom))
    entry.container_id = "container.not-installed"

    cfg = pixel_config_for(entry, "preset.pixel.snes-4bpp", reg)
    assert cfg.container_id == RAW_CONTAINER  # it still opens, as plain bytes
    assert cfg.write_enabled is False
    assert cfg.missing_plugins == ((Stage.CONTAINER, "container.not-installed"),)

    # The load is what reaches the user, so the notice rides the read's context.
    reported = notices(pipeline.load_pixel_data(cfg, reg).ctx)
    assert [n.summary for n in reported] == ["Missing plugin: container.not-installed"]
    assert reported[0].is_warning

    # Same rule on the slice's own stage, where saving through the pass-through
    # would write raw bytes over a compressed structure.
    sliced = ws.add_slice(str(rom), "gfx", 0x10, 0x40, "compression.not-installed")
    slice_cfg = pixel_config_for(sliced, "preset.pixel.snes-4bpp", reg, ws)
    assert slice_cfg.compression_id == NO_COMPRESSION
    assert slice_cfg.write_enabled is False
    assert slice_cfg.missing_plugins == (
        (Stage.COMPRESSION, "compression.not-installed"),
    )

    # Pointing either at a plugin that exists makes the entry whole again.
    entry.container_id = RAW_CONTAINER
    healed = pixel_config_for(entry, "preset.pixel.snes-4bpp", reg)
    assert healed.write_enabled is True
    assert healed.missing_plugins == ()
    assert notices(pipeline.load_pixel_data(healed, reg).ctx) == ()


def test_repair_presets_swaps_missing_formats_and_reports_them(tmp_path) -> None:
    """A project naming a format this build hasn't got has to open, not raise.

    Every surface reads these ids — the codec combo, the transform probes, the
    decode — so they are corrected once, up front, rather than each one guessing.
    """
    rom = tmp_path / "rom.bin"
    rom.write_bytes(b"\x00" * 64)
    entry = Entry(
        name="map",
        kind=EntryKind.FILE,
        path=str(rom),
        content_kind=ContentKind.TILEMAP,
        tilemap_preset_id="preset.tilemap.gone",
        session=EntrySession(
            pixel_preset_id="preset.pixel.gone",
            palette_preset_id="preset.palette.bgr555",
        ),
    )
    replaced = repair_presets([entry], default_registry())

    assert entry.tilemap_preset_id == "preset.tilemap.snes-bg"
    assert entry.session.pixel_preset_id == "preset.pixel.snes-4bpp"
    # The palette one was fine and is left exactly as it was.
    assert entry.session.palette_preset_id == "preset.palette.bgr555"
    assert [(item.stage, item.wanted, item.used) for item in replaced] == [
        (Stage.INTERPRET_PIXEL, "preset.pixel.gone", "preset.pixel.snes-4bpp"),
        (Stage.INTERPRET_TILEMAP, "preset.tilemap.gone", "preset.tilemap.snes-bg"),
    ]
    # A second pass has nothing left to say.
    assert repair_presets([entry], default_registry()) == []
