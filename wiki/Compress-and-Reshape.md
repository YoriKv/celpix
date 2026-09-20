# Compress & Reshape

**Super Mario World**'s level backgrounds, which the game rearranges after it
unpacks them.

A **reshape** reorders a region's bytes without changing how many there are. The
Reshape picker on a slice runs *before* decompression, over the packed bytes. A
game that unpacks a stream and *then* rearranges it needs the two the other way
round, as one scheme: a **Compress & Reshape** plugin.

This page uses the Super Mario World sample project
([opening it](Plugin-Inputs#1-open-a-project-that-has-plugins)).

## 1. The problem

A background is an RLE1 stream. `Layer 2 background — Mountains` is two screens
of 16×27 blocks, side by side.

![The background](images/pair-background.png)

The game addresses the unpacked buffer a screen at a time: all of the first
screen, then all of the second. Read through RLE1 alone, the right screen lands
under the left.

Right-click the entry ▸ **Edit…** and set **Compression** to **RLE1 (SMW, $FF
$FF terminated)**:

![Edit Slice](images/pair-edit-slice-rle1.png)

![RLE1 alone](images/pair-rle1-only.png)

The project has a reshape for the walk, **SMW level screens (16x27) laid
across**. It cannot go in the slice's **Reshape** row: there it would shuffle the
packed stream.

## 2. Make the pair

**File ▸ New Compress & Reshape Plugin…**. It needs a saved project, because the
plugin is written into the project's `plugins/` folder.

Pick the two halves, and name the pair.

![The pair](images/pair-dialog.png)

| | |
| --- | --- |
| **Compression** | RLE1 (SMW, $FF $FF terminated) |
| **Then reshape** | SMW level screens (16x27) laid across |
| **Name** | `SMW background (RLE1 + screens)` |

**Saved as** shows the id and the file it will write. A name already in use is
refused:

![A name that is taken](images/pair-dialog-name-taken.png)

The result is a few lines of data, so it loads with no prompt:

```toml
id = "compression.smw-background-rle1-screens"
name = "SMW background (RLE1 + screens)"
engine_id = "compression.compress-reshape"

[params]
compression = "compression.rle1"
reshape = "reshape.smw-screens-across"
```

![Added to the project](images/pair-created.png)

## 3. Use it

The pair is a compression scheme like any other. **Edit…** the entry again and
pick it, under **Project plugins**.

![In the picker](images/pair-compression-picker.png)

![The background, again](images/pair-using-it.png)

On load it unpacks, then reshapes. On write it runs both backwards: the reshape's
inverse, then the compressor. It finds the end of a stream if its compression
half does, and any [inputs](Plugin-Inputs) that half declares are bound on the
pair.

## View-only pairs

A pair writes only if both halves can run backwards. The dialog says so before it
makes one:

![A half with no way back](images/pair-dialog-view-only.png)

Slices read through such a pair open view-only.
