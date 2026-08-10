"""The plugin registry — the single place plugins and presets are looked up.

Built-ins register themselves in-process (see :func:`default_registry`); a
user's plugins folder registers through the same seam
(:mod:`celpix.plugins.discovery`), so no stage code knows where a plugin came
from.

Being the single place they are looked up is also what makes it the single place
a **renamed** one is forwarded: both lookups fall back to
:mod:`celpix.plugins.aliases` on a miss, so an id saved before a rename resolves
without any caller knowing a rename happened.
"""

from __future__ import annotations

from typing import TypeVar, overload

from celpix.core.errors import Stage
from celpix.plugins.aliases import current_id
from celpix.plugins.base import (
    STAGE_DEFAULT_PRESET,
    STAGE_PASSTHROUGH,
    Plugin,
    Preset,
    writes_back,
)

P = TypeVar("P", bound=Plugin)


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

    @overload
    def plugin(self, stage: Stage, plugin_id: str) -> Plugin: ...

    @overload
    def plugin(self, stage: Stage, plugin_id: str, kind: type[P]) -> P: ...

    def plugin(
        self, stage: Stage, plugin_id: str, kind: type[Plugin] | None = None
    ) -> Plugin:
        """The plugin registered at ``stage`` under ``plugin_id``.

        ``kind`` names the stage's protocol — :class:`ContainerPlugin` and the
        rest — so the caller gets back something that declares the methods it is
        about to call, rather than a bare :class:`Plugin` that declares none.
        Like :meth:`engine_for`'s, it is a **typing assertion, unread at
        runtime**: `STAGE_METHODS` already shape-checked the plugin against
        exactly that protocol before it reached this bucket
        (:func:`~celpix.plugins.base.missing_methods`).

        Deriving it from ``stage`` instead would be the nicer call, and it does
        not survive the tooling: a ``Literal[Stage.X]`` overload per stage is not
        discriminated on enum members, so every lookup silently resolves to the
        first overload and is checked against the wrong protocol. Naming it is
        the version that is actually right.
        """
        bucket = self._plugins[stage]
        # The live id first, so a plugin that has taken a retired name wins over
        # the alias — a user's own plugin is entitled to any id it likes, and
        # theirs is the one in front of them.
        plugin = bucket.get(plugin_id) or bucket.get(current_id(plugin_id))
        if plugin is None:
            raise KeyError(f"no {stage.value} plugin with id {plugin_id!r}")
        return plugin

    def plugins(self, stage: Stage) -> list[Plugin]:
        return list(self._plugins[stage].values())

    # -- presets -----------------------------------------------------------
    def register_preset(self, preset: Preset) -> None:
        if preset.id in self._presets:
            raise ValueError(f"duplicate preset id: {preset.id}")
        self._presets[preset.id] = preset

    def preset(self, preset_id: str) -> Preset:
        # Live id first, then the forwarding address — same order and same
        # reason as `plugin` above.
        preset = self._presets.get(preset_id) or self._presets.get(
            current_id(preset_id)
        )
        if preset is None:
            raise KeyError(f"no preset with id {preset_id!r}")
        return preset

    def has_preset(self, preset_id: str) -> bool:
        """Whether ``preset_id`` (or what it forwards to) is registered here."""
        return preset_id in self._presets or current_id(preset_id) in self._presets

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

    def resolve_preset(self, stage: Stage, preset_id: str) -> str:
        """``preset_id`` if this build has it, else ``stage``'s default preset.

        The Interpret-side twin of :meth:`resolve_stage`, and there for the same
        reason: a stored preset id outlives the preset that provided it — a
        plugin folder's format can be uninstalled, left untrusted, or belong to a
        project that has since been closed — and an entry naming one must still
        open rather than take the window down with a ``KeyError``.

        Unlike a byte stage there is no pass-through to fall back *to*, so this
        substitutes a real format (:data:`~celpix.plugins.base.STAGE_DEFAULT_PRESET`)
        and the substitution is almost certainly wrong. That makes it something to
        report and not merely absorb: the caller compares the answer against what
        it asked for and tells the user which format is missing
        (:func:`~celpix.project.workspace.repair_presets`).

        The stand-in is checked to be registered too, and any preset of the stage
        is taken over one that isn't: a registry assembled without the built-ins
        (a test's, a future trimmed build) would otherwise answer with an id that
        fails the same way the stored one did. With nothing at all at the stage,
        ``preset_id`` comes back untouched: there is no substitute to name, so the
        caller's own handling of the miss is the honest one.
        """
        if self.has_preset(preset_id):
            return preset_id
        fallback = STAGE_DEFAULT_PRESET.get(stage)
        if fallback is None:
            return preset_id
        if self.has_preset(fallback):
            return fallback
        registered = self.presets(stage)
        return registered[0].id if registered else preset_id

    @overload
    def engine_for(self, preset_id: str) -> tuple[Plugin, Preset]: ...

    @overload
    def engine_for(self, preset_id: str, kind: type[P]) -> tuple[P, Preset]: ...

    def engine_for(
        self, preset_id: str, kind: type[Plugin] | None = None
    ) -> tuple[Plugin, Preset]:
        """The interpret engine a preset resolves to, plus the preset itself.

        A preset names its own interpret stage and an ``engine_id`` within it, so
        this is the single place that hop is made — no caller has to respell
        ``plugin(Stage.INTERPRET_*, preset.engine_id)`` or know which interpret
        stage a preset belongs to.

        ``kind`` is that convenience's one cost. The stage arrives *inside* the
        preset, so unlike :meth:`plugin` nothing in the call says which protocol
        comes back, and the caller is left holding a bare :class:`Plugin`. Naming
        the protocol restores that — it is a **typing assertion and nothing
        else**, unread at runtime, because the guarantee behind it was already
        made at registration: a preset's ``engine_id`` resolves within the
        preset's own stage, and a plugin only reaches that stage's bucket by
        passing the stage's shape check.
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
