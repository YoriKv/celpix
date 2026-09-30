"""Writing an entry: what a save leaves behind for the other entries on the
same file (``docs/design/palette-editing.md`` §2)."""

from __future__ import annotations

from celpix.project.workspace import EntryKind, PaletteMode, slice_of
from celpix.ui.main_window import MainWindow
from celpix.ui.undo_commands import AddEntryCommand

BGR555 = "preset.palette.bgr555"
#: One BGR555 colour is two bytes.
COLOR = 2


def test_writing_a_slice_of_a_palette_keeps_the_mirrored_palette_live(
    qtbot, tmp_path
) -> None:
    """A write into a palette file drops its document as stale, but a graphic
    renders that document's colours by reference and the dock edits them there:
    it is decoded again at once, so the palette stays writable."""
    pal = tmp_path / "colors.pal"
    pal.write_bytes(b"".join((0x1234 + n).to_bytes(2, "little") for n in range(32)))
    rom = tmp_path / "rom.bin"
    rom.write_bytes(bytes(range(256)) * 4)
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(rom))
    graphic = window._workspace.current
    window._add_palette_file(str(pal), preset_id=BGR555)
    palette = next(e for e in window._workspace.entries if e.kind is EntryKind.PALETTE)
    window._use_palette_entry(palette)
    assert window._palette_mode is PaletteMode.FILE
    colors = list(graphic.doc.palette.colors)

    run = slice_of(palette, "upper row", 16 * COLOR, 16 * COLOR)
    window._seed_slice_from_parent(run)
    window._push_command(AddEntryCommand(window, run, "new slice"))
    window._activate_entry(run)
    assert window._write_entry(run)

    assert palette.doc is not None
    assert graphic.doc.palette is palette.doc.palette  # mirrored afresh
    assert list(graphic.doc.palette.colors) == colors
    # The dock's edits reach the palette entry, not the graphic's mirror.
    window._activate_entry(graphic)
    assert window._palette_doc() is palette.doc


# -- nested slices (docs/design/slices-and-parents.md) ----------------------
#: The packed stream's slot: room past the stream for an edit to grow into.
SLOT = 0x180


def _packed(qtbot, tmp_path, payload: bytes):
    """A window over a ROM holding ``payload`` LZ2-packed at 0x100, the packed
    stream carved as a slice. Returns (window, rom path, file, packed, image)."""
    from celpix.plugins.builtins.lz_command import compress

    stream = compress(payload, big_endian_offsets=True)
    image = bytearray(bytes([0x11]) * 0x800)
    image[0x100 : 0x100 + len(stream)] = stream
    rom = tmp_path / "rom.bin"
    rom.write_bytes(bytes(image))
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(rom))
    file = window._workspace.current
    packed = window._workspace.add_slice(
        file.path, "packed", 0x100, SLOT, compression_id="compression.lz2"
    )
    return window, rom, file, packed, bytes(image)


def _paint(window, first: int, color: int = 7) -> None:
    """One pixel of tile ``first`` in the entry on screen, as one undo step."""
    tiles = window._decode_run(first, 1)
    copy = type(tiles[0])(tiles[0].width, tiles[0].height, bytes(tiles[0].data))
    copy.set(3, 3, color)
    window._apply_tile_edit(first, [copy], "paint")


def _unpacked(rom, image: bytes) -> bytes:
    """The payload the ROM's slot now unpacks to — after checking nothing
    outside the slot moved."""
    from celpix.plugins.builtins.lz_command import decompress

    written = rom.read_bytes()
    assert written[:0x100] == image[:0x100]
    assert written[0x100 + SLOT :] == image[0x100 + SLOT :]
    return decompress(written[0x100 : 0x100 + SLOT], big_endian_offsets=True)[0]


def test_an_edit_through_a_nested_slice_is_written_into_the_packed_stream(
    qtbot, tmp_path
) -> None:
    """A tile painted in a slice of a decompressed stream, and a cell set in a map
    beside it in the same stream, reach the file as a re-packed stream that
    unpacks to exactly the edited bytes — and nothing outside the slot moves."""
    from dataclasses import replace

    from celpix.core.capabilities import ContentKind
    from celpix.core.context import PipelineContext
    from celpix.core.tilemap import Cell
    from celpix.plugins.builtins.tilemap_codec import TilemapCodec
    from celpix.plugins.registry import default_registry
    from celpix.project.workspace import TileMode, TileSource

    params = default_registry().preset("preset.tilemap.snes-bg").params
    cells = TilemapCodec().encode(
        [Cell(index=at % 4) for at in range(16)], params, PipelineContext()
    )
    art = bytes(32 * 4)
    window, rom, file, packed, image = _packed(qtbot, tmp_path, art + cells)
    ws = window._workspace
    tiles = ws.add_slice_under(packed, "tiles", 0, len(art))
    screen = slice_of(packed, "map", len(art), len(cells))
    screen.content_kind = ContentKind.TILEMAP
    screen.tile_source = TileSource(mode=TileMode.ENTRY, entry=tiles)
    ws.insert(screen, ws.add_index_for(screen))
    assert ws.children_of(packed) == [tiles, screen]

    window._activate_entry(tiles)
    _paint(window, 2)
    painted = bytes(tiles.doc.pixel_data)
    assert painted != art
    window._activate_entry(screen)
    edited = list(screen.doc.cells)
    edited[5] = replace(edited[5], index=3)
    window._apply_cells(edited, "set cell reference")
    assert window._write_entry(screen)

    unpacked = _unpacked(rom, image)
    assert unpacked[: len(art)] == painted
    assert unpacked[len(art) :] == screen.doc.tilemap_data != cells
    for entry in (tiles, screen, packed, file):
        assert not entry.pixel_dirty, entry.name


def test_a_nested_edit_is_unsaved_up_the_chain_until_undone_or_written(
    qtbot, tmp_path
) -> None:
    """An edit two levels down makes the slice, its parent slice and the file all
    read unsaved; undo clears all three and takes the edit back out of the file's
    buffer; writing any one of them writes and cleans every one."""
    payload = bytes((i * 5) & 0xFF for i in range(0x100))
    window, rom, file, packed, image = _packed(qtbot, tmp_path, payload)
    ws = window._workspace
    tiles = ws.add_slice_under(packed, "tiles", 0x40, 0x80)
    deep = ws.add_slice_under(tiles, "deep", 0x20, 0x40)
    chain = (deep, tiles, packed, file)

    window._activate_entry(deep)
    _paint(window, 0)
    assert all(e.pixel_dirty for e in chain)
    assert deep in tiles.pending_folds and tiles in packed.pending_folds
    assert packed in file.pending_folds

    window._undo_stack.undo()
    assert not any(e.pixel_dirty for e in chain)
    window._activate_entry(file)  # settles the chain: the undone bytes go back in
    assert _unpacked_buffer(file) == payload

    for writer in (deep, packed, file):
        window._activate_entry(deep)
        _paint(window, 1, color=len(writer.name))
        assert all(e.pixel_dirty for e in chain)
        painted = bytes(deep.doc.pixel_data)
        if writer.doc is None:
            window._load_entry(writer)
        assert window._write_entry(writer)
        assert not any(e.pixel_dirty for e in chain), writer.name
        assert _unpacked(rom, image)[0x60:0xA0] == painted


def _unpacked_buffer(file) -> bytes:
    """The payload the file's *buffer* holds at the slot, packed or not."""
    from celpix.plugins.builtins.lz_command import decompress

    slot = bytes(file.doc.pixel_data[0x100 : 0x100 + SLOT])
    return decompress(slot, big_endian_offsets=True)[0]


def test_editing_a_parent_re_derives_the_slices_nested_under_it(
    qtbot, tmp_path
) -> None:
    """An edit to a slice drops the documents nested under it, which re-read the
    new bytes; an edit to the file does the same two levels down."""
    payload = bytes(0x100)
    window, _rom, file, packed, _image = _packed(qtbot, tmp_path, payload)
    ws = window._workspace
    tiles = ws.add_slice_under(packed, "tiles", 0x00, 0x80)
    deep = ws.add_slice_under(tiles, "deep", 0x20, 0x20)
    window._activate_entry(deep)
    assert bytes(deep.doc.pixel_data) == bytes(0x20)

    window._activate_entry(tiles)
    _paint(window, 1)  # tile 1 is bytes 0x20..0x3F: exactly deep's window
    assert deep.doc is None
    window._activate_entry(deep)
    assert bytes(deep.doc.pixel_data) == bytes(tiles.doc.pixel_data[0x20:0x40])
    assert bytes(deep.doc.pixel_data) != bytes(0x20)

    # The file's own edit: every packed byte it changes re-derives both levels.
    window._activate_entry(file)
    window._apply_pixel_bytes(
        # A new stream over the old: 0x80 bytes of 0xAA, then the end.
        [(0x100, b"\xe4\x7f\xaa\xff")],
        ws.next_revision(),
        entry=file,
    )
    assert tiles.doc is None and deep.doc is None
    window._activate_entry(deep)
    assert bytes(deep.doc.pixel_data) == b"\xaa" * 0x20


def test_two_nested_siblings_go_out_in_one_write(qtbot, tmp_path) -> None:
    """Two slices of one decompressed stream, both edited: one write of either
    carries both, since the stream is one region with one authority."""
    payload = bytes(0x100)
    window, rom, _file, packed, image = _packed(qtbot, tmp_path, payload)
    ws = window._workspace
    first = ws.add_slice_under(packed, "first", 0x00, 0x40)
    second = ws.add_slice_under(packed, "second", 0x80, 0x40)
    window._activate_entry(first)
    _paint(window, 0)
    window._activate_entry(second)
    _paint(window, 1)
    first_bytes, second_bytes = (
        bytes(first.doc.pixel_data),
        bytes(second.doc.pixel_data),
    )

    assert window._write_entry(second)
    unpacked = _unpacked(rom, image)
    assert unpacked[0x00:0x40] == first_bytes
    assert unpacked[0x80:0xC0] == second_bytes
    assert not first.pixel_dirty and not second.pixel_dirty


def test_a_nested_edit_that_outgrows_the_packed_slot_stays_dirty_and_is_said(
    qtbot, tmp_path, captured_alerts
) -> None:
    """The fold of the parent slice into the file is the link that refuses: the
    re-packed stream no longer fits its slot. The parent slice, and the slice
    whose edit it carries, stay dirty and loaded; the file is written without
    them and the write says so; the ROM's stream is untouched."""
    from celpix.plugins.builtins.lz_command import compress

    payload = bytes(0x100)
    stream = compress(payload, big_endian_offsets=True)
    window, rom, file, _packed_slot, _image = _packed(qtbot, tmp_path, payload)
    ws = window._workspace
    tight = ws.add_slice(
        file.path, "tight", 0x100, len(stream), compression_id="compression.lz2"
    )
    tiles = ws.add_slice_under(tight, "tiles", 0x00, 0x80)
    before = rom.read_bytes()

    window._activate_entry(tiles)
    _paint(window, 0)  # one pixel is more than a fill command can say
    assert window._write_entry(file)

    assert tight.fold_refused is not None and "exceeds" in tight.fold_refused
    assert tiles.fold_refused is None  # its own link landed, in tight's buffer
    for entry in (tight, tiles):
        assert entry.pixel_dirty and entry.doc is not None, entry.name
    assert not file.pixel_dirty
    alert = captured_alerts[-1][1]
    assert "tight" in alert and "tiles" in alert and "still unsaved" in alert
    assert rom.read_bytes() == before


def test_a_nested_edit_held_in_a_slice_that_will_not_open_stays_dirty_and_is_said(
    qtbot, tmp_path, monkeypatch, captured_alerts
) -> None:
    """The parent slice has no document and will not open, so the nested edit
    has no buffer to ride up to the file in. The parent slice is recorded as
    refusing; it and the nested slice stay dirty, and the nested one keeps its
    document through the file's write and the file's own edit; the write says
    so and leaves the ROM untouched. Once it opens, the edit goes out."""
    from celpix.core.errors import Pathway, PipelineError, Stage

    window, rom, file, packed, image = _packed(qtbot, tmp_path, bytes(0x100))
    ws = window._workspace
    tiles = ws.add_slice_under(packed, "tiles", 0x00, 0x80)
    window._activate_entry(tiles)
    _paint(window, 0)
    ws.drop_document(packed)
    read = window._read_entry

    def refusing(entry, **kwargs):
        if entry is not packed:
            return read(entry, **kwargs)
        error = PipelineError(Stage.COMPRESSION, Pathway.PIXEL, "boom", "read")
        return window._fail_load(entry, error, quiet=True)

    monkeypatch.setattr(window, "_read_entry", refusing)
    before = rom.read_bytes()
    assert window._write_entry(file)

    for entry in (packed, tiles):
        assert entry.pixel_dirty, entry.name
    assert tiles.doc is not None
    assert "could not be opened" in (packed.fold_refused or "")
    alert = captured_alerts[-1][1]
    assert "packed" in alert and "tiles" in alert and "still unsaved" in alert
    assert rom.read_bytes() == before
    window._apply_pixel_bytes([(0, b"\x22")], ws.next_revision(), entry=file)
    assert tiles.doc is not None

    # Once it opens, the next write carries the edit up and lifts the refusal.
    monkeypatch.setattr(window, "_read_entry", read)
    painted = bytes(tiles.doc.pixel_data)
    assert window._write_entry(file)
    assert packed.fold_refused is None
    assert not any(e.pixel_dirty for e in (tiles, packed, file))
    assert _unpacked(rom, b"\x22" + image[1:])[:0x80] == painted


def test_a_nested_slices_own_stages_run_over_its_parents_output(
    qtbot, tmp_path
) -> None:
    """The second pass a loader runs after unpacking is a nested slice's own
    stage: a difference filter as its compression, a running sum as its reshape.
    Each reads the parent's decoded bytes through it, and an edit goes back
    through both — filtered or differenced, then re-packed by the parent."""
    from celpix.plugins.builtins import gba_diff
    from celpix.plugins.builtins.running_sum import difference_units, sum_units

    art = bytes((at * 5) & 0xFF for at in range(32 * 4))
    filtered = gba_diff.compress(art)
    inner = filtered + difference_units(art, 1)
    window, rom, _file, packed, image = _packed(qtbot, tmp_path, inner)
    ws = window._workspace
    behind_filter = ws.add_slice_under(
        packed, "filtered", 0, len(filtered), "compression.gba-diff8"
    )
    summed = ws.add_slice_under(
        packed, "summed", len(filtered), len(art), reshape_id="reshape.running-sum"
    )

    # Kept as painted: a sibling's write drops the document it was painted in.
    painted = {}
    for entry in (behind_filter, summed):
        window._activate_entry(entry)
        assert bytes(entry.doc.pixel_data) == art, entry.name
        _paint(window, 1)
        painted[entry.name] = bytes(entry.doc.pixel_data)
        assert painted[entry.name] != art
        assert window._write_entry(entry), entry.name

    unpacked = _unpacked(rom, image)
    head, tail = unpacked[: len(filtered)], unpacked[len(filtered) :]
    assert gba_diff.decompress(head)[0] == painted["filtered"]
    assert sum_units(tail, 1) == painted["summed"]
