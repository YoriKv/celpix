"""Per-pathway pipeline configuration.

A :class:`PathwayConfig` names the plugin chosen for each stage of one pathway
plus its source/destination. It is the same shape for all three — pixel,
palette, tilemap — because the stages ahead of Interpret do not care which one
they are running for: :func:`~celpix.pipeline.pipeline._read_reshape_decompress`
takes any of them, and the pathway is only the label a failure is reported
under. A pixel + palette pair plus the shared view options fully describe a
graphic's load/save; a tilemap entry carries a third alongside them, addressing
its *own* file where the pixel one addresses the tiles it borrows
(``docs/design/tilemap-entry.md`` §3). It is plain data; the project file stores
the workspace entries it is rebuilt from rather than the config itself
(:mod:`celpix.project.projectfile`).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from celpix.core.errors import Stage
from celpix.plugins.base import NO_COMPRESSION, NO_RESHAPE, RAW_CONTAINER, FileRef


class SlotFill(str, Enum):
    """What becomes of a bounded slot's tail when a write comes up short.

    A recompressor rarely reproduces the packing the original build used, so a
    re-encoded blob routinely lands smaller than the one it replaces and leaves
    room at the end of the slot the new stream never reaches. Every scheme here
    is self-delimiting, so nothing *reads* those bytes whichever way this goes —
    what it decides is what a person looking at the file finds there.

    - ``KEEP`` writes only what was produced, leaving the previous stream's tail
      standing. The most conservative answer, and the only one that cannot
      destroy data a slot's bounds wrongly claimed: a length measured to the next
      known offset can take in an alignment pad or a neighbour's header, and
      those bytes are only ours to overwrite if the bounds were right.
    - ``FF`` is the erased state of EPROM and flash, so it is what unwritten
      cartridge space physically reads as, and a run of it is unmistakable in a
      hex dump where a run of ``00`` is not.
    - ``ZERO`` for the images whose own padding is ``00`` — matching what
      surrounds the slot is worth more than either constant's pedigree.

    ``value`` is the string the project file stores, str-valued for the same
    reason :class:`~celpix.core.capabilities.ContentKind` is: the on-disk schema
    is a name rather than an ordinal that reordering this enum would change.
    """

    KEEP = "keep"
    FF = "ff"
    ZERO = "zero"

    @classmethod
    def parse(cls, value: object) -> SlotFill:
        """``value`` as a fill, falling back to the default for anything else.

        Tolerant like the rest of the project reader: a newer celPix's answer, or
        a hand-edited typo, pads the way an unstated one does rather than failing
        the entry.
        """
        try:
            return cls(value)
        except ValueError:
            return DEFAULT_SLOT_FILL

    @property
    def filler(self) -> bytes:
        """The byte to pad with, or empty for "leave the tail alone"."""
        return _FILLER_BYTES[self]


DEFAULT_SLOT_FILL = SlotFill.FF
_FILLER_BYTES = {SlotFill.KEEP: b"", SlotFill.FF: b"\xff", SlotFill.ZERO: b"\x00"}


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
    # What to do with the room a short compressed result leaves at the end of a
    # bounded slot (:class:`SlotFill`). Only a *compressed* pathway can come up
    # short — everywhere else the result is the length of the buffer it was read
    # from — so this is read only there, and the slice dialog offers it only
    # there too.
    slot_fill: SlotFill = DEFAULT_SLOT_FILL
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
        (``_anchor_base`` falls back to 0-based positions there), so the two
        agree and a carved region is the one that was on screen.

        Decompression is the one stage that breaks it. A decompressed stream is
        not a permutation of the bytes it came from — it is longer, with no
        position-for-position mapping back — so nothing in it can anchor a slice,
        whose offset has to name where the *compressed* structure starts.
        """
        return self.compression_id == NO_COMPRESSION
