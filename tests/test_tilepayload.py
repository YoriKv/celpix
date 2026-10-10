"""The clipboard's tile payload: round trip, and refusal of anything foreign."""

from __future__ import annotations

from celpix.core.index_grid import IndexGrid
from celpix.core.tilepayload import TilePayload


def test_payload_round_trips_and_rejects_junk() -> None:
    tiles = [IndexGrid(2, 2, bytes([0, 1, 2, 3]))]
    payload = TilePayload.from_tiles(tiles, (0xFF000000, 0xFFFFFFFF))
    raw = payload.to_bytes()
    assert TilePayload.from_bytes(raw) == payload
    assert TilePayload.from_bytes(raw).tiles() == tiles
    # The clipboard is shared with the whole machine: anything malformed has to
    # read as "no payload", never as a torn grid.
    assert TilePayload.from_bytes(raw[:-1]) is None
    assert TilePayload.from_bytes(b"") is None
    assert TilePayload.from_bytes(b"not a celpix payload") is None
    # A header that parses as JSON but is no object — nor finishes parsing.
    for head in (b"[1]", b"1", b'"x"', b"[" * 100_000):
        assert TilePayload.from_bytes(len(head).to_bytes(4, "little") + head) is None
