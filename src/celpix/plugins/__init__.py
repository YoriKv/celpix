"""Plugin API and registry (``docs/design/overview.md`` §3).

Every stage is an extension point and every concrete behaviour — including the
built-ins — is a plugin on this API: data-first, with code as the escape hatch
for what data cannot express.

The plugin-authoring names are re-exported here so a drop-in file in any folder
can write ``from celpix.plugins import FormatInfo``.
:class:`~celpix.plugins.base.FileRef` is *not* among them: it is the host's own
descriptor of where bytes live, and a container is handed the bytes themselves.
"""

from celpix.plugins.base import (
    PluginInfo,
    ReadSource,
    WriteTarget,
    plain_read,
    splice,
)
from celpix.plugins.formats import FormatInfo, PaletteFormat, PixelFormat

__all__ = [
    "FormatInfo",
    "PaletteFormat",
    "PixelFormat",
    "PluginInfo",
    "ReadSource",
    "WriteTarget",
    "plain_read",
    "splice",
]
