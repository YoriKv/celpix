# celPix plugins

This is your plugin folder. celPix loads the files in its subfolders at startup
and on **File ▸ Refresh plugins** (<kbd>F5</kbd>).

| Folder | Stage | A plugin here defines |
|---|---|---|
| `containers/` | container | an on-disk wrapper: a header to skip, an interleave to undo |
| `reshape/` | reshape | a byte reordering applied to a whole region |
| `compression/` | compression | a packing scheme, unpacked before the data is interpreted |
| `pixel/` | interpret | how bytes become tiles |
| `palette/` | interpret | how bytes become colours |
| `tilemap/` | interpret | how bytes become references to tiles: maps, screens, sprite frames |

The folder sets a file's stage. Files directly in this folder and unknown
subfolders are not loaded.

## Examples

Files starting with `_` are ignored. Every `_` file here is a working example:
**copy it, remove the underscore, edit it, press <kbd>F5</kbd>.** celPix rewrites
the `_` files and this README at startup to match the running version, and
deletes examples it no longer ships. It never touches a file without the `_`
prefix.

## `.toml` presets

A preset is data: it names a built-in engine (`engine_id`) and sets its
parameters. Nothing executes, and celPix loads presets without asking. Parameters
are checked when the preset is first used, not at load. There is one example per
engine; each lists every parameter and the shipped presets built on the engine.

| Example | Engine |
|---|---|
| `pixel/_planar.toml` | bitplanes (most console formats) |
| `pixel/_packed.toml` | one field per pixel at 1, 2, 4 or 8bpp (Mega Drive, GBA, …) |
| `pixel/_packed-straddling.toml` | packed 3bpp and 6bpp |
| `pixel/_nibble-planar.toml` | two bitplanes per byte, four pixels each |
| `pixel/_direct-color.toml` | pixels that carry their own colour; no palette |
| `pixel/_palette-swatch.toml` | bytes shown as palette colours, one swatch per entry |
| `palette/_color-mask.toml` | colour channels as bit fields (BGR555, …) |
| `palette/_color-indexed.toml` | bytes index a fixed hardware colour table |
| `tilemap/_packed.toml` | a grid of packed cell words (nearly every hardware map) |
| `tilemap/_sprite-record.toml` | sprite parts as fixed records, in any field order |
| `tilemap/_md-sprite.toml` | the Mega Drive VDP sprite record |
| `tilemap/_indirect-record.toml` | one byte per metatile, naming a definition record |
| `reshape/_bitswap.toml` | an address-line permutation |
| `reshape/_data-lut.toml` | a byte-value substitution |
| `compression/_compress-reshape.toml` | a compression scheme followed by a reshape |

**File ▸ New Compress & Reshape Plugin…** writes a compress-and-reshape preset
into the open project.

## `.py` plugins

Code, for what no engine expresses. A code plugin runs with the app's
privileges, so celPix asks before loading one and remembers the answer by the
file's contents: a changed file is asked about again at the next launch.

A file defines one class and a `register(registry)` function. There are two
shapes:

- **Format** (`pixel/`, `palette/`, `tilemap/`): a `FormatInfo(id, name)`, the
  stage's `decode`/`encode` pair, and `registry.register_format(...)`. It
  appears in the format picker like a preset. Use a format to implement one
  codec; presets parameterise an engine that serves many.
- **Plugin** (`containers/`, `reshape/`, `compression/`): a
  `PluginInfo(id, name)`, the stage's methods, and `registry.register(...)`.

| Example | Shows |
|---|---|
| `*/_example.py` | the minimal plugin for each folder, with its full contract |
| `containers/_tiff.py` | a real format whose payload position is a lookup; notices; Save As |
| `compression/_inputs.py` | a codec that reads a table stored elsewhere in the file |
| `palette/_nes-custom.py` | a format that loads its colour table from a companion file |

Rules common to every stage:

- A plugin implements both directions. `decode`/`encode` and the other pairs
  must be exact inverses: celPix does not check them, and a mismatch corrupts
  saves. Test the round trip.
- For containers, reshapes and compression the save method (`write`, `unshape`,
  `compress`) is optional. Without it the data opens read-only. Interpret
  formats require `encode`.
- Interpret code is stateless and buffer-relative: it decodes the bytes it is
  given, with no knowledge of their position in the file. celPix decodes only the
  visible part of a large file.
- Data from outside a stage's own bytes is declared as an `InputSpec` and bound
  per entry. Compression plugins and tilemap formats only; declared anywhere
  else, the inputs are ignored and celPix warns at load.
- Ids are stored in project files. Keep them stable.

A plugin that fails to load is skipped and listed in the **plugin load issues**
dialog, shown at startup and after a refresh. A plugin that raises while running
is reported in a dialog naming the plugin, the stage, the exception and the line
that raised it, with the traceback under **Show Details**: an error where the
operation failed, a warning where celPix worked around it (an optional method).

## The format picker

Everything from this folder appears under **Your plugins**, and everything from
a project's folder under **Project plugins**, ahead of the shipped headings. A
`category` value in your file is replaced.

## Project plugins

A `plugins/` folder next to a `.celpix` file, with the same subfolders, is loaded
while that project is open. The project's formats then travel with its folder.
celPix asks before running a project's `.py` plugins and states that they came
with the project.
