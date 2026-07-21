"""Access to bundled, read-only resources shipped inside the package.

This is where data-first plugin material and UI assets live — format/metadata
tables and built-in presets under ``data/``, plus icons and the pixel font as we
add them. Keeping them *inside* ``celpix`` (rather than a sibling data dir) means
they are packaged into the wheel and resolvable via :func:`resource`.

Paths are resolved with :mod:`importlib.resources`, so they work identically in a
source checkout and in a frozen/one-file build (PyInstaller relocates files to a
temp dir, which ``__file__``-relative lookups miss).

    from celpix import resources
    text = resources.read_text("data", "presets", "snes-4bpp.toml")
"""

from __future__ import annotations

from importlib.resources import files
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Traversable moved between modules across 3.9–3.13; import it only for
    # type-checkers (with `from __future__ import annotations`, the annotations
    # below are never evaluated at runtime, so there is no version-specific
    # import or deprecation warning in the shipped code).
    try:
        from importlib.resources.abc import Traversable
    except ImportError:  # Python 3.9 / 3.10
        from importlib.abc import Traversable

_ANCHOR = "celpix.resources"


def resource(*parts: str) -> Traversable:
    """Return a Traversable for ``celpix/resources/<parts...>``."""
    node = files(_ANCHOR)
    for part in parts:
        node = node / part
    return node


def read_bytes(*parts: str) -> bytes:
    """Read a bundled resource as bytes."""
    return resource(*parts).read_bytes()


def read_text(*parts: str, encoding: str = "utf-8") -> str:
    """Read a bundled text resource."""
    return resource(*parts).read_text(encoding=encoding)
