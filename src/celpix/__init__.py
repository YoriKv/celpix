"""Celpix — a graphics and palette editor for retro-game data.

The high-level design lives in ``docs/design/overview.md``. The package is laid
out to mirror that design's major subsystems:

- :mod:`celpix.core` — stage-agnostic data model (byte buffers, index grid,
  palette, the forward-flowing context/hints bag).
- :mod:`celpix.pipeline` — the strictly linear stage pipeline (Read, Decompress,
  View & Edit, Compress, Write) across the pixel and palette pathways.
- :mod:`celpix.plugins` — the plugin API and registry; every built-in stage
  behavior is itself a plugin on this API.
- :mod:`celpix.undo` — per-launch undo/redo history.
- :mod:`celpix.project` — resumable project save/load.
- :mod:`celpix.ui` — the Qt front end (main window, dockers, canvas).
- :mod:`celpix.resources` — bundled, read-only resources (format data, presets,
  icons, font), resolved so they survive frozen release builds.

Only :mod:`celpix.ui` (and the :mod:`celpix.app` bootstrap) may import Qt; every
other subsystem stays Qt-free so it is testable and reusable headless.
"""

__version__ = "0.0.3"
