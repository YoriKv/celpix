# Changelog

## v0.0.1 - 2026-07-21

- **First release**: a cross-platform (Windows/Linux/macOS) retro-graphics tile
  viewer built on Python + PySide6, MIT-licensed, with packaged builds.
- **Plugin pipeline**: strictly-linear pixel and palette pathways where every
  stage is a plugin — drop-in TOML presets and Python code plugins with a trust
  prompt, plugin-folder discovery, and F5 hot-reload.
- **Broad format support** covering the YY-CHR / Tile Molester catalogue:
  planar, packed, chunky, and direct-colour tile formats; mask-based and fixed
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