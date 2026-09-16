"""LZ4W — SGDK's word-oriented LZ for the Mega Drive, both directions.

The faster of the two schemes SGDK's resource compiler packs assets with (the
other is :mod:`~celpix.plugins.builtins.aplib`), designed so a 68000 unpacks it
with word moves alone: every length and distance counts **16-bit words**, and
an odd trailing byte rides in the end marker. Homebrew and late-commercial
Mega Drive titles built on SGDK carry tiles, maps, palettes and sprite frames
behind it. Stream shape, all words big-endian::

    block header  u16   LLLL MMMM OOOO OOOO
                        LLLL   literal words that follow the header
                        MMMM   match length - 1, so 2..16 words when non-zero
                        OOOO.. match distance - 1, 1..256 words back
      MMMM = 0, OOOOOOOO = 0    no match (a literal-only block); with
                                LLLL = 0 too it is the **end block**
      MMMM = 0, OOOOOOOO != 0   a *long* match: OOOOOOOO + 2 = 3..257 words,
                                and one more word follows the literals:
                                  X ddddddd dddddddd
                                  distance = ((-d) & 0x7FFF) + 1 words
                                  X = 0  from the output, as usual
                                  X = 1  from the **source**: the bytes that
                                         many words before the current read
                                         position in the compressed stream
    end block     0000  then one word  F... YYYYYYYY
                        F = 1: YYYYYYYY is the payload's last, odd byte

Three things about that are easy to get wrong, and two of them are silent:

- **Everything is in words.** A distance of 1 is two bytes back, a literal
  count of 3 is six bytes, and the stored long distance is the *negated*
  count so the decoder can add it to a pointer.
- **The long-match distance word comes after the literals**, not after the
  header, because the decoder copies the literals out first.
- **A source-relative long match reads the ROM, not the output.** SGDK packs
  each resource against everything it has already laid down in the binary
  and lets a match reach back into that, addressed from the compressed
  stream's own read pointer — so the reference can only be resolved by
  something that knows what precedes the stream.

**Where the preceding bytes come from.** The host publishes the buffer a
stream was cut from and where in it the stream starts
(:data:`~celpix.core.context.KEY_SURROUND`), whenever a position in the stream
is a position in that buffer — a raw ROM, a slice of one, the scan's and the
preview's windows. That covers what SGDK produces without anyone binding
anything. The optional ``previous`` input overrides it, for a stream lifted out
of its ROM into a file of its own, where nothing precedes it. A stream that
copies from before itself with neither available is refused, naming the block,
rather than decoded to something plausible.

**Copies are byte at a time, forward.** A match may be longer than its
distance, re-reading words it has just written, which is what gives the format
run-length encoding for free.

The encoder is a shortest-path parse in words over the two match forms, the
cost being the words a form adds — one for a header, two for a long match —
with a literal costing itself; ties go to the match, because fewer blocks
unpack faster and that is what the format is for. Round-tripping is the
contract, not byte-identity with SGDK's own packer.

**Packing against the preceding bytes is off unless asked for**, through the
``pack_previous`` input: a stream written that way is correct only while the
bytes before it stay what they were, which a ROM guarantees and an editor does
not. Asked for, it does what SGDK does — matches into the preceding data are
long matches with the source flag — and it is what fits a re-encode back into a
slot the original only fit by leaning on its neighbour. A source-relative
distance is measured from the stream's read pointer rather than the output, so
it is only known once the blocks before it are written; one that then does not
fit the field is written as literals instead.

Format provenance and the cross-check against that packer are in
``docs/graphics-formats-reference/implementation-guide.md`` §7.
"""

from __future__ import annotations

from celpix.core.context import (
    KEY_COMPRESSED_SIZE,
    KEY_DECOMPRESS_COMPLETE,
    KEY_DECOMPRESS_PARTIAL,
    KEY_INPUTS,
    KEY_SURROUND,
    KEY_SURROUND_START,
    PipelineContext,
)
from celpix.core.errors import Stage
from celpix.core.notices import warn
from celpix.plugins.base import InputKind, InputSpec, PluginInfo
from celpix.plugins.builtins._lz import MatchFinder, copy_back

WORD = 2

LITERAL_MAX = 15  # words per block header
SHORT_MIN, SHORT_MAX = 2, 16  # words; MMMM + 1
SHORT_MAX_DISTANCE = 256  # words; OOOOOOOO + 1
LONG_MIN, LONG_MAX = 3, 257  # words; OOOOOOOO + 2
LONG_MAX_DISTANCE = 0x4000  # words: what the negated 15-bit field reaches
SOURCE_FLAG = 0x8000

INPUT_PREVIOUS = "previous"
INPUT_PACK_PREVIOUS = "pack_previous"

OP_LITERAL, OP_SHORT, OP_LONG, OP_SOURCE = range(4)


def _fail(reason: str) -> ValueError:
    return ValueError(f"corrupt LZ4W stream: {reason}")


class _Truncated(Exception):
    """The stream ran out mid-block — recoverable only under ``partial``."""


def decompress(
    data: bytes, *, previous: bytes | memoryview | None = None, partial: bool = False
) -> tuple[bytes, int, bool]:
    """Unpack one stream from ``data[0]``.

    ``previous`` is what precedes the stream in ROM, for source-relative
    matches; without it such a match is an error. Returns ``(plain, consumed,
    complete)``; ``complete`` is false only when the buffer ran out before the
    end block, which ``partial`` downgrades from an error to a short result.
    """
    n = len(data)
    pos = 0
    out = bytearray()
    complete = False

    def word() -> int:
        nonlocal pos
        if pos + WORD > n:
            raise _Truncated
        value = (data[pos] << 8) | data[pos + 1]
        pos += WORD
        return value

    def check_reach(distance: int) -> None:
        if distance * WORD > len(out):
            raise _fail(
                f"match reaches {distance:,} words back "
                f"into {len(out) // WORD:,} words of output"
            )

    try:
        while True:
            header = word()
            literals = header >> 12
            match = (header >> 8) & 0xF
            operand = header & 0xFF
            if not header:
                tail = word()
                if tail & SOURCE_FLAG:
                    out.append(tail & 0xFF)
                complete = True
                break
            if literals:
                if pos + literals * WORD > n:
                    raise _Truncated
                out += data[pos : pos + literals * WORD]
                pos += literals * WORD
            if match:
                distance = operand + 1
                check_reach(distance)
                copy_back(out, distance * WORD, (match + 1) * WORD)
            elif operand:
                length = operand + 2
                stored = word()
                distance = ((-stored) & 0x7FFF) + 1
                if stored & SOURCE_FLAG:
                    # Addressed from the read pointer, which is now past the
                    # distance word itself.
                    at = pos - distance * WORD
                    if previous is None:
                        raise _fail(
                            f"the block at {pos - 2 * WORD:#x} copies from "
                            f"{distance:,} words before the stream's read "
                            "position - the data that precedes the stream in "
                            "ROM, which nothing here supplies"
                        )
                    before = len(previous)
                    at += before
                    end = at + length * WORD
                    if at < 0 or end > before + n:
                        raise _fail(
                            f"source-relative match at {pos - 2 * WORD:#x} "
                            "reaches outside the preceding data"
                        )
                    # The 68000 reads straight through into the compressed
                    # stream if the copy runs past the preceding data, so the
                    # decode does too.
                    out += previous[at : min(end, before)]
                    if end > before:
                        out += data[: end - before]
                else:
                    check_reach(distance)
                    copy_back(out, distance * WORD, length * WORD)
    except _Truncated:
        if not partial:
            raise _fail(f"source ended after {len(out):,} bytes") from None

    return bytes(out), pos, complete


# -- compression ------------------------------------------------------------


class _WordFinder:
    """The longest word-aligned match at every word, for both reaches.

    :class:`MatchFinder` counts in bytes and offers every position it is fed;
    feeding it only positions a whole number of words from the stream's start
    is what makes its candidates word-aligned and its distances even. Two of
    them, because the short form's reach is a different (nearer) match from the
    long form's longest, and both have to be priced.

    With ``previous`` the long-form search runs over the preceding bytes and
    the data as one buffer, the preceding positions indexed first, so a
    candidate before the data is a source-relative match. Those are counted in
    words from the *stream's* start, which is where the decoder measures them,
    so with an odd-length ``previous`` its odd positions are the aligned ones.
    """

    def __init__(self, data: bytes, words: int, previous: bytes) -> None:
        self.short_len = [0] * words
        self.short_dist = [0] * words
        self.long_len = [0] * words
        self.long_dist = [0] * words  # words back from the output position
        self.long_at = [0] * words  # or the absolute position in `previous`
        self.source = [False] * words
        before = len(previous)
        joined = previous + data if before else data
        near = MatchFinder(data, min_match=2 * WORD, window=SHORT_MAX_DISTANCE * WORD)
        far = MatchFinder(joined, min_match=2 * WORD, window=LONG_MAX_DISTANCE * WORD)
        for at in range(before % WORD, before, WORD):
            far.add(at)
        end = words * WORD
        for w in range(words):
            pos = w * WORD
            room = end - pos
            length, at = near.longest(pos, min(SHORT_MAX * WORD, room))
            if length >= SHORT_MIN * WORD:
                self.short_len[w] = length // WORD
                self.short_dist[w] = (pos - at) // WORD
            length, at = far.longest(before + pos, min(LONG_MAX * WORD, room))
            if at < before:
                # A copy from the preceding data must end inside it: what
                # follows in ROM is this stream's own bytes, unknown until
                # written.
                length = min(length, before - at)
                self.source[w] = True
            if length >= LONG_MIN * WORD:
                self.long_len[w] = length // WORD
                self.long_dist[w] = (before + pos - at) // WORD
                self.long_at[w] = at
            near.add(pos)
            far.add(before + pos)


def _emit_literals(out: bytearray, data: bytes, start: int, words: int) -> int:
    """Write full literal-only blocks for a run longer than a header carries.

    Returns the words still pending, which ride in the next block's header.
    """
    while words > LITERAL_MAX:
        out += bytes((LITERAL_MAX << 4, 0))
        out += data[start : start + LITERAL_MAX * WORD]
        start += LITERAL_MAX * WORD
        words -= LITERAL_MAX
    return words


def compress(data: bytes, *, previous: bytes | memoryview | None = None) -> bytes:
    """Encode ``data`` as one LZ4W stream, as few words as the forms allow.

    ``previous`` is what will precede the stream in ROM; given, matches into it
    are written as source-relative long matches, as SGDK's packer writes them.
    """
    n = len(data)
    words = n // WORD
    out = bytearray()
    # Only the reach of the long form's search can be matched, so a whole ROM
    # handed as the preceding data costs a copy of its last 32 KiB, not itself;
    # an even cut keeps every kept position the same parity from the stream.
    before = bytes(previous[-LONG_MAX_DISTANCE * WORD :]) if previous else b""

    if words:
        found = _WordFinder(data, words, before)
        cost = [0] * (words + 1)
        choice: list[tuple[int, int, int]] = [(OP_LITERAL, 1, 0)] * (words + 1)
        for w in range(words - 1, -1, -1):
            best, pick = cost[w + 1] + 1, (OP_LITERAL, 1, 0)
            # Every short length is priced: the form is one word whatever it
            # covers, and a shorter match sometimes leaves a cheaper tail.
            if found.short_len[w]:
                distance = found.short_dist[w]
                for length in range(SHORT_MIN, found.short_len[w] + 1):
                    value = cost[w + length] + 1
                    if value <= best:
                        best, pick = value, (OP_SHORT, length, distance)
            if found.long_len[w]:
                length = found.long_len[w]
                value = cost[w + length] + 2
                if value <= best:
                    op = OP_SOURCE if found.source[w] else OP_LONG
                    best, pick = value, (op, length, found.long_dist[w])
            cost[w], choice[w] = best, pick

        w = 0
        pending = 0  # literal words waiting for a header to ride in
        while w < words:
            op, length, distance = choice[w]
            if op == OP_SOURCE:
                # Measured from the read pointer once past the distance word:
                # after the literal-only blocks a long run needs, this block's
                # header, the literals riding in it, and that word.
                full = (pending - 1) // LITERAL_MAX if pending else 0
                riding = pending - full * LITERAL_MAX
                read_at = len(out) + full * (WORD + LITERAL_MAX * WORD)
                read_at += WORD + riding * WORD + WORD
                distance = (read_at + len(before) - found.long_at[w]) // WORD
                if distance > LONG_MAX_DISTANCE:
                    op = OP_LITERAL  # past the field: the words go as literals
            if op == OP_LITERAL:
                pending += length
                w += length
                continue
            start = (w - pending) * WORD
            pending = _emit_literals(out, data, start, pending)
            start = (w - pending) * WORD
            if op == OP_SHORT:
                out += bytes(((pending << 4) | (length - 1), distance - 1))
                out += data[start : start + pending * WORD]
            else:
                out += bytes(((pending << 4), length - 2))
                out += data[start : start + pending * WORD]
                stored = (-(distance - 1)) & 0x7FFF
                if op == OP_SOURCE:
                    stored |= SOURCE_FLAG
                out += stored.to_bytes(WORD, "big")
            pending = 0
            w += length
        pending = _emit_literals(out, data, (w - pending) * WORD, pending)
        if pending:
            out += bytes((pending << 4, 0))
            out += data[(w - pending) * WORD : w * WORD]

    out += bytes(WORD)  # the end block
    if n & 1:
        out += bytes((SOURCE_FLAG >> 8, data[-1]))
    else:
        out += bytes(WORD)
    return bytes(out)


def _preceding(ctx: PipelineContext) -> bytes | memoryview | None:
    """What precedes the stream: the bound input, else the host's surround."""
    inputs = ctx.get(KEY_INPUTS) or {}
    previous = inputs.get(INPUT_PREVIOUS)
    if isinstance(previous, bytes | bytearray):
        return bytes(previous)
    surround = ctx.get(KEY_SURROUND)
    start = ctx.get(KEY_SURROUND_START)
    if surround is not None and isinstance(start, int) and start >= 0:
        return memoryview(surround)[:start]
    return None


class Lz4wCompression:
    info = PluginInfo(
        id="compression.lz4w",
        name="LZ4W (SGDK word-oriented LZ)",
        stage=Stage.COMPRESSION,
        # The end block bounds the stream, so a ROM can chain these back to back.
        self_delimiting=True,
        category="Sega",
        inputs=(
            InputSpec(
                INPUT_PREVIOUS,
                "Preceding data",
                InputKind.REGION,
                required=False,
                stride=WORD,
                unit="word",
                tooltip=(
                    "The bytes that sit immediately before this stream in\n"
                    "the ROM, ending where the stream starts, for a stream\n"
                    "that copies from the resource packed before it. Needed\n"
                    "only for a stream lifted out of its ROM: in place, the\n"
                    "bytes before it are read from the file itself."
                ),
            ),
            InputSpec(
                INPUT_PACK_PREVIOUS,
                "Pack against preceding data",
                InputKind.FLAG,
                required=False,
                tooltip=(
                    "Checked, a save may copy from the bytes before the\n"
                    "stream, as SGDK's packer does, which is what fits a\n"
                    "stream back into a slot it only fit by doing so. The\n"
                    "stream then stays correct only while those bytes do.\n"
                    "Unchecked, the stream is valid wherever it lands."
                ),
            ),
        ),
    )

    def decompress(self, data: bytes, ctx: PipelineContext) -> bytes:
        out, consumed, complete = decompress(
            data,
            previous=_preceding(ctx),
            partial=bool(ctx.get(KEY_DECOMPRESS_PARTIAL)),
        )
        ctx.set(KEY_COMPRESSED_SIZE, consumed)
        ctx.set(KEY_DECOMPRESS_COMPLETE, complete)
        return out

    def compress(self, data: bytes, ctx: PipelineContext) -> bytes:
        inputs = ctx.get(KEY_INPUTS) or {}
        if not inputs.get(INPUT_PACK_PREVIOUS):
            return compress(data)
        previous = _preceding(ctx)
        if previous is None:
            warn(
                ctx,
                "Packed without the preceding data",
                "This entry asks to pack against the bytes before its\n"
                "stream, but nothing here says what they are: the\n"
                "destination is not a plain file position, or does not\n"
                "exist yet. The stream was written without them, which\n"
                "is valid but may be larger.",
                self.info.id,
            )
        return compress(data, previous=previous)
