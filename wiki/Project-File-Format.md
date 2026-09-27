# Project File Format

Specification of the `.celpix` project file. Schema version **5**.

## 1. Encoding

| Property | Value |
|---|---|
| Extension | `.celpix` |
| Container | One JSON document; top-level value is an object |
| Character encoding | UTF-8, no BOM |
| Non-ASCII | Written literally, not as `\uXXXX` escapes |
| Indentation | 2 spaces |
| Line endings | LF, trailing newline |
| Content | References and settings only. No file bytes, no undo history |

Not stored in the file:

| State | Where it lives |
|---|---|
| Tile selection | Not persisted |
| Zoom, grid, pinned-region visibility | App settings |
| Window geometry, panel layout | App settings |
| Recent projects list | App settings |
| Plugins | `plugins/` directory beside the `.celpix`, loaded as a plugin root. Not referenced from the JSON |

## 2. Conventions

### 2.1 Types

| Type | Definition |
|---|---|
| int | JSON integer. `true`/`false` are **not** integers and read as absent |
| bool | JSON `true`/`false` |
| string | JSON string. An empty string reads as absent where a default exists |
| id | string naming a registered plugin or preset, e.g. `compression.lz2`, `preset.pixel.snes-4bpp` |
| path | string; see §2.3 |
| entry ref | int; 0-based position in `entries`; see §2.4 |

### 2.2 Defaults and tolerance

- Unknown keys: ignored.
- Missing optional keys: default value.
- Keys marked *omitted at default* are not written when they hold the default.
- A malformed value reads as its default unless stated otherwise.
- A record that cannot be parsed is skipped; it still occupies its position (§2.4).
- The file as a whole fails to load only when: unreadable, not valid JSON/UTF-8,
  top level not an object, or `entries` present and not an array.

### 2.3 Paths

| Rule | |
|---|---|
| Written form | Relative to the directory holding the `.celpix`, `/` separators |
| Fallback | Absolute when no relative path exists (e.g. another Windows drive) |
| Read | Relative paths resolve against the `.celpix` directory, then normalized |
| Case | If the path does not exist, each missing segment is matched case-insensitively against the directory listing |
| Missing file | Entry is kept and flagged; it is not skipped |

### 2.4 Entry references

Keys that name another entry: `current`, `tile_source.entry_index`,
`pieces[].entry_index`, `inputs.*.*.entry_index`, `palette.entry`.

| Rule | |
|---|---|
| Value | 0-based index into `entries` |
| Counting | Counts **every** record in `entries`, including records that fail to parse |
| `-1` | Written for a reference to an entry that is no longer open |
| Out of range | Treated as no reference (per-key behaviour in the sections below) |
| Reordering `entries` | Changes the meaning of every reference past the moved record |

### 2.5 Plugin and preset ids

| Case | Load behaviour |
|---|---|
| Renamed id | Translated to the current id through the built-in alias table; the next save writes the current id |
| Unknown id, byte stage (`container_id`, `reshape_id`, `compression_id`) | Kept verbatim. Stage runs as pass-through; entry is view-only; user is notified |
| Unknown id, interpret preset (`pixel_preset_id`, `palette_preset_id`, `tilemap_preset_id`) | Replaced by the stage default (§2.6); user is notified; the next save writes the default |

### 2.6 Built-in defaults

| Stage | Default id |
|---|---|
| Container | `container.raw-file` |
| Reshape | `reshape.none` |
| Compression | `compression.none` |
| Pixel preset | `preset.pixel.snes-4bpp` |
| Palette preset | `preset.palette.bgr555` |
| Tilemap preset | `preset.tilemap.snes-bg` |

## 3. Versioning

| Rule | |
|---|---|
| `version` | Top-level int. Missing reads as `1` |
| Writer | Always writes the current version (`5`) |
| Older file | Migrated forward one version at a time before any key is read |
| Newer file | Opened with the tolerance rules of §2.2; the UI warns that saving rewrites it at version 5 and drops unknown keys |
| Plugin id renames | Independent of `version` (§2.5) |

| Migration | Change |
|---|---|
| 1 → 2 | `view.subpalette_row` renamed to `view.palette_row`. If both are present, `palette_row` wins |
| 2 → 3 | None. Adds `inputs` |
| 3 → 4 | None. Adds `palette_mode: "entry"` with `palette.entry`; `offset` palettes on composites; new plugin input keys |
| 4 → 5 | None. Adds `session`/`view` on `palette` entries, `parent` on slices and bookmarks, `current` naming a `palette` entry |

## 4. Top level

| Key | Type | Required | Default | Written | Meaning |
|---|---|---|---|---|---|
| `version` | int | yes | `1` | always | Schema version (§3) |
| `current` | entry ref \| `null` | no | `null` | always | The entry shown. Reads as `null` if out of range or naming a `bookmark` |
| `entries` | array of Entry | no | `[]` | always | The entry list, in display order (§5.1) |
| `hidden_pixel_presets` | array of id | no | `[]` | omitted when empty | Pixel presets hidden from the codec dropdown. Sorted. Non-string items ignored |
| `pixel_aspect` | `[int, int]` | no | unset | omitted when unset | Pixel shape `[width, height]`, both > 0. Unset is distinct from `[1, 1]`: unset lets a container's hint apply |

Key order as written: `version`, `current`, `entries`, `hidden_pixel_presets`, `pixel_aspect`.

## 5. Entry

### 5.1 Order

- `entries` order is the display order and is restored exactly. No sorting is applied on load.
- A `slice` or `bookmark` must follow the entry its `path` names. One written before its parent loads as a top-level row.
- Hierarchy depth is two: `file`/`palette` → `slice`/`bookmark`. A slice is never a parent.

### 5.2 Kinds

| `kind` | Is | `path` names | Can be `current` |
|---|---|---|---|
| `file` | A whole file (or multi-file region) | The file | yes |
| `slice` | A byte range of a parent | The **parent's** file | yes |
| `bookmark` | A saved position in a parent | The **parent's** file | no |
| `palette` | A registered external palette file | The palette file | yes |
| `composite` | A view assembled from other entries | — (no `path`) | yes |

Unknown or missing `kind` reads as `file`.

### 5.3 Key matrix

`●` always written · `○` optional · `–` not used for this kind

| Key | file | slice | bookmark | palette | composite |
|---|---|---|---|---|---|
| `kind` | ● | ● | ● | ● | ● |
| `name` | ○ | ○ | ○ | ○ | ○ |
| `path` | ● | ● | ● | ● | – |
| `extra_paths` | ○ | ○ | ○ | – | – |
| `container_id` | ○ | – | – | ○ | – |
| `reshape_id` | ○ | ○ | – | – | – |
| `parent` | – | ○ | ○ | – | – |
| `slice_offset` | – | ● | – | – | – |
| `slice_length` | – | ● | – | – | – |
| `compression_id` | – | ● | – | – | – |
| `slot_fill` | – | ○ | – | – | – |
| `offset` | – | – | ● | – | – |
| `palette_preset_id` | – | – | – | ● | – |
| `pieces` | – | – | – | – | ● |
| `content_kind` | ○ | ○ | – | – | – |
| `tilemap_preset_id` | ○ | ○ | – | – | – |
| `tile_source` | ○ | ○ | – | – | – |
| `sprite_size_pair` | ○ | ○ | – | – | – |
| `palette_row_base` | ○ | ○ | – | – | ○ |
| `font` | ○ | ○ | – | – | ○ |
| `inputs` | ○ | ○ | – | – | – |
| `session` | ● | ● | ● | ○ | ● |
| `view` | ○ | ○ | ○ | ○ | ○ |
| `palette` | ○ | ○ | ○ | – | ○ |

Required keys: an entry without them is skipped (`path`) or opens on defaults.

| `kind` | Required |
|---|---|
| `file` | `path` |
| `slice` | `path`, `slice_offset` |
| `bookmark` | `path`, `offset` |
| `palette` | `path` |
| `composite` | `pieces` |

### 5.4 Common keys

| Key | Type | Default | Written | Meaning |
|---|---|---|---|---|
| `kind` | string | `"file"` | always | §5.2 |
| `name` | string | basename of `path`; `"composite"` for a composite | always | Display name. Free text |
| `path` | path | — | always (not on composite) | Non-empty. Missing, empty or non-string: entry skipped |
| `extra_paths` | array of path | `[]` | omitted when empty | Further files joined after `path`, in join order, forming one region. On a slice/bookmark: the parent's list. Offsets are into the joined bytes |
| `container_id` | id | `container.raw-file` | omitted at default | Container the bytes are read and written through |
| `reshape_id` | id | `reshape.none` | omitted at default | Byte reordering of the region |
| `content_kind` | string | `"pixels"` | omitted at default | `"pixels"` \| `"tilemap"` \| `"palette"`. Unknown reads as `"pixels"` |
| `palette_row_base` | int | unset | omitted when unset; **written at 0** | Palette row the entry's row 0 counts from. Unset = the format's value. Signed |
| `font` | object | — | omitted when empty | §5.9 |
| `inputs` | object | `{}` | omitted when empty | §5.10 |
| `session` | object | defaults of §5.11 | see §5.11 | §5.11 |
| `view` | object | — | when the entry has view state | §5.12 |
| `palette` | object | — | when the entry has a palette source | §5.13 |

### 5.5 `slice`

| Key | Type | Default | Written | Meaning |
|---|---|---|---|---|
| `slice_offset` | int | `0` | always | Start, in bytes, absolute from byte 0 of the parent region. Not relative to any container header |
| `slice_length` | int \| `null` | `null` | always | Length in bytes. `null` = determined by decompression on first load. Always an int when `reshape_id` is set |
| `compression_id` | id | `compression.none` | always | Codec the slice is decompressed and recompressed with |
| `slot_fill` | string | `"ff"` | omitted at default | Padding after a recompressed stream shorter than its slot: `"ff"` (pad `$FF`), `"zero"` (pad `$00`), `"keep"` (leave old bytes). Unknown reads as `"ff"` |
| `parent` | string | `"file"` | omitted when `"file"` | Kind of row `path` names: `"file"` \| `"palette"`. Other values read as `"file"` |

### 5.6 `bookmark`

| Key | Type | Default | Written | Meaning |
|---|---|---|---|---|
| `offset` | int | `0` | always | Position, in bytes, absolute from byte 0 of the parent region |
| `parent` | string | `"file"` | omitted when `"file"` | As for `slice` |

- `session`, `view`, `palette`: snapshot of the parent's settings, applied to the parent when the bookmark is opened.
- `view.offset` and `view.byte_nudge` are written as `0`.

### 5.7 `palette`

| Key | Type | Default | Written | Meaning |
|---|---|---|---|---|
| `palette_preset_id` | id | `preset.palette.bgr555` | always | Color codec the file is decoded with |
| `container_id` | id | `container.raw-file` | omitted at default | Which bytes of the file are colors |

- `session` and `view`: present only once the palette has been opened as a swatch sheet. Absent `session` stays absent on re-save.
- Never carries a `palette` sub-object.
- A `palette` entry is always top-level, even when its `path` equals a `file` entry's.

### 5.8 `composite`

| Key | Type | Default | Written | Meaning |
|---|---|---|---|---|
| `pieces` | array of Piece | `[]` | always | Runs assembled end to end, in order |

Piece:

| Key | Type | Default | Written | Meaning |
|---|---|---|---|---|
| `entry_index` | entry ref | absent | absent on a pad | Source entry |
| `offset` | int ≥ 0 | `0` | omitted at 0 | Start of the run, bytes into the source's resolved data |
| `length` | int ≥ 0 | `0` | see shapes | Length of the run in bytes |
| `measured` | int ≥ 0 | `0` | omitted at 0 | Bytes the run produced at last assembly. Cache; used as the run's size when the source cannot be read |

Piece shapes:

| Keys present | Shape |
|---|---|
| `length`, no `entry_index` | Pad: `length` blank bytes |
| `entry_index` (+ `measured`) | Whole source entry |
| `entry_index`, `length` (+ `offset`, `measured`) | Byte range of the source |

- Resolved data = the source's decompressed stream when compressed, its own bytes otherwise.
- Valid source: an entry that can be shown, has `content_kind` `"pixels"`, is not a composite, and is not this entry.
- Invalid or out-of-range source: the piece becomes a pad of the recorded size.
- Negative `offset`, `length`, `measured` read as `0`.
- A piece that is not an object reads as a zero-length pad.

### 5.9 `font`

Written on a **pixels** entry: the character each tile draws.

| Key | Type | Default | Written | Meaning |
|---|---|---|---|---|
| `use` | bool | `false` | omitted when `false` | Use as Font. When `false` the table is not read |
| `base` | int | `0` | omitted at 0 | Code drawn by tile 0. Shifts `chars` only |
| `prepend` | int ≥ 0 | `0` | omitted at 0 | Extra editor rows before tile 0. Negative reads as 0 |
| `append` | int ≥ 0 | `0` | omitted at 0 | Extra editor rows after the last tile. Negative reads as 0 |
| `chars` | string | `""` | omitted when empty | One code point per tile, in tile order: tile *i* draws code `base + i`. `U+0000` = tile draws no character. Trailing `U+0000` trimmed. Non-string reads as `""` |
| `codes` | array of Code | `[]` | omitted when empty | Absolute codes. Override `chars` where they collide. Not shifted by `base` |

Code — character form:

| Key | Type | Meaning |
|---|---|---|
| `code` | int | Absolute code |
| `text` | string | Characters drawn. One character = role `text`; several = role `dict` |

Code — command form:

| Key | Type | Required | Meaning |
|---|---|---|---|
| `code` | int | yes | Absolute code |
| `name` | string | yes | Command name, as typed inside `[...]`. Whitespace and brackets become `-` |
| `role` | string | yes | `"break"` (line end) \| `"control"` (any other command) |
| `description` | string | no | Tooltip text. Omitted when empty |
| `params` | int ≥ 0 | no | Operand cells consumed after the code. Omitted at 0 |

- Records with a non-integer `code` or empty `text`/`name` are skipped.
- Missing `role` reads as `"text"`; unknown `role` reads as `"text"`.
- Legacy (no `font` key): `alphabet_preset_id` `"alphabet.ascii-upper"` or `"alphabet.ascii"` plus `alphabet_base` are read as a font. Never written.

### 5.10 `inputs`

Values bound to inputs a plugin declares (data outside the entry's bytes).

```
"inputs": { <plugin id>: { <input key>: <binding>, ... }, ... }
```

- Both levels sorted by key when written.
- Plugin ids go through the alias table (§2.5).
- On a `file`: bindings for each compression preview codec. On a `slice` or tilemap: bindings for the plugins it reads through.
- On save, bindings for plugins the entry no longer uses and keys the plugin no longer declares are removed.

Binding shapes (determined by type and keys present):

| Shape | JSON | Constraints |
|---|---|---|
| Flag | `true` \| `false` | — |
| Literal integer | int | — |
| Region | `{"offset": int, "length": int}` | `offset` ≥ 0, `length` ≥ 0 |
| Integer from bytes | `{"offset": int, "width": int, "endian": "big" \| "little"}` | `offset` ≥ 0, `width` 1–8. `endian` other than `"little"` reads as big. Presence of `width` selects this shape |

- Region and integer-from-bytes: `offset` is absolute from byte 0 of the entry's own file.
- Optional `entry_index` (entry ref) on either: offset is into that entry's resolved data instead.
- `entry_index` naming nothing: the binding is removed on load.
- Malformed bindings are skipped.

### 5.11 `session`

| Key | Type | Default | Written | Meaning |
|---|---|---|---|---|
| `pixel_preset_id` | id | `preset.pixel.snes-4bpp` | always | Pixel format preset |
| `palette_preset_id` | id | `preset.palette.bgr555` | always | Palette color format preset |
| `palette_mode` | string | `"default"` | always | `"default"` \| `"file"` \| `"offset"` \| `"entry"` \| `"emulator"` \| `"custom"`. Unknown reads as `"default"`. Selects the `palette` shape (§5.13) |
| `compression_id` | id | `compression.none` | always | Compression preview selection. On a slice this is not the slice's codec |
| `palette_view_preset_id` | id | `preset.palette.bgr555` | omitted at default | Color format used when the pixel preset is the palette-swatch view |

Written on every entry except a `palette` entry never opened as a sheet.

### 5.12 `view`

| Key | Type | Default | Written | Meaning |
|---|---|---|---|---|
| `columns` | int ≥ 1 | `16` | always | Tiles across |
| `rows` | int ≥ 1 | `16` | always | Tiles down |
| `palette_row` | int ≥ 0 | `0` | always | Palette row drawn through |
| `offset` | int ≥ 0 | `0` | always | Top-left tile index |
| `byte_nudge` | int ≥ 0 | `0` | always | Sub-tile byte shift of the grid |
| `block_columns` | int ≥ 1 | `1` | always | Tiles per block, horizontally |
| `block_rows` | int ≥ 1 | `1` | always | Tiles per block, vertically |
| `block_order` | string | `"row"` | always | `"row"` \| `"column"` \| `"row-interleave"`. Unknown reads as `"row"` |
| `two_dimensional` | bool | `false` | always | Read the bytes as a bitmap rather than tiles |
| `bitmap_width` | int ≥ 0 | `0` | always | Bitmap width in pixels. `0` = the codec's tile width |
| `pages_across` | int ≥ 0 | `0` | omitted at 0 | Pages of a paged tilemap laid side by side. `0` = default arrangement. Ignored where the format states its own |
| `show_all_frames` | bool | `false` | only when `true` | Sprite map: show every frame slot |
| `transparent_zero` | bool | `false` | only when `true` | Tilemap: draw color index 0 as transparent |
| `tile_rearrangement` | array of `[virtual, actual]` | `[]` | omitted when empty | Tile permutation. Non-negative ints; identity pairs dropped |
| `tile_orientations` | array of `[tile, flags]` | `[]` | omitted when empty | Per-tile orientation. `flags` 1–7: bit 0 mirror H, bit 1 mirror V, bit 2 transpose (applied after the mirrors) |
| `show_rearranged` | bool | `true` | when a rearrangement or orientation exists | Apply the rearrangement |
| `palette_regions` | array of `[start, length, row]` | `[]` | omitted when empty | Pinned palette rows. `start`/`length` in pixels of the entry's picture; `length` > 0. Written sorted, disjoint, coalesced |

- Values are clamped against the file size on load.
- `tile_rearrangement` must be a permutation: no index repeated on either side, and the set of virtual indices equals the set of actual indices. Otherwise both rearrangement keys are dropped.
- `tile_orientations`: a tile listed twice or `flags` outside 1–7 drops both rearrangement keys.
- `palette_regions`: malformed triples are skipped; overlaps resolve earlier-wins.
- Legacy, read but never written: `tile_map` (= `tile_rearrangement`). Ignored: `zoom`.

### 5.13 `palette`

Shape selected by `session.palette_mode`:

| `palette_mode` | `palette` object | Meaning |
|---|---|---|
| `default` | absent | Built-in fallback palette |
| `custom` | `{"colors": [string, ...]}` | Colors stored in the project. Each `"#AARRGGBB"`: 32-bit hex, uppercase when written. `#` optional on read; fewer than 8 digits zero-fill from the top (alpha `00`) |
| `file` | `{"path": path, "offset": int}` | External palette file; `offset` in bytes |
| `offset` | `{"offset": int}` | The entry's own bytes at `offset`. Slice: into its parent. Composite: into the file of its first piece that names an entry |
| `entry` | `{"entry": entry ref, "offset": int}` | Another entry's resolved data at `offset` |
| `emulator` | `{"path": path, "offset": int}` | Emulator save state. `offset` is written but ignored: the palette location and console codec are re-detected on load |

- Color codec: `session.palette_preset_id`, except `emulator`, where the detected console decides.
- Read precedence when keys are mixed: `colors` → `path` → `entry` → `offset`.
- Any unparseable color in `colors`: the whole object is ignored (default palette).
- `entry` mode, valid source: an entry that can be shown, has `content_kind` `"pixels"`, is not a `palette` entry, and is not this entry. Otherwise the default palette is used.
- Missing `file`/`emulator` file: default palette is used; the reference is kept and re-written on save.

### 5.14 Tilemap keys

Read only when `content_kind` is `"tilemap"`.

| Key | Type | Default | Written | Meaning |
|---|---|---|---|---|
| `tilemap_preset_id` | id | `preset.tilemap.snes-bg` | when set | Cell format preset |
| `tile_source` | object | unbound | when bound | Entry supplying the tiles |
| `sprite_size_pair` | `[int, int]` | unset | omitted when unset | Sprite map: the two subsprite sizes, in tiles, selected by the size bit. Both > 0; otherwise unset |

`tile_source`:

| Key | Type | Default | Written | Meaning |
|---|---|---|---|---|
| `mode` | string | — | always | `"entry"`. `"none"` or unknown = unbound |
| `entry_index` | entry ref | `-1` | always | Tile source entry |
| `base_index` | int | `0` | omitted at 0 | Cell N draws source tile `base_index + N`. Signed |

- Valid source: a `pixels` entry, or a `tilemap` entry whose own chain of `tile_source` bindings does not loop back. Never a `bookmark`, never itself.
- Unresolvable `entry_index`: the tilemap opens unbound.

## 6. Load behaviour

| Condition | Result |
|---|---|
| Record not an object, or unusable `path` | Record skipped; still counted for entry refs |
| Referenced data file missing | Entry listed and flagged; document UI disabled until relocated |
| Any file of a multi-file region missing | Entry unloadable until relocated |
| External palette file missing | Default palette; reference kept |
| Unknown id | §2.5 |
| `current` names a `bookmark` | `current` = `null` |
| Documents | Not read from disk until the entry is first shown |

## 7. Clipboard form

Copying entries places the same Entry records on the clipboard.

| Property | Value |
|---|---|
| MIME type (entries) | `application/x-celpix-entries` |
| Envelope | `{"version": 1, "session": string, "entries": [Entry, ...]}` |
| Envelope for Copy Inputs | `{"version": 1, "session": string, "inputs": {…}}` (§5.10 shape) |
| `version` | Clipboard version, independent of the project version. Any other value: nothing to paste |
| `session` | Token identifying the editor process that copied |
| Paths | Absolute, `/` separators |
| Extra key per Entry | `source_index`: the record's position in the list it was copied from |
| Entry refs | Positions in the source list. Resolved against other records of the same payload via `source_index`; in the same process, against the still-open entry; otherwise dropped |

## 8. Example

```json
{
  "version": 5,
  "current": 1,
  "entries": [
    {
      "kind": "file",
      "name": "game.sfc",
      "path": "roms/game.sfc",
      "container_id": "container.copier-header",
      "session": {
        "pixel_preset_id": "preset.pixel.snes-4bpp",
        "palette_preset_id": "preset.palette.bgr555",
        "palette_mode": "file",
        "compression_id": "compression.none"
      },
      "view": {
        "columns": 16,
        "rows": 16,
        "palette_row": 8,
        "offset": 4096,
        "byte_nudge": 0,
        "block_columns": 1,
        "block_rows": 1,
        "block_order": "row",
        "two_dimensional": false,
        "bitmap_width": 0
      },
      "palette": {
        "path": "roms/game.pal",
        "offset": 0
      }
    },
    {
      "kind": "slice",
      "name": "Title tiles",
      "path": "roms/game.sfc",
      "slice_offset": 1048576,
      "slice_length": 24576,
      "compression_id": "compression.lz2",
      "font": {
        "use": true,
        "base": 32,
        "chars": "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
        "codes": [
          { "code": 26, "text": "th" },
          { "code": 254, "name": "line-break", "role": "break" }
        ]
      },
      "session": {
        "pixel_preset_id": "preset.pixel.snes-4bpp",
        "palette_preset_id": "preset.palette.bgr555",
        "palette_mode": "custom",
        "compression_id": "compression.none"
      },
      "view": {
        "columns": 16,
        "rows": 8,
        "palette_row": 0,
        "offset": 0,
        "byte_nudge": 0,
        "block_columns": 2,
        "block_rows": 2,
        "block_order": "row",
        "two_dimensional": false,
        "bitmap_width": 0
      },
      "palette": {
        "colors": ["#FF000000", "#FFFFFFFF", "#FFB040A0"]
      }
    },
    {
      "kind": "slice",
      "name": "Title map",
      "path": "roms/game.sfc",
      "slice_offset": 1073152,
      "slice_length": 2048,
      "compression_id": "compression.none",
      "content_kind": "tilemap",
      "tilemap_preset_id": "preset.tilemap.snes-bg",
      "tile_source": { "mode": "entry", "entry_index": 1, "base_index": -256 },
      "session": {
        "pixel_preset_id": "preset.pixel.snes-4bpp",
        "palette_preset_id": "preset.palette.bgr555",
        "palette_mode": "entry",
        "compression_id": "compression.none"
      },
      "view": {
        "columns": 32,
        "rows": 32,
        "palette_row": 0,
        "offset": 0,
        "byte_nudge": 0,
        "block_columns": 1,
        "block_rows": 1,
        "block_order": "row",
        "two_dimensional": false,
        "bitmap_width": 0,
        "transparent_zero": true
      },
      "palette": { "entry": 5, "offset": 0 }
    },
    {
      "kind": "bookmark",
      "name": "$91:8000",
      "path": "roms/game.sfc",
      "offset": 1146880,
      "session": {
        "pixel_preset_id": "preset.pixel.snes-4bpp",
        "palette_preset_id": "preset.palette.bgr555",
        "palette_mode": "offset",
        "compression_id": "compression.none"
      },
      "view": {
        "columns": 16,
        "rows": 16,
        "palette_row": 3,
        "offset": 0,
        "byte_nudge": 0,
        "block_columns": 1,
        "block_rows": 1,
        "block_order": "row",
        "two_dimensional": false,
        "bitmap_width": 0
      },
      "palette": { "offset": 231234 }
    },
    {
      "kind": "palette",
      "name": "game.pal",
      "path": "roms/game.pal",
      "palette_preset_id": "preset.palette.bgr555"
    },
    {
      "kind": "composite",
      "name": "CGRAM",
      "pieces": [
        { "entry_index": 0, "offset": 1200128, "length": 256 },
        { "length": 256 }
      ],
      "session": {
        "pixel_preset_id": "preset.pixel.snes-4bpp",
        "palette_preset_id": "preset.palette.bgr555",
        "palette_mode": "default",
        "compression_id": "compression.none"
      }
    }
  ],
  "pixel_aspect": [8, 7]
}
```
