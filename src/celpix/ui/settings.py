"""The app's preferences: QSettings opened one way, typed loads, recent projects.

App-wide rather than per-project — the theme, the grid style, the window layout,
the recently opened projects: things about how *this user* works, which a
project opened on another machine should not drag along.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from enum import Enum
from typing import TypeVar

from PySide6.QtCore import QSettings

from celpix import APP_NAME

__all__ = [
    "MAX_RECENT_PROJECTS",
    "RECENT_PROJECTS_KEY",
    "clear_recent_projects",
    "forget_recent_project",
    "load_bool_setting",
    "load_enum_setting",
    "load_float_setting",
    "load_recent_projects",
    "load_setting",
    "remember_recent_project",
    "save_bool_setting",
    "save_enum_setting",
    "save_float_setting",
    "settings",
]

_T = TypeVar("_T")
_EnumT = TypeVar("_EnumT", bound=Enum)


def settings() -> QSettings:
    """The app's preference store, opened the one way everything here opens it.

    The organization is named **explicitly** rather than left to the bare
    ``QSettings()`` default. celPix sets only an application name on the
    QApplication (:mod:`celpix.app` says why), and with no organization Qt files
    the settings under a literal ``Unknown Organization`` — a directory on Linux
    and macOS, a registry key on Windows. celPix is its own organization, so
    naming it puts the file where its name says: ``~/.config/celPix.conf``,
    ``HKCU\\Software\\celPix``.

    The format is named too, as whatever the default currently *is*, rather than
    left to the constructor to imply: the overloads that take no format resolve
    the file through the platform's native path table whatever
    ``setDefaultFormat`` says, which would put the store beyond the reach of
    ``QSettings.setPath`` — and that is the one hook a test suite has for keeping
    its writes out of the developer's own config.
    """
    return QSettings(QSettings.defaultFormat(), QSettings.Scope.UserScope, APP_NAME)


def load_setting(key: str, default: _T, convert: Callable[[object], _T]) -> _T:
    """A preference out of QSettings, through ``convert``, or ``default``.

    ``default`` where nothing is stored, and where what is stored will not
    convert — an older or newer celPix wrote the settings, or someone edited the
    file by hand — since a stale preference is not a reason to fail to start.
    The typed loaders below are this with their own conversion.
    """
    stored = settings().value(key)
    if stored is None:
        return default
    try:
        return convert(stored)
    except (TypeError, ValueError):
        return default


def _to_bool(stored: object) -> bool:
    """A stored flag back as a bool, by Qt's rule rather than Python's.

    The INI backend hands a boolean back as the *string* it wrote, and
    ``bool("false")`` is True; Qt reads a string as true unless it is empty,
    ``0`` or ``false``, whatever the case, and so does this.
    """
    if isinstance(stored, bool):
        return stored
    return str(stored).strip().lower() not in ("", "0", "false")


def load_enum_setting(key: str, default: _EnumT) -> _EnumT:
    """An app-wide appearance/interaction preference out of QSettings.

    The app-global preferences (the grid, selection shape, active tool) are
    stored by their enum's string ``value``, so the settings file stays readable
    and stable. A stored value this build has no member for — an older or newer
    celPix wrote the settings — falls back to ``default`` rather than raising: a
    stale preference is not a reason to fail to start.
    """
    return load_setting(key, default, type(default))


def save_enum_setting(key: str, value: Enum) -> None:
    """Persist an app-wide preference — the write half of
    :func:`load_enum_setting`, storing the enum's ``value`` so the two agree on
    the on-disk form in one place rather than at each call site."""
    settings().setValue(key, value.value)


def load_bool_setting(key: str, default: bool) -> bool:
    """An app-wide on/off preference out of QSettings (:func:`_to_bool`)."""
    return load_setting(key, default, _to_bool)


def save_bool_setting(key: str, value: bool) -> None:
    """Persist an app-wide on/off preference (see :func:`load_bool_setting`)."""
    settings().setValue(key, value)


def load_float_setting(key: str, default: float) -> float:
    """An app-wide numeric preference out of QSettings.

    Converted from whatever the backend hands back — the INI backend returns the
    string it wrote — and ``default`` when the stored text is not a number.
    """
    return load_setting(key, default, float)


def save_float_setting(key: str, value: float) -> None:
    """Persist an app-wide numeric preference (see :func:`load_float_setting`)."""
    settings().setValue(key, value)


# The recently opened projects, newest first. App-wide rather than per-project:
# the list is how you get *back* to a project, so it cannot live inside one. Ten
# is deep enough to reach last week's work while the menu stays a menu.
RECENT_PROJECTS_KEY = "recent/projects"
MAX_RECENT_PROJECTS = 10


def _recent_path(path: str) -> str:
    """A project path in the one spelling the list stores it under.

    The same project reaches us spelled differently depending on how it was
    opened — Qt hands back a dropped file's URL with POSIX separators even on
    Windows, where the file dialog's answer is native — and a list that stores
    both grows a second row for a project the user only has one of. Separators
    are normalized for storage; case only for the comparison (:func:`_recent_key`),
    since the name still has to be shown as the file system spells it.
    """
    return os.path.normpath(os.path.abspath(path))


def _recent_key(path: str) -> str:
    """The identity a recent path is de-duplicated by — case-folded on the
    platforms whose file names are (Windows), so a project opened as
    ``D:\\roms`` and again as ``d:\\ROMS`` stays one row."""
    return os.path.normcase(_recent_path(path))


def load_recent_projects() -> list[str]:
    """The remembered project paths, newest first.

    A one-item list comes back out of QSettings as a bare string — the INI
    backend can't tell a single-element list from a scalar — so both shapes are
    folded to a list here rather than at each call site.
    """
    stored = settings().value(RECENT_PROJECTS_KEY)
    if stored is None:
        return []
    if isinstance(stored, str):
        return [stored] if stored else []
    return [str(path) for path in stored]


def remember_recent_project(path: str) -> None:
    """Record ``path`` as the newest recent project, dropping the oldest over
    :data:`MAX_RECENT_PROJECTS`. Re-opening a listed project moves it back to
    the top rather than listing it twice."""
    key = _recent_key(path)
    recent = [p for p in load_recent_projects() if _recent_key(p) != key]
    recent.insert(0, _recent_path(path))
    settings().setValue(RECENT_PROJECTS_KEY, recent[:MAX_RECENT_PROJECTS])


def forget_recent_project(path: str) -> None:
    """Drop ``path`` from the recent list — for a project that is no longer
    where it was, which the list itself has no way of noticing."""
    key = _recent_key(path)
    settings().setValue(
        RECENT_PROJECTS_KEY,
        [p for p in load_recent_projects() if _recent_key(p) != key],
    )


def clear_recent_projects() -> None:
    """Forget every recent project."""
    settings().remove(RECENT_PROJECTS_KEY)
