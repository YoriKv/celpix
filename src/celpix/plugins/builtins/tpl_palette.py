"""`TPL` palette files — a four-byte header that **names its own color format**.

A PC tile-editor palette container, and almost alone among palette files in
stating how its colors are encoded. A raw
``.pal`` records nothing about its own encoding, which is why celPix treats a
palette format as the user's guess everywhere else (:data:`KEY_PALETTE_ERROR`);
this one answers the question outright::

    0x00  3  "TPL"
    0x03  1  format   0 = RGB888 (3 bytes/entry)
                      1 = console master-palette index (1 byte/entry)
                      2 = BGR555 (2 bytes/entry)
    0x04  .. entries

**The entry count is the file's length, not a field.** Entries run to the end of
the file and the reader divides: ``(size - 4) / entry_size``. So a palette
written back shorter than the one read has to *shorten the file*, which is why
:meth:`TplPaletteContainer.write` rebuilds the file as header-plus-payload rather
than splicing into what is there — a splice would leave the tail of the old
palette behind and the count would still describe it.

**The format byte is published, not applied.** It goes on the context as
``KEY_PALETTE_PRESET``, which puts it in the container-info popup where a reader
can see what the file says it is. Adopting it into the palette dock's format
picker automatically is a further step this does not take: unlike the pixel
pathway's ``KEY_PIXEL_PRESET``, a palette entry's format is chosen at
registration time, before any pipeline has run to produce a context.

The three formats map onto codecs celPix already has, so nothing here is a new
color format — only the framing that says which one to use. Full provenance, and
why other readers get this format wrong by assuming type 0, is in
``docs/graphics-formats-reference/tile-layer-pro-formats.md`` §3.
"""

from __future__ import annotations

from celpix.core.capabilities import ContentKind
from celpix.core.context import KEY_PALETTE_PRESET, KEY_SOURCE_OFFSET, PipelineContext
from celpix.core.errors import Stage
from celpix.plugins.base import (
    ContainerField,
    PluginInfo,
    ReadSource,
    WriteTarget,
)

PLUGIN_ID = "container.tpl-palette"

# bytes: the file this palette came out of, so a Save As can rebuild the header.
# The format byte is not derivable from the colors — celPix's palette is ARGB and
# says nothing about the encoding it was read through — so a new file cannot be
# invented, only copied.
KEY_TPL_SOURCE = "tpl.source"

_MAGIC = b"TPL"
HEADER_SIZE = 4

# Format byte -> (color codec, bytes per entry, how the format is named to a
# reader). The three the format defines; anything else is a file this cannot read
# rather than a fourth to guess at.
FORMATS: dict[int, tuple[str, int, str]] = {
    0: ("preset.palette.rgb888", 3, "RGB888"),
    1: ("preset.palette.nes-indexed", 1, "master-palette index"),
    2: ("preset.palette.bgr555", 2, "BGR555"),
}

_MAGIC_PROBES: tuple[tuple[int, bytes], ...] = tuple(
    (0, _MAGIC + bytes([kind])) for kind in FORMATS
)


def _fail(reason: str) -> ValueError:
    return ValueError(f"TPL palette: {reason}")


def parse(raw: bytes) -> tuple[int, int]:
    """``(format byte, entry count)`` for ``raw``, or a :class:`ValueError`.

    The count is derived rather than read, the file having no field for it.
    """
    if len(raw) < HEADER_SIZE:
        raise _fail(f"file is {len(raw)} bytes; the header alone needs {HEADER_SIZE}")
    if raw[:3] != _MAGIC:
        raise _fail("file does not begin with the TPL signature")
    kind = raw[3]
    if kind not in FORMATS:
        raise _fail(
            f"format byte {kind} is not one of the three this reads "
            f"({', '.join(str(k) for k in FORMATS)})"
        )
    return kind, (len(raw) - HEADER_SIZE) // FORMATS[kind][1]


class TplPaletteContainer:
    """A `TPL` palette, read past its header to the entries.

    The header is four bytes and the payload is everything after it, so the read
    is nearly the plain one — what earns it a container is that those four bytes
    would otherwise decode as colors. At one byte an entry they are four spurious
    indices; at two they are two colors that look no less plausible than any
    others, since any pair of bytes does.
    """

    info = PluginInfo(
        id=PLUGIN_ID,
        name="TPL palette (format stated in header)",
        stage=Stage.CONTAINER,
        extensions=(".tpl",),
        magic=_MAGIC_PROBES,
        short_name="TPL",
        content_kinds=(ContentKind.PALETTE,),
    )

    def read(self, source: ReadSource, ctx: PipelineContext) -> bytes:
        kind, _ = parse(source.data)
        ctx.set(KEY_SOURCE_OFFSET, HEADER_SIZE)
        ctx.set(KEY_PALETTE_PRESET, FORMATS[kind][0])
        ctx.set(KEY_TPL_SOURCE, source.data)
        return source.data[HEADER_SIZE:]

    def write(self, data: bytes, dest: WriteTarget, ctx: PipelineContext) -> bytes:
        existing = dest.existing
        if not existing:
            stashed = ctx.get(KEY_TPL_SOURCE)
            if not isinstance(stashed, (bytes, bytearray)):
                raise _fail(
                    "no file to write into: the header's format byte cannot be "
                    "derived from the colors, only copied from the file they "
                    "were read out of"
                )
            existing = bytes(stashed)
        parse(existing)  # refuse to write a header this cannot read back
        # Rebuilt rather than spliced: the entry count *is* the file's length, so
        # a shorter palette has to shorten the file. Splicing would leave the old
        # tail in place and the count would go on describing it.
        return existing[:HEADER_SIZE] + data

    def describe(
        self, source: ReadSource, ctx: PipelineContext
    ) -> tuple[ContainerField, ...]:
        kind, count = parse(source.data)
        preset, entry_size, label = FORMATS[kind]
        leftover = (len(source.data) - HEADER_SIZE) % entry_size
        fields = [
            ContainerField(
                "Color format",
                f"{kind} - {label} ({entry_size} bytes/entry)",
                "This file states its own encoding, which almost no other\n"
                "palette file does. Read through the wrong format the\n"
                "colors are wrong but never obviously so, since any bytes\n"
                f"decode as some color. It names {preset.rsplit('.', 1)[-1]}.",
            ),
            ContainerField(
                "Entries",
                f"{count} at {HEADER_SIZE:#04x}",
                "Counted from the file's length rather than read: the\n"
                "entries run to the end and there is no count field. A\n"
                "save therefore rewrites the file's length rather than\n"
                "splicing into it.",
            ),
        ]
        if leftover:
            fields.append(
                ContainerField(
                    "Trailing bytes",
                    f"{leftover} past the last whole entry",
                    "The payload is not a whole number of entries, so the\n"
                    "file is either truncated or not what its header says.\n"
                    "What is shown stops at the last complete entry.",
                )
            )
        return tuple(fields)
