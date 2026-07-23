"""Stage-agnostic data model shared by every pipeline stage.

Home for the byte-buffer / index-grid / palette model and the forward-flowing,
advisory context/hints bag (``docs/design/overview.md`` §5). The host owns this
machinery so that plugins can stay thin (§3).
"""

from __future__ import annotations


def ceil_div(numerator: int, denominator: int) -> int:
    """``ceil(numerator / denominator)`` in exact integer arithmetic.

    The ``-(-a // b)`` trick, named once so the many tile/row/entry counts that
    round a partial unit up (a trailing partial tile still counts as viewable)
    stop open-coding it. ``denominator`` must be non-zero — callers that can
    reach zero geometry guard it themselves.
    """
    return -(-numerator // denominator)
