"""The plugin registry — the single place plugins and presets are looked up.

Built-ins register themselves in-process (see :func:`default_registry`); a
user's plugins folder registers through the same seam
(:mod:`celpix.plugins.discovery`), so no stage code knows where a plugin came
from.
"""

from __future__ import annotations

from celpix.core.errors import Stage
from celpix.plugins.base import (
    STAGE_PASSTHROUGH,
    Plugin,
    Preset,
    writes_back,
)


class Registry:
    """Holds plugins keyed by ``(stage, id)`` and presets keyed by ``id``."""

    def __init__(self) -> None:
        self._plugins: dict[Stage, dict[str, Plugin]] = {stage: {} for stage in Stage}
        self._presets: dict[str, Preset] = {}

    # -- plugins -----------------------------------------------------------
    def register(self, plugin: Plugin, stage: Stage | None = None) -> None:
        """Register ``plugin``, at ``stage`` when the caller knows better.

        Folder-drop discovery passes the stage its folder implies, so a dropped
        plugin need not repeat it (:class:`~celpix.plugins.base.PluginInfo`). A
        direct registration — as the built-ins do — has no folder behind it, so
        the descriptor has to say.
        """
        stage = stage or plugin.info.stage
        if stage is None:
            raise ValueError(
                f"plugin {plugin.info.id!r} names no stage, and none was supplied"
            )
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

    # -- convenience -------------------------------------------------------
    def resolve_stage(self, stage: Stage, plugin_id: str) -> tuple[str, bool]:
        """``plugin_id`` as ``(usable id, writes back)`` for a byte-handling stage.

        A stored plugin id outlives the plugin that provided it: a project names
        what its files were opened with, and that plugin can be uninstalled,
        renamed, or left untrusted at the next launch. Opening degrades to the
        stage's pass-through rather than failing, so the file still opens showing
        its untransformed bytes.

        **A degraded stage is view-only**, whatever the pass-through could do: the
        entry means to be read through something this build hasn't got, so what is
        on screen is not what the file holds. The user is told which plugin is
        missing (:func:`~celpix.pipeline.pipeline.load_pixel_data`) and Write comes
        back once they install it or pick another plugin for the stage.
        """
        try:
            plugin = self.plugin(stage, plugin_id)
        except KeyError:
            return STAGE_PASSTHROUGH[stage], False
        return plugin_id, writes_back(plugin, stage)

    def engine_for(self, preset_id: str) -> tuple[Plugin, Preset]:
        """The interpret engine a preset resolves to, plus the preset itself.

        A preset names its own interpret stage and an ``engine_id`` within it, so
        this is the single place that hop is made — no caller has to respell
        ``plugin(Stage.INTERPRET_*, preset.engine_id)`` or know which interpret
        stage a preset belongs to.
        """
        preset = self.preset(preset_id)
        return self.plugin(preset.stage, preset.engine_id), preset


def default_registry() -> Registry:
    """A registry populated with every built-in plugin and preset.

    Imported lazily so this module stays free of the built-in engines, and their
    resource loads, until something asks for them.
    """
    from celpix.plugins.builtins import register_builtins

    reg = Registry()
    register_builtins(reg)
    return reg
