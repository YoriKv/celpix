"""Load user plugins dropped into a typed plugin directory.

Users extend Celpix by putting files into a plugin directory — no reinstall, no
editing package internals. The directory is organised by **what a plugin is**,
and the folder a file sits in *determines* its type:

- ``pixel/`` — interpret-pixel formats, ``palette/`` — interpret-palette
  formats. Either kind of file:

  - **``*.toml`` — a preset** (zero code). The data-first tier: a parameter set
    for a built-in engine (a new planar format, a new colour format). Same
    schema as the shipped presets. TOML is chosen for hand-editing — comments,
    hex integers (``0x7C00``), trailing commas.
  - **``*.py`` — a code format** (see :mod:`celpix.plugins.formats`): a
    self-contained decode/encode implementation registered via
    ``registry.register_format(...)``, listed in the picker like any preset.

- ``compression/`` — ``*.py`` registering compress and/or decompress plugins
  (a scheme's two halves live in one file); ``containers/`` — ``*.py``
  registering read and/or write plugins.

Because the folder is authoritative, preset TOMLs carry no ``stage`` field, and
a ``register()`` call whose stage falls outside its folder is reported (that one
registration is skipped, the rest of the file still loads). Loose files in the
root are reported with a pointer to the right subfolder; *unknown* subfolders
are ignored — renaming a folder (``pixel.off/``) is a cheap way to park plugins.
``_``-prefixed files are ignored too: that keeps the seeded ``_example.*``
reference files (:func:`seed_examples`) and works-in-progress inert.

This module is Qt-free: *where* the plugin directory is (a platform data dir) is
chosen by the app bootstrap and passed in; discovery only scans what it is given
plus the ``CELPIX_PLUGIN_PATH`` environment override (each entry is itself a
typed root with these subfolders).

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

from celpix import resources
from celpix.core.errors import Stage
from celpix.plugins.base import Plugin, Preset
from celpix.plugins.formats import adapt_format
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

# The typed layout: which stages a plugin found in each subfolder may register.
# Folders pair the stages a user thinks of as one unit (a compression scheme is
# its compress + decompress halves; a container format is read + write).
FOLDER_STAGES: dict[str, frozenset[Stage]] = {
    "pixel": frozenset({Stage.INTERPRET_PIXEL}),
    "palette": frozenset({Stage.INTERPRET_PALETTE}),
    "compression": frozenset({Stage.COMPRESS, Stage.DECOMPRESS}),
    "containers": frozenset({Stage.READ, Stage.WRITE}),
}

# The folders whose *.toml files are presets (and the stage each folder implies).
# Shared with the built-in loader — the shipped preset tree uses the same names.
PRESET_FOLDER_STAGE: dict[str, Stage] = {
    "pixel": Stage.INTERPRET_PIXEL,
    "palette": Stage.INTERPRET_PALETTE,
}


@dataclass(frozen=True)
class PluginLoadIssue:
    """One plugin file that failed to load. Collected, never raised, so a single
    bad file can't stop the app (or the other plugins) from starting."""

    path: str
    message: str


def preset_from_spec(spec: dict, stage: Stage) -> Preset:
    """Build a :class:`Preset` from a parsed spec (built-in and user presets).

    ``stage`` comes from the folder the spec was found in — the folder is
    authoritative. A leftover ``stage`` field that matches is tolerated (cheap
    migration from the old self-describing schema); a conflicting one is an
    error so a preset never silently lands in the wrong pathway.
    """
    declared = spec.get("stage")
    if declared is not None and declared != stage.value:
        raise ValueError(
            f"stage {declared!r} conflicts with the folder's stage {stage.value!r} — "
            "remove the stage field; the folder determines it"
        )
    return Preset(
        id=spec["id"],
        name=spec["name"],
        stage=stage,
        engine_id=spec["engine_id"],
        params=spec.get("params", {}),
    )


def preset_from_toml(text: str, stage: Stage) -> Preset:
    """Parse a preset's TOML source into a :class:`Preset`."""
    return preset_from_spec(tomllib.loads(text), stage)


class ScopedRegistry:
    """The registry surface a code plugin's ``register()`` receives.

    It enforces the folder-determines-type rule at the registration boundary:
    an out-of-scope registration becomes a :class:`PluginLoadIssue` attributed
    to the source file and only *that* registration is skipped — a compression
    file registering both its compress and decompress halves keeps whichever
    are in scope. Reads pass through so plugins can inspect what exists.
    """

    def __init__(
        self,
        reg: Registry,
        folder: str,
        path: Path,
        issues: list[PluginLoadIssue],
    ) -> None:
        self._reg = reg
        self._folder = folder
        self._path = path
        self._issues = issues

    def _allows(self, stage: Stage) -> bool:
        allowed = FOLDER_STAGES[self._folder]
        if stage in allowed:
            return True
        names = ", ".join(sorted(s.value for s in allowed))
        self._issues.append(
            PluginLoadIssue(
                str(self._path),
                f"stage '{stage.value}' not allowed in folder "
                f"'{self._folder}/' (allowed: {names}); registration skipped",
            )
        )
        return False

    # -- writes (scope-checked) --------------------------------------------
    def register(self, plugin: Plugin) -> None:
        if self._allows(plugin.info.stage):
            self._reg.register(plugin)

    def register_preset(self, preset: Preset) -> None:
        if self._allows(preset.stage):
            self._reg.register_preset(preset)

    def register_format(self, fmt) -> None:  # noqa: ANN001 — duck-typed on purpose
        stage = PRESET_FOLDER_STAGE.get(self._folder)
        if stage is None:
            self._issues.append(
                PluginLoadIssue(
                    str(self._path),
                    "register_format is only valid in pixel/ or palette/; "
                    "registration skipped",
                )
            )
            return
        # Shape-check up front so a palette-shaped class dropped in pixel/ (or a
        # typo'd method) is a load issue now, not a decode-time crash later.
        required = (
            ("decode", "encode", "bytes_per_tile", "tile_size")
            if stage is Stage.INTERPRET_PIXEL
            else ("decode", "encode", "bytes_per_entry")
        )
        missing = [m for m in required if not callable(getattr(fmt, m, None))]
        if missing or getattr(fmt, "info", None) is None:
            what = ", ".join(missing) if missing else "info"
            self._issues.append(
                PluginLoadIssue(
                    str(self._path),
                    f"format for '{self._folder}/' is missing {what}; "
                    "registration skipped",
                )
            )
            return
        engine, preset = adapt_format(fmt, stage)
        self._reg.register(engine)
        self._reg.register_preset(preset)

    # -- reads (pass-through; kept explicit so the surface stays deliberate) --
    def plugin(self, stage: Stage, plugin_id: str) -> Plugin:
        return self._reg.plugin(stage, plugin_id)

    def plugins(self, stage: Stage) -> list[Plugin]:
        return self._reg.plugins(stage)

    def preset(self, preset_id: str) -> Preset:
        return self._reg.preset(preset_id)

    def presets(self, stage: Stage | None = None) -> list[Preset]:
        return self._reg.presets(stage)


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


def seed_examples(directory: str) -> None:
    """Copy the shipped ``_example.*`` reference files into the plugin root.

    The examples are ``_``-prefixed so discovery ignores them — living
    documentation a user copies (dropping the underscore) to activate. Existing
    files are never overwritten, and failures are swallowed: reference material
    is not worth blocking startup over. The ``.py`` examples ship as ``.py.txt``
    because frozen-build data collection excludes ``.py`` files; the suffix is
    dropped here.
    """
    for folder in FOLDER_STAGES:
        dest_dir = Path(directory) / folder
        try:
            entries = list(
                resources.resource("data", "plugin-examples", folder).iterdir()
            )
        except (FileNotFoundError, OSError):
            continue
        for entry in entries:
            dest = dest_dir / entry.name.removesuffix(".txt")
            try:
                if not dest_dir.is_dir() or dest.exists():
                    continue
                dest.write_text(entry.read_text(encoding="utf-8"), encoding="utf-8")
            except OSError:
                continue


def load_directory(
    reg: Registry,
    directory: str,
    *,
    trust: TrustStore | None = None,
    confirm: ConfirmCallback | None = None,
) -> list[PluginLoadIssue]:
    """Load the typed subfolders of the plugin root ``directory``.

    Loose plugin files directly in the root are reported (with a pointer to the
    right subfolder) rather than loaded; unknown subfolders are deliberately
    ignored so renaming one is a cheap way to disable its contents.
    """
    issues: list[PluginLoadIssue] = []
    root = Path(directory)
    if not root.is_dir():
        return issues
    for entry in sorted(root.iterdir()):
        if entry.is_dir() and entry.name in FOLDER_STAGES:
            _load_typed_dir(reg, entry, entry.name, issues, trust, confirm)
        elif (
            entry.is_file()
            and entry.suffix in (".toml", ".py")
            and not entry.name.startswith("_")
        ):
            issues.append(
                PluginLoadIssue(
                    str(entry),
                    "plugins live in typed subfolders — move this file into "
                    "pixel/, palette/, compression/ or containers/",
                )
            )
    return issues


def _load_typed_dir(
    reg: Registry,
    root: Path,
    folder: str,
    issues: list[PluginLoadIssue],
    trust: TrustStore | None,
    confirm: ConfirmCallback | None,
) -> None:
    """Load every plugin file directly inside one typed subfolder (non-recursive).

    ``_``-prefixed files are skipped entirely — the convention for inert files
    (the seeded ``_example.*`` reference files, work-in-progress plugins).
    """
    for entry in sorted(root.iterdir()):
        if not entry.is_file() or entry.name.startswith("_"):
            continue
        if entry.suffix == ".toml":
            stage = PRESET_FOLDER_STAGE.get(folder)
            if stage is None:
                issues.append(
                    PluginLoadIssue(
                        str(entry),
                        f"presets are pixel/palette only; '{folder}/' takes "
                        ".py code plugins",
                    )
                )
            else:
                _load_preset(reg, entry, stage, issues)
        elif entry.suffix == ".py":
            _load_module(reg, entry, folder, issues, trust, confirm)


def _load_preset(
    reg: Registry, path: Path, stage: Stage, issues: list[PluginLoadIssue]
) -> None:
    try:
        reg.register_preset(preset_from_toml(path.read_text(encoding="utf-8"), stage))
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
    folder: str,
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
        # The plugin registers through a folder-scoped surface so the layout's
        # type guarantee holds even for code.
        register(ScopedRegistry(reg, folder, path, issues))
    except Exception as exc:  # noqa: BLE001 — a broken plugin must not crash the app
        issues.append(PluginLoadIssue(str(path), f"module load failed: {exc}"))
