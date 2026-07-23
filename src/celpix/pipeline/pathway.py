"""Per-pathway pipeline configuration.

A :class:`PathwayConfig` names the plugin chosen for each stage of one pathway
(pixel or palette) plus its source/destination. Two of these — one per pathway —
plus the shared view options fully describe a load/save (see
``docs/design/overview.md`` §7). It is plain data, so it is already the core of a
future project file.
"""

from __future__ import annotations

from dataclasses import dataclass

from celpix.plugins.base import NO_COMPRESS, NO_DECOMPRESS, FileRef


@dataclass
class PathwayConfig:
    """The plugin ids + source/dest for one pathway.

    ``interpret_preset_id`` selects a registered :class:`~celpix.plugins.base.Preset`
    (which in turn names the codec engine and its params). ``dest`` defaults to
    ``source`` at save time, so a round trip writes back where it was read from;
    ``write_enabled=False`` skips Write entirely (used for view-only palettes).
    """

    source: FileRef
    interpret_preset_id: str
    read_id: str = "read.raw-file"
    decompress_id: str = NO_DECOMPRESS
    compress_id: str = NO_COMPRESS
    write_id: str = "write.raw-file"
    dest: FileRef | None = None
    write_enabled: bool = True

    def write_target(self) -> FileRef:
        """Where Write should put the bytes: explicit ``dest`` or back to source."""
        return self.dest if self.dest is not None else self.source
