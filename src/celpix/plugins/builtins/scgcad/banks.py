"""The S-CG-CAD non-tilemap members: the palette (COL) and the tile bank (CGX).

The two members that frame something other than a tilemap — which is why
:attr:`~celpix.plugins.base.PluginInfo.content_kinds` exists — and the two whose
payload comes *first*, so reading either raw decodes its trailing metadata as
colours or tiles of noise (``scgcad-formats.md`` §6, §7).
"""

from __future__ import annotations

from celpix.core.address import format_hex
from celpix.core.capabilities import ContentKind
from celpix.core.context import (
    KEY_PIXEL_PRESET,
    KEY_TILE_PALETTE_ROW_BASE,
    KEY_TILE_PALETTE_ROWS,
    PipelineContext,
)
from celpix.core.errors import Stage
from celpix.plugins.base import (
    ContainerField,
    PluginInfo,
    ReadSource,
    WriteTarget,
    format_size,
    plain_read,
    splice,
)

from ._common import (
    _DEPTH_BPP,
    _DEPTH_BYTE,
    HEADER,
    SIGNATURE,
    _blank,
    _metadata_fields,
    _payload,
    _row_base,
)

COL_SIZE = 0x400
COL_PAYLOAD = 0x200  # 256 BGR555 entries; the metadata block follows them
COL_HEADER_AT = 0x200
CGX_COL_HALF, CGX_COL_CELL = 0x22, 0x23
# A bank states its depth in the same encoding a screen does, at its own offset,
# and it is the file's *second* statement of which of the three variants it is —
# the first being where the signature sits. All 1,744 surveyed banks agree with
# their own length, so either signal identifies the variant on its own
# (`scgcad-formats.md` §6).
CGX_DEPTH = 0x20


class ColContainer:
    """Palette file: 0x200 of colour, then the 0x100 header and its 0x100 tail.

    The only member of the family that frames a *palette*, and the reason a
    container is needed at all: the colours stop halfway. Read whole, the
    metadata block decodes as 128 more BGR555 entries — junk rows 16-31 of a 4bpp
    palette — which look like colours because any two bytes do.

    The file holds 256 entries where the console has 256 too, so the trailing
    block is not a second bank: a screen picks which 128-colour half to draw
    through with its own ``col_half`` field (``scgcad-formats.md`` §2).
    """

    info = PluginInfo(
        id="container.scgcad-col",
        name="S-CG-CAD palette (COL)",
        stage=Stage.CONTAINER,
        extensions=(".col",),
        magic=((COL_HEADER_AT, SIGNATURE),),
        exact_size=COL_SIZE,
        short_name="COL",
        content_kinds=(ContentKind.PALETTE,),
        category="S-CG-CAD",
        preserves_offsets=True,
    )

    def read(self, source: ReadSource, ctx: PipelineContext) -> bytes:
        return _payload(source, ctx, 0, COL_PAYLOAD)

    def write(self, data: bytes, dest: WriteTarget, ctx: PipelineContext) -> bytes:
        # Splice, so a colour edit leaves the tool's own metadata block alone —
        # it names the version that wrote the file, and celPix is not it.
        existing = dest.existing or _blank(COL_SIZE, COL_HEADER_AT)
        return splice(existing, 0, data[:COL_PAYLOAD])

    def describe(
        self, source: ReadSource, ctx: PipelineContext
    ) -> tuple[ContainerField, ...]:
        return (
            *_metadata_fields(source.data, COL_HEADER_AT),
            ContainerField(
                "Colors",
                f"{format_size(COL_PAYLOAD)} at 0x000000 - {COL_PAYLOAD // 2} entries",
                "Where the colors stop; the reason for a container\n"
                "Read whole, the metadata block decodes as 128 junk colors",
            ),
            ContainerField(
                "Metadata block",
                f"{format_size(COL_SIZE - COL_PAYLOAD)}"
                f" at {format_hex(COL_HEADER_AT, 4)}, preserved",
                "Spliced around on write; a color edit leaves it as-is\n"
                "Not a second bank: a screen's own field picks the half",
            ),
        )


# A tile bank, by file size: payload length, the pixel preset that reads it, and
# its depth in bits. All 1024 tiles; the depth is what changes. Size and the
# header's own depth byte agree across the whole surveyed corpus, so either would
# do and neither has to trust the other.
CGX_BANKS: dict[int, tuple[int, str, int]] = {
    0x4500: (0x4000, "preset.pixel.snes-2bpp", 2),
    0x8500: (0x8000, "preset.pixel.snes-4bpp", 4),
    0x10100: (0x10000, "preset.pixel.snes-8bpp", 8),
}
# The same three, keyed by how many bytes of tiles they hold — which is what the
# write side has to name a variant from. What comes out of the pipeline is the
# payload; its length is the depth the user is actually saving at, and the ctx
# hint the read published is not, having only ever seeded a picker they own.
CGX_BY_PAYLOAD: dict[int, int] = {
    payload: size for size, (payload, *_) in CGX_BANKS.items()
}
CGX_ROW_TABLE = 0x400  # one byte per tile, each a palette row


def _cgx_bank(data: bytes) -> tuple[int, str, int] | None:
    """Which of the three banks ``data`` is, by length and then by signature.

    Length settles it for every well-formed file — the three differ by more than
    their payloads, since only 2bpp and 4bpp carry an attribute table. Signature
    position is the fallback for one that has picked up a tail, and it is the
    same signal detection matched on: the metadata block sits *after* the tiles,
    so where it starts is where they stop. Reading a tailed bank by length alone
    hands the header and the table on as three dozen tiles of noise and states no
    depth at all, which is exactly what the framing exists to prevent.

    ``None`` for bytes that are neither, which the caller passes through whole.
    """
    bank = CGX_BANKS.get(len(data))
    if bank is not None:
        return bank
    for bank in CGX_BANKS.values():
        payload = bank[0]
        if data[payload : payload + len(SIGNATURE)] == SIGNATURE:
            return bank
    return None


def _blank_cgx(payload: int) -> bytes:
    """An empty bank of the variant ``payload`` bytes of tiles names.

    Stamped so the file states that variant both ways a real one does — the
    signature's position, and the depth byte behind it — because a bank written
    with neither is not a bank: nothing detects it, and reopening it lands on raw
    bytes at a guessed depth — which is what a save to a path with no file at it
    would otherwise produce.

    The attribute table is zero, and 8bpp has none. Row 0 is a row rather than a
    sentinel (`scgcad-formats.md` §6), so a fresh bank saying every tile is row 0
    is a statement it can stand behind, and the base its header states is 0 to
    match.
    """
    size = CGX_BY_PAYLOAD[payload]
    _, _, bpp = CGX_BANKS[size]
    out = bytearray(_blank(size, payload))
    out[payload + CGX_DEPTH] = _DEPTH_BYTE[bpp]
    return bytes(out)


def _cgx_row_base(data: bytes, payload: int, bpp: int) -> int | None:
    """The palette row this bank's tiles count their own row 0 from, or None.

    ``col_half * 128 + col_cell * n`` colours, the same per-file base a screen or
    a panel applies (``scgcad-formats.md`` §3.3), divided through by ``n`` into
    rows. 1,072 of the 2,304 surveyed banks set one — 766 of them a whole colour
    half, which is eight rows out at 4bpp.

    It is the base for everything drawn *from* the bank, so it answers for the
    formats that carry a palette row and have nowhere to put a base: a sprite
    object's 3-bit field, an 8bpp bank's absent attribute table. None where the
    header is not there to be read — thirty-nine banks of the corpus carry no
    signature at all, their 0x100 header zeroed, and a zero base read off that is
    an invention rather than a reading.
    """
    header = data[payload : payload + HEADER]
    if header[: len(SIGNATURE)] != SIGNATURE or len(header) <= CGX_COL_CELL:
        return None
    return _row_base(header[CGX_COL_HALF], header[CGX_COL_CELL], bpp)


def _cgx_rows(data: bytes, payload: int, bpp: int) -> bytes:
    """A bank's per-tile palette rows as the file states them, or empty when it
    states none.

    **Relative rows**, exactly as the table holds them: what they count from is
    published beside them as :data:`~celpix.core.context.KEY_TILE_PALETTE_ROW_BASE`,
    and the host applies the base to a named row once, at render
    (:func:`~celpix.pipeline.pipeline.drawn_palette_row`). Folding it in here as
    well would move a bank's art twice.

    The table is only there when the header is. The thirty-nine header-less banks
    hold 0xFC throughout, a metadata block allocated and never written. Their
    tiles are real and at the depth the size gives, so the payload is still cut
    and the preset still published; the table is not a statement about them.

    An 8bpp bank is too short to hold a table at all, which is also the guard
    the only other implementation of this format applies.
    """
    if bpp >= 8:
        return b""
    if _cgx_row_base(data, payload, bpp) is None:
        return b""
    at = payload + HEADER
    return data[at : at + CGX_ROW_TABLE]


class CgxContainer:
    """Tile bank: the tiles, then a 0x100 header, then a per-tile row table.

    Payload-first like a screen, so reading the file raw *almost* works — which
    is the problem. The trailing header and table decode as a few dozen tiles of
    convincing noise at the end of every bank, and the bit depth has to be
    guessed from three that all look plausible. Both are in the file.

    **Three variants, one per depth**, and they differ by more than their
    payloads: 8bpp is too short to hold the attribute table at all, so the three
    lengths are 0x4500, 0x8500 and 0x10100 for 0x4000, 0x8000 and 0x10000 of
    tiles. Which one a file is comes from its length, and failing that from where
    its signature sits (:func:`_cgx_bank`) — the two signals detection and
    reading now share, so a bank with a tail is not read as a fourth thing.

    A write names the variant from the **payload** instead, because that is the
    depth being saved and the header of the file already there may be a
    different one. Same variant, and everything around the tiles is preserved;
    otherwise the bank is built (:func:`_blank_cgx`), which is also what a save
    to a path with no file at it gets. A payload that is none of the three sizes
    is refused when the destination is a bank — splicing it in would overwrite
    the header — and passed through whole otherwise, as the read passes through
    a file of a length the family does not have.

    Two hints go out on the context rather than being applied here: the pixel
    preset the payload is in, and the per-tile palette rows. A container's job is
    to say what it knows, not to reach into the view.
    """

    info = PluginInfo(
        id="container.scgcad-cgx",
        name="S-CG-CAD tile bank (CGX)",
        stage=Stage.CONTAINER,
        extensions=(".cgx",),
        # One offset per depth: the signature sits at the payload's end, so where
        # it is *is* which bank this is.
        magic=tuple((payload, SIGNATURE) for payload, _, _ in CGX_BANKS.values()),
        short_name="CGX",
        category="S-CG-CAD",
        preserves_offsets=True,
    )

    def read(self, source: ReadSource, ctx: PipelineContext) -> bytes:
        bank = _cgx_bank(source.data)
        if bank is None:
            # A size the family does not have: hand the bytes on whole rather
            # than cutting at a guess. Better a few trailing junk tiles than a
            # silently truncated bank.
            return plain_read(source, ctx)
        payload, preset_id, bpp = bank
        ctx.set(KEY_PIXEL_PRESET, preset_id)
        rows = _cgx_rows(source.data, payload, bpp)
        if rows:
            ctx.set(KEY_TILE_PALETTE_ROWS, rows)
        # Published beside the table rather than only folded into it: a tilemap
        # bound to this bank draws its own cells' rows from the same origin, and
        # a sprite object's 3-bit field has no header of its own to say so.
        base = _cgx_row_base(source.data, payload, bpp)
        if base is not None:
            ctx.set(KEY_TILE_PALETTE_ROW_BASE, base)
        return _payload(source, ctx, 0, payload)

    def write(self, data: bytes, dest: WriteTarget, ctx: PipelineContext) -> bytes:
        # Splice, so the header and the row table come back untouched — the rows
        # are the file's own statement about its tiles and celPix has no reason
        # to rewrite them from an edit that changed pixels.
        bank = _cgx_bank(dest.existing)
        if bank is not None and bank[0] == len(data):
            return splice(dest.existing, 0, data)
        # Either there is no file there yet or the depth has changed under it,
        # and both need a container built rather than one reused: the three
        # variants are different lengths, so writing 4bpp tiles into a 2bpp bank
        # can only drop half of them, and writing them into nothing at all leaves
        # a payload no reader can identify. What the old file said about a
        # different depth — its colour selectors, its table — is not a statement
        # about these tiles, so none of it carries over.
        if len(data) not in CGX_BY_PAYLOAD:
            if bank is not None:
                # The destination *is* a bank and these tiles are no size a bank
                # holds. Splicing them over it would lay tile bytes across the
                # header and the row table, leaving a file no reader recognises
                # - a resize past the family's sizes, not a save, since a save
                # puts back the length the read produced. Refused so the bank
                # stays a bank.
                sizes = ", ".join(
                    format_hex(size, None) for size in sorted(CGX_BY_PAYLOAD)
                )
                raise ValueError(
                    f"a tile bank holds {sizes} bytes of tiles and "
                    f"{format_hex(len(data), None)} "
                    "is none of them; the bank at the destination was left as it is"
                )
            # Not one of the three payload sizes and nothing to protect: the same
            # pass-through the read half does for a length this family does not
            # have.
            return splice(dest.existing, 0, data)
        return splice(_blank_cgx(len(data)), 0, data)

    def describe(
        self, source: ReadSource, ctx: PipelineContext
    ) -> tuple[ContainerField, ...]:
        data = source.data
        bank = _cgx_bank(data)
        if bank is None:
            return (
                ContainerField(
                    "Bank size",
                    f"{len(data)} bytes - not a size this family has",
                    "Banks are told by length, then by signature position;\n"
                    "this file matches neither\n"
                    "Passed on whole rather than cut at a guess",
                ),
            )
        payload, _, bpp = bank
        stated = data[payload + CGX_DEPTH] if len(data) > payload + CGX_DEPTH else None
        if bpp >= 8:
            table = "none - 8bpp banks carry no table"
        elif not _cgx_rows(data, payload, bpp):
            table = "present but not read - the header is absent"
        else:
            base = _cgx_row_base(data, payload, bpp)
            table = (
                f"{format_size(CGX_ROW_TABLE)} at {format_hex(payload + HEADER, 4)}"
                f", counted from row {base}"
            )
        return (
            *_metadata_fields(data, payload),
            ContainerField(
                "Payload",
                f"{format_size(payload)} at 0x000000 - {payload // (8 * bpp)} tiles",
                "Payload first, header after\n"
                "Read raw, the trailing block decodes as tiles of noise",
            ),
            ContainerField(
                "Bit depth",
                f"{bpp}bpp - {len(data)} bytes"
                + (
                    ", header says none"
                    if stated is None
                    else f", header says {_DEPTH_BPP.get(stated & 3, bpp)}bpp"
                ),
                f"Stated twice: the length, and header byte +{CGX_DEPTH:#04x}\n"
                "The two agree across the surveyed corpus\n"
                "Published as the pixel format the view starts at",
            ),
            ContainerField(
                "Palette row table",
                table,
                "One byte per tile: its palette row, from the header's base\n"
                "Published to seed pinned palette regions\n"
                "Preserved on write; a headerless bank states no rows",
            ),
        )
