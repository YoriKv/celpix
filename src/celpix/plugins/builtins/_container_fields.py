"""The rows and header guards several containers spell the same way.

A container's info popup is read across formats — a user opening a SNES ROM,
then a Mega Drive one, compares the checksum rows — so a readout that means the
same thing should read the same way everywhere. These are the ones that recur:
a checksum next to the value it should hold, a file the container passes through
untouched, the magic-and-length guard a file format opens with, and the
fixed-width text field a header carries a name in.
"""

from __future__ import annotations

from collections.abc import Callable

from celpix.plugins.base import ContainerField

# The second line of every "passed through" tooltip: what a save does with a file
# the container found nothing in to maintain.
_UNTOUCHED = "A save writes the bytes through untouched"


def checksum_field(
    label: str, stored: int, computed: int, *, digits: int = 4, detail: str
) -> ContainerField:
    """A checksum row: the file's copy, the right value, and whether they agree.

    Both values are always shown rather than one when they match: which of them
    a save will write is the question the row answers, and a dump whose copy
    already disagrees was edited by something that did not repair it.
    """
    verdict = " - matches" if stored == computed else " - stale"
    return ContainerField(
        label,
        f"${stored:0{digits}X} stored, ${computed:0{digits}X} computed{verdict}",
        detail,
    )


def untouched_field(label: str, value: str, reason: str) -> ContainerField:
    """A row saying the container found nothing to maintain, and why."""
    return ContainerField(label, value, f"{reason}\n{_UNTOUCHED}")


def require_header(
    raw: bytes,
    size: int,
    magic: bytes,
    *,
    what: str,
    fail: Callable[[str], ValueError],
) -> None:
    """Refuse ``raw`` unless it holds a ``size``-byte header opening with ``magic``.

    ``what`` completes "file does not begin with …" in the format's own terms.
    The length is checked first so a truncated file is reported as truncated
    rather than as some other format.
    """
    if len(raw) < size:
        raise fail(f"file is {len(raw)} bytes; the header alone needs {size}")
    if raw[: len(magic)] != magic:
        raise fail(f"file does not begin with {what}")


def fixed_text(raw: bytes, encoding: str = "ascii") -> str:
    """Text out of a fixed-width header field: cut at the first NUL, stripped.

    The cut comes first because a tool fills the unused tail of such a field with
    NULs, or with whatever its buffer held after one, and neither is part of it.
    """
    return raw.split(b"\x00", 1)[0].decode(encoding, "replace").strip()
