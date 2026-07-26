"""Container-aware Read/Write plugins: iNES header skip, Sega ``.smd`` and SNES
interleaved-image deinterleave.

All acquire raw bytes like :class:`~celpix.plugins.builtins.raw_file.RawFileReader`
but apply a container transform first, so the pixel codec downstream sees contiguous
tile data (``docs/graphics-formats-reference/implementation-guide.md`` §5). They
record provenance into the context like the raw reader does.

Every one of them ships a **writer** too, and must: each reader ignores
``source.offset`` and works out its own start from the format, so the offset the
host carries alongside describes addressing *within* the unwrapped bytes and says
nothing about where the wrapped ones belong. Writing through plain bytes at that
offset therefore does not restore the file, it destroys it — the deinterleaving
containers would lay an unwrapped image over a wrapped one, and iNES would drop a
bare CHR fragment at the wrong place and truncate the ROM to it. Nothing has to
declare that: a Read plugin with no ``write.X`` beside it is simply not written
through at all (:func:`~celpix.plugins.detect.container_write_enabled`).

So a container writer here **recomputes its own destination from the file on
disk** — the same rule its reader used, factored into a shared helper so the two
cannot drift — and preserves everything it did not decode: the copier or iNES
header, the PRG banks, and any tail the reader dropped for being less than a whole
block.
"""

from __future__ import annotations

from pathlib import Path

from celpix.core.context import KEY_SOURCE_OFFSET, KEY_SOURCE_PATH, PipelineContext
from celpix.core.errors import Stage
from celpix.core.notices import warn
from celpix.plugins.base import FileRef, PluginInfo

_INES_MAGIC = b"NES\x1a"


def _splice(path: Path, at: int, data: bytes) -> None:
    """Write ``data`` into ``path`` at ``at``, keeping every other byte.

    Shared by the container writers below: what makes them containers is the
    transform, not the file handling, and each must preserve the header it did
    not decode as well as any trailing bytes its reader dropped.
    """
    existing = bytearray(path.read_bytes()) if path.exists() else bytearray()
    end = at + len(data)
    if len(existing) < end:
        existing.extend(b"\x00" * (end - len(existing)))
    existing[at:end] = data
    path.write_bytes(bytes(existing))


def ines_chr_span(raw: bytes) -> tuple[int, int | None]:
    """``(start, length)`` of the CHR ROM in an iNES image; length None = to end.

    The 16-byte header (plus a 512-byte trainer when flagged) is followed by the
    PRG banks, and the CHR ROM after those. A cart with **CHR-RAM** declares zero
    CHR banks and so has no CHR ROM at all; the bytes past the header are handed
    over instead, which is the best a graphics editor can offer for one.

    Shared by the reader and the writer so the two cannot disagree about where the
    graphics live — a drift there would splice edited tiles over the program.
    """
    header_end = 16 + (512 if raw[6] & 0x04 else 0)
    prg_banks, chr_banks = raw[4], raw[5]
    if chr_banks == 0:
        return header_end, None
    return header_end + prg_banks * 16384, chr_banks * 8192


class INesReader:
    """Read a ``.nes`` file, auto-skipping the iNES header to the CHR ROM.

    If bytes 0–3 are ``NES\\x1a``, the 16-byte header (plus a 512-byte trainer when
    present) is skipped; the CHR ROM starts after the PRG banks. When the cart uses
    CHR-RAM (0 CHR banks) there is no CHR ROM, so the bytes after the header are
    returned. A file without the magic is read like a plain binary.
    """

    info = PluginInfo(
        id="read.ines",
        name="iNES file (auto-skip header)",
        stage=Stage.READ,
        extensions=(".nes",),
        magic=((0, _INES_MAGIC),),
        short_name="iNES",
    )

    def read(self, source: FileRef, ctx: PipelineContext) -> bytes:
        raw = Path(source.path).read_bytes()
        ctx.set(KEY_SOURCE_PATH, source.path)
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
        # Not an iNES file — behave like the raw reader.
        warn(
            ctx,
            "Not an iNES file: read as plain bytes",
            "The first four bytes are not NES\\x1a, so there is no\n"
            "header to skip and no CHR ROM to find. Nothing was\n"
            "changed; the whole file is shown as-is.",
            self.info.id,
        )
        start = source.offset
        end = len(raw) if source.length is None else start + source.length
        ctx.set(KEY_SOURCE_OFFSET, source.offset)
        return raw[start:end]


class INesWriter:
    """Splice edited CHR ROM back into a ``.nes``, keeping the header and PRG.

    The inverse of :meth:`INesReader.read`, and needed for the reason a header
    skip alone would not be: the reader starts past the PRG banks, an offset the
    host never learns, so a plain write would put the CHR fragment at the wrong
    place and truncate the ROM to it. The CHR start is recomputed from the file's
    own header (:func:`ines_chr_span`) rather than trusted from ``dest``.

    A destination that is not an iNES image is written plainly at ``dest.offset``,
    matching the reader's own fallback for a file without the magic.
    """

    info = PluginInfo(
        id="write.ines",
        name="iNES file (auto-skip header)",
        stage=Stage.WRITE,
    )

    def write(self, data: bytes, dest: FileRef, ctx: PipelineContext) -> None:
        path = Path(dest.path)
        raw = path.read_bytes() if path.exists() else b""
        if raw[:4] == _INES_MAGIC and len(raw) >= 16:
            _splice(path, ines_chr_span(raw)[0], data)
        else:
            _splice(path, dest.offset, data)


class SmdReader:
    """Read a Sega ``.smd`` (Genesis) file, deinterleaving to contiguous ROM bytes.

    ``.smd`` has a 512-byte header, then 16 KB blocks storing all the odd bytes
    first, then all the even bytes. Each block is reconstructed by interleaving the
    two halves back together. Plain ``.md``/``.bin`` need no transform (use the raw
    reader); this reader always deinterleaves.
    """

    # Suffix only: the 512-byte header carries no marker this reader can assert
    # on, so the name is the whole of what identifies a .smd.
    info = PluginInfo(
        id="read.smd",
        name="Sega .smd (deinterleave)",
        stage=Stage.READ,
        extensions=(".smd",),
        short_name="SMD",
    )

    _HEADER = 512
    _BLOCK = 16384
    _HALF = 8192

    def read(self, source: FileRef, ctx: PipelineContext) -> bytes:
        raw = Path(source.path).read_bytes()
        ctx.set(KEY_SOURCE_PATH, source.path)
        ctx.set(KEY_SOURCE_OFFSET, self._HEADER)
        body = raw[self._HEADER :]
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
    """0 or 512 by :data:`COPIER_SIZE_RULE`, for a reader that must decide itself.

    Shared by the interleaved reader and its writer so the two cannot disagree
    about where the image starts — which would put the writer's bytes half a
    kilobyte away from the reader's.
    """
    modulus, remainder = COPIER_SIZE_RULE
    return COPIER_HEADER if size % modulus == remainder else 0


class CopierHeaderReader:
    """Read a ROM behind a 512-byte copier header, skipping it.

    The plain case of a headered dump: no interleave, no byte-order quirk, just
    512 bytes of duplicator metadata in front of the cartridge image. Skipping it
    is what makes every published ROM offset line up, since those are all quoted
    against the cart rather than the file.

    Detection is by suffix **and** size (:data:`COPIER_SIZE_RULE`, floored at
    :data:`COPIER_MIN_SIZE`) — the header carries no marker worth asserting on, so
    the giveaway is the file being 512 bytes over a whole number of KiB, on
    something big enough to be a cartridge. Restricting it to the copier suffixes
    keeps the size rule from seizing arbitrary binaries of the right length, and
    the floor keeps it off the small ``*.4bpp.sfc`` tile sheets celPix is just as
    often opening; a headered dump that misses either is picked by hand.
    """

    info = PluginInfo(
        id="read.copier-header",
        name="Copier header (skip 512 bytes)",
        stage=Stage.READ,
        extensions=(".smc", ".swc", ".fig", ".sfc"),
        size_modulo=COPIER_SIZE_RULE,
        min_size=COPIER_MIN_SIZE,
        short_name="Header",
    )

    def read(self, source: FileRef, ctx: PipelineContext) -> bytes:
        raw = Path(source.path).read_bytes()
        ctx.set(KEY_SOURCE_PATH, source.path)
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


class CopierHeaderWriter:
    """Put the image back behind its copier header, leaving the header alone.

    The header is duplicator metadata this container never decoded, so it is
    preserved rather than regenerated — the file stays the dump it was.
    """

    info = PluginInfo(
        id="write.copier-header",
        name="Copier header (skip 512 bytes)",
        stage=Stage.WRITE,
    )

    def write(self, data: bytes, dest: FileRef, ctx: PipelineContext) -> None:
        _splice(Path(dest.path), COPIER_HEADER, data)


class SnesInterleavedReader:
    """Read an interleaved SNES HiROM image, restoring contiguous ROM bytes.

    Game Doctor / Super UFO copiers stored HiROM images with the upper 32 KB
    half of every 64 KB bank first, then all the lower halves — which is what
    put the internal header at the LoROM-style file offset 0x7Fxx (see
    ``docs/graphics-formats-reference/snes-hardware-notes.md``). A 512-byte
    copier header is skipped first when present; carts are always a whole
    number of KiB, so a header is present iff ``size % 1024 == 512``. LoROM
    images were never interleaved. Like the ``.smd`` reader this always
    deinterleaves — use it only on images known to be interleaved.
    """

    # Deliberately unsignatured, so it is never auto-detected: `.sfc`/`.smc` say
    # nothing about interleaving and an interleaved image carries no marker.
    # Deinterleaving a plain image scrambles it, so this one is picked by hand.
    info = PluginInfo(
        id="read.snes-interleaved",
        name="SNES interleaved ROM (deinterleave)",
        stage=Stage.READ,
        short_name="Interleaved",
    )

    _HALF = 0x8000  # half of a 64 KB HiROM bank

    def read(self, source: FileRef, ctx: PipelineContext) -> bytes:
        raw = Path(source.path).read_bytes()
        ctx.set(KEY_SOURCE_PATH, source.path)
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


class SmdWriter:
    """Re-interleave a ``.smd`` body, restoring the copier's block layout.

    The exact inverse of :meth:`SmdReader.read`, and block-local like it: each
    16 KB block gets its own odd-bytes-then-even-bytes split, so the halves never
    reach across a block boundary. Written as two strided assignments per block
    rather than a loop over 8192 byte pairs — a Mega Drive image is megabytes and
    the per-byte form would be seconds of Python.

    The 512-byte header is left exactly as it was: it is copier metadata this
    container never decoded, so it is not ours to regenerate. A trailing partial
    block — which the reader drops — is likewise left alone rather than truncated
    away.
    """

    info = PluginInfo(
        id="write.smd",
        name="Sega .smd (deinterleave)",
        stage=Stage.WRITE,
    )

    def write(self, data: bytes, dest: FileRef, ctx: PipelineContext) -> None:
        block, half = SmdReader._BLOCK, SmdReader._HALF
        blocks = len(data) // block
        body = bytearray(blocks * block)
        for i in range(blocks):
            at = i * block
            body[at : at + half] = data[at + 1 : at + block : 2]  # odd → first half
            body[at + half : at + block] = data[at : at + block : 2]  # even → second
        _splice(Path(dest.path), SmdReader._HEADER, bytes(body))


class SnesInterleavedWriter:
    """Re-interleave a SNES HiROM image back into copier order.

    The exact inverse of :meth:`SnesInterleavedReader.read`: where the ``.smd``
    split is per block, this one is *global* — every bank's upper half is written
    to the first region of the file and every lower half to the second, which is
    what put the internal header at the LoROM-style offset in the first place.

    The copier header is detected the same way the reader detects it
    (:func:`snes_copier_header_len`) and preserved, as is any tail past the whole
    banks the reader consumed.
    """

    info = PluginInfo(
        id="write.snes-interleaved",
        name="SNES interleaved ROM (deinterleave)",
        stage=Stage.WRITE,
    )

    def write(self, data: bytes, dest: FileRef, ctx: PipelineContext) -> None:
        half = SnesInterleavedReader._HALF
        bank = 2 * half
        banks = len(data) // bank
        body = bytearray(banks * bank)
        lowers = banks * half
        for i in range(banks):
            src = i * bank
            body[i * half : (i + 1) * half] = data[src + half : src + bank]
            body[lowers + i * half : lowers + (i + 1) * half] = data[src : src + half]
        path = Path(dest.path)
        header = snes_copier_header_len(len(path.read_bytes()) if path.exists() else 0)
        _splice(path, header, bytes(body))
