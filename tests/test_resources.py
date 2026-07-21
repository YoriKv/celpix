"""The bundled-resource accessor resolves inside the package (dev and frozen)."""

from __future__ import annotations

from celpix import resources


def test_data_dir_resolves() -> None:
    # Anchored at the package, so this holds in a checkout and in a frozen build.
    assert resources.resource("data").is_dir()
