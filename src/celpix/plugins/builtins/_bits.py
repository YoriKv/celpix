"""Byte-parallel primitives the tile codecs decode and encode through.

Every codec is a per-pixel bit shuffle, and a per-pixel Python loop costs a few
hundred nanoseconds a pixel — the difference between a view that repaints
instantly and one that stalls for a quarter of a second on a full-screen window.
All these shuffles are **byte-wise and position-independent**: what a source byte
contributes depends only on its value, never on where in the buffer it sits. So
each is a 256-entry table applied to a whole strided slice at once, with several
source bytes' contributions combined by OR — both running in C over the entire
buffer.

- :func:`or_bytes` / :func:`or_all` — byte-wise OR of equal-length buffers, as
  one big-integer OR (the widest the interpreter offers).
- :func:`bit_expansion` / :func:`bit_packing` — the planar kernel both ways: one
  plane byte to the eight pixels its bits land in, and back.
- :func:`field_expansion` / :func:`field_packing` — the packed kernel both ways:
  one byte to the sub-byte index fields inside it, and back.
- :func:`nibble_plane_expansion` / :func:`nibble_plane_packing` — the
  nibble-planar kernel: one byte carrying *two* bitplanes of four pixels, both
  ways.
The mask-based colour kernel has the same shape and lives with the rest of its
maths in :mod:`celpix.plugins.builtins._mask`. Tables are cached: a view refresh
re-enters the codec for every window it draws, and the parameters rarely change.
"""

from __future__ import annotations

from functools import cache


@cache
def bit_expansion(plane: int) -> tuple[bytes, ...]:
    """One plane byte → the eight pixels it contributes bit ``plane`` to.

    The planar kernel's ``index[x] |= ((byte >> (7 - x)) & 1) << plane`` for all
    eight pixels at once: entry *v* is the eight-byte row plane byte *v*
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
    to its row's plane-``plane`` byte. Packing needs one table per column, where
    expanding needs only one in total, because the destination *bit* is the pixel's
    position — so a plane byte is the OR of eight tables.
    """
    return bytes(((value >> plane) & 1) << (7 - x) for value in range(256))


def _field_shift(pos: int, pixels_per_byte: int, bpp: int, msb_first: bool) -> int:
    """Bit shift of the packed field holding pixel ``pos`` of a byte."""
    return ((pixels_per_byte - 1 - pos) if msb_first else pos) * bpp


@cache
def field_expansion(
    pixels_per_byte: int, bpp: int, msb_first: bool, dest_shift: int = 0
) -> tuple[bytes, ...]:
    """One packed byte → the ``pixels_per_byte`` indices packed into it.

    The packed kernel's sub-byte fields, left-to-right. ``msb_first`` puts pixel 0
    in the byte's high field (Genesis/MSX, Neo Geo Pocket); otherwise the low one
    (GBA, Virtual Boy).

    ``dest_shift`` lifts each field into the *top* half of a wider index, for
    formats whose index is assembled from two bytes: the byte carrying the high
    half expands through a shifted table and the low half's through a plain one,
    so the two combine with an ordinary :func:`or_bytes`.
    """
    mask = (1 << bpp) - 1
    shifts = [
        _field_shift(pos, pixels_per_byte, bpp, msb_first)
        for pos in range(pixels_per_byte)
    ]
    return tuple(
        bytes(((value >> shift) & mask) << dest_shift for shift in shifts)
        for value in range(256)
    )


def _nibble_planes(bpp: int, group_byte: int) -> tuple[int, int]:
    """The two index bits byte ``group_byte`` of a nibble-planar group carries.

    Bytes are ordered most-significant plane pair first, so byte 0 holds the top
    two bits of the index and each further byte drops two more.
    """
    return bpp - 1 - 2 * group_byte, bpp - 2 - 2 * group_byte


@cache
def nibble_plane_expansion(bpp: int, group_byte: int) -> tuple[bytes, ...]:
    """One nibble-planar byte → the four pixels its two bitplanes contribute to.

    The byte's **high nibble is the more significant plane** and its low nibble the
    less significant one, bit 3 of a nibble being the leftmost of the four pixels.
    Entry *v* is the four-byte pixel run byte value *v* contributes, so a group's
    pixels are the OR of one lookup per byte of the group.
    """
    hi, lo = _nibble_planes(bpp, group_byte)
    return tuple(
        bytes(
            (((value >> (7 - pos)) & 1) << hi) | (((value >> (3 - pos)) & 1) << lo)
            for pos in range(4)
        )
        for value in range(256)
    )


@cache
def nibble_plane_packing(bpp: int, group_byte: int, pos: int) -> bytes:
    """The inverse of :func:`nibble_plane_expansion` for one of the four pixels.

    A 256-byte ``bytes.translate`` table: what the index at pixel ``pos`` of the
    group contributes to that group byte. One table per position, the destination
    *bit* within each nibble being the pixel's position, so a group byte is the OR
    of four of these.
    """
    hi, lo = _nibble_planes(bpp, group_byte)
    return bytes(
        (((value >> hi) & 1) << (7 - pos)) | (((value >> lo) & 1) << (3 - pos))
        for value in range(256)
    )


@cache
def field_packing(
    pos: int,
    pixels_per_byte: int,
    bpp: int,
    msb_first: bool,
    source_shift: int = 0,
    source_mask: int = 0xFF,
) -> bytes:
    """The inverse of :func:`field_expansion` for one pixel of a packed byte.

    A 256-byte ``bytes.translate`` table placing an index in the field pixel
    ``pos`` occupies, so a packed byte is the OR of ``pixels_per_byte`` of these.
    An index wider than the field overflows into the neighbouring one, which is
    what the format does rather than something to guard against.

    ``source_shift`` and ``source_mask`` take *part* of the index instead of all of
    it — the inverse of :func:`field_expansion`'s ``dest_shift``, for an index
    split across two bytes. Masking is opt-in so the overflow above stays default.
    """
    shift = _field_shift(pos, pixels_per_byte, bpp, msb_first)
    return bytes(
        (((value >> source_shift) & source_mask) << shift) & 0xFF
        for value in range(256)
    )
