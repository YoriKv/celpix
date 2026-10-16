# Preset Reference

Parameters of every TOML preset engine celPix ships. A preset is data: it names
an engine by `engine_id` and sets that engine's `[params]`. The folder sets the
stage. `id` is stable and is what a project stores, `name` is shown in the
picker, `category` is optional. Folders, loading and the picker: [Plugins](Plugins).

**Contents**

- [1. Conventions](#1-conventions)
  - [1.1 Preset keys](#11-preset-keys)
  - [1.2 Bit layouts](#12-bit-layouts)
- [2. Pixel stage](#2-pixel-stage)
  - [2.1 Planar](#21-planar)
  - [2.2 Packed](#22-packed)
  - [2.3 Packed, fields straddling bytes](#23-packed-fields-straddling-bytes)
  - [2.4 Nibble-planar](#24-nibble-planar)
  - [2.5 Direct color](#25-direct-color)
  - [2.6 Palette swatches](#26-palette-swatches)
- [3. Palette stage](#3-palette-stage)
  - [3.1 Color mask](#31-color-mask)
  - [3.2 Color indexed](#32-color-indexed)
- [4. Tilemap stage](#4-tilemap-stage)
  - [4.1 Keys read by celPix on any tilemap preset](#41-keys-read-by-celpix-on-any-tilemap-preset)
  - [4.2 Packed cell](#42-packed-cell)
  - [4.3 Sprite record](#43-sprite-record)
  - [4.4 Mega Drive sprite](#44-mega-drive-sprite)
  - [4.5 Sprite table](#45-sprite-table)
  - [4.6 Indirect record](#46-indirect-record)
- [5. Reshape stage](#5-reshape-stage)
  - [5.1 Bitswap](#51-bitswap)
  - [5.2 Data LUT](#52-data-lut)
  - [5.3 Split parts](#53-split-parts)
  - [5.4 PS2 GS swizzle](#54-ps2-gs-swizzle)
- [6. Compression stage](#6-compression-stage)
  - [6.1 Compress and reshape](#61-compress-and-reshape)
- [7. Where a preset is not enough](#7-where-a-preset-is-not-enough)

## 1. Conventions

### 1.1 Preset keys

| Key | Type | Required | Meaning |
|---|---|---|---|
| `id` | string | yes | Stored in project files. Keep it stable |
| `name` | string | yes | Shown in the picker |
| `engine_id` | string | yes | The engine (sections 2 to 6) |
| `category` | string | no | Picker heading |
| `stage` | string | no | Not needed. One that is not the folder's stage fails the load |
| `[params]` | table | per engine | The engine's parameters |

| Rule | |
|---|---|
| Pixel, palette, tilemap presets | Params checked on first use, not at load: an invalid preset loads, then fails on the entry that uses it |
| Reshape, compression presets | Built into a plugin at load: a bad value, or a key the engine does not read, fails the load. `id` becomes the plugin id and must not be an engine id |
| Values | Strict. A boolean is `true`/`false`, an integer a TOML integer; a parameter with a fixed set of values refuses any other |

### 1.2 Bit layouts

| Rule | |
|---|---|
| Letters | One per bit, most significant first, case-insensitive |
| Separators | Whitespace and `\|`, ignored |
| Unused bit | `.`, `-`, `0` or `x`, written as zero. On `sprite-record`, `x` is the x offset |
| Split field | A letter in several runs is one field; the runs join most significant first |
| Width | Every bit has a letter; the bit count equals the value's width |
| `legend` | Optional table renaming letters: key one character, value one of the engine's field names. Any other value is an error |

The letters are listed per engine.

## 2. Pixel stage

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `palette_bpp` | int | the tile's depth | Read by celPix on any pixel preset: the depth a palette row is sized by, where hardware expands tiles on upload. Never below the tile's depth |

### 2.1 Planar

`codec.pixel.planar`. Bit `k` of a pixel's index is read from plane `k`; pixel
`x` of an eight-pixel group is bit `7 - x`. Plane `k`'s byte for row `y`, group
`g`: `base[k] + stride[k] * y + group_stride[k] * g`.

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `bpp` | int | required | 1 to 8. Equals the number of `planes` |
| `planes` | list of tables | required | Entry `k` supplies bit `k`; entry 0 is the low bit |
| `planes[].base` | int | required | Byte offset of the plane at row 0, group 0 |
| `planes[].stride` | int | required | Bytes between pixel rows |
| `planes[].group_stride` | int | `0` | Bytes between eight-pixel groups. May be negative |
| `tile_width` | int | `8` | Multiple of 8 |
| `tile_height` | int | `8` | Positive. A tile is `tile_width * tile_height * bpp / 8` bytes; planes are not checked for overlap |

```toml
id = "preset.pixel.example-planar"
name = "Example planar 2bpp (GB-style)"
engine_id = "codec.pixel.planar"
[params]
bpp = 2
planes = [{ base = 0, stride = 2 }, { base = 1, stride = 2 }]
```

Planes as `{base, stride, group_stride}`, `k` the plane number:

| Preset | Layout / settings |
|---|---|
| `1bpp-8x8`, `1bpp-16x16`, `1bpp-16x12-ff5`, `1bpp-16x11-ff6` | `{0,1}`; `{0,2,1}`; `{0,1,12}`; `{1,2,-1}`, the right half first |
| `snes-2bpp`, `gb-2bpp`, `ws-2bpp`; `nes-2bpp` | `{0,2} {1,2}`; `{0,1} {8,1}` |
| `pce-2bpp16` | 16x16, `{0,2,1} {32,2,1}` |
| `3bpp-planar`, `5bpp-planar` to `8bpp-planar`; `3bpp-planar-separate` | `{k,bpp}`: planes interleaved per row; `{8k,1}` |
| `snes-3bpp`, `snes-3bpp-as-4bpp` | `{0,2} {1,2} {16,1}`; the second with `palette_bpp = 4` |
| `snes-4bpp`, `pce-4bpp`; `snes-8bpp` | `{0,2} {1,2} {16,2} {17,2}`; `{16j,2} {16j+1,2}` for j = 0..3 |
| `sms-4bpp`, `pce-4bpp-flat-planes`, `pce-4bpp-sprite` | `{k,4}`; `{8k,1}`; 16x16 `{32k+1,2,-1}` |
| `4bpp-planar-16x16`, `4bpp-planar-32x32` | `{k,8,4}`; `{k,16,4}` |

Errors: `bpp` outside 1 to 8 or not the `planes` count; a plane without `base` or
`stride`; an offset outside the tile; `tile_width` not a multiple of 8.

### 2.2 Packed

`codec.pixel.packed`. A pixel's index is one `bpp`-bit field stored whole; a
byte holds `8 / bpp` pixels, left to right.

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `bpp` | int | required | 1, 2, 4 or 8. With `nibble_stride`: 2, 4 or 8 |
| `msb_first` | bool | `false` | `true`: pixel 0 in the high field (Mega Drive 4bpp). `false`: low field (GBA 4bpp, Virtual Boy) |
| `reverse_bytes` | bool | `false` | Read each row's bytes right to left (Neo Geo Pocket) |
| `nibble_stride` | int | `0` (off) | Split each index across two bytes `n` apart: high `bpp / 2` bits first. Must divide half the tile's bytes. 1 after a two-part join |
| `tile_width` | int | `8` | Multiple of the pixels a byte holds: `8 / bpp`, or `8 / (bpp / 2)` with `nibble_stride` |
| `tile_height` | int | `8` | Positive |

```toml
id = "preset.pixel.example-packed"
name = "Example packed 4bpp"
engine_id = "codec.pixel.packed"
[params]
bpp = 4
msb_first = true
```

| Preset | Layout / settings |
|---|---|
| `genesis-4bpp` | bpp 4, `msb_first`. Mega Drive, 32X, X68000, MSX |
| `gba-4bpp`, `psx-4bpp` | bpp 4, low nibble first |
| `vb-2bpp` | bpp 2, low bits first |
| `ngp-2bpp`, `ngp-2bpp-swapped` | bpp 2, `msb_first`; the second adds `reverse_bytes` |
| `4bpp-linear-16x16-lsb`, `-msb`, `4bpp-linear-32x32-lsb`, `-msb` | bpp 4, 16x16 or 32x32, low or high nibble first |
| `8bpp-linear`, `psx-8bpp` | bpp 8: one byte per pixel. SNES Mode 7 |
| `8bpp-linear-128x8`, `8bpp-linear-256x128`, `snes-8bpp-bank` | bpp 8 bitmaps: 128x8; 256x128, Super FX; 128x128, a Mode 7 bank |

Errors: a field width not dividing 8; `nibble_stride` with an odd `bpp` or not
dividing half the tile; `tile_width` not a whole number of bytes.

### 2.3 Packed, fields straddling bytes

`codec.pixel.packed-straddling`. The packed depths that do not divide 8. A
separate engine from 2.2: both packings are fixed and the tile is always 8x8.

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `bpp` | int | required | `3`: a row is three bytes. `6`: a row is six bytes, read in reverse byte order |

```toml
id = "preset.pixel.example-packed-straddling"
name = "Example packed 3bpp (fields straddle bytes)"
engine_id = "codec.pixel.packed-straddling"
[params]
bpp = 3
```

| Preset | Layout / settings |
|---|---|
| `3bpp-linear`, `6bpp-linear` | bpp 3, 24 bytes a tile; bpp 6, 48 bytes |

Errors: `bpp` other than 3 or 6.

### 2.4 Nibble-planar

`codec.pixel.nibble-planar`. A byte holds four pixels: its high nibble is one
bitplane of them, its low nibble the next, bit 3 the leftmost pixel. Above 2bpp a
four-pixel group takes `bpp / 2` bytes, most significant plane pair first.

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `bpp` | int | required | 2, 4, 6 or 8 |
| `tile_width` | int | `8` | Multiple of 4 |
| `tile_height` | int | `8` | Positive |

```toml
id = "preset.pixel.example-nibble-planar"
name = "Example nibble-planar 2bpp"
engine_id = "codec.pixel.nibble-planar"
[params]
bpp = 2
```

| Preset | Layout / settings |
|---|---|
| `atari-sy2-chars-2bpp`, `atari-sy2-sprites-4bpp` | 8x8 2bpp; 16x16 4bpp |
| `atari-sy2-tiles-4bpp` | 8x8 4bpp, over a buffer joined by `reshape.split-planes-2` |

Errors: `bpp` not an even 2 to 8; `tile_width` not a multiple of 4.

### 2.5 Direct color

`codec.pixel.direct-color`. Pixels that carry their own color, no palette. A
pixel's bytes are assembled by `byte_order`; each channel is sliced out and
scaled to 8 bits. The kernel of `codec.palette.mask` (3.1).

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `fields` | string | required | Bit layout of the assembled value |
| `legend` | table | none | Values `r`, `g`, `b`, `a`, `v` |
| `bytes_per_pixel` | int | layout bits / 8 | Must match the layout |
| `byte_order` | string | `"little"` | `"little"` or `"big"`. Independent of `fields` |
| `tile_width`, `tile_height` | int | `8` | Any positive size |

| Letter | Field |
|---|---|
| `r` `g` `b` `a` | red, green, blue, alpha. No `a`: opaque |
| `v` | one brightness driving r, g and b; overrides them. Written as the OR of the three |

A channel of 8 bits or fewer round trips exactly; a wider one loses its low bits.

```toml
id = "preset.pixel.example-direct-color"
name = "Example direct color RGB565"
engine_id = "codec.pixel.direct-color"
[params]
fields = "rrrr rggg gggb bbbb"
byte_order = "little"
```

Every multi-byte layout ships little-endian and, as `<name>-be`, big-endian.

| Preset | Layout / settings |
|---|---|
| `dc-rgb565`, `dc-bgr565`, `dc-argb4444` | `rrrr rggg gggb bbbb`; `bbbb bggg gggr rrrr`; `aaaa rrrr gggg bbbb` |
| `dc-rgb555`, `dc-bgr555`, `dc-argb1555`, `dc-abgr1555` | `.rrr rrgg gggb bbbb`; `.bbb bbgg gggr rrrr`; the same two with `a` for `.` |
| `dc-rgba5551`, `dc-bgra5551` | `rrrr rggg ggbb bbba`; `bbbb bggg ggrr rrra`. `dc-rgba5551-be` is N64 RGBA16 |
| `dc-rgb888`, `dc-bgr888`, `dc-argb8888`, `dc-abgr8888`, `dc-rgba8888`, `dc-bgra8888` | 3 or 4 bytes, one a channel, in the named order from the top |
| `dc-snes-direct`, `dc-bbgggrrr` | `bbgg grrr`: the SNES direct-color byte |
| `dc-i8`, `dc-ia8`, `dc-ia16-be` | N64: `vvvv vvvv`; `vvvv aaaa`; `vvvv vvvv aaaa aaaa` |

Errors: bit count not the pixel width; `bytes_per_pixel` not matching or not
positive; `byte_order` outside its set.

### 2.6 Palette swatches

`codec.pixel.palette-swatch`. Decodes the bytes with a palette format and draws a
solid 8x8 swatch per entry; painting writes the entry back through that format.
A tile is one read unit of the format, so several entries per unit give a tile
that many swatches wide.

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `palette_preset_id` | string | `"preset.palette.bgr555"` | A palette preset id. The toolbar's palette-format picker overrides it per entry |
| `tile_width`, `tile_height` | | | Ignored |

```toml
id = "preset.pixel.example-palette-swatch"
name = "Example palette swatches (RGB888)"
engine_id = "codec.pixel.palette-swatch"
[params]
palette_preset_id = "preset.palette.rgb888"
```

| Preset | Layout / settings |
|---|---|
| `view-as-palette` | "View as Palette" in the Pixels dropdown. Sets no palette format |

## 3. Palette stage

### 3.1 Color mask

`codec.palette.mask`. Fixed-size entries storing color as bit fields, assembled
by `byte_order` and scaled to 8 bits a channel.

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `fields` | string | required | Bit layout of the assembled entry |
| `legend` | table | none | Values `r`, `g`, `b`, `a`, `v` |
| `bytes_per_entry` | int | from the layout | Must match the layout. Also the stride of a palette read window |
| `bits_per_entry` | int | none | Instead of `bytes_per_entry`. 1, 2 or 4: `8 / n` entries a byte, from the low bits up. 8 or more: a multiple of 8 |
| `byte_order` | string | `"little"` | `"little"` or `"big"`. Independent of `fields` |
| `invert` | bool | `false` | Complement every field, alpha included, before scaling: greyscales that count darkness |
| `alpha_max` | int | all ones | The alpha value meaning opaque, 1 to all ones. Above it draws opaque. Needs an `a` field |

| Letter | Field |
|---|---|
| `r` `g` `b` `a` | red, green, blue, alpha. No `a`: opaque |
| `v` | one shade driving r, g and b; overrides them. Written as the color's luma |

```toml
id = "preset.palette.example-color-mask"
name = "Example RGB555"
engine_id = "codec.palette.mask"
[params]
fields = ".rrr rrgg gggb bbbb"
byte_order = "little"
```

| Preset | Layout / settings |
|---|---|
| `bgr555`, `bgr555-snes`, `bgr555-psx`, `bgr555-be` | `.bbb bbgg gggr rrrr`. SNES, GBC, GBA, PlayStation; the last big-endian |
| `rgb555-split-be` | `rrrr gggg bbbb rgb.`, big-endian: each channel in two runs |
| `rgb565`, `rgb565-be`; `grb333`, `grb333-be` | `rrrr rggg gggb bbbb`; `.... .ggg .rrr .bbb` |
| `genesis-9bpp`, `bgr333`; `genesis-9bpp-le`, `bgr333-le` | `.... bbb. ggg. rrr.`, big-endian (Mega Drive CRAM); little-endian |
| `pce-grb333` | `.... ...g ggrr rbbb`. PC Engine VCE |
| `bgr444`, `bgr444-be`, `rgb444` | `.... bbbb gggg rrrr`; `rgb444` is `.... rrrr gggg bbbb`, WonderSwan Color |
| `bgr222`, `sms-6bpp` | `..bb ggrr`. Master System CRAM |
| `rgb888` | 3 bytes big-endian, one byte a channel |
| `rgba8888`, `abgr8888`, `rgba8888-ps2` | `aaaa aaaa bbbb bbbb gggg gggg rrrr rrrr`: little-endian; big-endian; little-endian with `alpha_max = 128` |
| `gb-bgp`, `ws-gray`, `ngp-gray` | `invert`: `vv` at `bits_per_entry = 2`; `.vvv` at 4; `.... .vvv` |

Errors: bit count not the entry width; both `bytes_per_entry` and
`bits_per_entry`; `bits_per_entry` neither a divisor nor a multiple of 8;
`byte_order` outside its set; `alpha_max` without `a` or out of range.

### 3.2 Color indexed

`codec.palette.indexed`. Each palette byte indexes a color table fixed in the
machine. An entry is one byte; every color decodes opaque.

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `colors` | list of int | required | `0xRRGGBB` values in hardware order, 1 to 256 |
| `shared_every` | int | `0` (off) | Every Nth entry shows entry 0's color; editing one edits entry 0 |
| `prefer` | list of int | none | Indices a tie resolves to, first listed first |

Decode is a lookup; a byte past the table decodes to the missing-color sentinel.
Encode picks the nearest entry by Manhattan distance in RGB; a tie goes to the
first `prefer` entry among them, else the lowest index.

```toml
id = "preset.palette.example-color-indexed"
name = "Example 8-color indexed"
engine_id = "codec.palette.indexed"
[params]
colors = [0x000000, 0x0000AA, 0x00AA00, 0x00AAAA, 0xAA0000, 0xAA00AA, 0xAA5500, 0xAAAAAA]
```

| Preset | Layout / settings |
|---|---|
| `ega-indexed` | 16 colors |
| `msx-indexed` | 16 colors, `prefer = [1]`: black that is not transparent |
| `nes-indexed`, `nes-screen` | the 64-entry NES master palette, `prefer = [0x0F]`; the second with `shared_every = 4` |

## 4. Tilemap stage

### 4.1 Keys read by celPix on any tilemap preset

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `layout` | string | plain grid | `"text"`: character codes, adds View ▸ Text. `"sprite"`: parts grouped into frames |
| `subsprite_size` | string | unset | `"stated"`: each part's size is in the preset; the toolbar offers no size pair |
| `column_major` | bool | `false` | Grid: cells run down each column, display only. Sprite: a part's tiles run down each column |
| `palette_row_base` | int | `0` | Palette row a cell's row 0 means. Entry, container and bound bank can override it |
| `indirect` | bool | `false` | Cells are coordinates into another map: changes the bind prompt and labels the base "Base cell" |
| `source_hint` | string | `"Tilemap"` | With `indirect`: what the bind dialog calls the source |
| `stamp_cells` | [int, int] | see meaning | Source cells one coordinate names. Default `cell_tiles` where above [1, 1], else the source's, else [1, 1] |
| `stamp_dense` | bool | `false` | One entry per stamp, not per drawn position. Always true where `cell_tiles` sets the stamp |
| `index_addressing` | string | `"corner"` | `"corner"`: the index is the unit's first cell. `"ordinal"`: it counts units. `indirect-record` defaults to ordinal |

A table other maps stamp from, any engine:

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `offered_stamp_cells` | [int, int] | `stamp_cells` | The stamp a map above names, where it differs from the one this table draws |
| `stamp_stride` | int | the shown width | Cells between a stamp's rows, for records stored end to end |
| `stamp_order` | string | `"row"` | `"row"` (TL TR BL BR) or `"column"` (TL BL TR BR). `"column"` needs `stamp_stride` |
| `record_shape` | [int, int] | the offered stamp, when `stamp_stride` is its side | Draw as fixed records. Not on a paged, column-major or chained map, or a count of partial records |
| `record_order` | string | `"row"` | `"row"` or `"column"` |
| `record_header` | int | `0` | Cells of each record before its stamp, 0 to cells - 1. A record key on `indirect-record` (4.6) |

Errors: a word outside its set; a non-boolean flag; `"column"` without
`stamp_stride`. A malformed stamp or record number is dropped with a warning.

### 4.2 Packed cell

`codec.tilemap.packed`. Fixed-width cells in rows, each one integer with its
fields at fixed bit positions. Nearly every hardware map.

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `fields` | string | required | Bit layout of the assembled cell; whole bytes |
| `legend` | table | none | Values `index`, `palette`, `priority`, `flip_h`, `flip_v`, `drawn`, `terminator`, `flags` |
| `bytes` | int | the layout's | Must match the layout |
| `endian` | string | `"little"` | `"little"` or `"big"`. Independent of `fields` |
| `cell_tiles` | [int, int] | `[1, 1]` | Tiles a cell draws; `[2, 2]` is a 16x16 metatile. Over a map, a stamp of its cells |
| `cell_row_stride` | int | `16` | Bank distance from a metatile cell's upper tiles to its lower. Over a tile bank only |
| `cell_column_stride` | int | `1` | Bank distance from its left tiles to its right. Over a tile bank only |
| `page_columns`, `page_rows` | int | unset | Hardware page size. Applied when all three page keys are set, the cell count is whole pages, and the container states no page size |
| `page_counts` | list of int | none | Page counts the format comes in |

| Letter | Field |
|---|---|
| `i` `p` | tile index, palette row |
| `h` `v` | horizontal, vertical flip |
| `o` | priority, stored not rendered |
| `d` | drawn: the position draws only when set |
| `e` | ends the line, text formats |
| `f` | flags, no meaning to celPix, preserved |

A letter not used is a field the format lacks; its cell control is not offered.

Mixed one- and two-byte text. Needs `fields = "iiii iiii"` and no `side_fields`:

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `lead_codes` | list | none | Codes or inclusive `[first, last]` ranges. A lead and the next byte read as one cell, `(lead - lead_bias) * 256 + next` |
| `lead_bias` | int | `0` | Subtracted from a lead. Every letter stays above $FF |
| `operands` | table | none | Command codes keyed by how many following bytes they read raw: `{ 1 = [0x11], 2 = [[0x08, 0x0F]] }`. Needs `lead_codes`; none a lead |
| `line_bytes` | int | none | Fixed records of this many bytes, one line each. With or without `lead_codes` |

Bits stored outside the cell, in a "Side array" input ([Plugin Inputs](Plugin-Inputs)):

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `side_fields` | string | none | Layout of one side word, cell letters, its bits above the cell's. Read-only |
| `side_cells` | int | `1` | Consecutive cells one side word covers |
| `side_key` | string | `"position"` | `"position"`: word `k` covers cells from `k * side_cells`. `"index"`: word `index // side_cells`; past the end reads zero |

```toml
id = "preset.tilemap.example-packed"
name = "Example packed cell (16-bit)"
engine_id = "codec.tilemap.packed"
[params]
fields = "vhop ppii iiii iiii"
endian = "little"
```

| Preset | Layout / settings |
|---|---|
| `snes-bg`, `snes-bg-swapped`, `snes-bg-16x16` | `vhop ppii iiii iiii`: 32x32 pages (1, 2, 4); big-endian, no pages; `cell_tiles = [2, 2]` with pages |
| `snes-mode7` | `iiii iiii`, one 128x128 page. Also GBA affine |
| `gba-bg` | `pppp vhii iiii iiii`, 32x32 pages (1, 2, 4) |
| `gbc-bg` | `ovhf ippp iiii iiii`: a split index |
| `gb-bg`, `index-8bit`, `scgcad-std` | `iiii iiii` |
| `md-bg` | `oppv hiii iiii iiii`, big-endian |
| `sms-bg` | `fffo pvhi iiii iiii`. Also Game Gear |
| `pce-bat` | `pppp iiii iiii iiii` |
| `ngp-bg`, `ngpc-bg` | `hvpf fffi iiii iiii`; `hvfp pppi iiii iiii` |
| `ws-bg` | `vhip pppi iiii iiii` |
| `stamp-table-8bit-row`, `stamp-table-8bit-column`, `snes-stamp-table-column` | `stamp_cells = [2, 2]`, `stamp_stride = 2`; `-column` adds `stamp_order = "column"`; the SNES one has `vhop ppii iiii iiii` cells |
| `text-8bit`, `text-8bit-flag`, `text-16bit`, `text-16bit-be` | `layout = "text"`: `iiii iiii`; `eiii iiii`; 16-bit codes, little or big |
| `scgcad-map`, `scgcad-panel` | big-endian: `fdii iiii iiii iiii`, `indirect`, `source_hint = "PNL panel"`; `vhop ppii iiii iiii` |

Errors: layout not whole bytes; `bytes` not matching; `endian` outside its set;
lead-code keys on another cell or with `side_fields`; `lead_bias` or `operands`
without `lead_codes`; a two-byte letter at or below $FF; `line_bytes` below 1;
`side_key = "index"` with index bits in `side_fields`. Encode refuses a cell
that reads back differently, and a line not `line_bytes` long.

### 4.3 Sprite record

`codec.tilemap.sprite-record`. Sprite parts at pixel offsets, grouped into
frames: any record with fields at fixed byte positions, back to back or as
parallel arrays. Pixels edit through the bound bank; records do not
([Sprites](Sprites)).

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `layout` | string | required | `"sprite"` |
| `subsprite_size` | string | see meaning | `"stated"`. Leave it out only for a lone `s` bit whose pair is unknown: the Sm/Lg spins then set it per entry |
| `column_major` | bool | `false` | A part's tiles run down each column |
| `endian` | string | `"big"` | Every multi-byte field and the frame count |
| `record` | list of tables | required | Fields in file order, at most 64 bytes: `name`, `type` (`u8` `s8` `u16` `s16` `u32` `s32`), optional `bits` layout |
| `legend` | table | none | Values `index`, `palette`, `priority`, `flip_h`, `flip_v`, `columns`, `rows`, `size`, `x`, `y` |
| `frame_header` | table | none | `{ bytes, count_at, count_type }`: frames back to back, each after `bytes` header bytes holding its part count at `count_at` (default 0) as `count_type` (default `"u16"`). None: one frame |
| `arrays` | list of lists | none | Fields as parallel arrays of `count` elements, listing every field once in order. Needs `frame_header` |
| `subsprite_tiles` | [int, int] | `[1, 1]` | Every part's size, for a record with no `c`/`r` or `s` bits |
| `sizes` | list of [int, int] | none | Two or more sizes an `s` code indexes; past the end draws 1x1 with a notice. Needs `"stated"` |
| `size_pair` | [int, int] | entry's, `[1, 2]` | `[small, large]` squares in tiles: a lone `s` bit 0 picks small, 1 large. Needs `"stated"` |
| `palette_row_base` | int | `0` | Palette row a part's row 0 means |

| Letter | Field |
|---|---|
| `i` `p` `o` | tile index, palette row, priority |
| `h` `v` | horizontal, vertical flip |
| `c` `r` | width, height in tiles, minus 1 |
| `s` | size code |
| `x` `y` | x, y offset |
| `.` | unused bit |

| Rule | |
|---|---|
| Offset | The part's `x`/`y` bits, else the whole field named `x`/`y`, else 0 |
| Sign | `x`/`y` bits take their field's sign at their own width: `sxxx xxxx` is -64..63 on `s8`, 0..127 on `u8`. Other letters are unsigned |
| Size | `c`/`r` bits, else the `s` code, else `subsprite_tiles` |
| Preserved | Other fields, `.` bits and other header bytes; the count is rewritten |

```toml
id = "preset.tilemap.example-sprite-record"
name = "Example sprite record"
engine_id = "codec.tilemap.sprite-record"
[params]
layout = "sprite"
subsprite_size = "stated"
column_major = true
record = [
  { name = "x",        type = "s16" },
  { name = "x_mirror", type = "s16" },
  { name = "y",        type = "s16" },
  { name = "pad",      type = "u8" },
  { name = "size",     type = "u8",  bits = "....ccrr" },
  { name = "attr",     type = "u16", bits = "oppv hiii iiii iiii" },
]
```

| Preset | Layout / settings |
|---|---|
| `nes-oam` | `y.s8 tile.u8 attr.u8 x.s8`, attr `vho. ..pp`, `palette_row_base = 4` |

Errors: an unknown type; a record above 64 bytes; a letter placed by two fields;
`x` bits beside a field named `x` (or `y`); `s` with `c`/`r`; a wider `s` without
`sizes`; `sizes` without `s`, under two pairs or without `"stated"`; `size_pair`
without `"stated"` or a lone `s`; `"stated"` on a lone `s` without `size_pair`; a
count outside `frame_header`'s bytes; bad `arrays`; a frame count of 0, or past
the data.

### 4.4 Mega Drive sprite

`codec.tilemap.md-sprite`. The VDP sprite record, `y.b size.b attr.w x[.b|.w]
[x_mirror.b]`: `size` is `....ccrr` (1x1 to 4x4, tiles down each column), `attr`
is `oppv hiii iiii iiii`. No frame header: the entry starts at the first record.

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `layout` | string | required | `"sprite"` |
| `subsprite_size` | string | required | `"stated"` |
| `x_bytes` | int | `1` | `1`: signed byte. `2`: signed big-endian word |
| `mirror_x` | bool | `true` | A trailing signed byte holds the mirrored X, `-(x + width_in_pixels)` |
| `endian` | string | `"big"` | Byte order of the attribute word |
| `palette_row_base` | int | `0` | Palette row a part's row 0 means |

`x_bytes = 1` with `mirror_x` and `x_bytes = 2` without are both six bytes. With
`mirror_x`, a mirror byte that disagrees with its X raises a notice.

```toml
id = "preset.tilemap.example-md-sprite"
name = "Example rectangular sprite object"
engine_id = "codec.tilemap.md-sprite"
[params]
layout = "sprite"
subsprite_size = "stated"
```

| Preset | Layout / settings |
|---|---|
| `md-sprite` | the defaults above |

Errors: `x_bytes` other than 1 or 2; `endian` outside its set.

### 4.5 Sprite table

`codec.tilemap.sprite-table`. 2-byte offsets, one per frame, from the table's
first byte. The entry is the table; a save writes back only the offsets. Frames
are read from the entry's **Frames** input, a region starting at the table
([Plugin Inputs](Plugin-Inputs)). Takes every parameter of 4.3 with its letters
and rules; `endian` also orders the offsets.

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `frame_header` | table | required | As 4.3 |
| `grid_frames` | bool | `false` | A frame whose first byte is 0 or has bit 7 set is a grid: `x.w y.w columns-1.b rows-1.b`, then words (0 empty) drawn as 8x8 tiles. Needs a `u8` count at `count_at = 0`; a count of 0 then reads as a grid |

Each distinct offset is one frame, in table order. A count of 0 is an empty
frame. A frame past the Frames region draws empty, with a notice.

```toml
id = "preset.tilemap.example-sprite-table"
name = "Example sprite mapping table"
engine_id = "codec.tilemap.sprite-table"
[params]
layout = "sprite"
subsprite_size = "stated"
column_major = true
record = [
  { name = "y",    type = "s8" },
  { name = "size", type = "u8",  bits = "....ccrr" },
  { name = "attr", type = "u16", bits = "oppv hiii iiii iiii" },
  { name = "x",    type = "s8" },
]
frame_header = { bytes = 1, count_at = 0, count_type = "u8" }
```

| Preset | Layout / settings |
|---|---|
| `sega-mappings-5` | count byte; `y.b size.b attr.w x.b`. Sonic 1, Sonic CD, Phantasy Star II |
| `sonic2-mappings` | count word; `y.b size.b attr.w attr_2p.w x.w` |
| `sonic3k-mappings` | count word; `y.b size.b attr.w x.w` |

Errors: those of 4.3, except a count of 0; no `frame_header`; `grid_frames`
without a `u8` count at 0.

### 4.6 Indirect record

`codec.tilemap.indirect-record`. One byte per metatile, each the number of a
record in a definition table: another tilemap entry, bound as the source. The
record keys locate record `k`'s first cell. No flips, no writable palette row.

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `record_cells` | int | `4` | Cells per record. End to end, record `k` starts at cell `k * record_cells` |
| `records_per_row` | int | `0` (end to end) | A grid N records across: `k` starts at `(k // N) * N * record_cells + (k % N) * record_width` |
| `record_width` | int | `2` | Record width in cells, on a grid |
| `record_header` | int | `0` | Cells before what a record draws; whole rows of `record_width` on a grid |
| `group_bits` | int | `0` (off) | 1 to 7 top bits select one of several tables; the rest are the record |
| `group_starts` | list of int | with `group_bits` | Each table's first record, `2^group_bits` ascending values |
| `group_rows` | bool | `false` | The table number is the cell's palette row |

A preset stating `index_addressing = "ordinal"` and no record key takes the shape
from its bound table (4.1). The binding's base counts records. A table reaches
`2^(8 - group_bits)` records from its start; a record none reaches is not offered.

```toml
id = "preset.tilemap.example-indirect-record"
name = "Example indirect record map (byte -> metatile record)"
engine_id = "codec.tilemap.indirect-record"
[params]
record_cells = 4
indirect = true
stamp_cells = [2, 2]
stamp_dense = true
```

| Preset | Layout / settings |
|---|---|
| `metatile-index` | 2x2 records end to end, `indirect`, `stamp_cells = [2, 2]`, `stamp_dense` |

## 5. Reshape stage

### 5.1 Bitswap

`reshape.bitswap`. Permutes the byte address; values are unchanged. N entries
define a `2^N`-byte block, permuted block by block; a shorter tail passes
through. The inverse is derived, so edits write back.

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `bits` | list of int | required | MAME `bitswap<N>` argument order: entry `k` names the source address bit of output bit `N - 1 - k`. 1 to 24 entries |
| `gather` | bool | `false` | `false`: `out[bitswap(i)] = in[i]`. `true`: `out[i] = in[bitswap(i)]` |

```toml
id = "reshape.gaelco-16x16"
name = "Gaelco 16x16 tiles (bitswap)"
engine_id = "reshape.bitswap"
[params]
bits = [19, 18, 17, 16, 15, 12, 11, 10, 9, 8, 7, 6, 5, 14, 13, 4, 3, 2, 1, 0]
```

Shipped presets: none; a table is specific to its board.

Errors: not a permutation of `0..N-1`; over 24 entries; over 16 lines from the
lowest line the table moves up to line `N - 1`; a region smaller than one block.

### 5.2 Data LUT

`reshape.data-lut`. Substitutes the byte value; bytes do not move. Each table is
a 256-entry bijection, so edits write back. Give exactly one of `bitswaps` and
`luts`.

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `unit` | int | `1` | `1`: bytes. `2`: big-endian words, a trailing odd byte passes through; needs `bitswaps` |
| `selector_bits` | list of int | none: one table | Offset bits selecting the table, low first. Up to 4, each 0 to 20. N bits need `2^N` tables. No bit 0 at `unit = 2` |
| `selector_remap` | list of int | none | The table index per selector value, one distinct index per table |
| `bitswaps` | list of lists | | Tables as bit permutations, MAME `bitswap<>` order, one per selector value |
| `luts` | list of lists | | Literal 256-value tables. `unit = 1` only |

```toml
id = "reshape.nmk-bg"
name = "NMK background GFX descramble"
engine_id = "reshape.data-lut"
[params]
selector_bits = [2, 11, 18]
bitswaps = [
  [3, 0, 7, 2, 5, 1, 4, 6], [1, 2, 6, 5, 4, 0, 3, 7], [7, 6, 5, 4, 3, 2, 1, 0], [7, 6, 5, 0, 1, 4, 3, 2],
  [2, 0, 1, 4, 3, 5, 7, 6], [5, 3, 7, 0, 4, 6, 2, 1], [2, 7, 0, 6, 5, 3, 1, 4], [3, 4, 7, 6, 2, 0, 5, 1],
]
```

| Preset | Layout / settings |
|---|---|
| `reshape.deinterleave-nibbles` | `[[7,5,3,1,6,4,2,0]]`: two 4bpp pixels interleaved bit by bit, regrouped into nibbles |
| `reshape.reverse-nibble-bits` | `[[4,5,6,7,0,1,2,3]]`: leftmost pixel in bit 0 of each nibble |
| `reshape.reverse-byte-bits` | `[[0,1,2,3,4,5,6,7]]`: rows stored lsb-first |

Errors: both or neither table form; a table not a permutation; `luts` at
`unit = 2`; a table count not `2^N`; a bad or repeated selector bit;
`selector_remap` not distinct table indices.

### 5.3 Split parts

`reshape.split-parts`. The region is cut into `parts` equal parts, part `k`
holding every item's `k`-th unit; the join takes a unit from each part in turn,
and save splits them back. ROM chips, and tables stored as parallel arrays. A
tail past whole parts passes through.

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `parts` | int | required | 2 to 64 |
| `unit` | int | `1` | Bytes per unit, 1 to 8 |
| `groups` | int | `1` | Equal groups cut first, 1 to 1024, each joined on its own |
| `clockwise` | bool | `false` | Arrays stored around the stamp, TL TR BR BL. Needs `groups = 2` |
| `lock_columns` | bool | `groups > 1` | Lay each group out as one row and lock Cols to it, on a region of whole `groups * parts * unit` bytes |

```toml
id = "reshape.ff2-field-tables"
name = "FF2 field tables (3 tables of 4 byte arrays)"
engine_id = "reshape.split-parts"
[params]
parts = 4
groups = 3
lock_columns = false
```

| Preset | Layout / settings |
|---|---|
| `split-planes-2` to `-6`, `split-planes-8` | `parts` = the ROM count, byte-wise: bitplanes or byte lanes |
| `split-words-2`, `split-words-4` | 2 or 4 chips of 16-bit words, `unit = 2` |
| `split-words-2x2`, `split-words-2x2-clockwise` | 2x2 stamp table as four word arrays: `parts = 2`, `unit = 2`, `groups = 2`; the second `clockwise` |

Errors: a value out of range; `clockwise` without `groups = 2`;
`lock_columns = true` at `groups = 1`.

### 5.4 PS2 GS swizzle

`reshape.ps2-gs-swizzle`. A 4bpp or 8bpp texture stored as a 32-bit upload
leaves it in GS memory, read back in rows; save restores GS order. Slice exactly
the texture's pixels, no header or CLUT; the height follows from the length.

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `bpp` | int | required | `4` (PSMT4) or `8` (PSMT8) |
| `width` | int | required | Pixels, a power of two from 8 to 1024 |

```toml
id = "reshape.ps2-psmt4-w32"
name = "PS2 GS swizzle (4bpp, 32 wide)"
engine_id = "reshape.ps2-gs-swizzle"
[params]
bpp = 4
width = 32
```

| Preset | Layout / settings |
|---|---|
| `ps2-psmt4-w64` to `-w1024`, `ps2-psmt8-w64` to `-w1024` | 4bpp and 8bpp, every power of two from 64 to 1024 |

Errors: `bpp` other than 4 or 8; a bad `width`; a slice not a whole texture of
this width.

## 6. Compression stage

### 6.1 Compress and reshape

`compression.compress-reshape`. A compression scheme, then a reshape of the
whole unpacked stream, as one Compression picker entry. Load decompresses then
reshapes; save unshapes then compresses ([Compress & Reshape](Compress-and-Reshape)).

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `compression` | string | required | Plugin id: any Compression entry except "None" and another pair |
| `reshape` | string | required | Plugin id: any Reshape entry except "None", reshape presets included |

The pair is self-delimiting if its compression half is, writes back if both
halves do, and takes the inputs its compression half declares.

```toml
id = "compression.rnc2-running-sum"
name = "RNC ProPack method 2 + running sum"
engine_id = "compression.compress-reshape"
[params]
compression = "compression.rnc2"
reshape = "reshape.running-sum"
```

Shipped presets: none; a pairing is specific to a game. **File ▸ New Compress &
Reshape Plugin…** writes one into the open project.

## 7. Where a preset is not enough

A code plugin covers what no engine expresses: [Code Plugin Reference](Code-Plugin-Reference).

| Case | Example |
|---|---|
| A pixel, palette or tilemap format no engine reads | `pixel/_example.py`, `palette/_example.py`, `tilemap/_example.py` |
| A frame reached through pointers other than one offset table | `tilemap/_example.py` |
| A color table loaded from a companion file | `palette/_nes-custom.py` |
| A reordering no reshape engine expresses | `reshape/_example.py` |
| A compression scheme; one reading a table elsewhere in the file | `compression/_example.py`; `compression/_inputs.py` |
| A container: a header, an interleave | `containers/_example.py`, `containers/_tiff.py` |
