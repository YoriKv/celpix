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
    other's inverse — splitting them into separate registrations only ever meant
    two descriptors and an id convention holding the pair together.

    Which direction a load or save is going is not lost by that: it is the
    :attr:`PipelineError.action` sub-label, reported when something fails.

    String-valued for readable ids.
    """

    CONTAINER = "container"
    RESHAPE = "reshape"
    COMPRESSION = "compression"
    INTERPRET_PIXEL = "interpret-pixel"
    INTERPRET_PALETTE = "interpret-palette"


class Pathway(str, Enum):
    """The two parallel pathways data flows along (overview.md §2)."""

    PIXEL = "pixel"
    PALETTE = "palette"


class PipelineError(Exception):
    """A stage could not proceed; the pipeline halts and reports this.

    Attributes mirror what the user needs to fix the configuration and retry.

    ``action`` names the *direction* within the stage — ``read``/``write`` for a
    container, ``reshape``/``unshape`` for a reshape,
    ``decompress``/``compress`` for a compression scheme. A stage now
    spans both, and "the container failed" is a materially different report from
    "the container failed **while saving**", so the message keeps it:
    ``[pixel/container:write] …``. Empty for a stage where there is nothing to
    disambiguate.
    """

    def __init__(
        self, stage: Stage, pathway: Pathway, message: str, action: str = ""
    ) -> None:
        self.stage = stage
        self.pathway = pathway
        self.action = action
        self.message = message
        label = f"{stage.value}:{action}" if action else stage.value
        super().__init__(f"[{pathway.value}/{label}] {message}")
