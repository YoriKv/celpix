"""Which plugin and preset ids exist — from the running celPix, or the snapshot.

Two sources, and which one answered is reported, because they answer different
questions. A **live registry** knows the user's own dropped plugins and the
``plugins/`` folder beside the project, so it can say an id is genuinely
missing. The **snapshot** knows only what celPix ships, so an id it has never
heard of may be a typo or may be a plugin the author has installed and it
cannot tell which — and it says so rather than claiming the stronger finding.

The snapshot is the default because the linter's job is to run where celPix is
not: on a project file in a checkout, in CI, in an agent's sandbox.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from os.path import dirname, join

from celpix_lint.schema import PRESET_STAGES, STAGES

_SNAPSHOT = join(dirname(__file__), "data", "registry.json")


@dataclass
class KnownIds:
    """The ids one source knows, keyed by stage."""

    #: ``stage -> {plugin id: [content kinds it frames]}``. Only containers
    #: declare content kinds; every other stage stores the default pair.
    plugins: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    #: ``stage -> {preset ids}``.
    presets: dict[str, set] = field(default_factory=dict)
    #: ``retired id -> current id``, the forwarding table every lookup falls
    #: back to (``celpix.plugins.aliases``).
    renamed: dict[str, str] = field(default_factory=dict)
    #: ``"snapshot (celPix 0.5.9)"`` or ``"live registry"`` — quoted in the
    #: report so a reader knows how much an "unknown id" finding is worth.
    source: str = "snapshot"
    #: False when nothing could be loaded at all, which turns every id check
    #: off rather than reporting every id in the file as unknown.
    usable: bool = True
    #: Whether this source can see a user's own plugins. The snapshot cannot,
    #: so an unrecognised id is downgraded from "missing" to "not a built-in".
    authoritative: bool = False
    project_version: int = 1
    #: ``stage -> {ids}`` the project's own ``plugins/`` folder declares, set
    #: per file by :func:`for_project`. A project travels with its formats, so
    #: these are as present as a built-in for the project that carries them —
    #: and only for that one, which is why they are not merged into the tables
    #: above.
    local: dict = field(default_factory=dict)

    # -- lookups -----------------------------------------------------------
    def current_id(self, plugin_id: str) -> str:
        """``plugin_id`` walked through the rename table to its live name.

        The walk is a chain because the table is allowed to have been written
        one rename at a time, though celPix keeps it flat by rule.
        """
        seen = set()
        while plugin_id in self.renamed and plugin_id not in seen:
            seen.add(plugin_id)
            plugin_id = self.renamed[plugin_id]
        return plugin_id

    def has(self, stage: str, plugin_id: str) -> bool:
        """Whether ``stage`` has ``plugin_id``, or what it forwards to.

        Live name first, then the forwarding address — the registry's own
        order, so a user plugin that has taken a retired name wins.
        """
        bucket = self._bucket(stage)
        if plugin_id in bucket or self.current_id(plugin_id) in bucket:
            return True
        return plugin_id in self.local.get(stage, ())

    def is_local(self, stage: str, plugin_id: str) -> bool:
        """Whether only the project's own plugin folder provides ``plugin_id``."""
        return plugin_id in self.local.get(stage, ()) and plugin_id not in self._bucket(
            stage
        )

    def stage_of(self, plugin_id: str) -> str | None:
        """The stage ``plugin_id`` belongs to, or None if nothing has it.

        Used to turn "unknown id" into the more useful "that is a *tilemap*
        preset, on a pixel entry" whenever the id turns out to exist elsewhere.
        """
        current = self.current_id(plugin_id)
        for stage in STAGES:
            bucket = self._bucket(stage)
            if plugin_id in bucket or current in bucket:
                return stage
        return None

    def content_kinds(self, plugin_id: str) -> list[str] | None:
        """What content kinds a container frames, or None if it is not one."""
        containers = self.plugins.get("container", {})
        return containers.get(plugin_id) or containers.get(self.current_id(plugin_id))

    def _bucket(self, stage: str):
        if stage in PRESET_STAGES:
            return self.presets.get(stage, set())
        return self.plugins.get(stage, {})


def load_snapshot(path: str = _SNAPSHOT) -> KnownIds:
    """The ids that shipped with this linter."""
    try:
        with open(path, encoding="utf-8") as handle:
            body = json.load(handle)
    except (OSError, ValueError):
        # A missing or corrupt snapshot must not turn every id in the project
        # into a finding — it is the linter that is broken, not the project.
        return KnownIds(source="unavailable", usable=False)
    version = body.get("celpix_version", "?")
    return KnownIds(
        plugins={stage: dict(ids) for stage, ids in body.get("plugins", {}).items()},
        presets={stage: set(ids) for stage, ids in body.get("presets", {}).items()},
        renamed=dict(body.get("renamed", {})),
        source=f"snapshot (celPix {version})",
        project_version=body.get("project_version", 1),
    )


def load_live() -> KnownIds | None:
    """The running celPix's registry, or None when it is not importable.

    Built-ins plus whatever the user's plugin roots hold, which is what makes
    this source authoritative about a missing id where the snapshot is not.
    """
    try:
        from celpix.core.errors import Stage
        from celpix.plugins.aliases import RENAMED
        from celpix.plugins.registry import default_registry
        from celpix.project.projectfile import PROJECT_VERSION
    except ImportError:
        return None
    registry = default_registry()
    return KnownIds(
        plugins={
            stage.value: {
                plugin.info.id: [kind.value for kind in plugin.info.content_kinds]
                for plugin in registry.plugins(stage)
            }
            for stage in Stage
        },
        presets={
            stage.value: {preset.id for preset in registry.presets(stage)}
            for stage in Stage
        },
        renamed=dict(RENAMED),
        source="live registry",
        authoritative=True,
        project_version=PROJECT_VERSION,
    )


def for_project(ids: KnownIds, project_path: str) -> KnownIds:
    """``ids`` plus whatever the ``plugins/`` folder beside the project declares.

    A copy rather than a mutation: one run lints many files, and one project's
    formats must not silently vouch for the next project's ids.
    """
    from celpix_lint.sidecar import declared_ids

    local = declared_ids(project_path)
    return replace(ids, local=local) if local else ids


def known_ids(prefer_live: bool) -> KnownIds:
    """The id source to check against.

    ``prefer_live`` falls back to the snapshot rather than failing: asking for
    the live registry and not having celPix installed should degrade the id
    checks, not the run.
    """
    if prefer_live:
        live = load_live()
        if live is not None:
            return live
    return load_snapshot()
