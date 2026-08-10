"""A run of tiles as a self-describing byte string — the clipboard's tile flavour.

The serialisation half of a copy/paste, kept apart from the clipboard itself
because it is not a Qt concern: a small JSON header plus raw pixel bytes, with
every field validated on the way back in. What puts it on the system clipboard
and takes it off again is :mod:`celpix.ui.clipboard`.

Deliberately **not** a pickle. The clipboard is shared with every other program
on the machine, so a payload arriving from outside this process must never be
able to execute anything; a header this reader parses by hand can only fail to
"nothing to paste". :meth:`TilePayload.from_bytes` therefore checks the declared
geometry against the actual byte count rather than trusting either, and answers
``None`` for anything truncated, hand-edited or simply foreign.

The header is versioned, and a mismatch is ignored rather than repaired: a copy
made by a different celPix falls back on the image representation travelling
beside it, which is the honest result when the two builds disagree about what the
bytes mean. Qt-free, so the parse is testable without a window.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from celpix.core.argb_grid import ArgbGrid
from celpix.core.index_grid import IndexGrid

# Bumped only on an incompatible payload change — see the module docstring for
# what a mismatch costs.
TILES_PAYLOAD_VERSION = 1


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
        direct = tiles[0].bytes_per_pixel == 4
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
                "version": TILES_PAYLOAD_VERSION,
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
            if head.get("version") != TILES_PAYLOAD_VERSION:
                return None
            tw, th = int(head["tile_width"]), int(head["tile_height"])
            count = int(head["count"])
            direct = bool(head["direct_color"])
            colors = tuple(int(c) & 0xFFFFFFFF for c in head["colors"])
            # Optional, so a payload without it (any copy that carries no block
            # shape) reads back as the single row a linear paste lays down.
            columns = int(head.get("columns") or count)
        except (ValueError, KeyError, TypeError, UnicodeDecodeError):
            return None
        data = raw[4 + size :]
        if tw <= 0 or th <= 0 or count <= 0:
            return None
        if len(data) != count * tw * th * (4 if direct else 1):
            return None
        return cls(tw, th, count, direct, colors, data, max(1, min(columns, count)))
