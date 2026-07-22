"""Resumable project save/load (``docs/design/overview.md`` §7).

A project captures enough to resume a session — the source reference, the active
pipeline for both pathways, palette state, and view options — but not the undo
stack (undo is per-launch, §6).

:mod:`celpix.project.workspace` models the session's open files/slices;
:mod:`celpix.project.projectfile` serializes that workspace to and from the
``.celpix`` JSON project file (``docs/design/project-format.md``). Both are
Qt-free.
"""
