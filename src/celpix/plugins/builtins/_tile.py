"""Shared tile-codec plumbing: the guards, and the two shapes every codec ends on.

A tile codec's body is its format's bit rule. Everything around it — checking the
buffer divides into tiles, checking the caller handed over tiles of the right
size, cutting row buffers into grids, laying grids back out flat — is the same in
all of them, so it lives here rather than being retyped per format.
"""

from __future__ import annotations

from celpix.core.index_grid import IndexGrid


def require_whole_tiles(data_len: int, tile_bytes: int) -> None:
    """Raise if ``data_len`` isn't a whole number of ``tile_bytes`` tiles (decode)."""
    if tile_bytes <= 0 or data_len % tile_bytes != 0:
        raise ValueError(
            f"data length {data_len} is not a multiple of tile size {tile_bytes}"
        )


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
