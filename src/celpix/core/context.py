"""The forward-flowing pipeline context.

Stages are decoupled but not blind: each may *read* what earlier stages recorded
and *contribute* entries for later ones. **Everything here is advisory** — a
recommendation a downstream stage or the user may follow, adjust, or ignore, never
an enforced constraint (see ``docs/design/overview.md`` §5).

For the MVP the one concrete use is **provenance**: the Read stage records where
the bytes came from so the Write stage can default to writing them back to the
same place. The bag is intentionally an open, typed key/value store — plugins may
define new keys and stages ignore keys they do not understand.
"""

from __future__ import annotations

from typing import Any

# Well-known context keys. Plugins may add their own; these are the ones the
# built-in stages agree on. Kept as constants so producers and consumers can't
# drift on the spelling.
KEY_SOURCE_PATH = "source.path"  # str: filesystem path the bytes were read from
KEY_SOURCE_OFFSET = "source.offset"  # int: byte offset within that source


class PipelineContext:
    """An open key/value bag of advisory recommendations, per pathway."""

    __slots__ = ("_entries",)

    def __init__(self) -> None:
        self._entries: dict[str, Any] = {}

    def set(self, key: str, value: Any) -> None:
        self._entries[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        return self._entries.get(key, default)

    def __contains__(self, key: str) -> bool:
        return key in self._entries

    def __repr__(self) -> str:
        return f"PipelineContext({sorted(self._entries)})"
