"""Stage-agnostic data model shared by every pipeline stage.

Home for the byte-buffer / index-grid / palette model and the forward-flowing,
advisory context/hints bag (``docs/design/overview.md`` §5). The host owns this
machinery so that plugins can stay thin (§3).

Intentionally empty for now — the foundation stage only wires up the app shell.
"""
