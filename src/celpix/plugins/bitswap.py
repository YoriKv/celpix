"""The bitswap reshape engine — address-line permutations as data.

The dominant implementation of region reorderings across arcade hardware is an
**address-line bitswap**: a permutation applied to the byte *index*, one
algorithm with a per-board table (``docs/design/reshape-stage.md`` §6). This
module is that one algorithm; a TOML preset supplies the table:

```toml
id = "reshape.gaelco-16x16"
name = "Gaelco 16x16 tiles (bitswap)"
engine_id = "reshape.bitswap"

[params]
bits = [19, 18, 17, 16, 15, 12, 11, 10, 9, 8, 7, 6, 5, 14, 13, 4, 3, 2, 1, 0]
```

Unlike a pixel or palette preset, which stays data resolved through its engine at
decode time, a bitswap preset is adapted into an **ordinary reshape plugin
instance** at load (:func:`bitswap_from_spec`), because the Reshape stage
resolves plain plugin ids everywhere: the pipeline, the combos, and the
``reshape_id`` a project persists. Adapting at the edge leaves all of that
untouched — the mirror of what ``formats.adapt_format`` does for code formats.

``bits`` is **MAME's argument order** (most significant bit first), so a driver's
table copies verbatim: entry *k* names the source bit of output bit ``N-1-k``,
i.e. ``j = bitswap<N>(i, *bits)``. The load direction *scatters* —
``out[bitswap(i)] = in[i]`` — matching the common ``buffer[j] = src[i]``
descramble form; a driver written the other way round (``dst[i] =
src[bitswap(i)]``) sets ``gather = true`` rather than hand-inverting its table.

The table defines a ``2**N``-byte span and the region is permuted span by span,
so a table for one chip pair serves a region of several. A tail short of a span
passes through untouched, as with the split-plane joins; a region smaller than
one span raises, since nothing at all would be transformed.

Two separate bounds apply: :data:`MAX_BITS` caps how far the table *spans*, and
:data:`MAX_PERMUTED_BITS` how many of those lines actually *move*. Only the
second costs anything. Qt-free.
"""

from __future__ import annotations

from functools import lru_cache

from celpix.core.context import PipelineContext
from celpix.core.errors import Stage
from celpix.plugins.base import PluginInfo, check_declared_stage

BITSWAP_ENGINE = "reshape.bitswap"

# The largest table accepted: 2^24 = 16 MiB spans, which covers the widest
# swaps in the MAME corpus (PGM's sprite ROMs and Dynax's graphics both use
# bitswap<24>).
MAX_BITS = 24

# Span is not what costs anything: the chunk plan is sized by how many address
# lines actually *move*, and a table permuting all 24 would build a 16-million-
# entry plan. Real tables leave the low lines alone — they reorder tiles, not the
# bytes inside them (PGM's 24-line table holds its low 9 fixed) — so bounding the
# moving lines bounds the cost. 2^16 plan entries is comfortably above every
# table in the corpus.
MAX_PERMUTED_BITS = 16


def _identity_low(src_of: tuple[int, ...]) -> int:
    """How many low address lines the table leaves alone.

    Everything above them is what permutes, so this is both the chunk size the
    plan below copies in and the width :data:`MAX_PERMUTED_BITS` bounds. A
    permutation's inverse fixes the same low lines, so both directions of one
    preset always agree on it.
    """
    low = 0
    while low < len(src_of) and src_of[low] == low:
        low += 1
    return low


@lru_cache(maxsize=32)
def _chunk_plan(src_of: tuple[int, ...]) -> tuple[int, tuple[int, ...]]:
    """``(chunk_size, destination chunk per source chunk)`` for one span.

    ``src_of`` is lsb-indexed: output bit *k* comes from input bit ``src_of[k]``.
    Every low bit mapping to itself grows the chunk — bytes whose indices differ
    only in identity bits move together — so the per-span loop runs ``2^(N-low)``
    slice copies instead of ``2^N`` byte moves, which is what makes the
    pure-Python loop fast enough on real tables.

    The table being a permutation whose low positions map to themselves, the
    remaining positions permute among themselves, so the high field reduces to its
    own smaller bitswap.
    """
    n = len(src_of)
    low = _identity_low(src_of)
    reduced = tuple(b - low for b in src_of[low:])
    dst = tuple(
        sum(((hi >> src) & 1) << k for k, src in enumerate(reduced))
        for hi in range(1 << (n - low))
    )
    return 1 << low, dst


def _permute(data: bytes, src_of: tuple[int, ...]) -> bytes:
    """Scatter every whole span of ``data`` through the table; keep the tail."""
    if not data:
        return data
    span = 1 << len(src_of)
    whole = (len(data) // span) * span
    if not whole:
        raise ValueError(
            f"region ({len(data):#x} bytes) is smaller than this table's "
            f"{span:#x}-byte span, so nothing would be reshaped - the table "
            "and the region disagree about the hardware"
        )
    chunk, dst = _chunk_plan(src_of)
    out = bytearray(len(data))
    out[whole:] = data[whole:]
    for base in range(0, whole, span):
        for s, d in enumerate(dst):
            src_at = base + s * chunk
            dst_at = base + d * chunk
            out[dst_at : dst_at + chunk] = data[src_at : src_at + chunk]
    return bytes(out)


class BitswapReshape:
    """One bitswap table as a reshape plugin — built from preset data.

    Both directions derive from the one table, as the permutation and its
    inverse, so they cannot fall out of step the way a hand-written pair could.
    """

    def __init__(
        self,
        plugin_id: str,
        name: str,
        bits: object,
        gather: bool = False,
        category: str = "",
    ) -> None:
        if not isinstance(bits, (list, tuple)) or not all(
            isinstance(b, int) and not isinstance(b, bool) for b in bits
        ):
            raise ValueError("params.bits must be a list of bit positions")
        n = len(bits)
        if not 1 <= n <= MAX_BITS:
            raise ValueError(f"params.bits takes 1..{MAX_BITS} entries, got {n}")
        if sorted(bits) != list(range(n)):
            raise ValueError(
                f"params.bits must be a permutation of 0..{n - 1} - every "
                "address line exactly once"
            )
        # The TOML is msb-first (MAME's argument order); index by output bit.
        src_of = tuple(reversed(tuple(bits)))
        moving = n - _identity_low(src_of)
        if moving > MAX_PERMUTED_BITS:
            raise ValueError(
                f"params.bits may move at most {MAX_PERMUTED_BITS} address "
                f"lines, but this table moves {moving} - the chunk plan for it "
                f"would need {1 << moving:,} entries. A real table of this span "
                "leaves its low lines alone"
            )
        inverse = [0] * n
        for k, src in enumerate(src_of):
            inverse[src] = k
        forward, backward = (src_of, tuple(inverse))
        if gather:
            forward, backward = backward, forward
        self._forward = forward
        self._backward = backward
        self.info = PluginInfo(
            id=plugin_id, name=name, stage=Stage.RESHAPE, category=category
        )

    def reshape(self, data: bytes, ctx: PipelineContext) -> bytes:
        return _permute(data, self._forward)

    def unshape(self, data: bytes, ctx: PipelineContext) -> bytes:
        return _permute(data, self._backward)


def bitswap_from_spec(spec: dict) -> BitswapReshape:
    """Build the plugin a parsed preset spec describes.

    ``engine_id`` is required and must name this engine. On a reshape preset it is
    a **discriminator** rather than a registry key: the spec is adapted into a
    :class:`BitswapReshape` here instead of resolving to a registered plugin. That
    keeps the preset self-describing and is what
    :data:`~celpix.plugins.discovery.RESHAPE_ENGINES` dispatches on.
    """
    engine = spec.get("engine_id")
    if engine != BITSWAP_ENGINE:
        raise ValueError(
            f"engine_id {engine!r} is not a reshape engine "
            f"(expected {BITSWAP_ENGINE!r})"
        )
    check_declared_stage(spec, Stage.RESHAPE)
    params = spec.get("params", {})
    if not isinstance(params, dict):
        raise ValueError("params must be a table")
    return BitswapReshape(
        spec["id"],
        spec["name"],
        params.get("bits"),
        bool(params.get("gather", False)),
        spec.get("category", ""),
    )
