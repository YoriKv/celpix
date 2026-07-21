"""The strictly linear editing pipeline (``docs/design/overview.md`` §2).

Data flows forward through ordered stages — Read, Decompress, View & Edit,
Compress, Write — with the byte-handling stages running per pathway (pixel and
palette) and converging at the shared View & Edit stage. Failure at any stage is
a hard-stop that surfaces which stage, which pathway, and why.

Intentionally empty for now — stage interfaces land after the foundation.
"""
