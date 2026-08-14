"""Checks against the files on disk: do the references resolve, and do the
offsets land inside them.

These are the findings that cannot be had from the JSON alone, and they are the
ones that bite: an offset past the end of a ROM is a perfectly well-formed
project that opens on an empty document. Paths resolve exactly as celPix
resolves them — relative to the project file, case-insensitively — so a project
authored on Windows is not reported as broken under WSL.
"""

from __future__ import annotations

from celpix_lint.context import Context, EntryView, region_size
from celpix_lint.schema import is_int


def check(ctx: Context) -> None:
    if not ctx.check_files:
        return
    for view in ctx.entries:
        if not view.raw:
            continue
        _references(ctx, view)
        _bounds(ctx, view)
    _duplicates(ctx)


def _references(ctx: Context, view: EntryView) -> None:
    path = view.raw.get("path")
    if isinstance(path, str) and path:
        _one_reference(ctx, view, path, view.at("path"), "path")
    extra = view.raw.get("extra_paths")
    if isinstance(extra, list):
        for at, item in enumerate(extra):
            if isinstance(item, str) and item:
                _one_reference(
                    ctx, view, item, view.at("extra_paths", at), "extra_paths"
                )


def _one_reference(
    ctx: Context, view: EntryView, stored: str, pointer: str, key: str
) -> None:
    if not ctx.doc.exists(stored):
        detail = (
            "The entry stays listed and flagged; celPix offers to locate it. Every "
            "entry sharing this path is re-pointed together."
        )
        if key == "extra_paths":
            detail = (
                "A region is its files joined, so one missing chip moves every byte "
                "after it and leaves the whole entry unloadable."
            )
        ctx.error(
            "E301",
            f"{stored!r} does not exist",
            pointer=pointer,
            entry=view,
            detail=detail,
        )
        return
    if ctx.doc.is_dir(stored):
        ctx.error(
            "E302",
            f"{stored!r} is a directory, not a file",
            pointer=pointer,
            entry=view,
        )
        return
    if ctx.doc.size_of(stored) == 0:
        ctx.warn(
            "W303",
            f"{stored!r} is empty",
            pointer=pointer,
            entry=view,
            detail="The entry opens on no bytes.",
        )


def _bounds(ctx: Context, view: EntryView) -> None:
    if view.kind not in ("slice", "bookmark"):
        return
    key = "offset" if view.kind == "bookmark" else "slice_offset"
    offset = view.raw.get(key)
    if not is_int(offset) or offset < 0:
        return  # already reported as a malformed number
    size = region_size(ctx.doc, view)
    if size is None:
        return  # the file is missing; E301 has said so
    joined = " (joined)" if view.raw.get("extra_paths") else ""
    if offset >= size:
        ctx.error(
            "E310",
            f"`{key}` {_hex(offset)} is past the end of the {_hex(size)}-byte "
            f"file{joined}",
            pointer=view.at(key),
            entry=view,
            detail="The offset is absolute from byte 0 — it is not relative to the "
            "parent's container, so a copier header does not shift it.",
        )
        return
    length = view.raw.get("slice_length")
    if view.kind == "slice" and is_int(length) and length > 0:
        end = offset + length
        if end > size:
            ctx.error(
                "E311",
                f"the slice runs to {_hex(end)}, past the end of the {_hex(size)}-byte "
                f"file{joined}",
                pointer=view.at("slice_length"),
                entry=view,
                detail=f"{_hex(size - offset)} bytes are available from "
                f"{_hex(offset)}.",
            )


def _duplicates(ctx: Context) -> None:
    """Two entries editing the same file — refused in the UI, possible by hand.

    Only the kinds that *own* their bytes count: a slice and a bookmark name
    their parent's path on purpose, and a palette entry is a separate list even
    where its path coincides with a graphics file's.
    """
    seen: dict = {}
    for view in ctx.entries:
        if view.kind not in ("file",) or not view.raw:
            continue
        path = view.raw.get("path")
        if not (isinstance(path, str) and path) or not ctx.doc.exists(path):
            continue
        key = ctx.doc.identity(path)
        first = seen.setdefault(key, view)
        if first is not view:
            ctx.error(
                "E320",
                f"{path!r} is already open as entry {first.index}"
                + (f" ({first.name})" if first.name else ""),
                pointer=view.at("path"),
                entry=view,
                detail="Two file entries over one file means two documents editing the "
                "same bytes, each unaware of the other's writes. celPix refuses this "
                "when relocating; it cannot refuse it in a hand-edited file.",
            )


def _hex(value: int) -> str:
    return f"0x{value:X}"
