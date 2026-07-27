"""Konami RLE codecs — two mutually-exclusive framings of one RLE scheme.

Run-length-encoded NES/FDS CHR used by many Konami titles. It decompresses to
ordinary NES 2bpp, which the pixel codec then interprets
(``docs/graphics-formats-reference/implementation-guide.md`` §6), so these are
Compression-stage plugins rather than codec variants — pair either with the NES
2bpp pixel preset.

The core scheme is agreed: a control byte ``c`` with bit 7 clear is a **fill**
(repeat the next byte ``c`` times), bit 7 set a **literal copy** of ``c & 0x7F``
bytes, and ``0xFF`` ends the stream. But two game lineages read the reserved
``0x7F`` / ``0x80`` bytes incompatibly and no structural signal tells them apart,
so each is its own selectable scheme:

- **Contra family** (``decompress.konami-nes-rle``): ``0x7F`` is a **PPU address
  change** — the next 2 little-endian bytes reload the VRAM write cursor to place
  the following run elsewhere; ``0x80`` is an (unused) zero-length literal. Source:
  ``Contra - Hacking Guide`` §Graphics. We flatten VRAM layout, so the address is
  consumed but not honoured — skipping its 2 bytes is what keeps the stream in
  sync (a missed skip mis-reads the low address byte as a control and desyncs the
  tail). Fills/literals cap at ``0x7E`` because ``0x7F`` and ``0xFF`` are reserved.

- **Simon's Quest / FDS family** (``decompress.konami-fds-rle``): no address
  command — ``0x7F`` is a plain **127-byte fill** and ``0x80`` a **256-byte
  literal** (an incompressible block). Covers *Dracula II* / Simon's Quest, Ai
  Senshi Nicol and Rampart.

In both, the leading per-group 2-byte PPU destination is not modelled — point the
read past it.

**One shared compressor.** Both schemes encode through the same :func:`compress`,
which stays inside the *unambiguous subset* every reading decodes alike: it never
emits ``0x7F`` or ``0x80`` (runs cap at ``0x7E``), so its output round-trips under
either family's decoder. Byte-identity with a game's original blob is a non-goal;
round-tripping is the contract. The two schemes differ only in how they *decode*,
and share a base class carrying the one encoder.
"""

from __future__ import annotations

from celpix.core.context import (
    KEY_COMPRESSED_SIZE,
    KEY_DECOMPRESS_COMPLETE,
    PipelineContext,
)
from celpix.core.errors import Stage
from celpix.plugins.base import PluginInfo

# Largest byte count one fill or literal control byte can safely encode. 0x7F and
# 0xFF are reserved in the Contra reading (address change, terminator) and
# literals mask with 0x7F, so the shared compressor stays under 0x7E to keep its
# output valid for every variant.
_MAX_CHUNK = 0x7E
# Shortest run worth encoding as a fill. A fill costs 2 bytes; the same bytes left
# in a literal cost one each plus an amortised control byte, so a run only pays
# for itself at 3.
_MIN_FILL_RUN = 3


def decompress(data: bytes, *, fds: bool = False) -> tuple[bytes, int, bool]:
    """Decode a Konami RLE stream.

    ``fds`` selects the Simon's Quest / FDS reading of the ``0x7F`` / ``0x80``
    control bytes (see the module docstring); the default is the Contra reading.

    Returns ``(output, consumed, complete)``. ``complete`` is true when the
    ``0xFF`` terminator was reached inside the buffer, making ``consumed`` the
    structure's true byte length — the slot a save-back must fit. A buffer ending
    mid-stream, a bounded view window or a truncated dump, yields the best-effort
    prefix with ``complete`` false.
    """
    out = bytearray()
    i, n = 0, len(data)
    complete = False
    while i < n:
        c = data[i]
        i += 1
        if c == 0xFF:  # end (both variants)
            complete = True
            break
        if c == 0x7F and not fds:
            # Contra: PPU address change — consume the 2-byte destination, keep going.
            if i + 2 > n:  # buffer ends inside the address
                i = n
                break
            i += 2
            continue
        if c == 0x80 and fds:
            count = 0x100  # FDS: 256-byte incompressible literal
        elif c >= 0x80:
            count = c & 0x7F  # literal copy; Contra 0x80 -> 0 (unused no-op)
        else:
            # Fill: value byte repeated c times. In FDS mode 0x7F lands here as a
            # 127-fill; in Contra mode 0x7F was handled above, so c is <= 0x7E.
            if i >= n:  # buffer ended before the value byte
                break
            out += bytes([data[i]]) * c
            i += 1
            continue
        chunk = data[i : i + count]  # shared literal copy for both variants
        out += chunk
        if len(chunk) < count:  # buffer ended mid-literal
            i = n
            break
        i += count
    return bytes(out), i, complete


def compress(data: bytes) -> bytes:
    """Encode raw bytes into a Konami RLE stream every variant decodes back.

    Emits a fill for every run of ``_MIN_FILL_RUN`` or more equal bytes and packs
    everything else into literal chunks, both capped at ``_MAX_CHUNK``. A run
    remainder too short for its own fill folds back into the literal buffer.
    Never emits ``0x7F`` or ``0x80``, so the output is unambiguous under both the
    Contra and FDS readings.
    """
    out = bytearray()
    literals = bytearray()

    def flush_literals() -> None:
        start = 0
        while start < len(literals):
            take = min(len(literals) - start, _MAX_CHUNK)
            out.append(0x80 | take)
            out.extend(literals[start : start + take])
            start += take
        literals.clear()

    i, n = 0, len(data)
    while i < n:
        value = data[i]
        run = 1
        while i + run < n and data[i + run] == value:
            run += 1

        if run >= _MIN_FILL_RUN:
            flush_literals()
            remaining = run
            while remaining >= _MIN_FILL_RUN:
                take = min(remaining, _MAX_CHUNK)
                out.append(take)
                out.append(value)
                remaining -= take
            # A 1- or 2-byte tail can't earn a fill of its own; ship it literal.
            literals += bytes([value]) * remaining
            i += run
        else:
            literals.append(value)
            i += 1

    flush_literals()
    out.append(0xFF)
    return bytes(out)


class _KonamiRle:
    """Shared base: the two schemes differ only in how ``decompress`` reads them.

    Encoding is the portable subset every variant accepts, so it lives here and
    each scheme supplies only its own ``fds`` flag.
    """

    fds: bool

    def decompress(self, data: bytes, ctx: PipelineContext) -> bytes:
        out, consumed, complete = decompress(data, fds=self.fds)
        ctx.set(KEY_COMPRESSED_SIZE, consumed)
        ctx.set(KEY_DECOMPRESS_COMPLETE, complete)
        return out

    def compress(self, data: bytes, ctx: PipelineContext) -> bytes:
        return compress(data)


class KonamiNesRle(_KonamiRle):
    info = PluginInfo(
        id="compression.konami-nes-rle",
        name="Konami RLE (Contra family)",
        stage=Stage.COMPRESSION,
    )
    fds = False


class KonamiFdsRle(_KonamiRle):
    info = PluginInfo(
        id="compression.konami-fds-rle",
        name="Konami RLE (Simon's Quest / FDS family)",
        stage=Stage.COMPRESSION,
    )
    fds = True
