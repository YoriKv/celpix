"""Virtual tile rearrangement: showing tiles somewhere other than they live.

Tiles are rarely stored in the order they are drawn in — a face's eyes, mouth and
hair can sit hundreds of tiles apart because the game streams them by animation
frame, not by picture. A :class:`TileMap` lets the view show them side by side
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

A tile may also be shown **flipped** — the other half of reading scattered art,
since hardware mirrors tiles rather than storing both halves of a symmetric
sprite. That flip is display state too: the view flips on the way out and unflips
on the way back in, so what reaches the file is always the tile's own orientation.
Flips are keyed by *actual* tile index, so a flipped tile stays flipped when it is
dragged somewhere else — you flipped it to make the art read, and moving it should
keep it reading. Only H and V, matching the flip bits real tile attributes carry;
a rotation has no hardware analogue and would not survive a non-square tile.

Storage is sparse and canonical — only tiles that actually moved or flipped are
listed — so an unrearranged document costs nothing and two equal maps compare
equal.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from functools import cached_property

from celpix.core import transform

# Display-flip flags, as a bitmask per tile. Both set is a 180° turn, which is how
# the hardware expresses it too — there is no separate rotate bit.
TILE_FLIP_NONE = 0
TILE_FLIP_H = 1
TILE_FLIP_V = 2
TILE_FLIP_BOTH = TILE_FLIP_H | TILE_FLIP_V

# How many unwanted tiles a decode run will swallow to avoid becoming two runs.
# A rearranged window resolves to scattered tile indices, and each run costs a
# codec call plus its region arithmetic — far more than decoding a handful of
# neighbours nobody asked for. So runs separated by a small gap are merged, which
# collapses a locally shuffled area (the common case: tiles pulled together from
# around one sprite) back into a single decode. Tiles genuinely far apart still
# get their own run rather than dragging the span between them along.
RUN_MERGE_GAP = 8


@dataclass(frozen=True)
class TileMap:
    """Where each displayed tile really lives, and how it is shown.

    ``pairs`` holds ``(virtual, actual)`` for the moved tiles only, sorted by
    virtual index; every index not listed maps to itself. ``flips`` holds
    ``(actual, flags)`` for the flipped tiles only — keyed by the tile's *own*
    index, not its display position, so a flip rides along when the tile is
    moved. Construct through :meth:`from_pairs` (or the :meth:`swap` / :meth:`flip`
    family) rather than passing these directly — those normalize the input and
    check the permutation invariant.

    Frozen, and every mutator returns a *new* map, so a rearrangement step is an
    ordinary before/after pair for undo and the maps are cheap to hold: only the
    moved and flipped tiles are stored, whatever the size of the file.
    """

    pairs: tuple[tuple[int, int], ...] = ()
    flips: tuple[tuple[int, int], ...] = ()

    def __post_init__(self) -> None:
        virtuals = [v for v, _ in self.pairs]
        actuals = [a for _, a in self.pairs]
        # A repeat on either side is the failure mode the class exists to
        # prevent: two virtual slots sharing one actual tile makes an edit to
        # either overwrite the other.
        if len(set(virtuals)) != len(virtuals) or len(set(actuals)) != len(actuals):
            raise ValueError("a tile map must be a permutation (repeated index)")
        # Same *set* on both sides, or the map would move tiles in from — or out
        # to — indices it doesn't account for, and would not be a permutation of
        # the whole index space.
        if set(virtuals) != set(actuals):
            raise ValueError("a tile map must be a permutation (unbalanced)")
        if any(v < 0 or a < 0 for v, a in self.pairs):
            raise ValueError("tile indices cannot be negative")
        indices = [tile for tile, _ in self.flips]
        if len(set(indices)) != len(indices):
            raise ValueError("a tile cannot carry two flip states")
        if any(t < 0 or not 0 < f <= TILE_FLIP_BOTH for t, f in self.flips):
            raise ValueError("bad flip entry (index or flags out of range)")

    @classmethod
    def from_pairs(
        cls,
        pairs: Iterable[tuple[int, int]],
        flips: Iterable[tuple[int, int]] = (),
    ) -> TileMap:
        """A map from ``(virtual, actual)`` pairs, dropping the identity ones.

        The single normalizing constructor: identity entries — a position mapped
        to itself, a tile flipped by nothing — carry no information, and leaving
        them in would make two equal rearrangements compare unequal, which undo
        and the project file both rely on.
        """
        moved = {int(v): int(a) for v, a in pairs if int(v) != int(a)}
        turned = {int(t): int(f) for t, f in flips if int(f)}
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
    def _flips(self) -> dict[int, int]:
        return dict(self.flips)

    def is_identity(self) -> bool:
        """True when nothing is rearranged *or* flipped — the fast path everywhere.

        Both halves matter: a map with no moves but a flipped tile still has to
        go through the gather path, or the flip would silently not render.
        """
        return not self.pairs and not self.flips

    def flip_of(self, actual: int) -> int:
        """The flip flags tile ``actual`` is displayed with (0 = as stored)."""
        return self._flips.get(actual, TILE_FLIP_NONE)

    def flip(self, actuals: Iterable[int], flags: int) -> TileMap:
        """A new map with ``flags`` **toggled** on each of ``actuals``.

        Toggled rather than set, because the buttons and keys driving this are
        two-state: pressing Flip H twice puts the tile back the way it was, and
        an H on an already-H-flipped tile has no other sensible reading.

        Takes tile indices, not display positions — the flip belongs to the tile
        (see the module docstring), so :meth:`swap` never has to touch it.
        """
        turned = dict(self.flips)
        for actual in actuals:
            turned[actual] = turned.get(actual, TILE_FLIP_NONE) ^ flags
        return replace(
            self, flips=tuple(sorted((t, f) for t, f in turned.items() if f))
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

    def swap(self, a: int, b: int) -> TileMap:
        """A new map with the tiles shown at ``a`` and ``b`` exchanged.

        Composition with a transposition, so repeated swaps accumulate into one
        permutation and swapping the same pair twice returns the original map.
        """
        return self.swap_many(((a, b),))

    def swap_many(self, moves: Iterable[tuple[int, int]]) -> TileMap:
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

    def rearranged(self, sources: dict[int, int]) -> TileMap:
        """A new map where each position ``dest`` shows what ``sources[dest]`` did.

        The general position move, of which :meth:`swap_many` is the two-cycle
        case. A block flip needs it: mirroring a 3-wide block leaves its middle
        column where it is, so the permutation is transpositions *plus fixed
        points* and cannot be expressed as a list of disjoint pairs.

        ``sources`` must be a bijection over the positions it names — the same set
        on both sides — or it would move tiles in from, or out to, positions it
        doesn't account for, and the map would stop being a permutation.

        Flips ride through untouched: they are keyed by tile, and this moves
        positions, not tiles. That is exactly what makes a flipped tile stay
        flipped when it is dragged somewhere else.
        """
        if set(sources) != set(sources.values()):
            raise ValueError("a rearrangement must be a bijection over its positions")
        forward = dict(self._forward)  # a copy: the map's own table is shared
        # Resolved against the *old* map in one pass before any is written back:
        # source and destination sets overlap, so writing as we go would read
        # positions this very call has already moved.
        taken = {dest: forward.get(src, src) for dest, src in sources.items()}
        forward.update(taken)
        return TileMap.from_pairs(forward.items(), self.flips)

    def bounded(self, count: int) -> TileMap:
        """This map restricted to the first ``count`` tiles.

        A map outlives the geometry it was made under — switching to a codec with
        larger tiles leaves fewer of them — and a pair pointing past the end
        would resolve to a blank slot whose edits go nowhere. The unit that can
        be dropped is a whole **cycle**, not a pair: a cycle is a closed round
        trip of positions, and removing one of them alone leaves the rest
        pointing at a tile no longer in the map. So a cycle survives only if
        every index in it is in range; the rest fall back to identity.

        A flip has no such entanglement — it is one tile's own business — so an
        out-of-range one is simply dropped.
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
        return TileMap.from_pairs(
            keep.items(), ((t, f) for t, f in self.flips if t < count)
        )


def apply_flip(grid, flags: int):  # noqa: ANN001, ANN201 — any grid, same kind back
    """``grid`` mirrored per ``flags`` — the display flip, and its own inverse.

    Both flips are involutions, so this single function serves the read path
    (storage orientation → what is shown) *and* the write path (what was edited →
    storage orientation). That is not a convenience: an edit made on a flipped
    tile has to land unflipped or the mirror would bake itself into the file, and
    having one function for both directions makes it impossible for them to
    disagree.
    """
    if not flags:
        return grid
    if flags & TILE_FLIP_H:
        grid = transform.flip_horizontal(grid)
    if flags & TILE_FLIP_V:
        grid = transform.flip_vertical(grid)
    return grid


def coalesce_runs(
    indices: Iterable[int], gap: int = RUN_MERGE_GAP
) -> list[tuple[int, int]]:
    """``(first, count)`` runs covering ``indices``, merged across small gaps.

    The batching behind every mapped read and write: scattered tile indices
    become as few contiguous runs as is worth it, so the codec is called once per
    region instead of once per tile. Runs no further apart than ``gap`` are
    merged — see :data:`RUN_MERGE_GAP`. An unrearranged run of indices collapses
    to the single run it always was, which is what keeps the ordinary view on
    exactly the code path it had before rearrangement existed.
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
