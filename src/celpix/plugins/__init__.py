"""Plugin API and registry (``docs/design/overview.md`` §3).

Every stage is an extension point and every concrete behavior — including the
built-ins — is a plugin on this API; there is no privileged core path. The
direction is data-first plugins, with code as the escape hatch for what data
cannot express.

The plugin-authoring names are re-exported here so a drop-in plugin file can
write ``from celpix.plugins import FormatInfo`` — for every folder, not just
``pixel/`` and ``palette/``: a compression or container plugin needs
:class:`~celpix.plugins.base.PluginInfo`, the container types and
:func:`~celpix.plugins.base.splice`, and has the same claim on a one-line import.

:class:`~celpix.plugins.base.FileRef` is deliberately *not* here: it is the
host's own descriptor of where bytes live, and a container is handed the bytes
themselves.
"""

from celpix.plugins.base import PluginInfo, ReadSource, WriteTarget, splice
from celpix.plugins.formats import FormatInfo, PaletteFormat, PixelFormat

__all__ = [
    "FormatInfo",
    "PaletteFormat",
    "PixelFormat",
    "PluginInfo",
    "ReadSource",
    "WriteTarget",
    "splice",
]
