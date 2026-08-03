"""The interpreted pixel model: a grid of palette indices.

An :class:`IndexGrid` is the codec-neutral "framebuffer" the whole editor works
in — one **palette index per pixel**, row-major, decoupled from any palette (see
``docs/graphics-formats-reference/implementation-guide.md`` §1). Decoding bytes
produces index grids; rendering turns indices into color on the ``ui`` side;
editing paints indices; saving encodes indices back to bytes.

The same class serves as both a single tile (e.g. 8x8) and a composed image made
of many tiles, so tile codecs and the canvas share one type. It is deliberately
Qt-free.
"""

from __future__ import annotations

from functools import lru_cache

from celpix.core.grid import PixelGrid


@lru_cache(maxsize=256)
def _shift(bias: int) -> bytes:
    """The 256-byte translation map that adds ``bias`` to an index, saturating.

    Cached because a window holds at most a handful of distinct biases (one per
    pinned subpalette row on screen) and rebuilding the map per tile would cost
    more than the translate it feeds.

    Saturating at **both** ends. Past 255 is unreachable once the row is clamped
    to the palette (``row * 2**bpp + index <= 255`` always holds then), so that
    end only keeps a hand-edited project file from raising instead of rendering.
    Below 0 is reached by a **negative** bias, which is how a pixel edit made
    through a tilemap takes the cell's palette row back out of an index on the way
    to the tile that stores it (``docs/design/tilemap-entry.md`` §8.1) — and there
    the clamp is the meaning rather than a guard: a pixel cleared to palette index
    0 lands on the tile's own index 0, which is what "empty" is in every one of
    these formats.
    """
    return bytes(max(0, min(255, i + bias)) for i in range(256))


@lru_cache(maxsize=256)
def _fold(row_size: int) -> bytes:
    """The 256-byte map that reduces an absolute index to its place in its row.

    The inverse of :func:`_shift` for the one direction that has to be lossless:
    taking a **composed** pixel back to the index its tile stores. A composed
    index is ``row * row_size + stored``, and what a tile can hold is the
    ``stored`` part, so the row comes off by remainder rather than by subtraction.

    Subtracting a *particular* row's base is right only while the pixel came from
    that row. It does not, whenever a gesture **relocates** pixels rather than
    painting them — a float dragged onto a cell of another row, a paste, a
    marquee flip spanning two rows — and there the subtraction went negative and
    :func:`_shift` clamped it to 0, blanking every pixel whose colour sat below
    the destination's base. Reducing instead keeps the pattern and lets the
    destination cell's row recolour it, which is what moving art between palette
    rows means in every editor of this kind.
    """
    return bytes(i % max(1, row_size) for i in range(256))


class IndexGrid(PixelGrid):
    """A row-major grid of 8-bit palette indices.

    Indices are plain ints in ``0..255``; the meaningful range for a given
    interpretation is ``0..2**bpp - 1``, but the grid itself does not enforce a
    bit depth — that is the codec's concern.
    """

    __slots__ = ()

    bytes_per_pixel = 1

    def get(self, x: int, y: int) -> int:
        return self._data[y * self._width + x]

    def set(self, x: int, y: int, value: int) -> None:
        # Indices are one byte; mask so a caller passing a wider int can't corrupt
        # neighbouring pixels via bytearray's range check.
        self._data[y * self._width + x] = value & 0xFF

    def shifted(self, bias: int) -> IndexGrid:
        """A new grid with every index moved up by ``bias``.

        How a pinned subpalette reaches the screen: the render bridge builds one
        colour table for the *whole* image, so a tile that must render through a
        different palette row cannot be given its own table — the row moves into
        the indices instead (``docs/design/palette-editing.md``). Purely a
        rendering step; nothing that writes bytes ever sees a shifted grid.

        Done with :meth:`bytes.translate` against a cached 256-byte map rather
        than a per-pixel loop: a refresh shifts every visible tile, and at C speed
        that is a memcpy-shaped cost instead of a Python one. Indices that would
        run past 255 saturate — unreachable once the row is clamped to the palette
        (``row * 2**bpp + index <= 255`` always holds then), so this only keeps a
        hand-edited project file from raising instead of rendering.
        """
        if not bias:
            return self
        return IndexGrid(self._width, self._height, self._data.translate(_shift(bias)))

    def folded(self, row_size: int) -> IndexGrid:
        """A new grid with every index reduced to its position within its row.

        How a **composed** pixel becomes the index a tile stores, and the exact
        inverse of :meth:`shifted` for indices the shift produced
        (:func:`_fold`). Used on the way from a gesture to the bank, where
        subtracting the destination cell's own row is only right for pixels that
        were painted there — not for pixels moved in from a cell of another row,
        which is where the subtraction used to go negative and clamp to nothing.
        """
        if row_size <= 0 or row_size >= 256:
            return self
        return IndexGrid(
            self._width, self._height, self._data.translate(_fold(row_size))
        )
