# Getting Started

The overworld of **Super Mario World** (`Super Mario World (USA).sfc`): a
palette, four tile sheets and a map. Addresses are hex file offsets; the ROM is
headerless, so `$04:A533` is `$22533`.

> **Work on a copy of the ROM.** celPix writes in place and keeps no backup.

## 1. Open the ROM and save a project

![The empty window](images/01-empty-window.png)

**File ▸ Open pixel data…**, then **File ▸ Save Project As…** (Ctrl+Shift+S) as
`smw.celpix` beside the ROM. A project stores references and settings, not
bytes.

![The ROM, open](images/01-rom-open.png)

## 2. Find the graphics

The tile sheets are compressed 3bpp, back to back. Type `$459F9` in **Offset**
under the canvas and press Enter. Set **Pixel** to **SNES 3bpp (8x8)**.

![The first sheet's bytes, still packed](images/02-raw-bytes.png)

Set **Compression** to **LZ2 improved (SMW, Yoshi's Island)**. A **Decompressed
view** window shows the unpacked sheet.

![The first sheet, unpacked](images/02-lz2-preview.png)

The status line gives the packed size: `structure 0x838 B`. **Jump to Next**
skips to the next sheet: `$459F9`, `$46231`, `$46CBB`…

![The next sheet](images/02-jump-to-next.png)

> **LZ2 improved** unpacks the same as **LZ2** but packs about 10% smaller.
> Packed sheets leave no free space, so an edited sheet must pack no larger.

> **Scan** stops on anything LZ2 can unpack. **Smart Scan** also checks that the
> result looks like graphics.

Go to `$5547E`, the first overworld sheet.

![The overworld's first sheet](images/02-gfx1c-preview.png)

## 3. Take a palette from a save state

The game builds its palette at runtime, so take it from an emulator save state
(Mesen or Snes9x) made on the overworld. In the **Palette** dock, change
**Default** to **Emulator State** and pick the file.

![The console's palette](images/03-palette-loaded.png)

A 3bpp tile uses 8 colours. Set **Palette Row** to 8, or click a swatch in that
row.

![The sheet in its own colours](images/03-palette-row.png)

## 4. Carve the sheets as slices

A slice is a region of the file saved as its own entry.

**File ▸ New Slice from View** fills in offset, compression and length. Name it
`GFX1C`.

![New Slice from View](images/04-new-slice-from-view.png)

![The first slice](images/04-first-slice.png)

For the other three, select the ROM in **Files**, **File ▸ New Slice…**, set
**Compression** to LZ2 improved and leave **Length** blank.

| Name | Offset |
| --- | --- |
| `GFX1D` | `$55C88` |
| `GFX08` | `$4A657` |
| `GFX1E` | `$5667F` |

![New Slice](images/04-new-slice-dialog.png)

![Four slices](images/04-four-slices.png)

## 5. Edit pixels

Open `GFX1C`, set **Zoom** to 6 and press **Pixel Mode** (E). Tools: Select,
Pencil, Eyedropper, Fill, Line, Rect, Ellipse (1–9). Pick a colour in the
Palette dock and draw.

![Drawing on a tile](images/05-pixel-edit.png)

`●` marks unsaved changes. Each stroke is one **Edit ▸ Undo** (Ctrl+Z).

![Undo](images/05-undo.png)

## 6. Assemble the tile window

The map indexes tiles `$000`–`$1FF` in the VRAM window the four sheets load
into. A **composite** is that window: entries laid end to end as one tile
source.

**File ▸ New Composite View…**. **Add source…** in load order: `GFX1C`, `GFX1D`,
`GFX08`, `GFX1E`. Name it `Overworld tiles`.

![New Composite View](images/06-composite-dialog.png)

> Sizes read zero until the composite is first assembled.

Set the palette to the save state again, **Palette Row** 8, **Rows** 32.

![512 tiles](images/06-composite-open.png)

Right-click ▸ **Edit…** shows where each sheet landed.

![The pieces, measured](images/06-composite-edit.png)

## 7. Carve the map

The map is one byte per tile, RLE2-packed at `$22533`. RLE2 has no end marker,
so give a length. Select the ROM, **File ▸ New Slice…**:

| | |
| --- | --- |
| **Content** | Tilemap |
| **Name** | `Overworld Layer 2` |
| **Offset** | `$22533` |
| **Length** | `$1AF8` |
| **Compression** | RLE2 (SMW, no terminator) |

![The map's slice](images/07-tilemap-slice-dialog.png)

Set **Tilemap** to **Tile map (8-bit index only)** and **Cols** to 32.

![The map, with no tiles](images/07-tilemap-unbound.png)

Set **Tiles** (under the canvas) to `Overworld tiles`.

![The overworld](images/07-tilemap-bound.png)

The map is eight stacked 32×32 pages.

![Pages, stacked](images/07-tilemap-zoom-1.png)

## 8. Edit the map

Press **Edit Tiles** (T) and open the **Tile Source** tab. Left-click picks a
tile, right-drag picks a block of them. Click the map to place them.

![Stamping a hill](images/08-stamp.png)

Right-click the map to pick up the tile under the pointer.

## 9. Write the ROM

![Unsaved](images/09-unsaved.png)

**File ▸ Write All** (Ctrl+Shift+W), then **File ▸ Save Project** (Ctrl+S).

![Written](images/09-written.png)

Each stream must repack into its old space. A hill in the lake splits a long run
into three, and the write is refused:

![A map that no longer fits](images/09-write-refused.png)

Undo it, or free space elsewhere in the map. Test in an emulator.

## Where this goes

- [Text and Fonts](Text-and-Fonts): message boxes as text.
- [VRAM Windows](VRAM-Windows): composites with gaps and partial sheets.
- [Pinned Palette Rows](Pinned-Palette-Rows): one sheet in several palette rows.
- [Plugin Inputs](Plugin-Inputs): this map in colour, using its second
  attribute stream.
- [Compress & Reshape](Compress-and-Reshape): maps rearranged after unpacking.
