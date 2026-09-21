# Pinned Palette Rows

The tiles in one sheet often use different palette rows. **Palette Row** sets
one row for the whole view. A **pin** shows selected tiles in a fixed row.

Pins change only the display. They do not change the file. They are saved in
the project.

This page uses the Super Mario World sample project. To open it, see
[Plugin Inputs](Plugin-Inputs#1-open-a-project-that-has-plugins).

## 1. A pinned sheet

`GFX14` has pins. They come from the game's Map16 tables.

![GFX14, pinned](images/pins-gfx14.png)

![The Palette menu](images/pins-palette-menu.png)

With **Show Pinned Palette Colors** (Shift+P) off, all tiles use one row:

![One row](images/pins-gfx14-colors-off.png)

**Show Palette Rows** shows the row number on each pinned tile:

![Rows, numbered](images/pins-gfx14-rows.png)

These two settings apply to the app. They are not saved in the project.

## 2. Pin by hand

`GFX00` has no pins.

![GFX00](images/pins-gfx00.png)

1. Set **Selection** to **Rectangle**.
2. Set **Palette Row** to 11.
3. Select the P-switch.

![Row 11, and a selection](images/pins-select.png)

4. Click **Pin**. Also in **Palette ▸ Pin Selection to Palette Row** and the
   canvas right-click menu.

![The canvas menu](images/pins-canvas-menu.png)

5. Set **Palette Row** back to 8. The P-switch stays in row 11.

![Pinned](images/pins-pinned.png)

6. Pin the smiling block to row 10.

A white ring shows the row of the view. A blue ring shows the row of the
selection.

![Two pins](images/pins-two-pins.png)

To remove pins:

- **Unpin** removes the pins in the selection.
- **Palette ▸ Unpin All** removes all pins.

You can undo both.

## 3. The base row

Pins are relative to **Base Palette Row**. If you change the base row, all pins
move with it.

![Base Palette Row 1](images/pins-base-row.png)

## 4. On a tilemap

Each tilemap cell stores its own palette row. On a tilemap, the **Pin** button
is **Set Row**. It writes the row into the selected cells.

![A block table](images/pins-map16.png)

1. Set **Palette Row** to 6.
2. Select the first stamp.
3. Click **Set Row**.

![The cells, written](images/pins-map16-set.png)

This changes the file data. To save it to the ROM: **File ▸ Write**.

## In the project file

Each pin is `[first pixel, count, row]`. Pixels are counted in tile order, 64
pixels per tile:

```json
"palette_regions": [[4096, 128, 10], [4224, 128, 11], [5120, 128, 10], [5248, 128, 11]]
```
