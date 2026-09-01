# Changelog

## v0.5.14 - 2026-08-31

- More tilemap properties editing improvements and cleanup

## v0.5.13 - 2026-08-31

- Tilemap properties are now editable both in place and when placing tiles
- Some more tilemap fixes

## v0.5.12 - 2026-08-31

- Edit tiles previews the selected tile or stamp
- Right click drag in edit tile mode to select a stamp for placement. Works in both the canvas and tile source
- Fixes for stamp based tilemaps
- Cleand up S-CG-CAD tilemap format naming

## v0.5.11 - 2026-08-28

- Files list multi-select for move up/down and delete
- Canvas zoom is app-wide instead of per file
- Fixes

## v0.5.10 - 2026-08-14

- Added a filter box in the files list
- Added Pixel Aspect setting for rendering 2:1 and other pixel aspect ratios
- New composite views to model composite VRAM
- Lots of fontmap work improvements and fixes

## v0.5.9 - 2026-08-12

- More fontmap fixes

## v0.5.8 - 2026-08-12

- Fixed Refresh Plugins breaking tilemaps

## v0.5.7 - 2026-08-12

- Even more example plugin cleanup

## v0.5.6 - 2026-08-12

- Clean up comments on all plugin examples

## v0.5.5 - 2026-08-12

- Sort files and slices by name, offset, type
- Lots of updates and fixes to both the functionality and ui of font maps

## v0.5.4 - 2026-08-11

- Reorder slices, as well as cut/copy/paste/duplicate
- Added snes 16x16 tilemap format
- Lots of fontmap cleanup
- Lots of other various fixes and cleanups

## v0.5.3 - 2026-08-10

- **Subsprites window** (View -> Subsprites): separate window for displaying
  the subsprites of a sprite map
- **Bit layouts in presets**: any plugin config or preset that requires decoding
  bits can now specify how via a bit layout (ex: `vhop ppii iiii iiii`)
- Lots of fontmap and tilemap fixes
- Tooltips and shortcuts cleaned up and updated

## v0.5.2 - 2026-08-10

- Improved fontmap support, new alphabet editor build into the UI

## v0.5.1 - 2026-08-10

- Big cleanup pass on all code, plugins, and comments

## v0.5.0 - 2026-08-09

- **Fontmaps**: experimental support for "Fontmaps" which will theoretically be
  a universal string editing system for tile based fonts
- **New formats**: Nemesis, Enigma, Kosinski, SLZ16 and SLZ24 compression
- **Pixel editing on sprite objects**: added
- **Searchable, grouped format pickers**: cleaner and easier to use
- **Project plugins**: local to project plugin support
- Windows remember their layout
- Various fixes

## v0.4.9 - 2026-08-08

- Local build support: `packaging/build.py`
- Various fixes

## v0.4.8 - 2026-08-06

- **SNES LZ1/LZ2 variants**: byte-identical encoders alongside "improved"
  variants that pack tighter and still decompress accurately
- Various fixes

## v0.4.7 - 2026-08-04

- **New formats**: PlayStation TIM textures (all four depths), GBA/NDS BIOS LZ77
  compression, TPL palettes, PlayStation 4bpp/8bpp tiles, and a 128-wide 8bpp
  linear bitmap
- Animation player panning
- Various fixes

## v0.4.6 - 2026-08-03

- **Pixel editing on a tilemap**: the drawing tools work on the map itself, and
  the edit lands in the tile bank it is bound to
- **Animation player** (View ▸ Animation): sprite files play their stored
  animation sequences in a window of their own
- **Compression**: better-packing variants of the LZ algorithms alongside their
  byte-exact implementations
- Various fixes

## v0.4.5 - 2026-07-31

- **Palette rows**: unified palette row pin and tilemap palette row selection
- **Formats**: more tile and palette formats
- Various fixes

## v0.4.4 - 2026-07-31

- **View toggles**: Show Pinned Palette Colors and Show Pinned Palette Rows are
  stored in user preferences
- **Base Palette Row** applies to tile banks as well as maps, from the palette
  panel
- **Palette ▸ Wrap Palette Rows** (off by default): a row pushed off one end of
  the palette comes back on at the other
- **Plugin examples**: refreshed, with a more uniform naming scheme
- Various fixes

## v0.4.3 - 2026-07-31

- **Tilemaps**: improvements across the board

## v0.4.2 - 2026-07-30

- **File Container**: info popup describing the container
- **Tilemaps**: Tile source panel and edit tiles mode
- **Tile Source panel**: Ctrl+wheel zoom, hold-space panning, a Set Base Tile
  button, grid lines every 16 tiles, and the sheet reads in the selected cell's
  palette row
- Tooltip cleanup pass
- Various fixes

## v0.4.1 - 2026-07-30

- 0.5 zoom level
- Various fixes

## v0.4.0 - 2026-07-30

- **Back / Forward navigation** like a browser's: `Alt+Left` / `Alt+Right`, the
  mouse's back/forward buttons, or the Navigate menu. The tilemap bar's Tiles
  picker has a jump button to the entry the map draws from
- **Zoom 0.5**: a half-size zoom for reading a whole file or sprite sheet at
  once, remembered with the project
- **Tilemaps**: extensive tilemap support with custom containers, tilemap tools,
  and a complete set of SNES tilemap formats (CGX/SCR/OBJ/PNL/MAP), sprite maps
  as a sub-type, all plugin-based. **Still experimental**
- **View ▸ Show Tile IDs**: numbers each cell with the tile it names, in hex
- Ctrl while dragging a file in prompts for the type to import it as
- `.col` palette support, and palette entries can be renamed from the Files list
- Various fixes

## v0.3.5 - 2026-07-28

- **Compression**: LZSS (4 KiB ring, size-prefixed), PRS (Sega LZ + RLE), and
  PowerVR (Dreamcast) textures
- **Presets**: ARGB4444
- **View ▸ Entire File**: show the whole file at once instead of a row window.
  Editing performance on large files will be poor

## v0.3.4 - 2026-07-27

- Highlight for the currently viewed file
- Various fixes

## v0.3.3 - 2026-07-27

- **Dark Mode**
- **Palette**: pinned palette regions — pin tiles to a palette row and keep that
  value through global palette changes — plus cleaner palette imports and format
  selection
- Right-click menu and menu bar cleanup

## v0.3.2 - 2026-07-27

- **Grid**: more grid settings, improved visuals, and grid saved to preferences
- **Reshape presets**: more presets and plugin kinds, covering a wider variety of
  MAME graphics
- **Open Recent** in the File menu
- Various fixes

## v0.3.1 - 2026-07-27

- Various fixes

## v0.3.0 - 2026-07-27

- **Reshape stage**: byte reordering and split bitplanes, configurable under
  `Edit File Container`
- **Files**: support for several component ROMs appended into one file, with
  renaming and Move Up/Down (`Shift+Up`/`Shift+Down`)
- **Containers**: auto-detection from magic bytes or extension
- **Rearrange tool**: rotate tiles and blocks
- **Navigation**: improved block-mode navigation
- **Plugins**: an example per config-based plugin engine, data-LUT reshape
  plugins for scrambled values, byte-swapped word reshape for MAME's
  `ROM_LOAD16_WORD_SWAP`, and a plugin-folder README
- Keyboard mnemonics on all menus, and right-click Show in File Manager
- Various fixes

## v0.2.5 - 2026-07-25

- **Performance**: large improvement pass
- **Shortcuts**: `H`/`V` flip and `C`/`X` rotate from the transform bar, `Shift`
  for the block transforms, `R` for the rearrange tool and `Shift+R` to swap its
  view
- Various fixes

## v0.2.4 - 2026-07-24

- **Rearrange tool**: drag and mirror tiles into a readable order for editing,
  without changing the file
- **Compression**: PackBits (TIFF / ILBM / MacPaint)
- **Bitmap Width**: view data of any pixel width without shearing
- Various fixes

## v0.2.3 - 2026-07-24

- Undo/redo and palette grid cleanup
- Various fixes

## v0.2.2 - 2026-07-24

- Help menu, readme, and name change (celPix)

## v0.2.1 - 2026-07-24

- Improved layout
- Various fixes

## v0.2.0 - 2026-07-24

- **Pixel editing mode**: a full pixel editing mode with drawing tools, cut /
  copy / paste, and transforms
- **Canvas navigation**: Ctrl+scrollwheel zoom and hold space to pan
- **Flip and rotate transforms**: a toolbar with tile- and block-based
  flip/rotate
- **File palettes**: cleaner file palette workflow
- **Tooltips**: expanded, on labels as well as inputs
- Various fixes

## v0.1.0 - 2026-07-23

- **Color palette editing**: edit colors and write them back to file/offset
- **Cut/Copy/Paste pixels**: inside the application and to/from external editors,
  with a rectangle selection mode
- **Export to PNG**: export files and slices as images
- **Import from PNG**: to the tile selection position, from indexed or RGB PNGs,
  including drag and drop
- **Palette files**: shown in the Files list, each remembering the color format
  it was last read with
- **Palette from Emulator**: save-state palette import for modern emulators
- Various fixes

## v0.0.6 - 2026-07-22

- **Files panel browsing**: bookmarks, slices and bookmarks sorted by offset, and
  icons to tell them apart
- **Hex panel**: raw hex dump alongside the pixels
- **Undo/Redo**: full undo/redo stack
- **Tile arrangement**: a picker of named arrangement presets
- **Emulator state palette**: import the live palette from an emulator save state
- View and grid improvements

## v0.0.5 - 2026-07-22

- Rapidly switching pixel formats keeps the position
- When the stream ends mid-row, the rest of the row shows the neutral background
- Various fixes

## v0.0.4 - 2026-07-22

- **Projects**: save and reopen a session as a `.celpix` file
- **Open multiple files with slices**: a Files dock holds several open files, and
  offset+length regions (raw or compressed) edit and write back into the parent
- **More SNES hardware support**: Mode 7 pixel/map split, direct-color and 2bpp
  presets, and an interleaved-ROM reader
- **Konami RLE**: full round trip, with two variants

## v0.0.3 - 2026-07-21

- **macOS builds for Intel Macs**: separate Apple Silicon and Intel apps
  (macOS 13 or later)

## v0.0.2 - 2026-07-21

- **Address mapping**: LoROM/HiROM presets in both anchor conventions with mirror
  folding, plus ExHiROM and ExLoROM for >4 MB carts
- **SNES LZ compression**: LZ1, LZ2 and LZ16, decompress and recompress
- **Decompression preview overlay**: live-previews the current view window
  decompressed while the main view keeps showing raw bytes, with Jump to Next and
  Scan for finding more compressed graphics
- **Header skip**: hide a file header so offsets line up with the ROM proper

## v0.0.1 - 2026-07-21

- **First release**: a cross-platform (Windows/Linux/macOS) retro-graphics tile
  viewer built on Python + PySide6, MIT-licensed, with packaged builds
- **Plugin pipeline**: strictly-linear pixel and palette pathways where every
  stage is a plugin — drop-in TOML presets and Python code plugins with a trust
  prompt, plugin-folder discovery, and F5 hot-reload
- **Broad format support**: planar, packed, chunky and direct-color tile formats;
  mask-based and fixed hardware palettes; little- and big-endian variants
- **Containers & compression**: iNES header skip, Sega `.smd` deinterleave, and
  view-only Konami NES RLE decompression
- **Windowed viewing of files of any size**: only the visible tile window is
  decoded; drag & drop to open; partial-tile files load fine; byte-identical
  save-back
- **Navigation**: tile/row/page stepping, a byte nudge (+B/−B/0B) for realigning
  off-grid graphics, a byte-exact offset box with bank-address formats (SNES
  LoROM/HiROM, GB, GBA, PCE) alongside flat hex, a file-position scrollbar, and a
  View menu listing every shortcut
- **Palette workflow**: a dockable swatch-grid panel with subpalette-aware
  selection, readout and keyboard stepping; palette sources Custom / File /
  Offset, including Load from Selection (P) for palettes embedded in the pixel
  file
