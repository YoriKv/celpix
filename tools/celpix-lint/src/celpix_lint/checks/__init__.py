"""The check passes, in the order they run.

Order matters only for readability of the code — every pass reports
independently, and none consumes another's findings. It is arranged
outside-in: the document, then each entry's own shape, then what the entry
points at (the disk, the registry, the other entries), then the three nested
blocks.
"""

from __future__ import annotations

from celpix_lint.checks import (
    crossref,
    entries,
    files,
    font,
    ids,
    palette,
    toplevel,
    view,
)

PASSES = (
    toplevel.check,
    entries.check,
    files.check,
    ids.check,
    crossref.check,
    view.check,
    palette.check,
    font.check,
)

__all__ = ["PASSES"]
