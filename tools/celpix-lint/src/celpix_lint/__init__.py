"""Static checks for hand-edited ``.celpix`` project files.

A celPix project loads *tolerantly*: an unrecognised ``kind`` reads as
``"file"``, a preset id nothing answers to degrades to the stage default, a
malformed glyph record is skipped, a tile binding onto a missing entry leaves
the map unbound. Every one of those is the right behaviour for a loader — a
project that will not open is worse than one that opens degraded — and every
one of them is silent.

This package reports what a load would silently change, by checking the
document rather than the parse result. See :mod:`celpix_lint.schema` for why it
restates the schema instead of importing celPix's own reader.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
