# celPix plugins

This is your plugin folder. Drop files into the subfolders below and celPix picks
them up — no reinstall, no editing the app.

**The folder decides what a file is**, so nothing inside it declares its own type:

| Folder | What goes in it |
|---|---|
| `pixel/` | how bytes become tiles (a tile/character format) |
| `palette/` | how bytes become colours |
| `reshape/` | a byte reordering applied to a whole region |
| `compression/` | a packing scheme, unpacked before the pixel format reads it |
| `containers/` | an on-disk wrapper — a header to skip, an interleave to undo |

Files starting with `_` are ignored. Every `_example.*` here is a working
reference: **copy one, drop the underscore, and edit it.** Press <kbd>F5</kbd> in
celPix to reload the folder.

celPix rewrites these `_`-prefixed files when it starts, so they stay current with
the version you are running. Your own plugins never start with `_`, so they are
never touched.

## Two kinds of plugin

**`.toml` presets are data.** They name a built-in engine and fill in its
parameters, so a new tile or palette format is usually a handful of numbers and no
code at all. Nothing executes, and celPix loads them without asking. Start here —
most formats need nothing more.

`pixel/` and `palette/` have one example preset per engine; pick the one whose
layout matches your format:

- `_planar.toml` — bit *k* of a pixel comes from plane *k* (most console formats)
- `_packed.toml` — a pixel is a field stored whole: sub-byte (Genesis, GBA, …)
  or, at 8bpp, one whole byte per pixel
- `_nibble-planar.toml` — one byte holds four pixels, a bitplane to each nibble
- `_linear-bespoke.toml` — the 3bpp and 6bpp packings, whose fields straddle bytes
- `_direct-color.toml` — the pixel carries its own colour, no palette
- `_color-mask.toml` — a palette entry's channels as bit masks (RGB555, …)
- `_color-indexed.toml` — palette bytes index a table baked into the hardware

Each names what its engine does, which shipped presets are built on it, and every
parameter it takes with the values that parameter accepts.

`reshape/` takes presets too: `_bitswap.toml` for boards that scramble the byte
*address*, `_data-lut.toml` for boards that substitute byte *values*.

**`.py` plugins are code**, for anything the engines cannot express. They run with
the app's privileges, so celPix asks before loading one the first time and
remembers your answer; changing the file asks again. A plugin file defines a class
and a `register(registry)` function — see the `_example.py` in each folder, and
`containers/_tiff.py` for a full real-world format.

## Writing one

Each `_example.py` documents its stage in full. In short:

- A plugin carries **both directions**, load and save, on one object.
- The save half is **optional**: ship it and the data can be written back, leave
  it out and celPix opens that data read-only. Nothing else declares this.
- The two halves must be exact inverses. celPix trusts them, so a mismatch
  corrupts saves — test the round trip.
- Pixel and palette code must be **buffer-relative**: decode whatever bytes you
  are handed, with no assumption about where they sit in the file. That is what
  lets celPix decode only the visible part of a large ROM.

If a plugin fails to load, celPix reports it and carries on — check the plugin
issues it lists rather than looking for a crash.
