"""Container plugins: iNES header skip, Sega ``.smd`` and SNES interleaved-image
deinterleave, and the copier header in front of a SNES dump.

All transform the bytes the host hands them, like
:class:`~celpix.plugins.builtins.raw_file.RawFileContainer` but unwrapping framing
on the way, so the pixel codec downstream sees contiguous tile data
(``docs/graphics-formats-reference/implementation-guide.md`` §5). Each records
where the payload starts on the context.

Every one implements ``write``, and must: each ``read`` ignores ``source.offset``
and works out its own start from the format, so the offset the host carries
alongside addresses the *unwrapped* bytes and says nothing about where the wrapped
ones belong. Writing plain bytes at that offset would destroy the file — the
deinterleaving containers would lay an unwrapped image over a wrapped one, and
iNES would drop a bare CHR fragment at the wrong place and truncate the ROM to it.

So ``write`` **recomputes its destination from the destination's current bytes**,
by the same rule ``read`` used and through a shared helper so the two cannot
drift, and preserves everything it did not decode: the copier or iNES header, the
PRG banks, and any tail the read dropped for being less than a whole block.
"""

from __future__ import annotations

from typing import NamedTuple

from celpix.core.address import format_hex
from celpix.core.context import KEY_SOURCE_OFFSET, PipelineContext
from celpix.core.errors import Stage
from celpix.core.notices import warn
from celpix.plugins.base import (
    ContainerField,
    PluginInfo,
    ReadSource,
    WriteTarget,
    format_size,
    plain_read,
    splice,
)

from .md_rom import repair_checksum

_INES_MAGIC = b"NES\x1a"

# bytes: the ROM a pathway's tiles were read out of, so a Save As can put them
# back into a copy of it. Set by Read, and the only thing Write has to go on when
# the destination does not exist yet — the alternative being a bare CHR fragment
# that is not a cartridge and will not reopen as one. Container-local, like
# ``KEY_N64_SWAP``: nothing outside this file has a use for it.
KEY_INES_SOURCE = "ines.source"


class INesLayout(NamedTuple):
    """Where an iNES image's parts sit, as its header declares them."""

    header_end: int  # past the 16-byte header and any 512-byte trainer
    prg_size: int  # bytes of program ROM
    chr_size: int  # bytes of CHR ROM; 0 = a CHR-RAM cart
    nes2: bool
    # The header accounts for bytes after the CHR ROM: NES 2.0 miscellaneous ROM,
    # or a PlayChoice-10 INST-ROM. Neither has a declared size - each is simply
    # whatever follows - so a trailer is only suspicious without one of these.
    has_trailer: bool

    @property
    def prg_end(self) -> int:
        return self.header_end + self.prg_size


def _nes2_size(lsb: int, msb: int, unit: int) -> int:
    """A NES 2.0 ROM size: ``msb:lsb`` units, or ``2**E * (2*MM + 1)`` bytes when
    the MSB nibble is ``$F`` and ``lsb`` is ``EEEEEEMM`` - the form for sizes that
    are not a whole number of banks."""
    if msb == 0xF:
        return (1 << (lsb >> 2)) * ((lsb & 3) * 2 + 1)
    return ((msb << 8) | lsb) * unit


def ines_layout(raw: bytes) -> INesLayout:
    """The layout an iNES or NES 2.0 header (``raw[:16]``) declares.

    NES 2.0 (flags 7 bits 2-3 = ``10``) widens both bank counts with the nibbles
    of byte 9. Reading only bytes 4 and 5 would take a cart with exactly 256 CHR
    banks for a CHR-RAM one, and put the CHR of a 4 MiB program in the wrong place.
    Byte 9 is read only under that marker: older dumps carry junk in bytes 7-15.
    """
    nes2 = raw[7] & 0x0C == 0x08
    header_end = 16 + (512 if raw[6] & 0x04 else 0)
    if nes2:
        prg_size = _nes2_size(raw[4], raw[9] & 0x0F, 16384)
        chr_size = _nes2_size(raw[5], raw[9] >> 4, 8192)
    else:
        prg_size, chr_size = raw[4] * 16384, raw[5] * 8192
    # PlayChoice-10 is byte 7 bit 1 under iNES, but under NES 2.0 bits 0-1 are one
    # console-type field (0 NES, 1 Vs., 2 PlayChoice-10, 3 extended), and type 3
    # sets bit 1 with no INST-ROM behind it.
    playchoice = raw[7] & 0x03 == 0x02 if nes2 else bool(raw[7] & 0x02)
    has_trailer = playchoice or (nes2 and bool(raw[14] & 0x03))
    return INesLayout(header_end, prg_size, chr_size, nes2, has_trailer)


def ines_chr_span(raw: bytes) -> tuple[int, int | None]:
    """``(start, length)`` of the CHR ROM in an iNES image; length None = to end.

    The header (plus a 512-byte trainer when flagged) is followed by the PRG ROM
    and then the CHR ROM. A cart with **CHR-RAM** declares no CHR ROM: its program
    copies tiles out of PRG ROM into CHR-RAM at runtime, so the graphics are
    somewhere in the program banks and everything past the header is handed over.

    Shared by both directions so they cannot disagree about where the graphics
    live; a drift there would splice edited tiles over the program.
    """
    layout = ines_layout(raw)
    if layout.chr_size == 0:
        return layout.header_end, None
    return layout.prg_end, layout.chr_size


def _banks(size: int, unit: int) -> str:
    """A ROM size as ``"<banks> (<size>)"``, or just the size when NES 2.0's
    exponent form made it something other than a whole number of banks."""
    if size % unit:
        return format_size(size)
    return f"{size // unit} ({format_size(size)})"


class INesContainer:
    """A ``.nes`` file, read past the iNES header to the CHR ROM and back.

    If bytes 0–3 are ``NES\\x1a``, the 16-byte header (plus a 512-byte trainer when
    present) is skipped and the CHR ROM starts after the PRG banks, NES 2.0's wider
    bank counts included (:func:`ines_layout`). A CHR-RAM cart declares 0 CHR banks
    and keeps its art in the program banks, so the bytes after the header are
    returned instead. A file without the magic is read like a plain binary.

    ``write`` recomputes the CHR start from the destination's own header
    (:func:`ines_chr_span`) rather than trusting ``dest``: the read started past
    the PRG banks, an offset the host never learns. A destination that is not an
    iNES image is written plainly at ``dest.offset``, matching the read's fallback.

    **A Save As copies the cartridge, not the tiles.** With no file at the
    destination there is no header to splice into, and writing the CHR alone
    yields a bare fragment that is not a ROM and will not reopen as one. So the
    read stashes the image it came from (:data:`KEY_INES_SOURCE`) and the write
    lays the edited tiles into a copy of it — header, trainer, PRG banks and any
    tail included. Copying a document gives you the document.

    **A header can declare more banks than the file holds** — a truncated dump, or
    one whose header is simply wrong — and then the CHR would start past the end.
    The read has nothing to show for such a file and the write must leave it
    alone: splicing at an offset beyond the end zero-fills the gap the header
    invented, which on an all-``0xFF`` header is four megabytes of padding
    appended to the user's ROM.
    """

    info = PluginInfo(
        id="container.ines",
        name="iNES file (auto-skip header)",
        stage=Stage.CONTAINER,
        extensions=(".nes",),
        magic=((0, _INES_MAGIC),),
        short_name="iNES",
        category="Nintendo",
        preserves_offsets=True,
    )

    def read(self, source: ReadSource, ctx: PipelineContext) -> bytes:
        raw = source.data
        if raw[:4] == _INES_MAGIC and len(raw) >= 16:
            start, length = ines_chr_span(raw)
            ctx.set(KEY_SOURCE_OFFSET, start)
            # Kept whole rather than as the two pieces around the CHR: a Save As
            # splices into it by exactly the rule an in-place save uses, so the
            # two directions cannot drift over where the tiles belong.
            ctx.set(KEY_INES_SOURCE, raw)
            if start >= len(raw):
                warn(
                    ctx,
                    "Header declares more banks than the file holds",
                    "The CHR ROM would start past the end of this file, so\n"
                    "there is nothing to show. Either the dump is cut short\n"
                    "or its header is wrong. A save leaves the file exactly\n"
                    "as it is rather than padding out the missing banks.",
                    self.info.id,
                )
            elif length is None:
                # 0 CHR banks is how a header says CHR-RAM, and for such a cart
                # the program banks are exactly where the art is - nothing to
                # warn about. What is suspicious is a file that runs on past its
                # PRG: a CHR-RAM cart ends there, so whole banks after it look
                # like CHR ROM the header forgot to declare.
                layout = ines_layout(raw)
                extra = len(raw) - layout.prg_end
                if extra >= 8192 and not layout.has_trailer:
                    warn(
                        ctx,
                        "Header declares no CHR ROM, but the file runs on",
                        "The header declares 0 CHR banks (CHR-RAM), yet\n"
                        f"{format_size(extra)} follow the program banks, from\n"
                        f"{format_hex(layout.prg_end)}. A CHR-RAM cart ends at its\n"
                        "PRG, so this may be CHR ROM the header fails to declare.\n"
                        "Everything after the header is shown, those bytes last.",
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
        return plain_read(source, ctx)

    def write(self, data: bytes, dest: WriteTarget, ctx: PipelineContext) -> bytes:
        raw = dest.existing
        if not raw:
            # Save As. The ROM this pathway read is the thing being copied, so
            # the tiles go back into a copy of it rather than out on their own.
            source = ctx.get(KEY_INES_SOURCE)
            if isinstance(source, (bytes, bytearray)):
                raw = bytes(source)
        if raw[:4] == _INES_MAGIC and len(raw) >= 16:
            start = ines_chr_span(raw)[0]
            if start >= len(raw):
                # The banks the header claims are not in the file, so the read
                # found no CHR and there is nothing to put back. Splicing here
                # would append the whole invented gap as zeroes.
                return raw
            return splice(raw, start, data)
        # Deliberately `dest.existing`, not `raw`: a stashed source is only ever
        # spliced into as an iNES image, never emitted as a plain buffer.
        return splice(dest.existing, dest.offset, data)

    def describe(
        self, source: ReadSource, ctx: PipelineContext
    ) -> tuple[ContainerField, ...]:
        raw = source.data
        if raw[:4] != _INES_MAGIC or len(raw) < 16:
            return (
                ContainerField(
                    "Header",
                    "not an iNES image",
                    "The first four bytes are not NES\\x1a, so there is no\n"
                    "header to read and no CHR ROM to find. The file is\n"
                    "handed on whole, exactly as a plain binary would be.",
                ),
            )
        trainer = bool(raw[6] & 0x04)
        layout = ines_layout(raw)
        start, length = ines_chr_span(raw)
        fields = [
            ContainerField(
                "Header",
                "NES 2.0, 16 bytes" if layout.nes2 else "iNES, 16 bytes",
                "Bytes 0-15, holding the bank counts and flags below.\n"
                "Skipped on read and preserved on write, so a save\n"
                "leaves the cartridge's own metadata alone.",
            ),
            ContainerField(
                "Trainer",
                "present, 512 bytes (flag 6 bit 2)"
                if trainer
                else "none (flag 6 bit 2 clear)",
                "A 512-byte block some dumps carry between the header\n"
                "and the program. Counted into where the PRG banks\n"
                "start, so a trainer that went unnoticed would put the\n"
                "tiles 512 bytes off.",
            ),
            ContainerField(
                "PRG banks",
                _banks(layout.prg_size, 16384),
                "Program ROM, 16 KiB each. Not graphics, but their\n"
                "total is what the CHR ROM starts after - this is the\n"
                "arithmetic that finds the tiles.",
            ),
            ContainerField(
                "CHR banks",
                _banks(layout.chr_size, 8192)
                if layout.chr_size
                else "0 - CHR-RAM cartridge",
                "Tile ROM, 8 KiB each: the payload this container is\n"
                "after. Zero means CHR-RAM: the program copies its tiles\n"
                "out of PRG ROM at runtime, so everything after the\n"
                "header is shown and the art is among the program banks.",
            ),
        ]
        end = "end of file" if length is None else format_hex(start + length)
        fields.append(
            ContainerField(
                "Payload span",
                f"{format_hex(start)} to {end}",
                "The bytes handed on to be decoded. A save splices back\n"
                "over exactly this range, the header and program banks\n"
                "coming through untouched.",
            )
        )
        return tuple(fields)


class SmdContainer:
    """A Sega ``.smd`` (Genesis) file, deinterleaved to contiguous ROM bytes.

    ``.smd`` has a 512-byte header, then 16 KB blocks storing all the odd bytes
    first and then all the even ones; each block is reconstructed by interleaving
    the two halves. This container always deinterleaves, so plain ``.md``/``.bin``
    want the raw container instead.

    ``write`` is the exact inverse and block-local like the read, each 16 KB block
    getting its own odd-then-even split so the halves never reach across a block
    boundary. Both directions are two strided assignments per block rather than a
    loop over 8192 byte pairs, a Mega Drive image being megabytes. The 512-byte
    header is copier metadata this container never decoded, so it is preserved
    rather than regenerated, as is a trailing partial block the read dropped.
    The cartridge's own checksum is repaired on the deinterleaved image before it
    is split, exactly as ``container.md-rom`` does it for a plain image.
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
        category="Sega",
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
        block, half = self._BLOCK, self._HALF
        out = bytearray(blocks * block)
        for i in range(blocks):
            at = i * block
            out[at + 1 : at + block : 2] = body[at : at + half]  # first half → odd
            out[at : at + block : 2] = body[at + half : at + block]  # second → even
        return bytes(out)

    def write(self, data: bytes, dest: WriteTarget, ctx: PipelineContext) -> bytes:
        data = repair_checksum(data)
        block, half = self._BLOCK, self._HALF
        blocks = len(data) // block
        body = bytearray(blocks * block)
        for i in range(blocks):
            at = i * block
            body[at : at + half] = data[at + 1 : at + block : 2]  # odd → first half
            body[at + half : at + block] = data[at : at + block : 2]  # even → second
        return splice(dest.existing, self._HEADER, bytes(body))

    def describe(
        self, source: ReadSource, ctx: PipelineContext
    ) -> tuple[ContainerField, ...]:
        body = len(source.data) - self._HEADER
        blocks = max(0, body) // self._BLOCK
        tail = max(0, body) - blocks * self._BLOCK
        return (
            ContainerField(
                "Copier header",
                f"{self._HEADER} bytes, skipped",
                "The .smd wrapper's own metadata, which this container\n"
                "never decodes. Preserved as it stands on write rather\n"
                "than regenerated.",
            ),
            ContainerField(
                "Deinterleaved blocks",
                f"{blocks} x 16 KiB ({format_size(blocks * self._BLOCK)})",
                "Each block stores all its odd bytes and then all its\n"
                "even ones; the two halves are woven back together one\n"
                "block at a time, which is why the count matters.",
            ),
            ContainerField(
                "Trailing bytes",
                f"{tail} (dropped)" if tail else "none",
                "A partial block at the end cannot be reassembled - the\n"
                "odd/even split is defined per whole block - so it is not\n"
                "shown here, and a save leaves those bytes as they are.",
            ),
        )


# The copier header 1980s-90s cartridge duplicators prepended, and the only rule
# that spots one: carts are a whole number of KiB, so a ROM exactly 512 bytes over
# has a header on the front. Used as a constant and, via COPIER_SIZE_RULE, as the
# detection signature.
COPIER_HEADER = 512
COPIER_SIZE_RULE = (1024, COPIER_HEADER)
# The floor that keeps the rule off files that merely share the arithmetic. celPix
# is regularly pointed at small extracted tile sheets, and the ``*.4bpp.sfc``
# convention names plenty of them like ROMs; a 512-byte one is also "512 over zero
# KiB". No real cart is near this small and no tile sheet near this big, so 32 KiB
# separates the two cleanly.
COPIER_MIN_SIZE = COPIER_HEADER + 0x8000


def snes_copier_header_len(size: int) -> int:
    """0 or 512 by :data:`COPIER_SIZE_RULE`, for a container that decides itself.

    Shared by both directions so they cannot disagree about where the image
    starts, which would put the write's bytes half a kilobyte from the read's.
    """
    modulus, remainder = COPIER_SIZE_RULE
    return COPIER_HEADER if size % modulus == remainder else 0


class CopierHeaderContainer:
    """A ROM behind a 512-byte copier header, read past it and written behind it.

    The plain case of a headered dump: no interleave, no byte-order quirk, just
    512 bytes of duplicator metadata in front of the cartridge image. Skipping it
    lines every published ROM offset up, those all being quoted against the cart
    rather than the file. The header is metadata this container never decoded, so
    a save preserves it and the file stays the dump it was.

    Detection is by suffix **and** size (:data:`COPIER_SIZE_RULE`, floored at
    :data:`COPIER_MIN_SIZE`): the header carries no marker worth asserting on, so
    the giveaway is a file 512 bytes over a whole number of KiB and big enough to
    be a cartridge. The suffix restriction keeps the size rule off arbitrary
    binaries of the right length and the floor off the small ``*.4bpp.sfc`` tile
    sheets; a headered dump missing either is picked by hand.
    """

    info = PluginInfo(
        id="container.copier-header",
        name="Copier header (skip 512 bytes)",
        stage=Stage.CONTAINER,
        extensions=(".smc", ".swc", ".fig", ".sfc"),
        size_modulo=COPIER_SIZE_RULE,
        min_size=COPIER_MIN_SIZE,
        short_name="Header",
        category="Generic",
        preserves_offsets=True,
    )

    def read(self, source: ReadSource, ctx: PipelineContext) -> bytes:
        raw = source.data
        ctx.set(KEY_SOURCE_OFFSET, COPIER_HEADER)
        # Detection would not have chosen this container for such a file, so
        # reaching here means it was picked by hand, possibly by mistake — and
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

    def describe(
        self, source: ReadSource, ctx: PipelineContext
    ) -> tuple[ContainerField, ...]:
        size = len(source.data)
        headered = bool(snes_copier_header_len(size))
        return (
            ContainerField(
                "File length",
                f"{size} ({format_size(size)})",
                "The whole of what identifies a copier header: a cart is\n"
                "a whole number of KiB, so a file exactly 512 bytes over\n"
                "one has 512 bytes in front of the image.",
            ),
            ContainerField(
                "Size rule",
                "matches - headered" if headered else "does not match",
                "512 bytes are skipped either way, this container having\n"
                "been chosen by hand. Where the rule does not match, that\n"
                "removes 512 real bytes from the front of the image.",
            ),
            ContainerField(
                "Copier header",
                f"{COPIER_HEADER} bytes, skipped",
                "Duplicator metadata this container never decodes, so a\n"
                "save preserves it and the file stays the dump it was.\n"
                "Skipping it lines every published ROM offset up.",
            ),
        )


class SnesInterleavedContainer:
    """An interleaved SNES HiROM image, restored to contiguous ROM bytes.

    Game Doctor / Super UFO copiers stored HiROM images with the upper 32 KB half
    of every 64 KB bank first, then all the lower halves, which is what put the
    internal header at the LoROM-style file offset 0x7Fxx (see
    ``docs/graphics-formats-reference/snes-hardware-notes.md``). A 512-byte copier
    header is skipped first when present. LoROM images were never interleaved, and
    like the ``.smd`` container this one always deinterleaves, so use it only on
    images known to be interleaved.

    ``write`` is the exact inverse. Where the ``.smd`` split is per block, this one
    is *global*: every bank's upper half goes to the first region of the file and
    every lower half to the second. The copier header is detected as the read
    detects it (:func:`snes_copier_header_len`) and preserved, as is any tail past
    the whole banks the read consumed.
    """

    # Unsignatured, so it is never auto-detected: `.sfc`/`.smc` say nothing about
    # interleaving and an interleaved image carries no marker. Deinterleaving a
    # plain image scrambles it, so this one is picked by hand.
    info = PluginInfo(
        id="container.snes-interleaved",
        name="SNES interleaved ROM (deinterleave)",
        stage=Stage.CONTAINER,
        short_name="Interleaved",
        preserves_offsets=False,
        category="Nintendo",
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

    def describe(
        self, source: ReadSource, ctx: PipelineContext
    ) -> tuple[ContainerField, ...]:
        header = snes_copier_header_len(len(source.data))
        body = len(source.data) - header
        bank = 2 * self._HALF
        banks = body // bank
        return (
            ContainerField(
                "Copier header",
                f"{header} bytes, skipped" if header else "none",
                "Spotted by the same size rule the copier-header\n"
                "container uses, and skipped before deinterleaving so\n"
                "the bank halves line up on the image itself.",
            ),
            ContainerField(
                "Deinterleaved banks",
                f"{banks} x 64 KiB ({format_size(banks * bank)})",
                "Every bank's upper 32 KiB half sits in the first region\n"
                "of the file and every lower half in the second, so the\n"
                "split is global rather than per block.",
            ),
            ContainerField(
                "Trailing bytes",
                f"{body - banks * bank} (dropped)" if body - banks * bank else "none",
                "The upper/lower split is defined across whole banks, so\n"
                "a partial one at the end cannot be placed. Not shown\n"
                "here, and left exactly as they are on save.",
            ),
        )
