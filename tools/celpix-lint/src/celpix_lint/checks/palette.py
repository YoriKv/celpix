"""The `session` block and the `palette` block, which have to agree.

``session.palette_mode`` says where the colors come from and ``palette`` is the
source itself, and nothing reconciles them at load: a mode of ``"file"`` beside
a ``palette`` holding ``colors`` reads the mode, finds no path, and falls back
to the generated default palette. The entry opens grey with no complaint, which
is the most common way one of these files goes wrong by hand.
"""

from __future__ import annotations

import re

from celpix_lint.context import Context, EntryView, region_size
from celpix_lint.schema import (
    PALETTE_KEYS,
    PALETTE_MODE_NEEDS,
    PALETTE_MODES,
    SESSION_KEYS,
    is_int,
)

_HEX = re.compile(r"^#?[0-9A-Fa-f]{1,8}$")


def check(ctx: Context) -> None:
    for entry in ctx.entries:
        if not entry.raw:
            continue
        mode = _session(ctx, entry)
        _palette(ctx, entry, mode)


def _session(ctx: Context, entry: EntryView) -> str | None:
    """Check the session block; returns the palette mode the loader will read."""
    if "session" not in entry.raw:
        return None
    session = entry.raw["session"]
    if not isinstance(session, dict):
        ctx.error(
            "E600",
            f"`session` is {type(session).__name__}, not an object — the entry opens "
            "on default formats",
            pointer=entry.at("session"),
            entry=entry,
            detail="Its four keys are the pixel and palette presets, the palette mode "
            "and the preview compression.",
        )
        return None
    for key in session:
        if key not in SESSION_KEYS:
            ctx.warn(
                "W601",
                f"unknown key {key!r} in `session` — the reader ignores it",
                pointer=entry.at("session", key),
                entry=entry,
                detail=f"The four it reads are: {', '.join(sorted(SESSION_KEYS))}.",
            )
    raw_mode = session.get("palette_mode")
    if raw_mode is None:
        return "default"
    if raw_mode not in PALETTE_MODES:
        ctx.error(
            "E602",
            f'`session.palette_mode` is {raw_mode!r} — it reads as "default"',
            pointer=entry.at("session", "palette_mode"),
            entry=entry,
            detail=f"One of: {', '.join(PALETTE_MODES)}. The entry opens on the "
            "generated fallback palette instead of the colors it names.",
        )
        return "default"
    return raw_mode


def _palette(ctx: Context, entry: EntryView, mode: str | None) -> None:
    raw = entry.raw.get("palette")
    if raw is None:
        if mode and mode != "default":
            needed = PALETTE_MODE_NEEDS[mode]
            detail = (
                f'The "{mode}" mode reads `palette.{needed}`. Without it the entry '
                "falls back to the generated default palette."
            )
            if mode == "offset":
                # The one mode whose missing key has a *value*, not just an
                # absence: an unstated offset is 0, so it reads real bytes from
                # the wrong place rather than nothing from anywhere.
                detail += " An unstated offset is 0, so the colors are read from the "
                detail += "start of the file."
            ctx.error(
                "E610",
                f'`palette_mode` is "{mode}" but the entry has no `palette` block',
                pointer=entry.at("palette"),
                entry=entry,
                detail=detail,
            )
        return
    if not isinstance(raw, dict):
        ctx.error(
            "E611",
            f"`palette` is {type(raw).__name__}, not an object — the entry falls back "
            "to the default palette",
            pointer=entry.at("palette"),
            entry=entry,
        )
        return
    for key in raw:
        if key not in PALETTE_KEYS:
            ctx.warn(
                "W612",
                f"unknown key {key!r} in `palette` — the reader ignores it",
                pointer=entry.at("palette", key),
                entry=entry,
            )
    _shape(ctx, entry, raw, mode)
    _colors(ctx, entry, raw)
    _source_file(ctx, entry, raw, mode)


def _shape(ctx: Context, entry: EntryView, raw: dict, mode: str | None) -> None:
    """Does the block hold what the mode will look for?

    The reader picks by *shape*, in order — colors, then path, then offset — so
    a block can be well-formed and still be read as a different source than the
    mode names.
    """
    has = {key: key in raw for key in PALETTE_KEYS}
    if not any(has.values()):
        ctx.warn(
            "W620",
            "`palette` is empty and names no source",
            pointer=entry.at("palette"),
            entry=entry,
            detail="The entry falls back to the generated default palette.",
        )
        return
    if mode in (None, "default"):
        ctx.warn(
            "W621",
            f'`palette` names a source but `palette_mode` is "{mode or "default"}"',
            pointer=entry.at("palette"),
            entry=entry,
            detail="The mode decides where colors come from, so this block is never "
            "read and the entry draws through the generated default palette.",
        )
        return
    needed = PALETTE_MODE_NEEDS[mode]
    if not has[needed]:
        found = ", ".join(key for key, present in has.items() if present)
        ctx.error(
            "E622",
            f'`palette_mode` is "{mode}", which reads `palette.{needed}` — the block '
            f"holds {found}",
            pointer=entry.at("palette"),
            entry=entry,
            detail="The entry falls back to the generated default palette.",
        )
        return
    # `colors` wins over `path` wins over `offset`, whatever the mode says.
    if needed != "colors" and has["colors"]:
        ctx.error(
            "E623",
            f'`palette` holds `colors`, which is read before `{needed}` — the "{mode}" '
            "source is ignored",
            pointer=entry.at("palette", "colors"),
            entry=entry,
            detail="A custom palette is stored in the project itself; remove it, or "
            'set `palette_mode` to "custom".',
        )
    elif needed == "offset" and has["path"]:
        ctx.error(
            "E624",
            '`palette` holds `path`, which is read before `offset` — the "offset" '
            "source is ignored",
            pointer=entry.at("palette", "path"),
            entry=entry,
            detail='An "offset" palette is read from the entry\'s own file; set '
            '`palette_mode` to "file" if the separate file is what was meant.',
        )


def _colors(ctx: Context, entry: EntryView, raw: dict) -> None:
    if "colors" not in raw:
        return
    colors = raw["colors"]
    if not isinstance(colors, list):
        ctx.error(
            "E630",
            f"`palette.colors` is {type(colors).__name__}, not an array",
            pointer=entry.at("palette", "colors"),
            entry=entry,
        )
        return
    for at, color in enumerate(colors):
        if isinstance(color, str) and _HEX.match(color):
            if len(color.lstrip("#")) != 8:
                ctx.info(
                    "I631",
                    f"`palette.colors[{at}]` is {color!r} — celPix writes #AARRGGBB",
                    pointer=entry.at("palette", "colors", at),
                    entry=entry,
                    detail="It parses, but a short form leaves the alpha at 0, which "
                    "is fully transparent.",
                )
            continue
        ctx.error(
            "E632",
            f"`palette.colors[{at}]` is {color!r}, not an #AARRGGBB hex string",
            pointer=entry.at("palette", "colors", at),
            entry=entry,
            detail="One unparseable color discards the **whole** custom palette and "
            "the entry falls back to the generated default.",
        )
        return


def _source_file(ctx: Context, entry: EntryView, raw: dict, mode: str | None) -> None:
    offset = raw.get("offset")
    if "offset" in raw and (not is_int(offset) or offset < 0):
        ctx.error(
            "E640",
            f"`palette.offset` is {offset!r} — it must be a byte offset from 0",
            pointer=entry.at("palette", "offset"),
            entry=entry,
            detail="It reads as 0, so the palette is taken from the start of the file.",
        )
        return
    path = raw.get("path")
    if isinstance(path, str) and path:
        if ctx.check_files and not ctx.doc.exists(path):
            ctx.error(
                "E641",
                f"the palette file {path!r} does not exist",
                pointer=entry.at("palette", "path"),
                entry=entry,
                detail="The entry still opens and draws — it degrades quietly to the "
                "default palette, keeping the reference so a later relocate restores "
                "it. celPix offers to locate it, tagged `(palette)`.",
            )
            return
    elif "path" in raw:
        ctx.error(
            "E642",
            f"`palette.path` is {path!r}, not a path",
            pointer=entry.at("palette", "path"),
            entry=entry,
        )
        return
    if mode == "emulator" and is_int(offset) and offset:
        ctx.info(
            "I643",
            "`palette.offset` is ignored for an emulator state",
            pointer=entry.at("palette", "offset"),
            entry=entry,
            detail="The palette's position inside the state and the console's color "
            "codec are both re-detected on load, so a stale cached offset cannot win "
            "over a newer detector. celPix writes the key only for uniformity.",
        )
        return
    # Where the colors come out of a file we can measure, say so when the read
    # would start past its end. Not fatal — the entry degrades to the default.
    if not ctx.check_files or not is_int(offset) or offset <= 0:
        return
    if isinstance(path, str) and path:
        # A separate palette file: one file, measured on its own.
        size = ctx.doc.size_of(path)
    else:
        # An "offset" palette reads the entry's own bytes, which for a region
        # joined from several chips is the whole join.
        size = region_size(ctx.doc, entry)
    if size is not None and offset >= size:
        ctx.warn(
            "W644",
            f"`palette.offset` 0x{offset:X} is past the end of the 0x{size:X}-byte "
            f"file it reads from",
            pointer=entry.at("palette", "offset"),
            entry=entry,
            detail="No colors are read there and the entry falls back to the default "
            "palette.",
        )
