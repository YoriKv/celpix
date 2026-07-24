# Changelog

## v0.2.0 - unreleased

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