"""A minimal animated-GIF writer: ARGB frames in, GIF89a bytes out.

What the animation player exports a sequence as. Qt reads GIF but has no writer,
and a small encoder here costs less than a dependency for one format: the input
is pixel art that already fits a palette, so there is no quantizing to do — only
the colours actually used, gathered into one global table.

**Transparency is all-or-nothing.** GIF has one transparent index and no partial
alpha, so a pixel whose alpha is 0 becomes that index and every other pixel is
written as its opaque RGB. A blank frame (a step naming a frame the file does not
hold) is all transparent, which is how the player shows it too.

Each frame is a full-size image with disposal "restore to background", so a
transparent pixel shows through to the page rather than to the frame before it.

Qt-free, like the rest of ``core``.
"""

from __future__ import annotations

import struct
from collections.abc import Sequence

# GIF's code width ceiling; the table is cleared when it fills.
_MAX_CODES = 4096

# What a pixel with no alpha is keyed as while the table is built: not a colour
# any opaque pixel can be, so it cannot collide with one.
_CLEAR = -1


def delays_cs(ticks: Sequence[int], rate: int) -> list[int]:
    """Each step's delay in GIF centiseconds, for ``ticks`` at ``rate`` per second.

    Rounded on the **running total** rather than per step, so a run of five-tick
    steps at 60 Hz (8.33 cs each) keeps its overall speed instead of drifting to
    8 cs apiece. Never under 2 cs: browsers treat a delay of 0 or 1 as "use the
    default", which plays such a frame at a tenth of a second — so a one-tick
    step at 60 Hz plays slightly long, the closest GIF can come.
    """
    rate = max(1, rate)
    out: list[int] = []
    elapsed = 0
    shown = 0
    for tick in ticks:
        elapsed += max(1, tick)
        until = round(elapsed * 100 / rate)
        delay = max(2, until - shown)
        out.append(delay)
        shown += delay
    return out


def encode(
    width: int,
    height: int,
    frames: Sequence[tuple[Sequence[int] | None, int]],
) -> bytes:
    """An endlessly looping GIF of ``frames``.

    Each frame is ``(pixels, delay)``: ``pixels`` is ``width * height`` ARGB ints
    (``0xAARRGGBB``), or None for a blank frame, and ``delay`` is in centiseconds.
    Raises ``ValueError`` when the frames use more than 256 colours between them,
    counting transparency as one.
    """
    if width <= 0 or height <= 0 or not frames:
        raise ValueError("a GIF needs a size and at least one frame")
    size = width * height

    # One global table in first-seen order, transparency first when it is used
    # at all, so it can also be the background index a disposal restores to.
    keyed: list[list[int]] = []
    transparent = any(
        pixels is None or any(p >> 24 == 0 for p in pixels) for pixels, _ in frames
    )
    lookup: dict[int, int] = {_CLEAR: 0} if transparent else {}
    colours: list[int] = [0] if transparent else []
    # A sequence names the same frame over and over, and the caller hands a
    # repeat back as the *same* pixels object (``ui.export._step_pixels``), so
    # both per-pixel passes below — the keying here and the compression at the
    # bottom — are done once per distinct frame and shared by identity. A caller
    # that builds a fresh list per step simply gets the work it asked for.
    blank: list[int] = []
    seen: dict[int, list[int]] = {}
    for pixels, _ in frames:
        if pixels is None:
            if not blank:
                blank = [0] * size
            keyed.append(blank)
            continue
        shared = seen.get(id(pixels))
        if shared is not None:
            keyed.append(shared)
            continue
        if len(pixels) != size:
            raise ValueError("frame size does not match the image")
        indices = []
        for p in pixels:
            key = _CLEAR if p >> 24 == 0 else p & 0xFFFFFF
            index = lookup.get(key)
            if index is None:
                index = lookup[key] = len(colours)
                if index >= 256:
                    raise ValueError("more than 256 colours")
                colours.append(key)
            indices.append(index)
        keyed.append(indices)

    bits = max(1, (len(colours) - 1).bit_length())
    table = bytearray()
    for colour in colours + [0] * ((1 << bits) - len(colours)):
        table += bytes(((colour >> 16) & 0xFF, (colour >> 8) & 0xFF, colour & 0xFF))

    out = bytearray(b"GIF89a")
    out += struct.pack("<HHBBB", width, height, 0xF0 | (bits - 1), 0, 0)
    out += table
    # NETSCAPE2.0: loop forever — a sequence is a cycle, as the player plays it.
    out += b"\x21\xff\x0bNETSCAPE2.0\x03\x01\x00\x00\x00"
    compressed: dict[int, bytes] = {}
    for indices, (_, delay) in zip(keyed, frames, strict=True):
        # Graphic control: disposal 2 (restore to background), transparency flag.
        packed = 0x08 | (1 if transparent else 0)
        out += struct.pack("<BBBBHBB", 0x21, 0xF9, 4, packed, delay, 0, 0)
        out += struct.pack("<BHHHHB", 0x2C, 0, 0, width, height, 0)
        min_size = max(2, bits)
        out.append(min_size)
        data = compressed.get(id(indices))
        if data is None:
            data = compressed[id(indices)] = _lzw(indices, min_size)
        for at in range(0, len(data), 255):
            chunk = data[at : at + 255]
            out.append(len(chunk))
            out += chunk
        out.append(0)
    out.append(0x3B)
    return bytes(out)


def _lzw(indices: Sequence[int], min_size: int) -> bytes:
    """GIF's variable-width LZW over ``indices``, packed LSB-first."""
    clear = 1 << min_size
    end = clear + 1
    out = bytearray()
    acc = 0
    nbits = 0

    def emit(code: int, width: int) -> None:
        nonlocal acc, nbits
        acc |= code << nbits
        nbits += width
        while nbits >= 8:
            out.append(acc & 0xFF)
            acc >>= 8
            nbits -= 8

    width = min_size + 1
    codes: dict[int, int] = {}
    next_code = end + 1
    emit(clear, width)
    prefix = indices[0]
    for index in indices[1:]:
        key = (prefix << 8) | index
        found = codes.get(key)
        if found is not None:
            prefix = found
            continue
        emit(prefix, width)
        codes[key] = next_code
        next_code += 1
        # The decoder adds each entry one code later than this side does, so it
        # widens once the *next* code would not fit — one past the power of two.
        if next_code > (1 << width) and width < 12:
            width += 1
        if next_code == _MAX_CODES:
            emit(clear, width)
            codes.clear()
            next_code = end + 1
            width = min_size + 1
        prefix = index
    emit(prefix, width)
    emit(end, width)
    if nbits:
        out.append(acc & 0xFF)
    return bytes(out)
