"""Scanning a buffer for the next compressed structure.

Owns the forward scan behind **Find Next** (:func:`find_next_structure`): walk a
buffer offset by offset and ask a compression scheme whether a complete stream
starts there — the format's own word on where a structure ends. And the one
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
    KEY_INPUTS,
    KEY_SURROUND,
    KEY_SURROUND_START,
    PipelineContext,
)
from celpix.plugins.base import CompressionPlugin


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

    Walks ``data`` one byte at a time, trying a strict decompress of the
    ``probe_bytes`` compressed bytes at each offset. A hit is a non-empty decode
    that the plugin **reports complete** (:data:`KEY_DECOMPRESS_COMPLETE`): the
    structure's own end — terminator or declared size — landed inside the probe.
    Non-empty output alone is not enough, because a scheme with no end to find
    (PackBits, RLE2) decodes *any* bytes to something and never reports
    completion, and a hit on every byte is no scan at all. That is what makes
    the scan meaningless for a non-self-delimiting scheme, and the UI keeps it
    off for those. Every ``progress_every`` bytes ``on_tick(pos)`` is called if
    given; returning True aborts the scan (the UI pumps its event loop and
    reports a Stop there).

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
    while pos < n:
        ctx = PipelineContext()
        if inputs:
            ctx.set(KEY_INPUTS, inputs)
        # The probe is a window of `data`, and a scheme that copies from the
        # bytes before its stream has to be told where in `data` the window
        # sits, or it would resolve those copies against the window's own start.
        ctx.set(KEY_SURROUND, data)
        ctx.set(KEY_SURROUND_START, pos)
        try:
            out = plugin.decompress(data[pos : pos + probe_bytes], ctx)
            if (
                out
                and ctx.get(KEY_DECOMPRESS_COMPLETE)
                and (accept is None or accept(out, ctx.get(KEY_COMPRESSED_SIZE) or 0))
            ):
                return ScanResult(pos, pos, False)
        except Exception:  # noqa: BLE001 — not a structure here; keep walking
            pass
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
