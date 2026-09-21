# Plugin Inputs

Some formats need data from outside the slice. A plugin **declares** the inputs
it needs. The entry **binds** each input to a value.

This page uses the plugins of the Super Mario World sample project.

## 1. Open a project that has plugins

celPix loads the `plugins/` folder that is next to a `.celpix` file. To open the
project: **File ▸ Open Project…** (Ctrl+O).

- `.py` plugins are code. celPix asks before it runs each one. It asks again if
  the file changes.
- `.toml` presets are data. celPix does not ask.

![Loading a code plugin](images/inputs-trust-prompt.png)

## 2. A region: the other half of a map

Each cell of the overworld map has attributes: flips, priority and palette. The
game stores these in a second RLE2 stream. The **SMW overworld Layer 2** plugin
unpacks the tile numbers. It takes the attribute stream as an input.

![The overworld, in colour](images/inputs-overworld.png)

To see the inputs: **File ▸ Inputs…**, or right-click the entry. The
right-click menu also has **Copy Inputs** and **Paste Inputs**.

![Inputs](images/inputs-overworld-window.png)

The **Attribute stream** is `$164D` bytes at `$2402B`.

- **Use selection** uses the selection on the canvas.
- **Go to** shows that region.
- `✓` means that the input is valid.

Click **Apply** to save the change.

### Binding it yourself

1. Select the ROM.
2. **File ▸ New Slice…**, with these values:

| | |
| --- | --- |
| **Content** | Tilemap |
| **Offset** | `$22533` |
| **Length** | `$1AF8` |
| **Compression** | SMW overworld Layer 2 (RLE2 tiles + RLE2 attributes) |

![A codec that needs something](images/inputs-new-slice-unbound.png)

3. Click the inputs button.
4. Enter `$2402B` and `$164D`.
5. Click **Apply**, then **Close**.

![Binding the attribute stream](images/inputs-new-slice-with-window.png)

![Bound](images/inputs-new-slice-bound.png)

6. Set **Tilemap** to **SMW overworld Layer 2 (16-bit, 32x32 pages, 4 per
   map)**.
7. Set **Tiles** to the overworld main map BG VRAM window.
8. In **Files**, double-click the main map palette.
9. Set **Cols** to 64. Each map's four pages show as a square.

![The new slice](images/inputs-new-slice-result.png)

The binding is stored on the ROM entry. New slices with this compression use the
same binding.

### Left unbound

An entry with an unbound input shows its raw bytes. You cannot write it.
**Files** marks it with `!`. The tooltip tells you what to bind.

![Unbound](images/inputs-unbound.png)

![The badge](images/inputs-badge-unbound.png)

### What an edit cannot do

Inputs are read-only. **Flip H** changes the attribute stream, which is an
input. **File ▸ Write** (Ctrl+W) refuses the change:

![Refused](images/inputs-refusal.png)

## 3. A flag: what the game's code knows

The game uploads some 3bpp sheets with the fourth bit plane set. These sheets
use colours 9 to 15. Only the game code shows which sheets. So the sheet plugin
takes flags as inputs. `GFX1E` sets one flag.

![Two flags](images/inputs-flags-window.png)

| Ticked | Unticked |
|:-:|:-:|
| [![Colours 9 to 15](images/inputs-flag-on.png)](images/inputs-flag-on.png) | [![Colours 1 to 7](images/inputs-flag-off.png)](images/inputs-flag-off.png) |

The game uploads `GFX08` both ways. So it has two entries.

## 4. A number: one layer of two

A credits cast screen writes two layers from one stripe list. The stripe plugin
takes a **VRAM page** as an input. Two entries use the same bytes with different
pages. Each entry shows one layer.

![VRAM page](images/inputs-integer-window.png)

| `$5000`: the names | `$2000`: the mask |
|:-:|:-:|
| [![The names](images/inputs-integer-names.png)](images/inputs-integer-names.png) | [![The mask](images/inputs-integer-mask.png)](images/inputs-integer-mask.png) |

A number input can be:

- **Literal**: a value that you type.
- **From bytes**: a value read from another part of the file.

## Declaring one

```python
info = PluginInfo(
    id="compression.smw-ow-layer2",
    ...
    inputs=(InputSpec("attributes", "Attribute stream", InputKind.REGION),),
)

def decompress(self, data, ctx):
    attributes = ctx.get(KEY_INPUTS)["attributes"]
```

For a full example, see `compression/_inputs.py`. To open the folder: **File ▸
Open plugins folder…**.

## Plugin code

The sample project is not included with celPix. To follow this page with your
own project, copy these files into a `plugins/` folder next to your `.celpix`
file. Keep the subfolder names. Then reopen the project.

<details>
<summary><code>plugins/compression/smw_ow_layer2.py</code>: SMW overworld Layer 2 (RLE2 tiles + RLE2 attributes)</summary>

```python
"""The overworld's Layer 2: two RLE2 streams woven into one tilemap.

The overworld's background is an ordinary 16-bit SNES tilemap that the cartridge
stores as **two streams** — every word's low byte in one, every word's high byte
in the other, each RLE2-compressed on its own (``docs/smw/overworld.md``). The
loader runs the same decoder twice over the same buffer, the first pass writing
the even bytes and the second the odd ones.

A slice is one byte range, and no range covers both streams *as one structure*:
they are adjacent but separately framed, and RLE2 has no terminator, so the first
stream's end is a label rather than something a decoder finds. So the entry is
the **tile-number stream**, and the attribute stream is a **plugin input**
(``docs/design/plugin-inputs.md``): a region of the same file, bound on the entry,
resolved by the host and handed over read-only on the context.

**An input is never written back**, and that decides what an edit can do. The
tile numbers are this entry's bytes and re-pack into its slot; an attribute —
a flip, a palette, the priority bit — belongs to the other stream, which this
plugin was only lent. So an edit that changes one is **refused, with a message**,
rather than dropped on the floor: a save that silently discarded half of what was
painted would be the worse answer.

The byte RLE is celPix's own (``celpix/plugins/builtins/snes_rle.py``).
"""

from celpix.core.context import (
    KEY_COMPRESSED_SIZE,
    KEY_DECOMPRESS_COMPLETE,
    KEY_INPUTS,
    PipelineContext,
)
from celpix.core.errors import Stage
from celpix.plugins import InputKind, InputSpec, PluginInfo
from celpix.plugins.builtins.snes_rle import compress as rle_compress
from celpix.plugins.builtins.snes_rle import decompress as rle_decompress


def _attributes(ctx: PipelineContext) -> bytes:
    packed: bytes = (ctx.get(KEY_INPUTS) or {})["attributes"]
    out, _consumed, _complete = rle_decompress(packed, terminated=False)
    return out


class SmwOverworldLayer2:
    info = PluginInfo(
        id="compression.smw-ow-layer2",
        name="SMW overworld Layer 2 (RLE2 tiles + RLE2 attributes)",
        stage=Stage.COMPRESSION,
        # RLE2 has no end marker: the extent is the slice's, as it is for the
        # shipped scheme this is built on.
        self_delimiting=False,
        category="Project",
        inputs=(
            InputSpec(
                "attributes",
                "Attribute stream",
                InputKind.REGION,
                tooltip=(
                    "The RLE2 stream holding every tilemap word's high\n"
                    "byte: flips, priority, palette. It follows the tile\n"
                    "numbers in the ROM and unpacks to the same length."
                ),
            ),
        ),
    )

    def decompress(self, data: bytes, ctx: PipelineContext) -> bytes:
        tiles, consumed, _complete = rle_decompress(data, terminated=False)
        attributes = _attributes(ctx)
        if len(attributes) != len(tiles):
            raise ValueError(
                f"the attribute stream unpacks to {len(attributes)} bytes and the "
                f"tile numbers to {len(tiles)}; they are halves of the same words"
            )
        out = bytearray(len(tiles) * 2)
        out[0::2] = tiles
        out[1::2] = attributes
        ctx.set(KEY_COMPRESSED_SIZE, consumed)
        ctx.set(KEY_DECOMPRESS_COMPLETE, False)
        return bytes(out)

    def compress(self, data: bytes, ctx: PipelineContext) -> bytes:
        if bytes(data[1::2]) != _attributes(ctx):
            raise ValueError(
                "this edit changes a cell's flip, priority or palette, which is "
                "stored in the attribute stream — an input this entry reads and "
                "does not own. Only tile numbers can be written from here."
            )
        return rle_compress(bytes(data[0::2]), terminated=False)


def register(registry):
    registry.register(SmwOverworldLayer2())
```

</details>

<details>
<summary><code>plugins/tilemap/smw-ow-layer2.toml</code>: SMW overworld Layer 2 (16-bit, 32x32 pages, 4 per map)</summary>

```toml
# The overworld's Layer 2 — a raw 8x8 tilemap, four 32x32 pages per map.
#
# Not Map16 at all: the cells are ordinary SNES background words, and the cell
# below is `preset.tilemap.snes-bg`'s. What the cartridge does with them is keep
# the two bytes of every word in **separate streams** — the tile numbers and the
# attribute bytes, each RLE2-compressed on its own (docs/smw/overworld.md) — and
# `compression.smw-ow-layer2` (plugins/compression/) weaves them back into the
# words this reads.
#
# WHY THIS AND NOT `preset.tilemap.snes-bg` itself: the geometry. 512x512 pixels
# is 32x32 tiles held as four pages in the quartering the SNES applies to a 64x64
# tilemap, and two maps' worth sit back to back — the main map, then the submaps.
# The shipped preset states page counts of 1, 2 and 4, so eight pages would be
# refused as not this shape and read straight through, stacked in a 32x256 column
# no console ever drew. Stating the counts here is what lets celPix's own
# assembly lay them out (docs/design/tilemap-entry.md 6); how many go side by side
# is then the entry's, in `view.pages_across`.
id = "preset.tilemap.smw-ow-layer2"
name = "SMW overworld Layer 2 (16-bit, 32x32 pages, 4 per map)"
engine_id = "codec.tilemap.packed"
category = "Project"

[params]
bytes = 2
endian = "little"
fields = "vhop ppii iiii iiii"
# One map is four pages; the file holds two maps.
page_columns = 32
page_rows = 32
page_counts = [4, 8]
```

</details>

<details>
<summary><code>plugins/compression/smw_gfx.py</code>: SMW graphics file (LZ2, expanded to 4bpp)</summary>

```python
"""A tile sheet as VRAM holds it: the LZ, then the 3bpp-to-4bpp expansion.

Every background and sprite sheet in the cartridge is stored **3bpp** and drawn
**4bpp**. ``UploadGFXFile`` (``SMW/Banks/Bank00.asm``) does not decompress into
VRAM and convert later — it writes the decompressed file straight to the data
port, converting as it goes, 24 source bytes to 32 VRAM bytes per tile:

* bytes 0-15 are bitplanes 0 and 1, row-interleaved, and go through unchanged;
* bytes 16-23 are bitplane 2, one byte a row, and become the bp2/bp3 pair, with
  **bp3 = (bp0 | bp1 | bp2) & mask** and ``mask`` normally ``$0000``.

So the file is 3bpp and the tile the hardware draws is 4bpp with plane 3 clear —
which is not a nicety. **It is what decides the colours.** A palette row is
sixteen entries of CGRAM and a Map16 cell's three palette bits name one of them,
so a tile of this sheet reaches colours ``row * 16 + 1`` to ``+7``. Read as 3bpp
the same tile reaches ``row * 8``, and every picture in the project lands in the
wrong half of the wrong CGRAM row — a bank on the wrong row, from
``docs/rom-mapping/palettes.md`` §2, arrived at by arithmetic rather than by a
bad guess. Handing the pixel codec what the *uploader* produces is what makes one
plain CGRAM palette correct for the sheets, the assembled VRAM windows, the Map16
tables and every map that draws through them.

That is why this is a Compression plugin and not a Reshape: the conversion adds a
third of the bytes again, and a reshape is length-preserving by definition.

**The mask.** ``UploadGFXFile`` sets ``mask = $FF00`` in two cases, which sets
bit 3 on every non-transparent pixel and moves those tiles into colours 9-15 of
their row:

* the **whole file** — ``GFX1E`` always, and ``GFX08`` when the tileset being
  loaded is ``$11`` or above (``CPX #$11`` / ``CPY #$08``, then ``FilterSomeRAM``);
* **four tiles** — ``$00``, ``$01``, ``$10`` and ``$11`` of ``GFX01`` and
  ``GFX17``: the loop counts ``Y`` down from ``$7F`` and masks while ``Y >= $7E``
  or ``$6E <= Y < $70``, which in a sheet sixteen tiles wide is the one 16x16
  object at its head.

Neither is in the file. It is a property of *which file number is being loaded
under which tileset* — ``GFX08`` expands both ways in the same playthrough — so a
decoder that sees only bytes cannot know it, and it is the entry's to say. That is
what the two **flag inputs** below are (``docs/design/plugin-inputs.md``): facts
the cartridge records nowhere because its code knows them, bound per entry and
delivered on the context. Unbound, a flag is off, which is every other sheet.

Both directions are implemented. Plane 3 is *derived*, so dropping it is exact —
an unedited sheet re-encodes to the LZ's own parse and back to the cartridge's
bytes — and that is also the limit of an edit: a pixel painted in a colour its
tile's rule cannot produce has nowhere to be stored, so the save is **refused,
with a message**, rather than written back as a different colour.
"""

from celpix.core.context import (
    KEY_COMPRESSED_SIZE,
    KEY_DECOMPRESS_COMPLETE,
    KEY_DECOMPRESS_PARTIAL,
    KEY_INPUTS,
    PipelineContext,
)
from celpix.core.errors import Stage
from celpix.plugins import InputKind, InputSpec, PluginInfo
from celpix.plugins.builtins.lz_command import compress as lz_compress
from celpix.plugins.builtins.lz_command import decompress as lz_decompress

# This is a USA cartridge, so the LZ's backreference offsets are big-endian; the
# one instruction that decides it is the `XBA` at `CODE_00B966`, assembled only
# for the J and E1 releases.
BIG_ENDIAN_OFFSETS = True

STORED_TILE = 24  # three bitplanes: two row-interleaved, one a byte a row
VRAM_TILE = 32  # four bitplanes as two interleaved pairs, 16 bytes apart
ROWS = 8


# The four tiles the partial rule names: `Y` >= $7E, and $6E <= `Y` < $70, with
# `Y` counting down from $7F as the tiles go up.
HEAD_OBJECT = frozenset({0x00, 0x01, 0x10, 0x11})


def masked_tiles(ctx: PipelineContext, tiles: int) -> frozenset[int]:
    """The tiles this entry's flags say are uploaded with plane 3 set."""
    inputs = ctx.get(KEY_INPUTS) or {}
    if inputs.get("plane3_all"):
        return frozenset(range(tiles))
    return HEAD_OBJECT if inputs.get("plane3_head") else frozenset()


def expand(stored: bytes, masked: frozenset[int] = frozenset()) -> bytes:
    """3bpp planar to 4bpp planar — ``UploadGFXFile``'s conversion.

    Plane 3 is clear, except in the ``masked`` tiles, where it is the OR of the
    other three: set on every pixel that is not transparent.
    """
    tiles = len(stored) // STORED_TILE
    out = bytearray(tiles * VRAM_TILE)
    for t in range(tiles):
        src, dst = t * STORED_TILE, t * VRAM_TILE
        out[dst : dst + 16] = stored[src : src + 16]  # bp0/bp1, unchanged
        for y in range(ROWS):
            bp2 = stored[src + 16 + y]
            out[dst + 16 + 2 * y] = bp2
            if t in masked:
                out[dst + 17 + 2 * y] = (
                    stored[src + 2 * y] | stored[src + 2 * y + 1] | bp2
                )
    return bytes(out)


def contract(vram: bytes) -> bytes:
    """The inverse: keep planes 0-2 and drop plane 3, which the game derives."""
    tiles = len(vram) // VRAM_TILE
    out = bytearray(tiles * STORED_TILE)
    for t in range(tiles):
        src, dst = t * VRAM_TILE, t * STORED_TILE
        out[dst : dst + 16] = vram[src : src + 16]
        for y in range(ROWS):
            out[dst + 16 + y] = vram[src + 16 + 2 * y]
    return bytes(out)


class SmwGraphicsFile:
    info = PluginInfo(
        id="compression.smw-gfx-vram",
        name="SMW graphics file (LZ2, expanded to 4bpp)",
        stage=Stage.COMPRESSION,
        self_delimiting=True,
        category="Project",
        inputs=(
            InputSpec(
                "plane3_all",
                "Upper colours: whole file",
                InputKind.FLAG,
                required=False,
                tooltip=(
                    "The uploader sets plane 3 on every drawn pixel, so\n"
                    "the file reaches colours 9-15 of its row. GFX1E always;\n"
                    "GFX08 under tileset $11 and above."
                ),
            ),
            InputSpec(
                "plane3_head",
                "Upper colours: first 16x16 object",
                InputKind.FLAG,
                required=False,
                tooltip=(
                    "The same, for tiles $00, $01, $10 and $11 alone —\n"
                    "the object at the head of GFX01 and GFX17."
                ),
            ),
        ),
    )

    def decompress(self, data: bytes, ctx: PipelineContext) -> bytes:
        try:
            stored, consumed = lz_decompress(
                data, big_endian_offsets=BIG_ENDIAN_OFFSETS
            )
        except ValueError:
            if ctx.get(KEY_DECOMPRESS_PARTIAL):
                ctx.set(KEY_COMPRESSED_SIZE, 0)
                ctx.set(KEY_DECOMPRESS_COMPLETE, False)
                return b""
            raise
        if len(stored) % STORED_TILE and not ctx.get(KEY_DECOMPRESS_PARTIAL):
            raise ValueError(f"{len(stored)} bytes is not a whole number of 3bpp tiles")
        ctx.set(KEY_COMPRESSED_SIZE, consumed)
        ctx.set(KEY_DECOMPRESS_COMPLETE, True)
        return expand(stored, masked_tiles(ctx, len(stored) // STORED_TILE))

    def compress(self, data: bytes, ctx: PipelineContext) -> bytes:
        stored = contract(data)
        masked = masked_tiles(ctx, len(stored) // STORED_TILE)
        if expand(stored, masked) != bytes(data[: len(stored) // STORED_TILE * 32]):
            raise ValueError(
                "a pixel uses a colour its tile cannot store: this sheet is 3bpp, "
                "so a tile holds colours 0-7 of its row — or 0 and 9-15 where the "
                "uploader sets plane 3 (the entry's Inputs say which tiles)."
            )
        return lz_compress(stored, big_endian_offsets=BIG_ENDIAN_OFFSETS)


def register(registry):
    registry.register(SmwGraphicsFile())
```

</details>

<details>
<summary><code>plugins/compression/smw_stripe.py</code>: SMW stripe image (VRAM patch list)</summary>

```python
"""Stripe images — the cartridge's general "write this data to that VRAM address".

A stripe image is not compression and is read as compression, because what it
produces is what every other Compression plugin produces: the bytes the next
stage interprets. The stream is a list of VRAM patches; the bytes it patches are
tilemap cells; so decoding one and handing the result to the tilemap codec is how
a title screen or a Layer 3 background becomes a picture rather than a list of
records. ``docs/rom-mapping/authoring.md`` §5's last row — no engine expresses
this, so it is code.

A record is four header bytes and then its payload
(``UploadToVRAM`` in ``ROUTINE_RT01_SMW_LoadStripeImage``, bank $00):

    byte 0:  0AAAAAAA   VRAM word address, high byte -- bit 7 SET ends the list
    byte 1:  AAAAAAAA   VRAM word address, low byte
    byte 2:  VRLLLLLL   V = step 32 words, R = RLE, then the length's high 6 bits
    byte 3:  LLLLLLLL   length - 1, low byte (the 14-bit length is big-endian)

The length counts **bytes written to the VRAM port**, and the port is a 16-bit
word: a write lands in the low byte of a word, the next in the high byte, and
only then does the address advance. An RLE record's payload is the two bytes
written to the two halves, repeated; every other record's payload is its length
in bytes. ``V`` steps 32 *words* rather than 1 between address advances, which is
one column of a 32-wide tilemap page — that is how the game draws a vertical run.

**The window.** VRAM is 64 KiB and a stripe image patches a few hundred bytes of
it, so the output is not all of VRAM: it is the range of 4096-word (8 KiB) VRAM
pages the records actually touch. 8 KiB is one 64x64 SNES background tilemap,
which is what nearly every one of these patches, and it is why an entry using
this reads cleanly as a 64x64 map. The entry's name states the VRAM word the
window starts at, because the data cannot: a stripe image says where it writes,
not where its picture begins.

**One layer of a screen that patches several.** A few images write two tilemaps
in one list — a credits cast screen masks Layer 1 at word ``$2000`` and spells
its enemy names on Layer 3 at ``$5000``. The layers draw from different character
bases, so no single binding is right for a window spanning both, and which layer
an *entry* is looking at is not in the stream. It is an optional **integer
input**, ``vram_page`` (``docs/design/plugin-inputs.md``): bound, only the records
inside that 64x64 page are replayed and the window is exactly that page, so the
same bytes are carved twice and each entry binds to the tiles its layer draws
from. Unbound, the window is every page the records touch, as above.

**What fills the gaps.** Not zero. In the machine those cells hold whatever the
last screen left there, which this cannot know, and a zero cell is not "nothing"
— it is tile 0 of whatever bank the map is bound to, drawn a few thousand times
over a picture that never contained it. So the fill is ``$FF``, making every
untouched cell tile ``$3FF``: past the end of any window a background is drawn
from, so it renders blank and says so. It is also the cartridge's own idiom —
the unused tail of each page of the Map16 background table is ``$FF`` bytes
(``docs/smw/map16.md``).

**Read-only, and deliberately.** ``compress`` is not implemented, so celPix opens
these view-only. The inverse is not a function of the decoded bytes — which
records a run was cut into, which were RLE, which stepped vertically, and what
was in the untouched gaps are all lost by flattening — and inventing an answer
would rewrite a region the game reads with a routine that does not care what we
think it meant. Editing a Layer 3 background is done on the tile sheet it draws
through, which is an ordinary entry with an ordinary Write.
"""

from celpix.core.context import (
    KEY_COMPRESSED_SIZE,
    KEY_DECOMPRESS_COMPLETE,
    KEY_DECOMPRESS_PARTIAL,
    KEY_INPUTS,
    PipelineContext,
)
from celpix.core.errors import Stage
from celpix.plugins import InputKind, InputSpec, PluginInfo

#: One 64x64 SNES background tilemap, in VRAM words. The window is a whole
#: number of these, aligned, so a picture's rows line up with the hardware's.
PAGE_WORDS = 0x1000


def _scan(data: bytes):
    """``(records, consumed, complete)`` for the record list at ``data[0]``.

    A record is ``(vram_word, vertical, rle, length, payload)``.
    """
    recs = []
    i, n = 0, len(data)
    while i < n:
        if data[i] & 0x80:  # the terminator, conventionally $FF
            return recs, i + 1, True
        if i + 4 > n:
            break
        vram = (data[i] << 8) | data[i + 1]
        vertical = bool(data[i + 2] & 0x80)
        rle = bool(data[i + 2] & 0x40)
        length = (((data[i + 2] & 0x3F) << 8) | data[i + 3]) + 1
        payload = 2 if rle else length
        if i + 4 + payload > n:
            break
        recs.append((vram, vertical, rle, length, data[i + 4 : i + 4 + payload]))
        i += 4 + payload
    return recs, i, False


class SmwStripeImage:
    info = PluginInfo(
        id="compression.smw-stripe",
        name="SMW stripe image (VRAM patch list)",
        stage=Stage.COMPRESSION,
        self_delimiting=True,
        category="Project",
        inputs=(
            InputSpec(
                "vram_page",
                "VRAM page",
                InputKind.INTEGER,
                required=False,
                minimum=0,
                maximum=0xF000,
                unit="word",
                tooltip=(
                    "Replay only the records inside this 64x64 tilemap\n"
                    "page, given as its VRAM word address ($2000, $5000).\n"
                    "For an image that patches two layers at once."
                ),
            ),
        ),
    )

    def decompress(self, data: bytes, ctx: PipelineContext) -> bytes:
        recs, consumed, complete = _scan(data)
        page = (ctx.get(KEY_INPUTS) or {}).get("vram_page")
        if page is not None:
            if page % PAGE_WORDS:
                raise ValueError(f"VRAM page ${page:04X} is not a multiple of $1000")
            recs = [rec for rec in recs if page <= rec[0] < page + PAGE_WORDS]
        if not complete and not ctx.get(KEY_DECOMPRESS_PARTIAL):
            raise ValueError("no terminator — no SMW stripe image here")
        if not recs:
            ctx.set(KEY_COMPRESSED_SIZE, consumed)
            ctx.set(KEY_DECOMPRESS_COMPLETE, complete)
            # A bound page nothing writes is still that page, blank.
            return b"" if page is None else b"\xff" * (PAGE_WORDS * 2)

        # Two passes: the window has to be known before anything can be written
        # into it, and a record's reach depends on its own step.
        lo = min(v for v, *_ in recs)
        hi = max(_reach(v, vertical, length) for v, vertical, _, length, _ in recs)
        base = (lo // PAGE_WORDS) * PAGE_WORDS
        end = ((hi // PAGE_WORDS) + 1) * PAGE_WORDS
        if page is not None:
            base, end = page, page + PAGE_WORDS
        out = bytearray(b"\xff" * ((end - base) * 2))

        for vram, vertical, rle, length, payload in recs:
            step = 32 if vertical else 1
            word, half = vram, 0
            for k in range(length):
                byte = payload[k % 2] if rle else payload[k]
                at = (word - base) * 2 + half
                if 0 <= at < len(out):
                    out[at] = byte
                half += 1
                if half == 2:  # the port advances on the high byte
                    half = 0
                    word += step

        ctx.set(KEY_COMPRESSED_SIZE, consumed)
        ctx.set(KEY_DECOMPRESS_COMPLETE, complete)
        return bytes(out)


def _reach(vram: int, vertical: bool, length: int) -> int:
    """The last VRAM word a record touches."""
    words = (length + 1) // 2
    return vram + (words - 1) * (32 if vertical else 1)


def register(registry):
    registry.register(SmwStripeImage())
```

</details>
