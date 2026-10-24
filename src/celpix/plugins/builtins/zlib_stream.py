"""zlib — a DEFLATE stream in the RFC 1950 wrapper, through Python's own ``zlib``.

The one scheme here that is a library standard rather than a console's or a
game's: phone and PC-era asset archives store their pictures this way, behind
whatever record header the game wrote. Both directions are the standard
library's binding of zlib itself, so there is no DEFLATE implemented here to get
wrong — only the framing the pipeline needs around it.

A stream carries its own end: DEFLATE's final-block bit, then the Adler-32 of
the output. So the decoder is the length oracle — what the inflater left
unconsumed is past the structure — and a slice created without a length
backfills to exactly the stream. The two-byte header's check bits and the
checksum together also make the structure scan near-silent on bytes that are
not zlib.

**Raw DEFLATE (no header) and gzip are not this plugin.** They are the same
compressed data under a different wrapper, and a stream in either fails the
header check here rather than decoding.

The encoder is ``zlib.compress`` at level 9 — what packers default to when size
matters, and the level whose streams open ``78 DA``. A different packer's level
or strategy gives a different but equally legal stream, so a re-encode can come
out a little longer or shorter than the original; a stream re-packed by the same
library at the same level comes back byte for byte.
"""

from __future__ import annotations

import zlib

from celpix.core.errors import Stage
from celpix.plugins.base import PartialDecompression, PluginInfo

LEVEL = 9
# Memory guard for an unbounded read, not a format limit: the decoder is handed
# the rest of the file and DEFLATE expands up to ~1000x, so a damaged stream or
# a hostile file could otherwise inflate without bound. 16 MiB is far past any
# picture a structure of this kind holds.
_MAX_OUT = 16 * 1024 * 1024


def decompress(data: bytes, *, partial: bool = False) -> tuple[bytes, int, bool]:
    """``(output, consumed, complete)`` for the zlib stream at ``data[0]``.

    ``consumed`` is the stream's extent, Adler-32 trailer included, when it
    completes. A buffer that ends mid-stream raises unless ``partial``, which
    returns what inflated so far for a bounded window preview; corrupt data
    raises either way.
    """
    inflater = zlib.decompressobj()
    try:
        out = inflater.decompress(data, _MAX_OUT)
        if inflater.unconsumed_tail:
            raise ValueError(f"zlib stream inflates past {_MAX_OUT >> 20} MiB")
        if not inflater.eof:
            if not partial:
                raise ValueError("the zlib stream ends before its final block")
            out += inflater.flush()
    except zlib.error as exc:
        raise ValueError(f"not a valid zlib stream: {exc}") from exc
    return out, len(data) - len(inflater.unused_data), inflater.eof


def compress(data: bytes) -> bytes:
    return zlib.compress(data, LEVEL)


class ZlibCompression(PartialDecompression):
    info = PluginInfo(
        id="compression.zlib",
        name="zlib (DEFLATE, RFC 1950)",
        stage=Stage.COMPRESSION,
        category="Generic",
    )

    _decode = staticmethod(decompress)
    _encode = staticmethod(compress)
