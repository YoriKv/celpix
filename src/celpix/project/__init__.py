"""Resumable project save/load (``docs/design/overview.md`` §7).

A project captures enough to resume a session — the source reference, the active
pipeline for both pathways, palette state, and view options — but not the undo
stack (undo is per-launch, §6).

Intentionally empty for now.
"""
