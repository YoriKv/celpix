"""Qt-free readers the model suites share — the sibling of ``uihelpers``.

Same rule as that module: a helper lands here once more than one ``test_*.py``
needs it, and one used by a single suite stays in that suite. The split is the
one the app itself draws — ``uihelpers`` builds windows and imports Qt, so a
suite that runs under ``-m "not qt"`` cannot reach for it.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from celpix.core.context import KEY_INPUTS, PipelineContext
from celpix.core.errors import Stage
from celpix.plugins.base import InputKind, InputSpec, PluginInfo


def decoded_at_probe_length(
    engine: Any, params: dict, fill: Callable[[int], bytes]
) -> tuple[list, bytes, PipelineContext]:
    """``(cells, data, ctx)`` at the first length ``engine`` decodes anything from.

    **Grown rather than assumed to be four cells.** A cell stride is what most
    tilemap formats read at, but one whose colour lives in a plane *after* its
    cells reads a whole page or nothing — the two planes mean nothing apart — so
    a four-byte buffer is not a short map there, it is not a map at all. A caller
    that hard-codes one length silently stops covering the next format that needs
    another, which is how this ended up written twice.

    ``fill`` makes the probe bytes, and is the caller's because what may be
    asserted about the round trip differs. A preset whose layout leaves bits
    unclaimed (``.`` in its ``fields``) writes them back as zero, so only an
    all-zero probe is byte-exact across *every* shipped preset; a caller covering
    engines that model every bit passes something stronger, which catches a field
    dropped on the way through.

    The context is the helper's and comes back with the cells, so an encode is
    checked against the one that decoded — a format that publishes something
    about the file it just read (a page geometry, an attribute plane) needs its
    own read's answers and not a previous probe's.

    Empty cells and ``b""`` when nothing decoded at any length, for the caller to
    assert against with its own name for the format.
    """
    for length in (engine.bytes_per_cell(params) * 4, 1024, 2048):
        ctx = PipelineContext()
        data = fill(length)
        cells = engine.decode(data, params, ctx)
        if cells:
            return cells, data, ctx
    return [], b"", PipelineContext()


XOR_ID = "compression.test-xor-table"


class XorTableCodec:
    """A scheme whose stream is XORed against a table kept elsewhere — the
    smallest plugin with **inputs** (``docs/design/plugin-inputs.md``).

    Three inputs, one of each kind: the table is a required region of 2-byte
    elements, the output size an optional integer that truncates the result,
    and a flag that inverts every byte on top. Like PackBits it has no end
    marker, so it never reports completion. Both directions are the one XOR
    (and the one inversion), so a round trip is exact whatever the table.
    """

    info = PluginInfo(
        id=XOR_ID,
        name="XOR against a table",
        stage=Stage.COMPRESSION,
        self_delimiting=False,
        inputs=(
            InputSpec("table", "Key table", InputKind.REGION, stride=2, unit="word"),
            InputSpec(
                "output_size",
                "Output size",
                InputKind.INTEGER,
                required=False,
                minimum=1,
                maximum=0x10000,
                unit="byte",
            ),
            InputSpec("invert", "Invert", InputKind.FLAG, required=False),
        ),
    )

    def decompress(self, data: bytes, ctx: PipelineContext) -> bytes:
        inputs = ctx.get(KEY_INPUTS) or {}
        out = xor_bytes(data, inputs["table"])
        if inputs.get("invert"):
            out = bytes(b ^ 0xFF for b in out)
        return out[: inputs["output_size"]] if "output_size" in inputs else out

    def compress(self, data: bytes, ctx: PipelineContext) -> bytes:
        return self.decompress(data, ctx)


def xor_bytes(data: bytes, table: bytes) -> bytes:
    return bytes(b ^ table[i % len(table)] for i, b in enumerate(data))
