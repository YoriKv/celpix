"""Project file round-trips, path handling, and tolerant loading."""

from __future__ import annotations

import json
from os.path import normcase, samefile

from celpix.core.capabilities import ContentKind
from celpix.core.document import Document, ViewOptions
from celpix.core.font import HOLE, Glyph, GlyphRole, sequential
from celpix.core.palette import Palette
from celpix.core.paletteregions import PaletteRegions
from celpix.pipeline.pathway import DEFAULT_SLOT_FILL, PathwayConfig, SlotFill
from celpix.plugins.base import RAW_CONTAINER, FileRef
from celpix.project.projectfile import (
    PROJECT_VERSION,
    ProjectError,
    load_project,
    project_dict,
    save_project,
)
from celpix.project.workspace import (
    Entry,
    EntryKind,
    EntrySession,
    PaletteSource,
    TileMode,
    TileSource,
    Workspace,
    slice_of,
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
    file_entry.session = _session(palette_mode="file", selected_tile=3)
    file_view = ViewOptions(columns=8, rows=4, tile_offset=16)
    file_entry.doc = _doc(FileRef(str(pal), offset=4), file_view)

    slice_entry = ws.add_slice(str(rom), "title GFX", 0x100, None, "compression.lz2")
    slice_entry.session = _session(
        palette_mode="offset", preview_compression_id="compression.lz1"
    )
    # Exercise the arrangement fields (block grouping / interleave / 2D) so the
    # round-trip assertion below covers their persistence.
    slice_view = ViewOptions(
        byte_nudge=3,
        subpalette_row=2,
        block_columns=2,
        block_rows=2,
        block_order="column",
        two_dimensional=True,
    )
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
    # The tile selection is session-only state and is deliberately not stored.
    assert "selected_tile" not in raw["entries"][0]["session"]

    loaded = load_project(str(project))
    assert loaded.version == PROJECT_VERSION
    first, second = loaded.entries
    assert loaded.current is second

    assert first.kind is EntryKind.FILE
    assert normcase(first.path) == normcase(str(rom))
    assert first.session == _session(palette_mode="file")  # no selection
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
    assert second.compression_id == "compression.lz2"
    assert second.session == slice_entry.session
    assert second.pending_view == slice_view
    assert second.pending_palette == PaletteSource(offset=0x200)


def test_bookmark_round_trips_and_current_index_at_bookmark_degrades(tmp_path) -> None:
    rom = tmp_path / "rom.sfc"
    rom.write_bytes(b"\x00" * 0x400)

    ws = Workspace()
    file_entry = ws.open_file(str(rom))
    file_entry.session = _session()
    # A bookmark's restore trio (session/pending_view/pending_palette) is its
    # permanent snapshot, never consumed — it must survive a save/load intact.
    bookmark = Entry(
        name="title mark",
        kind=EntryKind.BOOKMARK,
        path=str(rom),
        slice_offset=0x140,
        session=_session(palette_mode="offset", selected_tile=5),
        pending_view=ViewOptions(columns=8, rows=4),
        pending_palette=PaletteSource(offset=0x140),
    )
    ws.insert(bookmark, len(ws.entries))
    ws.set_current(file_entry)

    project = tmp_path / "hack.celpix"
    save_project(ws, str(project))

    # On disk a bookmark carries "kind": "bookmark" and an "offset" key — not the
    # slice's "slice_offset"/"slice_length" — plus the ordinary sub-dicts.
    raw = json.loads(project.read_text(encoding="utf-8"))
    stored = raw["entries"][1]
    assert stored["kind"] == "bookmark"
    assert stored["offset"] == 0x140
    assert "slice_offset" not in stored and "slice_length" not in stored
    assert stored["palette"] == {"offset": 0x140}

    loaded = load_project(str(project))
    _, restored = loaded.entries
    assert restored.kind is EntryKind.BOOKMARK
    assert restored.slice_offset == 0x140  # "offset" reloads into slice_offset
    # Everything but the selection, which no entry kind persists.
    assert restored.session == _session(palette_mode="offset")
    assert restored.pending_view == bookmark.pending_view
    assert restored.pending_palette == PaletteSource(offset=0x140)

    # A hand-edited (or v1-degraded) current index naming a bookmark can't be
    # shown, so it loads as no-current rather than trying to activate one.
    raw["current"] = 1
    project.write_text(json.dumps(raw), encoding="utf-8")
    assert load_project(str(project)).current is None


def test_palette_entry_round_trips_with_its_import_codec(tmp_path) -> None:
    rom = tmp_path / "rom.sfc"
    rom.write_bytes(b"\x00" * 0x400)
    pal = tmp_path / "colors.pal"
    pal.write_bytes(b"\x00" * 0x20)

    ws = Workspace()
    file_entry = ws.open_file(str(rom))
    file_entry.session = _session()
    palette = Entry(
        name="colors.pal",
        kind=EntryKind.PALETTE,
        path=str(pal),
        container_id="container.scgcad-col",
        palette_preset_id="preset.palette.rgb888",
    )
    ws.insert(palette, len(ws.entries))
    ws.set_current(file_entry)

    project = tmp_path / "hack.celpix"
    save_project(ws, str(project))

    # On disk a palette entry carries "kind": "palette" and its import codec,
    # but none of the session/view/palette sub-dicts a file or slice has.
    raw = json.loads(project.read_text(encoding="utf-8"))
    assert raw["version"] == PROJECT_VERSION
    stored = raw["entries"][1]
    assert stored["kind"] == "palette"
    assert stored["palette_preset_id"] == "preset.palette.rgb888"
    # A palette's container rides along like a file's: its colors may stop before
    # its bytes do, and reopening at plain bytes would silently add junk rows.
    assert stored["container_id"] == "container.scgcad-col"
    assert "session" not in stored and "view" not in stored
    assert "slice_offset" not in stored

    loaded = load_project(str(project))
    _, restored = loaded.entries
    assert restored.kind is EntryKind.PALETTE
    assert restored.palette_preset_id == "preset.palette.rgb888"
    assert restored.container_id == "container.scgcad-col"
    assert normcase(restored.path) == normcase(str(pal))

    # A hand-edited current index naming a palette can't be shown, so it loads
    # as no-current rather than trying to activate one.
    raw["current"] = 1
    project.write_text(json.dumps(raw), encoding="utf-8")
    assert load_project(str(project)).current is None


def test_inline_colors_survive_without_activation(tmp_path) -> None:
    rom = tmp_path / "rom.bin"
    rom.write_bytes(b"\x00" * 32)
    ws = Workspace()
    entry = ws.open_file(str(rom))
    entry.session = _session()
    # A never-activated entry restored from a project keeps its pending state
    # through the next save — nothing may be lost by not clicking it.
    entry.pending_view = ViewOptions(columns=8)
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
    assert loaded.entries[0].pending_view == ViewOptions(columns=8)


def test_a_project_written_before_a_rename_still_opens_and_is_rewritten(
    tmp_path,
) -> None:
    """A plugin id is a compatibility surface: a project names the formats each
    entry was opened with, so renaming one without a forwarding address resets
    those entries to pass-through, which reads as data loss.

    Loading forwards them, so the workspace holds current ids and the *next save*
    writes them - a project touched after an upgrade stops depending on the
    alias table, and one that is never re-saved keeps opening through it.
    """
    rom = tmp_path / "rom.bin"
    rom.write_bytes(b"\x00" * 64)
    project = tmp_path / "old.celpix"
    # Hand-written the way v0.4.3 would have saved it, old ids throughout.
    project.write_text(
        json.dumps(
            {
                "version": PROJECT_VERSION,
                "current": 0,
                "entries": [
                    {
                        "kind": "file",
                        "name": "rom.bin",
                        "path": "rom.bin",
                        "session": {
                            "pixel_preset_id": "preset.pixel.chunky-8bpp",
                            "palette_preset_id": "preset.palette.r4g4b4",
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_project(str(project))
    session = loaded.entries[0].session
    assert session.pixel_preset_id == "preset.pixel.8bpp-linear"
    assert session.palette_preset_id == "preset.palette.bgr444"

    # ...and re-saving writes the new names, so the file stops needing the table.
    ws = Workspace()
    ws.replace(loaded.entries, loaded.current)
    save_project(ws, str(project))
    raw = json.loads(project.read_text(encoding="utf-8"))
    written = json.dumps(raw)
    assert "chunky-8bpp" not in written and "r4g4b4" not in written
    assert "preset.pixel.8bpp-linear" in written

    # An id from a plugin this build simply hasn't got is left alone rather than
    # reset: it may be one the user has yet to install, and rewriting it to a
    # default turns "your plugin is missing" into "your setting is gone".
    project.write_text(
        json.dumps(
            {
                "version": PROJECT_VERSION,
                "current": 0,
                "entries": [
                    {
                        "kind": "file",
                        "name": "rom.bin",
                        "path": "rom.bin",
                        "reshape_id": "reshape.somebody-elses",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    assert load_project(str(project)).entries[0].reshape_id == "reshape.somebody-elses"


def test_pixel_format_filter_round_trips(tmp_path) -> None:
    rom = tmp_path / "rom.bin"
    rom.write_bytes(b"\x00" * 32)
    ws = Workspace()
    ws.open_file(str(rom))
    ws.hidden_pixel_presets = {"preset.pixel.nes-2bpp", "preset.pixel.gb-2bpp"}

    project = tmp_path / "p.celpix"
    save_project(ws, str(project))
    raw = json.loads(project.read_text(encoding="utf-8"))
    # Serialized sorted (stable diffs) at the document root, not per entry.
    assert raw["hidden_pixel_presets"] == [
        "preset.pixel.gb-2bpp",
        "preset.pixel.nes-2bpp",
    ]
    assert load_project(str(project)).hidden_pixel_presets == ws.hidden_pixel_presets


def test_empty_pixel_filter_is_omitted_and_loads_empty(tmp_path) -> None:
    rom = tmp_path / "rom.bin"
    rom.write_bytes(b"\x00" * 32)
    ws = Workspace()
    ws.open_file(str(rom))  # no formats hidden

    project = tmp_path / "p.celpix"
    save_project(ws, str(project))
    raw = json.loads(project.read_text(encoding="utf-8"))
    assert "hidden_pixel_presets" not in raw  # a default project stays minimal
    assert load_project(str(project)).hidden_pixel_presets == set()


def test_emulator_mode_persists_only_the_state_path(tmp_path) -> None:
    roms = tmp_path / "roms"
    roms.mkdir()
    rom = roms / "game.sfc"
    rom.write_bytes(b"\x00" * 0x400)
    state = roms / "game.sv0"
    state.write_bytes(b"\x00" * 0x400)

    ws = Workspace()
    entry = ws.open_file(str(rom))
    entry.session = _session(palette_mode="emulator")
    # A loaded emulator-state palette lives on the document as a view-only read
    # window into the state file at the detected offset; only the path survives.
    entry.doc = _doc(FileRef(str(state), offset=1560, length=512), ViewOptions())
    ws.set_current(entry)

    project = tmp_path / "hack.celpix"
    save_project(ws, str(project))

    # The located offset is deliberately dropped — re-detected on restore — so
    # the stored palette carries the path alone (offset defaults to 0).
    raw = json.loads(project.read_text(encoding="utf-8"))
    assert raw["entries"][0]["palette"] == {"path": "roms/game.sv0", "offset": 0}

    loaded = load_project(str(project))
    restored = loaded.entries[0]
    assert restored.session.palette_mode == "emulator"
    assert restored.pending_palette is not None
    assert normcase(restored.pending_palette.path) == normcase(str(state))


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
    # The resolved path must point at the real file; its casing depends on the
    # host filesystem — a case-sensitive OS re-derives the on-disk casing
    # (roms/rom.sfc), while a case-insensitive one keeps the stored casing since
    # it already resolves. Only identity of the target is portable: macOS is
    # case-insensitive yet its normcase (posix) folds nothing.
    assert samefile(loaded.entries[0].path, rom)


def test_tolerant_load_defaults_unknowns_and_garbage(tmp_path) -> None:
    (tmp_path / "x.bin").write_bytes(b"\x00")
    document = {
        "version": 99,  # newer than this reader — still loads, degraded
        "future_top_level_key": {"ignored": True},
        "current": 0,  # points at the garbage entry below → no current
        "entries": [
            {"kind": "file"},  # no path: skipped, not fatal
            # "headered" was a session field once; a project written before it
            # was dropped must still load, with the stale key simply ignored.
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
    assert not hasattr(entry.session, "headered")  # retired field, silently dropped
    assert entry.pending_view is None


def test_a_tilemap_entry_round_trips_with_its_binding(tmp_path) -> None:
    (tmp_path / "tiles.bin").write_bytes(b"\x00" * 0x100)
    (tmp_path / "m.scr").write_bytes(b"\x00" * 0x100)
    ws = Workspace()
    ws.entries.append(
        Entry(name="tiles.bin", kind=EntryKind.FILE, path=str(tmp_path / "tiles.bin"))
    )
    ws.entries.append(
        Entry(
            name="m.scr",
            kind=EntryKind.FILE,
            path=str(tmp_path / "m.scr"),
            content_kind=ContentKind.TILEMAP,
            tilemap_preset_id="preset.tilemap.snes-bg",
            tile_source=TileSource(
                mode=TileMode.ENTRY, entry=ws.entries[0], base_index=16
            ),
            # 0 is the value that has to survive as a *choice*: the user setting it
            # against a format that says 8 is exactly the override this field is
            # for, so a falsy value must not be dropped as "nothing was set".
            palette_row_base=0,
        )
    )
    project = tmp_path / "p.celpix"
    save_project(ws, str(project))

    loaded = load_project(str(project))
    entry = loaded.entries[1]
    assert entry.content_kind is ContentKind.TILEMAP
    assert entry.tilemap_preset_id == "preset.tilemap.snes-bg"
    assert entry.palette_row_base == 0
    source = entry.tile_source
    assert source is not None and source.mode is TileMode.ENTRY
    # Resolved back to the *object* at the stored position, which is the entry
    # this project's first row loaded as — not a number kept for later.
    assert source.entry is loaded.entries[0]
    assert source.base_index == 16


def test_a_pixel_entry_writes_no_tilemap_keys(tmp_path) -> None:
    """The default has to cost nothing on disk, or every project that predates
    tilemaps changes shape the first time it is saved."""
    (tmp_path / "x.bin").write_bytes(b"\x00" * 0x40)
    ws = Workspace()
    ws.entries.append(
        Entry(name="x.bin", kind=EntryKind.FILE, path=str(tmp_path / "x.bin"))
    )
    written = project_dict(ws, str(tmp_path / "p.celpix"))["entries"][0]
    assert "content_kind" not in written
    assert "tile_source" not in written
    assert "tilemap_preset_id" not in written
    assert "palette_row_base" not in written


def test_a_pixel_entry_keeps_its_palette_row_base(tmp_path) -> None:
    """A base is not part of a binding: a tile bank's pinned rows count from one
    exactly as a map's cells do, so a pixel entry has one to keep.

    0 is again the value that has to survive as a *choice* — a bank whose header
    says 8, read against a palette holding only that half, is the whole reason
    the override exists, and dropping a falsy one would put the header's answer
    back on every project load.
    """
    (tmp_path / "bank.cgx").write_bytes(b"\x00" * 0x40)
    ws = Workspace()
    ws.entries.append(
        Entry(
            name="bank.cgx",
            kind=EntryKind.FILE,
            path=str(tmp_path / "bank.cgx"),
            palette_row_base=0,
        )
    )
    project = tmp_path / "p.celpix"
    save_project(ws, str(project))

    entry = load_project(str(project)).entries[0]
    assert entry.content_kind is ContentKind.PIXELS
    assert entry.palette_row_base == 0


def _font_entry(tmp_path, **fields) -> Entry:
    """A pixel entry with a font alphabet on it, for the round trips below."""
    (tmp_path / "font.bin").write_bytes(b"\x00" * 0x40)
    return Entry(
        name="font.bin",
        kind=EntryKind.FILE,
        path=str(tmp_path / "font.bin"),
        **fields,
    )


def _saved(tmp_path, entry: Entry) -> tuple[dict, Entry]:
    """``entry`` through a save and a load: the raw JSON, and what came back."""
    ws = Workspace()
    ws.entries.append(entry)
    project = tmp_path / "p.celpix"
    save_project(ws, str(project))
    raw = json.loads(project.read_text(encoding="utf-8"))
    return raw["entries"][0], load_project(str(project)).entries[0]


def test_a_font_entry_keeps_its_run_its_origin_and_its_named_codes(tmp_path) -> None:
    """Everything "what these tiles spell" is made of, on the entry that has them.

    Two pieces are worth naming. The **origin** is dialled by hand against a
    string, so losing it on save means doing that work again on every load — and
    it is stored beside the run rather than folded into it, because the run is
    read off the sheet and the origin is not. **Prepend/append** are how far past
    the sheet the table is read, which is a fact about this font: a stream that
    terminates on ``$FF`` is read to ``$FF`` every time it is opened
    (``docs/design/fontmap-entry.md`` §4).
    """
    written, entry = _saved(
        tmp_path,
        _font_entry(
            tmp_path,
            use_as_font=True,
            font_base=0x80,
            font_prepend=4,
            font_append=0x40,
            font_chars="AB",
            font_codes=(Glyph(0xFE, "line-break", GlyphRole.BREAK, "Ends it."),),
        ),
    )

    assert written["font"] == {
        "use": True,
        "base": 0x80,
        "prepend": 4,
        "append": 0x40,
        "chars": "AB",
        # A command is a name, a role and whatever the author said it does —
        # never `text`, which is what a *tile draws*.
        "codes": [
            {
                "code": 0xFE,
                "name": "line-break",
                "role": "break",
                "description": "Ends it.",
            }
        ],
    }
    assert entry.use_as_font and entry.font_base == 0x80
    assert (entry.font_prepend, entry.font_append) == (4, 0x40)
    assert entry.font_chars == "AB"
    assert entry.font_codes == (Glyph(0xFE, "line-break", GlyphRole.BREAK, "Ends it."),)


def test_a_pixel_entry_with_no_alphabet_writes_no_font_key(tmp_path) -> None:
    """Which is every entry any project written before this existed holds."""
    written, entry = _saved(tmp_path, _font_entry(tmp_path))

    assert "font" not in written
    assert not entry.use_as_font
    assert (entry.font_base, entry.font_chars, entry.font_codes) == (0, "", ())
    assert (entry.font_prepend, entry.font_append) == (0, 0)


def test_trailing_holes_are_trimmed_and_interior_ones_are_not(tmp_path) -> None:
    """A hole keeps the letters *after* it on the right tiles — and there are
    none after the last, so trailing ones say nothing and are dropped."""
    written, entry = _saved(
        tmp_path, _font_entry(tmp_path, font_chars=f"A{HOLE}B{HOLE}{HOLE}")
    )

    assert written["font"]["chars"] == f"A{HOLE}B"
    assert entry.font_chars == f"A{HOLE}B"
    # And the hole yields no glyph, rather than a glyph with no text.
    assert [(g.code, g.text) for g in sequential(0, entry.font_chars)] == [
        (0, "A"),
        (2, "B"),
    ]


def test_a_non_ascii_run_round_trips_and_is_written_unescaped(tmp_path) -> None:
    """A font sheet spells whatever it was drawn to spell.

    Unescaped because the alternative is a wall of ``\\uXXXX`` where the one
    human-readable thing in the file used to be — a project is meant to live in
    version control and be reviewed as a diff.
    """
    written, entry = _saved(
        tmp_path,
        _font_entry(
            tmp_path,
            font_chars="あいう",
            # Several code points for one code is what the named half is for: the
            # run is one code point per tile and cannot hold a composed glyph.
            font_codes=(Glyph(0x40, "é"),),
        ),
    )

    assert written["font"]["chars"] == "あいう"
    assert entry.font_chars == "あいう"
    assert entry.font_codes[0].text == "é"
    project = (tmp_path / "p.celpix").read_text(encoding="utf-8")
    assert "あいう" in project and "\\u3042" not in project


def test_malformed_font_records_are_skipped_not_raised(tmp_path) -> None:
    """A project is shared, hand-editable and untrusted: one bad line must not
    cost the user the rest of their table."""
    (tmp_path / "font.bin").write_bytes(b"\x00" * 0x40)
    project = tmp_path / "p.celpix"
    project.write_text(
        json.dumps(
            {
                "version": 1,
                "entries": [
                    {
                        "kind": "file",
                        "name": "font.bin",
                        "path": str(tmp_path / "font.bin"),
                        "font": {
                            "use": True,
                            "chars": ["not", "a", "string"],
                            "codes": [
                                "not a record",
                                {"text": "no code"},
                                {"code": "not a number", "text": "x"},
                                {"code": 3, "text": ""},
                                {"code": 4, "text": "ok", "role": "invented"},
                            ],
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    entry = load_project(str(project)).entries[0]
    assert entry.use_as_font
    assert entry.font_chars == ""
    # Only the last survives, and its unknown role reads as an ordinary
    # character rather than costing the whole record.
    assert entry.font_codes == (Glyph(4, "ok", GlyphRole.TEXT),)


def test_an_older_project_naming_a_shipped_alphabet_preset_still_reads(
    tmp_path,
) -> None:
    """The two tables celPix used to ship survive as the editor's templates, so a
    project that named one of them opens with its text still readable."""
    (tmp_path / "font.bin").write_bytes(b"\x00" * 0x40)
    project = tmp_path / "p.celpix"
    project.write_text(
        json.dumps(
            {
                "version": 1,
                "entries": [
                    {
                        "kind": "file",
                        "name": "font.bin",
                        "path": str(tmp_path / "font.bin"),
                        "alphabet_preset_id": "alphabet.ascii-upper",
                        "alphabet_base": 0x80,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    entry = load_project(str(project)).entries[0]
    assert entry.use_as_font
    assert entry.font_chars.startswith("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    # The dialled origin is the half that cost the user work, so it wins over the
    # template's own starting guess.
    assert entry.font_base == 0x80


def test_an_entry_bound_tile_source_stores_only_the_entry_index(tmp_path) -> None:
    """A binding holds the bound entry itself, and a file cannot name an object —
    so saving is where it becomes a position. Writing the whole dataclass would
    put a path on an entry binding, and a reader has no way to tell a meaningless
    field from a meant one."""
    (tmp_path / "m.scr").write_bytes(b"\x00" * 0x100)
    ws = Workspace()
    banks = [
        Entry(name=f"b{n}", kind=EntryKind.FILE, path=str(tmp_path / "m.scr"))
        for n in range(3)
    ]
    ws.entries.extend(banks)
    ws.entries.append(
        Entry(
            name="m.scr",
            kind=EntryKind.FILE,
            path=str(tmp_path / "m.scr"),
            content_kind=ContentKind.TILEMAP,
            tile_source=TileSource(mode=TileMode.ENTRY, entry=banks[2]),
        )
    )
    stored = project_dict(ws, str(tmp_path / "p.celpix"))["entries"][3]
    # The bound entry's *position* at save time, worked out here and nowhere else.
    assert stored["tile_source"] == {"mode": "entry", "entry_index": 2}
    # And a map reading in the rows its format states carries no row base at all,
    # so nothing written before that control existed changes shape.
    assert "palette_row_base" not in stored

    # A binding onto an entry that is no longer open writes -1, which reads back
    # as unbound rather than as whatever now sits at a stale index.
    ws.entries.remove(banks[2])
    stored = project_dict(ws, str(tmp_path / "p.celpix"))["entries"][2]
    assert stored["tile_source"] == {"mode": "entry", "entry_index": -1}


def test_a_broken_tile_binding_leaves_the_tilemap_unbound(tmp_path) -> None:
    """An unusable binding must not cost the map as well as its tiles: the entry
    opens showing placeholders and can be re-pointed."""
    (tmp_path / "m.scr").write_bytes(b"\x00" * 0x100)
    document = {
        "version": PROJECT_VERSION,
        "entries": [
            {
                "path": "m.scr",
                "content_kind": "tilemap",
                "tile_source": {"mode": "nonsense", "entry_index": 1},
            }
        ],
    }
    project = tmp_path / "p.celpix"
    project.write_text(json.dumps(document), encoding="utf-8")

    entry = load_project(str(project)).entries[0]
    assert entry.content_kind is ContentKind.TILEMAP
    assert entry.tile_source is None


def test_a_slice_of_a_tilemap_is_a_tilemap() -> None:
    """A window into a tilemap file is a tilemap — only the entry knows what its
    file holds, so the content kind travels down with the slice."""
    parent = Entry(
        name="rom.smc",
        kind=EntryKind.FILE,
        path="/x/rom.smc",
        content_kind=ContentKind.TILEMAP,
    )
    assert slice_of(parent, "map", 0x100).content_kind is ContentKind.TILEMAP


def test_a_rearrangement_stored_under_the_old_key_still_loads(tmp_path) -> None:
    """``tile_map`` was the key before the type was renamed to
    ``TileRearrangement``. Nothing else would notice the fallback going away —
    the project would just reopen unrearranged, silently."""
    (tmp_path / "x.bin").write_bytes(b"\x00" * 0x400)
    document = {
        "version": PROJECT_VERSION,
        "current": 0,
        "entries": [
            {
                "path": "x.bin",
                "view": {"tile_map": [[0, 3], [3, 0]], "show_rearranged": True},
            }
        ],
    }
    project = tmp_path / "p.celpix"
    project.write_text(json.dumps(document), encoding="utf-8")

    view = load_project(str(project)).entries[0].pending_view
    assert view is not None
    assert view.tile_rearrangement.actual(0) == 3


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
    resets: list[int] = []
    ws.on_added.append(added.append)
    ws.on_removed.append(removed.append)
    ws.on_reset.append(lambda: resets.append(len(ws.entries)))

    new = Entry(name="b.bin", kind=EntryKind.FILE, path=str(tmp_path / "b.bin"))
    ws.replace([new], new)
    assert ws.entries == [new]
    assert ws.current is new
    # The old list goes as one reset (the list already empty when it fires),
    # never as a removal per entry; the new one is still built one at a time.
    assert resets == [0]
    assert removed == []
    assert added == [new]


def test_container_round_trips_and_the_default_is_omitted(tmp_path) -> None:
    # A file's container is part of how it is read, so it has to survive a
    # project — but the plain-bytes default is left out entirely, so projects
    # written before containers existed keep round-tripping unchanged.
    rom = tmp_path / "game.nes"
    rom.write_bytes(b"\x00" * 32)
    ws = Workspace()
    plain = ws.open_file(str(rom))

    project = tmp_path / "p.celpix"
    save_project(ws, str(project))
    assert (
        "container_id"
        not in json.loads(project.read_text(encoding="utf-8"))["entries"][0]
    )
    assert load_project(str(project)).entries[0].container_id == RAW_CONTAINER

    plain.container_id = "container.ines"
    save_project(ws, str(project))
    assert load_project(str(project)).entries[0].container_id == "container.ines"


def test_reshape_round_trips_and_the_default_is_omitted(tmp_path) -> None:
    # A reshape is part of how a region is read, on files and slices alike —
    # but the pass-through default is left out entirely, so a project nobody
    # reshaped carries no trace of the stage.
    rom = tmp_path / "pair.bin"
    rom.write_bytes(b"\x00" * 64)
    ws = Workspace()
    file_entry = ws.open_file(str(rom))
    ws.add_slice(str(rom), "gfx", 16, 32, reshape_id="reshape.split-planes-2")

    project = tmp_path / "p.celpix"
    save_project(ws, str(project))
    raw = json.loads(project.read_text(encoding="utf-8"))
    assert "reshape_id" not in raw["entries"][0]
    assert raw["entries"][1]["reshape_id"] == "reshape.split-planes-2"

    file_entry.reshape_id = "reshape.split-words-2"
    save_project(ws, str(project))
    loaded_file, loaded_slice = load_project(str(project)).entries
    assert loaded_file.reshape_id == "reshape.split-words-2"
    assert loaded_slice.reshape_id == "reshape.split-planes-2"


def test_slot_fill_round_trips_and_the_default_is_omitted(tmp_path) -> None:
    # The slice's answer for the room a tighter re-pack leaves. Omitted at the
    # default so every project written before the choice existed round-trips
    # unchanged — and reads back as the default, which is what it now gets.
    rom = tmp_path / "rom.bin"
    rom.write_bytes(b"\x00" * 64)
    ws = Workspace()
    ws.open_file(str(rom))
    kept = ws.add_slice(str(rom), "kept", 16, 16, compression_id="compression.lz2")
    kept.slot_fill = SlotFill.KEEP
    ws.add_slice(str(rom), "default", 32, 16, compression_id="compression.lz2")

    project = tmp_path / "p.celpix"
    save_project(ws, str(project))
    raw = json.loads(project.read_text(encoding="utf-8"))
    assert raw["entries"][1]["slot_fill"] == "keep"
    assert "slot_fill" not in raw["entries"][2]

    loaded = load_project(str(project)).entries
    assert loaded[1].slot_fill is SlotFill.KEEP
    assert loaded[2].slot_fill is DEFAULT_SLOT_FILL


def test_palette_regions_round_trip_and_an_unpinned_view_omits_them(tmp_path) -> None:
    """Pinned regions persist; a project that pinned nothing carries no trace.

    Stored as pixel runs in the document's own picture space, so a triple reads as
    "these pixels, this row" and stays hand-editable. The toggle that shows them
    stays out of the file — it is a local preference in QSettings, so a project
    that pins something still writes only the pins.
    """
    rom = tmp_path / "gfx.bin"
    rom.write_bytes(b"\x00" * 256)
    ws = Workspace()
    entry = ws.open_file(str(rom))
    entry.session = _session()
    entry.doc = _doc(FileRef(str(rom)), ViewOptions())

    project = tmp_path / "p.celpix"
    save_project(ws, str(project))
    raw = json.loads(project.read_text(encoding="utf-8"))
    assert "palette_regions" not in raw["entries"][0]["view"]
    assert "show_palette_regions" not in raw["entries"][0]["view"]

    entry.doc.view.palette_regions = PaletteRegions().assigned([(64, 32), (160, 16)], 3)
    entry.doc.view.show_palette_regions = False
    save_project(ws, str(project))
    raw = json.loads(project.read_text(encoding="utf-8"))
    assert raw["entries"][0]["view"]["palette_regions"] == [[64, 32, 3], [160, 16, 3]]
    assert "show_palette_regions" not in raw["entries"][0]["view"]

    restored = load_project(str(project)).entries[0]
    assert restored.pending_view.palette_regions == entry.doc.view.palette_regions


def test_a_page_assembly_round_trips_and_an_unpaged_entry_omits_it(tmp_path) -> None:
    """Which arrangement a paged tilemap is being read in is the user's answer to
    a question the file does not raise, so it has to be remembered — and it is the
    only kind of entry that has one, so nothing else grows a key."""
    rom = tmp_path / "screen.scr"
    rom.write_bytes(b"\x00" * 256)
    ws = Workspace()
    entry = ws.open_file(str(rom))
    entry.session = _session()
    entry.doc = _doc(FileRef(str(rom)), ViewOptions())

    project = tmp_path / "p.celpix"
    save_project(ws, str(project))
    assert (
        "pages_across"
        not in json.loads(project.read_text(encoding="utf-8"))["entries"][0]["view"]
    )

    entry.doc.view.pages_across = 4
    save_project(ws, str(project))
    raw = json.loads(project.read_text(encoding="utf-8"))
    assert raw["entries"][0]["view"]["pages_across"] == 4
    assert load_project(str(project)).entries[0].pending_view.pages_across == 4


def test_malformed_palette_regions_are_skipped_not_raised(tmp_path) -> None:
    """A hand-edited file with a bad span opens without it, not with an error.

    Overlapping and unsorted spans are normalized too, so what loads is always a
    set the lookup can trust rather than one it has to defend against. Overlaps
    resolve earlier-wins, which is what makes the result depend only on the spans
    themselves and not on the order a hand-edited file happens to list them in.
    """
    rom = tmp_path / "gfx.bin"
    rom.write_bytes(b"\x00" * 256)
    ws = Workspace()
    entry = ws.open_file(str(rom))
    entry.session = _session()
    entry.doc = _doc(FileRef(str(rom)), ViewOptions())
    project = tmp_path / "p.celpix"
    save_project(ws, str(project))

    raw = json.loads(project.read_text(encoding="utf-8"))
    raw["entries"][0]["view"]["palette_regions"] = [
        [64, 32],  # too short
        ["x", 1, 2],  # not ints
        [96, 0, 1],  # empty span
        [160, 16, 2],
        [150, 20, 5],  # unsorted, and overlaps the one above
    ]
    project.write_text(json.dumps(raw), encoding="utf-8")

    view = load_project(str(project)).entries[0].pending_view
    # The span starting at 150 is the earlier one, so it keeps its whole extent
    # and the one at 160 is clipped to what is left of it.
    assert [(r.start, r.length, r.row) for r in view.palette_regions.regions] == [
        (150, 20, 5),
        (170, 6, 2),
    ]


def test_a_regions_extra_files_survive_a_project_round_trip(tmp_path) -> None:
    """A region is its files joined, so losing the list past the first one is
    losing the document — the slice offsets under it would all move.

    Stored relative like every other path, and on the slice too rather than
    re-derived from its parent: entries load before there is a workspace to look
    a parent up in.
    """
    lo, hi = tmp_path / "lo.bin", tmp_path / "hi.bin"
    for f in (lo, hi):
        f.write_bytes(bytes(64))
    ws = Workspace()
    ws.open_file(str(lo), extra_paths=(str(hi),))
    ws.add_slice(str(lo), "gfx", 32, 64)

    project = tmp_path / "p.celpix"
    save_project(ws, str(project))
    raw = json.loads(project.read_text(encoding="utf-8"))
    assert raw["entries"][0]["extra_paths"] == ["hi.bin"]  # relative, like path

    file_entry, slice_entry = load_project(str(project)).entries
    assert normcase(file_entry.paths[1]) == normcase(str(hi))
    assert normcase(slice_entry.paths[1]) == normcase(str(hi))


def test_a_one_file_entry_stores_no_file_list(tmp_path) -> None:
    # The ordinary entry is unchanged on disk, so nothing written before regions
    # existed reads differently and no project grows a key for nothing.
    rom = tmp_path / "rom.bin"
    rom.write_bytes(bytes(16))
    ws = Workspace()
    ws.open_file(str(rom))

    project = tmp_path / "p.celpix"
    save_project(ws, str(project))
    assert (
        "extra_paths"
        not in json.loads(project.read_text(encoding="utf-8"))["entries"][0]
    )


def test_pixel_aspect_round_trips_and_stays_absent_until_answered(tmp_path) -> None:
    """The project's pixel shape, and the difference between "square" and "unasked".

    The two are one value in the file's absence and two in the reader's hands: an
    omitted key leaves a container's hint free to seed it on the next load, where
    a stored ``[1, 1]`` is an answer that stops the seeding
    (``docs/design/pixel-aspect.md`` §3).
    """
    ws = Workspace()
    ws.open_file(str(tmp_path / "rom.bin"))
    path = str(tmp_path / "p.celpix")

    # Never answered: the key is not written at all, so a project predating the
    # setting re-saves byte-identical.
    assert "pixel_aspect" not in project_dict(ws, path)
    save_project(ws, path)
    assert load_project(path).pixel_aspect is None

    # Answered square: written, and read back as an answer rather than as nothing.
    ws.pixel_aspect = (1, 1)
    save_project(ws, path)
    assert json.loads(open(path, encoding="utf-8").read())["pixel_aspect"] == [1, 1]
    assert load_project(path).pixel_aspect == (1, 1)

    # And a real ratio survives as a tuple, not the list JSON holds it as.
    ws.pixel_aspect = (1, 2)
    save_project(ws, path)
    assert load_project(path).pixel_aspect == (1, 2)


def test_a_malformed_pixel_aspect_degrades_to_unanswered(tmp_path) -> None:
    """A hand-edited ratio that is not one must not reach a painter.

    Every shape below would either fail to draw or divide by zero, and the
    project still has to open — the reader's rule for every other key.
    """
    path = tmp_path / "p.celpix"
    for stored in ([0, 1], [2], "2:1", [1, -1], {"w": 1}, None):
        path.write_text(
            json.dumps(
                {"version": PROJECT_VERSION, "entries": [], "pixel_aspect": stored}
            ),
            encoding="utf-8",
        )
        assert load_project(str(path)).pixel_aspect is None, stored
