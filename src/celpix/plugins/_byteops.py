"""Whole-buffer byte arithmetic, shared by the codec kernels and the reshapes.

A leaf module on purpose. Both halves of the plugin package want the same
byte-wise OR — the codec kernels reassembling a pixel from its planes
(:mod:`celpix.plugins.builtins._bits`) and the data-LUT reshape reassembling a
unit from its lanes (:mod:`celpix.plugins.data_lut`) — but they cannot reach each
other: ``data_lut`` importing ``builtins._bits`` runs ``builtins/__init__``, which
imports ``discovery``, which imports ``data_lut``. Importing nothing from celpix
is what lets both sides import this instead.

Qt-free, like the rest of the model side.
"""

from __future__ import annotations


def or_bytes(a: bytes, b: bytes) -> bytes:
    """Byte-wise OR of two equal-length buffers."""
    return or_all([a, b])


def or_all(buffers: list[bytes]) -> bytes:
    """Byte-wise OR of equal-length buffers (empty list → ``b""``).

    One arbitrary-precision OR per buffer rather than one per byte: CPython's
    big-integer OR is a word-at-a-time loop in C, where the obvious comprehension
    would be a Python-level call for every byte in the picture. The accumulator
    stays an ``int`` across the whole run, so each buffer is converted once and
    the round trip back to bytes is paid once at the end.

    Big-endian throughout, which makes byte *i* of the result byte *i* of every
    input OR-ed together: the conversion is a formality to borrow C's loop, not a
    reading of the bytes as a number, and the direction only has to be the same
    on the way in and the way out.

    What callers OR together varies — bitplanes, the per-pixel field
    contributions inside one packed byte, the component bytes of an ARGB pixel,
    a substituted unit's lanes — so this is about buffers rather than any one of
    them.
    """
    if not buffers:
        return b""
    merged = 0
    for buffer in buffers:
        merged |= int.from_bytes(buffer, "big")
    return merged.to_bytes(len(buffers[0]), "big")
