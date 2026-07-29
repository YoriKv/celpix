"""The match search every LZ compressor here runs, once.

Three built-in schemes encode back-references — a 4 KiB ring LZSS, PRS, and the
command-byte LZ — and they differ entirely in how a match is *written* and not at
all in how one is *found*. The finding half is what lives here: an index from a
fixed-width byte prefix to the recent positions that share it, a bounded walk of
that chain newest-first, and an overlap-aware length count.

**Overlap is the part worth stating.** A match may legally reach past the position
being encoded, into bytes the decoder has not produced yet, because every one of
these decoders copies a byte at a time — so the source repeats with period
``distance`` and :meth:`MatchFinder.match_length` counts it modulo that period. A
length routine that stopped at ``at`` would silently give up the run-length
encoding these formats get for free.

What stays with each scheme is what only it knows: which distances it can reach,
what a match costs to write, and whether the longest match is the one it wants —
PRS deliberately prefers a nearer, shorter match when the cheaper op more than
pays for the bytes given up. So :meth:`longest` is offered for the two schemes
that want length alone, and :meth:`candidates` for the one that scores its own.
"""

from __future__ import annotations

from collections.abc import Iterator

# How many recent positions sharing a prefix a scheme tests by default. Highly
# repetitive data piles up thousands, and past the newest few dozen the extra
# candidates almost never yield a longer match — so the cap bounds a pathological
# input rather than costing anything on real data.
DEFAULT_CANDIDATES = 96


class MatchFinder:
    """A prefix index over one buffer, for finding back-references in it.

    Positions are added as the parse passes them (:meth:`add`, :meth:`add_run`),
    never in advance: a candidate has to be a position the decoder will already
    have produced, and indexing ahead of the cursor would offer matches that
    cannot be written.

    ``window`` is how far back the scheme can reach — the ring size, the largest
    encodable distance — or ``None`` where any earlier position is addressable, as
    it is for a format whose back-reference is an absolute offset within a bank.
    """

    __slots__ = ("_cap", "_data", "_index", "_min_match", "_n", "_window")

    def __init__(
        self,
        data: bytes,
        *,
        min_match: int,
        window: int | None = None,
        max_candidates: int = DEFAULT_CANDIDATES,
    ) -> None:
        self._data = data
        self._n = len(data)
        self._min_match = min_match
        self._window = window
        self._cap = max_candidates
        self._index: dict[bytes, list[int]] = {}

    def add(self, pos: int) -> None:
        """Index ``pos`` as a candidate for later positions.

        A bucket is trimmed to the newest ``max_candidates`` once it has grown to
        twice that, rather than on every insert: the walk only ever looks at the
        newest ones anyway, so trimming in batches keeps a run of identical bytes
        from turning each insert into a list copy.
        """
        if pos + self._min_match > self._n:
            return
        bucket = self._index.setdefault(self._data[pos : pos + self._min_match], [])
        bucket.append(pos)
        if len(bucket) > self._cap * 2:
            del bucket[: -self._cap]

    def add_run(self, start: int, end: int) -> None:
        """Index every position in ``[start, end)`` — the interior of a match.

        A match is emitted whole, so the positions it covered are stepped over by
        the parse and would otherwise never be indexed, leaving a hole in the
        history exactly where the data is most repetitive.
        """
        for pos in range(start, end):
            self.add(pos)

    def candidates(self, pos: int) -> Iterator[int]:
        """Reachable earlier positions sharing ``pos``'s prefix, newest first.

        Newest first because a nearer match is never worse — no scheme here pays
        *more* for a shorter distance — so the walk can stop at the first
        out-of-window candidate rather than filtering the whole chain.
        """
        bucket = self._index.get(self._data[pos : pos + self._min_match])
        if not bucket:
            return
        cutoff = -1 if self._window is None else pos - self._window
        for candidate in reversed(bucket[-self._cap :]):
            if candidate < cutoff:
                return  # the rest are older still — out of reach
            yield candidate

    def match_length(self, at: int, candidate: int, limit: int) -> int:
        """Length of the match at ``candidate``, counting legal self-overlap.

        See the module docstring: the copy may run past ``at``, so the source
        repeats with period ``at - candidate``.

        This is the innermost loop of every compress here, so the way to make it
        cheap is not to enter it: :meth:`can_reach` rules a candidate out on one
        comparison, and both callers use it before measuring.
        """
        data = self._data
        distance = at - candidate
        length = 0
        while length < limit:
            if data[at + length] != data[candidate + length % distance]:
                break
            length += 1
        return length

    def can_reach(self, at: int, candidate: int, length: int) -> bool:
        """Whether the match at ``candidate`` could be ``length`` bytes long.

        Tests the single byte a match that long would have to end on. A candidate
        that fails cannot reach ``length`` however well its front matches, so it
        never needs measuring — and over repetitive data, where a chain is long
        and most of it is beaten by the nearest few, that is what the search
        costs instead of a :meth:`match_length` per candidate.

        Necessary, not sufficient: the bytes between may still differ. It is a
        filter to put in front of the measurement, never a substitute for it.
        """
        if length <= 0 or at + length > self._n:
            return False
        distance = at - candidate
        offset = length - 1
        # Where the source is *reading* at that offset — past `distance` the copy
        # has wrapped back into its own output (see the module docstring).
        if offset >= distance:
            offset %= distance
        return self._data[candidate + offset] == self._data[at + length - 1]

    def longest(self, pos: int, limit: int) -> tuple[int, int]:
        """The longest reachable match at ``pos``, as ``(length, candidate)``.

        ``(0, -1)`` when there is none worth having — fewer than ``min_match``
        bytes left to match, or no reachable candidate. The walk stops early on a
        match that fills ``limit``, since nothing later in the chain can beat it.

        Only a candidate that could beat the best so far is measured: beating it
        means reaching ``best_len + 1`` bytes, which is the one-byte test
        :meth:`can_reach` makes — written out here rather than called because
        this loop runs tens of times per input byte and the call is a large
        share of what is left once the measuring is gone. The bounds ``can_reach``
        guards are given here: ``best_len < limit <= n - pos`` throughout.
        """
        if limit < self._min_match:
            return 0, -1
        data = self._data
        best_len, best_at = 0, -1
        for candidate in self.candidates(pos):
            if best_len:
                distance = pos - candidate
                offset = best_len if best_len < distance else best_len % distance
                if data[candidate + offset] != data[pos + best_len]:
                    continue
            length = self.match_length(pos, candidate, limit)
            if length > best_len:
                best_len, best_at = length, candidate
                if best_len == limit:
                    break
        return best_len, best_at
