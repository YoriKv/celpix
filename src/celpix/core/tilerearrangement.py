"""Virtual tile rearrangement: showing tiles somewhere other than they live.

Tiles are rarely stored in the order they are drawn in — a face's eyes, mouth and
hair can sit hundreds of tiles apart because the game streams them by animation
frame, not by picture. A :class:`TileRearrangement` lets the view show them side by side
anyway: it maps the **virtual** index a tile is displayed at to the **actual**
index it occupies in the file. Nothing is moved. Pixels edited at a virtual
position still encode back to the tile's actual bytes, which is the whole point —
the rearrangement is display state, exactly like the block arrangement and the 2D
walk it composes with (``docs/design/overview.md`` §4).

The position part of the map is a **permutation**, and every operation here
preserves that. This is not a formality: "the pixels you paint go back to the tile
you painted on" is only total if every virtual position resolves to exactly one
actual tile and vice versa. A mapping that merely *reassigned* positions could
point two virtual slots at one tile, and an edit to either would silently
overwrite the other.

A tile may also be shown **turned** — the other half of reading scattered art,
since hardware mirrors tiles rather than storing both halves of a symmetric
sprite, and art lifted from one context often sits at ninety degrees to the one
it is being read in. That orientation is display state too: the view orients on
the way out and puts it back on the way in, so what reaches the file is always
the tile's own orientation. Orientations are keyed by *actual* tile index, so a
turned tile stays turned when it is dragged somewhere else — you turned it to
make the art read, and moving it should keep it reading.

The eight orientations are the symmetries of a square, so composing two of them
gives a third and the buttons can just keep pressing (:func:`compose_orientation`).
The two mirrors are what a real tile attribute carries; the four that swap the
tile's axes need a **square** tile to land in the same cell, and are ignored — not
dropped — on a tile that isn't square.

Storage is sparse and canonical — only tiles that actually moved or turned are
listed — so an unrearranged document costs nothing and two equal maps compare
equal.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from functools import cached_property

from celpix.core import transform

# Display orientation, as a bitmask per tile: three independent bits, so eight
# ways a tile can be shown — every symmetry of a square, and nothing else.
#
# Bits 0 and 1 are the mirrors a real tile attribute carries (both set is a 180°
# turn, which is how the hardware expresses it too). Bit 2 is the diagonal
# transpose, the *axis swap* the quarter turns are built from: a turn is a
# transpose plus a mirror, so keeping the swap as its own bit is what makes the
# eight closed under composition instead of needing a lookup table.
#
# Read as: mirror per bits 0/1, **then** transpose. Bits 0/1 alone therefore mean
# exactly what the hardware attribute means, whatever bit 2 holds.
TILE_ORIENT_NONE = 0
TILE_FLIP_H = 1
TILE_FLIP_V = 2
TILE_FLIP_BOTH = TILE_FLIP_H | TILE_FLIP_V
TILE_TRANSPOSE = 4
TILE_ROTATE_CW = TILE_FLIP_V | TILE_TRANSPOSE
TILE_ROTATE_CCW = TILE_FLIP_H | TILE_TRANSPOSE
TILE_ORIENT_MASK = TILE_FLIP_BOTH | TILE_TRANSPOSE

# How many unwanted tiles a decode run will swallow to avoid becoming two runs.
# A rearranged window resolves to scattered tile indices, and each run costs a
# codec call plus its region arithmetic — far more than decoding a handful of
# neighbours nobody asked for. So runs separated by a small gap are merged, which
# collapses a locally shuffled area (the common case: tiles pulled together from
# around one sprite) back into a single decode. Tiles genuinely far apart still
# get their own run rather than dragging the span between them along.
RUN_MERGE_GAP = 8


@dataclass(frozen=True)
class TileRearrangement:
    """Where each displayed tile really lives, and how it is shown.

    ``pairs`` holds ``(virtual, actual)`` for the moved tiles only, sorted by
    virtual index; every index not listed maps to itself. ``orientations`` holds
    ``(actual, flags)`` for the turned tiles only — keyed by the tile's *own*
    index, not its display position, so an orientation rides along when the tile
    is moved. Construct through :meth:`from_pairs` (or the :meth:`swap` /
    :meth:`oriented` family) rather than passing these directly — those normalize
    the input and check the permutation invariant.

    Frozen, and every mutator returns a *new* map, so a rearrangement step is an
    ordinary before/after pair for undo and the maps are cheap to hold: only the
    moved and turned tiles are stored, whatever the size of the file.
    """

    pairs: tuple[tuple[int, int], ...] = ()
    orientations: tuple[tuple[int, int], ...] = ()

    def __post_init__(self) -> None:
        virtuals = [v for v, _ in self.pairs]
        actuals = [a for _, a in self.pairs]
        virtual_set, actual_set = set(virtuals), set(actuals)
        # A repeat on either side is the failure mode the class exists to
        # prevent: two virtual slots sharing one actual tile makes an edit to
        # either overwrite the other.
        if len(virtual_set) != len(virtuals) or len(actual_set) != len(actuals):
            raise ValueError("a tile map must be a permutation (repeated index)")
        # Same *set* on both sides, or the map would move tiles in from — or out
        # to — indices it doesn't account for, and would not be a permutation of
        # the whole index space.
        if virtual_set != actual_set:
            raise ValueError("a tile map must be a permutation (unbalanced)")
        if any(v < 0 or a < 0 for v, a in self.pairs):
            raise ValueError("tile indices cannot be negative")
        indices = [tile for tile, _ in self.orientations]
        if len(set(indices)) != len(indices):
            raise ValueError("a tile cannot carry two orientations")
        if any(t < 0 or not 0 < f <= TILE_ORIENT_MASK for t, f in self.orientations):
            raise ValueError("bad orientation entry (index or flags out of range)")

    @classmethod
    def from_pairs(
        cls,
        pairs: Iterable[tuple[int, int]],
        orientations: Iterable[tuple[int, int]] = (),
    ) -> TileRearrangement:
        """A map from ``(virtual, actual)`` pairs, dropping the identity ones.

        The single normalizing constructor: identity entries — a position mapped
        to itself, a tile turned by nothing — carry no information, and leaving
        them in would make two equal rearrangements compare unequal, which undo
        and the project file both rely on.
        """
        # Coerce once per element: a drag rebuilds the whole map every frame, and
        # re-running int() on both halves of every pair was a third of that cost.
        moved = {v: a for v, a in ((int(v), int(a)) for v, a in pairs) if v != a}
        turned = {t: f for t, f in ((int(t), int(f)) for t, f in orientations) if f}
        return cls(tuple(sorted(moved.items())), tuple(sorted(turned.items())))

    # The three lookup tables below are built once per map, not once per lookup.
    # Rendering and editing resolve the map a tile at a time — a window is
    # thousands of lookups — and rebuilding a dict of every moved tile for each of
    # them made a single ``actual()`` cost as much as the whole map is big. Safe to
    # hold because the class is frozen; callers that need to *change* one take
    # their own copy (see :meth:`rearranged`).
    @cached_property
    def _forward(self) -> dict[int, int]:
        return dict(self.pairs)

    @cached_property
    def _reverse(self) -> dict[int, int]:
        return {a: v for v, a in self.pairs}

    @cached_property
    def _orientations(self) -> dict[int, int]:
        return dict(self.orientations)

    def is_identity(self) -> bool:
        """True when nothing is rearranged *or* turned — the fast path everywhere.

        Both halves matter: a map with no moves but a turned tile still has to
        go through the gather path, or the orientation would silently not render.
        """
        return not self.pairs and not self.orientations

    def orient_of(self, actual: int) -> int:
        """The orientation tile ``actual`` is displayed in (0 = as stored)."""
        return self._orientations.get(actual, TILE_ORIENT_NONE)

    def oriented(self, actuals: Iterable[int], flags: int) -> TileRearrangement:
        """A new map with ``flags`` **composed onto** each of ``actuals``.

        Composed rather than assigned, because the buttons and keys driving this
        act on what is currently *on screen*: pressing Rotate Right turns the
        displayed tile one more quarter whatever got it there, four presses come
        back to the start, and Flip H twice puts the tile back the way it was.
        Composition is also what makes H mean "mirror left-right on screen" on an
        already-turned tile, where the stored axis it lands on is the other one
        (:func:`compose_orientation`).

        Takes tile indices, not display positions — the orientation belongs to the
        tile (see the module docstring), so :meth:`swap` never has to touch it.
        """
        turned = dict(self.orientations)
        for actual in actuals:
            turned[actual] = compose_orientation(
                flags, turned.get(actual, TILE_ORIENT_NONE)
            )
        return replace(
            self, orientations=tuple(sorted((t, f) for t, f in turned.items() if f))
        )

    def actual(self, virtual: int) -> int:
        """The tile index that is *displayed* at ``virtual``."""
        return self._forward.get(virtual, virtual)

    def virtual(self, actual: int) -> int:
        """Where tile ``actual`` is displayed — the inverse of :meth:`actual`."""
        return self._reverse.get(actual, actual)

    def actual_run(self, first: int, count: int) -> list[int]:
        """The actual indices behind ``count`` virtual slots from ``first``.

        The form the decode/encode paths want: one lookup pass instead of a
        per-slot dict rebuild, and the identity case returns the plain range.
        """
        if self.is_identity():
            return list(range(first, first + count))
        forward = self._forward
        return [forward.get(i, i) for i in range(first, first + count)]

    def swap(self, a: int, b: int) -> TileRearrangement:
        """A new map with the tiles shown at ``a`` and ``b`` exchanged.

        Composition with a transposition, so repeated swaps accumulate into one
        permutation and swapping the same pair twice returns the original map.
        """
        return self.swap_many(((a, b),))

    def swap_many(self, moves: Iterable[tuple[int, int]]) -> TileRearrangement:
        """A new map with every ``(a, b)`` in ``moves`` exchanged at once.

        One multi-tile drag is one step, so the whole block has to move as a
        single permutation rather than a sequence of swaps that would tread on
        each other. The moves must therefore be **disjoint** — no index may
        appear twice across them — which is exactly the condition under which
        they are independent transpositions; an overlapping block drop is
        refused at the gesture rather than silently turning into a rotation.
        """
        moves = [(int(a), int(b)) for a, b in moves]
        touched = [i for move in moves for i in move]
        if len(set(touched)) != len(touched):
            raise ValueError("swap_many needs disjoint moves")
        sources = {}
        for a, b in moves:
            sources[a] = b
            sources[b] = a
        return self.rearranged(sources)

    def rearranged(self, sources: dict[int, int]) -> TileRearrangement:
        """A new map where each position ``dest`` shows what ``sources[dest]`` did.

        The general position move, of which :meth:`swap_many` is the two-cycle
        case. A block flip or turn needs it: mirroring a 3-wide block leaves its
        middle column where it is, and turning a 3×3 one leaves its centre, so the
        permutation is transpositions *plus fixed points* and cannot be expressed
        as a list of disjoint pairs.

        ``sources`` must be a bijection over the positions it names — the same set
        on both sides — or it would move tiles in from, or out to, positions it
        doesn't account for, and the map would stop being a permutation.

        Orientations ride through untouched: they are keyed by tile, and this
        moves positions, not tiles. That is exactly what makes a turned tile stay
        turned when it is dragged somewhere else.
        """
        if set(sources) != set(sources.values()):
            raise ValueError("a rearrangement must be a bijection over its positions")
        forward = dict(self._forward)  # a copy: the map's own table is shared
        # Resolved against the *old* map in one pass before any is written back:
        # source and destination sets overlap, so writing as we go would read
        # positions this very call has already moved.
        taken = {dest: forward.get(src, src) for dest, src in sources.items()}
        forward.update(taken)
        return TileRearrangement.from_pairs(forward.items(), self.orientations)

    def bounded(self, count: int) -> TileRearrangement:
        """This map restricted to the first ``count`` tiles.

        A map outlives the geometry it was made under — switching to a codec with
        larger tiles leaves fewer of them — and a pair pointing past the end
        would resolve to a blank slot whose edits go nowhere. The unit that can
        be dropped is a whole **cycle**, not a pair: a cycle is a closed round
        trip of positions, and removing one of them alone leaves the rest
        pointing at a tile no longer in the map. So a cycle survives only if
        every index in it is in range; the rest fall back to identity.

        An orientation has no such entanglement — it is one tile's own business —
        so an out-of-range one is simply dropped.
        """
        forward = self._forward
        keep: dict[int, int] = {}
        seen: set[int] = set()
        for start in forward:
            if start in seen:
                continue
            cycle = []
            node = start
            while node not in seen:
                seen.add(node)
                cycle.append(node)
                node = forward.get(node, node)
            if all(i < count for i in cycle):
                keep.update((i, forward[i]) for i in cycle)
        return TileRearrangement.from_pairs(
            keep.items(), ((t, f) for t, f in self.orientations if t < count)
        )


def compose_orientation(second: int, first: int) -> int:
    """The one orientation that applying ``first`` and then ``second`` amounts to.

    The eight orientations are a group, and this is its multiplication — which is
    what every button press needs: an orientation already on a tile plus the one
    the user just asked for is a single orientation to store, never a list to
    replay.

    Two mirrors are the plain XOR the flags started life as. A transpose in
    ``first`` has traded the tile's axes, so ``second``'s mirror bits trade with
    it — that swap is the whole content of the group's non-commutativity, and it
    is what makes Flip H after a quarter turn mirror what is *on screen* rather
    than the stored tile.
    """
    if first & TILE_TRANSPOSE:
        second = (
            (second & TILE_TRANSPOSE)
            | ((second & TILE_FLIP_H) << 1)
            | ((second & TILE_FLIP_V) >> 1)
        )
    return (first ^ second) & TILE_ORIENT_MASK


def invert_orientation(flags: int) -> int:
    """The orientation that undoes ``flags`` — what the write path turns back by.

    A mirror is its own inverse, which is why flips alone never needed this. A
    quarter turn is not: undoing one means turning the other way, and getting it
    backwards would leave an edit made on a turned tile stored at 180° to where it
    belongs — the art coming apart in exactly the way a baked-in flip would.
    """
    if not flags & TILE_TRANSPOSE:
        return flags & TILE_ORIENT_MASK
    # Transposing last means the mirrors it swapped come off in the other order,
    # which for these three bits is just H and V trading places.
    return TILE_TRANSPOSE | ((flags & TILE_FLIP_H) << 1) | ((flags & TILE_FLIP_V) >> 1)


def apply_orientation(grid, flags: int):  # noqa: ANN001, ANN201 — grid in, same out
    """``grid`` as it is *displayed* under ``flags``: the read path's one step.

    Mirrors first, then the transpose — the order the flag values are defined in
    (see :data:`TILE_TRANSPOSE`), and the order :func:`invert_orientation` is
    derived against.

    A turn swaps the tile's width and height, so on a tile that isn't square the
    result could not be shown in the cell it came from, nor written back to the
    bytes it came from. Such an orientation is therefore **ignored** — kept in the
    map, so a codec with square tiles brings it back, but not rendered. The write
    path inverts to an orientation that is still a turn, so it is ignored in
    lockstep here and the round trip stays exact.
    """
    if not flags:
        return grid
    if flags & TILE_TRANSPOSE and grid.width != grid.height:
        return grid
    if flags & TILE_FLIP_H:
        grid = transform.flip_horizontal(grid)
    if flags & TILE_FLIP_V:
        grid = transform.flip_vertical(grid)
    if flags & TILE_TRANSPOSE:
        grid = transform.transpose(grid)
    return grid


def unapply_orientation(grid, flags: int):  # noqa: ANN001, ANN201 — grid in, same out
    """``grid`` back in the orientation the **file** holds it in: the write path's.

    The counterpart of :func:`apply_orientation`, and the reason there are two
    functions rather than one: a turn is not an involution, so "apply it again"
    does not undo it the way a mirror does. Routing the write path
    through the read function with an inverted orientation keeps the pair unable
    to disagree about what any given orientation means — an edit made on a turned
    tile has to land back the way the file holds it, or the turn bakes into the
    file and the art comes apart.
    """
    return apply_orientation(grid, invert_orientation(flags))


def coalesce_runs(
    indices: Iterable[int], gap: int = RUN_MERGE_GAP
) -> list[tuple[int, int]]:
    """``(first, count)`` runs covering ``indices``, merged across small gaps.

    The batching behind every mapped read and write: scattered tile indices
    become as few contiguous runs as is worth it, so the codec is called once per
    region instead of once per tile. Runs no further apart than ``gap`` are
    merged — see :data:`RUN_MERGE_GAP`. An unrearranged window's indices collapse
    to one run, so the ordinary view pays nothing for the machinery.
    """
    ordered = sorted(set(indices))
    if not ordered:
        return []
    runs: list[tuple[int, int]] = []
    start = prev = ordered[0]
    for index in ordered[1:]:
        if index - prev <= gap + 1:
            prev = index
            continue
        runs.append((start, prev - start + 1))
        start = prev = index
    runs.append((start, prev - start + 1))
    return runs
