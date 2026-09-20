# Pinned Palette Rows

Tiles in one sheet often use different palette rows. **Palette Row** sets one
row for the whole view; a **pin** draws chosen tiles in a fixed row. Pins are
display only, saved in the project.

Uses the Super Mario World sample project
([opening it](Plugin-Inputs#1-open-a-project-that-has-plugins)).

## 1. A pinned sheet

`GFX14`, pinned from the game's block tables:

![GFX14, pinned](images/pins-gfx14.png)

![The Palette menu](images/pins-palette-menu.png)

**Show Pinned Palette Colors** (Shift+P) off:

![One row](images/pins-gfx14-colors-off.png)

**Show Palette Rows** labels each pinned tile with its row:

![Rows, numbered](images/pins-gfx14-rows.png)

Both toggles are app settings, not per project.

## 2. Pin by hand

`GFX00` has no pins.

![GFX00](images/pins-gfx00.png)

Set **Selection** to **Rectangle**, **Palette Row** to 11, and select the
P-switch.

![Row 11, and a selection](images/pins-select.png)

Click **Pin** (also in **Palette ▸ Pin Selection to Palette Row** and the canvas
right-click menu).

![The canvas menu](images/pins-canvas-menu.png)

Set **Palette Row** back to 8. The P-switch stays in row 11.

![Pinned](images/pins-pinned.png)

Pin the smiling block to row 10. White ring: view's row. Blue ring: selection's
row.

![Two pins](images/pins-two-pins.png)

**Unpin** clears a selection; **Palette ▸ Unpin All** clears everything. Both
undo.

## 3. The base row

Pins are relative to **Base Palette Row**. Changing it moves every pin.

![Base Palette Row 1](images/pins-base-row.png)

## 4. On a tilemap

Map cells store their own row, so the button becomes **Set Row** and writes it
into the selected cells.

![A block table](images/pins-map16.png)

**Palette Row** 6, select the first block, **Set Row**:

![The cells, written](images/pins-map16-set.png)

This edits the file; write it with **File ▸ Write**.

## In the project file

Each pin is `[first pixel, count, row]` in tile order (64 pixels per tile):

```json
"palette_regions": [[4096, 128, 10], [4224, 128, 11], [5120, 128, 10], [5248, 128, 11]]
```
