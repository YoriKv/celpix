# Changelog

## v0.3.6 - unreleased

- **Tilemaps**: screen, panel and stamp-layout files open as tilemap entries and
  render their cells through a bound tile source, always showing the whole map.
  The row count and the file-position bar are disabled there, since a tilemap has
  no view window to move.
  The bar under the canvas carries the binding — which open entry supplies the
  tiles, where tile 0 sits in it, and the cell format — in place of the file
  offset controls, which a tilemap has no use for. Picking a file that isn't open
  yet opens it as an entry first. Saving writes the cells back through the file's
  own container, headers intact, and controls that don't apply to a tilemap
  (pixel tools, rearrange, pinned palettes, rotate) switch themselves off.
- **Base tile** accepts negative values, and binding a map to tiles it overflows
  now works it out: a screen numbering from $100, bound to a slice that starts
  there, lands on the slice's first tile instead of off the end. A map that
  already fits its source is left alone, and a base you set yourself is never
  overwritten.
- **Palettes (COL)**: S-CG-CAD palette files are recognised as a container, so
  one opens with its 256 real colours instead of decoding its trailing metadata
  block as 128 more.
- **Palette entries** carry a container of their own and offer "Edit File
  Container…", so a palette whose colours stop before its bytes do can be framed
  and corrected like any other file. Containers now declare which kind of entry
  they frame, and the picker lists only those — a palette is never offered a ROM
  wrapper, or the reverse.
- **Screens (SCR)**: a screen with 16x16 cells now opens as one, instead of
  drawing a quarter of every cell and dropping the rest.
- **Panels (PNL)**: a panel now opens at its true size. A panel word is one 8x8
  tile in every file of the survey, whichever of its three cell-size-shaped header
  bytes is set, and reading any of them was drawing the panel at four times its
  content.
- **Sprite objects (OBJ/OBX)**: open as tilemap entries and draw their frames one
  after another, each frame's parts assembled at the pixel offsets the file gives
  them. View-only for now, and the tiles come from whichever entry the object is
  bound to, like any other tilemap.
- **Tilemap entries** now take a tilemap format dropdown on the codecs toolbar, in
  place of the pixel format and compression pickers - neither of which says
  anything about a file of cells.
- **Exporting a tilemap** now writes the map as it is drawn on screen, rather
  than the tile bank it borrows its tiles from. Export Raw writes its cells for
  the same reason.
- **Cell transforms ask the format.** Flipping a cell goes through the tilemap
  format's own codec, which knows which bits - if any - say a flip. A format with
  nowhere to put one, like an index-only Game Boy map, now says so in the status
  bar and changes nothing, instead of showing a flip that the next save drops.
- **Tile banks (CGX)**: recognised as a container, so a bank opens at the bit
  depth the file records - 2, 4 or 8bpp - with its trailing header and table cut
  off instead of decoding as junk tiles. The per-tile palette-row table a 2bpp or
  4bpp bank carries seeds pinned palette regions.
- **Pinned palettes**: "Show Pinned Palettes" is now "Show Pinned Palette
  Colors", and a separate "Show Pinned Palette Rows" numbers each pinned tile
  with its subpalette row in the grid's colour.
- **Panels (PNL)**: read their own header - cell size and width - so a panel
  opens at the right geometry instead of needing it guessed, including the
  minority that use 8x8 cells rather than 16x16. Screens and stamp layouts
  publish their widths too.
- **Stamp layouts (MAP)**: read through the panel they were authored against -
  pick the panel in the binding bar and the layout draws with that panel's tiles
  and attributes. View-only: what an edit to a stamp should mean isn't settled.
- **Tilemap editing**: flip a cell or a block of cells - the block reorders them
  and flips each - and copy/cut/paste/clear cells within the app. Cell edits
  undo as one step and mark the map unsaved. The system clipboard is untouched:
  a cell is an index into a tile source, which means nothing outside celPix.
- **Opening files**: File ▸ Open tilemap data reads any file as a map of tile
  indices, and holding Ctrl while dropping a file asks whether to read it as
  pixels, a palette or a tilemap. `.col` files now land in the Palettes list.
- **Files list**: entries are grouped into Pixels, Tilemaps and Palettes
  sections, each shown only while it holds something.
- **Containers**: SCR, PNL and MAP authoring assets are now recognised and
  unwrapped to their payload, headers preserved on write. Container detection
  also looks deeper into a file, so a format whose signature sits past its data
  can be identified at all.

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