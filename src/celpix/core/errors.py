"""Pipeline failure types.

The pipeline **hard-stops** at the first stage that cannot proceed and surfaces
*which stage, which pathway, and why* — it never degrades, guesses, or writes
partial output (see ``docs/design/overview.md`` §2, Failure handling). A
:class:`PipelineError` carries exactly that context so the UI can report it.
"""

from __future__ import annotations

from enum import Enum


class Stage(str, Enum):
    """The pipeline stages, in forward order. String-valued for readable ids."""

    READ = "read"
    DECOMPRESS = "decompress"
    INTERPRET_PIXEL = "interpret-pixel"
    INTERPRET_PALETTE = "interpret-palette"
    COMPRESS = "compress"
    WRITE = "write"


class Pathway(str, Enum):
    """The two parallel pathways data flows along (overview.md §2)."""

    PIXEL = "pixel"
    PALETTE = "palette"


class PipelineError(Exception):
    """A stage could not proceed; the pipeline halts and reports this.

    Attributes mirror what the user needs to fix the configuration and retry.
    """

    def __init__(self, stage: Stage, pathway: Pathway, message: str) -> None:
        self.stage = stage
        self.pathway = pathway
        self.message = message
        super().__init__(f"[{pathway.value}/{stage.value}] {message}")
