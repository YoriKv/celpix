"""The Mega Drive's moduled framing: one payload as a run of 4 KiB streams.

A game that streams art into VRAM during play cannot stall for one long
decompression, so the payload is cut into **modules** of 0x1000 bytes and each is
compressed as an independent stream. The loader decompresses one module per
frame (or per spare slice of one) into a 4 KiB buffer and DMAs it out, so no
module ever needs the output of another — each starts with an empty window::

    +0  u16 big-endian   total uncompressed size
    +2  module 0         a complete stream, decoding to 0x1000 bytes
        (padding)        zero bytes up to the next multiple of ``padding``,
                         measured from +2, not from the start of the header
        module 1         ...
        module k         the last, decoding to what is left (1..0x1000 bytes)

What differs between the moduled formats is only the inner codec and the
padding: Kosinski modules sit on 16-byte boundaries, because the original queue
rounds its source pointer up after each one, while the byte-descriptor
Kosinski+ packs them end to end.

The module count comes from the size, not from a marker. So a module that
decodes to anything but its share is corrupt rather than merely odd: the loader
DMAs 0x1000 bytes per module whatever the stream produced.
"""

from __future__ import annotations

from collections.abc import Callable

MODULE_SIZE = 0x1000
HEADER_BYTES = 2
SIZE_LIMIT = 0xFFFF

Decoder = Callable[..., tuple[bytes, int, bool]]


def _modules(total: int) -> int:
    # An empty payload is still one (empty) module: the stream after the header
    # is what the encoders write, and what a decoder has to step over.
    return max(1, -(-total // MODULE_SIZE))


def decompress(
    data: bytes,
    decode: Decoder,
    *,
    padding: int,
    name: str,
    partial: bool = False,
    size_alias: dict[int, int] | None = None,
) -> tuple[bytes, int, bool]:
    """Unpack a moduled payload whose modules ``decode`` reads one at a time.

    Returns ``(plain, consumed, complete)`` like the inner decoders do;
    ``consumed`` ends after the last module's own stream, so the zero padding a
    packer may add to reach an even length is not part of it. ``size_alias``
    maps a header value to the size a loader actually reads for it.
    """

    def fail(reason: str) -> ValueError:
        return ValueError(f"corrupt {name} stream: {reason}")

    if len(data) < HEADER_BYTES:
        raise fail(f"shorter than the {HEADER_BYTES}-byte size header")
    total = int.from_bytes(data[:HEADER_BYTES], "big")
    total = (size_alias or {}).get(total, total)

    out = bytearray()
    pos = HEADER_BYTES
    count = _modules(total)
    for index in range(count):
        if index:
            body = pos - HEADER_BYTES
            pos = HEADER_BYTES + -(-body // padding) * padding
        # A bounded window cut here. Two bytes is the least any inner stream
        # opens with — Kosinski's descriptor word, LZKN1's size header — and
        # those decoders refuse a shorter slice as corrupt rather than reporting
        # it truncated, so the cut has to be recognised before they see it.
        if partial and len(data) - pos < 2:
            return bytes(out), min(pos, len(data)), False
        plain, used, complete = decode(data[pos:], partial=partial)
        pos += used
        share = min(MODULE_SIZE, total - len(out))
        if len(plain) > share or (complete and len(plain) != share):
            raise fail(
                f"module {index} decodes to {len(plain):,} bytes, "
                f"where the size header leaves it {share:,}"
            )
        out += plain
        if not complete:
            return bytes(out), pos, False
    return bytes(out), pos, True


def compress(
    data: bytes,
    encode: Callable[[bytes], bytes],
    *,
    padding: int,
    name: str,
    size_alias: dict[int, int] | None = None,
) -> bytes:
    """Cut ``data`` into modules, compress each, and frame them.

    A size in ``size_alias`` is refused: the loader would read its header as the
    other size and stop short.
    """
    total = len(data)
    if total > SIZE_LIMIT:
        raise ValueError(
            f"input is {total:,} bytes; the {name} size header holds {SIZE_LIMIT:,}"
        )
    if size_alias and total in size_alias:
        raise ValueError(
            f"the {name} loader reads a size of 0x{total:X} as "
            f"0x{size_alias[total]:X}, so a payload of exactly that size "
            "cannot be framed"
        )
    out = bytearray(total.to_bytes(HEADER_BYTES, "big"))
    for index in range(_modules(total)):
        if index:
            body = len(out) - HEADER_BYTES
            out += bytes(-body % padding)
        out += encode(data[index * MODULE_SIZE : (index + 1) * MODULE_SIZE])
    return bytes(out)
