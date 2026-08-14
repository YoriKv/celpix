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

Files starting with `_` are ignored. Every `_`-prefixed file here is a working
reference: **copy one, drop the underscore, and edit it.** Press <kbd>F5</kbd> in
celPix to reload the folder. celPix rewrites the `_` files at startup so they
track the version you are running — and removes one it no longer ships, so an
example never outlives what it taught. Yours never start with `_` and are never
touched.

## Two kinds of plugin

**`.toml` presets are data.** They name a built-in engine and fill in its
parameters, so a new format is usually a handful of numbers and no code at all.
Nothing executes, and celPix loads them without asking. Start here — most formats
need nothing more.

`pixel/`, `palette/` and `tilemap/` carry one example preset per engine. Pick the
one whose layout matches your format; each names its engine's shipped presets and
every parameter it takes.

- `_planar.toml` — bit *k* of a pixel comes from plane *k* (most console formats)
- `_packed.toml` — a pixel is a field stored whole: sub-byte (Genesis, GBA, …) or
  one whole byte per pixel at 8bpp
- `_nibble-planar.toml` — one byte holds four pixels, a bitplane to each nibble
- `_linear-bespoke.toml` — the 3bpp and 6bpp packings, whose fields straddle bytes
- `_direct-color.toml` — the pixel carries its own colour, no palette
- `_color-mask.toml` — a palette entry's channels as a bit layout (RGB555, …)
- `_color-indexed.toml` — palette bytes index a table baked into the hardware
- `tilemap/_packed.toml` — a cell is one packed integer: tile number in the low
  bits, attributes above it (nearly every hardware map)
- `_md-sprite.toml` — parts carrying signed pixel offsets, drawn as frames rather
  than laid out in rows, each record *stating* its own rectangle. The only sprite
  record with an example here: the other three celPix reads are **formats**, one
  bespoke codec apiece with nothing to parameterise, so there is no TOML for them
  to be an example of (see the `.py` section below)

`reshape/` takes presets too: `_bitswap.toml` for boards that scramble the byte
*address*, `_data-lut.toml` for boards that substitute byte *values*.

**`.py` plugins are code**, for what the engines cannot express. They run with the
app's privileges, so celPix asks before loading one the first time and remembers
your answer; changing the file asks again.

A code file defines one class and one registration call, in one of two shapes.
The interpret stages — `pixel/`, `palette/`, `tilemap/` — write a **format**: a
`FormatInfo` (an id and a name), that stage's decode/encode pair, and
`registry.register_format(...)`. It lands in the picker beside the presets with no
preset to author. A tilemap that has to *declare* something about its cells —
`layout = "text"` for a fontmap, `sprite`, `indirect`, `cell_tiles` — puts those
in its `FormatInfo(..., declares={...})`, which is for what the **app** has to be
told and never for what your own code reads; anything you would read yourself is a
constant in the class. See `tilemap/_example.py`. Every other stage writes a
**plugin**: a `PluginInfo` that names the stage as well, the stage's own pair of
methods, and `registry.register(...)`.

Reach for a format whenever you are implementing **one** codec. A preset is for
parameterising an engine that serves many, and an engine you would ship a single
preset for is a format that has not noticed yet.

Every folder carries an `_example.py` of the right shape for it, and
`containers/_tiff.py` is a full real-world format.

## Where yours appears in the picker

The format pickers group their entries under headings — `Nintendo`, `Sega`,
`Direct color` — with a search box on top. **Yours are not filed among them:**
everything from this folder appears under **Your plugins** and everything from a
project's own `plugins/` under **Project plugins**, both ahead of the shipped
headings, so a handful of yours is never lost among a hundred of theirs. The
`category` field in celPix's own presets is therefore not one to copy — it is set
for you, and a value you write is replaced.

## Plugins that travel with a project

A project can carry its own: put a `plugins/` folder **next to the `.celpix`
file**, with the same subfolders as this one, and celPix loads it while that
project is open (and on <kbd>F5</kbd>). That is how you hand someone a hack — zip
the project folder and the formats go with it, with nothing to install. Anything
you want in *every* project belongs here instead. A project's `.py` plugins are
code from whoever sent you the project, so celPix asks before running one and says
where it came from.

## Writing one

Each `_example.py` documents its own stage in full. In short:

- A plugin carries **both directions**, load and save, on one object.
- The save half is **optional**: ship it and the data can be written back, leave
  it out and celPix opens that data read-only. Nothing else declares this.
- The two halves must be exact inverses. celPix trusts them, so a mismatch
  corrupts saves — test the round trip.
- Interpret code (pixel, palette, tilemap) must be **buffer-relative**: decode
  whatever bytes you are handed, with no assumption about where they sit in the
  file. That is what lets celPix decode only the visible part of a large ROM.
- A **container** says what kind of entry it frames (`content_kinds`), defaulting
  to pixels and tilemaps; set it to `PALETTE` for one that frames a palette file,
  so the two are never offered each other's formats.
- A container's save is handed the **destination**, which on a Save As is empty.
  Write the payload alone there and the file will not reopen as your format, so
  rebuild the framing — or stash what you need on the context during the read, as
  `containers/_tiff.py` does.
- A container may also implement **`describe`**, which fills the container-info
  popup with what it read and what it did about it. Optional and display-only.

If a plugin fails to load, celPix reports it and carries on — check the plugin
issues it lists rather than looking for a crash.
