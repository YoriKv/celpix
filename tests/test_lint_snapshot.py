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
this environment's path.
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
