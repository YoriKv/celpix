# Changelog

## v0.1.0 - unreleased

- Initial project foundation: PySide6 app, `src/` layout, uv/ruff/pytest tooling,
  MIT license, and a cross-platform build & release pipeline.
- Plugin-based editing pipeline: strictly-linear pixel and palette pathways where
  every stage is a plugin (compression stubbed for now).
- Tile & palette format support: planar tile formats (GB 2bpp, SNES 4bpp, NES 2bpp)
  and mask-based palettes (BGR555, RGB888), defined as data-only TOML presets.
- View-only editor: open pixel and palette data, view the tiles, and save back.
- Extensible via a plugin folder: drop-in TOML presets and Python code plugins,
  with a trust prompt for code and a Refresh (F5) hot-reload for authors.
