"""The `view` block — display state, and the two lists that ride along in it.

Most of this is numbers that get clamped anyway. The parts worth checking are
the tile rearrangement and the pinned palette regions, because both are read
through a *whole-list* tolerance: one unusable pair in `tile_rearrangement`
takes the entire rearrangement down with it, silently, and the tiles reopen in
file order as though the feature had never been used.
"""

from __future__ import annotations

from celpix_lint.context import Context, EntryView
from celpix_lint.schema import (
    BLOCK_ORDERS,
    VIEW_BOOL_KEYS,
    VIEW_INT_MINIMUMS,
    VIEW_KEYS,
    is_int,
    is_number,
)

#: bit 0 mirror H, bit 1 mirror V, bit 2 diagonal transpose.
_MAX_ORIENTATION = 0b111


def check(ctx: Context) -> None:
    for entry in ctx.entries:
        if not entry.raw or "view" not in entry.raw:
            continue
        view = entry.raw["view"]
        if not isinstance(view, dict):
            ctx.warn(
                "W700",
                f"`view` is {type(view).__name__}, not an object — the entry opens on "
                "default display settings",
                pointer=entry.at("view"),
                entry=entry,
            )
            continue
        _keys(ctx, entry, view)
        _numbers(ctx, entry, view)
        _rearrangement(ctx, entry, view)
        _palette_regions(ctx, entry, view)
        _bookmark(ctx, entry, view)


def _keys(ctx: Context, entry: EntryView, view: dict) -> None:
    for key in view:
        if key not in VIEW_KEYS:
            ctx.warn(
                "W701",
                f"unknown key {key!r} in `view` — the reader ignores it",
                pointer=entry.at("view", key),
                entry=entry,
            )
    if "tile_map" in view:
        ctx.info(
            "I702",
            "`tile_map` is the superseded spelling of `tile_rearrangement`",
            pointer=entry.at("view", "tile_map"),
            entry=entry,
            detail="Still read, never written — re-saving converts it. Ignored "
            "entirely if `tile_rearrangement` is also present.",
        )
        if "tile_rearrangement" in view:
            ctx.warn(
                "W703",
                "`tile_map` sits beside `tile_rearrangement` and is ignored",
                pointer=entry.at("view", "tile_map"),
                entry=entry,
            )


def _numbers(ctx: Context, entry: EntryView, view: dict) -> None:
    for key, minimum in VIEW_INT_MINIMUMS.items():
        if key not in view:
            continue
        value = view[key]
        if not is_int(value):
            ctx.error(
                "E710",
                f"`view.{key}` is {value!r}, not an integer — it reads as the default",
                pointer=entry.at("view", key),
                entry=entry,
            )
        elif value < minimum:
            ctx.error(
                "E711",
                f"`view.{key}` is {value}, below its minimum of {minimum}",
                pointer=entry.at("view", key),
                entry=entry,
                detail="Values are clamped against the actual file on load, so the "
                "entry opens on something other than what is written here.",
            )
    if "zoom" in view:
        zoom = view["zoom"]
        if not is_number(zoom):
            ctx.error(
                "E712",
                f"`view.zoom` is {zoom!r}, not a number",
                pointer=entry.at("view", "zoom"),
                entry=entry,
                detail="Zoom is the one view number that may be fractional (the 0.5 "
                "level); every other level is written as a plain integer.",
            )
        elif zoom <= 0:
            ctx.error(
                "E713",
                f"`view.zoom` is {zoom}, which magnifies nothing",
                pointer=entry.at("view", "zoom"),
                entry=entry,
            )
    if "block_order" in view and view["block_order"] not in BLOCK_ORDERS:
        ctx.error(
            "E714",
            f'`view.block_order` is {view["block_order"]!r} — it reads as "row"',
            pointer=entry.at("view", "block_order"),
            entry=entry,
            detail=f"One of: {', '.join(BLOCK_ORDERS)}.",
        )
    for key in VIEW_BOOL_KEYS:
        if key in view and not isinstance(view[key], bool):
            # `bool(...)` is what the reader applies, so a truthy non-bool is
            # not lost — but 0/"" flipping to false is, and neither is what a
            # reviewer of this file would expect to read.
            ctx.warn(
                "W715",
                f"`view.{key}` is {view[key]!r}, not true or false",
                pointer=entry.at("view", key),
                entry=entry,
                detail="It is read for truthiness, so it works by accident.",
            )


def _rearrangement(ctx: Context, entry: EntryView, view: dict) -> None:
    pairs = view.get("tile_rearrangement", view.get("tile_map"))
    orientations = view.get("tile_orientations")
    fatal = _pair_list(ctx, entry, view, "tile_rearrangement", pairs, _check_move)
    fatal |= _pair_list(
        ctx, entry, view, "tile_orientations", orientations, _check_turn
    )
    if fatal:
        ctx.error(
            "E720",
            "the whole tile rearrangement is dropped by the pair above",
            pointer=entry.at("view", "tile_rearrangement"),
            entry=entry,
            detail="Both lists are read in one operation, so one value that will not "
            "convert to an integer discards the rearrangement *and* the orientations. "
            "The tiles reopen in file order with no complaint.",
        )
        return
    if isinstance(pairs, list):
        _duplicates(ctx, entry, pairs)
    if view.get("show_rearranged") and not pairs and not orientations:
        ctx.info(
            "I721",
            "`view.show_rearranged` is on but nothing is rearranged",
            pointer=entry.at("view", "show_rearranged"),
            entry=entry,
        )


def _pair_list(
    ctx: Context, entry: EntryView, view: dict, key: str, raw, check
) -> bool:
    """Walk one ``[[a, b], ...]`` list. Returns True if it kills the whole map."""
    if key not in view:
        return False
    if not isinstance(raw, list):
        ctx.error(
            "E722",
            f"`view.{key}` is {type(raw).__name__}, not an array — it is ignored",
            pointer=entry.at("view", key),
            entry=entry,
        )
        return False
    fatal = False
    for at, pair in enumerate(raw):
        where = entry.at("view", key, at)
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            ctx.error(
                "E723",
                f"`view.{key}[{at}]` is not a two-element pair and is skipped",
                pointer=where,
                entry=entry,
            )
            continue
        if not all(is_int(v) for v in pair):
            fatal = True
            ctx.error(
                "E724",
                f"`view.{key}[{at}]` is {list(pair)!r} — both halves must be integers",
                pointer=where,
                entry=entry,
            )
            continue
        check(ctx, entry, where, pair)
    return fatal


def _check_move(ctx: Context, entry: EntryView, where: str, pair) -> None:
    if pair[0] < 0 or pair[1] < 0:
        ctx.error(
            "E725",
            f"rearrangement pair {list(pair)!r} has a negative tile index",
            pointer=where,
            entry=entry,
        )


def _check_turn(ctx: Context, entry: EntryView, where: str, pair) -> None:
    tile, flags = pair
    if tile < 0:
        ctx.error(
            "E726",
            f"orientation pair {list(pair)!r} has a negative tile index",
            pointer=where,
            entry=entry,
        )
    if not 0 <= flags <= _MAX_ORIENTATION:
        ctx.error(
            "E727",
            f"orientation flags {flags} are outside 0-{_MAX_ORIENTATION}",
            pointer=where,
            entry=entry,
            detail="bit 0 mirrors horizontally, bit 1 vertically, bit 2 transposes "
            "diagonally — a quarter turn is a transpose plus a mirror.",
        )


def _duplicates(ctx: Context, entry: EntryView, pairs: list) -> None:
    """Two pairs claiming the same slot. The map is a dict either way, so one of
    them is simply lost — and which one depends on array order."""
    virtual: dict = {}
    actual: dict = {}
    for at, pair in enumerate(pairs):
        if not (isinstance(pair, (list, tuple)) and len(pair) == 2):
            continue
        if not all(is_int(v) for v in pair):
            continue
        first = virtual.setdefault(pair[0], at)
        if first != at:
            ctx.error(
                "E728",
                f"`view.tile_rearrangement` maps virtual tile {pair[0]} twice "
                f"(pairs {first} and {at})",
                pointer=entry.at("view", "tile_rearrangement", at),
                entry=entry,
                detail="The later pair wins and the earlier one is lost.",
            )
        first = actual.setdefault(pair[1], at)
        if first != at and pair[0] != pair[1]:
            ctx.warn(
                "W729",
                f"`view.tile_rearrangement` sends two virtual tiles to actual tile "
                f"{pair[1]} (pairs {first} and {at})",
                pointer=entry.at("view", "tile_rearrangement", at),
                entry=entry,
                detail="The map is no longer a permutation, so the reverse lookup — "
                "which tile a click lands on — can only answer for one of them.",
            )


def _palette_regions(ctx: Context, entry: EntryView, view: dict) -> None:
    if "palette_regions" not in view:
        return
    raw = view["palette_regions"]
    if not isinstance(raw, list):
        ctx.error(
            "E730",
            f"`view.palette_regions` is {type(raw).__name__}, not an array — no "
            "regions are pinned",
            pointer=entry.at("view", "palette_regions"),
            entry=entry,
        )
        return
    spans = []
    for at, item in enumerate(raw):
        where = entry.at("view", "palette_regions", at)
        if not isinstance(item, (list, tuple)) or len(item) != 3:
            ctx.error(
                "E731",
                f"`view.palette_regions[{at}]` is not a [start, length, row] triple "
                "and is skipped",
                pointer=where,
                entry=entry,
            )
            continue
        if not all(is_int(v) for v in item):
            ctx.error(
                "E732",
                f"`view.palette_regions[{at}]` is {list(item)!r} — all three must be "
                "integers, and it is skipped",
                pointer=where,
                entry=entry,
            )
            continue
        start, length, row = item
        if length <= 0 or start < 0 or row < 0:
            ctx.error(
                "E733",
                f"`view.palette_regions[{at}]` is {list(item)!r} and is skipped",
                pointer=where,
                entry=entry,
                detail="`length` must be positive, `start` and `row` from 0. Spans are "
                "**pixel** runs in the entry's own picture space, not byte offsets.",
            )
            continue
        spans.append((start, length, row, at))
    _overlaps(ctx, entry, spans)


def _overlaps(ctx: Context, entry: EntryView, spans: list) -> None:
    for (start, length, row, at), (
        other_start,
        other_length,
        other_row,
        other_at,
    ) in zip(sorted(spans), sorted(spans)[1:]):
        if other_start < start + length:
            ctx.warn(
                "W734",
                f"pinned regions {at} and {other_at} overlap "
                f"({start}+{length} into {other_start}+{other_length})",
                pointer=entry.at("view", "palette_regions", other_at),
                entry=entry,
                detail=f"They are normalized earlier-wins on load, so row {other_row} "
                f"does not apply to the shared pixels — row {row} does.",
            )


def _bookmark(ctx: Context, entry: EntryView, view: dict) -> None:
    """A bookmark's view is a settings snapshot with its position zeroed out."""
    if entry.kind != "bookmark":
        return
    for key in ("offset", "byte_nudge"):
        if view.get(key):
            ctx.info(
                "I740",
                f"a bookmark's `view.{key}` is written as 0",
                pointer=entry.at("view", key),
                entry=entry,
                detail="The position a bookmark marks lives in its top-level `offset`; "
                "the view here is the settings snapshot applied when you jump to it.",
            )
