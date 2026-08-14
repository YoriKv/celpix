"""Registry error-handling: unknown lookups raise, duplicates are rejected.

The built-in plugins and presets being present is covered transitively — the codec
and pipeline tests decode/round-trip through every one of them, so a missing
registration or broken resource load fails there.
"""

from __future__ import annotations

import pytest

from celpix.core.errors import Stage
from celpix.plugins.aliases import RENAMED, current_id
from celpix.plugins.base import PluginInfo
from celpix.plugins.registry import default_registry


def test_unknown_lookup_raises() -> None:
    reg = default_registry()
    with pytest.raises(KeyError):
        reg.plugin(Stage.CONTAINER, "nope")
    with pytest.raises(KeyError):
        reg.preset("nope")


def test_duplicate_registration_rejected() -> None:
    reg = default_registry()
    with pytest.raises(ValueError):
        reg.register(reg.plugin(Stage.CONTAINER, "container.raw-file"))


def test_the_rename_table_stays_honest() -> None:
    """Both invariants the alias table promises, checked against the registry.

    A rename is only half-done until this passes: an alias shadowed by a live
    plugin never fires, and one pointing at a name that no longer exists forwards
    a saved project to nothing.
    """
    reg = default_registry()
    live = {p.id for p in reg.presets()} | {
        plugin.info.id for stage in Stage for plugin in reg.plugins(stage)
    }

    # Nothing may still answer to a retired name, or the alias is dead code and
    # one string means two things.
    assert not (set(RENAMED) & live), sorted(set(RENAMED) & live)

    # Every target resolves, following chains — rename twice and the first row
    # has to be re-pointed at the final name rather than left aimed midway.
    unresolved = {
        old: current_id(old) for old in RENAMED if current_id(old) not in live
    }
    assert not unresolved, unresolved

    # ...and "re-pointed" is the half a chain-following check cannot see, so it
    # is asserted directly: a target that is itself a retired name resolves today
    # only because `current_id` walks, and reads as the pattern to copy for the
    # next twice-rename.
    midway = {old: new for old, new in RENAMED.items() if new in RENAMED}
    assert not midway, midway


def test_a_renamed_id_still_resolves() -> None:
    """The whole point: an id saved before a rename opens without the caller
    knowing one happened."""
    reg = default_registry()
    # A preset and an engine, one of each kind the table forwards.
    assert reg.preset("preset.palette.r5g5b5-split-be").id == (
        "preset.palette.rgb555-split-be"
    )
    assert reg.plugin(Stage.INTERPRET_PIXEL, "codec.planar").info.id == (
        "codec.pixel.planar"
    )
    # engine_for goes through both hops, which is what a user's own preset TOML
    # naming an old engine_id relies on.
    engine, preset = reg.engine_for("preset.pixel.chunky-8bpp")
    assert preset.id == "preset.pixel.8bpp-linear"
    assert engine.info.id == "codec.pixel.packed"
    # An id that was never renamed is not invented: a genuinely missing plugin
    # still has to raise, so the stage can degrade to pass-through.
    with pytest.raises(KeyError):
        reg.plugin(Stage.CONTAINER, "container.never-existed")


def test_a_live_plugin_beats_a_retired_name() -> None:
    """A user is entitled to any id they like, including one celPix retired.
    Theirs is the plugin in front of them, so it wins over the forwarding."""
    reg = default_registry()

    class Squatter:
        info = PluginInfo(
            id="codec.planar", name="Someone's own planar", stage=Stage.INTERPRET_PIXEL
        )

    reg.register(Squatter())
    assert reg.plugin(Stage.INTERPRET_PIXEL, "codec.planar").info.name == (
        "Someone's own planar"
    )
    # ...and the built-in is still reachable under the name it actually has.
    assert reg.plugin(Stage.INTERPRET_PIXEL, "codec.pixel.planar").info.name != (
        "Someone's own planar"
    )


def test_current_id_survives_a_cycle() -> None:
    """The table is hand-edited. A cycle is a bug in it, but it must fail a test
    rather than hang the app on a lookup."""
    import celpix.plugins.aliases as aliases

    original = aliases.RENAMED
    try:
        aliases.RENAMED = {"a": "b", "b": "a"}
        assert current_id("a") in ("a", "b")  # terminates, whichever it lands on
    finally:
        aliases.RENAMED = original


def test_a_missing_preset_falls_back_to_the_stage_default() -> None:
    """A stored preset id outlives the preset — an uninstalled plugin, an
    untrusted one, a project's own folder that has since been closed. Interpret
    has no pass-through to degrade to, so the stand-in is a real format."""
    reg = default_registry()
    assert reg.resolve_preset(Stage.INTERPRET_TILEMAP, "preset.tilemap.gone") == (
        "preset.tilemap.snes-bg"
    )
    assert reg.resolve_preset(Stage.INTERPRET_PIXEL, "preset.pixel.gone") == (
        "preset.pixel.snes-4bpp"
    )
    # A registered id — and one reached through the rename table — is left alone.
    assert reg.resolve_preset(Stage.INTERPRET_PALETTE, "preset.palette.bgr555") == (
        "preset.palette.bgr555"
    )
    assert reg.resolve_preset(
        Stage.INTERPRET_PALETTE, "preset.palette.r5g5b5-split-be"
    ) == ("preset.palette.r5g5b5-split-be")
    # A stage with no default at all comes back unchanged, for the caller's own
    # handling of the miss to be the honest one.
    assert reg.resolve_preset(Stage.COMPRESSION, "compression.gone") == (
        "compression.gone"
    )
