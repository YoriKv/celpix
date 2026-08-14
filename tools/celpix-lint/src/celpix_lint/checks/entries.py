"""Per-entry shape: the kind, the keys that kind may carry, and the scalars.

The recurring finding here is a key that is *read by nobody*. celPix reads a
fixed set of keys per kind and ignores the rest, so ``container_id`` on a slice
or ``slice_offset`` on a file is not rejected — it is simply never looked at,
and the setting the author thought they made was never made.
"""

from __future__ import annotations

from celpix_lint.context import Context, EntryView
from celpix_lint.schema import (
    CONTENT_KINDS,
    DEFAULT_SLOT_FILL,
    ENTRY_KEYS,
    KIND_ONLY,
    KINDS,
    PASSTHROUGH,
    REQUIRED,
    SLOT_FILLS,
    TILEMAP_ONLY,
    is_int,
)


def check(ctx: Context) -> None:
    for view in ctx.entries:
        _shape(ctx, view)
        if view.raw:
            _keys(ctx, view)
            _content_kind(ctx, view)
            _scalars(ctx, view)
            _slice_bounds(ctx, view)
            _legacy(ctx, view)


def _shape(ctx: Context, view: EntryView) -> None:
    if not view.raw:
        ctx.error(
            "E201",
            "entry is not a JSON object and is dropped from the project",
            pointer=view.pointer,
            entry=view,
            detail="It still counts for `current` and for every stored entry_index, "
            "so removing it renumbers those references.",
        )
        return
    raw_kind = view.raw_kind
    if raw_kind is None:
        ctx.warn(
            "W202",
            "no `kind` — the entry opens as a whole file",
            pointer=view.at("kind"),
            entry=view,
            detail=f"One of: {', '.join(KINDS)}.",
        )
    elif raw_kind not in KINDS:
        ctx.error(
            "E203",
            f"`kind` is {raw_kind!r}, which is not a kind — the entry opens as a file",
            pointer=view.at("kind"),
            entry=view,
            detail=f"One of: {', '.join(KINDS)}. Every key belonging to the kind that "
            "was meant is then ignored.",
        )
    if view.skipped:
        ctx.error(
            "E204",
            "no usable `path` — celPix drops this entry entirely",
            pointer=view.at("path"),
            entry=view,
            detail="Only a composite may have no path. The dropped entry still counts "
            "for `current` and for stored entry_index references.",
        )
    if view.kind == "composite" and "path" in view.raw:
        ctx.warn(
            "W205",
            "a composite carries a `path` — it is assembled from other entries and "
            "comes from no file",
            pointer=view.at("path"),
            entry=view,
            detail="The key is ignored.",
        )
    name = view.raw.get("name")
    if "name" in view.raw and not (isinstance(name, str) and name):
        ctx.info(
            "I206",
            f"`name` is {name!r} — the row falls back to the file's basename",
            pointer=view.at("name"),
            entry=view,
        )


def _keys(ctx: Context, view: EntryView) -> None:
    for key in view.raw:
        if key not in ENTRY_KEYS:
            ctx.warn(
                "W210",
                f"unknown key {key!r} — the reader ignores it",
                pointer=view.at(key),
                entry=view,
                detail="A misspelled key reads as an absent one.",
            )
            continue
        kinds = KIND_ONLY.get(key)
        if kinds is not None and view.kind not in kinds:
            ctx.warn(
                "W211",
                f"`{key}` is not read on a {view.kind} entry",
                pointer=view.at(key),
                entry=view,
                detail=f"Only {_listed(kinds)} entries carry it. "
                + _why_not(key, view.kind),
            )
    for key in REQUIRED.get(view.kind, ()):
        if key not in view.raw and not (key == "path" and view.skipped):
            severity = ctx.error if key != "pieces" else ctx.warn
            severity(
                "E212" if key != "pieces" else "W212",
                f"a {view.kind} entry needs `{key}`",
                pointer=view.at(key),
                entry=view,
                detail=_missing_detail(key, view.kind),
            )
    # Tilemap keys are gated on the content kind rather than the kind, so they
    # get their own pass.
    if not view.is_tilemap:
        for key in TILEMAP_ONLY:
            if key in view.raw:
                ctx.warn(
                    "W213",
                    f"`{key}` is only read on a tilemap entry",
                    pointer=view.at(key),
                    entry=view,
                    detail='Add "content_kind": "tilemap" if that is what these bytes '
                    "are; otherwise the key does nothing.",
                )
    if "font" in view.raw and view.content_kind != "pixels":
        ctx.warn(
            "W214",
            f"`font` is on a {view.content_kind} entry",
            pointer=view.at("font"),
            entry=view,
            detail="A font alphabet says what a tile *sheet* spells, so it belongs on "
            "the pixels entry holding the tiles — not on a map drawn through them.",
        )


def _listed(kinds) -> str:
    """``("file", "slice", "bookmark")`` as "file, slice or bookmark"."""
    if len(kinds) == 1:
        return kinds[0]
    return f"{', '.join(kinds[:-1])} or {kinds[-1]}"


def _why_not(key: str, kind: str) -> str:
    return {
        "container_id": "A slice reads through its parent's container, never its own.",
        "reshape_id": "",
        "slice_offset": "",
        "offset": "",
        "session": "A palette entry is a reference plus how to read it, nothing more.",
        "view": "A palette entry is never shown, so it has no view.",
        "palette": "A palette entry *is* a palette source; it does not have one.",
        "pieces": "Only a composite is assembled from other entries.",
        "palette_preset_id": "On anything else the codec lives at "
        "`session.palette_preset_id`.",
    }.get(key, "")


def _missing_detail(key: str, kind: str) -> str:
    return {
        "slice_offset": "Without it the slice starts at byte 0 of its parent.",
        "offset": "Without it the bookmark marks byte 0.",
        "pieces": "The composite assembles nothing and opens empty.",
        "path": "",
    }.get(key, "")


def _content_kind(ctx: Context, view: EntryView) -> None:
    raw = view.raw_content
    if raw is None:
        return
    if raw not in CONTENT_KINDS:
        ctx.error(
            "E220",
            f"`content_kind` is {raw!r} — the entry opens as pixels",
            pointer=view.at("content_kind"),
            entry=view,
            detail=f"One of: {', '.join(CONTENT_KINDS)}. A tilemap read as pixels "
            "shows "
            "its cell bytes as art.",
        )
        return
    if view.kind == "palette":
        ctx.info(
            "I221",
            "`content_kind` on a palette entry is ignored — its kind already says it",
            pointer=view.at("content_kind"),
            entry=view,
        )
    elif raw == "pixels":
        ctx.info(
            "I222",
            '`content_kind: "pixels"` is the default and celPix omits it',
            pointer=view.at("content_kind"),
            entry=view,
        )


def _scalars(ctx: Context, view: EntryView) -> None:
    raw = view.raw
    if "slot_fill" in raw:
        fill = raw["slot_fill"]
        if fill not in SLOT_FILLS:
            ctx.error(
                "E230",
                f"`slot_fill` is {fill!r} — it reads as {DEFAULT_SLOT_FILL!r}",
                pointer=view.at("slot_fill"),
                entry=view,
                detail=f"One of: {', '.join(SLOT_FILLS)}.",
            )
        elif fill == DEFAULT_SLOT_FILL:
            ctx.info(
                "I231",
                f'`slot_fill: "{DEFAULT_SLOT_FILL}"` is the default and is omitted',
                pointer=view.at("slot_fill"),
                entry=view,
            )
    if "palette_row_base" in raw:
        base = raw["palette_row_base"]
        # Signed, like `tile_source.base_index`: a palette holding only rows
        # 8-15 as 0-7 counts *down*, so a negative base is a real answer.
        if not is_int(base):
            ctx.error(
                "E232",
                f"`palette_row_base` is {base!r}, not an integer",
                pointer=view.at("palette_row_base"),
                entry=view,
                detail='It reads as absent, which means "whatever the format says" — '
                "not 0. 0 is itself a real answer against a format that says 8, which "
                "is why celPix writes the key even at 0.",
            )
    if "sprite_size_pair" in raw:
        pair = raw["sprite_size_pair"]
        ok = (
            isinstance(pair, list)
            and len(pair) == 2
            and all(is_int(v) and v > 0 for v in pair)
        )
        if not ok:
            ctx.error(
                "E233",
                f"`sprite_size_pair` is {pair!r} — it must be two positive tile counts",
                pointer=view.at("sprite_size_pair"),
                entry=view,
                detail="A malformed pair reads as absent and the format's own guess is "
                "used, which no file records.",
            )
    if "extra_paths" in raw:
        extra = raw["extra_paths"]
        if not isinstance(extra, list):
            ctx.error(
                "E234",
                f"`extra_paths` is {type(extra).__name__}, not an array",
                pointer=view.at("extra_paths"),
                entry=view,
                detail="The region loses every file after the first, and every offset "
                "in it then names different bytes.",
            )
        else:
            for at, item in enumerate(extra):
                if not (isinstance(item, str) and item):
                    ctx.error(
                        "E235",
                        f"`extra_paths[{at}]` is {item!r} and is dropped from the join",
                        pointer=view.at("extra_paths", at),
                        entry=view,
                        detail="Dropping a chip moves every byte after it, so the "
                        "region's offsets shift.",
                    )
    for key, stage in (("container_id", "container"), ("reshape_id", "reshape")):
        if raw.get(key) == PASSTHROUGH[stage]:
            ctx.info(
                "I236",
                f"`{key}` is the pass-through default and celPix omits it",
                pointer=view.at(key),
                entry=view,
            )


def _slice_bounds(ctx: Context, view: EntryView) -> None:
    """The two offset fields, checked as numbers. Against the file: `files.py`."""
    raw = view.raw
    key = "offset" if view.kind == "bookmark" else "slice_offset"
    if key in raw and view.kind in ("slice", "bookmark"):
        offset = raw[key]
        if not is_int(offset):
            ctx.error(
                "E240",
                f"`{key}` is {offset!r}, not an integer — it reads as 0",
                pointer=view.at(key),
                entry=view,
                detail="The offset is absolute from byte 0 of the parent file.",
            )
        elif offset < 0:
            ctx.error(
                "E241",
                f"`{key}` is {offset}, which is before the start of the file",
                pointer=view.at(key),
                entry=view,
            )
    if view.kind == "slice" and "slice_length" in raw:
        length = raw["slice_length"]
        if length is None:
            # Legal, and meaningful: "read to wherever the structure ends",
            # backfilled on the first load. Only wrong beside a reshape.
            if raw.get("reshape_id") not in (None, PASSTHROUGH["reshape"]):
                ctx.error(
                    "E242",
                    "`slice_length: null` on a reshaped slice",
                    pointer=view.at("slice_length"),
                    entry=view,
                    detail="A reshape's boundaries are fractions of the region's "
                    "length, so a reshaped slice must state an explicit length.",
                )
        elif not is_int(length):
            ctx.error(
                "E243",
                f"`slice_length` is {length!r} — it must be an integer or null",
                pointer=view.at("slice_length"),
                entry=view,
            )
        elif length <= 0:
            ctx.error(
                "E244",
                f"`slice_length` is {length}, so the slice holds no bytes",
                pointer=view.at("slice_length"),
                entry=view,
                detail='Use null for "read to wherever the structure ends".',
            )


def _legacy(ctx: Context, view: EntryView) -> None:
    has_font = isinstance(view.raw.get("font"), dict)
    for key in ("alphabet_preset_id", "alphabet_base"):
        if key not in view.raw:
            continue
        if has_font:
            ctx.warn(
                "W251",
                f"`{key}` sits beside a `font` block and is ignored",
                pointer=view.at(key),
                entry=view,
                detail="The legacy alphabet keys are read only when there is no "
                "`font` key at all.",
            )
        else:
            ctx.info(
                "I250",
                f"`{key}` is the superseded alphabet-preset form, never written",
                pointer=view.at(key),
                entry=view,
                detail="Only the two runs celPix used to ship still resolve; any other "
                "preset id leaves the entry with an empty alphabet. Re-saving converts "
                "it to a `font` block.",
            )
