"""The strictly linear editing pipeline (``docs/design/overview.md`` §2).

Data flows forward through ordered stages — container read, reshape, decompress,
interpret — with the byte-handling stages running per pathway (pixel and palette)
and converging on the document the editor works in. Saving runs the mirror image.
Failure at any stage is a hard-stop that surfaces which stage, which pathway, and
why.

:mod:`~celpix.pipeline.pathway` holds one pathway's configuration,
:mod:`~celpix.pipeline.pipeline` runs the stages in both directions, and
:mod:`~celpix.pipeline.importer` fits external pixels (a paste, a PNG) to a
document's format on the way in.
"""
