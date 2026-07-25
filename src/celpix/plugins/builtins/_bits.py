"""Byte-parallel primitives the tile codecs decode and encode through.

Every codec here is a per-pixel bit shuffle, and written as a per-pixel Python
loop each one costs a few hundred nanoseconds a pixel — which is the difference
between a view that repaints instantly and one that stalls for a quarter of a
second on a full-screen window of a large file. The way out is that all of these
shuffles are **byte-wise and position-independent**: what a source byte
contributes to the output depends only on its value, never on where in the
buffer it sits. So each one can be expressed as a 256-entry table applied to a
whole strided slice at once, with the contributions of several source bytes
combined by OR — and both of those run in C over the entire buffer rather than
in Python per pixel.

The primitives are:

- :func:`or_bytes` / :func:`merge_planes` — a byte-wise OR of equal-length
  buffers, done as one big-integer OR (the widest OR the interpreter offers).
- :func:`bit_expansion` / :func:`bit_packing` — the planar kernel both ways: one
  plane byte to the eight pixels its bits land in, and back.
- :func:`field_expansion` / :func:`field_packing` — the packed kernel both ways:
  one byte to the sub-byte index fields inside it, and back.
- :func:`expand_row` / :func:`pack_row` — the planar kernel applied to a *single*
  eight-pixel row, for the wide/odd tiles whose bytes are too scattered for a
  strided slice to gather. They go through the tables above rather than their own
  loop, so the ``7 - x`` rule that says which bit is which pixel is written once.

The mask-based colour kernel has the same shape and lives with the rest of its
maths in :mod:`celpix.plugins.builtins._mask`.

The tables are cached, since a view refresh re-enters the codec for every window
it draws and the parameters rarely change.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from functools import cache


def or_bytes(a: bytes, b: bytes) -> bytes:
    """Byte-wise OR of two equal-length buffers.

    Via ``int``: an arbitrary-precision OR is a single word-at-a-time loop in C,
    where the obvious comprehension would be a Python call per byte. Big-endian
    both ways, so byte *i* of the result is byte *i* of each input OR-ed — the
    conversion is a formality, not an interpretation of the bytes as a number.
    """
    n = len(a)
    return (int.from_bytes(a, "big") | int.from_bytes(b, "big")).to_bytes(n, "big")


def merge_planes(planes: list[bytes]) -> bytes:
    """OR a list of equal-length buffers together (empty list → ``b""``)."""
    if not planes:
        return b""
    merged = planes[0]
    for plane in planes[1:]:
        merged = or_bytes(merged, plane)
    return merged


@cache
def bit_expansion(plane: int) -> tuple[bytes, ...]:
    """One plane byte → the eight pixels it contributes bit ``plane`` to.

    The planar kernel's ``index[x] |= ((byte >> (7 - x)) & 1) << plane``, done for
    all eight pixels at once: entry *v* is the eight-byte row that plane byte *v*
    contributes, so a whole tile row is the OR of one lookup per plane.
    """
    return tuple(
        bytes(((value >> (7 - x)) & 1) << plane for x in range(8))
        for value in range(256)
    )


@cache
def bit_packing(plane: int, x: int) -> bytes:
    """The inverse of :func:`bit_expansion` for one pixel column of a row.

    A 256-byte ``bytes.translate`` table: what the index at pixel ``x`` contributes
    to its row's plane-``plane`` byte. Packing needs one table per column, not the
    single table expanding does, because the destination *bit* is the pixel's
    position — so a plane byte is the OR of eight tables, one per column.
    """
    return bytes(((value >> plane) & 1) << (7 - x) for value in range(256))


def expand_row(plane_bytes: Iterable[int]) -> bytes:
    """The eight pixels one byte per plane decodes to (byte *k* carries bit *k*).

    The row-at-a-time form of the planar kernel, for the wide/odd tiles: their
    planes sit at format-specific offsets rather than a fixed stride, so the
    buffer-wide walk the 8×8 engine uses has nothing regular to slice along, but
    the kernel inside one row is the same one.
    """
    return merge_planes(
        [bit_expansion(plane)[byte] for plane, byte in enumerate(plane_bytes)]
    )


def pack_row(pixels: Sequence[int], plane: int) -> int:
    """The inverse of :func:`expand_row` for one plane: eight pixels to one byte."""
    byte = 0
    for x in range(8):
        byte |= bit_packing(plane, x)[pixels[x]]
    return byte


def _field_shift(pos: int, pixels_per_byte: int, bpp: int, msb_first: bool) -> int:
    """Bit shift of the packed field holding pixel ``pos`` of a byte."""
    return ((pixels_per_byte - 1 - pos) if msb_first else pos) * bpp


@cache
def field_expansion(
    pixels_per_byte: int, bpp: int, msb_first: bool
) -> tuple[bytes, ...]:
    """One packed byte → the ``pixels_per_byte`` indices packed into it.

    The packed kernel's sub-byte fields, left-to-right. ``msb_first`` puts pixel 0
    in the byte's high field (Genesis/MSX, Neo Geo Pocket); otherwise it is the
    low one (GBA, Virtual Boy).
    """
    mask = (1 << bpp) - 1
    shifts = [
        _field_shift(pos, pixels_per_byte, bpp, msb_first)
        for pos in range(pixels_per_byte)
    ]
    return tuple(
        bytes((value >> shift) & mask for shift in shifts) for value in range(256)
    )


@cache
def field_packing(pos: int, pixels_per_byte: int, bpp: int, msb_first: bool) -> bytes:
    """The inverse of :func:`field_expansion` for one pixel of a packed byte.

    A 256-byte ``bytes.translate`` table placing an index in the field pixel
    ``pos`` occupies, so a packed byte is the OR of ``pixels_per_byte`` of these.
    An index wider than the field overflows into the neighbouring one, which is
    the packing this format has always done rather than something to guard.
    """
    shift = _field_shift(pos, pixels_per_byte, bpp, msb_first)
    return bytes((value << shift) & 0xFF for value in range(256))
