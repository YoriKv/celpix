# Text and Fonts

A string is a tilemap over a font sheet. An **alphabet** on the font maps tiles
to characters, so every string using that font reads as text.

Start from a saved project with the ROM open ([Getting Started](Getting-Started)
step 1).

## 1. Carve the font

Set **Pixel** to **SNES 2bpp (8x8)**, go to `$5CB7B`, set **Compression** to
**LZ2 (SMW, Yoshi's Island)**.

![The font, unpacked](images/text-01-font-preview.png)

**File ▸ New Slice from View**, name it `GFX2A font`.

![The slice, filled in](images/text-02-font-slice-dialog.png)

**Rows** 8, **Zoom** 3, palette from a save state
([Getting Started](Getting-Started#3-take-a-palette-from-a-save-state)),
**Palette Row** 6.

![The font sheet](images/text-02-font-slice.png)

## 2. Spell it

Tick **Use as Font** on the toolbar to open **Font Alphabet** (also **View ▸
Font Alphabet…**).

![The alphabet, empty](images/text-03-alphabet-empty.png)

Click a start tile and paste (Ctrl+V); characters fill one per tile.

| Click tile | Paste |
| --- | --- |
| `$00` | `ABCDEFGHIJKLMNOPQRSTUVWXYZ!.-,? ` (ending in the space) |
| `$40` | `abcdefghijklmnopqrstuvwxyz#()'` |
| `$64` | `12345670` |

![The alphabet, filled](images/text-03-alphabet-filled.png)

**Characters** captions each spelled tile when zoomed in.

![Captions](images/text-03-alphabet-captions.png)

For one tile: click, Enter, type, Enter. **Fill with…** offers `A-Z 0-9, from 0`
and `ASCII, from $20`; **Base code** shifts the start.

## 3. Carve the text

Select the ROM, **File ▸ New Slice…**:

| | |
| --- | --- |
| **Content** | Tilemap |
| **Name** | `Message boxes` |
| **Offset** | `$2A5D9` |
| **Length** | `$B26` |
| **Compression** | None (uncompressed) |

![The text's slice](images/text-04-text-slice-dialog.png)

The top bit of a line's last character ends the line. Set **Tilemap** to **[F]
Text run (8-bit, high bit ends a line)** and **Cols** to 18.

**[F]** formats open the **Text** window. Unbound, cells read as hex.

![No alphabet yet](images/text-04-text-unbound.png)

Set **Tiles** to `GFX2A font`.

![The messages](images/text-04-text-bound.png)

Amber marks line ends on the canvas. Short lines misalign the grid; the Text
window is the reliable view.

> Binding to a sheet without **Use as Font** offers to tick it.

Unspelled codes show as hex, e.g. `[$60][$61]` `[$62][$63]` (Yoshi's paw print).
Selecting one in the Text window selects its cell.

![Codes with no letter](images/text-04-text-unknown-codes.png)

Reopen with **View ▸ Text…**.

## 4. Type

Type over `Welcome!` in the Text window.

![An edited message](images/text-05-text-edited.png)

The region stays full (`2854 / 2854 cells`); typing overwrites. One run of typing
is one undo.

- Backspace/Delete blank a cell without closing the gap.
- **Insert** shifts text right; overflow is dropped (`'U' pushed off the end`).
- Enter sets the end-of-line bit on the previous character.
- Unmapped characters become blanks (`'@' has no code in this font`).

![Insert, and what it costs](images/text-05-text-insert.png)

## 5. A second region, the same alphabet

**File ▸ New Slice…**: Tilemap, `Level names`, `$21AC5`, length `$1CC`, same
**[F]** format, **Tiles** `GFX2A font`, **Cols** 19.

![The level names](images/text-06-level-names.png)

`[$38]`…`[$3C]` is `YELLOW` in a condensed face: six letters over five tiles, so
none map to a character.

**File ▸ Write All** (Ctrl+Shift+W), **File ▸ Save Project** (Ctrl+S).

![Written](images/text-07-written.png)

> Unticking **Use as Font** deletes the alphabet (after confirming; undoable).

## In the sample project

![The sample's message boxes](images/text-09-sample-messages.png)

The castle cutscenes use the same font with a 16-bit cell format:

![A castle line](images/text-09-sample-castle-line.png)

In `GFX28`, rows with **Role** **control** are named codes: `mario1`–`mario5`
read as `[mario1]` etc.

![Named codes](images/text-09-sample-named-codes.png)

**Append** adds codes past the sheet's end; here `$85` and `$86` are `'` and `"`
from the next sheet.

![Rows past the sheet](images/text-09-sample-appended-rows.png)
