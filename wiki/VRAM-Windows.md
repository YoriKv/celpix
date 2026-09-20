# VRAM Windows

Composite views of **Super Mario World**'s video memory: gaps held open, and
pieces cut out of other sheets.

A map names its tiles by number in the window of video memory the game had
loaded. A **composite** is that window: entries laid end to end as one tile
source. [Getting Started](Getting-Started#6-assemble-the-tile-window) makes the
plain kind, four sheets in a row. This page is the rest, in the Super Mario World
sample project ([opening it](Plugin-Inputs#1-open-a-project-that-has-plugins)).

## 1. A window is what video memory holds

A level's four sheets come from a load list. The `?`-block, the coin and the
water are in none of them: the game copies them in from a fifth sheet of
animation frames as the level loads.

![A level's background window](images/vram-bg-window.png)

Right-click the window ▸ **Edit…**. Widen the dialog to read the ranges.

![Its pieces](images/vram-bg-pieces.png)

**At** is where a run starts, **Tile** the same place in tiles, **Bytes** its
size. A row with a name alone is a whole entry. A row ending
`[0x001800–0x001880]` is a **byte range** of one — `$80` bytes, four tiles, out
of what `GFX33` unpacks to. The runs of `GFX17` between them are that sheet, cut
where the game writes over it.

A range counts bytes of the source *as unpacked*, so it reaches part of a
compressed sheet, which no slice can.

> The dialog shows a range, keeps it, and lets you move or remove it. It cannot
> make one: **Add source…** always adds a whole entry. A range is written in the
> project file, as `offset` and `length` on the piece:
>
> ```json
> { "entry_index": 115, "measured": 128, "offset": 6144, "length": 128 }
> ```

The sprite windows carry the player the same way: the head and body of small
Mario, out of `GFX32`, over the first tiles of the sprite sheet.

![A sprite window](images/vram-sprite-window.png)

## 2. A gap, held open

A slot is 128 tiles, and one of the sheets `THE END` loads is 64. The game fills
the slot anyway, so the sheets after it start where they always do. A **blank**
run does the same: leave it out, and every tile after it moves.

![A window with a gap](images/vram-blank-window.png)

**File ▸ New Composite View…**, **Add source…** twice, then **Add blank**. A blank
arrives one tile long. Its **Bytes** cell is a box: set it to 2048, 64 tiles of
4bpp. Add the last two sheets, and name it.

![Add blank](images/vram-blank-sized.png)

> Sizes read zero until the composite has been assembled once. After OK,
> **Edit…** shows them measured:

![Measured](images/vram-blank-by-hand-pieces.png)

## 3. A map through a window

`Overworld Layer 2` is bound to `BG VRAM window — overworld main map`. The bar
under the canvas names the source.

![The overworld](images/vram-map.png)

Open the **Tile Source** tab and click a cell: the window is shown in that cell's
palette row, with its tile ringed.

![The tile a cell draws](images/vram-map-tile-source.png)

The ring button beside **Tiles** opens the source itself. **Navigate ▸ Back**
(Alt+Left) returns to the map.

![Jumped to the source](images/vram-map-jump.png)

## 4. Editing through a window

A composite holds no bytes. Draw on one and the edit goes to the entry that owns
the tile: `●` appears on that sheet and on the ROM, never on the window.

![Drawing on a window](images/vram-paint.png)

This tile is a range of `GFX33`, and three other runs take the same range. The
cross appears in all four, each in its own palette row — they are one tile.

![What became unsaved](images/vram-paint-unsaved.png)

A blank run has no source. Drawing on it changes nothing, and the status bar says
so.

![Drawing on a gap](images/vram-paint-blank.png)

The same routing works two steps away: **Pixel Mode** on the overworld map paints
the sheet, through the window.
