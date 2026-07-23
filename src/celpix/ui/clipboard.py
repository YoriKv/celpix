"""The clipboard bridge: tiles ⇄ the system clipboard.

A copy goes onto the OS clipboard in **two representations at once**, and which
one a paste uses decides how faithful it is:

- ``application/x-celpix-tiles`` — the tiles themselves, as indices (or ARGB for
  a direct-color codec) plus the palette they were seen through. Pasting this
  back into Celpix is lossless: indices are the data, and a same-format paste
  moves them verbatim rather than round-tripping them through color.
- **An image**, so every other program on the machine sees a normal picture. Qt
  converts it to whatever the receiving app asks for (PNG, DIB, …).

Pasting reverses the priority: the Celpix payload if it is there, otherwise any
image on the clipboard, which enters through the Qt-free import pathway
(:mod:`celpix.pipeline.importer`) and is fitted to the target palette. That is
what makes "draw a sprite in an image editor, paste it into the ROM" work.

The payload is a small JSON header plus raw pixel bytes, versioned so a future
Celpix can recognise (or reject) an old clipboard. It is deliberately *not* a
pickle: the clipboard is shared with the rest of the machine and must never be
able to execute anything on paste.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from PySide6.QtCore import QByteArray, QMimeData
from PySide6.QtGui import QGuiApplication, QImage

from celpix.core.argb_grid import ArgbGrid
from celpix.core.index_grid import IndexGrid

# Our own clipboard flavour. The name is a private MIME type — no other program
# claims it, so its presence proves the copy came from Celpix.
TILES_MIME = "application/x-celpix-tiles"

# Bumped only on an incompatible payload change; a mismatch is ignored on paste
# (the image representation is still there to fall back on).
PAYLOAD_VERSION = 1


@dataclass(frozen=True)
class TilePayload:
    """A run of tiles as it travels on the clipboard.

    ``data`` is every tile's buffer concatenated in linear slot order — one byte
    per pixel for indices, four for direct color. ``colors`` is the palette
    window the tiles were *seen* through, which is what lets a paste into a
    different palette re-fit them by color instead of by raw index. ``columns``
    is how many cells wide the copy read on screen, so a paste can put a 2×2
    metatile back down as a 2×2 metatile instead of a strip of four tiles.
    """

    tile_width: int
    tile_height: int
    count: int
    direct_color: bool
    colors: tuple[int, ...]
    data: bytes
    columns: int = 1

    @classmethod
    def from_tiles(
        cls, tiles: list, colors: tuple[int, ...], columns: int = 1
    ) -> TilePayload | None:
        """Pack decoded tiles for the clipboard; None for an empty run."""
        if not tiles:
            return None
        direct = getattr(tiles[0], "bytes_per_pixel", 1) == 4
        blob = bytearray()
        for tile in tiles:
            blob += tile.data
        return cls(
            tile_width=tiles[0].width,
            tile_height=tiles[0].height,
            count=len(tiles),
            direct_color=direct,
            colors=tuple(colors),
            data=bytes(blob),
            columns=max(1, min(columns, len(tiles))),
        )

    def tiles(self) -> list:
        """Unpack back into grids of the type the source codec produced."""
        size = self.tile_width * self.tile_height * (4 if self.direct_color else 1)
        kind = ArgbGrid if self.direct_color else IndexGrid
        return [
            kind(
                self.tile_width,
                self.tile_height,
                self.data[i * size : (i + 1) * size],
            )
            for i in range(self.count)
        ]

    @property
    def max_index(self) -> int:
        """The largest index used — how a paste decides whether the indices fit
        the target format, or have to be re-matched by color."""
        return max(self.data) if self.data and not self.direct_color else 0

    def to_bytes(self) -> bytes:
        header = json.dumps(
            {
                "version": PAYLOAD_VERSION,
                "tile_width": self.tile_width,
                "tile_height": self.tile_height,
                "count": self.count,
                "direct_color": self.direct_color,
                "colors": list(self.colors),
                "columns": self.columns,
            }
        ).encode("utf-8")
        return len(header).to_bytes(4, "little") + header + self.data

    @classmethod
    def from_bytes(cls, raw: bytes) -> TilePayload | None:
        """Parse a clipboard payload; None for anything malformed or foreign.

        Every field is validated against the declared geometry before use — the
        bytes come from outside the process, and a truncated or hand-edited
        payload must fail to a plain "nothing to paste", not to a torn grid.
        """
        try:
            if len(raw) < 4:
                return None
            size = int.from_bytes(raw[:4], "little")
            head = json.loads(raw[4 : 4 + size].decode("utf-8"))
            if head.get("version") != PAYLOAD_VERSION:
                return None
            tw, th = int(head["tile_width"]), int(head["tile_height"])
            count = int(head["count"])
            direct = bool(head["direct_color"])
            colors = tuple(int(c) & 0xFFFFFFFF for c in head["colors"])
            # Optional: a copy from a build that predates block-shaped pastes
            # reads back as a single row, its old behaviour.
            columns = int(head.get("columns") or count)
        except (ValueError, KeyError, TypeError, UnicodeDecodeError):
            return None
        data = raw[4 + size :]
        if tw <= 0 or th <= 0 or count <= 0:
            return None
        if len(data) != count * tw * th * (4 if direct else 1):
            return None
        return cls(tw, th, count, direct, colors, data, max(1, min(columns, count)))


def put(payload: TilePayload | None, image: QImage) -> None:
    """Place a copy on the system clipboard in both representations."""
    mime = QMimeData()
    if not image.isNull():
        mime.setImageData(image)
    if payload is not None:
        mime.setData(TILES_MIME, QByteArray(payload.to_bytes()))
    QGuiApplication.clipboard().setMimeData(mime)


def take_payload() -> TilePayload | None:
    """The Celpix tile payload on the clipboard, if a Celpix copy put one there."""
    mime = QGuiApplication.clipboard().mimeData()
    if mime is None or not mime.hasFormat(TILES_MIME):
        return None
    return TilePayload.from_bytes(bytes(mime.data(TILES_MIME)))


def take_image() -> QImage | None:
    """Any image on the clipboard — the cross-application paste path."""
    mime = QGuiApplication.clipboard().mimeData()
    if mime is None or not mime.hasImage():
        return None
    image = QImage(mime.imageData())
    return None if image.isNull() else image


def has_content() -> bool:
    """Whether a paste could do anything — drives the Paste action's enabled state."""
    mime = QGuiApplication.clipboard().mimeData()
    return mime is not None and (mime.hasFormat(TILES_MIME) or mime.hasImage())


def image_to_argb(image: QImage) -> ArgbGrid:
    """Convert a QImage into the Qt-free grid the import pathway takes.

    Converted to ``Format_ARGB32`` first, so one code path handles every source
    format a foreign app might hand over (indexed GIFs, 16-bit, premultiplied),
    and the grid's little-endian ARGB layout then matches Qt's scanlines byte for
    byte. Rows are copied one at a time because ``bytesPerLine`` may exceed
    ``width * 4`` (Qt pads scanlines for alignment).
    """
    src = image.convertToFormat(QImage.Format.Format_ARGB32)
    w, h = src.width(), src.height()
    grid = ArgbGrid(w, h)
    if w == 0 or h == 0:
        return grid
    stride = src.bytesPerLine()
    buf = bytes(src.constBits())
    row_bytes = w * 4
    dst = grid.data
    for y in range(h):
        s0 = y * stride
        dst[y * row_bytes : (y + 1) * row_bytes] = buf[s0 : s0 + row_bytes]
    return grid
