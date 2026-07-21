"""The plugin registry — the single place stages and presets are discovered.

For the MVP, built-ins register themselves in-process (see
:func:`default_registry`). This is the one seam a later discovery mechanism —
Python entry points or a plugins folder — plugs into, without any stage code
changing.
"""

from __future__ import annotations

from celpix.core.errors import Stage
from celpix.plugins.base import Plugin, Preset


class Registry:
    """Holds plugins keyed by ``(stage, id)`` and presets keyed by ``id``."""

    def __init__(self) -> None:
        self._plugins: dict[Stage, dict[str, Plugin]] = {stage: {} for stage in Stage}
        self._presets: dict[str, Preset] = {}

    # -- plugins -----------------------------------------------------------
    def register(self, plugin: Plugin) -> None:
        stage = plugin.info.stage
        bucket = self._plugins[stage]
        if plugin.info.id in bucket:
            raise ValueError(f"duplicate plugin id for {stage.value}: {plugin.info.id}")
        bucket[plugin.info.id] = plugin

    def plugin(self, stage: Stage, plugin_id: str) -> Plugin:
        try:
            return self._plugins[stage][plugin_id]
        except KeyError:
            raise KeyError(f"no {stage.value} plugin with id {plugin_id!r}") from None

    def plugins(self, stage: Stage) -> list[Plugin]:
        return list(self._plugins[stage].values())

    # -- presets -----------------------------------------------------------
    def register_preset(self, preset: Preset) -> None:
        if preset.id in self._presets:
            raise ValueError(f"duplicate preset id: {preset.id}")
        self._presets[preset.id] = preset

    def preset(self, preset_id: str) -> Preset:
        try:
            return self._presets[preset_id]
        except KeyError:
            raise KeyError(f"no preset with id {preset_id!r}") from None

    def presets(self, stage: Stage | None = None) -> list[Preset]:
        items = self._presets.values()
        if stage is None:
            return list(items)
        return [p for p in items if p.stage == stage]


def default_registry() -> Registry:
    """A registry populated with every built-in plugin and preset.

    Imported lazily so ``celpix.plugins.registry`` stays free of the built-in
    engines (and their resource loads) until something actually asks for them.
    """
    from celpix.plugins.builtins import register_builtins

    reg = Registry()
    register_builtins(reg)
    return reg
