"""Shared tile-codec plumbing: the guards, and the two shapes every codec ends on.

A tile codec's body is its format's bit rule. Everything around it — checking the
buffer divides into tiles, checking the caller handed over tiles of the right
size, cutting row buffers into grids, laying grids back out flat — is the same in
all of them, so it lives here rather than being retyped per format.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from celpix.core.index_grid import IndexGrid
from celpix.plugins._params import integer


def tile_dims(
    params: Mapping[str, Any],
    *,
    default: int = 8,
    multiple_of: int = 1,
    why: str = "",
    engine: str,
) -> tuple[int, int]:
    """``(tile_width, tile_height)`` from a preset, both positive integers.

    ``multiple_of`` is the pixel group a row is built from, which the width has
    to be whole groups of; ``why`` says what makes it the unit, for the error.
    ``engine`` names the codec in the messages, since the pipeline reports them
    against an entry rather than a codec.
    """
    width = integer(params, "tile_width", default)
    height = integer(params, "tile_height", default)
    if width <= 0 or height <= 0:
        raise ValueError(f"{engine} tile size must be positive: {width}x{height}")
    if width % multiple_of:
        reason = f" ({why})" if why else ""
        raise ValueError(
            f"{engine} tile_width must be a multiple of {multiple_of}{reason}: "
            f"got {width}"
        )
    return width, height


def require_whole(
    data_len: int, unit: int, *, noun: str = "tile", what: str = "data"
) -> None:
    """Raise if ``data_len`` isn't a whole number of ``unit``-byte units.

    ``what`` names the buffer and ``noun`` its unit in the message — a tile of
    pixel data, an entry of a palette — which is the only thing that differs
    between the codecs that check this.
    """
    if unit <= 0 or data_len % unit != 0:
        raise ValueError(
            f"{what} length {data_len} is not a multiple of {noun} size {unit}"
        )


def require_whole_tiles(data_len: int, tile_bytes: int) -> None:
    """Raise if ``data_len`` isn't a whole number of ``tile_bytes`` tiles (decode)."""
    require_whole(data_len, tile_bytes)


def check_tile_size(grid, width: int, height: int, index: int) -> None:
    """Raise if ``grid`` isn't ``width`` × ``height`` (encode)."""
    if grid.width != width or grid.height != height:
        raise ValueError(
            f"tile {index} is {grid.width}x{grid.height}, expected {width}x{height}"
        )


def tiles_from_rows(
    rows: list[bytes], width: int, height: int, count: int
) -> list[IndexGrid]:
    """Cut ``height`` full-width row buffers into ``count`` per-tile grids.

    Every tile codec decodes a *row of every tile at once*, which is what makes
    the bit shuffle one strided pass instead of a loop per tile, so each of
    ``rows`` holds pixel row ``y`` of every tile back to back. This is the
    transpose back to tiles that ends all of them.
    """
    return [
        IndexGrid(
            width,
            height,
            b"".join(row[t * width : (t + 1) * width] for row in rows),
        )
        for t in range(count)
    ]


def flatten_tiles(tiles: list[IndexGrid], width: int, height: int) -> bytes:
    """Every tile's pixels back to back, after checking each one's size.

    The encode-side mirror of :func:`tiles_from_rows`: the flat buffer is what
    lets a codec pack one plane across the whole run with a strided slice.
    """
    for index, grid in enumerate(tiles):
        check_tile_size(grid, width, height, index)
    return b"".join(bytes(grid.data) for grid in tiles)
