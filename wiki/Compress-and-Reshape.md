# Compress & Reshape

The **Reshape** setting of a slice runs *before* decompression. Some games unpack
data and *then* rearrange it. A **Compress & Reshape** plugin does the two steps
in that order.

This page uses the Super Mario World sample project. To open it, see
[Plugin Inputs](Plugin-Inputs#1-open-a-project-that-has-plugins).

## 1. The problem

The Mountains Layer 2 background is an RLE1 stream. It has two screens of 16×27
stamps. In the game, the screens are side by side.

![The background](images/pair-background.png)

The unpacked data stores one screen after the other. With RLE1 only, the right
screen shows below the left screen.

1. Right-click ▸ **Edit…**.
2. Set **Compression** to **RLE1 (SMW, $FF $FF terminated)**.

![Edit Slice](images/pair-edit-slice-rle1.png)

![RLE1 alone](images/pair-rle1-only.png)

The reshape **SMW level screens (16x27) laid across** puts the screens side by
side. But in the **Reshape** setting, it runs before decompression, so it would
rearrange the packed bytes.

## 2. Make the pair

1. **File ▸ New Compress & Reshape Plugin…**. The project must be saved
   first. celPix puts the plugin in the `plugins/` folder.
2. Enter these values:

![The pair](images/pair-dialog.png)

| | |
| --- | --- |
| **Compression** | RLE1 (SMW, $FF $FF terminated) |
| **Then reshape** | SMW level screens (16x27) laid across |
| **Name** | `SMW background (RLE1 + screens)` |

Each name must be unique:

![A name that is taken](images/pair-dialog-name-taken.png)

The plugin is a data file. celPix loads it without asking for trust:

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

1. Right-click ▸ **Edit…**.
2. Under **Project plugins**, select the pair.

![In the picker](images/pair-compression-picker.png)

![The background, again](images/pair-using-it.png)

When celPix loads the entry, it unpacks the data and then reshapes it. When it
writes the entry, it does the two steps in reverse. If the compression has
[inputs](Plugin-Inputs), you bind them on the pair.

## View-only pairs

celPix can write a pair only if both steps can be reversed. If one step cannot
be reversed, the dialog shows a warning:

![A half with no way back](images/pair-dialog-view-only.png)
