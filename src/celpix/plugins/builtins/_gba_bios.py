"""The four-byte header every GBA/NDS BIOS decompression call reads, once.

LZ77, RLE, Huffman and the diff filter differ entirely in their bodies and not at
all in their framing: a type byte whose high nibble names the scheme, then the
decompressed size as a 24-bit little-endian count. The BIOS dispatches on that
byte and stops on that count, so each codec here reads and writes it the same way
— and a zero size or an empty payload is refused the same way, because a stream
that declares nothing is four bytes of noise wherever the type byte happens to
land.

What stays with each codec is how it names itself in an error: the tests and a
user read "not an LZ77 header" and "the GBA BIOS diff filter", so the wording is
the caller's and only the checks are shared.
"""

from __future__ import annotations

from collections.abc import Callable, Container

HEADER_SIZE = 4
# The start alignment the BIOS imposes: it reads the header with one 32-bit
# load, which on this CPU rotates rather than faults at an unaligned address,
# so a stream at an unaligned offset decodes to garbage on the console. The
# scan probes only aligned offsets for that reason; a decode is handed a
# buffer, not an address, and cannot check it.
ALIGNMENT = 4

MAX_DECOMPRESSED = 0xFFFFFF  # the header's size field is 24 bits


def read_header(
    data: bytes,
    *,
    types: Container[int],
    what: str,
    fail: Callable[[str], ValueError],
) -> int:
    """The declared decompressed size of the stream ``data`` starts with.

    ``types`` are the type bytes the caller accepts — one for most schemes, one
    per symbol width for Huffman — and ``what`` completes "type byte 0x.. is not
    …" in the caller's own words. Raises ``fail(...)`` for a short buffer, a
    foreign type byte, or a zero size.
    """
    if len(data) < HEADER_SIZE:
        raise fail(f"shorter than the {HEADER_SIZE}-byte header")
    if data[0] not in types:
        raise fail(f"type byte {data[0]:#04x} is not {what}")
    target = int.from_bytes(data[1:HEADER_SIZE], "little")
    if target == 0:
        # Nothing to produce, and accepting it would make four bytes of noise a
        # valid structure wherever the type byte happened to land.
        raise fail("declared decompressed size is zero")
    return target


def write_header(
    type_byte: int, size: int, *, what: str, field: str = "size field"
) -> bytearray:
    """A fresh output buffer holding the header for ``size`` bytes of payload.

    ``what`` names the scheme in the refusals ("… has no encoding for an empty
    payload") and ``field`` the size field ("the 24-bit … holds").
    """
    if size == 0:
        # A zero size is what the decoders refuse, so writing one would save a
        # stream that cannot be opened again.
        raise ValueError(f"{what} has no encoding for an empty payload")
    if size > MAX_DECOMPRESSED:
        raise ValueError(
            f"input is {size:,} bytes; the 24-bit {field} holds {MAX_DECOMPRESSED:,}"
        )
    return bytearray([type_byte]) + size.to_bytes(3, "little")
