"""Plugin and preset ids: does anything answer to them, and at the right stage.

An id nothing is registered under does not fail the entry — it degrades it, and
the two halves of the pipeline degrade differently (project-format.md §4.1):

- A **byte stage** (container, reshape, compression) falls back to its
  pass-through, so the file opens showing untransformed bytes and the entry goes
  **view-only** — what is on screen is not what the file holds.
- An **interpret preset** has no pass-through to fall back to, so it lands on
  the stage's default format, and that fallback is written into the entry. A
  save then makes it permanent.

Which makes a typo'd preset id one of the more expensive mistakes available
here, and worth the strongest wording this linter has.
"""

from __future__ import annotations

from celpix_lint.context import Context, EntryView
from celpix_lint.schema import ID_STAGES

#: What a missing id costs, per stage — quoted in the finding, since it is the
#: reason one of these matters and the other is merely untidy.
_CONSEQUENCE = {
    "container": "The entry falls back to plain bytes and goes view-only.",
    "reshape": "The entry falls back to no reordering and goes view-only.",
    "compression": "The entry falls back to no compression and goes view-only.",
    "interpret-pixel": "The entry falls back to the default pixel format, and that "
    "fallback is what a save would write.",
    "interpret-palette": "The entry falls back to the default palette format, and that "
    "fallback is what a save would write.",
    "interpret-tilemap": "The entry falls back to the default tilemap format, and that "
    "fallback is what a save would write.",
}


def check(ctx: Context) -> None:
    if not ctx.ids.usable:
        return
    for view in ctx.entries:
        if not view.raw:
            continue
        for key in (
            "container_id",
            "reshape_id",
            "compression_id",
            "tilemap_preset_id",
        ):
            if key in view.raw:
                _one(ctx, view, key, view.raw[key], view.at(key))
        if view.kind == "palette" and "palette_preset_id" in view.raw:
            _one(
                ctx,
                view,
                "palette_preset_id",
                view.raw["palette_preset_id"],
                view.at("palette_preset_id"),
            )
        session = view.raw.get("session")
        if isinstance(session, dict):
            for key in ("pixel_preset_id", "palette_preset_id", "compression_id"):
                if key in session:
                    _one(ctx, view, key, session[key], view.at("session", key))
        _container_content(ctx, view)


def _one(ctx: Context, view: EntryView, key: str, value: object, pointer: str) -> None:
    stage = ID_STAGES[key]
    if not isinstance(value, str) or not value:
        ctx.error(
            "E401",
            f"`{key}` is {value!r}, not an id",
            pointer=pointer,
            entry=view,
            detail=_CONSEQUENCE[stage],
        )
        return
    if ctx.ids.has(stage, value):
        current = ctx.ids.current_id(value)
        if current != value:
            ctx.info(
                "I402",
                f"`{key}` {value!r} has been renamed to {current!r}",
                pointer=pointer,
                entry=view,
                detail="It still resolves through the forwarding table, so the project "
                "works as written; re-saving rewrites it to the current id.",
            )
        return
    # It resolves nowhere at this stage. Naming the stage it *does* belong to is
    # nearly always the actual answer — the ids are long and the stages'
    # namespaces overlap by design.
    elsewhere = ctx.ids.stage_of(value)
    if elsewhere is not None:
        ctx.error(
            "E403",
            f"`{key}` names {value!r}, which is a {elsewhere} id, not a {stage} one",
            pointer=pointer,
            entry=view,
            detail=_CONSEQUENCE[stage],
        )
        return
    if ctx.ids.authoritative:
        ctx.error(
            "E404",
            f"`{key}` names {value!r}, which nothing is registered under",
            pointer=pointer,
            entry=view,
            detail=_CONSEQUENCE[stage],
        )
    else:
        # The snapshot knows only what celPix ships, so it cannot tell a typo
        # from a plugin the author has installed. Say the weaker thing.
        ctx.warn(
            "W405",
            f"`{key}` names {value!r}, which is not one of celPix's built-in "
            f"{stage} ids",
            pointer=pointer,
            entry=view,
            detail="If it is not a plugin you have installed, this is a typo. "
            + _CONSEQUENCE[stage]
            + " Re-run with --live to check against your own registry.",
        )


def _container_content(ctx: Context, view: EntryView) -> None:
    """A container framing the wrong kind of entry.

    Palette files and graphics files are unwrapped by disjoint sets of formats,
    and the dropdowns never offer one to the other — so this is only reachable
    by hand, and reading a palette as an iNES ROM is exactly what it produces.
    """
    container = view.raw.get("container_id")
    if not isinstance(container, str) or not container:
        return
    kinds = ctx.ids.content_kinds(container)
    if kinds is None or view.content_kind in kinds:
        return
    ctx.error(
        "E410",
        f"container {container!r} does not frame {view.content_kind} entries",
        pointer=view.at("container_id"),
        entry=view,
        detail=f"It declares {', '.join(kinds)}. celPix never offers this pairing; the "
        "entry's bytes are unwrapped by a format that was not written for them.",
    )
