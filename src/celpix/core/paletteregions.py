"""Pixel regions pinned to a subpalette row: showing a bank the way the game draws it.

A ROM's tile bank is rarely one palette's worth of art — the status bar sits at
palette 0, the player at 3, the enemies at 5 — because the hardware takes the row
from tilemap/OAM attributes that never travel with the pixel data. celPix has one
global :attr:`~celpix.core.document.ViewOptions.subpalette_row`, so such a bank can
only be read a group at a time. A :class:`PaletteRegions` records "these pixels
render through row *n*" so the whole sheet can be read at once.

This changes no bytes and no indices. It is display state exactly like the tile
rearrangement (:mod:`celpix.core.tilerearrangement`) — an edit made inside a
region still stores the index it always did, and the region only decides which
colours that index is shown through.

**Anchored in pixels, not bytes and not tile indices.** A byte is not a run of
pixels in most retro codecs: SNES 4bpp is *planar*, so pixels 0-7 of a tile are
assembled from four bytes sixteen apart, and a byte boundary lands nowhere in
particular in the picture. A span measured in bytes is therefore only meaningful at
whole-tile granularity, and silently meaningless inside one. A pixel index is
well-defined for every codec — planar, packed, or direct-colour — so a span
boundary is always a boundary the user can see.

A pixel index is also stable under the geometry changes that matter to a *picture*:
switching bit depth re-cuts the bytes but leaves the tile grid alone, so a pinned
region keeps covering the art it was drawn over; and if the tile size itself
changes, the region still covers the same area of picture, just a different number
of tiles. The trade, worth naming: a region follows the **picture**, not the data,
so a ``byte_nudge`` or a bit-depth switch moves what bytes it happens to sit on.
That is the right way round for a feature whose whole job is "colour what I am
looking at".

**Pixel 0 is the document's first pixel.** The address space is the picture as it
is laid out, tile-major in the ordinary 1D reading and bitmap-row-major under the
2D wide-bitmap walk (:func:`~celpix.core.arrangement.tile_pixel_spans`). It carries
no file base: pixels are a property of the decoded picture, not of the ROM's
address space, which is what the offset box and the hex dump are for.

**A tile belongs to the region holding its first pixel.** Under the 2D walk a tile
does not own a contiguous pixel run — its rows are a whole bitmap-row apart — so
pinning one there records one span per pixel row and every span lands in the
region. Deciding membership on the first pixel keeps the *lookup* a single point
query whatever the walk, while the stored spans still describe the picture honestly.

Storage is canonical — sorted, non-overlapping, and adjacent same-row spans merged
— so two equal assignments compare equal, which undo and the project file both
rely on, and so a point query is one binary search.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from functools import cached_property

# A (start, length) run of pixels in the document's own pixel space.
Span = tuple[int, int]


@dataclass(frozen=True)
class PaletteRegion:
    """``length`` pixels from ``start``, rendered through subpalette ``row``."""

    start: int
    length: int
    row: int

    @property
    def end(self) -> int:
        """One past the last pixel — the half-open bound every walk here uses."""
        return self.start + self.length


@dataclass(frozen=True)
class PaletteRegions:
    """The pinned regions of one entry: sorted, disjoint, coalesced.

    Construct through :meth:`from_regions` (or :meth:`assigned` / :meth:`cleared`)
    rather than passing ``regions`` directly — those establish the invariant the
    lookup depends on. Frozen, and every mutator returns a *new* value, so an
    assignment is an ordinary before/after pair for undo.
    """

    regions: tuple[PaletteRegion, ...] = ()

    @classmethod
    def from_regions(cls, items: Iterable[PaletteRegion]) -> PaletteRegions:
        """Normalize ``items`` into the canonical form.

        Empty spans are dropped and overlaps are resolved **earlier-wins**, since
        this is the tolerant path a project load comes through and a file that
        contradicts itself should still open deterministically. The editing
        gestures do not rely on that rule: :meth:`assigned` subtracts first, so
        what the user just pinned always wins.
        """
        ordered = sorted(
            (r for r in items if r.length > 0), key=lambda r: (r.start, r.end)
        )
        kept: list[PaletteRegion] = []
        for region in ordered:
            start = max(region.start, kept[-1].end) if kept else region.start
            if start >= region.end:
                continue  # wholly covered by an earlier span
            kept.append(PaletteRegion(start, region.end - start, region.row))
        return cls(tuple(_coalesce(kept)))

    # The lookup table is built once per value, not once per query: a refresh
    # resolves one row per visible tile — thousands of point queries against the
    # same regions — and rebuilding the key list for each would make a single
    # lookup cost as much as the whole set is big. Safe to hold because the class
    # is frozen.
    @cached_property
    def _starts(self) -> list[int]:
        return [region.start for region in self.regions]

    def is_empty(self) -> bool:
        """True when nothing is pinned — the fast path through the render cycle."""
        return not self.regions

    def region_at(self, pixel: int) -> PaletteRegion | None:
        """The region holding ``pixel``, or None.

        One binary search. The regions are disjoint by construction, so the only
        candidate is the last one starting at or before ``pixel`` — which is why
        this needs no interval tree: a stabbing structure earns its keep when
        spans overlap and a point can be in many, and here it never can be.
        """
        index = bisect_right(self._starts, pixel) - 1
        if index < 0:
            return None
        region = self.regions[index]
        return region if pixel < region.end else None

    def row_at(self, pixel: int, default: int) -> int:
        """The subpalette row ``pixel`` renders through, or ``default``."""
        region = self.region_at(pixel)
        return default if region is None else region.row

    def rows_for(self, offsets: Sequence[int], default: int | None) -> list[int | None]:
        """:meth:`row_at` over a whole window, in one call.

        The render path's entry point. Kept as one method rather than a loop at
        the call site so the per-tile work stays in a tight loop over cached
        state, and so the empty case — every document that has pinned nothing —
        short-circuits without touching the regions at all.

        ``default`` may be ``None``, which is how a caller asks *which* offsets are
        pinned rather than what each one renders through. The two are different
        questions and only the second has an answer for an unpinned offset: a
        renderer needs a row there and takes the view's, while a caller marking the
        pinned ones has to be able to tell "pinned to the view's row" — a real pin,
        with a row of its own that happens to match — from "not pinned at all".
        """
        if not self.regions:
            return [default] * len(offsets)
        starts, regions = self._starts, self.regions
        out = []
        for pixel in offsets:
            index = bisect_right(starts, pixel) - 1
            if index < 0:
                out.append(default)
                continue
            region = regions[index]
            out.append(region.row if pixel < region.end else default)
        return out

    def assigned(self, spans: Iterable[Span], row: int) -> PaletteRegions:
        """A new set with ``spans`` pinned to ``row``, overwriting what was there.

        The new assignment wins outright: whatever the spans cover is subtracted
        from the existing regions first, so pinning over a half-pinned selection
        leaves no slivers of the old row behind. Spans need not be sorted or
        disjoint — one tile under the 2D walk contributes one per pixel row, and
        a rectangle selection contributes a run per cell row.
        """
        wanted = _merge_spans(spans)
        if not wanted:
            return self
        kept = _subtract(self.regions, wanted)
        fresh = [PaletteRegion(start, length, row) for start, length in wanted]
        merged = sorted(kept + fresh, key=lambda r: r.start)
        return PaletteRegions(tuple(_coalesce(merged)))

    def cleared(self, spans: Iterable[Span]) -> PaletteRegions:
        """A new set with ``spans`` unpinned, splitting any region they cut."""
        wanted = _merge_spans(spans)
        if not wanted or not self.regions:
            return self
        return PaletteRegions(tuple(_coalesce(_subtract(self.regions, wanted))))

    def bounded(self, pixel_count: int, max_row: int) -> PaletteRegions:
        """This set restricted to ``pixel_count`` pixels and rows up to ``max_row``.

        Two things outlive the state a region was pinned under: the picture can
        get shorter (a re-read, a resized slice, a codec whose bigger tiles leave
        fewer of them), and the palette can (a File palette holding a single row).
        A span past the end matches no tile and a row past the palette would
        render the magenta missing-colour sentinel, so both are dropped rather
        than left to surface as a puzzle.

        Unlike :meth:`~celpix.core.tilerearrangement.TileRearrangement.bounded`
        this has no cycles to
        keep whole — a region is its own business. Bit depth alone never
        invalidates a region: it re-cuts the bytes but not the tile grid, so the
        pinned area of picture is unchanged.
        """
        clipped = []
        for region in self.regions:
            if region.row > max_row or region.start >= pixel_count:
                continue
            end = min(region.end, pixel_count)
            clipped.append(PaletteRegion(region.start, end - region.start, region.row))
        return PaletteRegions(tuple(_coalesce(clipped)))


def _merge_spans(spans: Iterable[Span]) -> list[Span]:
    """``spans`` sorted, positive-length, and overlaps/abutments fused.

    Both editing gestures feed this: the caller assembles one span per tile (or
    per 2D pixel row) without caring about order or adjacency, and everything
    downstream gets a clean sorted disjoint list to walk against.
    """
    ordered = sorted(s for s in spans if s[1] > 0)
    merged: list[Span] = []
    for start, length in ordered:
        if merged and start <= merged[-1][0] + merged[-1][1]:
            prev_start, prev_len = merged[-1]
            merged[-1] = (prev_start, max(prev_len, start + length - prev_start))
            continue
        merged.append((start, length))
    return merged


def _subtract(
    regions: Sequence[PaletteRegion], spans: Sequence[Span]
) -> list[PaletteRegion]:
    """The parts of ``regions`` that ``spans`` does not cover.

    A single merge walk over two sorted disjoint sequences rather than a pass per
    span, so clearing a wide selection costs the size of the picture, not the
    product of the two sets.
    """
    out: list[PaletteRegion] = []
    first = 0
    for region in regions:
        cursor, end = region.start, region.end
        # Spans wholly before this region can never matter again: both sequences
        # are sorted, so the next region starts even later.
        while first < len(spans) and spans[first][0] + spans[first][1] <= cursor:
            first += 1
        index = first
        while index < len(spans) and spans[index][0] < end:
            span_start, span_len = spans[index]
            if span_start > cursor:
                out.append(PaletteRegion(cursor, span_start - cursor, region.row))
            cursor = max(cursor, span_start + span_len)
            if cursor >= end:
                break
            index += 1
        if cursor < end:
            out.append(PaletteRegion(cursor, end - cursor, region.row))
    return out


def _coalesce(regions: Sequence[PaletteRegion]) -> list[PaletteRegion]:
    """Fuse abutting same-row spans in an already-sorted, disjoint sequence.

    Canonicalization, not tidiness: without it, pinning two halves of a run
    separately would compare unequal to pinning it in one gesture, and the
    project file would churn between two spellings of the same state.
    """
    out: list[PaletteRegion] = []
    for region in regions:
        if region.length <= 0:
            continue
        if out and out[-1].row == region.row and out[-1].end == region.start:
            out[-1] = PaletteRegion(
                out[-1].start, region.end - out[-1].start, region.row
            )
            continue
        out.append(region)
    return out
