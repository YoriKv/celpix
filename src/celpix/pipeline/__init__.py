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

Three modules answer what is asked *around* a run rather than by one, and
:mod:`~celpix.pipeline.pipeline` re-exports all three so a caller has a single
import: :mod:`~celpix.pipeline.render` lays decoded data out as a picture,
:mod:`~celpix.pipeline.inspection` reports what one container made of one file, and
:mod:`~celpix.pipeline.metrics` puts scalar questions to a resolved codec. Their
shared machinery — running one stage, acquiring a source, resolving tile
geometry — is :mod:`~celpix.pipeline._stage`.
"""
