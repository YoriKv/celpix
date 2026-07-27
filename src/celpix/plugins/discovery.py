"""Load user plugins dropped into a typed plugin directory.

Users extend celPix by putting files into a plugin directory — no reinstall, no
editing package internals. The folder a file sits in *determines* its type:

- ``pixel/`` and ``palette/`` take both kinds of file. A **``*.toml`` preset** is
  the zero-code tier: a parameter set for a built-in engine, on the same schema
  as the shipped presets. TOML suits hand-editing — comments, hex integers
  (``0x7C00``), trailing commas. A **``*.py`` code format**
  (:mod:`celpix.plugins.formats`) is a self-contained decode/encode registered
  via ``registry.register_format(...)`` and listed in the picker like any preset.
- ``compression/`` and ``containers/`` take ``*.py`` plugins.
- ``reshape/`` takes ``*.py`` plugins plus ``*.toml`` presets for the bitswap and
  data-LUT engines, adapted into ordinary reshape plugins at load
  (:data:`RESHAPE_ENGINES`).

Each plugin covers both directions of its stage, so a folder maps to exactly one
stage. Because the folder is authoritative, preset TOMLs carry no ``stage``
field, and a ``register()`` call naming another stage is reported with only that
registration skipped. Loose files in the root are reported with a pointer to the
right subfolder; *unknown* subfolders are ignored, so renaming one (``pixel.off/``)
parks its plugins. ``_``-prefixed files are ignored too, which keeps the seeded
reference files (:func:`seed_examples`) and works-in-progress inert.

Where the plugin directory lives is the app bootstrap's choice and is passed in;
discovery scans that plus the ``CELPIX_PLUGIN_PATH`` override, each entry being a
typed root of its own. Qt-free.

**Trust:** loading a ``*.py`` plugin executes its code with the app's privileges,
gated on the user's approval (:mod:`celpix.plugins.trust`). Sandboxing and
signing are a later concern (``docs/design/overview.md`` §9); a plugin directory
is as trusted as the code put in it.
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
from celpix.plugins.base import (
    Plugin,
    Preset,
    check_declared_stage,
    missing_methods,
)
from celpix.plugins.bitswap import BITSWAP_ENGINE, bitswap_from_spec
from celpix.plugins.data_lut import DATA_LUT_ENGINE, data_lut_from_spec
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

# The typed layout: which stage a plugin found in each subfolder may register.
# One stage per folder, because a stage is the unit a user thinks in — a
# compression scheme is both its halves, a container both of its.
FOLDER_STAGE: dict[str, Stage] = {
    "pixel": Stage.INTERPRET_PIXEL,
    "palette": Stage.INTERPRET_PALETTE,
    "reshape": Stage.RESHAPE,
    "compression": Stage.COMPRESSION,
    "containers": Stage.CONTAINER,
}

# The two interpret folders, whose *.toml files are pixel/palette presets and
# whose *.py files are code formats. Derived from FOLDER_STAGE so a folder's
# stage is stated once, and shared with the built-in loader since the shipped
# preset tree uses the same names. (reshape/ takes presets of a different shape —
# see :func:`_load_reshape_preset`.)
INTERPRET_FOLDER_STAGE: dict[str, Stage] = {
    folder: FOLDER_STAGE[folder] for folder in ("pixel", "palette")
}


@dataclass(frozen=True)
class PluginLoadIssue:
    """One plugin file that failed to load. Collected, never raised, so one bad
    file can't stop the app or the other plugins from starting."""

    path: str
    message: str


def preset_from_spec(spec: dict, stage: Stage) -> Preset:
    """Build a :class:`Preset` from a parsed spec (built-in and user presets).

    ``stage`` comes from the folder the spec was found in — the folder is
    authoritative (:func:`~celpix.plugins.base.check_declared_stage`).
    """
    check_declared_stage(spec, stage)
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

    Enforces the folder-determines-type rule at the registration boundary: an
    out-of-scope registration becomes a :class:`PluginLoadIssue` against the
    source file and only *that* registration is skipped, so a file registering
    several plugins keeps whichever are in scope. Reads pass through so plugins
    can inspect what exists.
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

    def _allows(self, stage: Stage | None) -> bool:
        """Whether ``stage`` may be registered here. ``None`` means "the folder's".

        Omitting it is the normal case, the folder being authoritative; a stated
        one is honoured as an assertion, and disagreeing with the folder is a load
        issue.
        """
        allowed = FOLDER_STAGE[self._folder]
        if stage is None or stage is allowed:
            return True
        self._issues.append(
            PluginLoadIssue(
                str(self._path),
                f"stage '{stage.value}' not allowed in folder "
                f"'{self._folder}/' (allowed: {allowed.value}); registration skipped",
            )
        )
        return False

    # -- writes (scope-checked) --------------------------------------------
    def register(self, plugin: Plugin) -> None:
        if not self._allows(plugin.info.stage):
            return
        stage = FOLDER_STAGE[self._folder]
        # The folder supplies the stage, so a plugin in the wrong one would
        # otherwise register as something it cannot do
        # (:data:`~celpix.plugins.base.STAGE_METHODS`).
        missing = missing_methods(plugin, stage)
        if missing:
            self._issues.append(
                PluginLoadIssue(
                    str(self._path),
                    f"{plugin.info.id!r} is missing {', '.join(missing)} — a "
                    f"{stage.value} plugin needs it; registration skipped",
                )
            )
            return
        self._reg.register(plugin, stage)

    def register_preset(self, preset: Preset) -> None:
        if self._allows(preset.stage):
            self._reg.register_preset(preset)

    def register_format(self, fmt) -> None:  # noqa: ANN001 — duck-typed on purpose
        stage = INTERPRET_FOLDER_STAGE.get(self._folder)
        if stage is None:
            self._issues.append(
                PluginLoadIssue(
                    str(self._path),
                    "register_format is only valid in pixel/ or palette/; "
                    "registration skipped",
                )
            )
            return
        # So a palette-shaped class dropped in pixel/, or a typo'd method, is a
        # load issue now rather than a decode-time crash later. A format's methods
        # take no params, so only their names are shared with a full plugin's and
        # callability is all this tests.
        missing = missing_methods(fmt, stage)
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

    # -- reads (pass-through; spelled out so the surface stays deliberate) ---
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


# The plugin folder's own documentation, seeded into its root beside the typed
# subfolders. Not a plugin, and `.md` is not a suffix discovery loads, so it sits
# there inertly.
PLUGIN_README = "README.md"


def seed_examples(directory: str) -> None:
    """Refresh the shipped reference material in the plugin root.

    The examples are ``_``-prefixed so discovery ignores them: living
    documentation a user copies, dropping the underscore, to activate.
    :data:`PLUGIN_README` is seeded alongside them.

    **A stale copy is replaced**, matched by filename, so the examples and the
    README describe the version actually running rather than whichever one first
    created the folder. That cannot take a user's work with it: what they edit is
    the activated copy under a different name, and this only ever writes the
    reserved ``_`` names and the README. Files whose contents already match are
    left alone, so an unchanged folder is not rewritten on every launch.

    Failures are swallowed — reference material is not worth blocking startup
    over. The ``.py`` examples ship as ``.py.txt`` because frozen-build data
    collection excludes ``.py`` files; the suffix is dropped here.
    """
    root = Path(directory)
    _seed_file(resources.resource("data", "plugin-examples", PLUGIN_README), root)
    for folder in FOLDER_STAGE:
        dest_dir = root / folder
        if not dest_dir.is_dir():
            continue
        try:
            entries = list(
                resources.resource("data", "plugin-examples", folder).iterdir()
            )
        except (FileNotFoundError, OSError):
            continue
        for entry in entries:
            _seed_file(entry, dest_dir)


def _seed_file(entry, dest_dir: Path) -> None:  # noqa: ANN001 — a Traversable
    """Write one shipped file into ``dest_dir`` unless it is already identical."""
    dest = dest_dir / entry.name.removesuffix(".txt")
    try:
        shipped = entry.read_text(encoding="utf-8")
        if dest.exists() and dest.read_text(encoding="utf-8") == shipped:
            return
        dest.write_text(shipped, encoding="utf-8")
    except OSError:
        pass


def load_directory(
    reg: Registry,
    directory: str,
    *,
    trust: TrustStore | None = None,
    confirm: ConfirmCallback | None = None,
) -> list[PluginLoadIssue]:
    """Load the typed subfolders of the plugin root ``directory``.

    Loose plugin files in the root are reported with a pointer to the right
    subfolder rather than loaded; unknown subfolders are ignored, so renaming one
    disables its contents.
    """
    issues: list[PluginLoadIssue] = []
    root = Path(directory)
    if not root.is_dir():
        return issues
    for entry in sorted(root.iterdir()):
        if entry.is_dir() and entry.name in FOLDER_STAGE:
            _load_typed_dir(reg, entry, entry.name, issues, trust, confirm)
        elif (
            entry.is_file()
            and entry.suffix in (".toml", ".py")
            and not entry.name.startswith("_")
        ):
            issues.append(
                PluginLoadIssue(
                    str(entry),
                    "plugins live in typed subfolders - move this file into "
                    "pixel/, palette/, reshape/, compression/ or containers/",
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

    ``_``-prefixed files are skipped: the convention for inert files, covering
    both the seeded reference files and works in progress.
    """
    for entry in sorted(root.iterdir()):
        if not entry.is_file() or entry.name.startswith("_"):
            continue
        if entry.suffix == ".toml":
            stage = INTERPRET_FOLDER_STAGE.get(folder)
            if stage is not None:
                _load_preset(reg, entry, stage, issues)
            elif folder == "reshape":
                _load_reshape_preset(reg, entry, issues)
            else:
                issues.append(
                    PluginLoadIssue(
                        str(entry),
                        f"presets are pixel/palette/reshape only; '{folder}/' "
                        "takes .py code plugins",
                    )
                )
        elif entry.suffix == ".py":
            _load_module(reg, entry, folder, issues, trust, confirm)


def _load_preset(
    reg: Registry, path: Path, stage: Stage, issues: list[PluginLoadIssue]
) -> None:
    try:
        reg.register_preset(preset_from_toml(path.read_text(encoding="utf-8"), stage))
    except Exception as exc:  # noqa: BLE001 — report, don't abort startup
        issues.append(PluginLoadIssue(str(path), f"preset load failed: {exc}"))


# The reshape preset engines, keyed by the `engine_id` a preset declares. Unlike
# a pixel or palette preset's, a reshape preset's engine_id is a *discriminator*:
# it picks which adapter turns the spec into a plugin rather than naming one to
# resolve at decode time. Address permutations and value substitutions are
# unrelated transforms sharing only a file format, so this is where they part.
RESHAPE_ENGINES = {
    BITSWAP_ENGINE: bitswap_from_spec,
    DATA_LUT_ENGINE: data_lut_from_spec,
}


def _load_reshape_preset(
    reg: Registry, path: Path, issues: list[PluginLoadIssue]
) -> None:
    """A ``reshape/*.toml`` preset, adapted into a reshape plugin.

    Data like any preset, so no code runs and no trust gate applies, but
    registered as a *plugin* rather than a :class:`Preset` because the Reshape
    stage resolves plain plugin ids everywhere — the pipeline, the combos, a
    project's ``reshape_id``. Adapting here leaves all of that untouched.
    """
    try:
        spec = tomllib.loads(path.read_text(encoding="utf-8"))
        adapt = RESHAPE_ENGINES.get(spec.get("engine_id"))
        if adapt is None:
            raise ValueError(
                f"engine_id {spec.get('engine_id')!r} is not a reshape engine "
                f"(expected one of {', '.join(sorted(RESHAPE_ENGINES))})"
            )
        reg.register(adapt(spec))
    except Exception as exc:  # noqa: BLE001 — report, don't abort startup
        issues.append(PluginLoadIssue(str(path), f"reshape preset load failed: {exc}"))


def _is_approved(
    path: Path,
    digest: str,
    trust: TrustStore | None,
    confirm: ConfirmCallback | None,
) -> bool:
    """Trusted already, or approved now (and then remembered). Default deny."""
    if trust is not None and trust.is_trusted(digest):
        return True
    # Developer loop: a path approved earlier this run reloads without a prompt
    # when its code changes. Across runs a changed hash still prompts, the
    # session set being empty at launch (TrustStore.is_session_path).
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
        # Through a folder-scoped surface, so the layout's type guarantee holds
        # for code as well as data.
        register(ScopedRegistry(reg, folder, path, issues))
    except Exception as exc:  # noqa: BLE001 — a broken plugin must not crash the app
        issues.append(PluginLoadIssue(str(path), f"module load failed: {exc}"))
