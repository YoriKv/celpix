# Plugin Inputs

The overworld of **Super Mario World** in its own colours, and two more formats
that keep part of themselves somewhere else.

A plugin is handed one run of bytes: the slice. Some formats need more than
that — the other half of a map, a fact only the game's code knows. Such a plugin
**declares** what it needs, and the entry **binds** each thing to where it is.

This page uses the plugins that come with the Super Mario World sample project.

## 1. Open a project that has plugins

A `plugins/` folder beside a `.celpix` is loaded with it. **File ▸ Open
Project…** (Ctrl+O).

A `.py` plugin is code, and celPix asks before it runs one — once per file, and
again if the file changes. A `.toml` preset is data and never asks.

![Loading a code plugin](images/inputs-trust-prompt.png)

## 2. A region: the other half of a map

[Getting Started](Getting-Started#7-carve-the-map) reads the overworld as one
byte per cell, in one palette row. The cartridge keeps each cell's other byte —
flips, priority, palette — in a second RLE2 stream after the first. No slice
covers both: RLE2 has no end marker, so they are two structures.

The project's **SMW overworld Layer 2** compression plugin unpacks the first
stream, and declares the second as an input.

![The overworld, in colour](images/inputs-overworld.png)

**File ▸ Inputs…** shows what the entry binds. It is also on the entry's
right-click menu, with **Copy Inputs** and **Paste Inputs**.

![Inputs](images/inputs-overworld-window.png)

**Attribute stream** is `$164D` bytes of **This file** at `$2402B`. **Use
selection** takes the range selected on the canvas; **Go to** jumps there. `✓`
means it resolves. Nothing changes until **Apply**.

### Binding it yourself

Select the ROM. **File ▸ New Slice…**:

| | |
| --- | --- |
| **Content** | Tilemap |
| **Offset** | `$22533` |
| **Length** | `$1AF8` |
| **Compression** | SMW overworld Layer 2 (RLE2 tiles + RLE2 attributes) |

A codec with inputs adds an **Inputs** row, and a button beside **Compression**
that opens the window.

![A codec that needs something](images/inputs-new-slice-unbound.png)

Type `$2402B` and `$164D`, **Apply**, **Close**.

![Binding the attribute stream](images/inputs-new-slice-with-window.png)

![Bound](images/inputs-new-slice-bound.png)

Then, as for any map: **Tilemap** to **SMW overworld Layer 2 (16-bit, 32x32
pages, 4 per map)**, **Tiles** to `BG VRAM window — overworld main map`. A new
slice opens on its file's palette; double-click `overworld palette — main map`
in **Files** to use that one.

The map is eight pages of 32×32, and nothing in it says how they sit. **Cols**
moves in whole pages here: set it to 64, two across, and the four pages of each
map make a square.

![The new slice](images/inputs-new-slice-result.png)

The binding is made on the ROM and copied to the slice, so the next slice under
this codec already has it.

### Left unbound

An input nothing is bound to does not stop the entry opening. The plugin is set
aside, the packed bytes are read as they are, and the entry cannot be written.
The row in **Files** is marked `!`, and its tooltip says what to bind.

![Unbound](images/inputs-unbound.png)

On a file, the same mark sits beside the **Compression** picker.

![The badge](images/inputs-badge-unbound.png)

### What an edit cannot do

An input is read, never written. Here a tile number is the entry's own byte, and
a flip is the other stream's. Tick **Flip H** on a cell and **File ▸ Write**
(Ctrl+W):

![Refused](images/inputs-refusal.png)

Nothing is written. Undo the flip.

## 3. A flag: what the game's code knows

These tile sheets are stored 3bpp and drawn 4bpp, and for a few of them the
game's uploader also sets the fourth bit plane, which moves the art into colours
9–15 of its row. Nothing in the file says which. The sheet plugin declares two
flags, and `GFX1E` ticks one.

![Two flags](images/inputs-flags-window.png)

| Ticked | Unticked |
|:-:|:-:|
| [![Colours 9–15](images/inputs-flag-on.png)](images/inputs-flag-on.png) | [![Colours 1–7](images/inputs-flag-off.png)](images/inputs-flag-off.png) |

`GFX08` is uploaded both ways in one playthrough, so it is two entries over the
same bytes, one ticked.

## 4. A number: one layer of two

A credits cast screen is one list of patches to video memory that writes two
layers: the enemy names, and a mask over the level behind them. Each layer draws
from its own tiles, so each wants its own entry. The stripe plugin declares
**VRAM page**, and the two entries carve the same bytes with different answers.

![VRAM page](images/inputs-integer-window.png)

| `$5000`: the names | `$2000`: the mask |
|:-:|:-:|
| [![The names](images/inputs-integer-names.png)](images/inputs-integer-names.png) | [![The mask](images/inputs-integer-mask.png)](images/inputs-integer-mask.png) |

A number can be **Literal**, or read **From bytes** — a size kept in a table
elsewhere in the file, which then follows the table when it is edited.

## Declaring one

In the plugin, an input is a line of its `PluginInfo`, and the values arrive on
the context:

```python
info = PluginInfo(
    id="compression.smw-ow-layer2",
    ...
    inputs=(InputSpec("attributes", "Attribute stream", InputKind.REGION),),
)

def decompress(self, data, ctx):
    attributes = ctx.get(KEY_INPUTS)["attributes"]
```

The shipped example is `compression/_inputs.py` in the plugins folder (**File ▸
Open plugins folder…**).
