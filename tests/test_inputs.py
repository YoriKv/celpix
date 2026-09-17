"""Plugin inputs: a stage's declared regions and integers, bound per entry,
resolved by the host and delivered on the context
(``docs/design/plugin-inputs.md``).

Model-layer only: a toy scheme that needs a table and an optional size, run
through the real config factories, the real pipeline and the real project file.
"""

from __future__ import annotations

import json
from dataclasses import replace

from celpix.core.context import (
    KEY_DECOMPRESS_COMPLETE,
    KEY_INPUTS,
    KEY_SOURCE_OFFSET,
    PipelineContext,
)
from celpix.core.document import Document
from celpix.core.errors import Stage
from celpix.core.notices import notices
from celpix.core.palette import Palette
from celpix.pipeline import pipeline
from celpix.pipeline.pathway import PathwayConfig
from celpix.plugins.base import NO_COMPRESSION, STAGE_DEFAULT_PRESET, FileRef
from celpix.plugins.registry import default_registry
from celpix.project import projectfile
from celpix.project.inputs import (
    IntegerFromBytes,
    RegionBinding,
    can_supply_input,
    declared_inputs,
    prune_bindings,
    resolve_inputs,
    with_bindings,
)
from celpix.project.projectfile import load_project, project_dict, save_project
from celpix.project.workspace import (
    Entry,
    EntryKind,
    Workspace,
    pixel_config_for,
    slice_of,
)
from modelhelpers import XOR_ID, XorTableCodec, xor_bytes

PIXEL = STAGE_DEFAULT_PRESET[Stage.INTERPRET_PIXEL]


def _registry():
    reg = default_registry()
    reg.register(XorTableCodec())
    return reg


_xor = xor_bytes
_XorTable = XorTableCodec


TABLE = bytes([0x11, 0x22, 0x33, 0x44])
STREAM = bytes(range(64))


def _rom(tmp_path):
    """A file with the table at 0x100 and a stream at 0x200, and a workspace
    holding it plus a slice over the stream bound to that table."""
    rom = tmp_path / "rom.bin"
    body = bytearray(0x400)
    body[0x100 : 0x100 + len(TABLE)] = TABLE
    body[0x200 : 0x200 + len(STREAM)] = STREAM
    rom.write_bytes(bytes(body))
    ws = Workspace()
    parent = ws.open_file(str(rom))
    sl = ws.add_slice(parent.path, "stream", 0x200, len(STREAM), XOR_ID)
    sl.inputs = {XOR_ID: {"table": RegionBinding(offset=0x100, length=len(TABLE))}}
    return ws, parent, sl


def _load(sl, ws, reg):
    cfg = pixel_config_for(sl, PIXEL, reg, ws)
    return cfg, pipeline.load_pixel_data(cfg, reg)


def test_a_bound_region_and_integer_reach_the_stage_on_the_context(tmp_path) -> None:
    reg = _registry()
    ws, _parent, sl = _rom(tmp_path)
    cfg, px = _load(sl, ws, reg)
    assert cfg.compression_id == XOR_ID and cfg.write_enabled
    assert px.data == _xor(STREAM, TABLE)

    # The optional integer, as a literal: absent from the dict until bound.
    sl.inputs = with_bindings(sl, XOR_ID, {**sl.inputs[XOR_ID], "output_size": 16})
    _cfg, px = _load(sl, ws, reg)
    assert px.data == _xor(STREAM, TABLE)[:16]
    # And the save side is handed the same inputs: the round trip holds.
    doc = Document(
        pixel_data=px.data,
        bytes_per_tile=32,
        tile_width=8,
        tile_height=8,
        palette=Palette([0]),
        pixel_config=_cfg,
        palette_config=_cfg,
        pixel_ctx=px.ctx,
    )
    assert pipeline.encoded_pixel_bytes(doc, reg)[:16] == STREAM[:16]


def test_the_stage_key_is_cleared_for_a_scheme_declaring_nothing(tmp_path) -> None:
    """A slice under an ordinary codec must not find another stage's dict."""
    reg = _registry()
    ws, parent, _sl = _rom(tmp_path)
    plain = ws.add_slice(parent.path, "plain", 0x200, 8)
    cfg, px = _load(plain, ws, reg)
    assert cfg.inputs == {} and cfg.input_problems == ()
    assert px.ctx.get(KEY_INPUTS) == {}


def test_every_failure_opens_degraded_and_says_why(tmp_path) -> None:
    reg = _registry()
    ws, _parent, sl = _rom(tmp_path)
    cases = {
        "unbound": {},
        "past the end": {"table": RegionBinding(offset=0x3FE, length=4)},
        "off the stride": {"table": RegionBinding(offset=0x100, length=3)},
        "wrong shape": {"table": 5},
        "integer out of range": {
            "table": RegionBinding(offset=0x100, length=4),
            "output_size": 0,
        },
        "integer as a region": {
            "table": RegionBinding(offset=0x100, length=4),
            "output_size": RegionBinding(offset=0x100, length=2),
        },
    }
    for why, bindings in cases.items():
        sl.inputs = {XOR_ID: bindings}
        cfg, px = _load(sl, ws, reg)
        # The scheme falls back to the pass-through and the entry goes
        # view-only, exactly as a missing plugin does.
        assert cfg.compression_id == NO_COMPRESSION, why
        assert not cfg.write_enabled, why
        assert px.data == STREAM, why
        notes = notices(px.ctx)
        assert len(notes) == 1 and notes[0].source == "compression", why
        assert "Input not resolved" in notes[0].summary, why


def test_an_optional_input_left_unbound_is_absent_or_its_default(tmp_path) -> None:
    reg = _registry()
    ws, _parent, sl = _rom(tmp_path)
    got = resolve_inputs(sl, Stage.COMPRESSION, XOR_ID, reg, ws)
    assert got.ok and "output_size" not in got.values
    # A flag is always delivered: unbound is its default, False here.
    assert got.values["invert"] is False


def test_a_flag_is_a_bool_and_refuses_a_number(tmp_path) -> None:
    reg = _registry()
    ws, _parent, sl = _rom(tmp_path)
    sl.inputs = {XOR_ID: {**sl.inputs[XOR_ID], "invert": True}}
    got = resolve_inputs(sl, Stage.COMPRESSION, XOR_ID, reg, ws)
    assert got.ok and got.values["invert"] is True

    sl.inputs = {XOR_ID: {**sl.inputs[XOR_ID], "invert": 1}}
    got = resolve_inputs(sl, Stage.COMPRESSION, XOR_ID, reg, ws)
    assert [p.key for p in got.problems] == ["invert"]
    assert "yes or no" in got.problems[0].detail

    spec = replace(_XorTable.info.inputs[2], default=1)
    reg.plugin(Stage.COMPRESSION, XOR_ID).info = replace(
        _XorTable.info, inputs=(*_XorTable.info.inputs[:2], spec)
    )
    sl.inputs = {XOR_ID: {"table": sl.inputs[XOR_ID]["table"]}}
    assert (
        resolve_inputs(sl, Stage.COMPRESSION, XOR_ID, reg, ws).values["invert"] is True
    )

    spec = replace(_XorTable.info.inputs[1], default=8)
    reg.plugin(Stage.COMPRESSION, XOR_ID).info = replace(
        _XorTable.info, inputs=(_XorTable.info.inputs[0], spec)
    )
    got = resolve_inputs(sl, Stage.COMPRESSION, XOR_ID, reg, ws)
    assert got.values["output_size"] == 8


def test_a_parent_file_region_reads_the_parents_unsaved_bytes(tmp_path) -> None:
    """The table is read where a raw slice at that offset would read it: from
    the parent's live buffer when it has edits, so an edited table re-keys
    every stream decoded against it rather than the stale one on disk."""
    reg = _registry()
    ws, parent, sl = _rom(tmp_path)
    cfg = PathwayConfig(source=FileRef(parent.path), interpret_preset_id=PIXEL)
    parent.doc = Document(
        pixel_data=(tmp_path / "rom.bin").read_bytes(),
        bytes_per_tile=32,
        tile_width=8,
        tile_height=8,
        palette=Palette([0]),
        pixel_config=cfg,
        palette_config=cfg,
    )
    parent.doc.pixel_ctx.set(KEY_SOURCE_OFFSET, 0)
    edited = bytes([0xAA, 0xBB, 0xCC, 0xDD])
    parent.doc.replace_bytes(0x100, edited)
    ws.set_pixel_revision(parent, ws.next_revision())

    _cfg, px = _load(sl, ws, reg)
    assert px.data == _xor(STREAM, edited)


def test_an_integer_read_from_bytes_follows_the_file(tmp_path) -> None:
    reg = _registry()
    ws, _parent, sl = _rom(tmp_path)
    rom = tmp_path / "rom.bin"
    body = bytearray(rom.read_bytes())
    body[0x300:0x302] = (24).to_bytes(2, "big")
    rom.write_bytes(bytes(body))
    sl.inputs = with_bindings(
        sl,
        XOR_ID,
        {**sl.inputs[XOR_ID], "output_size": IntegerFromBytes(offset=0x300, width=2)},
    )
    _cfg, px = _load(sl, ws, reg)
    assert len(px.data) == 24

    body[0x300:0x302] = (40).to_bytes(2, "little")
    rom.write_bytes(bytes(body))
    sl.inputs = with_bindings(
        sl,
        XOR_ID,
        {
            **sl.inputs[XOR_ID],
            "output_size": IntegerFromBytes(offset=0x300, width=2, little_endian=True),
        },
    )
    _cfg, px = _load(sl, ws, reg)
    assert len(px.data) == 40


def test_a_region_may_name_another_entry_one_hop_deep(tmp_path) -> None:
    """The table is itself a compressed slice: its *resolved* bytes are the
    input, addressed 0-based like a composite piece. A source that reaches into
    an entry of its own is refused, and so is one that is not open."""
    reg = _registry()
    ws, parent, sl = _rom(tmp_path)
    # A second slice over the stream, decoded against the plain table, is the
    # table for the first: its *resolved* bytes — the stream XORed once — are
    # what the binding addresses, and their first four bytes are the new key.
    packed_table = ws.add_slice(parent.path, "packed table", 0x200, 8, XOR_ID)
    packed_table.inputs = {XOR_ID: {"table": RegionBinding(offset=0x100, length=4)}}
    sl.inputs = {
        XOR_ID: {"table": RegionBinding(entry=packed_table, offset=0, length=4)}
    }
    assert can_supply_input(sl, packed_table)
    _cfg, px = _load(sl, ws, reg)
    assert px.data == _xor(STREAM, _xor(STREAM, TABLE)[:4])

    # Two hops: the packed table now itself names an entry, so it may not be a
    # source — a cycle is refused off the bindings, before anything is loaded.
    packed_table.inputs = {
        XOR_ID: {"table": RegionBinding(entry=sl, offset=0, length=4)}
    }
    assert not can_supply_input(sl, packed_table)
    cfg, _px = _load(sl, ws, reg)
    assert cfg.compression_id == NO_COMPRESSION
    assert "cannot supply" in cfg.input_problems[0][2]

    packed_table.inputs = {XOR_ID: {"table": RegionBinding(offset=0x100, length=4)}}
    ws.close(packed_table)
    cfg, _px = _load(sl, ws, reg)
    assert "not open" in cfg.input_problems[0][2]


def test_a_slice_carved_under_a_codec_copies_the_files_preview_bindings(
    tmp_path,
) -> None:
    ws, parent, _sl = _rom(tmp_path)
    parent.inputs = {XOR_ID: {"table": RegionBinding(offset=0x100, length=4)}}
    carved = slice_of(parent, "new", 0x200, 16, XOR_ID)
    assert carved.inputs == parent.inputs
    assert carved.inputs[XOR_ID] is not parent.inputs[XOR_ID]  # a copy, not a link
    # Under another codec nothing is copied: the bindings are the codec's.
    assert slice_of(parent, "raw", 0x200, 16).inputs == {}


def test_declared_inputs_lists_the_stages_that_want_any(tmp_path) -> None:
    reg = _registry()
    ws, parent, sl = _rom(tmp_path)
    assert [d.plugin_id for d in declared_inputs(sl, reg)] == [XOR_ID]
    assert declared_inputs(parent, reg) == []
    assert declared_inputs(ws.add_slice(parent.path, "raw", 0, 8), reg) == []


def test_bindings_round_trip_through_a_project_in_every_shape(tmp_path) -> None:
    reg = _registry()
    ws, parent, sl = _rom(tmp_path)
    other = ws.add_slice(parent.path, "other", 0x200, 8, XOR_ID)
    parent.inputs = {XOR_ID: {"table": RegionBinding(offset=0x100, length=4)}}
    sl.inputs = {
        XOR_ID: {
            "table": RegionBinding(entry=other, offset=2, length=4),
            "output_size": IntegerFromBytes(offset=0x300, width=2, little_endian=True),
        }
    }
    other.inputs = {
        XOR_ID: {
            "table": RegionBinding(offset=0x100, length=4),
            "output_size": 32,
            "invert": True,
        }
    }
    project = tmp_path / "p.celpix"
    save_project(ws, str(project), reg)

    raw = json.loads(project.read_text())
    assert raw["version"] == projectfile.PROJECT_VERSION
    stored = raw["entries"][1]["inputs"][XOR_ID]
    assert stored["table"] == {"entry_index": 2, "offset": 2, "length": 4}
    assert stored["output_size"] == {"offset": 0x300, "width": 2, "endian": "little"}
    assert raw["entries"][2]["inputs"][XOR_ID]["output_size"] == 32
    assert raw["entries"][2]["inputs"][XOR_ID]["invert"] is True
    assert raw["entries"][0]["inputs"][XOR_ID]["table"] == {
        "offset": 0x100,
        "length": 4,
    }

    loaded = load_project(str(project))
    assert loaded.migrated_from is None
    file, first, second = loaded.entries
    assert file.inputs == parent.inputs
    # The entry-shaped binding came back as the *object* at the stored position.
    assert first.inputs[XOR_ID]["table"].entry is second
    assert first.inputs[XOR_ID]["table"].offset == 2
    assert first.inputs[XOR_ID]["output_size"] == IntegerFromBytes(
        offset=0x300, width=2, little_endian=True
    )
    assert second.inputs[XOR_ID]["output_size"] == 32
    assert second.inputs[XOR_ID]["invert"] is True


def test_a_binding_onto_a_closed_entry_is_dropped_on_load(tmp_path) -> None:
    ws, parent, sl = _rom(tmp_path)
    gone = Entry(name="gone", kind=EntryKind.SLICE, path=parent.path)
    sl.inputs = {
        XOR_ID: {
            "table": RegionBinding(entry=gone, offset=0, length=4),
            "output_size": 16,
        }
    }
    project = tmp_path / "p.celpix"
    save_project(ws, str(project))
    raw = json.loads(project.read_text())
    assert raw["entries"][1]["inputs"][XOR_ID]["table"]["entry_index"] == -1
    loaded = load_project(str(project))
    # Dropped rather than left as "this file", which would decode against
    # the wrong bytes; the other binding is untouched.
    assert loaded.entries[1].inputs == {XOR_ID: {"output_size": 16}}


def test_a_version_2_project_reads_with_no_bindings_and_is_walked_forward(
    tmp_path,
) -> None:
    (tmp_path / "x.bin").write_bytes(b"\x00" * 0x400)
    document = {
        "version": 2,
        "entries": [
            {"path": "x.bin"},
            {"kind": "slice", "path": "x.bin", "slice_offset": 0, "slice_length": 16},
        ],
    }
    project = tmp_path / "p.celpix"
    project.write_text(json.dumps(document), encoding="utf-8")
    loaded = load_project(str(project))
    assert loaded.migrated_from == 2
    assert loaded.version == projectfile.PROJECT_VERSION
    assert all(entry.inputs == {} for entry in loaded.entries)
    # And a project that never binds anything writes no key at all.
    ws = Workspace()
    ws.replace(loaded.entries, loaded.current)
    assert all("inputs" not in e for e in project_dict(ws, str(project))["entries"])


def test_a_save_prunes_what_the_entry_no_longer_names(tmp_path) -> None:
    reg = _registry()
    ws, parent, sl = _rom(tmp_path)
    sl.inputs = {
        XOR_ID: {"table": RegionBinding(offset=0x100, length=4), "stale": 1},
        "compression.other": {"table": RegionBinding(offset=0, length=2)},
        "compression.not-installed": {"whatever": 3},
    }
    # The slice reads through XOR: the other codec's bindings go, and so does a
    # key XOR no longer declares. A plugin this build lacks would keep every key,
    # but only while the entry still names it.
    assert prune_bindings(sl, reg) == {
        XOR_ID: {"table": RegionBinding(offset=0x100, length=4)}
    }
    # A file keeps every codec's — each is that codec's preview binding.
    parent.inputs = dict(sl.inputs)
    assert set(prune_bindings(parent, reg)) == set(sl.inputs)
    assert prune_bindings(parent, reg)["compression.not-installed"] == {"whatever": 3}
    # Without a registry nothing is pruned: the next save that has one will.
    assert set(
        project_dict(ws, str(tmp_path / "p.celpix"))["entries"][1]["inputs"]
    ) == set(sl.inputs)


def test_the_clipboard_form_carries_bindings_and_their_join(tmp_path) -> None:
    ws, parent, sl = _rom(tmp_path)
    other = ws.add_slice(parent.path, "other", 0x200, 8)
    sl.inputs = {
        XOR_ID: {
            "table": RegionBinding(entry=other, offset=0, length=4),
            "output_size": 16,
        }
    }
    payload = projectfile.entries_payload([sl, other], ws.entries, "session")
    copied = projectfile.entries_from_payload(payload)
    record = next(c for c in copied if c.entry.name == "stream")
    assert record.input_sources == ((XOR_ID, "table", 2),)
    assert record.entry.inputs[XOR_ID]["output_size"] == 16


def test_the_scan_probes_with_the_bindings_it_is_given() -> None:
    class _Bounded(_XorTable):
        """The XOR stub with an end to find: a scan hit needs a decode that
        *reports complete*, which the stream-shaped stub never does."""

        def decompress(self, data: bytes, ctx: PipelineContext) -> bytes:
            out = super().decompress(data, ctx)
            ctx.set(KEY_DECOMPRESS_COMPLETE, bool(out))
            return out

    plugin = _Bounded()
    table = bytes([0x5A, 0xA5])
    data = b"\x00" * 8 + _xor(b"hello", table)
    # Without the table every probe raises on the missing key and nothing is
    # found; with it, the first offset that decodes to a complete, non-empty
    # structure wins — which for this stub is wherever the scan starts.
    assert pipeline.find_next_structure(data, plugin, 8, 0).found is None
    hit = pipeline.find_next_structure(data, plugin, 8, 3, inputs={"table": table})
    assert hit.found == 3
