"""PlayStation `TIM` textures — the header, the image block, and the CLUT.

The texture format PlayStation art ships in, and the wrapper the console's own
DMA expected: a TIM is a **VRAM upload record**, not a picture file. Its header
says which framebuffer rectangle the pixels belong at and which rectangle the
colours belong at, and both are quoted in 16-bit units because VRAM is addressed
in halfwords whatever the pixels inside them mean.

That is the one thing a reader has to get right. The image block's ``W`` is the
rectangle's width **in halfwords**, so the picture is four times as wide at 4bpp
and twice as wide at 8bpp; read as a pixel count it comes out a quarter of its
real width and shears into diagonal stripes. Nothing in the file marks the
difference — the same field means a different number of pixels per depth.

    0x00  u32  0x00000010    magic (id byte 0x10, version 0x00, two reserved)
    0x04  u32  flags         bits 0-2 pmode, bit 3 CLUT present, rest reserved

    pmode  0 = 4bpp indexed   1 = 8bpp indexed
           2 = 16bpp direct   3 = 24bpp direct

A block follows for the CLUT (only when bit 3 is set) and then one for the image.
Both carry the same 12-byte header, and ``bnum`` counts **itself** — the payload
is ``bnum - 12`` bytes:

    +0x00  u32  bnum          byte length of this block, header included
    +0x04  u16  DX, +0x06 DY  where in VRAM it is uploaded (not a picture offset)
    +0x08  u16  W             CLUT: entries per palette. Image: halfwords per row
    +0x0A  u16  H             CLUT: how many palettes. Image: rows
    +0x0C  ...  payload

**Two containers over one file, because a TIM holds two kinds of thing.** celPix
detects a container per content kind (:func:`~celpix.plugins.detect.frames_kind`),
so :class:`TimContainer` claims the file on the pixel pathway and unwraps to the
image payload, while :class:`TimClutContainer` claims the same file on the palette
pathway and unwraps to the CLUT entries. Pointing both pathways at one path is how
a TIM opens in its own colours.

**The colours are BGR555 with a bit left over.** Bit 15 is `STP`, the
semi-transparency flag the GPU consults per pixel; the low 15 bits are the
ordinary R/G/B fields ``preset.palette.bgr555`` already reads, which is why no
new colour codec is needed. celPix's palette model is plain ARGB and has nowhere
to keep STP, so re-encoding a palette would zero all 256 of those bits. The CLUT
container's write therefore **carries each entry's STP bit over from the file it
is saving into** — the same "preserve what you did not decode" rule the rest of
the container layer keeps.

**What the pixels need after unwrapping** is a matching pixel preset, which the
read publishes as ``KEY_PIXEL_PRESET`` to seed the picker. The four depths are
ordinary formats celPix already had: 4bpp and 8bpp are packed indices low-nibble
first, 16bpp is direct BGR555, and 24bpp is three bytes in R, G, B order.

**The image's width is reported, not applied.** The pixel pathway has no
"columns" hint for a container to publish — a tilemap has one, pixels do not — so
the real pixel width lands in the container-info popup for the user to dial in.
Read at the wrong width a TIM shears rather than fails, which is why the number is
worth stating even though nothing acts on it.

Provenance and the wider format family live in
``docs/graphics-formats-reference/psx-tim-formats.md``.
"""

from __future__ import annotations

from dataclasses import dataclass

from celpix.core.capabilities import ContentKind
from celpix.core.context import KEY_PIXEL_PRESET, KEY_SOURCE_OFFSET, PipelineContext
from celpix.core.errors import Stage
from celpix.core.notices import warn
from celpix.plugins.base import (
    ContainerField,
    PluginInfo,
    ReadSource,
    WriteTarget,
    format_size,
    splice,
)

PIXEL_PLUGIN_ID = "container.tim"
CLUT_PLUGIN_ID = "container.tim-clut"

# bytes: the TIM this pathway's payload came out of, so a Save As can put the
# edit back into a copy of it. A bare payload is not a TIM and would not reopen
# as one — the same reason the iNES container keeps its ROM.
KEY_TIM_SOURCE = "tim.source"

_MAGIC = b"\x10\x00\x00\x00"
_BLOCK_HEADER = 12  # bnum, DX, DY, W, H — the same shape for both blocks
_FLAGS_AT = 4
_FIRST_BLOCK = 8
_CLUT_FLAG = 0x08
_PMODE_MASK = 0x07

# Bits per pixel per pmode, and the pixel preset that reads that depth once the
# payload is unwrapped. 4bpp and 8bpp are indices into the CLUT; 16bpp and 24bpp
# carry colour directly and ignore it.
DEPTHS: dict[int, int] = {0: 4, 1: 8, 2: 16, 3: 24}
PRESETS: dict[int, str] = {
    0: "preset.pixel.psx-4bpp",
    1: "preset.pixel.psx-8bpp",
    2: "preset.pixel.dc-bgr555",
    3: "preset.pixel.dc-bgr888",
}

# Every leading eight bytes a TIM can begin with: the magic, then a flags word
# that is one of the four pixel modes with or without the CLUT bit. Spelled out
# as probes because a signature is inert data matched before any plugin runs
# (:class:`~celpix.plugins.base.PluginInfo`), and the magic alone is four bytes
# weak enough to claim any file whose first word happens to be 16.
_MAGIC_PROBES: tuple[tuple[int, bytes], ...] = tuple(
    (0, _MAGIC + bytes([pmode | clut, 0, 0, 0]))
    for clut in (0, _CLUT_FLAG)
    for pmode in DEPTHS
)


def _fail(reason: str) -> ValueError:
    return ValueError(f"TIM: {reason}")


@dataclass(frozen=True)
class TimLayout:
    """Where a TIM's two blocks sit and what they declare.

    Parsed once by :func:`parse` and shared by both containers' read, write and
    describe halves, so no two of them can drift over where the payload begins.
    """

    pmode: int
    has_clut: bool
    # The CLUT block. ``clut_start`` addresses the first *entry*, past the block
    # header; all four are 0 for a TIM that carries no CLUT.
    clut_start: int
    clut_width: int  # entries in one palette — 16 at 4bpp, 256 at 8bpp
    clut_count: int  # how many palettes the block holds
    clut_bytes: int
    # The image block, likewise addressed past its own header.
    image_start: int
    image_bytes: int
    units: int  # the block's W: halfwords per row, *not* pixels
    height: int

    @property
    def bpp(self) -> int:
        return DEPTHS[self.pmode]

    @property
    def width(self) -> int:
        """The row width in **pixels**, which is what ``units`` is not.

        A halfword holds four 4bpp pixels, two 8bpp ones, exactly one 16bpp one,
        and two thirds of a 24bpp one — hence the division rather than a fourth
        multiplier for the last.
        """
        return self.units * 16 // self.bpp

    @property
    def pixel_preset(self) -> str:
        return PRESETS[self.pmode]


def parse(raw: bytes) -> TimLayout:
    """``raw`` as a TIM, or a :class:`ValueError` naming what disagreed.

    Lengths are read as the file states them and only then checked against what
    the file actually holds, so a truncated dump is reported as truncated rather
    than as some other format.
    """
    if len(raw) < _FIRST_BLOCK:
        raise _fail(f"file is {len(raw)} bytes; a header alone needs {_FIRST_BLOCK}")
    if raw[:4] != _MAGIC:
        raise _fail("file does not begin with the 0x00000010 header word")
    flags = int.from_bytes(raw[_FLAGS_AT : _FLAGS_AT + 4], "little")
    pmode = flags & _PMODE_MASK
    if pmode not in DEPTHS:
        # pmode 4 is the "mixed" mode no retail disc uses; 5-7 are unassigned.
        raise _fail(f"pixel mode {pmode} is not one of the four this reads (0-3)")
    has_clut = bool(flags & _CLUT_FLAG)

    at = _FIRST_BLOCK
    clut_start = clut_width = clut_count = clut_bytes = 0
    if has_clut:
        clut_bytes, clut_width, clut_count = _block(raw, at, "CLUT")
        clut_start = at + _BLOCK_HEADER
        at = clut_start + clut_bytes

    image_bytes, units, height = _block(raw, at, "image")
    return TimLayout(
        pmode=pmode,
        has_clut=has_clut,
        clut_start=clut_start,
        clut_width=clut_width,
        clut_count=clut_count,
        clut_bytes=clut_bytes,
        image_start=at + _BLOCK_HEADER,
        image_bytes=image_bytes,
        units=units,
        height=height,
    )


def _block(raw: bytes, at: int, what: str) -> tuple[int, int, int]:
    """``(payload bytes, W, H)`` of the block whose header starts at ``at``.

    ``bnum`` counts its own 12-byte header, so the payload is what is left of it.
    A block whose ``bnum`` is under the header size is rejected outright: a zero
    there is what a file of noise gives, and taking it at face value would put
    the next block on top of this one's header.
    """
    if at + _BLOCK_HEADER > len(raw):
        raise _fail(f"file ends inside the {what} block header")
    bnum = int.from_bytes(raw[at : at + 4], "little")
    if bnum < _BLOCK_HEADER:
        raise _fail(f"{what} block declares {bnum} bytes, less than its own header")
    width = int.from_bytes(raw[at + 8 : at + 10], "little")
    height = int.from_bytes(raw[at + 10 : at + 12], "little")
    return bnum - _BLOCK_HEADER, width, height


def _truncated(
    ctx: PipelineContext, source: str, what: str, want: int, got: int
) -> None:
    warn(
        ctx,
        f"{what.capitalize()} block runs past the end of the file",
        f"The header declares {want} bytes of {what} data and only\n"
        f"{got} are here, so what is shown stops short. Either the\n"
        "dump is cut off or its header is wrong; a save writes back\n"
        "only as far as the file goes.",
        source,
    )


class TimContainer:
    """A TIM read as **pixels**: past both block headers to the image payload.

    ``read`` returns the image block's payload alone — the CLUT ahead of it is a
    palette, and left in place it would decode as several rows of tiles that are
    not part of the picture. The depth the header states seeds the pixel picker
    (``KEY_PIXEL_PRESET``), since the four TIM depths look alike enough as bytes
    that a wrong guess reads as plausible garbage rather than as nothing.

    ``write`` recomputes the payload's span from the **destination's** own header
    rather than trusting the offset the read published, by the rule the rest of
    the container layer keeps: that offset addresses unwrapped bytes and says
    nothing about where wrapped ones belong. Everything outside the payload —
    both headers and the CLUT — is spliced around and survives untouched.
    """

    info = PluginInfo(
        id=PIXEL_PLUGIN_ID,
        name="PlayStation TIM texture",
        stage=Stage.CONTAINER,
        extensions=(".tim",),
        magic=_MAGIC_PROBES,
        short_name="TIM",
        content_kinds=(ContentKind.PIXELS,),
    )

    def read(self, source: ReadSource, ctx: PipelineContext) -> bytes:
        layout = parse(source.data)
        ctx.set(KEY_SOURCE_OFFSET, layout.image_start)
        ctx.set(KEY_PIXEL_PRESET, layout.pixel_preset)
        # Kept whole rather than as the pieces around the payload: a Save As
        # splices into it by exactly the rule an in-place save uses, so the two
        # directions cannot drift over where the pixels belong.
        ctx.set(KEY_TIM_SOURCE, source.data)
        end = layout.image_start + layout.image_bytes
        if end > len(source.data):
            _truncated(
                ctx,
                PIXEL_PLUGIN_ID,
                "image",
                layout.image_bytes,
                len(source.data) - layout.image_start,
            )
        return source.data[layout.image_start : end]

    def write(self, data: bytes, dest: WriteTarget, ctx: PipelineContext) -> bytes:
        existing = _destination(dest, ctx)
        layout = parse(existing)
        return splice(existing, layout.image_start, data[: layout.image_bytes])

    def describe(
        self, source: ReadSource, ctx: PipelineContext
    ) -> tuple[ContainerField, ...]:
        layout = parse(source.data)
        return (
            ContainerField(
                "Pixel mode",
                f"{layout.pmode} - {layout.bpp}bpp"
                f"{' indexed' if layout.bpp <= 8 else ' direct'}",
                "The depth the header states, from bits 0-2 of the flags\n"
                "word. It picks the pixel format the payload is read at;\n"
                "the four depths look alike as bytes, so a wrong guess\n"
                "reads as plausible garbage rather than as nothing.",
            ),
            ContainerField(
                "Image size",
                f"{layout.width}x{layout.height} pixels"
                f" ({layout.units} halfwords per row)",
                "The header quotes the row width in 16-bit VRAM units,\n"
                "not pixels, so the real width is four times it at 4bpp\n"
                "and twice at 8bpp. Set the view to this many pixels\n"
                "across; read narrower, the picture shears into stripes.",
            ),
            ContainerField(
                "Pixels",
                f"{format_size(layout.image_bytes)} at {layout.image_start:#08x}",
                "Where the image block's payload begins, past its own\n"
                "12-byte header and past the CLUT block ahead of it.\n"
                "Every address in the view is anchored here.",
            ),
            _clut_field(layout),
        )


class TimClutContainer:
    """The same file read as a **palette**: the CLUT block's entries.

    A separate container rather than a second mode of the one above, because
    celPix picks a container per content kind: this one is what the *palette*
    pathway detects when pointed at a TIM, and the two unwrap the same file to
    different halves of it.

    The entries are BGR555 with `STP` in bit 15, so ``preset.palette.bgr555``
    reads them as they stand. It cannot *write* that bit — celPix's palette is
    ARGB and holds no flag — so :meth:`write` restores each entry's STP from the
    destination it is saving into rather than letting a colour edit clear all of
    them.

    A TIM with no CLUT (16bpp and 24bpp carry colour in the pixels) has nothing
    here, and reading one says so instead of returning the image bytes as if they
    were colours.
    """

    info = PluginInfo(
        id=CLUT_PLUGIN_ID,
        name="PlayStation TIM color table (CLUT)",
        stage=Stage.CONTAINER,
        extensions=(".tim",),
        magic=_MAGIC_PROBES,
        short_name="TIM CLUT",
        content_kinds=(ContentKind.PALETTE,),
    )

    def read(self, source: ReadSource, ctx: PipelineContext) -> bytes:
        layout = parse(source.data)
        if not layout.has_clut:
            raise _fail(
                f"this {layout.bpp}bpp file carries no color table - its pixels "
                "hold color directly, so there is no palette to read"
            )
        ctx.set(KEY_SOURCE_OFFSET, layout.clut_start)
        ctx.set(KEY_TIM_SOURCE, source.data)
        end = layout.clut_start + layout.clut_bytes
        if end > len(source.data):
            _truncated(
                ctx,
                CLUT_PLUGIN_ID,
                "color table",
                layout.clut_bytes,
                len(source.data) - layout.clut_start,
            )
        return source.data[layout.clut_start : end]

    def write(self, data: bytes, dest: WriteTarget, ctx: PipelineContext) -> bytes:
        existing = _destination(dest, ctx)
        layout = parse(existing)
        if not layout.has_clut:
            raise _fail(
                "the file being saved into carries no color table, so there is "
                "nowhere to put these colors"
            )
        entries = data[: layout.clut_bytes]
        return splice(
            existing,
            layout.clut_start,
            _restore_stp(entries, existing[layout.clut_start :]),
        )

    def describe(
        self, source: ReadSource, ctx: PipelineContext
    ) -> tuple[ContainerField, ...]:
        layout = parse(source.data)
        fields = [_clut_field(layout)]
        if layout.has_clut:
            fields.append(
                ContainerField(
                    "Semi-transparency bits",
                    "preserved on save",
                    "Bit 15 of every entry is the GPU's STP flag, not part\n"
                    "of the color. celPix's palette is plain ARGB and has\n"
                    "nowhere to keep it, so a save carries each entry's bit\n"
                    "over from the file rather than clearing all of them.",
                )
            )
        return tuple(fields)


def _clut_field(layout: TimLayout) -> ContainerField:
    """The CLUT row both containers show, whether or not the file has one."""
    if not layout.has_clut:
        return ContainerField(
            "Color table",
            "none - the pixels carry color directly",
            "Bit 3 of the flags word is clear, which at this depth is\n"
            "what it should be: a 16bpp or 24bpp TIM stores color in\n"
            "the pixel and indexes nothing.",
        )
    total = layout.clut_width * layout.clut_count
    return ContainerField(
        "Color table",
        f"{layout.clut_count} x {layout.clut_width} entries"
        f" at {layout.clut_start:#08x} ({total} total)",
        "BGR555 entries, bit 15 being the GPU's semi-transparency\n"
        "flag rather than color. A file may carry several palettes\n"
        "for one image; they follow each other end to end, so the\n"
        "second starts one palette's width into this block.",
    )


def _restore_stp(entries: bytes, original: bytes) -> bytes:
    """``entries`` with each 16-bit value's bit 15 taken from ``original``.

    The colour codec builds an entry from its masks alone, so every bit outside
    them — here the STP flag — comes back zero. Restoring from the destination
    keeps a colour edit to the colour: entries past the end of ``original``, and a
    trailing odd byte, are left as the codec wrote them because there is no older
    bit to carry over.
    """
    out = bytearray(entries)
    for at in range(0, len(out) - 1, 2):
        if at + 1 < len(original):
            out[at + 1] |= original[at + 1] & 0x80
    return bytes(out)


def _destination(dest: WriteTarget, ctx: PipelineContext) -> bytes:
    """The TIM a write is splicing into: the destination, or the source copied.

    A Save As has no file at the destination, and writing the payload alone would
    leave a bare image with no header — not a TIM, and it would not reopen as
    one. So the read stashed the file it came from and a copy of that is what the
    edit lands in, header, CLUT and all.
    """
    if dest.existing:
        return dest.existing
    source = ctx.get(KEY_TIM_SOURCE)
    if not isinstance(source, (bytes, bytearray)):
        raise _fail(
            "no file to write into: a TIM can only be saved back through the "
            "one it was loaded from, its header being what makes it a TIM"
        )
    return bytes(source)
