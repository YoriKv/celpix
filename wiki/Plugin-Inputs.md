# Plugin Inputs

Some formats need data outside the slice. A plugin **declares** inputs; the
entry **binds** each one.

Uses the Super Mario World sample project's plugins.

## 1. Open a project that has plugins

A `plugins/` folder beside a `.celpix` loads with it. **File ▸ Open Project…**
(Ctrl+O).

`.py` plugins are code: celPix asks before running each one, and again if it
changes. `.toml` presets never ask.

![Loading a code plugin](images/inputs-trust-prompt.png)

## 2. A region: the other half of a map

Each overworld cell's attributes (flips, priority, palette) are in a second RLE2
stream. The **SMW overworld Layer 2** plugin unpacks the tiles and takes the
attribute stream as an input.

![The overworld, in colour](images/inputs-overworld.png)

**File ▸ Inputs…** (or right-click, with **Copy Inputs** / **Paste Inputs**):

![Inputs](images/inputs-overworld-window.png)

**Attribute stream**: `$164D` bytes at `$2402B`. **Use selection** takes the
canvas selection; **Go to** jumps there; `✓` means it resolves. Click **Apply**
to commit.

### Binding it yourself

Select the ROM, **File ▸ New Slice…**:

| | |
| --- | --- |
| **Content** | Tilemap |
| **Offset** | `$22533` |
| **Length** | `$1AF8` |
| **Compression** | SMW overworld Layer 2 (RLE2 tiles + RLE2 attributes) |

![A codec that needs something](images/inputs-new-slice-unbound.png)

Open the inputs button, enter `$2402B` and `$164D`, **Apply**, **Close**.

![Binding the attribute stream](images/inputs-new-slice-with-window.png)

![Bound](images/inputs-new-slice-bound.png)

Set **Tilemap** to **SMW overworld Layer 2 (16-bit, 32x32 pages, 4 per map)**,
**Tiles** to the overworld main map BG VRAM window, and double-click the main
map palette in **Files**. **Cols** 64 shows each map's four pages as a square.

![The new slice](images/inputs-new-slice-result.png)

The binding is stored on the ROM, so later slices with this codec inherit it.

### Left unbound

The entry opens with raw bytes and can't be written. **Files** marks it `!`;
the tooltip says what to bind.

![Unbound](images/inputs-unbound.png)

![The badge](images/inputs-badge-unbound.png)

### What an edit cannot do

Inputs are read-only. Flipping a cell (**Flip H**) changes the attribute stream,
so **File ▸ Write** (Ctrl+W) refuses:

![Refused](images/inputs-refusal.png)

## 3. A flag: what the game's code knows

Some 3bpp sheets are uploaded with the fourth plane set, shifting them to colours
9–15. Only the game code knows which, so the sheet plugin takes flags. `GFX1E`
sets one.

![Two flags](images/inputs-flags-window.png)

| Ticked | Unticked |
|:-:|:-:|
| [![Colours 9–15](images/inputs-flag-on.png)](images/inputs-flag-on.png) | [![Colours 1–7](images/inputs-flag-off.png)](images/inputs-flag-off.png) |

`GFX08` is uploaded both ways, so it has two entries.

## 4. A number: one layer of two

A credits cast screen writes two layers from one stripe list. The stripe plugin
takes a **VRAM page**, so two entries over the same bytes show each layer.

![VRAM page](images/inputs-integer-window.png)

| `$5000`: the names | `$2000`: the mask |
|:-:|:-:|
| [![The names](images/inputs-integer-names.png)](images/inputs-integer-names.png) | [![The mask](images/inputs-integer-mask.png)](images/inputs-integer-mask.png) |

A number is **Literal** or **From bytes** (read from elsewhere in the file).

## Declaring one

```python
info = PluginInfo(
    id="compression.smw-ow-layer2",
    ...
    inputs=(InputSpec("attributes", "Attribute stream", InputKind.REGION),),
)

def decompress(self, data, ctx):
    attributes = ctx.get(KEY_INPUTS)["attributes"]
```

Example: `compression/_inputs.py` (**File ▸ Open plugins folder…**).
