"""Plugin API and registry (``docs/design/overview.md`` §3).

Every stage is an extension point and every concrete behavior — including the
built-ins — is a plugin on this API; there is no privileged core path. The
direction is data-first plugins, with code as the escape hatch for what data
cannot express.

Intentionally empty for now — the plugin contract lands after the foundation.
"""
