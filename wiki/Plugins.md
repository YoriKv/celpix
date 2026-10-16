# Plugins

How celPix finds, loads and trusts plugins. The parameters of each preset
engine are in [Preset Reference](Preset-Reference); the `.py` contract per
stage is in [Code Plugin Reference](Code-Plugin-Reference).

## 1. Folders

| Folder | Where | Loaded |
|---|---|---|
| User plugins | `<app data>/celPix/plugins/`. **File ▸ Open plugins folder…** opens it. Linux `~/.local/share/celPix/plugins`, Windows `%APPDATA%\celPix\plugins`, macOS `~/Library/Application Support/celPix/plugins` | At startup and on **File ▸ Refresh plugins** (F5) |
| Project plugins | `plugins/` beside a `.celpix` file, same subfolders | While that project is open; refreshed with F5 |

Each folder has one subfolder per stage. The subfolder sets a file's stage; no
`stage` key is read. Files directly in the root and in unknown subfolders are
not loaded.

| Subfolder | Stage | A plugin here defines |
|---|---|---|
| `containers/` | container | an on-disk wrapper: a header to skip, an interleave to undo |
| `reshape/` | reshape | a byte reordering applied to a whole region |
| `compression/` | compression | a packing scheme, unpacked before the data is interpreted |
| `pixel/` | interpret | how bytes become tiles |
| `palette/` | interpret | how bytes become colours |
| `tilemap/` | interpret | how bytes become references to tiles: maps, screens, text, sprite frames |

## 2. Examples

| Property | Value |
|---|---|
| Location | Every subfolder of the user plugin folder, plus its `README.md` |
| Naming | Leading `_`: `_planar.toml`, `_example.py.txt` |
| Loaded | No. A `_` file is documentation |
| Maintained | Rewritten at every startup to match the running version; examples no longer shipped are deleted. A file without the `_` prefix is never touched |
| Use | Copy, remove the `_` (and `.txt` on a code example), edit, press F5 |

One `.toml` example per engine lists every parameter and the shipped presets
built on it. One `_example.py.txt` per stage carries that stage's full contract.

## 3. Presets and code plugins

| | `.toml` preset | `.py` code plugin |
|---|---|---|
| Is | Data: an `engine_id` and its `[params]` | Code: one class and a `register(registry)` function |
| Expresses | A layout an engine's parameters cover | A layout no engine covers |
| Executes | Nothing | With the app's privileges |
| Loaded | Without asking | After approval, see §4 |
| Checked | `pixel/`, `palette/`, `tilemap/`: params on first use, so an invalid preset loads and fails on the entry that uses it. `reshape/`, `compression/`: at load, as a load issue | Import at load; failures listed in the load-issues dialog |
| Picker heading | **Your plugins** or **Project plugins** | Same |
| Stages | Every stage with an engine: see [Preset Reference](Preset-Reference) | Every stage |

Preset keys outside `[params]`:

| Key | Required | Meaning |
|---|---|---|
| `id` | yes | Unique and stable. Stored in `.celpix` files; never shown |
| `name` | yes | Shown in the format picker |
| `engine_id` | yes | The engine the params configure |
| `category` | no | Ignored for user and project plugins; the heading is the folder's |

Code plugins come in two shapes:

| Shape | Folders | Info object | Registration |
|---|---|---|---|
| Format | `pixel/`, `palette/`, `tilemap/` | `FormatInfo(id, name, ...)` | `registry.register_format(obj)` |
| Plugin | `containers/`, `reshape/`, `compression/` | `PluginInfo(id, name, ...)` | `registry.register(obj)` |

A format appears in the format picker like a preset. Ids are stored in project
files; keep them stable.

## 4. Trust

| Event | Behaviour |
|---|---|
| New `.py` file | celPix asks before loading it. Default: No |
| Approved | Remembered by the file's content hash |
| Edited after approval, then F5 | Reloads without asking in the same run; asked again at the next launch |
| Declined | Skipped. F5 asks again |
| `.py` in a project's `plugins/` | Asked on every change, refresh included, and the dialog says it came with the project |
| `.toml` | Never asked |

## 5. Rules common to every stage

| Rule | Consequence of breaking it |
|---|---|
| `decode`/`encode` (and `read`/`write`, `reshape`/`unshape`, `decompress`/`compress`) are exact inverses | Not checked by celPix. A mismatch corrupts saves. Test `encode(decode(x)) == x` |
| Save methods (`write`, `unshape`, `compress`) are optional for containers, reshapes and compression | Without one the data opens read-only |
| Every stage has required methods; interpret formats require `encode` | A class missing one is a load issue and its registration is skipped. The list per stage is in [Code Plugin Reference](Code-Plugin-Reference) |
| Interpret code is stateless and buffer-relative | It decodes the bytes it is given, with no knowledge of their file position. celPix decodes only the visible part of a large file |
| Data from outside a stage's bytes is an `InputSpec`, bound per entry | Compression plugins and tilemap formats only. Declared elsewhere it is ignored with a warning at load. See [Plugin Inputs](Plugin-Inputs) |
| Ids are stable | A renamed id orphans every entry that stored it |

## 6. Failures

| Failure | Shown |
|---|---|
| A file fails to load | Skipped; listed in the **plugin load issues** dialog at startup and after F5 |
| A plugin raises while running | A dialog naming the plugin, the stage, the exception and the line, with the traceback under **Show Details**. An error where the operation failed; a warning where celPix worked around it (an optional method) |
| A preset's params are invalid | Reported on the entry that first uses the preset |
| A code plugin's approval prompt was declined | The entries naming it open with no format. F5 asks again |

## 7. The format picker

| Heading | Contents |
|---|---|
| **Your plugins** | Every preset and format from the user plugin folder |
| **Project plugins** | Every preset and format from the open project's `plugins/` |
| Shipped headings | celPix's own presets, by console or family |

User and project headings come first. A `category` value in a user or project
file is replaced by the heading.
