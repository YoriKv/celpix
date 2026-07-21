"""Load user plugins dropped into a plugin directory.

Users extend Celpix by putting files into a plugin directory — no reinstall, no
editing package internals. Two kinds are recognised:

- **``*.toml`` — a preset** (zero code). The data-first tier: a parameter set for a
  built-in engine (a new planar format, a new colour format). Same schema as the
  shipped presets, and self-describing via its ``stage`` field. TOML is chosen for
  hand-editing — comments, hex integers (``0x7C00``), trailing commas.
- **``*.py`` — a code plugin** (the escape hatch for behaviour data can't express:
  a decompressor, a bespoke reader). The module must expose a
  ``register(registry)`` function; the host calls it to add the module's plugins.

This module is Qt-free: *where* the plugin directory is (a platform data dir) is
chosen by the app bootstrap and passed in; discovery only scans what it is given
plus the ``CELPIX_PLUGIN_PATH`` environment override.

**Trust:** loading a ``*.py`` plugin executes its code with the app's privileges —
the same trust model as any native editor's plugin DLLs. Sandboxing/signing of
third-party plugins is a later concern (``docs/design/overview.md`` §9); for now a
plugin directory is as trusted as the code you put in it.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover - exercised only on 3.9/3.10
    import tomli as tomllib

from celpix.core.errors import Stage
from celpix.plugins.base import Preset
from celpix.plugins.trust import (
    ConfirmCallback,
    PendingCodePlugin,
    TrustStore,
    digest_bytes,
)

if TYPE_CHECKING:
    from celpix.plugins.registry import Registry

# os.pathsep-separated list of extra plugin directories, honoured before the ones
# the app passes in. Handy for development and tests without a real data dir.
ENV_PLUGIN_PATH = "CELPIX_PLUGIN_PATH"


@dataclass(frozen=True)
class PluginLoadIssue:
    """One plugin file that failed to load. Collected, never raised, so a single
    bad file can't stop the app (or the other plugins) from starting."""

    path: str
    message: str


def preset_from_spec(spec: dict) -> Preset:
    """Build a :class:`Preset` from a parsed spec (built-in and user presets)."""
    return Preset(
        id=spec["id"],
        name=spec["name"],
        stage=Stage(spec["stage"]),
        engine_id=spec["engine_id"],
        params=spec.get("params", {}),
    )


def preset_from_toml(text: str) -> Preset:
    """Parse a preset's TOML source into a :class:`Preset`."""
    return preset_from_spec(tomllib.loads(text))


def plugin_search_path(extra_dirs: Iterable[str] = ()) -> list[str]:
    """Ordered plugin dirs: ``CELPIX_PLUGIN_PATH`` first, then ``extra_dirs``."""
    dirs: list[str] = []
    env = os.environ.get(ENV_PLUGIN_PATH)
    if env:
        dirs.extend(part for part in env.split(os.pathsep) if part)
    dirs.extend(extra_dirs)
    return dirs


def load_user_plugins(
    reg: Registry,
    extra_dirs: Iterable[str] = (),
    *,
    trust: TrustStore | None = None,
    confirm: ConfirmCallback | None = None,
) -> list[PluginLoadIssue]:
    """Scan every plugin directory and register what is found. Returns any issues.

    Code plugins are gated: one is loaded only if its content hash is already in
    ``trust`` or ``confirm`` approves it (and is then remembered). Presets are data
    and load ungated.
    """
    issues: list[PluginLoadIssue] = []
    for directory in plugin_search_path(extra_dirs):
        issues.extend(load_directory(reg, directory, trust=trust, confirm=confirm))
    return issues


def load_directory(
    reg: Registry,
    directory: str,
    *,
    trust: TrustStore | None = None,
    confirm: ConfirmCallback | None = None,
) -> list[PluginLoadIssue]:
    """Load every plugin file directly inside ``directory`` (non-recursive)."""
    issues: list[PluginLoadIssue] = []
    root = Path(directory)
    if not root.is_dir():
        return issues
    for entry in sorted(root.iterdir()):
        if not entry.is_file():
            continue
        if entry.suffix == ".toml":
            _load_preset(reg, entry, issues)
        elif entry.suffix == ".py" and not entry.name.startswith("_"):
            _load_module(reg, entry, issues, trust, confirm)
    return issues


def _load_preset(reg: Registry, path: Path, issues: list[PluginLoadIssue]) -> None:
    try:
        reg.register_preset(preset_from_toml(path.read_text(encoding="utf-8")))
    except Exception as exc:  # noqa: BLE001 — report, don't abort startup
        issues.append(PluginLoadIssue(str(path), f"preset load failed: {exc}"))


def _is_approved(
    path: Path,
    digest: str,
    trust: TrustStore | None,
    confirm: ConfirmCallback | None,
) -> bool:
    """Trusted already, or approved now (and then remembered). Default deny."""
    if trust is not None and trust.is_trusted(digest):
        return True
    # Developer loop: a plugin whose path was approved earlier this run reloads
    # without re-prompting when its code changes. Cross-run, a changed hash still
    # prompts (session set is empty at launch). See TrustStore.is_session_path.
    if trust is not None and trust.is_session_path(str(path)):
        trust.trust(digest, str(path))
        return True
    if confirm is not None and confirm(PendingCodePlugin(str(path), digest)):
        if trust is not None:
            trust.trust(digest, str(path))
        return True
    return False


def _load_module(
    reg: Registry,
    path: Path,
    issues: list[PluginLoadIssue],
    trust: TrustStore | None,
    confirm: ConfirmCallback | None,
) -> None:
    try:
        source = path.read_bytes()
    except Exception as exc:  # noqa: BLE001
        issues.append(PluginLoadIssue(str(path), f"could not read: {exc}"))
        return

    if not _is_approved(path, digest_bytes(source), trust, confirm):
        issues.append(
            PluginLoadIssue(str(path), "skipped: code plugin not approved by user")
        )
        return

    try:
        # Execute exactly the bytes we hashed (not a re-read), so approval can't be
        # bypassed by swapping the file after the check.
        namespace: dict = {
            "__name__": f"celpix_plugin_{path.stem}",
            "__file__": str(path),
        }
        exec(compile(source, str(path), "exec"), namespace)  # noqa: S102 — gated above
        register = namespace.get("register")
        if not callable(register):
            issues.append(
                PluginLoadIssue(str(path), "no register(registry) function found")
            )
            return
        register(reg)
    except Exception as exc:  # noqa: BLE001 — a broken plugin must not crash the app
        issues.append(PluginLoadIssue(str(path), f"module load failed: {exc}"))
