"""Record ops on a sprite map: add, remove, reorder and move its pieces.

A sprite object's pieces are its document's cells — every shipped format but
one decodes a :class:`~celpix.core.sprite.Subsprite` from a cell and its
``flags``, and encodes ``flags`` back verbatim — so an edit to a piece is an
edit to the cell list, and these functions take one and return a new one
(``docs/design/sprite-map.md`` §8.2). They never mutate their input, which
is what lets the host push the old and new lists as one undo step.

What an op *means* is the format's structure kind
(:data:`~celpix.core.sprite.StructureKind`, design §8.4): "add" sets the drawn
bit on a free slot in one format, inserts a cell in another, and is impossible
in a third. Each function dispatches on the kind and refuses what the kind
cannot spell, rather than approximating it. Under ``table`` nothing here
applies, because the records are not the entry's to edit. Under ``fixed`` the
records *are* editable, but only in their cell fields — the tile, and the
flips where the format has them, which the host writes through the codec's
``decode``/``encode`` and ``transform_cell`` like any tilemap cell — so every
op here refuses, and so does :func:`fit_group_delta`: the drawing routine
places each piece, and a delta has nothing to move.

The functions never learn where a format keeps a field. The host binds the
codec's ``place_subsprite`` and ``assign_frames`` and hands them in as
callables; this module decides *which* cell changes and *which frame* each
cell belongs to afterwards, and the codec writes it. Which cells are drawn,
and in which frame, comes from the codec's own ``frames()`` output through
:attr:`Subsprite.record` — never from reading a cell, because under ``slots``
an undrawn slot's content is residue of whatever sat there last and would read
as a piece.

**No op here creates, deletes or renumbers a frame** (design §10.5): animation
tables name frames by number and are preserved opaquely, so a frame count that
moved under them would point every later step at the wrong drawing. An op can
populate an empty frame or empty one, where the kind allows that.

Frames are numbered from 0 in every message, as the status line's pick readout
numbers them.

Qt-free, like the rest of ``core``.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from celpix.core.sprite import (
    Frame,
    SizeChoice,
    Subsprite,
    SubspriteLimits,
    range_text,
)
from celpix.core.tilemap import Cell

# The host's binding of ``TilemapCodecPlugin.place_subsprite``:
# ``place(cell, *, x=None, y=None, size=None, drawn=None) -> Cell``, raising
# ValueError for a value the field cannot hold.
Place = Callable[..., Cell]

# The host's binding of ``TilemapCodecPlugin.assign_frames``: the cells in their
# new order and one frame number per cell. None where the format carries no
# frame mark in its cells (``slots``, ``run``).
Assign = Callable[[list[Cell], Sequence[int]], list[Cell]]


class SpriteEditError(ValueError):
    """A record op the format cannot spell, worded for the status line."""


class FixedLayoutError(SpriteEditError):
    """The refusal every op raises under ``fixed``, as its own type so the host
    can say it in the format's terms (:func:`fixed_refusal`) — the text here
    is :data:`FIXED_REFUSAL`, which assumes the cells carry flips."""

    def __init__(self) -> None:
        super().__init__(FIXED_REFUSAL)


_NOT_EDITABLE = "Frames here are not editable"


def fixed_refusal(flips: bool) -> str:
    """Why a ``fixed`` layout refuses a record op, naming what *can* change.

    Distinct from ``_NOT_EDITABLE``: a fixed format's pieces are editable in
    their cell fields, so the refusal names what is left rather than calling
    the frame read-only. ``flips`` is whether the format's cells carry a flip
    bit at all — a pose table of bare tile words has none, and naming a flip
    there would promise a control the property row does not grow.
    """
    what = "tiles and flips" if flips else "tiles"
    return (
        f"Pieces here sit where the drawing routine puts them - "
        f"only their {what} can change"
    )


# The ops below have no codec in hand to ask about flips, so they raise the
# wording that names both; the host rewords a FixedLayoutError for its format.
FIXED_REFUSAL = fixed_refusal(flips=True)
_EMPTIES_FRAME = "A frame here holds at least one record - not removed"


# --- queries -----------------------------------------------------------------


def frame_count(
    cells: Sequence[Cell], frames: Sequence[Frame], limits: SubspriteLimits
) -> int:
    """How many frames the file holds — the bound every op checks a frame against.

    Not ``len(frames)`` in general: a slot or counted format holds a fixed
    number of frames whether or not the codec's ``frames()`` reports the empty
    tail, and an add may populate any of them. A headed run has exactly the
    frames it has headers for, a bare run has one, and a fixed format has
    the frames its codec draws.
    """
    kind = limits.structure
    if kind == "slots":
        return max(len(cells) // _stride(limits), len(frames))
    if kind == "counted":
        return limits.frames if limits.frames is not None else len(frames)
    if kind == "run":
        return 1
    return len(frames)


def membership(
    cells: Sequence[Cell], frames: Sequence[Frame], limits: SubspriteLimits
) -> list[int]:
    """The frame each cell belongs to, one number per cell.

    Under ``slots`` this is positional — an undrawn slot still belongs to the
    frame whose range it sits in. Every other kind draws every cell, so the
    frame is the one whose pieces name it; a cell no frame names means the
    frames are out of step with the cells, and an op built on that guess would
    write the wrong frame marks.
    """
    kind = limits.structure
    if kind == "table":
        raise SpriteEditError(_NOT_EDITABLE)
    if kind == "fixed":
        raise FixedLayoutError()
    if kind == "slots":
        stride = _stride(limits)
        return [at // stride for at in range(len(cells))]
    owner: list[int | None] = [None] * len(cells)
    for number, frame in enumerate(frames):
        for sub in frame:
            if sub.record is not None and 0 <= sub.record < len(cells):
                owner[sub.record] = number
    missing = [at for at, number in enumerate(owner) if number is None]
    if missing:
        raise SpriteEditError(
            f"Record {missing[0]} is in no frame - the frames do not match the cells"
        )
    return [number for number in owner if number is not None]


def record_positions(frames: Sequence[Frame]) -> dict[int, tuple[int, int]]:
    """``record -> (frame, position among that frame's drawn pieces)``.

    Position 0 is the front: draw order is file order
    (:data:`~celpix.core.sprite.Frame`). Pieces with no record — a sprite
    table's — are left out, since nothing can address them.

    A record drawn more than once — a ``fixed`` layout's word drawn as several
    pieces, or a pose drawn again mirrored in a later frame — resolves to its
    **first** place here, the one answer that does not move when a later frame
    is added to the view. A host that knows where the record was picked prefers
    that place (:func:`record_places`); the ops that take this are refused
    under ``fixed``, and every other kind draws a record once.
    """
    found: dict[int, tuple[int, int]] = {}
    for number, frame in enumerate(frames):
        for position, sub in enumerate(frame):
            if sub.record is not None:
                found.setdefault(sub.record, (number, position))
    return found


def record_places(frames: Sequence[Frame]) -> dict[int, list[tuple[int, int]]]:
    """``record -> every (frame, position)`` it is drawn at, in draw order.

    One place per record under every kind but ``fixed``, where a record is a
    word the drawing routine may draw several times; what outlines a pick
    marks all of them, since an edit to the word changes every one.
    """
    found: dict[int, list[tuple[int, int]]] = {}
    for number, frame in enumerate(frames):
        for position, sub in enumerate(frame):
            if sub.record is not None:
                found.setdefault(sub.record, []).append((number, position))
    return found


# --- structural ops ----------------------------------------------------------


def add_subsprite(
    cells: Sequence[Cell],
    frames: Sequence[Frame],
    limits: SubspriteLimits,
    frame: int,
    template: Cell,
    *,
    x: int,
    y: int,
    size: SizeChoice | None = None,
    place: Place,
    assign: Assign | None,
) -> tuple[list[Cell], int]:
    """A piece from ``template`` added to ``frame`` at ``(x, y)``.

    Returns ``(cells, record)``, the record being the new piece's cell index.

    The template is the held record, opaque bits and all (design §8.4): copying
    is the one policy that cannot be wrong about a bit nothing documents. Under
    ``slots`` it replaces the free slot's content *whole*, because that content
    is residue — a cleared slot keeps its old tile and attributes, and
    re-setting the drawn bit alone would resurrect them.

    Under the inserting kinds the new piece joins the *back* of its frame, the
    end of its run. Under ``slots`` it takes the first free slot, which may sit
    in front of drawn pieces: the slot is fixed by the stride, and a reorder
    puts it elsewhere if that matters.
    """
    kind = limits.structure
    if kind == "fixed":
        raise FixedLayoutError()
    _check_frame(cells, frames, limits, frame)
    if kind == "slots":
        start, stop = _slot_range(cells, limits, frame)
        drawn = _drawn_records(frames)
        free = next((at for at in range(start, stop) if at not in drawn), None)
        if free is None:
            raise SpriteEditError(
                f"Frame {frame} has no free slot ({stop - start} of {stop - start})"
            )
        out = list(cells)
        out[free] = place(template, x=x, y=y, size=size, drawn=True)
        return out, free

    owner = membership(cells, frames, limits)
    held = owner.count(frame)
    if limits.per_frame is not None and held >= limits.per_frame:
        raise SpriteEditError(f"Frame {frame} is full ({held} of {limits.per_frame})")
    at = _run_end(owner, frame)
    out = list(cells)
    out.insert(at, place(template, x=x, y=y, size=size))
    owner.insert(at, frame)
    return _assigned(out, owner, assign), at


def remove_subsprites(
    cells: Sequence[Cell],
    frames: Sequence[Frame],
    limits: SubspriteLimits,
    records: Sequence[int],
    *,
    place: Place,
    assign: Assign | None,
) -> list[Cell]:
    """``cells`` without the pieces ``records`` name.

    Under ``slots`` a remove is the drawn bit cleared and nothing else — what
    the authoring tool's own files hold for an unused slot — so the length is
    constant and no other piece's record changes. Every other kind deletes the
    cells, and ``headed`` refuses the whole op if it would leave a frame with
    no record: a header count of 0 is a decode error, and dropping the frame
    would renumber every later one.

    Under ``headed``, before the delete, the doomed cells are **demoted** to
    the back of their frames and the frames assigned once (:func:`_demoted`).
    ``assign`` takes a frame's header payload from a cell that bears it — the
    front one, which a remove of the frame's front piece takes with it — so handed
    the list with that cell already gone, it would open the frame with a bare
    count and zero every other header byte (a frame's width and height, say).
    Demoted first, the header lands on a surviving cell before anything goes.
    """
    kind = limits.structure
    if kind == "table":
        raise SpriteEditError(_NOT_EDITABLE)
    if kind == "fixed":
        raise FixedLayoutError()
    positions = record_positions(frames)
    doomed = set(records)
    for record in doomed:
        _check_record(positions, record, cells)
    if not doomed:
        return list(cells)
    if kind == "slots":
        out = list(cells)
        for record in doomed:
            out[record] = place(out[record], drawn=False)
        return out

    owner = membership(cells, frames, limits)
    if kind == "headed":
        left = {number for at, number in enumerate(owner) if at not in doomed}
        if any(number not in left for number in set(owner)):
            raise SpriteEditError(_EMPTIES_FRAME)
    staged, owner, source = _demoted(cells, owner, doomed, kind, assign)
    keep = [at for at in range(len(staged)) if source[at] not in doomed]
    return _assigned([staged[at] for at in keep], [owner[at] for at in keep], assign)


def reorder_subsprite(
    cells: Sequence[Cell],
    frames: Sequence[Frame],
    limits: SubspriteLimits,
    record: int,
    to: int,
    *,
    assign: Assign | None,
) -> list[Cell]:
    """``record`` moved to position ``to`` among its frame's drawn pieces.

    0 is the front, and ``to`` is clamped into the frame. The frame's pieces
    are permuted among the cell indices they already occupy, so the set of
    indices the frame uses does not change: under ``slots`` an undrawn slot
    never moves (its residue stays where it is, and a later add finds the same
    free slot), and under the other kinds this is a splice within the frame's
    run. It follows that the moved piece's new record is the ``to``-th lowest
    of the frame's record indices, which is how a caller re-finds its pick.
    """
    kind = limits.structure
    if kind == "table":
        raise SpriteEditError(_NOT_EDITABLE)
    if kind == "fixed":
        raise FixedLayoutError()
    positions = record_positions(frames)
    _check_record(positions, record, cells)
    number, position = positions[record]
    order = [sub.record for sub in frames[number] if sub.record is not None]
    to = max(0, min(to, len(order) - 1))
    if to == position:
        return list(cells)
    order.insert(to, order.pop(position))
    out = list(cells)
    for at, source in zip(sorted(order), order, strict=True):
        out[at] = cells[source]
    if kind == "slots":
        return out
    return _assigned(out, membership(cells, frames, limits), assign)


def move_subsprite(
    cells: Sequence[Cell],
    frames: Sequence[Frame],
    limits: SubspriteLimits,
    record: int,
    frame: int,
    *,
    place: Place,
    assign: Assign | None,
) -> tuple[list[Cell], int]:
    """``record`` moved to the back of ``frame``, fields kept.

    Returns ``(cells, record)``, the record being the piece's new cell index.

    Under ``slots`` the piece's slot is cleared and its content taken into the
    target's first free slot; elsewhere the cell is spliced to the end of the
    target's run. A bare run has one frame to move within and a table no
    record to move, so both refuse.

    Under ``headed`` the moved cell is **demoted** to the back of its own frame
    and the frames assigned once before the splice (:func:`_demoted`), for the
    reason
    :func:`remove_subsprites` gives: a headed frame's header rides on its
    front cell, and moving that cell out would otherwise take the header's
    other bytes away from the frame they describe.
    """
    kind = limits.structure
    if kind in ("table", "run"):
        raise SpriteEditError(_NOT_EDITABLE)
    if kind == "fixed":
        raise FixedLayoutError()
    positions = record_positions(frames)
    _check_record(positions, record, cells)
    _check_frame(cells, frames, limits, frame)
    source = positions[record][0]
    if source == frame:
        return list(cells), record
    if kind == "slots":
        start, stop = _slot_range(cells, limits, frame)
        drawn = _drawn_records(frames)
        free = next((at for at in range(start, stop) if at not in drawn), None)
        if free is None:
            raise SpriteEditError(
                f"Frame {frame} has no free slot ({stop - start} of {stop - start})"
            )
        out = list(cells)
        out[free] = place(cells[record], drawn=True)
        out[record] = place(cells[record], drawn=False)
        return out, free

    owner = membership(cells, frames, limits)
    if kind == "headed" and owner.count(source) == 1:
        raise SpriteEditError("A frame here holds at least one record - not moved")
    held = owner.count(frame)
    if limits.per_frame is not None and held >= limits.per_frame:
        raise SpriteEditError(f"Frame {frame} is full ({held} of {limits.per_frame})")
    out, owner, source = _demoted(cells, owner, {record}, kind, assign)
    moved = out.pop(source.index(record))
    del owner[source.index(record)]
    at = _run_end(owner, frame)
    out.insert(at, moved)
    owner.insert(at, frame)
    return _assigned(out, owner, assign), at


# --- geometry ----------------------------------------------------------------


def fit_group_delta(
    pieces: Sequence[Subsprite], dx: int, dy: int, limits: SubspriteLimits
) -> tuple[int, int]:
    """The move ``(dx, dy)`` shrunk until every piece's offset stays in range.

    Shrunk **as a group** (design §10.2): one piece pinned at the edge while the
    rest kept moving would change the selection's shape, which is a silent
    edit to the picture. Each axis is shrunk on its own, so a drag along a wall
    still slides along it. A caller that refuses a nudge whole compares the
    answer with what it asked.

    A piece already outside the range — a file read with offsets the field
    could not have written, or a format that changed under it — raises
    :class:`SpriteEditError` naming it, rather than being dragged further out
    or snapped in. It is not a clamp: answered as ``(0, 0)`` it would read as
    a group pinned at an edge no piece touched.

    Under ``fixed`` it refuses before anything else, even for an empty group:
    no record holds an offset, and every host path that moves pieces — drag,
    nudge, duplicate, paste — asks here first, so this is the one place they
    all learn why.
    """
    if limits.structure == "fixed":
        raise FixedLayoutError()
    if not pieces:
        return dx, dy
    for sub in pieces:
        for at, reach in ((sub.x, limits.x_range), (sub.y, limits.y_range)):
            if at not in reach:
                raise SpriteEditError(
                    f"Subsprite at ({sub.x:+d}, {sub.y:+d}) is outside the "
                    f"offsets this format holds ({range_text(reach)})"
                )
    xs = [sub.x for sub in pieces]
    ys = [sub.y for sub in pieces]
    return _fit(min(xs), max(xs), dx, limits.x_range), _fit(
        min(ys), max(ys), dy, limits.y_range
    )


def reflect_group(
    pieces: Sequence[Subsprite],
    tile_w: int,
    tile_h: int,
    *,
    horizontal: bool,
    pivot: int | None = None,
) -> list[tuple[int, int]]:
    """The new ``(x, y)`` of each piece, the selection mirrored as one picture.

    ``x' = pivot - x - w`` per piece, ``w`` its own width — reflecting the left
    edges alone is exact only when every piece is one size, and on a mixed
    frame puts the small piece a tile off (design §10.2). ``pivot`` defaults to
    ``left + right`` of the group's box, which maps the box onto itself; 0 is
    the mirror about the origin, ``x' = -x - w``. Vertical is the same in ``y``
    with each piece's height. Only positions — the piece's own flip bit is
    the codec's ``transform_cell``, toggled by the caller beside this.
    """
    if not pieces:
        return []
    if horizontal:
        spans = [(sub.x, sub.pixels(tile_w, tile_h)[0]) for sub in pieces]
    else:
        spans = [(sub.y, sub.pixels(tile_w, tile_h)[1]) for sub in pieces]
    if pivot is None:
        pivot = min(at for at, _ in spans) + max(at + length for at, length in spans)
    mirrored = [pivot - at - length for at, length in spans]
    if horizontal:
        return [(at, sub.y) for at, sub in zip(mirrored, pieces, strict=True)]
    return [(sub.x, at) for at, sub in zip(mirrored, pieces, strict=True)]


# --- helpers -----------------------------------------------------------------


def _stride(limits: SubspriteLimits) -> int:
    if not limits.per_frame:
        raise SpriteEditError("Frames here state no slot count - not editable")
    return limits.per_frame


def _slot_range(
    cells: Sequence[Cell], limits: SubspriteLimits, frame: int
) -> tuple[int, int]:
    # Clipped to the cells, so a short final frame offers the slots it has.
    stride = _stride(limits)
    start = min(frame * stride, len(cells))
    return start, min(start + stride, len(cells))


def _drawn_records(frames: Sequence[Frame]) -> set[int]:
    return {sub.record for frame in frames for sub in frame if sub.record is not None}


def _check_frame(
    cells: Sequence[Cell], frames: Sequence[Frame], limits: SubspriteLimits, frame: int
) -> None:
    kind = limits.structure
    if kind == "table":
        raise SpriteEditError(_NOT_EDITABLE)
    count = frame_count(cells, frames, limits)
    if 0 <= frame < count:
        return
    if kind == "run":
        raise SpriteEditError("A sprite here has one frame")
    if kind == "headed":
        raise SpriteEditError(
            f"Frame {frame} does not exist - frames here are not created"
        )
    raise SpriteEditError(f"A sprite here has {count} frame slots")


def _check_record(
    positions: dict[int, tuple[int, int]], record: int, cells: Sequence[Cell]
) -> None:
    # Only a piece the codec drew is addressable: under `slots` a cell index
    # outside every frame is an undrawn slot, whose content means nothing.
    if record not in positions or not 0 <= record < len(cells):
        raise SpriteEditError(f"Record {record} is not a drawn subsprite")


def _run_end(owner: Sequence[int], frame: int) -> int:
    """Where a piece joins ``frame``'s run: after its last cell.

    An empty frame's run is where it would sit — after the last cell of any
    lower-numbered frame — so frames stay in file order however they are
    populated.
    """
    at = 0
    for index, number in enumerate(owner):
        if number <= frame:
            at = index + 1
    return at


def _demoted(
    cells: Sequence[Cell],
    owner: Sequence[int],
    leaving: set[int],
    kind: str,
    assign: Assign | None,
) -> tuple[list[Cell], list[int], list[int]]:
    """Under ``headed``, ``cells`` with each of ``leaving`` moved to the back
    of its frame's run, stably, and the frames assigned over that order.

    Returns ``(cells, owner, source)``: the staged cells, the frame of each, and
    the index each came from in ``cells``, by which a caller finds the leaving
    cells again. The assign is the point — it is what puts a frame's header on
    a cell that will still be there once the leaving ones are deleted or moved,
    so the header's payload survives the op that follows.

    Every other kind comes back as given, unassigned: only a headed frame keeps
    a payload on one of its cells (a counted frame's marks are its number,
    which the final assign restamps on every cell alike).
    """
    order = list(range(len(cells)))
    if kind != "headed" or assign is None:
        return list(cells), list(owner), order
    # A headed run is in frame order already, so sorting by frame first leaves
    # every run where it is; within one the leaving cells go behind the
    # staying ones, each side in its own order.
    order.sort(key=lambda at: (owner[at], at in leaving))
    staged = assign([cells[at] for at in order], [owner[at] for at in order])
    return staged, [owner[at] for at in order], order


def _assigned(
    cells: list[Cell], owner: Sequence[int], assign: Assign | None
) -> list[Cell]:
    # `assign` restamps the frame marks in place — it keeps the order and the
    # length it is handed, which is what lets the ops report a record index
    # computed before the call.
    return assign(cells, list(owner)) if assign is not None else cells


def _fit(low: int, high: int, delta: int, bounds: range) -> int:
    if delta < 0:
        return max(delta, bounds.start - low)
    if delta > 0:
        return min(delta, bounds.stop - 1 - high)
    return 0
