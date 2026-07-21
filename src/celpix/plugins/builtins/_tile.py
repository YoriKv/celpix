"""Shared codec guards: the two validation checks every tile codec repeats."""

from __future__ import annotations


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
