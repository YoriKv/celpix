"""`tools/celpix-lint` ships a copy of the built-in ids; keep it honest.

The linter is a standalone package with no dependency on celPix — it has to run
where celPix is not installed, which is most of what it is for — so it resolves
plugin and preset ids against a generated snapshot of the built-in registry.
That copy is allowed to exist; it is not allowed to go stale, because a stale
one reports a working project's ids as unknown and an id that was retired as
fine.

This is the whole cost of that arrangement, paid here rather than at the next
person to add a preset. When it fails::

    export UV_PROJECT_ENVIRONMENT=.venv-linux
    uv run tools/celpix-lint/generate_snapshot.py

The file is read as data rather than through ``celpix_lint``, which is not on
this environment's path; the one test of the linter's hand-written schema puts
its source on the path for itself.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from celpix.core.errors import Stage
from celpix.plugins.aliases import RENAMED
from celpix.plugins.registry import default_registry
from celpix.project.projectfile import PROJECT_VERSION

SNAPSHOT = (
    Path(__file__).parent.parent
    / "tools"
    / "celpix-lint"
    / "src"
    / "celpix_lint"
    / "data"
    / "registry.json"
)

REGENERATE = "stale — run `uv run tools/celpix-lint/generate_snapshot.py`"


@pytest.fixture(scope="module")
def snapshot() -> dict:
    if not SNAPSHOT.exists():  # pragma: no cover - the tool would be half-installed
        pytest.skip(f"no linter snapshot at {SNAPSHOT}")
    return json.loads(SNAPSHOT.read_text(encoding="utf-8"))


def test_every_stage_matches_the_built_in_registry(snapshot):
    """Both directions matter: an id the snapshot lacks is reported as unknown
    on a project that works, and one it keeps after a rename hides the rename."""
    registry = default_registry()
    for stage in Stage:
        assert set(snapshot["plugins"].get(stage.value, {})) == {
            plugin.info.id for plugin in registry.plugins(stage)
        }, f"{stage.value} plugins {REGENERATE}"
        assert set(snapshot["presets"].get(stage.value, ())) == {
            preset.id for preset in registry.presets(stage)
        }, f"{stage.value} presets {REGENERATE}"


def test_container_content_kinds_match(snapshot):
    """The linter refuses a container framing the wrong kind of entry, so it
    needs each one's declared kinds and not just its id."""
    registry = default_registry()
    for plugin in registry.plugins(Stage.CONTAINER):
        assert snapshot["plugins"]["container"][plugin.info.id] == [
            kind.value for kind in plugin.info.content_kinds
        ], f"{plugin.info.id} content kinds {REGENERATE}"


def test_alias_table_matches(snapshot):
    """The table is append-only, so this only ever fails on a new rename."""
    assert snapshot["renamed"] == dict(RENAMED), f"alias table {REGENERATE}"


def test_project_version_matches(snapshot):
    """The linter warns that a newer file will be rewritten on save; it can only
    do that while it knows which version this build writes."""
    assert snapshot["project_version"] == PROJECT_VERSION, f"version {REGENERATE}"


def test_input_declarations_match(snapshot):
    """The range a plugin accepts for an input is what the app checks a binding
    against before dropping the stage; the linter reports the same failure
    from the snapshot, so the snapshot has to carry the same numbers."""
    registry = default_registry()
    for stage in Stage:
        expected = {
            plugin.info.id: [
                {
                    "key": spec.key,
                    "kind": spec.kind.value,
                    "required": bool(spec.required),
                    "minimum": int(spec.minimum),
                    "maximum": int(spec.maximum),
                    "stride": int(spec.stride),
                    # a choice's keys are what a stored binding is checked against
                    **(
                        {"options": [key for key, _label in spec.options]}
                        if spec.options
                        else {}
                    ),
                }
                for spec in plugin.info.inputs
            ]
            for plugin in registry.plugins(stage)
            if plugin.info.inputs
        }
        assert snapshot.get("inputs", {}).get(stage.value, {}) == expected, (
            f"{stage.value} inputs {REGENERATE}"
        )


def test_every_key_the_writer_emits_is_one_the_linter_reads_on_that_kind(
    tmp_path, monkeypatch
):
    """The snapshot's counterpart for the linter's hand-written schema: a key
    celPix writes on a kind the linter thinks never reads it is reported to the
    user as doing nothing, which is the opposite of what happens. One entry of
    every kind with every optional stage set, saved by the real writer."""
    from celpix.core.capabilities import ContentKind
    from celpix.project.inputs import RegionBinding
    from celpix.project.projectfile import save_project
    from celpix.project.workspace import (
        CompositePiece,
        Entry,
        EntryKind,
        FileStages,
        TileMode,
        TileSource,
        Workspace,
    )

    monkeypatch.syspath_prepend(str(SNAPSHOT.parent.parent.parent))
    from celpix_lint import schema

    for name in ("rom.bin", "c.pal"):
        (tmp_path / name).write_bytes(bytes(0x100))
    rom_path, pal_path = str(tmp_path / "rom.bin"), str(tmp_path / "c.pal")
    binding = {"compression.lz2": {"table": RegionBinding(offset=0, length=4)}}
    ws = Workspace()
    rom = ws.open_file(rom_path)
    rom.set_file_stages(
        FileStages("container.smd", "reshape.swap-bytes-2", "compression.lz2")
    )
    rom.inputs = binding
    rom.content_kind = ContentKind.TILEMAP
    rom.tile_source = TileSource(mode=TileMode.ENTRY, entry=rom)
    cut = ws.add_slice(
        rom_path, "cut", 0, 16, "compression.lz2", "reshape.swap-bytes-2"
    )
    cut.inputs = binding
    ws.add_slice_under(cut, "inner", 0, 8)
    ws.entries.append(Entry(name="mark", kind=EntryKind.BOOKMARK, path=rom_path))
    palette = ws.add_palette(pal_path, "preset.palette.bgr555")
    palette.set_file_stages(
        FileStages("container.smd", "reshape.swap-bytes-2", "compression.lz2")
    )
    palette.inputs = binding
    ws.entries.append(
        Entry(
            name="joined",
            kind=EntryKind.COMPOSITE,
            path="",
            pieces=(CompositePiece(entry=cut),),
        )
    )
    project = tmp_path / "p.celpix"
    save_project(ws, str(project))
    assert schema.KNOWN_PROJECT_VERSION == PROJECT_VERSION
    for entry in json.loads(project.read_text(encoding="utf-8"))["entries"]:
        for key in entry:
            assert key in schema.ENTRY_KEYS, f"{key} is not in the linter's schema"
            kinds = schema.KIND_ONLY.get(key)
            assert kinds is None or entry["kind"] in kinds, (
                f"celPix writes `{key}` on a {entry['kind']} entry; "
                "the linter's KIND_ONLY says it is never read there"
            )
