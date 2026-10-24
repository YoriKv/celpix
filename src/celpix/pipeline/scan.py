"""Scanning a buffer for the next compressed structure.

Owns the forward scan behind **Find Next** (:func:`find_next_structure`): walk a
buffer offset by offset and ask a compression scheme whether a complete stream
starts there — the format's own word on where a structure ends. The question at
one offset is :func:`structure_at`'s, which the decompression preview asks too,
to find the end of a structure that runs past the view. And the one
heuristic laid over it, the smart scan's test that a complete stream plausibly
holds tiles (:func:`looks_like_graphics`), with the thresholds it was measured
at. Neither runs a pathway; both are re-exported from
:mod:`celpix.pipeline.pipeline` with the rest of the pipeline's surface.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from celpix.core.context import (
    KEY_COMPRESSED_SIZE,
    KEY_DECOMPRESS_COMPLETE,
    KEY_DECOMPRESS_PARTIAL,
    KEY_INPUTS,
    KEY_SURROUND,
    KEY_SURROUND_START,
    PipelineContext,
)
from celpix.plugins.base import CompressionPlugin, self_delimiting

# How much a window cut short of a structure grows by before the next try: a
# few rounds reach the end of any buffer, and each round copies no more than
# a few times the bytes the structure turns out to hold.
WINDOW_GROWTH = 4
# The most compressed bytes a chase may examine from one offset. Far more than
# any one structure a retro ROM holds (the largest in our sample projects is
# ~255 KB), while padding that a scheme decodes without ever erroring - zeros
# under most LZ formats - costs this much per offset and no more, instead of
# the rest of the buffer.
CHASE_LIMIT = 1 << 20


@dataclass(frozen=True)
class ScanResult:
    """Where a forward structure scan ended (:func:`find_next_structure`).

    ``found`` is the hit offset or ``None``; ``end`` is where the scan stopped
    (where the caller lands when there was no hit — ``len(data)`` once the whole
    buffer is exhausted); ``stopped`` is True when the caller aborted the scan
    via its tick callback rather than reaching the end.
    """

    found: int | None
    end: int
    stopped: bool


def _decode_at(
    data: bytes,
    plugin: CompressionPlugin,
    pos: int,
    size: int,
    inputs: dict[str, bytes | int | str] | None,
    *,
    partial: bool,
) -> tuple[bytes, PipelineContext]:
    """Decode the ``size`` bytes at ``pos`` on a fresh context."""
    ctx = PipelineContext()
    if inputs:
        ctx.set(KEY_INPUTS, inputs)
    if partial:
        ctx.set(KEY_DECOMPRESS_PARTIAL, True)
    # The probe is a window of `data`, and a scheme that copies from the bytes
    # before its stream has to be told where in `data` the window sits, or it
    # would resolve those copies against the window's own start.
    ctx.set(KEY_SURROUND, data)
    ctx.set(KEY_SURROUND_START, pos)
    return plugin.decompress(data[pos : pos + size], ctx), ctx


def structure_at(
    data: bytes,
    plugin: CompressionPlugin,
    pos: int,
    probe_bytes: int,
    *,
    inputs: dict[str, bytes | int | str] | None = None,
    chase: bool | None = None,
    tick: Callable[[], bool] | None = None,
) -> tuple[bytes, int] | None:
    """The complete structure starting at ``data[pos]``, as ``(output,
    consumed)``, or ``None`` where none starts there.

    Complete means a non-empty decode that the plugin **reports complete**
    (:data:`KEY_DECOMPRESS_COMPLETE`): the structure's own end — terminator or
    declared size — was found. It is looked for in the ``probe_bytes`` at
    ``pos`` first. Where that window cuts a structure short, the window grows
    (:data:`WINDOW_GROWTH`) and the decode is tried again, up to the end of
    ``data`` or :data:`CHASE_LIMIT` bytes, whichever comes first: a format
    whose header states its size can be far bigger than any one view, and is no
    less a structure for it. One whose end lies past the limit is not found.

    ``chase`` is whether the scheme carries its own end under ``inputs``
    (:func:`~celpix.plugins.base.self_delimiting`), computed when not given; a
    scan asks it once rather than at every offset. ``tick`` is called before
    each wider round, and returning True abandons the chase with ``None`` —
    the caller that passed it knows to read that as a stop, not a miss.

    *Cut short* is the lenient decode's word (the preview's,
    :data:`KEY_DECOMPRESS_PARTIAL`), which changes only what a decoder does when
    its input runs out: it hands back what it has instead of raising. So one
    lenient decode answers both questions — complete is a hit exactly as a
    strict one would be, output without the end is a structure running past the
    window, and a failure or no output is no structure at all and is not
    chased. Only a scheme that carries its own end
    (:func:`~celpix.plugins.base.self_delimiting`) is chased, and so decoded
    leniently: one without an end decodes any bytes to something and never
    completes, so every window would look cut short.
    """
    size = max(1, probe_bytes)
    if chase is None:
        chase = self_delimiting(plugin, inputs)
    while True:
        try:
            out, ctx = _decode_at(data, plugin, pos, size, inputs, partial=chase)
        except Exception:  # noqa: BLE001 — not a structure here
            return None
        if out and ctx.get(KEY_DECOMPRESS_COMPLETE):
            return out, ctx.get(KEY_COMPRESSED_SIZE) or 0
        if not chase or not out or pos + size >= len(data) or size >= CHASE_LIMIT:
            return None
        if tick is not None and tick():
            return None
        # The last round is clamped rather than skipped, so a structure that
        # ends just inside the limit is still found.
        size = min(size * WINDOW_GROWTH, CHASE_LIMIT)


def find_next_structure(
    data: bytes,
    plugin: CompressionPlugin,
    probe_bytes: int,
    start: int,
    *,
    progress_every: int = 64,
    on_tick: Callable[[int], bool] | None = None,
    inputs: dict[str, bytes | int | str] | None = None,
    alignment: int = 1,
    accept: Callable[[bytes, int], bool] | None = None,
) -> ScanResult:
    """The first offset ≥ ``start`` where ``plugin`` decodes a complete structure.

    Walks ``data`` one byte at a time, asking :func:`structure_at` at each
    offset: a hit is a non-empty decode that the plugin **reports complete**
    (:data:`KEY_DECOMPRESS_COMPLETE`), its own end — terminator or declared
    size — found in the ``probe_bytes`` at the offset or in the wider window a
    structure running past them is chased into.
    Non-empty output alone is not enough, because a scheme with no end to find
    (PackBits, RLE2) decodes *any* bytes to something and never reports
    completion, and a hit on every byte is no scan at all. That is what makes
    the scan meaningless for a non-self-delimiting scheme, and the UI keeps it
    off for those. Every ``progress_every`` bytes ``on_tick(pos)`` is called if
    given, and between the rounds of a chase at ``pos`` too, since one chase
    can take far longer than a batch of offsets; returning True aborts the scan
    (the UI pumps its event loop and reports a Stop there).

    ``alignment`` is the plugin's declared start alignment
    (:attr:`~celpix.plugins.base.PluginInfo.alignment`): only multiples of it
    are probed, counted from ``data[0]``, so the caller hands in a buffer whose
    start *is* aligned — a whole ROM image, for the one scheme that declares it.

    ``accept(output, consumed)`` is a further test a complete structure has to
    pass to count — the smart scan's :func:`looks_like_graphics` — and the
    walk simply moves on past one that fails it. That is the one place the
    scan applies a *heuristic*: everything above is the format's own word on
    where a structure is.

    ``inputs`` is what a scheme that needs a table declares
    (:class:`~celpix.plugins.base.InputSpec`), already resolved: "find the next
    stream that decodes with *this* table", which is the question a shared-table
    format asks. Handed to every probe on a fresh context, so a hit means the
    structure decodes with those inputs and nothing else.
    """
    alignment = max(1, alignment)
    pos = -(-start // alignment) * alignment  # first aligned offset >= start
    n = len(data)
    chase = self_delimiting(plugin, inputs)
    stopped = False

    def chase_tick() -> bool:
        # Records the stop, because the chase reports it only as "no structure".
        nonlocal stopped
        stopped = on_tick is not None and bool(on_tick(pos))
        return stopped

    while pos < n:
        found = structure_at(
            data,
            plugin,
            pos,
            probe_bytes,
            inputs=inputs,
            chase=chase,
            tick=chase_tick,
        )
        if stopped:
            return ScanResult(None, pos, True)
        if found is not None and (accept is None or accept(*found)):
            return ScanResult(pos, pos, False)
        pos += alignment
        if on_tick is not None and pos % progress_every == 0 and on_tick(pos):
            return ScanResult(None, pos, True)
    return ScanResult(None, pos, False)


# The smart scan's plausibility rule, three thresholds measured rather than
# guessed (``docs/rom-mapping/finding-data.md`` §1.3): over 575 known graphics
# streams in four cartridges (SMW and Zelda 3 LZ, Yoshi's Island GBA LZ77, Alex
# Kidd RLE) every one passes all three, while 97-99% of the complete decodes the
# plain scan lands on fail at least one.
#
# Fewer compressed bytes than this is a single fill or literal command with its
# terminator — a 3-byte "structure" that random bytes form every few dozen
# offsets. The smallest real stream measured is 12 bytes.
MIN_COMPRESSED = 8
# Beyond this ratio the "structure" is one command producing hundreds of bytes
# of one value. Real art tops out well under it (31:1 for a nearly blank tile
# set, 8:1 elsewhere); an all-blank bank compresses 5:1 in the RLE that hit 31.
MAX_RATIO = 32


def looks_like_graphics(output: bytes, consumed: int, bytes_per_tile: int) -> bool:
    """Whether a complete structure plausibly holds tiles of the current format.

    The heuristic half of the smart scan, deliberately small: the output is a
    whole number of tiles under the view's pixel preset and at least one, the
    structure is more than a lone command, and it did not expand beyond what
    graphics ever compress to. Anything about the *pixels* — how often
    neighbours match, how many colours a tile uses, whether the palette's
    colours sit close together — was measured and left out: it cost real
    streams (a dithered Mode 7 backdrop, a 16-colour test sheet) for a few
    fewer false hits, and the palette gave no separation at all.
    """
    if bytes_per_tile <= 0 or len(output) < bytes_per_tile:
        return False
    if len(output) % bytes_per_tile:
        return False
    if consumed < MIN_COMPRESSED:
        return False
    return len(output) <= MAX_RATIO * consumed
