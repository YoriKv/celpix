"""Composite views: assembling one tile source out of several files and slices.

The model half (assembly order, blank pads, the run lengths a load records) and
the editing half (a stroke landing in the piece that owns it, and surviving an
undo), plus the project round trip. See ``docs/design/composite-entry.md``.
"""

from __future__ import annotations

import json

from celpix.plugins.registry import default_registry
from celpix.project import projectfile
from celpix.project.workspace import (
    CompositePiece,
    EntryKind,
    EntrySession,
    Workspace,
    composite_layout,
    composite_preset_id,
    new_composite,
    pixel_config_for,
)
from celpix.ui.main_window import MainWindow

# One 4bpp tile is 32 bytes, which is the unit every count here is in.
TILE = 32


def _tiles(tmp_path, name: str, tiles: int, fill: int):
    path = tmp_path / name
    path.write_bytes(bytes([fill]) * (TILE * tiles))
    return path


def _session() -> EntrySession:
    return EntrySession("preset.pixel.snes-4bpp", "preset.palette.bgr555")


def _composite_of(ws: Workspace, name: str, *pieces: CompositePiece):
    entry = new_composite(name, pieces)
    ws.entries.append(entry)
    return entry


def test_pieces_are_joined_in_order_with_pads_holding_their_place(tmp_path) -> None:
    """The whole of what a composite is: its sources' bytes end to end, with a
    blank run standing for each hole in the window being reproduced.

    The pad is the half worth asserting. A converted console tilemap indexes the
    tile window the hardware had loaded, and that window has gaps — so a piece
    that contributed nothing would put every tile after it on the wrong index,
    which is a wrong *picture* in every map bound to the composite rather than a
    missing run.
    """
    reg = default_registry()
    ws = Workspace()
    first = ws.open_file(str(_tiles(tmp_path, "a.chr", 4, 0x11)))
    first.session = _session()
    second = ws.open_file(str(_tiles(tmp_path, "b.chr", 2, 0x22)))
    second.session = _session()
    composite = _composite_of(
        ws,
        "window",
        CompositePiece(first),
        CompositePiece(length=TILE * 3),  # the hole
        CompositePiece(second),
    )

    layout = composite_layout(composite, reg, ws)

    assert layout.data == (
        bytes([0x11]) * (TILE * 4) + bytes(TILE * 3) + bytes([0x22]) * (TILE * 2)
    )
    # And the runs are measured, so the entry can answer where each piece sits
    # without joining the sources again.
    assert [p.measured for p in layout.pieces] == [TILE * 4, TILE * 3, TILE * 2]
    # ...and the *requests* are untouched: only the pad ever asked for a length.
    assert [p.length for p in layout.pieces] == [0, TILE * 3, 0]
    assert [(s.start, s.length) for s in layout.spans] == [
        (0, TILE * 4),
        (TILE * 4, TILE * 3),
        (TILE * 7, TILE * 2),
    ]


def test_a_piece_that_ends_mid_tile_is_padded_out_and_says_so(tmp_path) -> None:
    """A composite exists to give a map one predictable index space, so a source that
    does not fill a whole tile is rounded up rather than left to put everything
    after it half a tile out.

    A notice and not a failure: the file is what it is, and the user is the one
    who can decide whether it belongs in this composite.
    """
    reg = default_registry()
    ws = Workspace()
    ragged = tmp_path / "ragged.chr"
    ragged.write_bytes(bytes([0x33]) * (TILE + 8))  # one tile and a quarter
    entry = ws.open_file(str(ragged))
    entry.session = _session()
    after = ws.open_file(str(_tiles(tmp_path, "after.chr", 1, 0x44)))
    after.session = _session()
    composite = _composite_of(ws, "b", CompositePiece(entry), CompositePiece(after))

    layout = composite_layout(composite, reg, ws)

    assert len(layout.data) == TILE * 3
    assert layout.data[TILE + 8 : TILE * 2] == bytes(TILE - 8)
    # The next piece starts on a tile boundary, which is the point of the pad.
    assert layout.data[TILE * 2 :] == bytes([0x44]) * TILE
    assert any("not a whole number" in problem for problem in layout.problems)


def test_a_closed_source_leaves_a_hole_of_the_size_it_had(tmp_path) -> None:
    """Closing a piece must not renumber the composite.

    A piece holds the entry object, so closing it does not stop the file being
    readable off disk — but a piece pointing into a list the entry has left is
    the "not open" state a binding reports rather than quietly going on drawing.
    The run it last measured is what keeps every later piece on its index.
    """
    reg = default_registry()
    ws = Workspace()
    first = ws.open_file(str(_tiles(tmp_path, "a.chr", 4, 0x11)))
    first.session = _session()
    second = ws.open_file(str(_tiles(tmp_path, "b.chr", 2, 0x22)))
    second.session = _session()
    composite = _composite_of(ws, "w", CompositePiece(first), CompositePiece(second))
    composite.pieces = composite_layout(composite, reg, ws).pieces  # record the lengths

    ws.close(first)
    layout = composite_layout(composite, reg, ws)

    assert len(layout.data) == TILE * 6  # unchanged
    assert layout.data[: TILE * 4] == bytes(TILE * 4)  # blank where it was
    assert layout.data[TILE * 4 :] == bytes([0x22]) * (TILE * 2)  # still at tile 4
    assert layout.problems == ("a.chr is not open, so its run is blank",)
    # ...and nothing owns the run, so a stroke there has nowhere to land.
    assert layout.spans[0].owner is None


def test_a_piece_may_take_a_byte_range_of_its_source(tmp_path) -> None:
    """The case carving a slice cannot cover.

    A console DMAs half a decompressed blob to one VRAM address and half of
    another below it. A slice bounds the *compressed stream*; what is wanted is
    part of the **resolved output**, which no offset and length into the file can
    name — so the piece has to be able to say it.

    The window is a statement about the composite's layout, so it keeps its
    length whatever the source turns out to hold, and it is measured in the
    composite's own tiles rather than the source's (sources differ in depth; the
    index space does not).
    """
    reg = default_registry()
    ws = Workspace()
    src = ws.open_file(str(_tiles(tmp_path, "a.chr", 8, 0x11)))
    src.session = _session()
    tail = ws.open_file(str(_tiles(tmp_path, "b.chr", 2, 0x22)))
    tail.session = _session()
    composite = _composite_of(
        ws,
        "w",
        CompositePiece(src, offset=TILE * 6, length=TILE * 2),  # a.chr's last 2 tiles
        CompositePiece(tail),
    )

    layout = composite_layout(composite, reg, ws)

    assert len(layout.data) == TILE * 4
    assert layout.data == bytes([0x11]) * (TILE * 2) + bytes([0x22]) * (TILE * 2)
    assert layout.problems == ()
    # The request survives the measurement, which is what stops a refresh from
    # quietly turning the window into the whole of its source.
    assert layout.pieces[0].length == TILE * 2
    assert layout.pieces[0].offset == TILE * 6
    assert layout.pieces[0].measured == TILE * 2
    # And the span records where the run began inside the owner, which is what a
    # deposit adds to land in the right place.
    assert [(s.start, s.length, s.source_base) for s in layout.spans] == [
        (0, TILE * 2, TILE * 6),
        (TILE * 2, TILE * 2, 0),
    ]


def test_an_edit_through_a_range_lands_at_the_ranges_own_offset(
    qtbot, tmp_path
) -> None:
    """A ranged run starts partway into its source, so a deposit that ignored
    that would land the edit that many bytes early — in art the user was not
    looking at, silently."""
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_tiles(tmp_path, "a.chr", 8, 0x11)))
    src = window._workspace.current
    composite = new_composite(
        "w", (CompositePiece(src, offset=TILE * 6, length=TILE * 2),)
    )
    window._workspace.entries.append(composite)
    window._activate_entry(composite)

    _paint(window, 0, 1)  # the composite's tile 0 is the source's tile 6

    assert src.doc.pixel_data[: TILE * 6] == bytes([0x11]) * (TILE * 6)
    assert src.doc.pixel_data[TILE * 6 : TILE * 7] != bytes([0x11]) * TILE
    assert src.doc.pixel_data[TILE * 7 :] == bytes([0x11]) * TILE
    assert src.pixel_dirty


def test_a_composite_cannot_be_assembled_out_of_another(tmp_path) -> None:
    """A coordinate into a coordinate has no meaning, and a join of a join has no
    end: two composites pointed at each other would each assemble the other.

    Refused in the assembly as well as in the picker that offers the sources, so
    what is offered and what is accepted cannot disagree.
    """
    reg = default_registry()
    ws = Workspace()
    inner = _composite_of(ws, "inner")
    outer = _composite_of(ws, "outer", CompositePiece(inner, measured=TILE * 2))

    layout = composite_layout(outer, reg, ws)

    assert layout.problems == (
        "inner cannot supply bytes to a composite, so its run is blank",
    )
    assert layout.data == bytes(TILE * 2)


def test_a_composite_reads_as_plain_bytes_and_never_writes_itself(tmp_path) -> None:
    """Its pieces have already been through their own container, reshape and
    decompressor, so running a second set over the join would apply a file's
    framing to a buffer that is not that file.

    ``write_enabled`` is False for the reason that matters more: a composite owns
    no bytes, so there is nothing here to deposit — saying so is what stops the
    assembled buffer being written to a file named after the entry.
    """
    reg = default_registry()
    ws = Workspace()
    source = ws.open_file(str(_tiles(tmp_path, "a.chr", 2, 0x55)))
    source.session = _session()
    composite = _composite_of(ws, "w", CompositePiece(source))

    cfg = pixel_config_for(composite, "preset.pixel.snes-2bpp", reg, ws)

    assert cfg.source.data == bytes([0x55]) * (TILE * 2)
    assert cfg.write_enabled is False
    assert cfg.reads_raw_bytes  # no container, no reshape, no compression
    # The format asked for is the format used, even where no source uses it: a
    # tile window is read at the depth its *consumer* wants, and the same bytes
    # are 4bpp to one background layer and 2bpp to another.
    assert cfg.interpret_preset_id == "preset.pixel.snes-2bpp"
    # The first source's format is only where a new composite *starts*.
    assert composite_preset_id(composite, reg) == "preset.pixel.snes-4bpp"


def test_the_format_picker_reads_the_composite_at_the_depth_it_names(
    qtbot, tmp_path
) -> None:
    """The reason a composite's format is its own rather than its sources'.

    A console's tile window is depth-agnostic: the same bytes are 4bpp to one
    background layer and 2bpp to another, and the corpus this was built for reads
    nine of seventeen assembled windows at a depth none of their sources use. So
    the picker has to actually move the composite, and a **map bound to it** has
    to see the bank at the composite's depth — otherwise every cell index in the
    converted tilemap lands on the wrong tile.

    Both halves are one assertion about one bug: the config the switch rebuilds
    through has to carry the requested format into the assembly *and* the decode.
    """
    from celpix.core.tilemap import Cell
    from celpix.project.workspace import TileMode, TileSource
    from uihelpers import _scr_file

    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_tiles(tmp_path, "a.chr", 8, 0x11)))  # a 4bpp sheet
    source = window._workspace.current
    composite = new_composite("w", (CompositePiece(source),))
    window._workspace.entries.append(composite)
    window._activate_entry(composite)
    assert composite.doc.bytes_per_tile == TILE  # its first source's, to start

    at = window._pixel_preset.findData("preset.pixel.snes-2bpp")
    window._pixel_preset.setCurrentIndex(at)  # the real signal, not a snap

    assert window._doc.pixel_config.interpret_preset_id == "preset.pixel.snes-2bpp"
    assert window._doc.bytes_per_tile == TILE // 2
    assert window._doc.tile_count == 16  # the same bytes, twice the tiles
    # The run lengths are bytes, so the switch does not move them.
    assert [piece.measured for piece in composite.pieces] == [TILE * 8]

    window._capture_session()
    window._load_pixel(str(_scr_file(tmp_path, [Cell(index=0), Cell(index=1)])))
    screen = window._workspace.current
    screen.tile_source = TileSource(mode=TileMode.ENTRY, entry=composite)
    window._reload_tilemap(screen)

    assert screen.doc.bytes_per_tile == TILE // 2  # the composite's, not a.chr's


def test_a_composite_round_trips_through_a_project(tmp_path) -> None:
    """Pieces are stored as positions and resolved back to objects, the one place
    the positional form exists — and a position naming nothing degrades to a pad
    of the recorded length rather than to whatever now sits at a stale index."""
    ws = Workspace()
    first = ws.open_file(str(_tiles(tmp_path, "a.chr", 4, 0x11)))
    first.session = _session()
    composite = _composite_of(
        ws,
        "window",
        CompositePiece(first, measured=TILE * 4),
        CompositePiece(length=TILE * 3),
    )
    composite.session = _session()
    ws.set_current(composite)
    path = tmp_path / "p.celpix"
    projectfile.save_project(ws, str(path))

    stored = json.loads(path.read_text())["entries"][1]
    assert "path" not in stored  # nothing to name it by
    assert stored["pieces"] == [
        {"entry_index": 0, "measured": TILE * 4},  # a whole entry: no length
        {"length": TILE * 3},  # a pad states nothing else
    ]

    loaded = projectfile.load_project(str(path))
    restored = loaded.entries[1]
    assert restored.kind is EntryKind.COMPOSITE
    assert [(p.entry, p.extent) for p in restored.pieces] == [
        (loaded.entries[0], TILE * 4),
        (None, TILE * 3),
    ]
    assert loaded.current is restored

    # A stale index is a pad, not a neighbour.
    document = json.loads(path.read_text())
    document["entries"][1]["pieces"][0]["entry_index"] = 99
    path.write_text(json.dumps(document))
    degraded = projectfile.load_project(str(path)).entries[1]
    assert [(p.entry, p.extent) for p in degraded.pieces] == [
        (None, TILE * 4),
        (None, TILE * 3),
    ]


# -- the editing half ------------------------------------------------------
def _window_with_composite(qtbot, tmp_path):
    """A window holding two files and a composite of them, with the composite shown."""
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_tiles(tmp_path, "a.chr", 4, 0x11)))
    first = window._workspace.current
    window._load_pixel(str(_tiles(tmp_path, "b.chr", 4, 0x22)))
    second = window._workspace.current
    composite = new_composite("window", (CompositePiece(first), CompositePiece(second)))
    window._workspace.entries.append(composite)
    window._activate_entry(composite)
    return window, first, second, composite


def _paint(window, first: int, count: int, index: int = 7) -> None:
    """Set one pixel of each of ``count`` tiles from ``first``, as one edit.

    Through the ordinary write path, so what is exercised is what a brush stroke
    exercises. The pixel has to actually *change* — an edit that would write back
    the bytes already there is skipped rather than pushed.
    """
    tiles = window._decode_run(first, count)
    edited = []
    for tile in tiles:
        copy = type(tile)(tile.width, tile.height, bytes(tile.data))
        copy.set(0, 0, index)
        edited.append(copy)
    window._apply_tile_edit(first, edited, "paint")


def test_a_stroke_on_a_composite_lands_in_the_piece_that_owns_it(
    qtbot, tmp_path
) -> None:
    """A composite's buffer is a derived copy of several entries', the way a
    tilemap's is of one — so an edit that stopped there would be lost at the next
    reassembly. Each piece takes its own share, and it is the *piece* that reads
    dirty, because that is the file a write puts the edit on disk through.
    """
    window, first, second, composite = _window_with_composite(qtbot, tmp_path)

    _paint(window, 5, 1)  # tile 5 of the composite is tile 1 of the second file

    assert second.doc.pixel_data[TILE : TILE * 2] != bytes([0x22]) * TILE
    assert second.doc.pixel_data[:TILE] == bytes([0x22]) * TILE  # only that tile
    assert second.pixel_dirty
    assert first.doc.pixel_data == bytes([0x11]) * (TILE * 4)  # untouched
    assert not first.pixel_dirty
    # And File ▸ Write on the composite knows where to send it.
    assert window._unsaved_owners(composite) == [second]


def test_one_stroke_across_two_pieces_is_one_undo(qtbot, tmp_path) -> None:
    """A gesture is one interaction and must stay one Ctrl+Z however many owners
    it turns out to have — which is the whole reason the command carries a list
    of them rather than the single parent a slice edit needed.
    """
    window, first, second, composite = _window_with_composite(qtbot, tmp_path)
    before = window._undo_stack.count()

    # Straddling the boundary: the last tile of `first`, the first of `second`.
    _paint(window, 3, 2)

    assert window._undo_stack.count() == before + 1
    assert first.pixel_dirty and second.pixel_dirty
    assert first.doc.pixel_data[TILE * 3 :] != bytes([0x11]) * TILE
    assert second.doc.pixel_data[:TILE] != bytes([0x22]) * TILE

    window._undo_stack.undo()

    assert first.doc.pixel_data == bytes([0x11]) * (TILE * 4)
    assert second.doc.pixel_data == bytes([0x22]) * (TILE * 4)
    assert not first.pixel_dirty and not second.pixel_dirty


def test_painting_a_blank_run_is_refused(qtbot, tmp_path) -> None:
    """Nothing owns a pad, so an edit there has nowhere to be deposited — and
    keeping it would put pixels on screen that the next reassembly silently takes
    away again. The parts of the gesture that can be honoured still are.
    """
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(_tiles(tmp_path, "a.chr", 2, 0x11)))
    source = window._workspace.current
    composite = new_composite(
        "w", (CompositePiece(source), CompositePiece(length=TILE * 2))
    )
    window._workspace.entries.append(composite)
    window._activate_entry(composite)

    # Tiles 1-2: the second is a pad, and only the first may be written.
    _paint(window, 1, 2)

    assert source.doc.pixel_data[TILE : TILE * 2] != bytes([0x11]) * TILE
    assert source.pixel_dirty
    assert "blank tiles" in window.statusBar().currentMessage()


def test_editing_a_piece_directly_shows_through_the_composite(qtbot, tmp_path) -> None:
    """A composite's buffer is a join, so it cannot be patched with the splices
    that were right for the piece — an offset in a join is not an offset in the
    run it came from. It is dropped and re-assembled instead, which is what makes
    an edit to a source visible in every composite built on it.
    """
    window, first, second, composite = _window_with_composite(qtbot, tmp_path)
    window._activate_entry(first)

    _paint(window, 0, 1)
    painted = first.doc.pixel_data[:TILE]

    window._activate_entry(composite)

    assert composite.doc.pixel_data[:TILE] == painted
    assert composite.doc.pixel_data[TILE * 4 :] == bytes([0x22]) * (TILE * 4)


def test_the_dialog_lays_out_the_index_space_and_refuses_a_composite(
    qtbot, tmp_path
) -> None:
    """The *at tile* column is the VRAM table the user is transcribing, so it has
    to answer for a blank run as well as a source — and the source picker offers
    exactly what the assembly would accept, never a composite.
    """
    from celpix.ui.composite_dialog import CompositeDialog

    window, first, second, composite = _window_with_composite(qtbot, tmp_path)
    dialog = CompositeDialog(
        entry=composite,
        candidates=list(window._workspace.entries),
        tile_bytes=TILE,
        name="window",
        pieces=(
            CompositePiece(first, measured=TILE * 4),
            CompositePiece(length=TILE * 3),
            CompositePiece(second, measured=TILE * 4),
        ),
    )
    qtbot.addWidget(dialog)

    rows = dialog._items()
    # The byte a run starts on, and the same place as a tile index.
    assert [r.text(0) for r in rows] == ["0x000000", "0x000080", "0x0000E0"]
    assert [r.text(1) for r in rows] == ["0", "4", "7"]
    assert [r.text(2) for r in rows] == ["a.chr", "(blank)", "b.chr"]
    assert "11 tiles" in dialog._total.text()
    # The composite itself is not on offer, and neither would another composite be.
    assert [e.name for e in dialog._candidates] == ["a.chr", "b.chr"]

    # Removing the pad closes the gap, which is the whole point of the column.
    dialog._list.setCurrentItem(rows[1])
    dialog._remove_selected()
    assert [r.text(1) for r in dialog._items()] == ["0", "4"]
    assert dialog.pieces() == (
        CompositePiece(first, measured=TILE * 4),
        CompositePiece(second, measured=TILE * 4),
    )


def test_a_map_binds_to_a_composite_and_paints_through_it(qtbot, tmp_path) -> None:
    """The whole point of the feature, end to end.

    A converted console tilemap indexes the tile window the hardware had loaded,
    which no single file holds. Bound to the assembled composite at base 0 it reaches
    tiles from *both* files through one index space — and a stroke made on the
    map is deposited into whichever file the tile really came from, two hops down
    (``docs/design/composite-entry.md`` §3).
    """
    from celpix.core.tilemap import Cell
    from celpix.project.workspace import TileMode, TileSource
    from uihelpers import _scr_file

    window, first, second, composite = _window_with_composite(qtbot, tmp_path)
    # Cell 0 draws tile 1 (the first file); cell 1 draws tile 5 (the second).
    window._load_pixel(str(_scr_file(tmp_path, [Cell(index=1), Cell(index=5)])))
    screen = window._workspace.current
    screen.tile_source = TileSource(mode=TileMode.ENTRY, entry=composite)
    window._reload_tilemap(screen)

    # One index space over two files: the composite is what the map sees.
    assert window._tile_bank_owner(screen) is composite
    assert len(screen.doc.pixel_data) == TILE * 8
    assert screen.doc.pixel_data[TILE : TILE * 2] == bytes([0x11]) * TILE
    assert screen.doc.pixel_data[TILE * 5 : TILE * 6] == bytes([0x22]) * TILE

    window._apply_bank_tile_edit({5: window._decode_run(0, 1)[0]}, "paint")

    # The stroke landed in the *second* file, through the composite, through the map.
    assert second.pixel_dirty
    assert not first.pixel_dirty
    assert second.doc.pixel_data[TILE : TILE * 2] != bytes([0x22]) * TILE
    # And File ▸ Write on the map knows which file to put it in.
    assert window._unsaved_owners(screen) == [second]


def test_a_composite_of_slices_folds_through_to_the_rom(qtbot, tmp_path) -> None:
    """The composing case: a piece that is itself a slice records the fold it owes
    its parent, so a stroke on a composite of slices ends up in the ROM's own buffer by
    the machinery that was already there — and the ROM reads dirty alongside.
    """
    rom = tmp_path / "rom.bin"
    rom.write_bytes(bytes((i * 7) & 0xFF for i in range(0x800)))
    window = MainWindow()
    qtbot.addWidget(window)
    window._load_pixel(str(rom))
    parent = window._workspace.current
    cut = window._workspace.add_slice(parent.path, "gfx", 0x100, TILE * 2)
    composite = new_composite("w", (CompositePiece(cut),))
    window._workspace.entries.append(composite)
    window._activate_entry(composite)

    _paint(window, 0, 1)

    assert cut.pixel_dirty
    assert parent.pixel_dirty  # the file genuinely has unsaved changes
    window._activate_entry(parent)  # settles the fold on the way in
    assert parent.doc.pixel_data[0x100 : 0x100 + TILE] == cut.doc.pixel_data[:TILE]


def test_a_copied_composite_keeps_its_pieces(tmp_path) -> None:
    """A composite's pieces are entries, and an entry is not a value — so a copy
    has to carry the *positions* and rebind them, exactly as a map's binding is.

    Model-layer, because that is where the loss was: the payload wrote the pieces
    and nothing read them back, so Cut and Paste — a gesture the files pane
    offers — emptied the entry with no message.
    """
    ws = Workspace()
    source = ws.open_file(str(_tiles(tmp_path, "a.chr", 4, 0x11)))
    source.session = _session()
    composite = _composite_of(
        ws, "w", CompositePiece(source, measured=TILE * 4), CompositePiece(length=TILE)
    )

    payload = projectfile.entries_payload([composite], ws.entries, "session")
    back = projectfile.entries_from_payload(payload)

    assert len(back) == 1
    assert [p.extent for p in back[0].entry.pieces] == [TILE * 4, TILE]
    # The entry each piece named, as a position into the same snapshot — the join
    # the paste resolves back into objects.
    assert back[0].piece_sources == (ws.entries.index(source), -1)


def test_a_ranged_piece_round_trips_through_a_project(tmp_path) -> None:
    """The project file is the only place a range can be *stated* today, so it is
    the one shape with no UI to catch a mistake in it."""
    ws = Workspace()
    source = ws.open_file(str(_tiles(tmp_path, "a.chr", 8, 0x11)))
    source.session = _session()
    composite = _composite_of(
        ws, "w", CompositePiece(source, offset=TILE * 6, length=TILE * 2)
    )
    composite.session = _session()
    path = tmp_path / "p.celpix"
    projectfile.save_project(ws, str(path))

    stored = json.loads(path.read_text())["entries"][1]["pieces"][0]
    assert stored == {"entry_index": 0, "offset": TILE * 6, "length": TILE * 2}

    piece = projectfile.load_project(str(path)).entries[1].pieces[0]
    assert (piece.offset, piece.length, piece.is_ranged) == (TILE * 6, TILE * 2, True)


def test_painting_a_composite_leaves_the_composite_itself_clean(
    qtbot, tmp_path
) -> None:
    """A composite owns no bytes, so it has no unsaved state of its own and no
    write that could clear one — the dirt belongs to the piece the stroke landed
    in, which is what Write acts on.

    Stamping it anyway left it in every "unsaved changes" prompt with no gesture
    able to satisfy them.
    """
    window, first, second, composite = _window_with_composite(qtbot, tmp_path)

    _paint(window, 0, 1)

    assert first.pixel_dirty
    assert not composite.pixel_dirty
    assert [e.name for e in window._workspace.dirty_entries()] == ["a.chr"]


def test_closing_a_piece_rebuilds_the_composite_on_screen(qtbot, tmp_path) -> None:
    """Closing a source has to reach the composites built on it, and an undo
    putting it back has to reach them again — otherwise the blank run a missing
    piece is supposed to leave never appears, and never goes away.

    Through the close *path*, not the assembly: the drop-and-reload is what that
    path owes, and it is the half no other test drives.
    """
    window, first, second, composite = _window_with_composite(qtbot, tmp_path)
    assert composite.doc.pixel_data[:TILE] == bytes([0x11]) * TILE

    window._remove_entry(first, confirm=False)

    assert composite.doc.pixel_data[: TILE * 4] == bytes(TILE * 4)  # blank, held open
    assert composite.doc.pixel_data[TILE * 4 :] == bytes([0x22]) * (TILE * 4)

    window._undo_stack.undo()

    assert composite.doc.pixel_data[:TILE] == bytes([0x11]) * TILE
