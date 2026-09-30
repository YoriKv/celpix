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
rounds its source pointer up after each one, while Kosinski+, LZKN1, Comper and
ComperX pack them end to end. So a moduled plugin is written once, by
:func:`moduled_plugin`, from the inner plugin and those two facts.

The module count comes from the size, not from a marker. So a module that
decodes to anything but its share is corrupt rather than merely odd: the loader
DMAs 0x1000 bytes per module whatever the stream produced.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import cast

from celpix.core.errors import Stage
from celpix.plugins.base import PartialDecompression, PluginInfo

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
    size_alias: Mapping[int, int] | None = None,
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
    size_alias: Mapping[int, int] | None = None,
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


@dataclass(frozen=True)
class Moduled:
    """One moduled format: an inner codec bound to its padding and size quirks.

    :meth:`decompress` and :meth:`compress` are what a codec module publishes as
    its ``decompress_moduled``/``compress_moduled``, so the framing can be called
    without going through a plugin instance.
    """

    decode: Decoder
    encode: Callable[[bytes], bytes]
    padding: int
    name: str
    size_alias: Mapping[int, int] | None = None

    def decompress(
        self, data: bytes, *, partial: bool = False
    ) -> tuple[bytes, int, bool]:
        """Unpack a size header and the 4 KiB modules behind it."""
        return decompress(
            data,
            self.decode,
            padding=self.padding,
            name=self.name,
            partial=partial,
            size_alias=self.size_alias,
        )

    def compress(self, data: bytes) -> bytes:
        """Encode ``data`` as 4 KiB modules behind a size header."""
        return compress(
            data,
            self.encode,
            padding=self.padding,
            name=self.name,
            size_alias=self.size_alias,
        )


def moduled_plugin(
    inner: type[PartialDecompression],
    label: str,
    *,
    padding: int = 1,
    size_alias: Mapping[int, int] | None = None,
) -> tuple[type[PartialDecompression], Moduled]:
    """The moduled plugin over ``inner``, and the :class:`Moduled` behind it.

    Everything a moduled plugin states follows from the inner one: its id gains
    ``-moduled``, its name and error prefix are ``label``'s, and its category is
    the inner's. It is always self-delimiting — the header's size fixes how many
    modules follow and each ends on its own marker, so the last one's end is the
    structure's.

    The inner codec is reached through an instance rather than the class so a
    variant that binds a parameter in an ordinary ``_decode`` method (ComperX's
    word form) frames the same way as one whose ``_decode`` is a staticmethod.
    """
    codec = inner()
    moduled = Moduled(
        codec._decode,
        codec._encode,
        padding,
        f"{label} moduled",
        size_alias,
    )
    name = inner.__name__.removesuffix("Compression") + "ModuledCompression"
    plugin = cast(
        "type[PartialDecompression]",
        type(
            name,
            (PartialDecompression,),
            {
                "__module__": inner.__module__,
                "__qualname__": name,
                "__doc__": f"{label} in 4 KiB modules behind a size header.",
                "info": PluginInfo(
                    id=f"{inner.info.id}-moduled",
                    name=f"{label}, moduled (4 KiB streams behind a size header)",
                    stage=Stage.COMPRESSION,
                    self_delimiting=True,
                    category=inner.info.category,
                ),
                "_decode": staticmethod(moduled.decompress),
                "_encode": staticmethod(moduled.compress),
            },
        ),
    )
    return plugin, moduled
