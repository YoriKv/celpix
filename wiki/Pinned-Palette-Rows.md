# Pinned Palette Rows

The tiles in one sheet often use different palette rows. **Palette Row** sets
one row for the whole view. A **pin** shows selected tiles in a fixed row.

Pins change only the display. They do not change the file. They are saved in
the project.

Start from the project you made in [Getting Started](Getting-Started).

## 1. Pin by hand

`GFX14` has pipes, `?`-blocks and ground. In row 0, they all show in the
background's colours.

![GFX14](images/pins-gfx14.png)

1. Select `GFX14`.
2. Set **Selection** to **Rectangle**.
3. Set **Palette Row** to 5.
4. Select tiles `$10` to `$15`: the green pipe.

![Row 5, and a selection](images/pins-select.png)

5. Click **Pin**. Also in **Palette ▸ Pin Selection to Palette Row** and the
   canvas right-click menu.

![The canvas menu](images/pins-canvas-menu.png)

6. Set **Palette Row** back to 0. The pipe stays in row 5.

![Pinned](images/pins-pinned.png)

7. Pin tiles `$16` to `$19`, the `?`-block, to row 6.

A white ring shows the row of the view. A blue ring shows the row of the
selection.

![Two pins](images/pins-two-pins.png)

To remove pins:

- **Unpin** removes the pins in the selection.
- **Palette ▸ Unpin All** removes all pins.

You can undo both.

## 2. Show and hide pins

![The Palette menu](images/pins-palette-menu.png)

With **Show Pinned Palette Colors** (Shift+P) off, all tiles use one row:

![One row](images/pins-gfx14-colors-off.png)

**Show Palette Rows** shows the row number on each pinned tile:

![Rows, numbered](images/pins-gfx14-rows.png)

These two settings apply to the app. They are not saved in the project.

## 3. The base row

Pins are relative to **Base Palette Row**. If you change the base row, all pins
move with it.

![Base Palette Row 1](images/pins-base-row.png)

## 4. On a tilemap

Each tilemap cell stores its own palette row. On a tilemap, the **Pin** button
is **Set Row**. It writes the row into the selected cells.

![The stamp table](images/pins-map16.png)

1. Select `Background stamps`.
2. Set **Palette Row** to 1.
3. Select the first four cells: the first stamp.
4. Click **Set Row**.

![The cells, written](images/pins-map16-set.png)

This changes the file data. To save it to the ROM: **File ▸ Write**.

## In the project file

Each pin is `[first pixel, count, row]`. Pixels are counted in tile order, 64
pixels per tile:

```json
"palette_regions": [[1024, 384, 5], [1408, 256, 6]]
```
