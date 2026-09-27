"""Reading a preset strictly, because lenient is wrong.

A parameter with a fixed set of values invites the shortest possible read - test
for one value and treat everything else as the other. That turns a typo into the
*opposite* setting rather than an error: ``byte_order = "Little"`` reads
big-endian, ``msb_first = "false"`` (a string, so truthy) reads true, and either
draws a plausible picture in the wrong colours with nothing pointing at the
preset. Every engine reads such a parameter through here, so a bad value is a
:class:`ValueError` naming the key, which the pipeline reports against the entry.

The same goes for a key the engine never reads at all. A preset adapted into a
plugin at load (the reshape presets) is read once, by its engine, so a misspelt
``units = 2`` is dropped and the join runs at the default unit - a picture in
the wrong colours again, from a file that looks right. Those engines name the
keys they read (:func:`only_keys`), and check the preset's own ``id`` and
``name`` the same way (:func:`preset_identity`), since each becomes the plugin's.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Any, Literal


def one_of(params: Mapping[str, Any], key: str, allowed: tuple, default: Any) -> Any:
    """``params[key]``, which must be one of ``allowed``; ``default`` when absent."""
    value = params.get(key, default)
    # Compared with its type, so a TOML ``true`` is not taken for the integer 1.
    if not any(value == item and type(value) is type(item) for item in allowed):
        choices = " or ".join(
            str(item).lower() if isinstance(item, bool) else repr(item)
            for item in allowed
        )
        raise ValueError(f"{key} must be {choices}, got {value!r}")
    return value


def flag(params: Mapping[str, Any], key: str, default: bool = False) -> bool:
    """The boolean parameter ``key``: ``true`` or ``false``, nothing truthy."""
    return one_of(params, key, (True, False), default)


def byte_order(
    params: Mapping[str, Any], key: str, default: str
) -> Literal["little", "big"]:
    """The ``"little"``/``"big"`` parameter ``key``."""
    return one_of(params, key, ("little", "big"), default)


def only_keys(params: Mapping[str, Any], known: Collection[str]) -> None:
    """Refuse any key of ``params`` outside ``known``, the keys the engine reads."""
    unknown = sorted(set(params) - set(known))
    if unknown:
        noun = "parameter" if len(unknown) == 1 else "parameters"
        raise ValueError(
            f"unknown {noun} {', '.join(f'params.{key}' for key in unknown)} - "
            f"this engine reads {', '.join(sorted(known))}"
        )


def preset_identity(spec: Mapping[str, Any]) -> tuple[str, str, str]:
    """A preset's ``id``, ``name`` and ``category``, each checked as text.

    ``id`` and ``name`` are required and non-empty: the id is what a project
    stores to name the plugin, and a number there would register under a key no
    project file can spell back. ``category`` is optional; any heading is allowed
    (an unlisted one sorts after the known ones), but it has to be a string.
    """
    values = []
    for key in ("id", "name"):
        value = spec.get(key)
        if value is None:
            raise ValueError(f"{key} is required")
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{key} must be a non-empty string, got {value!r}")
        values.append(value)
    category = spec.get("category", "")
    if not isinstance(category, str):
        raise ValueError(f"category must be a string, got {category!r}")
    return values[0], values[1], category
