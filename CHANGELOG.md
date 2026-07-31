# Changelog

## v0.4.4 - 2026-07-31

- **View toggles**: Show Pinned Palette Colors and Show Pinned
  Palette Rows now stored in user preferences
- **Base Palette Row** now applies to tile banks as well as maps, and moved to the
  palette panel
- **Palette > Wrap Palette Rows** (off by default): lets a row the base pushes off
  one end of the palette come back on at the other, instead of stopping at the
  first/last row
- **Plugin examples**: updated and some fixes applied, more uniform naming scheme

## v0.4.3 - 2026-07-31

- **Tilemaps**: tilemap improvements across the board

## v0.4.2 - 2026-07-30

- **File Container**: new info popup showing information about the file container
- **Tilemaps**: Tile source panel and edit tiles mode, lots of codec fixes, added
  Yoshi's Island sprite support
- **Tile Source panel**: Ctrl+wheel zoom and hold-space panning like the canvas,
  a Set Base Tile button, grid lines every 16 tiles, and the sheet now reads in
  the selected cell's palette row
- Tooltip cleanup pass

## v0.4.1 - 2026-07-30

- Add 0.5 zoom level and some more fixes

## v0.4.0 - 2026-07-30

- **Back / Forward navigation**, like a browser's
  history: `Alt+Left` / `Alt+Right`, the mouse's back/forward buttons, or the two
  new entries at the top of the Navigate menu.
- The tilemap bar's Tiles picker has a **jump button** beside it that shows the
  entry the map draws from, so you can go and look at a tile - or edit it where it
  lives - and come straight back with Back.
- **Zoom 0.5**: a half-size zoom level for reading a file too big for the window,
  a screen or a whole sprite sheet at once. Remembered with the project like any
  other zoom.
- Holding Ctrl while drag and dropping a file now prompts the user for what type
  of file (pixel/palette/tilemap) to import it as.
- **Tilemaps**: added extensive tile map support with custom containers, tilemap
  tools, and a complete set of SNES tilemap file formats (CGX/SCR/OBJ/PNL/MAP/etc).
  Sprite maps (OBJ) as a sub-type of tilemapsAll plugin based and similarly extensible.
  **Still Experimental**
- **View > Show Tile IDs**: numbers each tilemap cell with the tile it names, in
  hex, so you can see which tile a cell is drawing and not just what it looks like.
- Added .col palette file support
- Palette entries can now be renamed from the files list, like every other entry
- Performance and cleanup pass

## v0.3.5 - 2026-07-28

- **Compression**: added LZSS (4 KiB ring, size-prefixed), PRS (Sega LZ + RLE), and
  PowerVR (Dreamcast) textures
- **Presets**: added ARGB4444
- **View**: Entire File toggle - show the whole file at once instead of a row window.
  Should be used carefully on bigger iles, performance for anything other than viewing
  the canvas will probably be poor.

## v0.3.4 - 2026-07-27

- Highlight for currently viewed file
- Some project save cleanup

## v0.3.3 - 2026-07-27

- **Dark Mode**: added
- **Palette**: pinned palette regions, select tiles to pin to the current palette
  row/subpalette and keep that value through global palette changes. Also cleaned
  up palette imports and format selection
- Right click menu and menu bar cleanup

## v0.3.2 - 2026-07-27

- **Grid**: More grid settings, improved grid visuals, and grid saved to local
  preferences
- **Reshape presets**: expanded the number of reshape presets and support for
  more kinds of reshape plugins to support a wider variety of MAME graphics
- Cleaned up some plugin examples and documentation.
- `Open Recent` menu in the file menu

## v0.3.1 - 2026-07-27

- **Paste/Import fix**: pasted and imported images now work correctly on block
  layouts.

## v0.3.0 - 2026-07-27

- **New Reshape stage**: add a new reshape stage for byte reodering and split bitplanes.
  Configurable under `Edit File Container`.
- **Navigation**: improved navigation while in block mode.
- **Rearrange tool**: rotate tiles and blocks added.
- **Files**: added support for files with multiple component files appended
  together to support an arcade board's ROM chip. File rows can now be renamed,
  and reordered with Move Up/Down (`Shift+Up`/`Shift+Down`).
- **Containers**: container auto detection based on magic bytes or extension.
- Palette, pixel, and reshape name consistency pass
- Added keyboard mnemonics to all menus
- **Plugins**:
- - Added an example for each config based plugin engine
- - Data-LUT reshap plugins for scrabled values
- - Byte-swapped word reshape for MAME's ROM_LOAD16_WORD_SWAP (NMK16)
- - Updated plugin examples
- - Plugin example folder README
- Unified offset and slice meanings
- Added right click show in file manager
- Big codebase cleanup pass
- Lots of other fixes

## v0.2.5 - 2026-07-25

- **Performance**: big performance improvement pass.
- **Keyboard Shortcuts**: `H`/`V` flip and `C`/`X` rotate from the transform bar,
  `Shift` for the block transforms. `R` for the rearrange tool and `Shift+R` swaps its view.
- More fixes

## v0.2.4 - 2026-07-24

- **Rearrange tool**: drag and mirror tiles into a readable order for
  editing, without changing the file. Still in development.
- **Compression**: PackBits compression (TIFF / ILBM / MacPaint) added.
- **Bitmap Width**: view data of any pixel width without shearing.
- Various other fixes and improvements

## v0.2.3 - 2026-07-24

- Fixed a bug with floating selection and undo
- Undo/redo and palette grid cleanup

## v0.2.2 - 2026-07-24

- Help menu, readme, and name change (celPix)

## v0.2.1 - 2026-07-24

- Improved layout and some more tooltip fixes

## v0.2.0 - 2026-07-24

- **Canvas navigation**: Ctrl+scrollwheel zoom and hold space to pan.
- **File palettes**: cleaner file palette workflow.
- **Flip and rotate transforms**: a new toolbar with tile and block based
  flip/rotate.
- **Pixel editing mode**: a full pixel editing mode with drawing tools, cut
  copy paste, and transforms.
- **Tooltips**: updated and expanded tooltips. Tooltips on labels as well as
  inputs.
- **Shortcuts**: shortcut cleanup and adjustments.
- **Fixes**: various improvements and fixes.

## v0.1.0 - 2026-07-23

- **Color Palette Editing**: edit colors that get written back to file/offset/etc.
- **Cut/Copy/Paste Pixels**: both inside the application and to/from external
  editors. Rectangle selection mode to support copy pasting.
- **Export to PNG**: export files and slices as images.
- **Import from PNG**: import to tile selection position. Works similar to
  paste but from a PNG file. Supports both indexed color and RGB PNGs. Supports
  drag and drop.
- **Palette Files**: show palette files in the Files list; each remembers the
  color format it was last read with.
- **Palette from Emulator**: updated emulator state palette import to more modern
  emulators.
- Various bug fixes and improvements.

## v0.0.6 - 2026-07-22

- **Files panel browsing**: added bookmarks, slices and bookmarks are now sorted
  by offset, and added icons to differentiate them.
- **Hex panel**: View raw hex dump alongside the pixels.
- **Undo/Redo**: Full undo/redo stack.
- **Tile arrangement**: a picker of named presets for various arrangement patterns.
- **View improvements**: various view improvements including grid options  and cleanup.
- **Emulator State palette**: a new palette mode imports the live palette from
  an emulator save state.

## v0.0.5 - 2026-07-22

- **Fix**: selecting a pixel format whose bit depth is fixed by its codec no
  longer crashes on load.
- **Smoother format cycling**: rapidly switching pixel formats to eyeball an
  offset keeps its position. Row setting no longer clamps.
- **Cleaner end-of-data**: when the stream ends mid-row, the rest of that row
  shows the neutral background instead of black tiles.

## v0.0.4 - 2026-07-22

- **Projects**: save and reopen a session as a `.celpix` file.
- **Open multiple files with slices**: a Files dock holds several open files;
  mark offset+length regions (raw or compressed) as slices that edit and write
  back into the parent.
- **More SNES hardware support**: Mode 7 pixel/map split, direct-color and 2bpp
  presets, and an interleaved-ROM reader.
- **Konami RLE**: full round trip compress/decompress and added two variants.

## v0.0.3 - 2026-07-21

- **macOS builds for Intel Macs**: releases now ship separate Apple Silicon and
  Intel apps (macOS 13 or later; previously Apple Silicon only).

## v0.0.2 - 2026-07-21

- **Address-mapping coverage**: LoROM/HiROM presets in both anchor
  conventions with mirror folding, plus ExHiROM and ExLoROM for >4 MB carts.
- **SNES LZ compression**: LZ1, LZ2, and LZ16 codecs, decompress and
  recompress.
- **Decompression preview overlay**: live-previews the current view window
  decompressed; the main view keeps showing the raw bytes. Jump to Next and
  Scan make it easier to look for more compressed graphics.
- **Header skip**: hide a file header so offsets line up with the ROM proper.

## v0.0.1 - 2026-07-21

- **First release**: a cross-platform (Windows/Linux/macOS) retro-graphics tile
  viewer built on Python + PySide6, MIT-licensed, with packaged builds.
- **Plugin pipeline**: strictly-linear pixel and palette pathways where every
  stage is a plugin — drop-in TOML presets and Python code plugins with a trust
  prompt, plugin-folder discovery, and F5 hot-reload.
- **Broad format support** covering the YY-CHR / Tile Molester catalogue:
  planar, packed, chunky, and direct-color tile formats; mask-based and fixed
  hardware palettes; little/big-endian variants.
- **Containers & compression**: iNES header skip, Sega `.smd` deinterleave, and
  view-only Konami NES RLE decompression.
- **Windowed viewing of files of any size**: only the visible tile window is
  decoded; drag & drop to open; partial-tile files load fine; byte-identical
  save-back.
- **Navigation**: tile/row/page stepping, a byte nudge (+B/−B/0B) for
  realigning off-grid graphics, a byte-exact offset box with bank-address
  formats (SNES LoROM/HiROM, GB, GBA, PCE) alongside flat hex, a file-position
  scrollbar, and a View menu listing every shortcut.
- **Palette workflow**: a dockable swatch-grid panel with subpalette-aware
  selection, readout, and keyboard stepping; palette sources Custom / File /
  Offset, including Load from Selection (P) to view palettes embedded in the
  pixel file.