"""Checks on the document itself — version, `current`, and the two view settings."""

from __future__ import annotations

from celpix_lint.context import Context
from celpix_lint.schema import KINDS_WITH_DOCUMENT, TOP_LEVEL_KEYS, is_int


def check(ctx: Context) -> None:
    _version(ctx)
    _current(ctx)
    _entries_present(ctx)
    _hidden_presets(ctx)
    _pixel_aspect(ctx)
    _unknown_keys(ctx)


def _version(ctx: Context) -> None:
    data = ctx.doc.data
    known = ctx.ids.project_version
    if "version" not in data:
        ctx.warn(
            "W101",
            "no `version` — the reader assumes 1",
            pointer="/version",
            detail='Every project celPix writes is version-stamped; add "version": '
            f"{known}.",
        )
        return
    version = data["version"]
    if not is_int(version):
        ctx.error(
            "E102",
            f"`version` is {version!r}, not an integer",
            pointer="/version",
            detail="A non-integer version reads as 1, so a newer file would open "
            "with no warning that saving will rewrite it.",
        )
        return
    if version > known:
        ctx.warn(
            "W103",
            f"`version` {version} is newer than this build understands ({known})",
            pointer="/version",
            detail="celPix will open it tolerantly, but saving rewrites the file at "
            f"version {known} and drops whatever it did not understand.",
        )
    elif version < 1:
        ctx.error(
            "E104",
            f"`version` {version} is not a real schema version",
            pointer="/version",
        )


def _current(ctx: Context) -> None:
    data = ctx.doc.data
    if "current" not in data:
        return
    current = data["current"]
    if current is None:
        return
    if not is_int(current):
        ctx.error(
            "E110",
            f"`current` is {current!r} — it must be an entry index or null",
            pointer="/current",
            detail="Anything else reads as no-current; the project opens on nothing.",
        )
        return
    entries = ctx.doc.entries
    if not 0 <= current < len(entries):
        ctx.error(
            "E111",
            f"`current` is {current}, outside the {len(entries)} entries",
            pointer="/current",
            detail="The project opens with no entry shown.",
        )
        return
    view = ctx.entries[current] if current < len(ctx.entries) else None
    if view is not None and view.kind not in KINDS_WITH_DOCUMENT:
        ctx.error(
            "E112",
            f"`current` names entry {current}, a {view.kind}, which cannot be shown",
            pointer="/current",
            entry=view,
            detail="A bookmark is a position and a palette is applied rather than "
            "opened; the project opens with no entry shown.",
        )


def _entries_present(ctx: Context) -> None:
    if "entries" not in ctx.doc.data:
        ctx.error(
            "E105",
            "no `entries` array — the project holds nothing",
            pointer="/entries",
        )


def _hidden_presets(ctx: Context) -> None:
    data = ctx.doc.data
    if "hidden_pixel_presets" not in data:
        return
    hidden = data["hidden_pixel_presets"]
    if not isinstance(hidden, list):
        ctx.warn(
            "W120",
            "`hidden_pixel_presets` is not an array — the filter is ignored",
            pointer="/hidden_pixel_presets",
        )
        return
    for at, item in enumerate(hidden):
        if not isinstance(item, str):
            ctx.warn(
                "W121",
                f"`hidden_pixel_presets[{at}]` is not a string and is dropped",
                pointer=f"/hidden_pixel_presets/{at}",
            )
        elif ctx.ids.usable and not ctx.ids.has("interpret-pixel", item):
            # Harmless — it hides nothing — so this stays informational even
            # against a live registry.
            ctx.info(
                "I122",
                f"`hidden_pixel_presets` names {item!r}, which is not a pixel preset",
                pointer=f"/hidden_pixel_presets/{at}",
                detail="It hides nothing. Harmless, but likely a leftover.",
            )
    if len(set(map(str, hidden))) != len(hidden):
        ctx.info(
            "I123",
            "`hidden_pixel_presets` has duplicates",
            pointer="/hidden_pixel_presets",
            detail="celPix writes this sorted and unique.",
        )
    elif hidden != sorted(hidden, key=str):
        ctx.info(
            "I124",
            "`hidden_pixel_presets` is not sorted",
            pointer="/hidden_pixel_presets",
            detail="celPix writes it sorted so the file diffs cleanly.",
        )


def _pixel_aspect(ctx: Context) -> None:
    data = ctx.doc.data
    if "pixel_aspect" not in data:
        return
    aspect = data["pixel_aspect"]
    ok = (
        isinstance(aspect, list)
        and len(aspect) == 2
        and all(is_int(v) and v > 0 for v in aspect)
    )
    if not ok:
        ctx.warn(
            "W130",
            f"`pixel_aspect` is {aspect!r} — it must be [width, height], both positive",
            pointer="/pixel_aspect",
            detail="A malformed ratio reads as unanswered, which lets the container's "
            "own hint set the shape instead. That is not the same as [1, 1].",
        )


def _unknown_keys(ctx: Context) -> None:
    for key in ctx.doc.data:
        if key not in TOP_LEVEL_KEYS:
            ctx.warn(
                "W140",
                f"unknown top-level key {key!r} — the reader ignores it",
                pointer=f"/{key}",
                detail="A misspelled key reads as an absent one, so whatever it was "
                "meant to set is at its default.",
            )
