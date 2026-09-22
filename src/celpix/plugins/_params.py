"""Reading a preset's closed-set parameters: strict, because lenient is wrong.

A parameter with a fixed set of values invites the shortest possible read - test
for one value and treat everything else as the other. That turns a typo into the
*opposite* setting rather than an error: ``byte_order = "Little"`` reads
big-endian, ``msb_first = "false"`` (a string, so truthy) reads true, and either
draws a plausible picture in the wrong colours with nothing pointing at the
preset. Every engine reads such a parameter through here, so a bad value is a
:class:`ValueError` naming the key, which the pipeline reports against the entry.
"""

from __future__ import annotations

from collections.abc import Mapping
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
