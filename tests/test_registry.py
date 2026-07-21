"""Registry error-handling: unknown lookups raise, duplicates are rejected.

The built-in plugins and presets being present is covered transitively — the codec
and pipeline tests decode/round-trip through every one of them, so a missing
registration or broken resource load fails there.
"""

from __future__ import annotations

import pytest

from celpix.core.errors import Stage
from celpix.plugins.registry import default_registry


def test_unknown_lookup_raises() -> None:
    reg = default_registry()
    with pytest.raises(KeyError):
        reg.plugin(Stage.READ, "nope")
    with pytest.raises(KeyError):
        reg.preset("nope")


def test_duplicate_registration_rejected() -> None:
    reg = default_registry()
    with pytest.raises(ValueError):
        reg.register(reg.plugin(Stage.READ, "read.raw-file"))
