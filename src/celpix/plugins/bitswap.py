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

Unlike a pixel/palette preset — which stays data resolved through its engine at
decode time — a bitswap preset is adapted into an **ordinary reshape plugin
instance** at load time (:func:`bitswap_from_toml`), because the Reshape stage
resolves plain plugin ids everywhere: the pipeline, the combos, and the
``reshape_id`` a project persists. Adapting at the edge keeps all of that
untouched — the mirror of what ``formats.adapt_format`` does for code formats.

``bits`` is **MAME's argument order** (most significant bit first), so a
driver's table can be copied verbatim: entry *k* names the source bit of output
bit ``N-1-k``, i.e. ``j = bitswap<N>(i, *bits)``. The load direction *scatters*
— ``out[bitswap(i)] = in[i]`` — matching the common ``buffer[j] = src[i]``
descramble form; drivers written the other way round (``dst[i] =
src[bitswap(i)]``) set ``gather = true`` instead of hand-inverting the table.

The table defines a ``2**N``-byte block. The region is permuted block-wise, so
a table for one chip pair serves a region of several; a tail short of a block
passes through untouched (the same degradation rule as the split-plane joins);
a region smaller than one block hard-stops — nothing at all would be
transformed, which is a misconfiguration, not a tail.

Qt-free, like everything under ``plugins``.
"""

from __future__ import annotations

from functools import lru_cache

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover - exercised only on 3.9/3.10
    import tomli as tomllib

from celpix.core.context import PipelineContext
from celpix.core.errors import Stage
from celpix.plugins.base import PluginInfo

ENGINE_ID = "reshape.bitswap"

# The largest table accepted: 2^20 = 1 MiB blocks, the biggest region the MAME
# corpus swaps in one go (bitswap<20>). The bound is what keeps the chunk plan
# below a sane size for a table with no identity tail.
MAX_BITS = 20


@lru_cache(maxsize=32)
def _chunk_plan(src_of: tuple[int, ...]) -> tuple[int, tuple[int, ...]]:
    """``(chunk_size, destination chunk per source chunk)`` for one block.

    ``src_of`` is lsb-indexed: output bit *k* comes from input bit
    ``src_of[k]``. Every low bit that maps to itself grows the chunk — bytes
    whose indices differ only in identity bits move together — so the per-block
    loop runs ``2^(N-low)`` slice copies instead of ``2^N`` byte moves. Real
    tables keep several low bits intact (they reorder tiles or rows, not the
    bytes inside them), which is what makes the pure-Python loop fast enough.

    Because the table is a permutation and the low positions map to
    themselves, the remaining positions permute among themselves, so the high
    field reduces to its own smaller bitswap.
    """
    n = len(src_of)
    low = 0
    while low < n and src_of[low] == low:
        low += 1
    reduced = tuple(b - low for b in src_of[low:])
    dst = tuple(
        sum(((hi >> src) & 1) << k for k, src in enumerate(reduced))
        for hi in range(1 << (n - low))
    )
    return 1 << low, dst


def _permute(data: bytes, src_of: tuple[int, ...]) -> bytes:
    """Scatter every whole block of ``data`` through the table; keep the tail."""
    if not data:
        return data
    block = 1 << len(src_of)
    whole = (len(data) // block) * block
    if not whole:
        raise ValueError(
            f"region ({len(data):#x} bytes) is smaller than this table's "
            f"{block:#x}-byte block, so nothing would be reshaped - the table "
            "and the region disagree about the hardware"
        )
    chunk, dst = _chunk_plan(src_of)
    out = bytearray(len(data))
    out[whole:] = data[whole:]
    for base in range(0, whole, block):
        for s, d in enumerate(dst):
            src_at = base + s * chunk
            dst_at = base + d * chunk
            out[dst_at : dst_at + chunk] = data[src_at : src_at + chunk]
    return bytes(out)


class BitswapReshape:
    """One bitswap table as a reshape plugin — built from preset data.

    The two directions are the permutation and its inverse, derived from one
    table, so they cannot fall out of step the way a hand-written pair could.
    """

    def __init__(
        self, plugin_id: str, name: str, bits: object, gather: bool = False
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
        inverse = [0] * n
        for k, src in enumerate(src_of):
            inverse[src] = k
        forward, backward = (src_of, tuple(inverse))
        if gather:
            forward, backward = backward, forward
        self._forward = forward
        self._backward = backward
        self.info = PluginInfo(id=plugin_id, name=name, stage=Stage.RESHAPE)

    def reshape(self, data: bytes, ctx: PipelineContext) -> bytes:
        return _permute(data, self._forward)

    def unshape(self, data: bytes, ctx: PipelineContext) -> bytes:
        return _permute(data, self._backward)


def bitswap_from_spec(spec: dict) -> BitswapReshape:
    """Build the plugin a parsed preset spec describes.

    ``engine_id`` is required and must name this engine — the field is what
    keeps the preset self-describing and leaves room for other reshape engines
    to dispatch on it later. A stated ``stage`` is tolerated when it agrees,
    exactly as :func:`~celpix.plugins.discovery.preset_from_spec` tolerates it.
    """
    engine = spec.get("engine_id")
    if engine != ENGINE_ID:
        raise ValueError(
            f"engine_id {engine!r} is not a reshape engine (expected {ENGINE_ID!r})"
        )
    declared = spec.get("stage")
    if declared is not None and declared != Stage.RESHAPE.value:
        raise ValueError(
            f"stage {declared!r} conflicts with the folder's stage "
            f"{Stage.RESHAPE.value!r} - remove the stage field"
        )
    params = spec.get("params", {})
    if not isinstance(params, dict):
        raise ValueError("params must be a table")
    return BitswapReshape(
        spec["id"], spec["name"], params.get("bits"), bool(params.get("gather", False))
    )


def bitswap_from_toml(text: str) -> BitswapReshape:
    """Parse a bitswap preset's TOML source into a registrable plugin."""
    return bitswap_from_spec(tomllib.loads(text))
