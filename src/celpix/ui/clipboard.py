"""The clipboard bridge: tiles ⇄ the system clipboard.

A copy goes onto the OS clipboard in **two representations at once**, and which
one a paste uses decides how faithful it is:

- ``application/x-celpix-tiles`` — the tiles themselves, as indices (or ARGB for
  a direct-color codec) plus the palette they were seen through. Pasting this
  back into celPix is lossless: indices are the data, and a same-format paste
  moves them verbatim rather than round-tripping them through color.
- **An image**, so every other program on the machine sees a normal picture. Qt
  converts it to whatever the receiving app asks for (PNG, DIB, …).

Pasting reverses the priority: the celPix payload if it is there, otherwise any
image on the clipboard, which enters through the Qt-free import pathway
(:mod:`celpix.pipeline.importer`) and is fitted to the target palette. That is
what makes "draw a sprite in an image editor, paste it into the ROM" work.

This module is the **Qt bridge alone** — what goes on the clipboard and what comes
off it. The tile flavour's own byte format, and the validation that makes reading
one safe, are :class:`~celpix.core.tilepayload.TilePayload`'s: a payload arrives
from outside the process, so parsing it belongs with the rest of the model where
it can be tested without a window.
"""

from __future__ import annotations

import json
import re

from PySide6.QtCore import QByteArray, QMimeData
from PySide6.QtGui import QGuiApplication, QImage

from celpix.core.argb_grid import ArgbGrid
from celpix.core.tilepayload import TilePayload

# Our own clipboard flavours. Both names are private MIME types — no other
# program claims them, so their presence proves the copy came from celPix. Each
# flavour's payload carries a version bumped only on an incompatible change; a
# mismatch is ignored on paste, which falls back on the interchange
# representation alongside it (an image for tiles, hex text for colors).
TILES_MIME = "application/x-celpix-tiles"
# Palette colors travel under their own type (lossless ARGB) *and* as
# ``#RRGGBB``/``#AARRGGBB`` text, so a color copies to and pastes from any other
# program that speaks hex.
PALETTE_MIME = "application/x-celpix-palette"
PALETTE_PAYLOAD_VERSION = 1

# A 6- or 8-digit hex run, optionally ``#``-prefixed, not embedded in a longer
# hex string — how a foreign clipboard's colors are recognised.
_HEX_COLOR = re.compile(
    r"(?<![0-9A-Fa-f])#?([0-9A-Fa-f]{8}|[0-9A-Fa-f]{6})(?![0-9A-Fa-f])"
)


def put(payload: TilePayload | None, image: QImage) -> None:
    """Place a copy on the system clipboard in both representations."""
    mime = QMimeData()
    if not image.isNull():
        mime.setImageData(image)
    if payload is not None:
        mime.setData(TILES_MIME, QByteArray(payload.to_bytes()))
    QGuiApplication.clipboard().setMimeData(mime)


def take_payload() -> TilePayload | None:
    """The celPix tile payload on the clipboard, if a celPix copy put one there."""
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


# -- palette colors --------------------------------------------------------
def color_text(argb: int) -> str:
    """One color as ``#RRGGBB`` (opaque) or ``#AARRGGBB`` (carries alpha)."""
    argb &= 0xFFFFFFFF
    if (argb >> 24) == 0xFF:
        return f"#{argb & 0xFFFFFF:06X}"
    return f"#{argb:08X}"


def put_colors(colors: list[int]) -> None:
    """Place palette colors on the system clipboard, lossless + as hex text."""
    mime = QMimeData()
    payload = json.dumps(
        {
            "version": PALETTE_PAYLOAD_VERSION,
            "colors": [c & 0xFFFFFFFF for c in colors],
        }
    ).encode("utf-8")
    mime.setData(PALETTE_MIME, QByteArray(payload))
    mime.setText(" ".join(color_text(c) for c in colors))
    QGuiApplication.clipboard().setMimeData(mime)


def _parse_palette_payload(raw: bytes) -> list[int] | None:
    """Our own palette payload → ARGB list; None for anything malformed."""
    try:
        head = json.loads(raw.decode("utf-8"))
        if head.get("version") != PALETTE_PAYLOAD_VERSION:
            return None
        return [int(c) & 0xFFFFFFFF for c in head["colors"]]
    except (ValueError, KeyError, TypeError, UnicodeDecodeError):
        return None


def _parse_hex_colors(text: str) -> list[int]:
    """Every ``#RRGGBB``/``#AARRGGBB`` token in ``text`` as ARGB (6-digit → opaque).

    The cross-application path: a color copied from any editor that writes hex
    pastes straight in, and a run of them fills consecutive entries.
    """
    colors = []
    for match in _HEX_COLOR.finditer(text):
        digits = match.group(1)
        value = int(digits, 16)
        if len(digits) == 6:
            value |= 0xFF000000  # no alpha field means fully opaque
        colors.append(value & 0xFFFFFFFF)
    return colors


def take_colors() -> list[int] | None:
    """Palette colors from the clipboard: our lossless payload, else hex text."""
    mime = QGuiApplication.clipboard().mimeData()
    if mime is None:
        return None
    if mime.hasFormat(PALETTE_MIME):
        colors = _parse_palette_payload(bytes(mime.data(PALETTE_MIME)))
        if colors:
            return colors
    if mime.hasText():
        colors = _parse_hex_colors(mime.text())
        if colors:
            return colors
    return None


def has_colors() -> bool:
    """Whether a palette paste could do anything — drives the action's enabled state."""
    mime = QGuiApplication.clipboard().mimeData()
    if mime is None:
        return False
    if mime.hasFormat(PALETTE_MIME):
        return True
    return mime.hasText() and bool(_parse_hex_colors(mime.text()))


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
