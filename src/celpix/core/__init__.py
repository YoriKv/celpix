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


def fit_length(data: bytes, size: int) -> bytes:
    """``data`` cut at its tail, or grown there with zero bytes, to ``size``.

    How a run of fixed-size units is resized (``docs/design/new-file.md`` §6):
    the tail is the only place units can be added or removed without moving the
    ones already there, and zero is what an empty unit reads as for every codec
    celPix carries. Named once because a new file, a resize and a codec with its
    own say over its runs all need exactly this and must not drift apart.
    """
    if size < len(data):
        return data[: max(0, size)]
    return data + bytes(size - len(data))
