"""The data-LUT reshape engine — value substitutions as data.

The complement of :mod:`~celpix.plugins.bitswap`: that engine permutes the byte
*index* and never touches a value, this one substitutes the *value* and never
moves a byte. No address permutation can express a value change, so a board that
scrambles both ways needs both (``docs/design/reshape-stage.md`` §8).

A board's transform is nearly always a **bit permutation** of each byte or 16-bit
word, but the kernel here is the more general 256-entry lookup table. A
permutation compiles to a LUT at load, so ``bitswaps`` is the convenient
authoring form while XOR masks and outright substitutions stay expressible as
``luts``.

```toml
id = "reshape.nmk-bg"
name = "NMK background GFX descramble"
engine_id = "reshape.data-lut"

[params]
unit = 1                          # 1 = byte, 2 = big-endian word
selector_bits = [2, 11, 18]       # region-offset bits picking the table
bitswaps = [[3, 0, 7, 2, 5, 1, 4, 6], ...]   # MAME argument order, msb first
```

**The table can depend on where the byte is.** NMK's descramblers pick one of
eight permutations from three address lines, so ``selector_bits`` names those
lines — positions in the **region offset**, lsb first — and ``selector_remap``
covers boards that put a lookup between the extracted value and the table index.
Omit both and one table applies throughout.

That address dependence is why this is a Reshape rather than a pixel codec: the
selector reads offsets far outside any one tile, and codecs are buffer-relative
so a window of a large file still decodes (``docs/design/overview.md`` §4).

Every LUT must be a **bijection**, so the inverse is well defined and write-back
stays byte-exact. Permutations and XOR masks both qualify; only tables that would
lose data are rejected.

The hot loop is byte-parallel: each table is applied to the whole buffer at once
in C and the results merged by a selector mask (:func:`_selector_masks`) through
one big-integer OR, costing a few milliseconds for a region where a per-byte
Python loop would cost a few hundred nanoseconds a byte. Qt-free.
"""

from __future__ import annotations

from functools import lru_cache

from celpix.core.context import PipelineContext
from celpix.core.errors import Stage
from celpix.plugins._byteops import or_all
from celpix.plugins.base import PluginInfo, check_declared_stage

DATA_LUT_ENGINE = "reshape.data-lut"

# Byte and big-endian word are the two widths arcade descramblers use — NMK's
# pair of chips is one of each. A wider unit would need a table per byte lane
# anyway, so generalising past these gains nothing.
UNITS = (1, 2)

# The highest region-offset bit a selector may read. The masks below are
# materialised at 2^(bit+1) bytes, so this bounds their memory: at 20 a full set
# is 8 x 2 MiB, and the widest selector in the MAME corpus (NMK's sprite
# descrambler, bits 4/17/20) sits exactly there.
MAX_SELECTOR_BIT = 20

# 2^3 tables is what NMK's two descramblers use; the cap keeps a typo'd selector
# from asking for a table set large enough to matter.
MAX_SELECTOR_BITS = 4


def _permutation_lut(bits_msb: tuple[int, ...], width: int) -> list[int]:
    """A bit permutation, written MAME-style, as a value->value table.

    ``bits_msb`` is ``bitswap<N>``'s argument list: entry *k* names the source bit
    of output bit ``N-1-k``. Compiling it to a LUT lets one kernel serve
    permutations, XOR masks and arbitrary substitutions alike.
    """
    n = len(bits_msb)
    return [
        sum(((value >> bits_msb[n - 1 - k]) & 1) << k for k in range(n))
        for value in range(1 << width)
    ]


def _invert(lut: list[int]) -> list[int]:
    """The inverse table. Sound only for a bijection, which is validated."""
    out = [0] * len(lut)
    for value, mapped in enumerate(lut):
        out[mapped] = value
    return out


def _compile(lut: list[int], unit: int) -> tuple[tuple[bytes, ...], ...]:
    """``[source lane][destination lane]`` -> a 256-byte ``translate`` table.

    A unit's new value is the OR of its source bytes' contributions, so a 16-bit
    substitution never needs a 65536-entry table; decomposed *per output byte*
    like this, every lookup is a plain ``bytes.translate`` over a strided slice —
    ``unit**2`` passes in C rather than a Python loop per byte.

    The decomposition holds because every output bit of a permutation comes from
    exactly one input bit, hence one source lane. ``luts``, which need not
    decompose, is restricted to single bytes for that reason.
    """
    return tuple(
        tuple(
            bytes(
                (lut[value << (8 * (unit - 1 - src))] >> (8 * (unit - 1 - dst))) & 0xFF
                for value in range(256)
            )
            for dst in range(unit)
        )
        for src in range(unit)
    )


@lru_cache(maxsize=32)
def _selector_masks(bits: tuple[int, ...], length: int) -> tuple[int, ...]:
    """A byte mask per selector value: 0xFF where that table applies, else 0.

    One address bit is a square wave — ``2^p`` bytes low then ``2^p`` high — which
    ``bytes * n`` builds in C, and a multi-bit selector is the AND of one wave per
    bit. Building these per byte instead would cost seconds rather than
    milliseconds, so the shape of the construction is the point.
    """
    masks = []
    everything = (1 << (length * 8)) - 1
    for value in range(1 << len(bits)):
        mask = everything
        for k, bit in enumerate(bits):
            half = 1 << bit
            cell = b"\x00" * half + b"\xff" * half
            if not (value >> k) & 1:
                cell = cell[half:] + cell[:half]
            repeated = cell * (length // len(cell) + 1)
            mask &= int.from_bytes(repeated[:length], "big")
        masks.append(mask)
    return tuple(masks)


class DataLutReshape:
    """One address-selected substitution table set, as a reshape plugin.

    Both directions come from the same tables, ``unshape`` using their inverses
    under the *same* selector — sound because the selector reads the offset and a
    substitution never moves a byte.
    """

    def __init__(
        self,
        plugin_id: str,
        name: str,
        luts: list[list[int]],
        selector_bits: tuple[int, ...] = (),
        unit: int = 1,
        category: str = "",
    ) -> None:
        self._unit = unit
        self._bits = selector_bits
        # Compiled once rather than per call: the lane tables derive from the
        # LUT's contents, so caching them by that content would hash a
        # 65536-entry table on every chunk.
        self._forward = [_compile(lut, unit) for lut in luts]
        self._backward = [_compile(_invert(lut), unit) for lut in luts]
        # Chunking by the selector's period keeps the masks bounded: the
        # selector repeats every 2^(highest bit + 1) bytes, so one set of masks
        # serves every chunk.
        self._period = (1 << (max(selector_bits) + 1)) if selector_bits else 0
        self.info = PluginInfo(
            id=plugin_id, name=name, stage=Stage.RESHAPE, category=category
        )

    def _map(self, data: bytes, tables: tuple[tuple[bytes, ...], ...]) -> bytes:
        """Substitute every unit of ``data`` through one compiled table set."""
        unit = self._unit
        if unit == 1:
            return data.translate(tables[0][0])
        sources = [data[src::unit] for src in range(unit)]
        out = bytearray(len(data))
        for dst in range(unit):
            out[dst::unit] = or_all(
                [sources[src].translate(tables[src][dst]) for src in range(unit)]
            )
        return bytes(out)

    def _apply(self, data: bytes, luts: list[tuple[tuple[bytes, ...], ...]]) -> bytes:
        # A tail short of a whole unit cannot be substituted, so it passes
        # through, as with the split-plane joins.
        whole = (len(data) // self._unit) * self._unit
        body, tail = data[:whole], data[whole:]
        if not self._bits:
            return self._map(body, luts[0]) + tail
        out = bytearray(len(body))
        for base in range(0, len(body), self._period):
            chunk = body[base : base + self._period]
            masks = _selector_masks(self._bits, len(chunk))
            merged = 0
            for value, lut in enumerate(luts):
                merged |= int.from_bytes(self._map(chunk, lut), "big") & masks[value]
            out[base : base + len(chunk)] = merged.to_bytes(len(chunk), "big")
        return bytes(out) + tail

    def reshape(self, data: bytes, ctx: PipelineContext) -> bytes:
        return self._apply(data, self._forward)

    def unshape(self, data: bytes, ctx: PipelineContext) -> bytes:
        return self._apply(data, self._backward)


def _checked_selector(params: dict) -> tuple[int, ...]:
    bits = params.get("selector_bits", [])
    if not isinstance(bits, (list, tuple)) or not all(
        isinstance(b, int) and not isinstance(b, bool) for b in bits
    ):
        raise ValueError("params.selector_bits must be a list of bit positions")
    bits = tuple(bits)
    if len(bits) > MAX_SELECTOR_BITS:
        raise ValueError(
            f"params.selector_bits takes at most {MAX_SELECTOR_BITS} bits, "
            f"got {len(bits)}"
        )
    if len(set(bits)) != len(bits):
        raise ValueError("params.selector_bits must not repeat an address line")
    for bit in bits:
        if not 0 <= bit <= MAX_SELECTOR_BIT:
            raise ValueError(
                f"params.selector_bits entries must be 0..{MAX_SELECTOR_BIT}, got {bit}"
            )
    return bits


def _resolve_luts(params: dict, unit: int, wanted: int) -> list[list[int]]:
    """The table set, from either authoring form, validated as bijections."""
    width = 8 * unit
    raw, bitswaps = params.get("luts"), params.get("bitswaps")
    if (raw is None) == (bitswaps is None):
        raise ValueError("params needs exactly one of `bitswaps` or `luts`")
    if bitswaps is not None:
        if not isinstance(bitswaps, (list, tuple)) or not bitswaps:
            raise ValueError("params.bitswaps must be a non-empty list of tables")
        luts = []
        for table in bitswaps:
            if sorted(table) != list(range(width)):
                raise ValueError(
                    f"each params.bitswaps table must be a permutation of "
                    f"0..{width - 1} - every data line exactly once"
                )
            luts.append(_permutation_lut(tuple(table), width))
    else:
        # A raw table is byte-wide by construction: a 16-bit one would need
        # 65536 entries, and the boards wanting a wider unit all use
        # permutations, which `bitswaps` expresses compactly.
        if unit != 1:
            raise ValueError("params.luts is byte-only; use `bitswaps` for unit = 2")
        if not isinstance(raw, (list, tuple)) or not raw:
            raise ValueError("params.luts must be a non-empty list of tables")
        luts = []
        for table in raw:
            if sorted(table) != list(range(256)):
                raise ValueError(
                    "each params.luts table must be a permutation of 0..255 - a "
                    "table that is not a bijection could not be written back"
                )
            luts.append(list(table))
    if len(luts) != wanted:
        raise ValueError(f"{wanted} table(s) needed for this selector, got {len(luts)}")
    return luts


def data_lut_from_spec(spec: dict) -> DataLutReshape:
    """Build the plugin a parsed preset spec describes."""
    engine = spec.get("engine_id")
    if engine != DATA_LUT_ENGINE:
        raise ValueError(
            f"engine_id {engine!r} is not this reshape engine "
            f"(expected {DATA_LUT_ENGINE!r})"
        )
    check_declared_stage(spec, Stage.RESHAPE)
    params = spec.get("params", {})
    if not isinstance(params, dict):
        raise ValueError("params must be a table")
    unit = params.get("unit", 1)
    if unit not in UNITS:
        raise ValueError(f"params.unit must be one of {UNITS}, got {unit!r}")
    bits = _checked_selector(params)
    if unit == 2 and 0 in bits:
        # Both bytes of a unit must resolve to the same table, and bit 0 is the
        # one that differs between them.
        raise ValueError("params.selector_bits cannot use bit 0 when unit = 2")
    luts = _resolve_luts(params, unit, 1 if not bits else 1 << len(bits))
    remap = params.get("selector_remap")
    if remap is not None:
        # Some boards put a lookup between the extracted address bits and the
        # table index (NMK's 215 MCU programs one into the 214). Folding it into
        # the table order keeps the hot loop a plain index.
        if sorted(set(remap)) != sorted(remap) or len(remap) != len(luts):
            raise ValueError(
                f"params.selector_remap must be {len(luts)} distinct table indices"
            )
        if not all(0 <= i < len(luts) for i in remap):
            raise ValueError("params.selector_remap entries must index a table")
        luts = [luts[i] for i in remap]
    return DataLutReshape(
        spec["id"], spec["name"], luts, bits, unit, spec.get("category", "")
    )
