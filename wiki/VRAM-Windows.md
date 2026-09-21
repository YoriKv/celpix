# VRAM Windows

A **composite** is a copy of an area of video memory (VRAM). It puts entries one
after another and uses them as one tile source.
[Getting Started](Getting-Started#6-assemble-the-tile-window) makes a simple
composite. This page shows the other features.

This page uses the Super Mario World sample project. To open it, see
[Plugin Inputs](Plugin-Inputs#1-open-a-project-that-has-plugins).

## 1. A window is what video memory holds

When a level loads, the game copies the `?`-block, coin and water tiles from an
animation sheet into VRAM. The composite includes these tiles.

![A level's background window](images/vram-bg-window.png)

1. Right-click ▸ **Edit…**.
2. Make the dialog wider so that all columns show.

![Its pieces](images/vram-bg-pieces.png)

**At**, **Tile** and **Bytes** give the start and the size of each piece.

A row that ends with a range, for example `[0x001800-0x001880]`, is a **byte
range**. It uses only part of an entry. This example is 4 tiles of `GFX33`. The
range counts unpacked bytes, so it can use part of a compressed sheet.

> The dialog can move or remove ranges. It cannot make new ones. **Add
> source…** adds a whole entry. Ranges are stored in the project file:
>
> ```json
> { "entry_index": 115, "measured": 128, "offset": 6144, "length": 128 }
> ```

Sprite windows use the same method to add Mario's tiles from `GFX32`.

![A sprite window](images/vram-sprite-window.png)

## 2. A gap, held open

Each VRAM slot is 128 tiles. One `THE END` sheet is only 64 tiles. A **blank**
piece fills the rest of the slot, so the sheets after it stay in the correct
place.

![A window with a gap](images/vram-blank-window.png)

1. **File ▸ New Composite View…**.
2. Click **Add source…** two times and add the first two sheets.
3. Click **Add blank**.
4. Set the **Bytes** of the blank to 2048. This is 64 tiles of 4bpp.
5. Add the last two sheets.

![Add blank](images/vram-blank-sized.png)

> The sizes show 0 until the composite is opened for the first time:

![Measured](images/vram-blank-by-hand-pieces.png)

## 3. A map through a window

The **Tiles** setting of `Overworld Layer 2` is the overworld main map window.

![The overworld](images/vram-map.png)

In the **Tile Source** tab, click a cell. A ring shows the tile that the cell
uses. The tile shows in the palette row of the cell.

![The tile a cell draws](images/vram-map-tile-source.png)

To open the tile source, click the ring button next to **Tiles**. To go back:
**Navigate ▸ Back** (Alt+Left).

![Jumped to the source](images/vram-map-jump.png)

## 4. Editing through a window

When you draw on a composite, celPix edits the entry that the tile comes from.

![Drawing on a window](images/vram-paint.png)

This tile is in the window four times. All four copies change.

![What became unsaved](images/vram-paint-unsaved.png)

Drawing on a blank piece has no effect.

![Drawing on a gap](images/vram-paint-blank.png)

You can also draw on the map in **Pixel Mode**. celPix edits the sheet through
the window.
