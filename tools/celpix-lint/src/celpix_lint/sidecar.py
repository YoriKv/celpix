"""Ids declared by the ``plugins/`` folder beside the project file.

A project travels with its formats: ``<the project's folder>/plugins/`` is
loaded as a typed plugin root when the project opens, so a preset only that
folder provides is not missing at all (project-format.md §6). Without reading
it, every entry naming one of those presets is reported as an unknown id — 420
of them across this repo's own sample projects, which is the noise level at
which a linter stops being read.

The folder is **typed**: the subfolder a file sits in determines its stage
(``celpix.plugins.discovery.FOLDER_STAGE``), so an id found under ``tilemap/``
is registered here as a tilemap preset and nowhere else.

Nothing here executes a plugin, and nothing here is meant to be a loader. It
harvests declared ids so an id check can stop complaining about them — which is
the safe direction to be approximate in. A `.py` plugin's id is read out of its
source as a string literal for the same reason: over-reading it suppresses a
warning that would have been wrong anyway, while under-reading it produces the
false positive this module exists to remove.
"""

from __future__ import annotations

import re
from os import walk
from os.path import basename, dirname, isdir, join, relpath, sep

#: Subfolder -> stage, mirroring ``discovery.FOLDER_STAGE``. Note ``containers``
#: is plural and the rest are singular; that is the folder layout, not a typo.
FOLDER_STAGE = {
    "pixel": "interpret-pixel",
    "palette": "interpret-palette",
    "tilemap": "interpret-tilemap",
    "reshape": "reshape",
    "compression": "compression",
    "containers": "container",
}

PROJECT_PLUGIN_DIRNAME = "plugins"

# A TOML preset states `id = "..."` at top level. Only the part before the first
# `[section]` header is scanned, so a `[params]` key called `id` cannot be
# mistaken for the preset's own — which is the one way a plain-text read of TOML
# goes wrong here.
_TOML_ID = re.compile(r'^\s*id\s*=\s*["\']([^"\']+)["\']', re.MULTILINE)
_SECTION = re.compile(r"^\s*\[", re.MULTILINE)

# An id as the ids are shaped: dotted, lowercase, no spaces. Used only to pick
# plausible strings out of a `.py` plugin's source.
_PY_ID = re.compile(r'["\']([a-z][a-z0-9-]*(?:\.[a-z0-9-]+)+)["\']')


def plugin_dir(project_path: str) -> str | None:
    """The plugin root travelling with ``project_path``, if it is there."""
    directory = join(dirname(project_path) or ".", PROJECT_PLUGIN_DIRNAME)
    return directory if isdir(directory) else None


def declared_ids(project_path: str) -> dict:
    """``stage -> {ids}`` the project's own plugin folder provides.

    Empty when there is no folder, which is most projects.
    """
    root = plugin_dir(project_path)
    found: dict = {}
    if root is None:
        return found
    for directory, _subdirs, names in walk(root):
        stage = _stage_of(root, directory)
        if stage is None:
            # Unknown subfolders are ignored by celPix too, which is what makes
            # renaming one (`pixel.off/`) the documented way to disable it.
            continue
        for name in names:
            if name.startswith("_"):
                continue
            path = join(directory, name)
            if name.endswith(".toml"):
                ids = _toml_ids(path)
            elif name.endswith(".py"):
                ids = _py_ids(path)
            else:
                continue
            if ids:
                found.setdefault(stage, set()).update(ids)
    return found


def _stage_of(root: str, directory: str) -> str | None:
    """The stage a file in ``directory`` registers at, by its folder.

    The typed folder is the *first* segment under the root: celPix reads
    ``plugins/tilemap/...`` and a deeper tree below that is the author's to
    organise.
    """
    if directory == root:
        return None
    relative = relpath(directory, root)
    return FOLDER_STAGE.get(relative.split(sep)[0])


def _toml_ids(path: str) -> set:
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except (OSError, UnicodeDecodeError):
        return set()
    section = _SECTION.search(text)
    head = text[: section.start()] if section else text
    return set(_TOML_ID.findall(head))


def _py_ids(path: str) -> set:
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except (OSError, UnicodeDecodeError):
        return set()
    # Every dotted-lowercase string literal in the file. Deliberately loose: it
    # is only ever used to *stop* a warning, and a plugin naming an id it does
    # not register is not a project-file problem.
    return set(_PY_ID.findall(text))


def describe(project_path: str) -> str:
    """A short note for the report, or "" when the project has no folder."""
    root = plugin_dir(project_path)
    return basename(root) + "/" if root else ""
