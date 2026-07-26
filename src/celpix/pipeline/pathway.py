"""Per-pathway pipeline configuration.

A :class:`PathwayConfig` names the plugin chosen for each stage of one pathway
(pixel or palette) plus its source/destination. Two of these — one per pathway —
plus the shared view options fully describe a load/save (see
``docs/design/overview.md`` §7). It is plain data, so it is already the core of a
future project file.
"""

from __future__ import annotations

from dataclasses import dataclass

from celpix.plugins.base import NO_COMPRESSION, NO_RESHAPE, RAW_CONTAINER, FileRef


@dataclass
class PathwayConfig:
    """The plugin ids + source/dest for one pathway.

    ``interpret_preset_id`` selects a registered :class:`~celpix.plugins.base.Preset`
    (which in turn names the codec engine and its params). ``dest`` defaults to
    ``source`` at save time, so a round trip writes back where it was read from;
    ``write_enabled=False`` skips the save-side stages entirely (used for
    view-only palettes, and for a container or compression scheme that ships no
    inverse).

    One id per stage, covering both directions: the container that unwraps this
    pathway's file is the one that re-wraps it, and the scheme that decompresses
    is the one that compresses. A load and a save cannot disagree about which
    plugin they are going through, because there is only one to name.
    """

    source: FileRef
    interpret_preset_id: str
    container_id: str = RAW_CONTAINER
    reshape_id: str = NO_RESHAPE
    compression_id: str = NO_COMPRESSION
    dest: FileRef | None = None
    write_enabled: bool = True

    def write_target(self) -> FileRef:
        """Where Write should put the bytes: explicit ``dest`` or back to source."""
        return self.dest if self.dest is not None else self.source
