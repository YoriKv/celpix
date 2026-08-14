"""References between entries: parents, tile bindings and composite pieces.

Everything here is positional or path-matched, and both are fragile under hand
editing in the same way: **an index counts every entry, including ones the
loader drops**, because it was written against a list that had them. So
deleting an entry from the array silently re-points every binding after it, and
that is the single most expensive edit an agent can make to one of these files.
"""

from __future__ import annotations

from celpix_lint.context import Context, EntryView
from celpix_lint.schema import PIECE_KEYS, TILE_MODES, TILE_SOURCE_KEYS, is_int


def check(ctx: Context) -> None:
    _parents(ctx)
    for view in ctx.entries:
        if not view.raw:
            continue
        _tile_source(ctx, view)
        _pieces(ctx, view)
    _binding_cycles(ctx)


# -- slices and bookmarks under their file ---------------------------------
def _parents(ctx: Context) -> None:
    """A slice or bookmark is tied to its parent **by path**, and the reader's
    one ordering rule is that the parent comes first."""
    files: dict = {}
    for view in ctx.entries:
        if view.kind == "file" and view.raw:
            path = view.raw.get("path")
            if isinstance(path, str) and path:
                files.setdefault(ctx.doc.identity(path), view)
    for view in ctx.entries:
        if view.kind not in ("slice", "bookmark") or not view.raw:
            continue
        path = view.raw.get("path")
        if not (isinstance(path, str) and path):
            continue
        parent = files.get(ctx.doc.identity(path))
        if parent is None:
            detail = (
                "It opens as a top-level row, and it reads raw bytes: a slice takes "
                "its container from its parent, so without one the parent's header "
                "skip and framing are not applied to it."
            )
            # A palette entry is the one other kind whose `path` is genuinely its
            # own file, so it is the one that can look like a parent and is not.
            # Slices and bookmarks store their *parent's* path, so a sibling
            # matching here says nothing.
            palette = _palette_at(ctx, path)
            if palette is not None:
                detail += (
                    f" Entry {palette.index} opens that file, but it is a palette "
                    "entry — a parent is always a whole file entry, and the two are "
                    "separate lists even when their paths coincide."
                )
            ctx.warn(
                "W502",
                f"no file entry opens {path!r}, so this {view.kind} has no parent",
                pointer=view.at("path"),
                entry=view,
                detail=detail,
            )
        elif _join_mismatch(parent, view):
            ctx.error(
                "E504",
                f"the parent (entry {parent.index}) joins "
                f"{1 + len(parent.raw.get('extra_paths') or ())} files, but this "
                f"{view.kind} names "
                f"{1 + len(view.raw.get('extra_paths') or ())}",
                pointer=view.at("extra_paths"),
                entry=view,
                detail="Offsets are into the files **joined**, and a child stores the "
                "list rather than re-deriving it — entries load before there is a "
                "workspace to look a parent up in. Against a different join the same "
                "offset names different bytes. Copy the parent's `extra_paths` here.",
            )
        elif parent.index > view.index:
            ctx.error(
                "E503",
                f"this {view.kind} is written before its parent (entry {parent.index})",
                pointer=view.pointer,
                entry=view,
                detail="The panel can only nest a child under a row that already "
                "exists, so it opens as a top-level row instead. Move the parent "
                "above it — but remember every stored entry_index counts positions.",
            )


def _join_mismatch(parent: EntryView, child: EntryView) -> bool:
    """Whether a child names a different set of files than its parent joins.

    A region spanning several ROM chips is the parent's `path` plus its
    `extra_paths`, and every offset under it is into that concatenation. A slice
    or bookmark written without the same list is measured against the first
    file alone, which puts it somewhere else entirely — or, more visibly, past
    the end.
    """
    return list(parent.raw.get("extra_paths") or ()) != list(
        child.raw.get("extra_paths") or ()
    )


def _palette_at(ctx: Context, path: str) -> EntryView | None:
    """A palette entry whose own file is ``path``, if there is one."""
    key = ctx.doc.identity(path)
    for view in ctx.entries:
        if view.kind != "palette" or not view.raw:
            continue
        other = view.raw.get("path")
        if isinstance(other, str) and other and ctx.doc.identity(other) == key:
            return view
    return None


# -- a tilemap's tiles ------------------------------------------------------
def _tile_source(ctx: Context, view: EntryView) -> None:
    if "tile_source" not in view.raw:
        if view.is_tilemap:
            ctx.warn(
                "W510",
                "a tilemap entry with no `tile_source` opens unbound",
                pointer=view.pointer,
                entry=view,
                detail="It shows placeholder cells until tiles are pointed at it. "
                'Bind it with {"mode": "entry", "entry_index": N}.',
            )
        return
    source = view.raw["tile_source"]
    if not isinstance(source, dict):
        ctx.error(
            "E511",
            f"`tile_source` is {type(source).__name__}, not an object — the map opens "
            "unbound",
            pointer=view.at("tile_source"),
            entry=view,
        )
        return
    for key in source:
        if key not in TILE_SOURCE_KEYS:
            ctx.warn(
                "W512",
                f"unknown key {key!r} in `tile_source`",
                pointer=view.at("tile_source", key),
                entry=view,
                detail="There is no path here: the tiles are always another entry in "
                "this project, and that entry stores its own path.",
            )
    mode = source.get("mode")
    if mode not in TILE_MODES:
        ctx.error(
            "E513",
            f"`tile_source.mode` is {mode!r} — the map opens unbound",
            pointer=view.at("tile_source", "mode"),
            entry=view,
            detail='It should be "entry"; the tiles are always another open entry.',
        )
        return
    if mode == "none":
        ctx.info(
            "I514",
            '`tile_source.mode` is "none" — the map is unbound',
            pointer=view.at("tile_source", "mode"),
            entry=view,
            detail="celPix writes no tile_source at all for an unbound map.",
        )
        return
    base = source.get("base_index")
    # Deliberately **signed**, and both directions are used: positive when the
    # bank sits partway into the bound entry, negative when the map numbers from
    # partway into a slice that starts at the tiles it wants. So only the type
    # is checked here — a cell landing outside the source renders blank, which
    # makes a wrong base visible rather than damaging.
    if "base_index" in source and not is_int(base):
        ctx.error(
            "E515",
            f"`tile_source.base_index` is {base!r}, not an integer — it reads as 0",
            pointer=view.at("tile_source", "base_index"),
            entry=view,
            detail="Cell N draws source tile base_index + N. It is signed: negative "
            "shifts the map's numbering back onto a slice that starts at its tiles.",
        )
    at = source.get("entry_index")
    if not is_int(at):
        ctx.error(
            "E516",
            f"`tile_source.entry_index` is {at!r}, not an integer — the map opens "
            "unbound",
            pointer=view.at("tile_source", "entry_index"),
            entry=view,
        )
        return
    if at == -1:
        ctx.warn(
            "W517",
            "`tile_source.entry_index` is -1 — the map has no tiles",
            pointer=view.at("tile_source", "entry_index"),
            entry=view,
            detail="-1 is what celPix writes for a binding onto an entry that is no "
            "longer open. Point it at the entry holding the tiles.",
        )
        return
    if not 0 <= at < len(ctx.entries):
        ctx.error(
            "E518",
            f"`tile_source.entry_index` is {at}, outside the "
            f"{len(ctx.entries)} entries",
            pointer=view.at("tile_source", "entry_index"),
            entry=view,
            detail="The map opens unbound.",
        )
        return
    target = ctx.entries[at]
    problem = _cannot_supply(view, target)
    if problem:
        ctx.error(
            "E519",
            f"`tile_source.entry_index` {at} names {_describe(target)}, which "
            f"{problem}",
            pointer=view.at("tile_source", "entry_index"),
            entry=view,
            detail="An unusable binding is dropped and the map opens unbound. Note "
            "that the index counts every entry in the array, including any celPix "
            "drops.",
        )


def _cannot_supply(view: EntryView, target: EntryView) -> str:
    """Why ``target`` cannot supply ``view``'s tiles, or "" if it can.

    Mirrors the editor's own rule: art always, a map only while it reaches art
    itself, never a bookmark, never the entry itself.
    """
    if target.index == view.index:
        return "is the map itself"
    if target.skipped:
        return "celPix drops for having no usable path"
    if target.kind == "bookmark":
        return "is a bookmark — a position, not content"
    if target.content_kind == "palette" or target.kind == "palette":
        return "is a palette, which holds no tiles"
    return ""


def _binding_cycles(ctx: Context) -> None:
    """A map bound to a map bound back to the first.

    Legal one level deep — a map may draw through another map that reaches art —
    so the shape has to be walked rather than forbidden.
    """
    reported = set()
    for view in ctx.entries:
        if not view.is_tilemap:
            continue
        seen, at = [], view.index
        while True:
            if at in seen:
                cycle = seen[seen.index(at) :]
                key = tuple(sorted(cycle))
                if key not in reported:
                    reported.add(key)
                    chain = " -> ".join(str(i) for i in [*cycle, at])
                    ctx.error(
                        "E520",
                        f"tile bindings form a loop: entries {chain}",
                        pointer=ctx.entries[cycle[0]].at("tile_source", "entry_index"),
                        entry=ctx.entries[cycle[0]],
                        detail="Each map is waiting on the next for its tiles, so none "
                        "of them ever reaches art. Point one at a pixels entry.",
                    )
                break
            seen.append(at)
            nxt = _bound_index(ctx, at)
            if nxt is None:
                break
            at = nxt


def _bound_index(ctx: Context, index: int) -> int | None:
    view = ctx.entries[index]
    source = view.raw.get("tile_source") if view.raw else None
    if not isinstance(source, dict) or source.get("mode") != "entry":
        return None
    at = source.get("entry_index")
    if not is_int(at) or not 0 <= at < len(ctx.entries):
        return None
    # Only a map can continue the chain; art ends it.
    return at if ctx.entries[at].is_tilemap else None


# -- a composite's pieces ---------------------------------------------------
def _pieces(ctx: Context, view: EntryView) -> None:
    if view.kind != "composite":
        return
    pieces = view.raw.get("pieces")
    if pieces is None:
        return  # reported as a missing required key
    if not isinstance(pieces, list):
        ctx.error(
            "E530",
            f"`pieces` is {type(pieces).__name__}, not an array — the composite "
            "assembles nothing",
            pointer=view.at("pieces"),
            entry=view,
        )
        return
    if not pieces:
        ctx.warn(
            "W531",
            "the composite has no pieces and opens empty",
            pointer=view.at("pieces"),
            entry=view,
        )
        return
    for at, piece in enumerate(pieces):
        _one_piece(ctx, view, at, piece)


def _one_piece(ctx: Context, view: EntryView, at: int, piece: object) -> None:
    where = view.at("pieces", at)
    if not isinstance(piece, dict):
        ctx.error(
            "E532",
            f"`pieces[{at}]` is not an object — it becomes a zero-length hole",
            pointer=where,
            entry=view,
            detail="Every run after it shifts, so a map indexing this composite lands "
            "on the wrong tiles.",
        )
        return
    for key in piece:
        if key not in PIECE_KEYS:
            ctx.warn(
                "W533",
                f"unknown key {key!r} in `pieces[{at}]`",
                pointer=f"{where}/{key}",
                entry=view,
            )
    for key in ("offset", "length", "measured"):
        if key in piece and (not is_int(piece[key]) or piece[key] < 0):
            ctx.error(
                "E534",
                f"`pieces[{at}].{key}` is {piece[key]!r} — it must be a byte count "
                "from 0",
                pointer=f"{where}/{key}",
                entry=view,
                detail="It reads as 0, which changes the length of the run.",
            )
    if "entry_index" not in piece:
        # A pad: it names nothing and its length is the whole of it.
        length = piece.get("length")
        if not is_int(length) or length <= 0:
            ctx.error(
                "E535",
                f"`pieces[{at}]` names no entry and has no positive `length`",
                pointer=where,
                entry=view,
                detail="A piece with no entry_index is a blank run, and `length` is "
                "the whole of what it is. This one contributes nothing.",
            )
        return
    index = piece["entry_index"]
    if not is_int(index):
        ctx.error(
            "E536",
            f"`pieces[{at}].entry_index` is {index!r}, not an integer",
            pointer=f"{where}/entry_index",
            entry=view,
            detail="It becomes a blank run of the recorded length.",
        )
        return
    if index == -1:
        ctx.warn(
            "W537",
            f"`pieces[{at}].entry_index` is -1 — a blank run",
            pointer=f"{where}/entry_index",
            entry=view,
            detail="-1 is what celPix writes for a source that is no longer open. The "
            "run keeps its recorded length so nothing after it moves.",
        )
        return
    if not 0 <= index < len(ctx.entries):
        ctx.error(
            "E538",
            f"`pieces[{at}].entry_index` is {index}, outside the "
            f"{len(ctx.entries)} entries",
            pointer=f"{where}/entry_index",
            entry=view,
            detail="It becomes a blank run of the recorded length, so the picture is "
            "right-sized and empty.",
        )
        return
    target = ctx.entries[index]
    problem = _cannot_compose(view, target)
    if problem:
        ctx.error(
            "E539",
            f"`pieces[{at}].entry_index` {index} names {_describe(target)}, which "
            f"{problem}",
            pointer=f"{where}/entry_index",
            entry=view,
            detail="The piece becomes a blank run of the recorded length.",
        )


def _cannot_compose(view: EntryView, target: EntryView) -> str:
    """celPix's ``can_compose``: not itself, never a composite, pixels only."""
    if target.index == view.index:
        return "is the composite itself"
    if target.kind == "composite":
        return "is another composite — a composite cannot contain one"
    if target.content_kind != "pixels":
        return f"holds {target.content_kind}, and only pixel bytes can be assembled"
    return ""


def _describe(view: EntryView) -> str:
    label = f"entry {view.index}"
    if view.name:
        label += f" ({view.name})"
    return label
