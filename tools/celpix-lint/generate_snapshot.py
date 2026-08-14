"""Regenerate ``src/celpix_lint/data/registry.json`` from the live registry.

Run from the celPix repo, in an environment where celPix is importable::

    export UV_PROJECT_ENVIRONMENT=.venv-linux
    uv run tools/celpix-lint/generate_snapshot.py

The linter checks plugin and preset ids against this file whenever celPix is not
installed beside it, which is the case it is built for. The snapshot is
therefore allowed to be a copy — but not to be a *stale* copy, so
``tests/test_lint_snapshot.py`` in the celPix suite fails the moment the
built-in registry and this file disagree. That test is the reason regenerating
is a chore and not a judgement call: run it whenever the suite says to.

Only **built-ins** are captured. A user's dropped plugins are theirs and vary by
machine, which is what the `registry` extra is for.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

SNAPSHOT = Path(__file__).parent / "src" / "celpix_lint" / "data" / "registry.json"


def snapshot() -> dict:
    """The built-in registry as the linter's ``registry.json`` body."""
    from celpix import __version__
    from celpix.core.errors import Stage
    from celpix.plugins.aliases import RENAMED
    from celpix.plugins.registry import default_registry
    from celpix.project.projectfile import PROJECT_VERSION

    registry = default_registry()
    return {
        "celpix_version": __version__,
        "project_version": PROJECT_VERSION,
        # Container-only in celPix, but stored for every stage so the reader
        # needs no special case: the other stages simply declare the default.
        "plugins": {
            stage.value: {
                plugin.info.id: [kind.value for kind in plugin.info.content_kinds]
                for plugin in registry.plugins(stage)
            }
            for stage in Stage
        },
        "presets": {
            stage.value: sorted(preset.id for preset in registry.presets(stage))
            for stage in Stage
        },
        # Carried so the linter can tell "an id this build never had" from "an id
        # that has been renamed since" — the second is a working project that
        # will stop depending on the table as soon as it is re-saved, and saying
        # so is worth more than flagging it as broken.
        "renamed": dict(RENAMED),
    }


def main() -> int:
    try:
        body = snapshot()
    except ImportError as exc:
        print(f"celpix is not importable: {exc}", file=sys.stderr)
        print(
            "Run this from the celPix repo with its environment active.",
            file=sys.stderr,
        )
        return 2
    SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    with open(SNAPSHOT, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(body, handle, indent=2, sort_keys=True)
        handle.write("\n")
    counts = sum(len(ids) for ids in body["plugins"].values())
    presets = sum(len(ids) for ids in body["presets"].values())
    print(
        f"{SNAPSHOT}: {counts} plugins, {presets} presets, "
        f"celPix {body['celpix_version']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
