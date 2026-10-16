# Code Plugin Reference

Contract for `.py` code plugins, per stage. A file defines one class and a
`register(registry)` function. Folders, loading, approval and the picker:
[Plugins](Plugins). Data-only presets: [Preset Reference](Preset-Reference).

| Shape | Folders | Info class | Registered with |
|---|---|---|---|
| Format | `pixel/`, `palette/`, `tilemap/` | `FormatInfo` | `registry.register_format(obj)` |
| Plugin | `containers/`, `reshape/`, `compression/` | `PluginInfo` | `registry.register(obj)` |

- `id` is stored in project files. Keep it stable.
- Code runs with the app's privileges, so celPix asks before loading a file.
- Every folder ships a working `_example.py`. Copy it, remove the `_`, edit, press <kbd>F5</kbd>.

## 1. Common rules

### 1.1 Rules

| Rule | |
|---|---|
| Inverse pairs | `read`/`write`, `reshape`/`unshape`, `decompress`/`compress`, `decode`/`encode` are exact inverses. celPix does not check them; a mismatch corrupts saves. Test the round trip |
| Optional save half | `write`, `unshape`, `compress` may be omitted. The data then opens read-only |
| Interpret save half | `encode` is required on every format. There is no read-only format |
| Stateless interpret code | `decode`/`encode` process the bytes they are given, with no knowledge of their file position. celPix decodes only the visible window |
| Lossy fields | `encode` masks a value too wide for its field rather than raising. Exception: a tilemap index outside `index_runs()` (§7) |
| Inputs | `InputSpec`s are delivered to compression plugins and tilemap formats only. Declared elsewhere: the plugin loads, the inputs are ignored, a load warning is shown |
| `ctx` | A `PipelineContext`: an open key/value bag, `ctx.get(key, default=None)`, `ctx.set(key, value)`. Keys in §8 |
| Own context keys | Any other string key may be set. celPix reads only its own; the Container Info popup lists the rest under the bare key |
| `stage` | Optional on `PluginInfo`; the folder sets it. A conflicting value skips the registration |
| `category` | Replaced in a plugin folder: **Your plugins** or **Project plugins** |
| Companion files | `__file__` is the plugin's own path |
| Failure | Raise. §9 |

### 1.2 Registry

What `register(registry)` receives.

| Method | Valid in | Effect |
|---|---|---|
| `register(plugin)` | any | Registers a plugin. Checked: id, name, required methods, container signature. In `pixel/`, `palette/`, `tilemap/` it registers an engine whose methods also take `params`, for presets to name |
| `register_format(fmt)` | `pixel/`, `palette/`, `tilemap/` | Registers a format as an engine plus a preset of the same id. Checked: `info`, required methods, id not taken |
| `register_preset(preset)` | `pixel/`, `palette/`, `tilemap/` | Registers a `Preset` (`celpix.plugins.base`) built in code |
| `plugin(stage, id)` | any | Read: one registered plugin |
| `plugins(stage)` | any | Read: every plugin of a stage |

A failed check skips that registration only and is listed as a load issue.

### 1.3 Imports

| Name | Module |
|---|---|
| `PluginInfo`, `FormatInfo`, `InputSpec`, `InputKind`, `ReadSource`, `WriteTarget`, `ContainerField`, `PartialDecompression`, `plain_read`, `splice`, `format_size` | `celpix.plugins` |
| `PipelineContext`, `KEY_*` | `celpix.core.context` |
| `Stage` | `celpix.core.errors` |
| `ContentKind` | `celpix.core.capabilities` |
| `IndexGrid` | `celpix.core.index_grid` |
| `Palette`, `MISSING_COLOR` | `celpix.core.palette` |
| `Cell`, `CellOp` | `celpix.core.tilemap` |
| `Subsprite`, `Frame` | `celpix.core.sprite` |
| `warn`, `inform` | `celpix.core.notices` |

## 2. Containers

`containers/`. Removes on-disk framing (a header, an interleave, a checksum
block) on load and restores it on save. Neither method touches the disk.

### 2.1 `PluginInfo`

| Field | Type | Default | Meaning |
|---|---|---|---|
| `id` | str | required | e.g. `container.example` |
| `name` | str | required | Picker label |
| `stage` | `Stage` | `None` | `Stage.CONTAINER` or omitted |
| `magic` | tuple of `(offset, bytes)` | `()` | Detection probes; any one matching claims the file. When declared, detection uses magic only |
| `extensions` | tuple of str | `()` | Suffixes with the dot, any case. Detection only when no `magic`; also the default suffix in **File ▸ New File…** |
| `exact_size` | int | `0` | Narrows a match to one file length. `0` = any |
| `size_modulo` | `(modulus, remainder)` \| `None` | `None` | Narrows a match: file length modulo `modulus` |
| `min_size` | int | `0` | Narrows a match: minimum file length |
| `short_name` | str | `""` | Tag beside the filename in the Files list. Empty = `name` |
| `content_kinds` | tuple of `ContentKind` | `(PIXELS, TILEMAP)` | Entry kinds this container frames. `(ContentKind.PALETTE,)` for a palette file format |
| `preserves_offsets` | bool | `False` | `True`: every payload byte keeps its file position. Checked on each read; a mismatch warns. Leave `False` for a container that reorders or joins bytes |

- Detection fields are data, read without running plugin code. Their shape is checked at registration.
- `exact_size`, `size_modulo` and `min_size` only narrow a `magic` or `extensions` match; they never match alone.

### 2.2 Methods

| Method | Required | Signature | Returns | Notes |
|---|---|---|---|---|
| `read` | yes | `read(source: ReadSource, ctx) -> bytes` | The payload | Sets `KEY_SOURCE_OFFSET`: the file-absolute offset of the payload's first byte |
| `write` | no | `write(data: bytes, dest: WriteTarget, ctx) -> bytes` | The destination file's complete new contents | Absent: files open read-only |
| `describe` | no | `describe(source: ReadSource, ctx) -> tuple[ContainerField, ...]` | Rows for the Container Info popup | Called after `read`, same `source` and `ctx`, whether or not `read` succeeded. Display only; an exception discards the rows and nothing else |

### 2.3 Types

| Type | Fields | Notes |
|---|---|---|
| `ReadSource` | `data`, `path`, `offset`, `length`, `base`; `start`, `window()` | `data` is the whole source. `base` is the file offset of `data[0]`. `offset`/`length` are the window the caller wants (`length` `None` = to end). `path` is for messages; never open it |
| `WriteTarget` | `existing`, `path`, `offset`, `length`; `whole_file` | `existing` is the destination's current bytes, `b""` for Save As and **File ▸ New File…**. `offset`/`length` are a slot inside a larger file |
| `ContainerField` | `name`, `value`, `detail` | `detail` is a tooltip: break lines at ~60 characters |

| Helper | Signature | Use |
|---|---|---|
| `plain_read` | `plain_read(source, ctx) -> bytes` | Complete `read` of a container that strips nothing |
| `splice` | `splice(existing, at, data) -> bytes` | `existing` with `data` laid over it at `at`; zero-extends. Returning only the payload into a slot would truncate the file |
| `format_size` | `format_size(count) -> str` | `"512 KiB"`, or exact bytes |

### 2.4 Skeleton

```python
from celpix.core.context import KEY_SOURCE_OFFSET
from celpix.plugins import ContainerField, PluginInfo, splice

MAGIC = b"MYFORMAT"

class MyContainer:
    info = PluginInfo(id="container.my-format", name="My format", magic=((0, MAGIC),))

    def read(self, source, ctx):
        ctx.set(KEY_SOURCE_OFFSET, source.base + len(MAGIC))
        return source.data[len(MAGIC) :]  # honour source.length where it applies

    def write(self, data, dest, ctx):  # optional
        return MAGIC + data if dest.whole_file else splice(dest.existing, dest.offset, data)

    def describe(self, source, ctx):  # optional
        return (ContainerField("Magic", MAGIC.decode(), "Bytes before the payload."),)

def register(registry):
    registry.register(MyContainer())
```

### 2.5 Further example

| `containers/_tiff.py` shows | |
|---|---|
| Payload position found by lookup | Strips located through a directory, joined end to end, written back strip by strip |
| Two byte orders | One `magic` probe each |
| `preserves_offsets=False` | Joined strips do not keep file positions |
| Notices | `warn` for an unsupported compression, `inform` for a joined image |
| Save As | `read` stores the source on a plugin key; `write` uses it when `dest.existing` is empty. **File ▸ New File…** has no source and raises |
| `describe` on bad data | One row naming the error instead of raising |

## 3. Reshape

`reshape/`. A length-preserving reordering of a whole region. Runs after the
container and before decompression.

### 3.1 `PluginInfo`

`PluginInfo(id, name)`, with `stage=Stage.RESHAPE` optional. No other field applies.

### 3.2 Methods

| Method | Required | Signature | Returns | Notes |
|---|---|---|---|---|
| `reshape` | yes | `reshape(data: bytes, ctx) -> bytes` | The reordered region | Runs on load. Receives the entire region, never a window. Output length equals input length |
| `unshape` | no | `unshape(data: bytes, ctx) -> bytes` | The original order | Runs on save. Exact inverse. Absent: read-only |

- While a reshape is active, view addresses count from the start of the reordered buffer.
- Test `unshape(reshape(x)) == x` for odd and even lengths.
- An address-line permutation, a byte substitution or a parts join needs no code: [Preset Reference](Preset-Reference).

### 3.3 Skeleton

```python
from celpix.plugins import PluginInfo

class MyReshape:
    info = PluginInfo(id="reshape.my-swap", name="My swap")

    def reshape(self, data, ctx):
        half = len(data) // 2
        return data[half : half * 2] + data[:half] + data[half * 2 :]

    def unshape(self, data, ctx):  # optional
        return self.reshape(data, ctx)

def register(registry):
    registry.register(MyReshape())
```

## 4. Compression

`compression/`. Unpacks one structure before interpretation; packs it on save.
A compression scheme followed by a reshape needs no code:
[Compress & Reshape](Compress-and-Reshape).

### 4.1 `PluginInfo`

| Field | Type | Default | Meaning |
|---|---|---|---|
| `id` | str | required | e.g. `compression.example-rle` |
| `name` | str | required | Picker label |
| `stage` | `Stage` | `None` | `Stage.COMPRESSION` or omitted |
| `self_delimiting` | bool | `True` | `True`: the stream marks its own end. `False`: the extent comes from outside (a slice length). Scan searches only self-delimiting schemes |
| `alignment` | int | `1` | Hardware load alignment in bytes. Scan steps by it |
| `inputs` | tuple of `InputSpec` | `()` | Data from outside the stream (§4.4) |

### 4.2 Methods

| Method | Required | Signature | Returns | Notes |
|---|---|---|---|---|
| `decompress` | yes | `decompress(data: bytes, ctx) -> bytes` | Unpacked bytes | Runs on load, preview and Scan. Sets `KEY_COMPRESSED_SIZE` and `KEY_DECOMPRESS_COMPLETE` |
| `compress` | no | `compress(data: bytes, ctx) -> bytes` | Packed bytes | Runs on save. Output may differ from the original; `decompress(compress(x)) == x` is the requirement. Absent: read-only |
| `delimits_itself` | no | `delimits_itself(inputs: dict) -> bool` | Whether the stream marks its end under `inputs` | For a scheme whose end marker is an input. Overrides `self_delimiting`, which then answers for unbound inputs |

| `decompress` rule | |
|---|---|
| Not a structure | Raise when no complete structure starts at `data[0]`. Scan counts a hit when the decode succeeds, returns data and reports complete; a lenient decoder gives false hits |
| `KEY_DECOMPRESS_PARTIAL` set | The live preview's window may cut a structure short: return the valid prefix instead of raising. Corrupt data still raises |
| `KEY_COMPRESSED_SIZE` | Bytes consumed. A save-back must fit this size |
| `KEY_DECOMPRESS_COMPLETE` | Whether the structure's end was reached. Always `False` for a scheme with no end marker |
| `compress` and inputs | Inputs are never written back. Raise for data the bound inputs cannot represent |

### 4.3 `PartialDecompression`

Base class supplying both methods. Subclass it and define:

| Method | Signature | Returns |
|---|---|---|
| `_decode` | `_decode(data: bytes, *, partial: bool) -> tuple[bytes, int, bool]` | `(output, consumed, complete)` for one structure at `data[0]` |
| `_encode` | `_encode(data: bytes) -> bytes` | Packed bytes |

`decompress` passes `KEY_DECOMPRESS_PARTIAL` as `partial` and sets both result keys.

Read-only scheme: leave `_encode` undefined and set `compress = None` on the class. The inherited `compress` is callable, so without that the plugin counts as writable and the save raises.

### 4.4 `InputSpec`

Declared in `PluginInfo(inputs=...)`; bound per entry in the Inputs window.
Walkthrough: [Plugin Inputs](Plugin-Inputs).

| Field | Type | Default | Meaning |
|---|---|---|---|
| `key` | str | required | Key in `KEY_INPUTS`. Stored in project files: renaming orphans bindings |
| `label` | str | required | Shown in the Inputs window |
| `kind` | `InputKind` | `REGION` | `REGION`, `INTEGER`, `FLAG`, `CHOICE` |
| `required` | bool | `True` | Must be `False` on a `CHOICE` |
| `tooltip` | str | `""` | Break lines at ~60 characters |
| `unit` | str | `""` | Element name in the UI. Empty = bytes |
| `stride` | int | `1` | `REGION` only: element size; a bound length must be a multiple |
| `minimum`, `maximum` | int | `0`, `0xFFFFFFFF` | `INTEGER` only: a bound value outside is rejected |
| `default` | int \| str \| `None` | `None` | `INTEGER`: delivered when unbound. `FLAG`: `bool(default)`. `CHOICE`: an option key |
| `options` | tuple of `(key, label)` | `()` | `CHOICE` only. Option keys are stored in project files |

| Kind | Delivered as | Unbound |
|---|---|---|
| `REGION` | `bytes` | Required: stage runs pass-through, entry marked, input named. Optional: absent |
| `INTEGER` | `int` | Required: as above. Optional: `default`, or absent when `None` |
| `FLAG` | `bool` | Always present |
| `CHOICE` | `str` (option key) | Always present: `default`, else the first option |

- Read with `ctx.get(KEY_INPUTS) or {}`. A required input is always present when a method runs.
- Set for `decompress`, `compress`, the live preview and Scan.
- A malformed `CHOICE` declaration fails the load with the reason.

### 4.5 Skeleton

```python
from celpix.core.context import (
    KEY_COMPRESSED_SIZE,
    KEY_DECOMPRESS_COMPLETE,
    KEY_DECOMPRESS_PARTIAL,
)
from celpix.plugins import PluginInfo

class MyScheme:
    info = PluginInfo(id="compression.my-scheme", name="My scheme", self_delimiting=True)

    def decompress(self, data, ctx):
        out, consumed, complete = bytearray(), 0, False
        ...  # decode one structure from data[0]
        if not complete and not ctx.get(KEY_DECOMPRESS_PARTIAL):
            raise ValueError("no terminator: not a My scheme structure")
        ctx.set(KEY_COMPRESSED_SIZE, consumed)
        ctx.set(KEY_DECOMPRESS_COMPLETE, complete)
        return bytes(out)

    def compress(self, data, ctx):  # optional
        ...

def register(registry):
    registry.register(MyScheme())
```

### 4.6 Further example

| `compression/_inputs.py` shows | |
|---|---|
| A required `REGION` input | A phrase table stored elsewhere in the file, with `stride` and `unit` |
| An optional `INTEGER` input | `default`, `minimum`, `maximum`: the common case needs no binding |
| `self_delimiting=False` | No end marker; `KEY_DECOMPRESS_COMPLETE` always `False` |
| `compress` refusing | Raises for data the read-only table cannot express |

## 5. Pixel formats

`pixel/`. Bytes to tiles of palette indices.

### 5.1 `FormatInfo`

Shared by all three format folders.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `id` | str | required | e.g. `format.pixel.example-2bpp`. Also the preset id |
| `name` | str | required | Picker label |
| `category` | str | `""` | Replaced in a plugin folder |
| `declares` | mapping | `{}` | Facts the host needs before decoding. Not parameters: values the methods use are class constants |
| `inputs` | tuple of `InputSpec` | `()` | Tilemap formats only (§4.4) |

| Pixel `declares` key | Type | Meaning |
|---|---|---|
| `palette_bpp` | int | Palette row width in bits, where rows are wider than the tiles' depth |

### 5.2 Methods

| Method | Required | Signature | Returns | Notes |
|---|---|---|---|---|
| `decode` | yes | `decode(data: bytes, ctx) -> list[IndexGrid]` | One grid per tile | A whole number of tiles; drop a trailing partial tile |
| `encode` | yes | `encode(tiles: list[IndexGrid], ctx) -> bytes` | Tile bytes | Mask indices to the depth |
| `bytes_per_tile` | yes | `bytes_per_tile() -> int` | Bytes in one tile | Sizes the decode window |
| `tile_size` | yes | `tile_size() -> tuple[int, int]` | `(width, height)` in pixels | |

- Bit depth is derived as `ceil(bytes_per_tile * 8 / (width * height))`. It sizes a palette row and clamps painted indices; it is not checked against `decode`.

### 5.3 Types

- `IndexGrid(width, height, data)` (`celpix.core.index_grid`): one byte per pixel, a palette index, row-major. `data` is exactly `width * height` bytes, whatever the format stores. `.width`, `.height`, `.data` (`bytearray`), `.get(x, y)`, `.set(x, y, v)`.

### 5.4 Skeleton

```python
from celpix.core.index_grid import IndexGrid
from celpix.plugins import FormatInfo

class MyPixels:
    info = FormatInfo(id="format.pixel.my-2bpp", name="My 2bpp")

    def decode(self, data, ctx):
        ...  # one IndexGrid(8, 8, indices) per 16 bytes; 64 indices each

    def encode(self, tiles, ctx):
        ...

    def bytes_per_tile(self):
        return 16

    def tile_size(self):
        return (8, 8)

def register(registry):
    registry.register_format(MyPixels())
```

## 6. Palette formats

`palette/`. Bytes to colors. `FormatInfo` as §5.1; no `declares` keys.

### 6.1 Methods

| Method | Required | Signature | Returns | Notes |
|---|---|---|---|---|
| `decode` | yes | `decode(data: bytes, ctx) -> Palette` | Colors as `0xAARRGGBB` | No alpha field: decode alpha as `0xFF` |
| `encode` | yes | `encode(palette: Palette, ctx) -> bytes` | Color bytes | Quantize a color the format cannot store; do not reject it |
| `bytes_per_entry` | yes | `bytes_per_entry() -> int` | Bytes in one read unit | N entries = N × this |
| `entries_per_unit` | no | `entries_per_unit() -> int` | Entries in one unit | Omitted: `1`. Four 2-bit shades in a byte: `4` |
| `shared_entries` | no | `shared_entries(index: int, count: int) -> tuple[int, ...]` | Entries of a `count`-entry palette showing `index`'s color, owner first | An edit to any is written to the owner. Omitted: `(index,)` |

- Alpha control: celPix round-trips a transparent color through `encode` and `decode`. Returned transparent, the format gets an alpha control; `0xFF`, it does not.

### 6.2 Types

- `Palette(colors)` (`celpix.core.palette`): `.colors` is a list of `0xAARRGGBB` ints.
- `MISSING_COLOR` (`celpix.core.palette`): `0xFFFF00FF`, for a value that names no color.

### 6.3 Skeleton

```python
from celpix.core.palette import Palette
from celpix.plugins import FormatInfo

class MyPalette:
    info = FormatInfo(id="format.palette.my-gray", name="My gray")

    def decode(self, data, ctx):
        return Palette([0xFF000000 | (b * 0x010101) for b in data])

    def encode(self, palette, ctx):
        return bytes((c >> 16) & 0xFF for c in palette.colors)

    def bytes_per_entry(self):
        return 1

def register(registry):
    registry.register_format(MyPalette())
```

### 6.4 Further example

| `palette/_nes-custom.py` shows | |
|---|---|
| Companion file | Reads `Path(__file__).with_suffix(".pal")` beside the plugin, with a built-in fallback. Read at load; <kbd>F5</kbd> after changing it |
| State built once | `__init__` builds the color table; `decode`/`encode` stay stateless |
| Out-of-range value | Decodes as `MISSING_COLOR` |
| Lossy `encode` | Nearest color by RGB distance; a duplicate color is written as its first index |

## 7. Tilemap formats

`tilemap/`. Bytes to a flat list of cells in file order; celPix lays it out as
a grid. `FormatInfo` as §5.1.

### 7.1 `declares`

| Key | Value | Meaning |
|---|---|---|
| `layout` | `"text"` \| `"sprite"` | Cells are character codes, or sprite parts at offsets. Absent: a grid of tile references |
| `subsprite_size` | `"stated"` | Sprite parts carry their own size |
| `palette_row_base` | int | Palette row a cell's row 0 means |
| `indirect`, `source_hint`, `stamp_cells`, `stamp_dense`, `index_addressing`, `column_major`, `cell_row_stride`, `cell_column_stride`, `offered_stamp_cells`, `stamp_stride`, `stamp_order`, `record_shape`, `record_order`, `record_header` | | As for the packed engine: [Preset Reference](Preset-Reference) |
| `record_cells`, `record_width`, `records_per_row`, `record_header` | | Read only when `counts_records()` returns `True` |

The packed engine's own parameters (`fields`, `legend`, `bytes`, `endian`, page
geometry) have no effect as declarations.

### 7.2 Methods

`params` is never passed to a format.

| Method | Required | Signature | Returns | Notes |
|---|---|---|---|---|
| `decode` | yes | `decode(data: bytes, ctx) -> list[Cell]` | Cells in file order | Fill every field the format stores; a field left at its default is written back as that default. Drop a trailing partial cell |
| `encode` | yes | `encode(cells: list[Cell], ctx) -> bytes` | Cell bytes | Mask values to their fields |
| `bytes_per_cell` | yes | `bytes_per_cell() -> int` | Bytes in one cell | Sizes the decode window |
| `cell_tiles` | yes | `cell_tiles() -> tuple[int, int]` | Tiles one cell draws, `(across, down)` | `(2, 2)` for a cell that is a 16x16 metatile |
| `index_limit` | no | `index_limit() -> int \| None` | Highest tile number | Omitted or `None`: cells cannot be pointed at a tile (no Cell control, no Edit Tiles) |
| `index_runs` | no | `index_runs() -> Sequence[range]` | Ascending, disjoint runs of nameable indices | Where not `0 .. index_limit()`. `encode` then raises for a cell outside them |
| `palette_row_limit` | no | `palette_row_limit() -> int \| None` | Highest palette row | Omitted: rows cannot be assigned |
| `has_palette_rows` | no | `has_palette_rows() -> bool` | Whether cells name a row | Omitted: `True`. `False`: the view's palette row control applies |
| `palette_row_granularity` | no | `palette_row_granularity() -> tuple[int, int]` | Cells `(across, down)` sharing one stored row | Omitted: `(1, 1)`. NES nametable: `(2, 2)` |
| `transform_cell` | no | `transform_cell(cell: Cell, op: CellOp) -> Cell \| None` | The cell after `op`, or `None` to refuse | Omitted: every transform refused |
| `cell_fields` | no | `cell_fields() -> dict[str, int]` | `Cell` attribute name → highest value | Builds the per-cell controls. Omitted: assembled from the methods above, never with `priority` or `flags` |
| `has_line_flag` | no | `has_line_flag() -> bool` | Whether `ends_line` is stored | Omitted: `False`. Text formats |
| `has_visibility` | no | `has_visibility() -> bool` | Whether `visible` is stored | Omitted: `False`. `True`: Clear cells clears it |
| `counts_records` | no | `counts_records() -> bool` | Whether the index is a record number of the bound table | Omitted: `False`. `True`: record keys read from `declares` |
| `settle_cells` | no | `settle_cells(cells: list[Cell], inputs: dict = None) -> list[Cell]` | Cells with derived fields re-derived | Called on every edit and before `encode`. Idempotent, pure; same list object back when nothing changed; never a different length or order. `inputs` is passed only when the entry's resolved inputs are non-empty |
| `cell_widths` | no | `cell_widths(cells: list[Cell]) -> list[int] \| None` | Byte width of each cell | Mixed-width text. Omitted or `None`: every cell is `bytes_per_cell()` |
| `line_bytes` | no | `line_bytes() -> int` | Fixed record length per line | Name tables. Omitted: `0` |
| `size_pair` | no | `size_pair() -> tuple[int, int]` | The two part sizes a size bit selects, in tiles | Sprite formats only |
| `frames` | no | `frames(cells: list[Cell], ctx) -> list[Frame]` | The parts grouped into frames | Sprite formats only. Runs as part of the read: a raise fails it |

- An optional method that raises, or answers in the wrong shape, is read as absent and recorded as a warning. `settle_cells` and `cell_widths` are then not called again for that document.

### 7.3 Types

| Type | Defined in | |
|---|---|---|
| `Cell` | `celpix.core.tilemap` | Frozen dataclass: `index` (0), `palette_row` (0), `priority` (0), `flip_h` (False), `flip_v` (False), `visible` (True), `ends_line` (False), `flags` (0). `index` is the file's own number, before any tile-source base. `priority` and `flags` are stored, never rendered; `flags` holds bits celPix has no field for. `.flipped_h()`, `.flipped_v()`; other changes via `dataclasses.replace` |
| `CellOp` | `celpix.core.tilemap` | `FLIP_H`, `FLIP_V`, `ROTATE_CW`, `ROTATE_CCW` |
| `Subsprite` | `celpix.core.sprite` | Frozen dataclass: `x`, `y` (signed pixel offsets), `index`, `palette_row`, `priority`, `flip_h`, `flip_v`, `across`, `down` (tiles, default 1), `column_major`, `group` |
| `Frame` | `celpix.core.sprite` | `tuple[Subsprite, ...]`, front to back: subsprite 0 is on top |

Sprite maps in the app: [Sprites](Sprites).

### 7.4 Skeleton

```python
from celpix.core.tilemap import Cell
from celpix.plugins import FormatInfo

class MyMap:
    info = FormatInfo(id="format.tilemap.my-cell", name="My cell")

    def decode(self, data, ctx):
        ends = range(2, len(data) + 1, 2)  # drops a partial cell
        words = (int.from_bytes(data[end - 2 : end], "little") for end in ends)
        return [Cell(index=w & 0x3FF, flip_h=bool(w & 0x4000)) for w in words]

    def encode(self, cells, ctx):
        words = ((c.index & 0x3FF) | (0x4000 if c.flip_h else 0) for c in cells)
        return b"".join(w.to_bytes(2, "little") for w in words)

    def bytes_per_cell(self):
        return 2

    def cell_tiles(self):
        return (1, 1)

    def index_limit(self):  # optional; without it cells cannot be re-pointed
        return 0x3FF

def register(registry):
    registry.register_format(MyMap())
```

The shipped `tilemap/_example.py` adds `palette_row_limit`, `has_palette_rows`,
`transform_cell` and `cell_fields`, on a 3-byte cell with a split tile number. A tilemap format declares inputs as
`FormatInfo(..., inputs=(InputSpec(...),))` (§4.4); `KEY_INPUTS` is then set
for its methods.

## 8. Context keys

Constants in `celpix.core.context` unless stated.

| Constant | Key | Type | Set by | Read by |
|---|---|---|---|---|
| `KEY_SOURCE_OFFSET` | `source.offset` | int | Container `read` | Host: displayed addresses, slice anchors, the `preserves_offsets` check |
| `KEY_SOURCE_FILES` | `source.files` | tuple of `SourceFile(path, start, length)` | Host, before `read` | A container that needs which file supplied which range of a joined region |
| `KEY_DECOMPRESS_PARTIAL` | `compression.allow-partial` | bool | Host, for the live preview | `decompress` |
| `KEY_COMPRESSED_SIZE` | `compression.compressed-size` | int | `decompress` | Host: save-back slot, Scan |
| `KEY_DECOMPRESS_COMPLETE` | `compression.complete` | bool | `decompress` | Host: Scan, slice length from a decode |
| `KEY_INPUTS` | `plugin.inputs` | dict: `key` → `bytes` \| `int` \| `bool` \| `str` | Host, before each call of a stage that declares inputs | Compression plugins, tilemap formats |
| `KEY_NOTICES` (`celpix.core.notices`) | `notices` | tuple of `Notice` | `warn`, `inform` | Host. Do not set directly |
| (plugin's own) | any other string | any | The plugin | The plugin; listed in the Container Info popup |

## 9. Errors and notices

| Situation | Plugin does | User sees |
|---|---|---|
| Operation cannot proceed | Raise. Any `Exception`; the examples use `ValueError` with a message naming the offending value, offset or input key | Error dialog naming the plugin, stage, direction, exception and the plugin line that raised; traceback under **Show Details**. The operation fails |
| Read succeeds with a caveat | `warn(ctx, summary, detail="", source="")` | Entry tooltip; entry marked in the Files list |
| Fact worth knowing | `inform(ctx, summary, detail="", source="")` | Entry tooltip |
| Notices, then a raise | Both | Notices recorded before the raise are kept |
| Optional method raises | | Method read as absent; warning dialog |
| `describe` raises | | Rows discarded; nothing else |
| Required input unbound or invalid | | Stage runs as pass-through; entry marked; the input to bind is named |
| File fails to load: syntax error, exception at import or in `register`, no `register` | | **Plugin load issues** dialog, at startup and after a refresh. File skipped |
| Approval declined | | Status bar message; listed in the **Plugin load issues** dialog when that opens for another file. **File ▸ Refresh plugins** asks again |
| Registration refused: stage conflicts with folder, required method missing, bad id or name, malformed container signature, format id taken | | **Plugin load issues** dialog. That registration skipped; others in the file kept |
| Inputs on a stage that receives none | | **Plugin load issues** dialog, as a warning. Plugin loads; inputs ignored |

- `summary` is one line that reads on its own. `detail` is a tooltip: a few lines, broken at ~60 characters. `source` is the plugin id (`self.info.id`).
