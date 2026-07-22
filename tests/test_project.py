"""Project file round-trips, path handling, and tolerant loading."""

from __future__ import annotations

import json
from os.path import normcase

from celpix.core.document import Document, ViewOptions
from celpix.core.palette import Palette
from celpix.pipeline.pathway import PathwayConfig
from celpix.plugins.base import FileRef
from celpix.project.projectfile import (
    PROJECT_VERSION,
    ProjectError,
    load_project,
    save_project,
)
from celpix.project.workspace import (
    Entry,
    EntryKind,
    EntrySession,
    PaletteSource,
    Workspace,
)


def _session(**overrides) -> EntrySession:
    values = dict(
        pixel_preset_id="preset.pixel.snes-4bpp",
        palette_preset_id="preset.palette.bgr555",
    )
    values.update(overrides)
    return EntrySession(**values)


def _doc(palette_source: FileRef, view: ViewOptions) -> Document:
    return Document(
        pixel_data=b"\x00" * 64,
        bytes_per_tile=32,
        tile_width=8,
        tile_height=8,
        palette=Palette([0xFF000000]),
        pixel_config=PathwayConfig(source=FileRef("x"), interpret_preset_id="p"),
        palette_config=PathwayConfig(
            source=palette_source, interpret_preset_id="preset.palette.bgr555"
        ),
        view=view,
    )


def test_round_trip_preserves_entries_sessions_and_state(tmp_path) -> None:
    roms = tmp_path / "roms"
    roms.mkdir()
    rom = roms / "smw.sfc"
    rom.write_bytes(b"\x00" * 0x400)
    pal = roms / "smw.pal"
    pal.write_bytes(b"\x00" * 0x20)

    ws = Workspace()
    file_entry = ws.open_file(str(rom))
    file_entry.session = _session(palette_mode="file", headered=True, selected_tile=3)
    file_view = ViewOptions(columns=8, rows=4, zoom=2, show_grid=True, offset=16)
    file_entry.doc = _doc(FileRef(str(pal), offset=4), file_view)

    slice_entry = ws.add_slice(str(rom), "title GFX", 0x100, None, "decompress.lz2")
    slice_entry.session = _session(
        palette_mode="offset", compression_id="decompress.lz1"
    )
    slice_view = ViewOptions(byte_nudge=3, subpalette_row=2)
    slice_entry.doc = _doc(FileRef(str(rom), offset=0x200, length=32), slice_view)
    ws.set_current(slice_entry)

    project = tmp_path / "hack.celpix"
    save_project(ws, str(project))

    # On-disk form: current schema version, relative POSIX paths, current index.
    raw = json.loads(project.read_text(encoding="utf-8"))
    assert raw["version"] == PROJECT_VERSION
    assert raw["current"] == 1
    assert raw["entries"][0]["path"] == "roms/smw.sfc"
    assert raw["entries"][0]["palette"] == {"path": "roms/smw.pal", "offset": 4}
    assert "slice_offset" not in raw["entries"][0]  # file entries carry no slice keys

    loaded = load_project(str(project))
    assert loaded.version == PROJECT_VERSION
    first, second = loaded.entries
    assert loaded.current is second

    assert first.kind is EntryKind.FILE
    assert normcase(first.path) == normcase(str(rom))
    assert first.session == file_entry.session
    assert first.doc is None  # documents stay lazy on load
    assert first.pending_view == file_view
    assert first.pending_palette is not None
    assert normcase(first.pending_palette.path) == normcase(str(pal))
    assert first.pending_palette.offset == 4

    assert second.kind is EntryKind.SLICE
    assert (second.name, second.slice_offset, second.slice_length) == (
        "title GFX",
        0x100,
        None,
    )
    assert second.decompress_id == "decompress.lz2"
    assert second.session == slice_entry.session
    assert second.pending_view == slice_view
    assert second.pending_palette == PaletteSource(offset=0x200)


def test_inline_colors_survive_without_activation(tmp_path) -> None:
    rom = tmp_path / "rom.bin"
    rom.write_bytes(b"\x00" * 32)
    ws = Workspace()
    entry = ws.open_file(str(rom))
    entry.session = _session()
    # A never-activated entry restored from a project keeps its pending state
    # through the next save — nothing may be lost by not clicking it.
    entry.pending_view = ViewOptions(zoom=8)
    entry.pending_palette = PaletteSource(colors=[0xFF000000, 0xFFFFFFFF, 0x80FF00FF])

    project = tmp_path / "p.celpix"
    save_project(ws, str(project))
    raw = json.loads(project.read_text(encoding="utf-8"))
    assert raw["entries"][0]["palette"]["colors"] == [
        "#FF000000",
        "#FFFFFFFF",
        "#80FF00FF",
    ]
    loaded = load_project(str(project))
    assert loaded.entries[0].pending_palette == entry.pending_palette
    assert loaded.entries[0].pending_view == ViewOptions(zoom=8)


def test_case_insensitive_path_resolution(tmp_path) -> None:
    roms = tmp_path / "roms"
    roms.mkdir()
    rom = roms / "rom.sfc"
    rom.write_bytes(b"\x00" * 16)
    project = tmp_path / "p.celpix"
    project.write_text(
        json.dumps({"version": 1, "entries": [{"path": "ROMS/ROM.SFC"}]}),
        encoding="utf-8",
    )
    loaded = load_project(str(project))
    # A project written under a case-insensitive OS finds its file here too.
    assert loaded.entries[0].path == str(rom)


def test_tolerant_load_defaults_unknowns_and_garbage(tmp_path) -> None:
    (tmp_path / "x.bin").write_bytes(b"\x00")
    document = {
        "version": 99,  # newer than this reader — still loads, degraded
        "future_top_level_key": {"ignored": True},
        "current": 0,  # points at the garbage entry below → no current
        "entries": [
            {"kind": "file"},  # no path: skipped, not fatal
            {"path": "x.bin", "unknown_key": 1, "session": {"headered": "yes"}},
            {"path": "gone.bin"},  # missing file: listed anyway, fails at activation
        ],
    }
    project = tmp_path / "p.celpix"
    project.write_text(json.dumps(document), encoding="utf-8")

    loaded = load_project(str(project))
    assert loaded.version == 99
    assert loaded.current is None
    assert [e.name for e in loaded.entries] == ["x.bin", "gone.bin"]
    entry = loaded.entries[0]
    # Missing/odd session fields fall back to workable defaults.
    assert entry.session is not None
    assert entry.session.pixel_preset_id == "preset.pixel.snes-4bpp"
    assert entry.session.header_length == 512
    assert entry.session.headered is True  # truthy string coerces
    assert entry.pending_view is None


def test_unreadable_or_non_project_file_raises(tmp_path) -> None:
    bad = tmp_path / "bad.celpix"
    bad.write_text("not json", encoding="utf-8")
    for path in (bad, tmp_path / "missing.celpix"):
        try:
            load_project(str(path))
        except ProjectError:
            continue
        raise AssertionError(f"expected ProjectError for {path}")
    # Valid JSON that isn't a project document is rejected too.
    bad.write_text("[1, 2]", encoding="utf-8")
    try:
        load_project(str(bad))
    except ProjectError:
        pass
    else:
        raise AssertionError("expected ProjectError for a non-dict document")


def test_replace_swaps_list_and_notifies(tmp_path) -> None:
    ws = Workspace()
    old = ws.open_file(str(tmp_path / "a.bin"))
    ws.set_current(old)
    added: list[Entry] = []
    removed: list[Entry] = []
    ws.on_added.append(added.append)
    ws.on_removed.append(removed.append)

    new = Entry(name="b.bin", kind=EntryKind.FILE, path=str(tmp_path / "b.bin"))
    ws.replace([new], new)
    assert ws.entries == [new]
    assert ws.current is new
    assert removed == [old]
    assert added == [new]
