"""Container plugins: iNES header skip, Sega ``.smd`` and SNES interleaved-image
deinterleave, and the copier header in front of a SNES dump.

All transform the bytes the host hands them, like
:class:`~celpix.plugins.builtins.raw_file.RawFileContainer` but unwrapping framing
on the way, so the pixel codec downstream sees contiguous tile data
(``docs/graphics-formats-reference/implementation-guide.md`` §5). They record where
the payload starts into the context like the raw container does.

Every one of them implements ``write`` too, and must: each ``read`` ignores
``source.offset`` and works out its own start from the format, so the offset the
host carries alongside describes addressing *within* the unwrapped bytes and says
nothing about where the wrapped ones belong. Writing through plain bytes at that
offset therefore does not restore the file, it destroys it — the deinterleaving
containers would lay an unwrapped image over a wrapped one, and iNES would drop a
bare CHR fragment at the wrong place and truncate the ROM to it. Nothing has to
declare that: a container with no ``write`` is simply not written through at all
(:func:`~celpix.plugins.detect.container_write_enabled`).

So ``write`` here **recomputes its own destination from the destination's current
bytes** — the same rule ``read`` used, factored into a shared helper so the two
cannot drift — and preserves everything it did not decode: the copier or iNES
header, the PRG banks, and any tail the read dropped for being less than a whole
block. Both halves living on one class is what makes "the same rule" checkable by
reading a single object.
"""

from __future__ import annotations

from celpix.core.context import KEY_SOURCE_OFFSET, PipelineContext
from celpix.core.errors import Stage
from celpix.core.notices import warn
from celpix.plugins.base import PluginInfo, ReadSource, WriteTarget, splice

_INES_MAGIC = b"NES\x1a"


def ines_chr_span(raw: bytes) -> tuple[int, int | None]:
    """``(start, length)`` of the CHR ROM in an iNES image; length None = to end.

    The 16-byte header (plus a 512-byte trainer when flagged) is followed by the
    PRG banks, and the CHR ROM after those. A cart with **CHR-RAM** declares zero
    CHR banks and so has no CHR ROM at all; the bytes past the header are handed
    over instead, which is the best a graphics editor can offer for one.

    Shared by both directions so the two cannot disagree about where the graphics
    live — a drift there would splice edited tiles over the program.
    """
    header_end = 16 + (512 if raw[6] & 0x04 else 0)
    prg_banks, chr_banks = raw[4], raw[5]
    if chr_banks == 0:
        return header_end, None
    return header_end + prg_banks * 16384, chr_banks * 8192


class INesContainer:
    """A ``.nes`` file, read past the iNES header to the CHR ROM and back.

    If bytes 0–3 are ``NES\\x1a``, the 16-byte header (plus a 512-byte trainer when
    present) is skipped; the CHR ROM starts after the PRG banks. When the cart uses
    CHR-RAM (0 CHR banks) there is no CHR ROM, so the bytes after the header are
    returned. A file without the magic is read like a plain binary.

    Writing needs to exist for the reason a header skip alone would not: the read
    starts past the PRG banks, an offset the host never learns, so a plain write
    would put the CHR fragment at the wrong place and truncate the ROM to it. The
    CHR start is recomputed from the destination's own header
    (:func:`ines_chr_span`) rather than trusted from ``dest``. A destination that is
    not an iNES image is written plainly at ``dest.offset``, matching the read's own
    fallback for a file without the magic.
    """

    info = PluginInfo(
        id="container.ines",
        name="iNES file (auto-skip header)",
        stage=Stage.CONTAINER,
        extensions=(".nes",),
        magic=((0, _INES_MAGIC),),
        short_name="iNES",
    )

    def read(self, source: ReadSource, ctx: PipelineContext) -> bytes:
        raw = source.data
        if raw[:4] == _INES_MAGIC and len(raw) >= 16:
            start, length = ines_chr_span(raw)
            ctx.set(KEY_SOURCE_OFFSET, start)
            if length is None:
                warn(
                    ctx,
                    "CHR-RAM cart: no tile data in this file",
                    "The header declares 0 CHR banks, so the cartridge\n"
                    "generates its tiles at runtime and the ROM holds none.\n"
                    "Showing the bytes after the header, which are program\n"
                    "code rather than graphics.",
                    self.info.id,
                )
            return raw[start:] if length is None else raw[start : start + length]
        # Not an iNES file — behave like the raw container.
        warn(
            ctx,
            "Not an iNES file: read as plain bytes",
            "The first four bytes are not NES\\x1a, so there is no\n"
            "header to skip and no CHR ROM to find. Nothing was\n"
            "changed; the whole file is shown as-is.",
            self.info.id,
        )
        ctx.set(KEY_SOURCE_OFFSET, source.offset)
        return source.window()

    def write(self, data: bytes, dest: WriteTarget, ctx: PipelineContext) -> bytes:
        raw = dest.existing
        if raw[:4] == _INES_MAGIC and len(raw) >= 16:
            return splice(raw, ines_chr_span(raw)[0], data)
        return splice(raw, dest.offset, data)


class SmdContainer:
    """A Sega ``.smd`` (Genesis) file, deinterleaved to contiguous ROM bytes.

    ``.smd`` has a 512-byte header, then 16 KB blocks storing all the odd bytes
    first, then all the even bytes. Each block is reconstructed by interleaving the
    two halves back together. Plain ``.md``/``.bin`` need no transform (use the raw
    container); this one always deinterleaves.

    Writing is the exact inverse, and block-local like the read: each 16 KB block
    gets its own odd-bytes-then-even-bytes split, so the halves never reach across a
    block boundary. It is written as two strided assignments per block rather than a
    loop over 8192 byte pairs — a Mega Drive image is megabytes and the per-byte
    form would be seconds of Python. The 512-byte header is left exactly as it was:
    it is copier metadata this container never decoded, so it is not ours to
    regenerate. A trailing partial block — which the read drops — is likewise left
    alone rather than truncated away.
    """

    # Suffix only: the 512-byte header carries no marker this container can assert
    # on, so the name is the whole of what identifies a .smd.
    info = PluginInfo(
        id="container.smd",
        name="Sega .smd (deinterleave)",
        stage=Stage.CONTAINER,
        extensions=(".smd",),
        short_name="SMD",
        preserves_offsets=False,
    )

    _HEADER = 512
    _BLOCK = 16384
    _HALF = 8192

    def read(self, source: ReadSource, ctx: PipelineContext) -> bytes:
        ctx.set(KEY_SOURCE_OFFSET, self._HEADER)
        body = source.data[self._HEADER :]
        blocks = len(body) // self._BLOCK
        tail = len(body) - blocks * self._BLOCK
        if tail:
            warn(
                ctx,
                f"Dropped {tail} trailing byte(s): not a whole 16 KB block",
                "The odd/even split is per 16 KB block, so a partial one\n"
                "at the end cannot be reassembled. Those bytes are not\n"
                "shown here, and a save leaves them exactly as they are.",
                self.info.id,
            )
        if not blocks:
            warn(
                ctx,
                "No complete 16 KB block: nothing to show",
                "After the 512-byte header this file has less than one\n"
                "whole block, so there is nothing the deinterleaver can\n"
                "reassemble. It may not be a .smd at all.",
                self.info.id,
            )
        out = bytearray(blocks * self._BLOCK)
        for i in range(blocks):
            src = i * self._BLOCK
            dst = i * self._BLOCK
            for j in range(self._HALF):
                out[dst + j * 2 + 1] = body[src + j]  # first half → odd positions
                out[dst + j * 2] = body[src + self._HALF + j]  # second half → even
        return bytes(out)

    def write(self, data: bytes, dest: WriteTarget, ctx: PipelineContext) -> bytes:
        block, half = self._BLOCK, self._HALF
        blocks = len(data) // block
        body = bytearray(blocks * block)
        for i in range(blocks):
            at = i * block
            body[at : at + half] = data[at + 1 : at + block : 2]  # odd → first half
            body[at + half : at + block] = data[at : at + block : 2]  # even → second
        return splice(dest.existing, self._HEADER, bytes(body))


# The copier header every 1980s-90s cartridge duplicator prepended, and the only
# rule that spots one: carts are a whole number of KiB, so a ROM that is exactly
# 512 bytes over has a header on the front. Expressed once here, used both as a
# constant and — via COPIER_SIZE_RULE — as the detection signature.
COPIER_HEADER = 512
COPIER_SIZE_RULE = (1024, COPIER_HEADER)
# ...and the floor that keeps the rule from claiming things that merely share the
# arithmetic. celPix is regularly pointed at small extracted tile sheets, and the
# ``*.4bpp.sfc`` convention means plenty of them are named like ROMs; a 512-byte
# one is "512 over zero KiB" and would otherwise be opened as a header with no
# cartridge behind it. No real cart is anywhere near this small, and no tile sheet
# is anywhere near this big, so 32 KiB separates the two cleanly.
COPIER_MIN_SIZE = COPIER_HEADER + 0x8000


def snes_copier_header_len(size: int) -> int:
    """0 or 512 by :data:`COPIER_SIZE_RULE`, for a container that decides itself.

    Shared by both directions so they cannot disagree about where the image starts —
    which would put the write's bytes half a kilobyte away from the read's.
    """
    modulus, remainder = COPIER_SIZE_RULE
    return COPIER_HEADER if size % modulus == remainder else 0


class CopierHeaderContainer:
    """A ROM behind a 512-byte copier header, read past it and written behind it.

    The plain case of a headered dump: no interleave, no byte-order quirk, just
    512 bytes of duplicator metadata in front of the cartridge image. Skipping it
    is what makes every published ROM offset line up, since those are all quoted
    against the cart rather than the file. The header itself is metadata this
    container never decoded, so a save preserves it rather than regenerating it —
    the file stays the dump it was.

    Detection is by suffix **and** size (:data:`COPIER_SIZE_RULE`, floored at
    :data:`COPIER_MIN_SIZE`) — the header carries no marker worth asserting on, so
    the giveaway is the file being 512 bytes over a whole number of KiB, on
    something big enough to be a cartridge. Restricting it to the copier suffixes
    keeps the size rule from seizing arbitrary binaries of the right length, and
    the floor keeps it off the small ``*.4bpp.sfc`` tile sheets celPix is just as
    often opening; a headered dump that misses either is picked by hand.
    """

    info = PluginInfo(
        id="container.copier-header",
        name="Copier header (skip 512 bytes)",
        stage=Stage.CONTAINER,
        extensions=(".smc", ".swc", ".fig", ".sfc"),
        size_modulo=COPIER_SIZE_RULE,
        min_size=COPIER_MIN_SIZE,
        short_name="Header",
    )

    def read(self, source: ReadSource, ctx: PipelineContext) -> bytes:
        raw = source.data
        ctx.set(KEY_SOURCE_OFFSET, COPIER_HEADER)
        # Detection would not have chosen this container for such a file, so
        # reaching here means it was picked by hand — possibly by mistake, and
        # the cost is 512 real bytes of the image silently going missing.
        if not snes_copier_header_len(len(raw)):
            warn(
                ctx,
                "This file does not look headered",
                "A copier header leaves the file 512 bytes over a whole\n"
                "number of KiB, and this one is not. Skipping 512 bytes\n"
                "anyway removes real data from the front of the image.",
                self.info.id,
            )
        return raw[COPIER_HEADER:]

    def write(self, data: bytes, dest: WriteTarget, ctx: PipelineContext) -> bytes:
        return splice(dest.existing, COPIER_HEADER, data)


class SnesInterleavedContainer:
    """An interleaved SNES HiROM image, restored to contiguous ROM bytes.

    Game Doctor / Super UFO copiers stored HiROM images with the upper 32 KB
    half of every 64 KB bank first, then all the lower halves — which is what
    put the internal header at the LoROM-style file offset 0x7Fxx (see
    ``docs/graphics-formats-reference/snes-hardware-notes.md``). A 512-byte
    copier header is skipped first when present; carts are always a whole
    number of KiB, so a header is present iff ``size % 1024 == 512``. LoROM
    images were never interleaved. Like the ``.smd`` container this always
    deinterleaves — use it only on images known to be interleaved.

    Writing is the exact inverse: where the ``.smd`` split is per block, this one
    is *global* — every bank's upper half goes to the first region of the file and
    every lower half to the second, which is what put the internal header at the
    LoROM-style offset in the first place. The copier header is detected the same
    way the read detects it (:func:`snes_copier_header_len`) and preserved, as is
    any tail past the whole banks the read consumed.
    """

    # Deliberately unsignatured, so it is never auto-detected: `.sfc`/`.smc` say
    # nothing about interleaving and an interleaved image carries no marker.
    # Deinterleaving a plain image scrambles it, so this one is picked by hand.
    info = PluginInfo(
        id="container.snes-interleaved",
        name="SNES interleaved ROM (deinterleave)",
        stage=Stage.CONTAINER,
        short_name="Interleaved",
        preserves_offsets=False,
    )

    _HALF = 0x8000  # half of a 64 KB HiROM bank

    def read(self, source: ReadSource, ctx: PipelineContext) -> bytes:
        raw = source.data
        header = snes_copier_header_len(len(raw))
        ctx.set(KEY_SOURCE_OFFSET, header)
        body = raw[header:]
        banks = len(body) // (2 * self._HALF)
        tail = len(body) - banks * 2 * self._HALF
        if tail:
            warn(
                ctx,
                f"Dropped {tail} trailing byte(s): not a whole 64 KB bank",
                "The upper/lower split is defined across whole banks, so a\n"
                "partial one at the end cannot be placed. Those bytes are\n"
                "not shown, and a save leaves them exactly as they are.",
                self.info.id,
            )
        if not banks:
            warn(
                ctx,
                "No complete 64 KB bank: nothing to show",
                "This file is smaller than one bank, so there is nothing\n"
                "to deinterleave. It is probably not an interleaved image.",
                self.info.id,
            )
        lowers = banks * self._HALF  # the lower-half region starts here
        out = bytearray()
        for i in range(banks):
            lo = body[lowers + i * self._HALF : lowers + (i + 1) * self._HALF]
            hi = body[i * self._HALF : (i + 1) * self._HALF]
            out += lo + hi
        return bytes(out)

    def write(self, data: bytes, dest: WriteTarget, ctx: PipelineContext) -> bytes:
        half = self._HALF
        bank = 2 * half
        banks = len(data) // bank
        body = bytearray(banks * bank)
        lowers = banks * half
        for i in range(banks):
            src = i * bank
            body[i * half : (i + 1) * half] = data[src + half : src + bank]
            body[lowers + i * half : lowers + (i + 1) * half] = data[src : src + half]
        header = snes_copier_header_len(len(dest.existing))
        return splice(dest.existing, header, bytes(body))
