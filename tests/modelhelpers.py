"""Qt-free readers the model suites share — the sibling of ``uihelpers``.

Same rule as that module: a helper lands here once more than one ``test_*.py``
needs it, and one used by a single suite stays in that suite. The split is the
one the app itself draws — ``uihelpers`` builds windows and imports Qt, so a
suite that runs under ``-m "not qt"`` cannot reach for it.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from celpix.core.context import PipelineContext


def decoded_at_probe_length(
    engine: Any, params: dict, fill: Callable[[int], bytes]
) -> tuple[list, bytes, PipelineContext]:
    """``(cells, data, ctx)`` at the first length ``engine`` decodes anything from.

    **Grown rather than assumed to be four cells.** A cell stride is what most
    tilemap formats read at, but one whose colour lives in a plane *after* its
    cells reads a whole page or nothing — the two planes mean nothing apart — so
    a four-byte buffer is not a short map there, it is not a map at all. A caller
    that hard-codes one length silently stops covering the next format that needs
    another, which is how this ended up written twice.

    ``fill`` makes the probe bytes, and is the caller's because what may be
    asserted about the round trip differs. A preset whose layout leaves bits
    unclaimed (``.`` in its ``fields``) writes them back as zero, so only an
    all-zero probe is byte-exact across *every* shipped preset; a caller covering
    engines that model every bit passes something stronger, which catches a field
    dropped on the way through.

    The context is the helper's and comes back with the cells, so an encode is
    checked against the one that decoded — a format that publishes something
    about the file it just read (a page geometry, an attribute plane) needs its
    own read's answers and not a previous probe's.

    Empty cells and ``b""`` when nothing decoded at any length, for the caller to
    assert against with its own name for the format.
    """
    for length in (engine.bytes_per_cell(params) * 4, 1024, 2048):
        ctx = PipelineContext()
        data = fill(length)
        cells = engine.decode(data, params, ctx)
        if cells:
            return cells, data, ctx
    return [], b"", PipelineContext()
