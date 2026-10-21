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
cannot spell, rather than approximating it.

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

from celpix.core.sprite import Frame, SizeChoice, Subsprite, SubspriteLimits
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


_NOT_EDITABLE = "Frames here are not editable"
_EMPTIES_FRAME = "A frame here holds at least one record - not removed"


# --- queries -----------------------------------------------------------------


def frame_count(
    cells: Sequence[Cell], frames: Sequence[Frame], limits: SubspriteLimits
) -> int:
    """How many frames the file holds — the bound every op checks a frame against.

    Not ``len(frames)`` in general: a slot or counted format holds a fixed
    number of frames whether or not the codec's ``frames()`` reports the empty
    tail, and an add may populate any of them. A headed run has exactly the
    frames it has headers for, and a bare run has one.
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
    """
    found: dict[int, tuple[int, int]] = {}
    for number, frame in enumerate(frames):
        for position, sub in enumerate(frame):
            if sub.record is not None:
                found[sub.record] = (number, position)
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
    """
    kind = limits.structure
    if kind == "table":
        raise SpriteEditError(_NOT_EDITABLE)
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
    keep = [at for at in range(len(cells)) if at not in doomed]
    return _assigned([cells[at] for at in keep], [owner[at] for at in keep], assign)


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
    """
    kind = limits.structure
    if kind in ("table", "run"):
        raise SpriteEditError(_NOT_EDITABLE)
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
    out = list(cells)
    moved = out.pop(record)
    del owner[record]
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
    still slides along it. A piece already outside the range — a file read
    with offsets the field could not have written, or a format that changed
    under it — stops the group dead rather than being dragged further out or
    snapped in. A caller that refuses a nudge whole compares the answer with
    what it asked.
    """
    if not pieces:
        return dx, dy
    xs = [sub.x for sub in pieces]
    ys = [sub.y for sub in pieces]
    if not all(x in limits.x_range for x in xs) or not all(
        y in limits.y_range for y in ys
    ):
        return 0, 0
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
