"""Reading a preset strictly, because lenient is wrong.

A parameter with a fixed set of values invites the shortest possible read - test
for one value and treat everything else as the other. That turns a typo into the
*opposite* setting rather than an error: ``byte_order = "Little"`` reads
big-endian, ``msb_first = "false"`` (a string, so truthy) reads true, and either
draws a plausible picture in the wrong colours with nothing pointing at the
preset. Engines read such a parameter through here, so a bad value is a
:class:`ValueError` naming the key, which the pipeline reports against the entry.
A number is the same trap one step removed: ``bpp = "4"`` or ``unit = true``
reads as a number to Python and as a typo to everyone else, so :func:`integer`
refuses anything but a TOML integer.

The same goes for a key the engine never reads at all. A preset adapted into a
plugin at load (the reshape presets) is read once, by its engine, so a misspelt
``units = 2`` is dropped and the join runs at the default unit - a picture in
the wrong colours again, from a file that looks right. Those engines name the
keys they read (:func:`only_keys`), and check the preset's own ``id`` and
``name`` the same way (:func:`preset_identity`), since each becomes the plugin's
— the whole prologue is :func:`adapter_spec`. The interpret engines leave an
unknown key alone: their presets are read by the host as well, which has keys of
its own there, so a closed list would have to be the union of both.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Any, Literal

from celpix.core.errors import Stage
from celpix.plugins.base import check_declared_stage


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


def integer(
    params: Mapping[str, Any],
    key: str,
    default: int | None = None,
    *,
    low: int | None = None,
    high: int | None = None,
) -> int:
    """The integer parameter ``key``, within ``low..high`` where either is given.

    ``default`` None makes the key required. A bool is refused even though Python
    counts it as an int: ``parts = true`` is a typo, not a 1.
    """
    value = params.get(key, default)
    if value is None:
        raise ValueError(f"{key} is required")
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{key} must be an integer, got {value!r}")
    if (low is not None and value < low) or (high is not None and value > high):
        if high is None:
            span = f"at least {low}"
        elif low is None:
            span = f"at most {high}"
        else:
            span = f"{low}..{high}"
        raise ValueError(f"{key} must be {span}, got {value}")
    return value


def int_list(
    params: Mapping[str, Any], key: str, default: tuple[int, ...] = ()
) -> tuple[int, ...]:
    """The list-of-integers parameter ``key``, as a tuple; ``default`` when absent."""
    value = params.get(key, default)
    if not isinstance(value, (list, tuple)) or not all(
        isinstance(item, int) and not isinstance(item, bool) for item in value
    ):
        raise ValueError(f"{key} must be a list of integers, got {value!r}")
    return tuple(value)


def only_keys(params: Mapping[str, Any], known: Collection[str]) -> None:
    """Refuse any key of ``params`` outside ``known``, the keys the engine reads."""
    unknown = sorted(set(params) - set(known))
    if unknown:
        noun = "parameter" if len(unknown) == 1 else "parameters"
        raise ValueError(
            f"unknown {noun} {', '.join(f'params.{key}' for key in unknown)} - "
            f"this engine reads {', '.join(sorted(known))}"
        )


def required_text(spec: Mapping[str, Any], key: str) -> str:
    """The required, non-empty string ``spec[key]``."""
    value = spec.get(key)
    if value is None:
        raise ValueError(f"{key} is required")
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string, got {value!r}")
    return value


def params_table(spec: Mapping[str, Any]) -> dict[str, Any]:
    """A preset's ``params`` table, empty when absent.

    A list or a scalar there would otherwise register and fail only at decode,
    inside the engine, on every entry that picks the preset.
    """
    params = spec.get("params", {})
    if not isinstance(params, dict):
        raise ValueError(f"params must be a table, got {params!r}")
    return params


def preset_identity(spec: Mapping[str, Any]) -> tuple[str, str, str]:
    """A preset's ``id``, ``name`` and ``category``, each checked as text.

    ``id`` and ``name`` are required and non-empty: the id is what a project
    stores to name the plugin, and a number there would register under a key no
    project file can spell back. ``category`` is optional; any heading is allowed
    (an unlisted one sorts after the known ones), but it has to be a string.
    """
    category = spec.get("category", "")
    if not isinstance(category, str):
        raise ValueError(f"category must be a string, got {category!r}")
    return required_text(spec, "id"), required_text(spec, "name"), category


def adapter_spec(
    spec: dict[str, Any],
    *,
    engine_id: str,
    stage: Stage,
    known: Collection[str],
) -> tuple[str, str, str, dict[str, Any]]:
    """``(id, name, category, params)`` of a preset adapted into a plugin at load.

    The prologue every such adapter shares: the spec must name this engine, sit
    in the folder of its stage, carry an identity of its own
    (:func:`preset_identity`) and a ``params`` table holding only the keys the
    engine reads (``known``, :func:`only_keys`).
    """
    engine = spec.get("engine_id")
    if engine != engine_id:
        raise ValueError(
            f"engine_id {engine!r} is not this engine (expected {engine_id!r})"
        )
    check_declared_stage(spec, stage)
    plugin_id, name, category = preset_identity(spec)
    params = params_table(spec)
    only_keys(params, known)
    return plugin_id, name, category, params
