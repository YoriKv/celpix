"""User plugin discovery: drop a file into a directory and it loads."""

from __future__ import annotations

import pytest

from celpix.core.context import PipelineContext
from celpix.core.errors import Stage
from celpix.plugins import discovery
from celpix.plugins.registry import default_registry
from celpix.plugins.trust import TrustStore

# Auto-approve confirm callback for tests that aren't exercising the gate itself.
_ALLOW = lambda pending: True  # noqa: E731

# A minimal code plugin: a Read plugin plus the register() hook the host calls.
_CODE_PLUGIN = """
from celpix.core.errors import Stage
from celpix.plugins.base import PluginInfo


class HelloReader:
    info = PluginInfo(id="read.hello", name="Hello reader", stage=Stage.READ)

    def read(self, source, ctx):
        return b"hello"


def register(registry):
    registry.register(HelloReader())
"""

# A zero-code preset: a new 1bpp planar format for the built-in planar engine.
# (No tile geometry — the planar engine's atomic tile is a fixed 8x8.)
_PRESET = """
id = "preset.pixel.custom-1bpp"
name = "Custom 1bpp"
stage = "interpret-pixel"
engine_id = "codec.planar"

[params]
bpp = 1
planes = [ { base = 0, stride = 1 } ]
"""


def test_drop_in_preset_is_registered_and_usable(tmp_path) -> None:
    (tmp_path / "custom.toml").write_text(_PRESET, encoding="utf-8")
    reg = default_registry()

    issues = discovery.load_directory(reg, str(tmp_path))
    assert issues == []

    preset = reg.preset("preset.pixel.custom-1bpp")
    engine = reg.plugin(Stage.INTERPRET_PIXEL, preset.engine_id)
    tiles = engine.decode(b"\x80" + b"\x00" * 7, preset.params, PipelineContext())
    assert tiles[0].get(0, 0) == 1  # leftmost bit set -> index 1


def test_drop_in_code_plugin_loads_when_approved(tmp_path) -> None:
    (tmp_path / "hello.py").write_text(_CODE_PLUGIN, encoding="utf-8")
    reg = default_registry()

    issues = discovery.load_directory(reg, str(tmp_path), confirm=_ALLOW)
    assert issues == []

    plugin = reg.plugin(Stage.READ, "read.hello")
    assert plugin.read(None, PipelineContext()) == b"hello"


def test_code_plugin_skipped_when_not_approved(tmp_path) -> None:
    (tmp_path / "hello.py").write_text(_CODE_PLUGIN, encoding="utf-8")
    reg = default_registry()

    # No confirm callback and nothing trusted -> default deny.
    issues = discovery.load_directory(reg, str(tmp_path))
    assert len(issues) == 1
    assert "not approved" in issues[0].message
    with pytest.raises(KeyError):
        reg.plugin(Stage.READ, "read.hello")


def _plugin_dir(tmp_path):
    # Mirror production: plugins live in a subdir; the trust store sits outside it
    # (in the data dir) so it is never itself scanned as a plugin.
    plugdir = tmp_path / "plugins"
    plugdir.mkdir()
    (plugdir / "hello.py").write_text(_CODE_PLUGIN, encoding="utf-8")
    trust = TrustStore(str(tmp_path / "trust.json"))
    return plugdir, trust


def test_approval_is_remembered_by_hash(tmp_path) -> None:
    plugdir, trust = _plugin_dir(tmp_path)

    # First load: approve once; it is remembered.
    reg1 = default_registry()
    issues1 = discovery.load_directory(reg1, str(plugdir), trust=trust, confirm=_ALLOW)
    assert issues1 == []
    assert reg1.plugin(Stage.READ, "read.hello")

    # Second load: deny everything, but the trusted hash loads silently.
    deny = lambda pending: False  # noqa: E731
    reg2 = default_registry()
    issues2 = discovery.load_directory(reg2, str(plugdir), trust=trust, confirm=deny)
    assert issues2 == []
    assert reg2.plugin(Stage.READ, "read.hello")


def test_changed_code_is_reprompted_in_a_new_run(tmp_path) -> None:
    plugdir, trust = _plugin_dir(tmp_path)
    discovery.load_directory(
        default_registry(), str(plugdir), trust=trust, confirm=_ALLOW
    )

    # Editing the file changes its hash. A *fresh* run (new TrustStore reading the
    # persisted file — empty session set) does not trust the new hash, so it prompts.
    (plugdir / "hello.py").write_text(_CODE_PLUGIN + "\n# edited\n", encoding="utf-8")
    fresh_trust = TrustStore(str(tmp_path / "trust.json"))
    reg = default_registry()
    deny = lambda pending: False  # noqa: E731
    issues = discovery.load_directory(
        reg, str(plugdir), trust=fresh_trust, confirm=deny
    )
    assert len(issues) == 1
    with pytest.raises(KeyError):
        reg.plugin(Stage.READ, "read.hello")


def test_session_edit_reloads_without_prompt(tmp_path) -> None:
    plugdir, trust = _plugin_dir(tmp_path)
    # Approve once this run -> the path becomes session-trusted.
    discovery.load_directory(
        default_registry(), str(plugdir), trust=trust, confirm=_ALLOW
    )

    # Edit the code and reload within the *same* run (same TrustStore): the
    # developer loop auto-approves the changed file, even with a denying callback.
    (plugdir / "hello.py").write_text(_CODE_PLUGIN + "\n# edited\n", encoding="utf-8")
    reg = default_registry()
    deny = lambda pending: False  # noqa: E731
    issues = discovery.load_directory(reg, str(plugdir), trust=trust, confirm=deny)
    assert issues == []
    assert reg.plugin(Stage.READ, "read.hello")


def test_broken_preset_is_reported_not_raised(tmp_path) -> None:
    (tmp_path / "bad.toml").write_text("this is not valid toml", encoding="utf-8")
    reg = default_registry()

    issues = discovery.load_directory(reg, str(tmp_path))
    assert len(issues) == 1
    assert "bad.toml" in issues[0].path


def test_module_without_register_is_reported(tmp_path) -> None:
    (tmp_path / "nohook.py").write_text("x = 1\n", encoding="utf-8")
    reg = default_registry()

    # Approve past the gate so we reach (and report) the missing register hook.
    issues = discovery.load_directory(reg, str(tmp_path), confirm=_ALLOW)
    assert len(issues) == 1
    assert "register" in issues[0].message


def test_env_path_is_searched(tmp_path, monkeypatch) -> None:
    (tmp_path / "custom.toml").write_text(_PRESET, encoding="utf-8")
    monkeypatch.setenv(discovery.ENV_PLUGIN_PATH, str(tmp_path))
    reg = default_registry()

    issues = discovery.load_user_plugins(reg)
    assert issues == []
    assert reg.preset("preset.pixel.custom-1bpp")


def test_missing_directory_is_silent(tmp_path) -> None:
    reg = default_registry()
    assert discovery.load_directory(reg, str(tmp_path / "does-not-exist")) == []
