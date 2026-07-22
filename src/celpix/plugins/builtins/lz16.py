"""LZ16 — the Yoshi's Island Super FX bit-stream graphics codec.

Unlike the byte-oriented LZ1/LZ2 family, LZ16 is a predictive bit-stream codec
specialized to 4bpp tile graphics. The uncompressed unit is a **tile row**: 16
SNES 4bpp tiles = 512 bytes = a 128-pixel-wide, 8-pixel-tall strip. The stream
carries no row count of its own — the decoder must be told how many tile rows to
produce, and consumes bits until it has decoded them.

Stream shape (full details + provenance:
``docs/graphics-formats-reference/implementation-guide.md`` §6):

- **Header** — 4 bytes = 8 nibbles: the first 7 seed the predictor palette
  ``pred[0..6]`` (4-bit colours); the 8th primes the LSB-first bit stream.
  ``pred[7]`` is a dynamic slot refreshed inline by an escape code.
- **Rows** — each 128-pixel row starts with one mode bit (0 = pure RLE row,
  1 = delta row edited against a copy of the previous row), then run commands
  walking a cursor right-to-left: a unary-style count followed by one of four
  ops (fill with a predictor colour / skip unchanged runs / a run grew /
  a run shrank).
- After all rows decode, the row-major pixels transpose into standard SNES
  4bpp planar tiles.

The row count normally comes from context outside the stream (game pointer
tables). When the caller doesn't know it, :func:`probe_rows` recovers it from
the compressed length: decode consumption grows strictly with the row count, so
at most one count consumes the buffer exactly. That only works when the buffer
ends exactly where the compressed structure does — an over-read buffer (offset
to end-of-file) defeats it, which is why the plugin lets a context key supply
the count explicitly.

The compressor mirrors the scheme the original tooling used: encode every row
both ways (RLE and delta), keep the shorter, and choose the 7 predictor colours
by ranking. Two rankings are tried — colour-change frequency across the delta
encoding (the original's rule) and maximal-run frequency — and the smaller
result wins. Byte-identity with any particular original blob is a non-goal;
round-tripping is the contract.
"""

from __future__ import annotations

from celpix.core.context import (
    KEY_COMPRESSED_SIZE,
    KEY_DECOMPRESS_COMPLETE,
    KEY_DECOMPRESS_PARTIAL,
    PipelineContext,
)
from celpix.core.errors import Stage
from celpix.plugins.base import PluginInfo

# int: LZ16 tile-row count for decode, when known. Recorded back after a
# successful probe so later stages (and a future UI) can see what was used.
KEY_LZ16_ROWS = "lz16.rows"

ROW_PIXELS = 128
TILES_PER_ROW = 16
BYTES_PER_TILE = 32
BYTES_PER_TILE_ROW = TILES_PER_ROW * BYTES_PER_TILE  # 512

# Probe ceiling: 64 tile rows = 32 KB of 4bpp tiles, comfortably past any
# structure the format is used for.
_PROBE_MAX_ROWS = 64


def _fail(reason: str) -> ValueError:
    return ValueError(f"corrupt LZ16 stream: {reason}")


def _decode_pixel_rows(
    data: bytes,
    tile_rows: int,
    stop_len: int | None = None,
    partial: bool = False,
) -> tuple[bytearray, int, int]:
    """The core row decoder: bit stream -> row-major pixels (one byte each).

    Decodes up to ``tile_rows`` tile rows. With ``stop_len`` set, returns early
    at the first tile-row boundary where exactly that many source bytes have
    been consumed (the probe's exact-fit test). With ``partial`` (a bounded
    buffer that may cut the stream short), any decode failure past the first
    complete tile row returns the rows completed so far instead of raising —
    without a terminator or size in the stream, "ran out of buffer" and
    "decoded into trailing garbage" are indistinguishable, so the first tile
    row doubles as the validity test. Returns
    ``(pixels, tile_rows_done, consumed)``.
    """
    n = len(data)
    if n < 4:
        raise _fail("shorter than the 4-byte header")

    total_rows = tile_rows * 8
    pixels = bytearray(ROW_PIXELS * total_rows)

    # LSB-first bit reader. `pos` is the index of the next unread byte; the
    # low `bit_count` bits of `bit_buf` are already-fetched unread bits.
    pos = 4
    header = int.from_bytes(data[0:4], "little")
    pred = bytearray(8)
    for k in range(7):
        pred[k] = (header >> (4 * k)) & 0xF
    bit_buf = (header >> 28) & 0xF
    bit_count = 4

    def read_bit() -> int:
        nonlocal pos, bit_buf, bit_count
        if bit_count == 0:
            if pos >= n:
                raise _fail("source exhausted mid-stream")
            bit_buf = data[pos]
            pos += 1
            bit_count = 8
        bit = bit_buf & 1
        bit_buf >>= 1
        bit_count -= 1
        return bit

    def read_bits_msb(count: int) -> int:
        value = 0
        for _ in range(count):
            value = (value << 1) | read_bit()
        return value

    # Snapshot after each *complete* tile row so partial mode can rewind a
    # failure mid-row to the last whole row (pixels past it are junk-in-progress).
    done_rows = 0
    done_pos = 0
    try:
        for row in range(total_rows):
            base = row * ROW_PIXELS
            if row > 0:
                pixels[base : base + ROW_PIXELS] = pixels[base - ROW_PIXELS : base]
            row_mode = read_bit()
            cursor = ROW_PIXELS - 1

            while 0 <= cursor < ROW_PIXELS:
                # Unary-style count: pairs (continue, bit); a 0 terminates and
                # ORs an implicit 1 at the current magnitude.
                count = 0
                bit_mask = 1
                while True:
                    if read_bit() == 0:
                        break
                    if read_bit():
                        count |= bit_mask
                    bit_mask <<= 1
                    if bit_mask > ROW_PIXELS:
                        raise _fail("run count overflows a row")
                count |= bit_mask

                mode = 1 if row_mode == 0 else read_bits_msb(2)

                if mode == 1:  # fill with a predictor colour
                    pred_idx = read_bits_msb(3)
                    if pred_idx == 7:
                        pred[7] = read_bits_msb(4)
                    if cursor + 1 < count:
                        raise _fail("predictor fill runs past the row start")
                    fill = pred[pred_idx]
                    start = base + cursor - count + 1
                    pixels[start : start + count] = bytes([fill]) * count
                    cursor -= count
                elif mode == 0:  # skip `count` unchanged runs
                    for _ in range(count):
                        if not 0 <= cursor < ROW_PIXELS:
                            raise _fail("run skip walked out of the row")
                        ref = pixels[base + cursor]
                        cursor -= 1
                        while cursor >= 0 and pixels[base + cursor] == ref:
                            cursor -= 1
                elif mode == 2:  # run grew: extend, carrying the boundary colour
                    ref = pixels[base + cursor]
                    cursor -= 1
                    while cursor >= 0 and pixels[base + cursor] == ref:
                        cursor -= 1
                    if cursor < 0 or cursor + 1 < count:
                        raise _fail("run-grow walked out of the row")
                    saved = pixels[base + cursor]
                    start = base + cursor - count + 1
                    pixels[start : start + count] = bytes([ref]) * count
                    cursor -= count
                    if cursor >= 0:
                        pixels[base + cursor] = saved
                else:  # mode 3 — run shrank: jump the cursor forward
                    ref = pixels[base + cursor]
                    cursor -= 1
                    while cursor >= 0 and pixels[base + cursor] == ref:
                        cursor -= 1
                    saved = pixels[base + cursor] if cursor >= 0 else 0
                    cursor += count
                    if not 0 <= cursor < ROW_PIXELS:
                        raise _fail("run-shrink jumped out of the row")
                    pixels[base + cursor] = saved

            if (row + 1) % 8 == 0:
                done_rows = (row + 1) // 8
                done_pos = pos
                if stop_len is not None and pos == stop_len:
                    return pixels[: done_rows * 8 * ROW_PIXELS], done_rows, pos
    except ValueError:
        if not (partial and done_rows > 0):
            raise
        return pixels[: done_rows * 8 * ROW_PIXELS], done_rows, done_pos

    return pixels, tile_rows, pos


def _pixels_to_tiles(pixels: bytearray, tile_rows: int) -> bytes:
    """Transpose row-major 128-wide pixels into SNES 4bpp planar tiles."""
    out = bytearray(tile_rows * BYTES_PER_TILE_ROW)
    for tile in range(TILES_PER_ROW * tile_rows):
        col = tile & 0xF
        t_row = tile >> 4
        for y in range(8):
            d = tile * BYTES_PER_TILE + y * 2
            s = col * 8 + (t_row * 8 + y) * ROW_PIXELS
            b0 = b1 = b2 = b3 = 0
            for k in range(8):
                px = pixels[s + k]
                shift = 7 - k
                b0 |= (px & 1) << shift
                b1 |= ((px >> 1) & 1) << shift
                b2 |= ((px >> 2) & 1) << shift
                b3 |= ((px >> 3) & 1) << shift
            out[d] = b0
            out[d + 1] = b1
            out[d + 16] = b2
            out[d + 17] = b3
    return bytes(out)


def _tiles_to_pixels(tiles: bytes) -> bytearray:
    """Inverse transpose: SNES 4bpp planar tiles -> row-major pixels."""
    tile_rows = len(tiles) // BYTES_PER_TILE_ROW
    pixels = bytearray(ROW_PIXELS * tile_rows * 8)
    for tile in range(TILES_PER_ROW * tile_rows):
        col = tile & 0xF
        t_row = tile >> 4
        for y in range(8):
            d = tile * BYTES_PER_TILE + y * 2
            s = col * 8 + (t_row * 8 + y) * ROW_PIXELS
            b0 = tiles[d]
            b1 = tiles[d + 1]
            b2 = tiles[d + 16]
            b3 = tiles[d + 17]
            for k in range(8):
                shift = 7 - k
                pixels[s + k] = (
                    ((b0 >> shift) & 1)
                    | (((b1 >> shift) & 1) << 1)
                    | (((b2 >> shift) & 1) << 2)
                    | (((b3 >> shift) & 1) << 3)
                )
    return pixels


def decompress(data: bytes, tile_rows: int) -> tuple[bytes, int]:
    """Decode ``tile_rows`` tile rows; returns ``(tiles, consumed)``."""
    if tile_rows <= 0:
        raise ValueError(f"tile row count must be positive, not {tile_rows}")
    pixels, _, consumed = _decode_pixel_rows(data, tile_rows)
    return _pixels_to_tiles(pixels, tile_rows), consumed


def probe_rows(data: bytes) -> int:
    """Recover the tile-row count of a stream whose exact length is known.

    Consumption is strictly monotonic in the row count, so at most one count
    consumes ``len(data)`` exactly. Raises when none fits — over-read data,
    or not LZ16 at all.
    """
    try:
        _, done, consumed = _decode_pixel_rows(
            data, _PROBE_MAX_ROWS, stop_len=len(data)
        )
    except ValueError:
        consumed = -1
        done = 0
    if consumed == len(data) and done > 0:
        return done
    raise ValueError(
        "cannot determine the LZ16 tile-row count: no row count consumes the "
        "data exactly. Give the read a length that ends exactly at the "
        "compressed structure's last byte, or supply the row count explicitly"
    )


def decompress_partial(data: bytes) -> tuple[bytes, int, int]:
    """Best-effort decode of a bounded buffer (a view window).

    Decodes as many *complete* tile rows as the buffer's bits support, up to
    the probe ceiling. Returns ``(tiles, tile_rows, consumed)``; raises when
    not even the first tile row decodes — the validity test for "this isn't
    LZ16 data at all".
    """
    pixels, rows, consumed = _decode_pixel_rows(data, _PROBE_MAX_ROWS, partial=True)
    return _pixels_to_tiles(pixels, rows), rows, consumed


# -- compression ------------------------------------------------------------


def _run_length(line: bytearray, x: int) -> int:
    """Length of the constant-colour run ending at ``x``, walking left."""
    colour = line[x]
    count = 1
    for k in range(x - 1, -1, -1):
        if line[k] != colour:
            break
        count += 1
    return count


def _emit_number(bits: list[int], value: int) -> None:
    """The count encoding: for top set bit K, pairs (1, bit_k) for k<K, then 0."""
    top = value.bit_length() - 1
    for k in range(top):
        bits.append(1)
        bits.append((value >> k) & 1)
    bits.append(0)


def _emit_colour_coded(bits: list[int], colour: int, palette: bytearray) -> None:
    """3-bit predictor index (MSB-first), or index 7 + the raw 4-bit colour."""
    idx = 7
    for k in range(7):
        if palette[k] == colour:
            idx = k
            break
    bits += ((idx >> 2) & 1, (idx >> 1) & 1, idx & 1)
    if idx == 7:
        bits += ((colour >> 3) & 1, (colour >> 2) & 1, (colour >> 1) & 1, colour & 1)


def _encode_rle_row(line: bytearray, palette: bytearray) -> list[int]:
    """Row mode 0: plain right-to-left RLE of the row."""
    bits = [0]
    x = ROW_PIXELS - 1
    while x >= 0:
        count = _run_length(line, x)
        _emit_number(bits, count)
        _emit_colour_coded(bits, line[x], palette)
        x -= count
    return bits


def _encode_delta_row(now: bytearray, old: bytearray, palette: bytearray) -> list[int]:
    """Row mode 1: delta against the previous row.

    ``old`` is mutated with the same boundary carries the decoder applies —
    pass a disposable copy.
    """
    bits = [1]
    x = ROW_PIXELS - 1
    while x >= 0:
        now_colour = now[x]
        now_cnt = _run_length(now, x)
        old_colour = old[x]
        old_cnt = _run_length(old, x)

        if now_colour != old_colour:  # colour changed
            _emit_number(bits, now_cnt)
            bits += (0, 1)  # mode 1, 2 bits LSB-first pairs (flag & 1, flag & 2)
            _emit_colour_coded(bits, now_colour, palette)
            x -= now_cnt
            continue
        if now_cnt != old_cnt:
            grow = now_cnt - old_cnt
            if grow > 0:  # run grew
                _emit_number(bits, grow)
                bits += (1, 0)  # mode 2
                k = x - now_cnt
                if k >= 0:
                    old[k] = old[x - old_cnt]
            else:  # run shrank
                _emit_number(bits, -grow)
                bits += (1, 1)  # mode 3
                k = x - old_cnt
                old[x - now_cnt] = old[k] if k >= 0 else 0
            x -= now_cnt
            continue
        # runs identical: count how many consecutive runs stay unchanged
        unchanged = 0
        while now_colour == old_colour and now_cnt == old_cnt:
            unchanged += 1
            x -= now_cnt
            if x < 0:
                break
            now_colour = now[x]
            now_cnt = _run_length(now, x)
            old_colour = old[x]
            old_cnt = _run_length(old, x)
        _emit_number(bits, unchanged)
        bits += (0, 0)  # mode 0
    return bits


def _rank_colours(counts: list[int]) -> bytearray:
    """Colours sorted by descending count, ties to the lower colour value."""
    order = bytearray(range(16))
    for a in range(15):
        for b in range(a + 1, 16):
            if counts[order[a]] < counts[order[b]]:
                order[a], order[b] = order[b], order[a]
    return order


def _palette_by_colour_changes(pixels: bytearray, total_rows: int) -> bytearray:
    """Rank colours by delta-mode colour-change frequency (the original rule).

    Replays the delta walk — including its boundary carries — so the counts
    match what :func:`_encode_delta_row` will actually emit.
    """
    counts = [0] * 16
    now = bytearray(ROW_PIXELS)
    old = bytearray(ROW_PIXELS)
    for row in range(total_rows):
        old[:] = now
        now[:] = pixels[row * ROW_PIXELS : (row + 1) * ROW_PIXELS]
        x = ROW_PIXELS - 1
        while x >= 0:
            now_colour = now[x]
            now_cnt = _run_length(now, x)
            old_colour = old[x]
            old_cnt = _run_length(old, x)
            if now_colour != old_colour:
                counts[now_colour] += 1
                x -= now_cnt
                continue
            if now_cnt != old_cnt:
                grow = now_cnt - old_cnt
                if grow > 0:
                    k = x - now_cnt
                    if k >= 0:
                        old[k] = old[x - old_cnt]
                else:
                    k = x - old_cnt
                    old[x - now_cnt] = old[k] if k >= 0 else 0
                x -= now_cnt
                continue
            while now_colour == old_colour and now_cnt == old_cnt:
                x -= now_cnt
                if x < 0:
                    break
                now_colour = now[x]
                now_cnt = _run_length(now, x)
                old_colour = old[x]
                old_cnt = _run_length(old, x)
    return _rank_colours(counts)


def _palette_by_run_frequency(pixels: bytearray, total_rows: int) -> bytearray:
    """Rank colours by how many maximal runs each forms — often favours the
    background colour, which the colour-change ranking under-weights."""
    counts = [0] * 16
    for row in range(total_rows):
        base = row * ROW_PIXELS
        x = 0
        while x < ROW_PIXELS:
            colour = pixels[base + x]
            length = 1
            while x + length < ROW_PIXELS and pixels[base + x + length] == colour:
                length += 1
            counts[colour] += 1
            x += length
    return _rank_colours(counts)


def _encode_with_palette(
    pixels: bytearray, total_rows: int, palette: bytearray
) -> list[int]:
    bits: list[int] = []
    for k in range(7):  # 28-bit header, each colour LSB-first
        colour = palette[k]
        bits += (colour & 1, (colour >> 1) & 1, (colour >> 2) & 1, (colour >> 3) & 1)
    now = bytearray(ROW_PIXELS)
    old = bytearray(ROW_PIXELS)
    for row in range(total_rows):
        old[:] = now
        now[:] = pixels[row * ROW_PIXELS : (row + 1) * ROW_PIXELS]
        rle = _encode_rle_row(now, palette)
        delta = _encode_delta_row(now, bytearray(old), palette)
        bits += rle if len(rle) <= len(delta) else delta
    return bits


def compress(tiles: bytes) -> bytes:
    """Encode 4bpp tile bytes (a whole number of 512-byte tile rows)."""
    if len(tiles) == 0 or len(tiles) % BYTES_PER_TILE_ROW != 0:
        raise ValueError(
            f"LZ16 data must be a positive multiple of {BYTES_PER_TILE_ROW} "
            f"bytes (16 4bpp tiles per tile row); got {len(tiles)}"
        )
    tile_rows = len(tiles) // BYTES_PER_TILE_ROW
    pixels = _tiles_to_pixels(tiles)
    total_rows = tile_rows * 8

    best: list[int] | None = None
    for palette in (
        _palette_by_colour_changes(pixels, total_rows),
        _palette_by_run_frequency(pixels, total_rows),
    ):
        bits = _encode_with_palette(pixels, total_rows, palette)
        if best is None or len(bits) < len(best):
            best = bits

    out = bytearray((len(best) + 7) // 8)
    for k, bit in enumerate(best):
        if bit:
            out[k >> 3] |= 1 << (k & 7)
    return bytes(out)


class Lz16Decompress:
    info = PluginInfo(
        id="decompress.lz16",
        name="LZ16 (Yoshi's Island Super FX)",
        stage=Stage.DECOMPRESS,
    )

    def decompress(self, data: bytes, ctx: PipelineContext) -> bytes:
        rows = ctx.get(KEY_LZ16_ROWS)
        if rows is not None:
            out, consumed = decompress(data, rows)
            complete = True
        elif ctx.get(KEY_DECOMPRESS_PARTIAL):
            # Bounded window: take every complete tile row the buffer holds.
            # Never "complete" — without a terminator the true end is unknown.
            out, rows, consumed = decompress_partial(data)
            complete = False
        else:
            rows = probe_rows(data)
            out, consumed = decompress(data, rows)
            complete = True
        ctx.set(KEY_LZ16_ROWS, rows)
        ctx.set(KEY_COMPRESSED_SIZE, consumed)
        ctx.set(KEY_DECOMPRESS_COMPLETE, complete)
        return out


class Lz16Compress:
    info = PluginInfo(
        id="compress.lz16",
        name="LZ16 (Yoshi's Island Super FX)",
        stage=Stage.COMPRESS,
    )

    def compress(self, data: bytes, ctx: PipelineContext) -> bytes:
        return compress(data)
