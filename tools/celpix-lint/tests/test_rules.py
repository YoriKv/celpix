"""What each rule fires on, and — as often — what it does not.

The negative cases carry most of the weight here. A linter over a format this
tolerant is only useful while it is quiet on correct files, and every false
positive found while building this one was a legal state read as an illegal
one: a signed `base_index`, a `slice_length` of null, an offset into a region
joined from several ROM chips. Each has a test below so it stays fixed.
"""

from __future__ import annotations

import json

import pytest

from celpix_lint.known import KnownIds, load_snapshot
from celpix_lint.linter import lint

ROM = {"rom.sfc": 0x10000}


# -- the document ----------------------------------------------------------
def test_unreadable_file_is_fatal(tmp_path, ids):
    report = lint(str(tmp_path / "nope.celpix"), ids)
    assert report.fatal and [d.code for d in report.diagnostics] == ["F001"]


def test_malformed_json_is_fatal_and_stops_the_entry_checks(tmp_path, ids):
    path = tmp_path / "bad.celpix"
    path.write_text('{"entries": [', encoding="utf-8")
    report = lint(str(path), ids)
    assert report.fatal and [d.code for d in report.diagnostics] == ["F002"]


def test_entries_must_be_an_array(tmp_path, ids):
    path = tmp_path / "bad.celpix"
    path.write_text('{"version": 1, "entries": {}}', encoding="utf-8")
    assert [d.code for d in lint(str(path), ids).diagnostics] == ["F004"]


def test_current_naming_a_palette_cannot_be_shown(project, entry):
    codes = project(
        {
            "version": 1,
            "current": 0,
            "entries": [
                {
                    "kind": "palette",
                    "path": "p.pal",
                    "palette_preset_id": "preset.palette.bgr555",
                }
            ],
        },
        files={"p.pal": 512},
    )
    assert "E112" in codes


def test_current_out_of_range(project, entry):
    codes = project({"version": 1, "current": 7, "entries": [entry()]}, files=ROM)
    assert "E111" in codes


def test_newer_version_warns_that_saving_rewrites(project, entry):
    codes = project({"version": 99, "current": 0, "entries": [entry()]}, files=ROM)
    assert "W103" in codes


def test_a_clean_project_is_silent(project, entry):
    assert project({"version": 1, "current": 0, "entries": [entry()]}, files=ROM) == []


# -- entry shape -----------------------------------------------------------
def test_unknown_kind_reads_as_file(project, entry):
    codes = project({"version": 1, "entries": [entry(kind="sliced")]}, files=ROM)
    assert "E203" in codes


def test_missing_path_drops_the_entry(project):
    codes = project({"version": 1, "entries": [{"kind": "file", "name": "x"}]})
    assert "E204" in codes


def test_key_belonging_to_another_kind_is_never_read(project, entry):
    # A container on a slice: a slice reads through its parent's coordinates.
    codes = project(
        {
            "version": 1,
            "entries": [
                entry(),
                entry(kind="slice", slice_offset=0, container_id="container.ines"),
            ],
        },
        files=ROM,
    )
    assert "W211" in codes


def test_tilemap_keys_on_a_pixels_entry_do_nothing(project, entry):
    codes = project(
        {"version": 1, "entries": [entry(tilemap_preset_id="preset.tilemap.snes-bg")]},
        files=ROM,
    )
    assert "W213" in codes


def test_unknown_entry_key_is_ignored_on_load(project, entry):
    codes = project({"version": 1, "entries": [entry(slice_ofset=16)]}, files=ROM)
    assert "W210" in codes


def test_slice_length_null_is_legal(project, entry):
    codes = project(
        {
            "version": 1,
            "entries": [
                entry(),
                entry(kind="slice", slice_offset=0, slice_length=None),
            ],
        },
        files=ROM,
    )
    assert codes == []


def test_slice_length_null_beside_a_reshape_is_not(project, entry):
    codes = project(
        {
            "version": 1,
            "entries": [
                entry(),
                entry(
                    kind="slice",
                    slice_offset=0,
                    slice_length=None,
                    reshape_id="reshape.split-planes-2",
                ),
            ],
        },
        files=ROM,
    )
    assert "E242" in codes


def test_signed_palette_row_base_is_legal(project, entry):
    assert (
        project({"version": 1, "entries": [entry(palette_row_base=-8)]}, files=ROM)
        == []
    )


# -- files on disk ---------------------------------------------------------
def test_missing_file(project, entry):
    assert "E301" in project({"version": 1, "entries": [entry()]})


def test_no_files_mode_skips_the_disk(project, entry):
    assert project({"version": 1, "entries": [entry()]}, check_files=False) == []


def test_slice_past_the_end_of_its_file(project, entry):
    codes = project(
        {"version": 1, "entries": [entry(), entry(kind="slice", slice_offset=0x20000)]},
        files=ROM,
    )
    assert "E310" in codes


def test_slice_running_past_the_end(project, entry):
    codes = project(
        {
            "version": 1,
            "entries": [
                entry(),
                entry(kind="slice", slice_offset=0xFF00, slice_length=0x400),
            ],
        },
        files=ROM,
    )
    assert "E311" in codes


def test_offsets_are_measured_against_the_whole_join(project, entry):
    """A region built from several ROM chips is one address space, and an offset
    inside the second chip is in range even though it is past the end of the
    first. The check that got this wrong reported four false positives."""
    codes = project(
        {
            "version": 1,
            "entries": [
                entry(extra_paths=["rom2.sfc"]),
                entry(kind="slice", slice_offset=0x18000, extra_paths=["rom2.sfc"]),
            ],
        },
        files={"rom.sfc": 0x10000, "rom2.sfc": 0x10000},
    )
    assert codes == []


def test_child_missing_its_parents_join(project, entry):
    codes = project(
        {
            "version": 1,
            "entries": [
                entry(extra_paths=["rom2.sfc"]),
                entry(kind="slice", slice_offset=0x18000),
            ],
        },
        files={"rom.sfc": 0x10000, "rom2.sfc": 0x10000},
    )
    assert "E504" in codes and "E310" in codes


def test_two_file_entries_over_one_file(project, entry):
    codes = project(
        {"version": 1, "entries": [entry(), entry(name="again")]}, files=ROM
    )
    assert "E320" in codes


# -- plugin ids ------------------------------------------------------------
def test_unknown_preset_id_against_a_live_registry(project, entry):
    codes = project(
        {
            "version": 1,
            "entries": [entry(session={"pixel_preset_id": "preset.pixel.nope"})],
        },
        files=ROM,
    )
    assert "E404" in codes


def test_id_from_the_wrong_stage_says_which_stage_it_is(project, entry):
    codes = project(
        {
            "version": 1,
            "entries": [entry(session={"pixel_preset_id": "preset.tilemap.snes-bg"})],
        },
        files=ROM,
    )
    assert "E403" in codes


def test_renamed_id_still_resolves(project, entry):
    codes = project(
        {
            "version": 1,
            "entries": [entry(session={"palette_preset_id": "preset.palette.r4g4b4"})],
        },
        files=ROM,
    )
    assert codes == ["I402"]


def test_snapshot_only_registry_downgrades_an_unknown_id(tmp_path, entry):
    """The shipped snapshot cannot see a user's own plugins, so it must not claim
    an id is missing — only that it is not a built-in."""
    quiet = KnownIds(
        presets={"interpret-pixel": {"preset.pixel.snes-4bpp"}},
        source="snapshot (test)",
        authoritative=False,
    )
    path = tmp_path / "p.celpix"
    (tmp_path / "rom.sfc").write_bytes(b"\x00" * 256)
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "entries": [entry(session={"pixel_preset_id": "preset.pixel.mine"})],
            }
        ),
        encoding="utf-8",
    )
    codes = [d.code for d in lint(str(path), quiet).diagnostics]
    assert "W405" in codes and "E404" not in codes


def test_a_container_that_does_not_frame_this_content(project, entry):
    codes = project(
        {"version": 1, "entries": [entry(container_id="container.scgcad-col")]},
        files=ROM,
    )
    assert "E410" in codes


def test_the_projects_own_plugin_folder_provides_ids(tmp_path, ids, entry):
    """A project travels with its formats: a preset only its own plugins/ folder
    declares is present, not missing."""
    (tmp_path / "rom.sfc").write_bytes(b"\x00" * 256)
    preset = tmp_path / "plugins" / "pixel" / "mine.toml"
    preset.parent.mkdir(parents=True)
    preset.write_text(
        'id = "preset.pixel.mine"\n[params]\nbytes = 1\n', encoding="utf-8"
    )
    path = tmp_path / "p.celpix"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "entries": [entry(session={"pixel_preset_id": "preset.pixel.mine"})],
            }
        ),
        encoding="utf-8",
    )
    assert [d.code for d in lint(str(path), ids).diagnostics] == []


# -- cross-references ------------------------------------------------------
def test_tilemap_bound_to_a_palette(project, entry):
    codes = project(
        {
            "version": 1,
            "entries": [
                {
                    "kind": "palette",
                    "path": "p.pal",
                    "palette_preset_id": "preset.palette.bgr555",
                },
                entry(
                    content_kind="tilemap",
                    tilemap_preset_id="preset.tilemap.snes-bg",
                    tile_source={"mode": "entry", "entry_index": 0},
                ),
            ],
        },
        files={"rom.sfc": 0x1000, "p.pal": 512},
    )
    assert "E519" in codes


def test_tile_source_index_out_of_range(project, entry):
    codes = project(
        {
            "version": 1,
            "entries": [
                entry(
                    content_kind="tilemap",
                    tilemap_preset_id="preset.tilemap.snes-bg",
                    tile_source={"mode": "entry", "entry_index": 9},
                )
            ],
        },
        files=ROM,
    )
    assert "E518" in codes


def test_signed_base_index_is_legal(project, entry):
    """Negative when the map numbers from partway into a slice that holds
    exactly its tiles — both directions are used, which is why it is signed."""
    codes = project(
        {
            "version": 1,
            "entries": [
                entry(),
                entry(
                    path="map.bin",
                    content_kind="tilemap",
                    tilemap_preset_id="preset.tilemap.snes-bg",
                    tile_source={"mode": "entry", "entry_index": 0, "base_index": -256},
                ),
            ],
        },
        files={"rom.sfc": 0x10000, "map.bin": 0x800},
    )
    assert codes == []


def test_binding_loop(project, entry):
    codes = project(
        {
            "version": 1,
            "entries": [
                entry(
                    content_kind="tilemap",
                    tilemap_preset_id="preset.tilemap.snes-bg",
                    tile_source={"mode": "entry", "entry_index": 1},
                ),
                entry(
                    content_kind="tilemap",
                    tilemap_preset_id="preset.tilemap.snes-bg",
                    tile_source={"mode": "entry", "entry_index": 0},
                ),
            ],
        },
        files=ROM,
    )
    assert "E520" in codes


def test_a_map_may_draw_through_a_map_that_reaches_art(project, entry):
    codes = project(
        {
            "version": 1,
            "entries": [
                entry(),
                entry(
                    path="map.bin",
                    content_kind="tilemap",
                    tilemap_preset_id="preset.tilemap.snes-bg",
                    tile_source={"mode": "entry", "entry_index": 0},
                ),
                entry(
                    path="map2.bin",
                    content_kind="tilemap",
                    tilemap_preset_id="preset.tilemap.snes-bg",
                    tile_source={"mode": "entry", "entry_index": 1},
                ),
            ],
        },
        files={"rom.sfc": 0x10000, "map.bin": 0x800, "map2.bin": 0x800},
    )
    assert codes == []


def test_child_before_its_parent(project, entry):
    codes = project(
        {"version": 1, "entries": [entry(kind="slice", slice_offset=0), entry()]},
        files=ROM,
    )
    assert "E503" in codes


def test_slice_with_no_parent_entry(project, entry):
    codes = project(
        {"version": 1, "entries": [entry(kind="slice", slice_offset=0)]}, files=ROM
    )
    assert "W502" in codes


def test_composite_cannot_hold_a_composite(project, entry):
    codes = project(
        {
            "version": 1,
            "entries": [
                {"kind": "composite", "name": "a", "pieces": [{"length": 16}]},
                {"kind": "composite", "name": "b", "pieces": [{"entry_index": 0}]},
            ],
        }
    )
    assert "E539" in codes


def test_composite_pad_needs_a_length(project):
    codes = project(
        {"version": 1, "entries": [{"kind": "composite", "name": "a", "pieces": [{}]}]}
    )
    assert "E535" in codes


# -- view ------------------------------------------------------------------
def test_one_bad_pair_discards_the_whole_rearrangement(project, entry):
    codes = project(
        {
            "version": 1,
            "entries": [entry(view={"tile_rearrangement": [[0, 3], [1, "x"]]})],
        },
        files=ROM,
    )
    assert "E724" in codes and "E720" in codes


def test_a_slot_claimed_twice_loses_one_of_them(project, entry):
    codes = project(
        {
            "version": 1,
            "entries": [entry(view={"tile_rearrangement": [[0, 3], [0, 5]]})],
        },
        files=ROM,
    )
    assert "E728" in codes


def test_unknown_block_order_reads_as_row(project, entry):
    codes = project(
        {"version": 1, "entries": [entry(view={"block_order": "snake"})]}, files=ROM
    )
    assert "E714" in codes


def test_overlapping_pinned_regions(project, entry):
    codes = project(
        {
            "version": 1,
            "entries": [entry(view={"palette_regions": [[0, 64, 1], [32, 64, 2]]})],
        },
        files=ROM,
    )
    assert "W734" in codes


def test_zoom_is_noted_as_ignored(project, entry):
    assert project(
        {"version": 1, "entries": [entry(view={"zoom": 0.5})]}, files=ROM
    ) == ["I704"]


# -- session and palette ---------------------------------------------------
@pytest.mark.parametrize(
    "mode, block, code",
    [
        ("file", {"offset": 0}, "E622"),
        ("custom", {"path": "p.pal"}, "E622"),
        ("offset", {"offset": 16, "path": "p.pal"}, "E624"),
        ("file", {"colors": ["#FF000000"], "path": "p.pal"}, "E623"),
    ],
)
def test_palette_mode_and_block_must_agree(project, entry, mode, block, code):
    codes = project(
        {
            "version": 1,
            "entries": [entry(session={"palette_mode": mode}, palette=block)],
        },
        files={"rom.sfc": 0x1000, "p.pal": 512},
    )
    assert code in codes


def test_palette_mode_with_no_block(project, entry):
    codes = project(
        {"version": 1, "entries": [entry(session={"palette_mode": "offset"})]},
        files=ROM,
    )
    assert "E610" in codes


def test_one_bad_color_discards_the_custom_palette(project, entry):
    codes = project(
        {
            "version": 1,
            "entries": [
                entry(
                    session={"palette_mode": "custom"},
                    palette={"colors": ["#FF000000", "not a color"]},
                )
            ],
        },
        files=ROM,
    )
    assert "E632" in codes


def test_unknown_palette_mode_reads_as_default(project, entry):
    codes = project(
        {"version": 1, "entries": [entry(session={"palette_mode": "from-emu"})]},
        files=ROM,
    )
    assert "E602" in codes


def test_missing_palette_file_degrades_quietly(project, entry):
    codes = project(
        {
            "version": 1,
            "entries": [
                entry(
                    session={"palette_mode": "file"},
                    palette={"path": "gone.pal", "offset": 0},
                )
            ],
        },
        files=ROM,
    )
    assert "E641" in codes


# -- font ------------------------------------------------------------------
def test_glyph_with_no_code_is_skipped(project, entry):
    codes = project(
        {
            "version": 1,
            "entries": [entry(font={"use": True, "codes": [{"text": "A"}]})],
        },
        files=ROM,
    )
    assert "E834" in codes


def test_glyph_that_spells_nothing_is_skipped(project, entry):
    codes = project(
        {"version": 1, "entries": [entry(font={"use": True, "codes": [{"code": 65}]})]},
        files=ROM,
    )
    assert "E838" in codes


def test_unknown_role_reads_as_text(project, entry):
    codes = project(
        {
            "version": 1,
            "entries": [
                entry(
                    font={
                        "use": True,
                        "codes": [{"code": 254, "name": "end", "role": "stop"}],
                    }
                )
            ],
        },
        files=ROM,
    )
    assert "E837" in codes


def test_font_on_a_tilemap_entry(project, entry):
    codes = project(
        {
            "version": 1,
            "entries": [
                entry(
                    content_kind="tilemap",
                    tilemap_preset_id="preset.tilemap.snes-bg",
                    font={"use": True, "chars": "ABC"},
                )
            ],
        },
        files=ROM,
    )
    assert "W214" in codes


def test_table_kept_but_not_read(project, entry):
    codes = project(
        {"version": 1, "entries": [entry(font={"chars": "ABC"})]}, files=ROM
    )
    assert "W803" in codes


def test_negative_base_pushes_the_run_below_zero(project, entry):
    codes = project(
        {
            "version": 1,
            "entries": [entry(font={"use": True, "base": -4, "chars": "ABCDEF"})],
        },
        files=ROM,
    )
    assert "W812" in codes


def test_a_full_font_block_is_silent(project, entry):
    codes = project(
        {
            "version": 1,
            "entries": [
                entry(
                    font={
                        "use": True,
                        "base": 32,
                        "chars": "ABC",
                        "codes": [
                            {"code": 26, "text": "th"},
                            {"code": 254, "name": "line-break", "role": "break"},
                        ],
                    }
                )
            ],
        },
        files=ROM,
    )
    assert codes == []


# -- the shipped snapshot --------------------------------------------------
def test_the_shipped_snapshot_loads():
    snapshot = load_snapshot()
    assert snapshot.usable and not snapshot.authoritative
    assert snapshot.has("interpret-pixel", "preset.pixel.snes-4bpp")
    assert snapshot.has("container", "container.raw-file")


# -- plugin inputs ------------------------------------------------------------
def _bound_slice(entry, **inputs):
    return entry(
        kind="slice",
        slice_offset=0x200,
        slice_length=64,
        compression_id="compression.lz2",
        inputs={"compression.lz2": inputs},
    )


def test_a_well_formed_inputs_block_is_silent(project, entry):
    codes = project(
        {
            "version": 1,
            "entries": [
                entry(),
                _bound_slice(
                    entry,
                    table={"offset": 0x100, "length": 4},
                    size={"offset": 0x300, "width": 2, "endian": "big"},
                    count=16,
                    other={"entry_index": 0, "offset": 0, "length": 4},
                ),
            ],
        },
        files=ROM,
    )
    assert codes == []


def test_inputs_shapes_the_loader_skips_are_reported(project, entry):
    codes = project(
        {
            "version": 1,
            "entries": [
                entry(),
                _bound_slice(
                    entry,
                    flag=True,
                    text="0x100",
                    negative={"offset": -1, "length": 4},
                    wide={"offset": 0x300, "width": 9},
                    order={"offset": 0x300, "width": 2, "endian": "middle"},
                    empty={"offset": 0x100, "length": 0},
                ),
            ],
        },
        files=ROM,
    )
    # A boolean is a flag's shape, so `flag=True` is only an undeclared key.
    assert codes.count("E907") == 1
    assert "E909" in codes and "E910" in codes
    assert "W911" in codes and "W913" in codes


def test_a_region_past_the_file_and_a_bad_target_are_reported(project, entry):
    codes = project(
        {
            "version": 1,
            "entries": [
                entry(),
                _bound_slice(
                    entry,
                    past={"offset": 0xFFF0, "length": 0x20},
                    gone={"entry_index": -1, "offset": 0, "length": 4},
                    far={"entry_index": 9, "offset": 0, "length": 4},
                    me={"entry_index": 1, "offset": 0, "length": 4},
                    map={"entry_index": 2, "offset": 0, "length": 4},
                    deep={"entry_index": 3, "offset": 0, "length": 4},
                ),
                entry(
                    content_kind="tilemap", tilemap_preset_id="preset.tilemap.snes-bg"
                ),
                _bound_slice(entry, table={"entry_index": 0, "offset": 0, "length": 4}),
            ],
        },
        files=ROM,
    )
    assert "E912" in codes
    assert "W915" in codes and "E916" in codes
    assert codes.count("E917") == 2
    assert "E918" in codes


def test_inputs_for_another_codec_and_an_unknown_plugin_are_noted(project, entry):
    codes = project(
        {
            "version": 1,
            "entries": [
                entry(),
                entry(
                    kind="slice",
                    slice_offset=0,
                    inputs={
                        "compression.lz2": {"table": {"offset": 0, "length": 4}},
                        "compression.nobody": {"table": 1},
                        "preset.pixel.snes-4bpp": {"table": 1},
                    },
                ),
            ],
        },
        files=ROM,
    )
    assert "I906" in codes and "E904" in codes and "E903" in codes


def test_inputs_on_a_bookmark_are_never_read(project, entry):
    codes = project(
        {
            "version": 1,
            "entries": [
                entry(),
                entry(kind="bookmark", offset=0, inputs={"compression.lz2": {}}),
            ],
        },
        files=ROM,
    )
    assert "W211" in codes


# -- inputs against the plugin's declarations ------------------------------
def _project_with_input(tmp_path, value):
    path = tmp_path / "in.celpix"
    (tmp_path / "rom.sfc").write_bytes(bytes(0x10000))
    path.write_text(
        json.dumps(
            {
                "version": 3,
                "current": 0,
                "entries": [
                    {"kind": "file", "name": "rom.sfc", "path": "rom.sfc"},
                    {
                        "kind": "slice",
                        "name": "map",
                        "path": "rom.sfc",
                        "slice_offset": 0,
                        "slice_length": 16,
                        "compression_id": "compression.parted",
                        "inputs": {"compression.parted": value},
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def _input_codes(report) -> list[str]:
    # the minimal fixture project draws its own findings (no session, no view);
    # only the declaration checks are under test here
    return [d.code for d in report.diagnostics if d.code in ("E914", "W913", "E919")]


def test_a_flag_bound_as_a_number_is_an_error_and_a_boolean_is_quiet(tmp_path, ids):
    report = lint(str(_project_with_input(tmp_path, {"strict": 1})), ids)
    assert _input_codes(report) == ["E919"]
    report = lint(str(_project_with_input(tmp_path, {"strict": True})), ids)
    assert _input_codes(report) == []


def test_an_input_outside_the_declared_range_is_an_error(tmp_path, ids):
    # The app refuses the binding and drops the stage, so the entry opens raw
    # with only a notice: the quietest failure in the format, and the one a
    # generator that seeded the context by hand never saw.
    report = lint(str(_project_with_input(tmp_path, {"interleave": 12})), ids)
    assert _input_codes(report) == ["E914"]


def test_an_input_inside_the_declared_range_is_quiet(tmp_path, ids):
    report = lint(str(_project_with_input(tmp_path, {"interleave": 4})), ids)
    assert _input_codes(report) == []


def test_an_input_the_plugin_never_declared_is_a_warning(tmp_path, ids):
    report = lint(str(_project_with_input(tmp_path, {"parts": 4})), ids)
    assert _input_codes(report) == ["W913"]


def test_live_without_celpix_says_it_fell_back(tmp_path, monkeypatch, capsys):
    """--live is a stronger claim than the snapshot can back; when celPix cannot
    be imported the run says so, instead of reporting clean against the snapshot
    and then telling the user to re-run with the flag they already passed."""
    from celpix_lint import cli, known

    monkeypatch.setattr(known, "load_live", lambda: None)
    path = tmp_path / "p.celpix"
    path.write_text(json.dumps({"version": 1, "entries": []}))
    cli.main(["--live", "--no-files", str(path)])
    err = capsys.readouterr().err
    assert "celPix is not importable here" in err
    assert "Re-run with --live" not in err
