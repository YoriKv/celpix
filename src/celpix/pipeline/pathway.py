"""Per-pathway pipeline configuration.

A :class:`PathwayConfig` names the plugin chosen for each stage of one pathway
(pixel or palette) plus its source/destination. Two of these — one per pathway —
plus the shared view options fully describe a load/save (see
``docs/design/overview.md`` §7). It is plain data; the project file stores the
workspace entries it is rebuilt from rather than the config itself
(:mod:`celpix.project.projectfile`).
"""

from __future__ import annotations

from dataclasses import dataclass

from celpix.core.errors import Stage
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
    # Set when this pathway's encoded bytes belong *inside* another entry's
    # region rather than at a file position of their own — every **slice**, whose
    # parent owns the region's bytes (``docs/design/slices-and-parents.md``).
    # False for a whole file, which is the owner and where the buffer finally
    # becomes files. ``dest`` still carries the slice's bounds — they are what the
    # slot checks are made against — but the bytes are delivered by splicing them
    # into the parent's buffer and writing the *parent*: the only write that knows
    # how the region maps back onto the files, and the one that lets the parent's
    # container run its own write half over bytes that changed inside it.
    #
    # A *fact* here rather than a question asked of the entry, because this is the
    # plain-data handoff to a pipeline that knows nothing of entries or workspaces
    # — and it is what lets `save` see that it must not deposit while still being
    # unable to see what to deposit into, so it refuses rather than guesses. The
    # host routes it (``project.workspace.pixel_config_for``).
    writes_through_parent: bool = False
    # The stored plugin ids this build hasn't got, as ``(stage, wanted id)``.
    # The stage falls back to its pass-through so the file still opens, and the
    # entry goes view-only; carried here so the load can say *which* plugin is
    # missing rather than leaving the user with a greyed-out Write and no reason.
    missing_plugins: tuple[tuple[Stage, str], ...] = ()

    def write_target(self) -> FileRef:
        """Where Write should put the bytes: explicit ``dest`` or back to source."""
        return self.dest if self.dest is not None else self.source

    @property
    def reads_raw_bytes(self) -> bool:
        """Whether a position in the view still names a position in the file.

        Both a decompression and a reshape are byte permutations, so under either
        the on-screen bytes are no one's file offset and the address display has
        nothing true to show. One statement of that rule, since every surface
        that maps between the two spaces has to ask it.
        """
        return self.compression_id == NO_COMPRESSION and self.reshape_id == NO_RESHAPE

    @property
    def positions_are_slice_offsets(self) -> bool:
        """Whether a view position names a place a slice can be anchored.

        Weaker than :attr:`reads_raw_bytes`, and deliberately so: a slice offset
        is a position in its **parent's own coordinates**, which are file offsets
        only when the parent reads the file straight. Where a reshape or a
        permuting container moved the bytes, the parent's coordinates are its
        reordered buffer — and that is exactly what a slice of it reads
        (``workspace._parent_view_bytes``) *and* what the view displays
        (``_display_base`` falls back to 0-based positions there), so the two
        agree and a carved region is the one that was on screen.

        Decompression is the one stage that breaks it. A decompressed stream is
        not a permutation of the bytes it came from — it is longer, with no
        position-for-position mapping back — so nothing in it can anchor a slice,
        whose offset has to name where the *compressed* structure starts.
        """
        return self.compression_id == NO_COMPRESSION
