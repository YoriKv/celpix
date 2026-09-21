# Text and Fonts

A text string is a tilemap that uses a font sheet. An **alphabet** on the font
gives a character to each tile. Every string that uses the font can then be read
as text.

Before you start, save a project with the ROM open. See
[Getting Started](Getting-Started) step 1.

## 1. Carve the font

1. Set **Pixel** to **SNES 2bpp (8x8)**.
2. Go to `$5CB7B`.
3. Set **Compression** to **LZ2 (SMW, Yoshi's Island)**.

![The font, unpacked](images/text-01-font-preview.png)

4. **File ▸ New Slice from View**. Name the slice `GFX2A font`.

![The slice, filled in](images/text-02-font-slice-dialog.png)

5. Set **Rows** to 8 and **Zoom** to 3.
6. Take the palette from a save state. See
   [Getting Started](Getting-Started#3-take-a-palette-from-a-save-state).
7. Set **Palette Row** to 6.

![The font sheet](images/text-02-font-slice.png)

## 2. Spell it

1. On the toolbar, tick **Use as Font**. The **Font Alphabet** window opens. You
   can also open it with **View ▸ Font Alphabet…**.

![The alphabet, empty](images/text-03-alphabet-empty.png)

2. Click a start tile and paste (Ctrl+V). celPix puts one character on each
   tile. Do this for each row of the table:

| Click tile | Paste |
| --- | --- |
| `$00` | `ABCDEFGHIJKLMNOPQRSTUVWXYZ!.-,? ` (the last character is a space) |
| `$40` | `abcdefghijklmnopqrstuvwxyz#()'` |
| `$64` | `12345670` |

![The alphabet, filled](images/text-03-alphabet-filled.png)

**Characters** shows the character on each tile when you zoom in.

![Captions](images/text-03-alphabet-captions.png)

To set one tile: click it, press Enter, type the character, and press Enter.

**Fill with…** has two presets: `A-Z 0-9, from 0` and `ASCII, from $20`.
**Base code** changes the first code.

## 3. Carve the text

1. Select the ROM.
2. **File ▸ New Slice…**, with these values:

| | |
| --- | --- |
| **Content** | Tilemap |
| **Name** | `Message boxes` |
| **Offset** | `$2A5D9` |
| **Length** | `$B26` |
| **Compression** | None (uncompressed) |

![The text's slice](images/text-04-text-slice-dialog.png)

In this format, the top bit of the last character of a line ends the line.

3. Set **Tilemap** to **[F] Text run (8-bit, high bit ends a line)**.
4. Set **Cols** to 18.

A format marked **[F]** opens the **Text** window. With no font bound, the cells
show as hex codes.

![No alphabet yet](images/text-04-text-unbound.png)

5. Set **Tiles** to `GFX2A font`.

![The messages](images/text-04-text-bound.png)

On the canvas, amber marks the end of each line. Short lines move the grid out
of line. Use the Text window to read the text.

> If you bind to a sheet that does not have **Use as Font** ticked, celPix offers
> to tick it.

Codes with no character show as hex. For example, `[$60][$61]` `[$62][$63]` is
Yoshi's paw print. If you select a code in the Text window, celPix selects its
cell.

![Codes with no letter](images/text-04-text-unknown-codes.png)

To open the Text window again: **View ▸ Text…**.

## 4. Type

Type over `Welcome!` in the Text window.

![An edited message](images/text-05-text-edited.png)

The region is always full (`2854 / 2854 cells`). Typing replaces the characters
that are there. One run of typing is one undo step.

- Backspace and Delete make a cell blank. They do not close the gap.
- **Insert** moves the text to the right. Text that goes past the end is lost
  (`'U' pushed off the end`).
- Enter sets the end-of-line bit on the character before it.
- A character that is not in the font becomes a blank (`'@' has no code in this
  font`).

![Insert, and what it costs](images/text-05-text-insert.png)

## 5. A second region, the same alphabet

1. **File ▸ New Slice…**, with these values:
   - **Content**: Tilemap
   - **Name**: `Level names`
   - **Offset**: `$21AC5`
   - **Length**: `$1CC`
2. Set **Tilemap** to the same **[F]** format.
3. Set **Tiles** to `GFX2A font`.
4. Set **Cols** to 19.

![The level names](images/text-06-level-names.png)

Codes `[$38]` to `[$3C]` spell `YELLOW` in a narrow font. The six letters use
five tiles, so no tile matches one character.

5. **File ▸ Write All** (Ctrl+Shift+W).
6. **File ▸ Save Project** (Ctrl+S).

![Written](images/text-07-written.png)

> If you untick **Use as Font**, celPix deletes the alphabet. It asks first, and
> you can undo it.

## In the sample project

![The sample's message boxes](images/text-09-sample-messages.png)

The castle cutscenes use the same font. Their cells are 16 bits:

![A castle line](images/text-09-sample-castle-line.png)

In `GFX28`, a row with **Role** set to **control** is a named code. Codes
`mario1` to `mario5` show as `[mario1]` and so on.

![Named codes](images/text-09-sample-named-codes.png)

**Append** adds codes after the end of the sheet. Here, `$85` and `$86` are `'`
and `"` from the next sheet.

![Rows past the sheet](images/text-09-sample-appended-rows.png)
