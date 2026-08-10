"""Pipeline failure types.

The pipeline **hard-stops** at the first stage that cannot proceed and surfaces
*which stage, which pathway, and why* — it never degrades, guesses, or writes
partial output (see ``docs/design/overview.md`` §2, Failure handling). A
:class:`PipelineError` carries exactly that context so the UI can report it.
"""

from __future__ import annotations

from enum import Enum


class Stage(str, Enum):
    """The pipeline's extension points, in forward order.

    A stage is **one plugin covering both directions**: a container is the read
    *and* write of one on-disk wrapper, a compression scheme is its decompress
    *and* compress halves. They are one stage because they are one thing a user
    picks, one thing a plugin author writes, and one thing that has to stay each
    other's inverse.

    Which direction a load or save is going is not lost by that: it is the
    :attr:`PipelineError.action` sub-label, reported when something fails.

    String-valued for readable ids.
    """

    CONTAINER = "container"
    RESHAPE = "reshape"
    COMPRESSION = "compression"
    INTERPRET_PIXEL = "interpret-pixel"
    INTERPRET_PALETTE = "interpret-palette"
    INTERPRET_TILEMAP = "interpret-tilemap"
    # The one stage that is not on the byte path at all: it turns a fontmap's
    # decoded *cells* into readable text and back (``docs/design/fontmap-entry.md``).
    # A stage rather than a corner of the tilemap codec because of who states it —
    # the lookup belongs to the **font**, and one font is shared by every string
    # drawn through it, so it is picked once per tile source and not once per map.
    ALPHABET = "alphabet"


class Pathway(str, Enum):
    """The parallel pathways data flows along (overview.md §2).

    Pixel and palette run for one document and converge at the interactive
    stage. Tilemap is a third interpretation of a byte buffer rather than a
    third half of the same document: it belongs to an entry of its own, which
    *names* the pixel entry supplying its tiles (``docs/design/tilemap-entry.md``).
    It is a pathway here for the reason the other two are — it is the label a
    failure is reported under.
    """

    PIXEL = "pixel"
    PALETTE = "palette"
    TILEMAP = "tilemap"


class PipelineError(Exception):
    """A stage could not proceed; the pipeline halts and reports this.

    Attributes mirror what the user needs to fix the configuration and retry.

    ``action`` names the *direction* within the stage — ``read``/``write`` for a
    container, ``reshape``/``unshape`` for a reshape,
    ``decompress``/``compress`` for a compression scheme. A stage spans both
    directions, and "the container failed" is a materially different report from
    "the container failed **while saving**", so the message keeps it:
    ``[pixel/container:write] …``. Empty for a stage where there is nothing to
    disambiguate.

    ``plugin`` names **which** plugin or preset was running, and is what turns a
    report into something a user can act on: a stage says what kind of work
    failed, not whose code was doing it, so ``[pixel/interpret-pixel] data length
    16 != 64`` leaves someone with three codecs installed no way to tell which one
    to look at — and no way at all to tell a plugin of their own from a built-in.
    Rendered ahead of the message rather than inside the bracket, so the
    ``[pathway/stage:action]`` sub-label documented in
    ``docs/design/plugin-system.md`` stays exactly what it was:
    ``[pixel/interpret-pixel] snes-4bpp: data length 16 != 64``. Empty where the
    failure is not any one plugin's.
    """

    def __init__(
        self,
        stage: Stage,
        pathway: Pathway,
        message: str,
        action: str = "",
        *,
        plugin: str = "",
    ) -> None:
        self.stage = stage
        self.pathway = pathway
        self.action = action
        self.plugin = plugin
        self.message = message
        label = f"{stage.value}:{action}" if action else stage.value
        detail = f"{plugin}: {message}" if plugin else message
        super().__init__(f"[{pathway.value}/{label}] {detail}")
