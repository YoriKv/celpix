"""The "Phantasy Star" RLE — Sega's de-interleaved run-length scheme for the
Master System and Game Gear.

About 65 Sega first-party titles load tiles and tilemaps through it: Phantasy
Star, Alex Kidd in Miracle World, Fantasy Zone, Golden Axe, Out Run, Castle of
Illusion and Land of Illusion among them (the list is in
``docs/graphics-formats-reference/implementation-guide.md`` §7). The stream is
cut into **parts**, each run-length coded on its own::

    per part, repeating:
      %0nnnnnnn dd        n copies of dd              (n = 1..127)
      %1nnnnnnn b1 .. bn  n literal bytes             (n = 1..127; see $80 below)
      %00000000           end of this part

**The parts are the interleaved data taken apart.** The game's loader writes
part *k*'s *i*-th byte at ``dest + k + parts * i``, so tiles (four parts) come
out as ordinary interleaved 4bpp and a tilemap (two parts) as ordinary 2-byte
cells. celPix's reshape stage runs before decompression and so cannot weave
them back, and this codec does it itself: the output is what
``preset.pixel.sms-4bpp`` or ``preset.tilemap.sms-bg`` reads directly.

**How many parts is not in the bytes** — the same stream grammar is loaded by a
tile routine that walks four and a tilemap routine that walks two. So it is a
plugin **input** (``docs/design/plugin-inputs.md``): an optional integer,
``interleave``, defaulting to the four of tile data, which a tilemap slice binds
to 2.

Edge cases, settled from the game's Z80 loaders rather than the prose:

- **A count of zero in a run is unreachable.** ``$00`` is read as the end of the
  part before the run/literal split, so runs and literals start at 1.
- **``$80`` is not a zero-length literal.** The tile loader keeps the count in
  ``b`` for ``djnz``, which loops 256 times from zero, so ``$80`` is 256 literal
  bytes; the RAM tilemap loader keeps it in ``bc`` for ``ldi``/``jp pe``, which
  reads it as 65,536. The decoder takes the tile loader's 256 (no real stream
  can mean 65,536 — that overruns the whole of RAM), and the encoder never
  writes ``$80``, capping both packet kinds at 127, where every reading agrees.
- **Parts may differ in length as far as the Z80 cares** — each is written
  independently, and a short part just leaves VRAM alone. A real stream never
  does, so unequal parts are how a **wrong part count** shows: decoding a
  two-part tilemap as four reads its two parts and then two more out of
  whatever follows. A strict decode rejects it with the lengths in the message;
  a partial decode (``KEY_DECOMPRESS_PARTIAL``) weaves what it has with zeros in
  the gaps and reports the structure incomplete. The reverse mistake — a
  four-part stream read as two — gives two equal parts and cannot be told from a
  real tilemap; it decodes half the stream and reports that extent.

**A stated size takes the parts' slack.** Where a game's loader is told how many
bytes to expect — a size word ahead of the stream, a map's width times its
height — its packer is free to leave a byte of slack on some parts and not on
others, and one did: a Mega Drive cartridge whose streams are this grammar has
parts one byte apart in 39 of its 148 (``docs/rom-mapping/console-mega-drive.md``
§5). The loader never notices, because the shortest part still covers its share
of the size. So a second optional input, ``size``, states the decoded length:
with it the decoder accepts parts that differ, requires each to cover its share
(``k, k + parts, …`` below ``size``), and weaves exactly ``size`` bytes, dropping
the slack; the encoder writes the parts that length splits into, a remainder and
all. Without it nothing changes, so the unequal parts that expose a wrong part
count on the Master System still do.

The terminators make the scheme **self-delimiting**: the byte after the last
part's ``$00`` is the structure's true end, reported as its compressed size. A
buffer that ends before the last terminator is an error unless partial.

The encoder is the shared run/literal packer (:func:`~._rle.pack_runs`) over
each part in turn. Byte-identity with Sega's own packer is a non-goal;
round-tripping is the contract.
"""

from __future__ import annotations

from celpix.core.context import (
    KEY_COMPRESSED_SIZE,
    KEY_DECOMPRESS_COMPLETE,
    KEY_DECOMPRESS_PARTIAL,
    KEY_INPUTS,
    PipelineContext,
)
from celpix.core.errors import Stage
from celpix.plugins.base import InputKind, InputSpec, PluginInfo
from celpix.plugins.builtins._rle import pack_runs

#: The input key the part count is bound under — a compatibility surface, like
#: the plugin id.
INPUT_PARTS = "interleave"
#: Tile data: one part per bitplane.
TILE_PARTS = 4
#: A tilemap: one part per byte of the 2-byte cell.
TILEMAP_PARTS = 2
#: The input key the decoded length is bound under.
INPUT_SIZE = "size"
# A bound on the input spin, not a property of the scheme: the loader picks the
# count, and a level map decompressed to RAM one part per **row** takes as many
# parts as it has rows -- 12 on Alex Kidd in Shinobi World, whose to-RAM entry
# point is called with C = 12 for every room. 32 leaves headroom for a screen
# cut per row (28) without admitting the values a typo produces.
_MAX_PARTS = 32

# The largest packet the encoder writes: $80 means 256 to one loader and 65,536
# to another, so both packet kinds stop one short of it.
_MAX_PACKET = 0x7F
# A run packet costs 2 bytes, so a run inside a literal only pays at 3.
_MIN_RUN = 3
# Memory guard for one part, not a format limit: four times the Master System's
# 16 KB of VRAM, so no real part comes near it while a stream of $80 packets
# found by a scan cannot grow without bound.
_MAX_PART = 0x10000


def _fail(reason: str) -> ValueError:
    return ValueError(f"corrupt Phantasy Star RLE stream: {reason}")


def decompress(
    data: bytes, *, parts: int = TILE_PARTS, size: int = 0, partial: bool = False
) -> tuple[bytes, int, bool]:
    """Decode ``parts`` parts and weave them back into interleaved order.

    Returns ``(output, consumed, complete)``. ``complete`` is true when every
    part's terminator was inside ``data`` and the parts agree in length — or,
    with ``size``, each covers its share of it — which makes ``consumed`` the
    structure's true byte length. With ``partial`` a buffer ending early, or
    parts that fall short, give the best-effort weave (zeros where a part has no
    byte) instead of raising.
    """
    if size < 0:
        raise ValueError(f"size must not be negative, not {size}")
    if parts < 1:
        raise ValueError(f"part count must be at least 1, not {parts}")
    decoded: list[bytearray] = []
    i, n = 0, len(data)
    ran_out = False
    for k in range(parts):
        part = bytearray()
        decoded.append(part)
        while True:
            if i >= n:
                ran_out = True
                break
            control = data[i]
            i += 1
            if control == 0:
                break
            if control & 0x80:
                count = control & 0x7F or 0x100
                chunk = data[i : i + count]
                part += chunk
                i += len(chunk)
                if len(chunk) < count:
                    ran_out = True
                    break
            else:
                if i >= n:
                    ran_out = True
                    break
                part += bytes((data[i],)) * control
                i += 1
            if len(part) > _MAX_PART:
                raise _fail(f"part {k} passes {_MAX_PART:#x} bytes")
        if ran_out:
            if not partial:
                raise _fail(f"source ends inside part {k} of {parts}")
            break

    lengths = [len(part) for part in decoded]
    if size:
        shares = [len(range(k, size, parts)) for k in range(parts)]
        covered = len(decoded) == parts and all(
            have >= need for have, need in zip(lengths, shares, strict=True)
        )
        if not ran_out and not covered and not partial:
            raise _fail(
                f"its {parts} parts decode to {', '.join(map(str, lengths))} bytes, "
                f"short of the {', '.join(map(str, shares))} a {size:,}-byte "
                "stream needs; the part count or the size is likely wrong"
            )
        return _weave(decoded, parts, size), i, not ran_out and covered
    even = len(decoded) == parts and len(set(lengths)) == 1
    if not ran_out and not even and not partial:
        raise _fail(
            f"its {parts} parts decode to {', '.join(map(str, lengths))} bytes; "
            "a real stream's parts agree, so the part count is likely wrong"
        )
    return _weave(decoded, parts), i, not ran_out and even


def _weave(decoded: list[bytearray], parts: int, size: int = 0) -> bytes:
    """Part *k*'s *i*-th byte at ``k + parts * i`` — the loader's write order.

    ``size`` cuts the weave there, and a part's bytes past its share of it are
    the packer's slack, dropped.
    """
    width = max((len(part) for part in decoded), default=0)
    out = bytearray(size or width * parts)
    for k, part in enumerate(decoded):
        lane = len(range(k, len(out), parts))
        out[k : len(out) : parts] = bytes(part[:lane]).ljust(lane, b"\0")
    return bytes(out)


def compress(data: bytes, *, parts: int = TILE_PARTS, size: int = 0) -> bytes:
    """Take ``data`` apart into ``parts`` parts and run-length code each.

    Without ``size``, refuses data that is not a whole number of parts: the
    decoder weaves equal parts, so a remainder has nowhere to go. With it the
    decoder knows where the data stops, so the parts may differ by the
    remainder — and ``data`` must be that long.
    """
    if parts < 1:
        raise ValueError(f"part count must be at least 1, not {parts}")
    if size and len(data) != size:
        raise ValueError(f"{len(data):,} bytes to pack, but the stated size is {size:,}")
    if not size and len(data) % parts:
        raise ValueError(
            f"{len(data):,} bytes is not a whole number of {parts}-part groups "
            f"({len(data) % parts} left over)"
        )
    out = bytearray()
    for k in range(parts):
        pack_runs(
            data[k::parts],
            out,
            literal_header=lambda count: 0x80 | count,
            run_header=lambda count: count,
            max_packet=_MAX_PACKET,
            min_run=_MIN_RUN,
            spill_pair_as_run=True,
        )
        out.append(0)
    return bytes(out)


def _parts(ctx: PipelineContext) -> int:
    # The host delivers the spec's default when nothing is bound; a caller that
    # never seeded inputs at all gets the same default rather than a KeyError.
    return int((ctx.get(KEY_INPUTS) or {}).get(INPUT_PARTS, TILE_PARTS))


def _size(ctx: PipelineContext) -> int:
    return int((ctx.get(KEY_INPUTS) or {}).get(INPUT_SIZE, 0))


class PhantasyStarRleCompression:
    info = PluginInfo(
        id="compression.phantasy-star-rle",
        name='"Phantasy Star" RLE (Master System, de-interleaved)',
        stage=Stage.COMPRESSION,
        self_delimiting=True,
        category="Sega",
        inputs=(
            InputSpec(
                INPUT_PARTS,
                "Interleave",
                InputKind.INTEGER,
                required=False,
                default=TILE_PARTS,
                minimum=1,
                maximum=_MAX_PARTS,
                unit="part",
                tooltip=(
                    "How many parts the stream is cut into, one per byte\n"
                    "of the interleaved data: 4 for tiles (one per\n"
                    "bitplane), 2 for a tilemap (one per cell byte).\n"
                    "The bytes do not say; the game's loader does."
                ),
            ),
            InputSpec(
                INPUT_SIZE,
                "Decoded size",
                InputKind.INTEGER,
                required=False,
                default=0,
                minimum=0,
                maximum=_MAX_PART * _MAX_PARTS,
                unit="byte",
                tooltip=(
                    "The bytes the stream decodes to, where the game's\n"
                    "loader is told: a size word, a map's width x height x 2.\n"
                    "Lets the parts differ by a packer's slack byte and\n"
                    "drops it. 0 requires every part to be the same length."
                ),
            ),
        ),
    )

    def decompress(self, data: bytes, ctx: PipelineContext) -> bytes:
        out, consumed, complete = decompress(
            data,
            parts=_parts(ctx),
            size=_size(ctx),
            partial=bool(ctx.get(KEY_DECOMPRESS_PARTIAL)),
        )
        ctx.set(KEY_COMPRESSED_SIZE, consumed)
        ctx.set(KEY_DECOMPRESS_COMPLETE, complete)
        return out

    def compress(self, data: bytes, ctx: PipelineContext) -> bytes:
        return compress(data, parts=_parts(ctx), size=_size(ctx))
