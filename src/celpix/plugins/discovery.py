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
typed root of its own. A **project** carries a root of its own the same way — the
``plugins/`` folder beside its ``.celpix`` file (:func:`project_plugin_dir`) —
so the formats a project needs can travel with it. Qt-free.

**Trust:** loading a ``*.py`` plugin executes its code with the app's privileges,
gated on the user's approval (:mod:`celpix.plugins.trust`) — a project's plugins
by the same gate, which is the point of routing them through here rather than
giving them a pathway of their own: a project file is something a user
*receives*, so its code has to be approved before it runs, exactly like a file
dropped in the user's own folder. Sandboxing and signing are a later concern
(``docs/design/overview.md`` §9); a plugin directory is as trusted as the code
put in it.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Union

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
from celpix.plugins.formats import adapt_format, format_behind
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
    "tilemap": Stage.INTERPRET_TILEMAP,
    "reshape": Stage.RESHAPE,
    "compression": Stage.COMPRESSION,
    "containers": Stage.CONTAINER,
}

# The interpret folders, whose *.toml files are presets and whose *.py files are
# code formats. Derived from FOLDER_STAGE so a folder's stage is stated once, and
# shared with the built-in loader since the shipped preset tree uses the same
# names. (reshape/ takes presets of a different shape — see
# :func:`_load_reshape_preset`.)
INTERPRET_FOLDER_STAGE: dict[str, Stage] = {
    folder: FOLDER_STAGE[folder] for folder in ("pixel", "palette", "tilemap")
}

# Every folder whose *.toml files are ordinary presets. The same three, kept as
# its own name because the two lists answer different questions — which folders
# take presets, and which of those also take code *formats* — and have parted
# company before.
PRESET_FOLDER_STAGE: dict[str, Stage] = dict(INTERPRET_FOLDER_STAGE)


@dataclass(frozen=True)
class PluginLoadIssue:
    """One plugin file that did not load. Collected, never raised, so one bad
    file can't stop the app or the other plugins from starting.

    ``declined`` separates **a choice from a breakage**: a code plugin the user
    refused at the trust prompt did exactly what they asked, and reporting it as
    a failure would put a "plugins failed to load" modal in front of them at
    every launch for as long as they keep saying no. It is still an issue —
    something in the folder is not running, and that is worth being able to see —
    so it is collected here and told apart at the point it is shown, not dropped.
    """

    path: str
    message: str
    declined: bool = False


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
        category=spec.get("category", ""),
    )


def preset_from_toml(text: str, stage: Stage) -> Preset:
    """Parse a preset's TOML source into a :class:`Preset`."""
    return preset_from_spec(tomllib.loads(text), stage)


# The picker headings the two plugin roots are filed under. A **source**, not a
# format family, and the one category a file does not get to choose: whatever
# ``category`` a dropped preset states is replaced, because a handful of your own
# entries scattered through a hundred shipped ones under vendor headings is the
# problem the grouping exists to solve (:data:`~celpix.plugins.base.CATEGORIES`).
USER_CATEGORY = "Your plugins"
PROJECT_CATEGORY = "Project plugins"


# What discovery's loaders actually take: the registry, or the wrapper standing in
# for it while one plugin root is scanned. A wrapper rather than a subclass because
# it is scoped to a scan, not to a registry — and the loaders below only ever
# *register* through it, which is the whole of what both provide.
RegistryLike = Union["Registry", "SourceRegistry"]


class SourceRegistry:
    """A registry that files everything registered through it under one heading.

    Wrapped around the real registry for the length of one plugin root's scan, so
    a preset, a code plugin and a code *format* out of that folder are all
    labelled by one rule rather than each loader remembering to. Reads pass
    straight through — a plugin inspecting what exists is asking about the
    registry, not about where it came from.

    Labelling the plugin means writing over its ``info``, which for a class
    attribute shadows it per instance; a plugin that refuses the write (``
    __slots__``, a read-only descriptor) is registered as it is rather than
    dropped, since the heading is presentation and the format is the point.
    """

    def __init__(self, reg: Registry, category: str) -> None:
        self._reg = reg
        self._category = category

    def register(self, plugin: Plugin, stage: Stage | None = None) -> None:
        try:
            plugin.info = replace(plugin.info, category=self._category)
        except (AttributeError, TypeError):
            pass
        self._reg.register(plugin, stage)

    def register_preset(self, preset: Preset) -> None:
        self._reg.register_preset(replace(preset, category=self._category))

    def __getattr__(self, name: str):
        return getattr(self._reg, name)


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
        reg: RegistryLike,
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
                    "register_format is only valid in pixel/, palette/ or tilemap/; "
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


# A project's own plugin root: the folder beside its .celpix file, in the same
# typed layout as the user's. Named `plugins` for exactly that reason - one
# layout to learn, and a plugin moves between the two roots by being copied.
PROJECT_PLUGIN_DIRNAME = "plugins"


def project_plugin_dir(project_path: str | None) -> str | None:
    """The plugin root travelling with the project file at ``project_path``.

    ``<the project's folder>/plugins/``, and only when it is actually there:
    unlike the user's root (created and seeded at startup) this one is the
    project author's to make, so most projects simply have none. ``None`` with
    no project open, which is how a caller says "user plugins only".
    """
    if not project_path:
        return None
    directory = Path(project_path).parent / PROJECT_PLUGIN_DIRNAME
    return str(directory) if directory.is_dir() else None


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
    project_dir: str | None = None,
    trust: TrustStore | None = None,
    confirm: ConfirmCallback | None = None,
) -> list[PluginLoadIssue]:
    """Scan every plugin directory and register what is found. Returns any issues.

    Code plugins are gated: one is loaded only if its content hash is already in
    ``trust`` or ``confirm`` approves it (and is then remembered). Presets are data
    and load ungated.

    ``project_dir`` names whichever of the scanned roots travels with the open
    project, so its formats are filed under their own picker heading rather than
    the user's own (:data:`PROJECT_CATEGORY`). It is matched against the paths in
    ``extra_dirs`` as given, since the caller built both from the same string
    (:func:`project_plugin_dir`).
    """
    issues: list[PluginLoadIssue] = []
    for directory in plugin_search_path(extra_dirs):
        category = PROJECT_CATEGORY if directory == project_dir else USER_CATEGORY
        issues.extend(
            load_directory(
                reg, directory, category=category, trust=trust, confirm=confirm
            )
        )
    return issues


# The plugin folder's own documentation, seeded into its root beside the typed
# subfolders. Not a plugin, and `.md` is not a suffix discovery loads, so it sits
# there inertly.
PLUGIN_README = "README.md"

# Reference files celPix used to seed and no longer ships, by folder. Retiring an
# example is not the same as replacing one: a name that stops being shipped stops
# being *rewritten*, so without this it sits in every upgraded folder for ever,
# teaching whatever it taught the day it was written. These three taught a
# parameterised sprite-record engine that no longer exists — copying one now
# produces a preset the loader refuses (:func:`check_engine_takes_params`), which
# is a worse first hour with the app than no example at all.
#
# **Names only, and only names celPix itself minted.** The ``_`` prefix is the
# reserved namespace this function already writes into without asking; removing
# one of its own retired names is the same promise as overwriting a live one, and
# is why this needs no record of what was shipped when. A user's own work never
# starts with ``_`` — that is what activating an example means — so nothing here
# can reach it.
RETIRED_EXAMPLES: dict[str, tuple[str, ...]] = {
    "tilemap": ("_object.toml", "_obz.toml", "_ys-spr.toml"),
}


def seed_examples(directory: str) -> None:
    """Refresh the shipped reference material in the plugin root.

    The examples are ``_``-prefixed so discovery ignores them: living
    documentation a user copies, dropping the underscore, to activate.
    :data:`PLUGIN_README` is seeded alongside them.

    **A stale copy is replaced**, matched by filename, so the examples and the
    README describe the version actually running rather than whichever one first
    created the folder. A **retired** one is removed on the same rule
    (:data:`RETIRED_EXAMPLES`), since a file that is no longer shipped is no
    longer replaced either and would otherwise outlive the mechanism it teaches.
    Neither can take a user's work with it: what they edit is the activated copy
    under a different name, and both halves touch only the reserved ``_`` names
    and the README. Files whose contents already match are left alone, so an
    unchanged folder is not rewritten on every launch.

    Failures are swallowed — reference material is not worth blocking startup
    over. The ``.py`` examples ship as ``.py.txt`` because frozen-build data
    collection excludes ``.py`` files; the suffix is dropped here.
    """
    root = Path(directory)
    # Ahead of the seeding and independent of it: a folder celPix no longer ships
    # any example for still has to shed the ones it used to.
    for folder, retired in RETIRED_EXAMPLES.items():
        for name in retired:
            try:
                (root / folder / name).unlink(missing_ok=True)
            except OSError:
                pass
    _seed_file(resources.resource("data", "plugin-examples", PLUGIN_README), root)
    for folder in FOLDER_STAGE:
        try:
            entries = list(
                resources.resource("data", "plugin-examples", folder).iterdir()
            )
        except (FileNotFoundError, OSError):
            continue
        # Made here rather than assumed: a stage added after a user's plugin
        # folder was created has no folder of its own yet, and seeding into one
        # that does not exist would silently skip the whole new category.
        dest_dir = root / folder
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
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
    category: str = USER_CATEGORY,
    trust: TrustStore | None = None,
    confirm: ConfirmCallback | None = None,
) -> list[PluginLoadIssue]:
    """Load the typed subfolders of the plugin root ``directory``.

    Loose plugin files in the root are reported with a pointer to the right
    subfolder rather than loaded; unknown subfolders are ignored, so renaming one
    disables its contents.

    Everything registered is filed under ``category`` in the format pickers — one
    wrapper for the whole scan (:class:`SourceRegistry`), so a preset, a code
    plugin and a code format out of this root are grouped by one rule. Pass ``""``
    to leave each file's own ``category`` standing, which is what a test loading a
    fixture folder wants.
    """
    issues: list[PluginLoadIssue] = []
    root = Path(directory)
    if not root.is_dir():
        return issues
    target: RegistryLike = SourceRegistry(reg, category) if category else reg
    for entry in sorted(root.iterdir()):
        if entry.is_dir() and entry.name in FOLDER_STAGE:
            _load_typed_dir(target, entry, entry.name, issues, trust, confirm)
        elif (
            entry.is_file()
            and entry.suffix in (".toml", ".py")
            and not entry.name.startswith("_")
        ):
            issues.append(
                PluginLoadIssue(
                    str(entry),
                    "plugins live in typed subfolders - move this file into "
                    "pixel/, palette/, tilemap/, reshape/, "
                    "compression/ or containers/",
                )
            )
    return issues


def _load_typed_dir(
    reg: RegistryLike,
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
            stage = PRESET_FOLDER_STAGE.get(folder)
            if stage is not None:
                _load_preset(reg, entry, stage, issues)
            elif folder == "reshape":
                _load_reshape_preset(reg, entry, issues)
            else:
                issues.append(
                    PluginLoadIssue(
                        str(entry),
                        f"presets are pixel/palette/tilemap/reshape only; "
                        f"'{folder}/' takes .py code plugins",
                    )
                )
        elif entry.suffix == ".py":
            _load_module(reg, entry, folder, issues, trust, confirm)


def check_engine_takes_params(reg: RegistryLike, preset: Preset) -> None:
    """Refuse a preset whose engine is a self-contained format: one takes none.

    A format *is* its own parameterisation, so the engine adapted from it ignores
    the ``params`` every codec method carries (:mod:`celpix.plugins.formats`).
    Nothing downstream can notice that: a preset written for an engine that has
    since **become** a format still resolves — the rename table forwards the
    name, which is all a name can do — and is then read with the format's own
    answers in place of its author's. A byte order or a subsprite size quietly
    substituted decodes every cell differently and re-encodes the file that way
    on the next save, with the preset looking healthy the whole time
    (:mod:`celpix.plugins.aliases`).

    So the refusal is here, where the preset is still a file with a path to
    report against, and the entry naming it is left to the ordinary
    missing-format repair (:func:`~celpix.project.workspace.repair_presets`) —
    which tells the user which format their entry wanted, rather than showing
    them a wrong picture that saves.

    Only params the format does not itself declare count: those are what a codec
    would have read (:class:`~celpix.plugins.formats.FormatInfo`), and a preset
    carrying nothing else is one more name for the same behaviour.
    """
    try:
        engine = reg.plugin(preset.stage, preset.engine_id)
    except KeyError:
        # Not this check's business: an engine this build hasn't got is either a
        # genuine miss, reported per entry when one names the preset, or a code
        # plugin in the same folder that this scan has not reached yet.
        return
    fmt = format_behind(engine)
    if fmt is None:
        return
    declares = getattr(fmt.info, "declares", None) or {}
    ignored = sorted(set(preset.params) - set(declares))
    if ignored:
        raise ValueError(
            f"engine_id {preset.engine_id!r} is {engine.info.name!r}, a code "
            f"format, which takes no parameters — {', '.join(ignored)} would be "
            f"silently ignored; delete this preset and pick that format directly"
        )


def _load_preset(
    reg: RegistryLike, path: Path, stage: Stage, issues: list[PluginLoadIssue]
) -> None:
    try:
        spec = tomllib.loads(path.read_text(encoding="utf-8"))
        preset = preset_from_spec(spec, stage)
        check_engine_takes_params(reg, preset)
        reg.register_preset(preset)
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
    reg: RegistryLike, path: Path, issues: list[PluginLoadIssue]
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
    reg: RegistryLike,
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
            PluginLoadIssue(
                str(path),
                "not approved to run: the trust prompt for this code plugin was "
                "declined",
                declined=True,
            )
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
