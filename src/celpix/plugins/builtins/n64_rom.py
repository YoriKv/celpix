"""Nintendo 64 ROM container — byte-order normalisation.

An N64 dump exists in three byte orders, told apart by how the same four header
bytes come out. Nothing else differs: the orders are one ROM seen through copiers
that disagreed about endianness, so *every* documented offset is quoted in the
native one.

| Order | First four bytes | Relation to native |
|---|---|---|
| ``.z64`` big-endian (native) | ``80 37 12 40`` | — |
| ``.v64`` byteswapped | ``37 80 40 12`` | each 2-byte pair reversed |
| ``.n64`` little-endian | ``40 12 37 80`` | each 4-byte word reversed |

Read normalises to native order so tiles decode and offsets mean what the
documentation says; Write restores the order the file arrived in, so a ``.v64``
stays a ``.v64`` and the user's other tools keep reading it.

Both transforms are their own inverse — reversing a group twice restores it —
which is what lets one width describe each direction. The width is carried on the
context rather than re-derived at save time: by then the bytes in hand are
normalised and no longer say which order they came from.

A trailing partial group is left alone, so a truncated dump keeps whatever bytes
it has rather than losing the tail to a group that was never whole.

See ``docs/graphics-formats-reference/implementation-guide.md`` §5.
"""

from __future__ import annotations

from celpix.core.context import KEY_SOURCE_OFFSET, PipelineContext
from celpix.core.errors import Stage
from celpix.core.notices import warn
from celpix.plugins.base import PluginInfo, ReadSource, WriteTarget, splice

# int: group width whose reversal converts this file between its on-disk order
# and native order (2 = .v64, 4 = .n64); 0 = already native. Set by Read so Write
# can restore the order the file arrived in.
KEY_N64_SWAP = "n64.swap-width"

_NATIVE = b"\x80\x37\x12\x40"
_BYTESWAPPED = b"\x37\x80\x40\x12"  # .v64 — 2-byte groups
_LITTLE = b"\x40\x12\x37\x80"  # .n64 — 4-byte groups

# Signature → the width that normalises it. Declared to the host as magic too,
# so a dump in any of the three orders is claimed whatever it is named.
_ORDERS: dict[bytes, int] = {_NATIVE: 0, _BYTESWAPPED: 2, _LITTLE: 4}


def swap_groups(data: bytes, width: int) -> bytes:
    """``data`` with every whole ``width``-byte group reversed (``width`` 0 = as is).

    Written as ``width`` strided slice assignments rather than a loop over the
    groups, so the work happens inside CPython's slicing: an N64 image is tens of
    megabytes, and a per-group Python loop would be seconds of it.
    """
    if width < 2:
        return data
    whole = len(data) - len(data) % width
    out = bytearray(data)
    body = data[:whole]
    for i in range(width):
        out[i:whole:width] = body[width - 1 - i :: width]
    return bytes(out)


def swap_width(head: bytes) -> int:
    """The normalising width for a dump starting with ``head``; 0 if unrecognised."""
    return _ORDERS.get(bytes(head[:4]), 0)


class N64RomContainer:
    info = PluginInfo(
        id="container.n64-rom",
        name="Nintendo 64 ROM (normalise byte order)",
        stage=Stage.CONTAINER,
        extensions=(".z64", ".v64", ".n64"),
        magic=tuple((0, sig) for sig in _ORDERS),
        short_name="N64",
        # A .z64 passes through untouched, but a .v64/.n64 is reordered within
        # every group, so a native-order offset names a different byte in the
        # file. Declared for the whole container, since which of the three a
        # given file is isn't known until it has been read.
        preserves_offsets=False,
    )

    def read(self, source: ReadSource, ctx: PipelineContext) -> bytes:
        raw = source.data
        width = swap_width(raw)
        ctx.set(KEY_N64_SWAP, width)
        if bytes(raw[:4]) not in _ORDERS:
            # Nothing says which order this file is in. Treating it as native
            # leaves it unchanged, the only non-destructive guess available.
            warn(
                ctx,
                "Unrecognised N64 header: assuming native byte order",
                "The first four bytes match none of the three known\n"
                "orders, so the file is read (and written) unswapped.\n"
                "If the tiles look byte-swapped, this is why.",
                self.info.id,
            )
        # Window the *normalised* stream: a byte's position only survives the
        # swap within its own group, so an offset means something only once the
        # file reads in native order — also the order every published N64 offset
        # is quoted in.
        native = swap_groups(raw, width)
        start = source.start
        end = len(native) if source.length is None else start + source.length
        ctx.set(KEY_SOURCE_OFFSET, source.offset)
        return native[start:end]

    def write(self, data: bytes, dest: WriteTarget, ctx: PipelineContext) -> bytes:
        # The context records the order this file was read in. Without it — a
        # write with no prior read — the destination's own bytes still say, and a
        # file that isn't there yet is written native.
        width = ctx.get(KEY_N64_SWAP)
        if width is None:
            width = swap_width(dest.existing[:4])
        # Splice in native order and swap the whole result back rather than
        # splicing into the on-disk order: an offset that is not group-aligned
        # names different bytes in the two orders, and the native one is what the
        # rest of the app has been addressing.
        native = splice(swap_groups(dest.existing, width), dest.offset, data)
        return swap_groups(native, width)
