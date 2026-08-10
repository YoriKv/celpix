# celPix plugins

This is your plugin folder. Drop files into the subfolders below and celPix picks
them up — no reinstall, no editing the app.

**The folder decides what a file is**, so nothing inside it declares its own type:

| Folder | What goes in it |
|---|---|
| `pixel/` | how bytes become tiles (a tile/character format) |
| `palette/` | how bytes become colours |
| `tilemap/` | how bytes become *references* to tiles — a map, a screen, a sprite's frames |
| `reshape/` | a byte reordering applied to a whole region |
| `compression/` | a packing scheme, unpacked before the pixel format reads it |
| `containers/` | an on-disk wrapper — a header to skip, an interleave to undo |
| `alphabet/` | what a font's tiles spell, so a text run reads as words |

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

`alphabet/` goes one better: drop a plain **table file** in it — one `20=A` line
per character, or `A=20` with `order = "text-first"` in a preset — and it appears
in the Alphabet picker by itself, named after the file. No preset needed.

`pixel/`, `palette/` and `tilemap/` have one example preset per engine; pick the
one whose layout matches your format:

- `_planar.toml` — bit *k* of a pixel comes from plane *k* (most console formats)
- `_packed.toml` — a pixel is a field stored whole: sub-byte (Genesis, GBA, …)
  or, at 8bpp, one whole byte per pixel
- `_nibble-planar.toml` — one byte holds four pixels, a bitplane to each nibble
- `_linear-bespoke.toml` — the 3bpp and 6bpp packings, whose fields straddle bytes
- `_direct-color.toml` — the pixel carries its own colour, no palette
- `_color-mask.toml` — a palette entry's channels as bit masks (RGB555, …)
- `_color-indexed.toml` — palette bytes index a table baked into the hardware
- `_packed.toml` (in `tilemap/`) — a cell is one packed integer: tile number in
  the low bits, attributes above it (nearly every hardware map)
- `_object.toml` — parts carrying signed pixel offsets, drawn as frames rather
  than laid out in rows
- `_obz.toml` — the same shape with each field in its own byte instead of packed
  into a sprite attribute word
- `_ys-spr.toml` — the same again where frames are *different lengths*, the count
  coming from the container rather than the record

Each names what its engine does, which shipped presets are built on it, and every
parameter it takes with the values that parameter accepts.

`reshape/` takes presets too: `_bitswap.toml` for boards that scramble the byte
*address*, `_data-lut.toml` for boards that substitute byte *values*.

## Where yours appears in the picker

celPix's format pickers group their entries under headings — `Nintendo`, `Sega`,
`Direct color` and so on — and have a search box at the top.

**Yours are not filed among them.** Everything loaded out of this folder appears
under **Your plugins**, and everything out of a project's own `plugins/` folder
under **Project plugins**, and those two headings come *first* in every picker.
Nothing to write and nothing to keep in sync: a handful of your formats scattered
through a hundred shipped ones is the thing the grouping exists to prevent, and
where a file came from is a fact the file cannot state about itself.

So the `category` field you will see in celPix's own presets is not something to
copy — it is set for you, and a value you write is replaced.

**`.py` plugins are code**, for anything the engines cannot express. They run with
the app's privileges, so celPix asks before loading one the first time and
remembers your answer; changing the file asks again. A plugin file defines a class
and a `register(registry)` function — see the `_example.py` in each folder, and
`containers/_tiff.py` for a full real-world format.

## Plugins that travel with a project

A project can carry its own plugins: put a `plugins/` folder **next to the
`.celpix` file**, with the same subfolders as this one, and celPix loads it when
that project opens (and on <kbd>F5</kbd>). They leave again when the project
closes, so they only exist while you are working on it.

That is how you hand someone a hack: zip the project folder and the formats go
with it, with nothing to install. Anything you want in *every* project belongs
here in your own folder instead.

A project's `.py` plugins are code that came from whoever sent you the project,
so celPix asks before running one just as it does for your own — and says the
plugin came with the project.

## Writing one

Each `_example.py` documents its stage in full. In short:

- A plugin carries **both directions**, load and save, on one object.
- The save half is **optional**: ship it and the data can be written back, leave
  it out and celPix opens that data read-only. Nothing else declares this.
- The two halves must be exact inverses. celPix trusts them, so a mismatch
  corrupts saves — test the round trip.
- Interpret code (pixel, palette, tilemap) must be **buffer-relative**: decode
  whatever bytes you are handed, with no assumption about where they sit in the
  file. That is what lets celPix decode only the visible part of a large ROM.
- A **container** says what kind of entry it frames (`content_kinds`). It
  defaults to pixels and tilemaps, which is what almost every wrapper is; set it
  to `PALETTE` for one that frames a palette file, so the two are never offered
  each other's formats.
- A container's save is handed the **destination**, which on a Save As is empty.
  Writing the payload alone there produces a file that is not your format and
  will not reopen as one, so rebuild the framing — or, if it cannot be rebuilt
  from the payload, stash what you need on the context during the read.
  `containers/_tiff.py` does exactly that.
- A container may also implement **`describe`**, which fills the container-info
  popup with what it read and what it did about it. Optional and display-only.

If a plugin fails to load, celPix reports it and carries on — check the plugin
issues it lists rather than looking for a crash.
