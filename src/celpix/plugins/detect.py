"""Pick a container for a file from its name and its leading bytes.

Opening a file should not start with a format interrogation. Every Read plugin
declares what it recognises — suffixes and/or magic bytes, on its
:class:`~celpix.plugins.base.PluginInfo` — and this module walks the registered
containers and answers *which one claims this file*, falling back to plain bytes
when none does. The user can always override the answer afterwards; detection
only decides where the file starts out.

Matching is deliberately dumb: a byte comparison and a suffix test, no plugin
code executed. Detection runs across **every** registered container before the
file is open, including untrusted user ones, so it must not be a place a plugin
gets to run — a container claims files by describing itself, not by inspecting
them. That is also why a signature is static data on the descriptor rather than
a ``sniff(head)`` hook.

Qt-free, like everything under ``plugins``.
"""

from __future__ import annotations

from pathlib import Path

from celpix.core.errors import Stage
from celpix.plugins.base import RAW_READ, RAW_WRITE, PluginInfo
from celpix.plugins.registry import Registry

# How much of a file detection looks at. Comfortably past every signature we
# carry (the Game Boy logo at 0x104 is the deepest) while staying one cheap read
# on a file that may be tens of megabytes.
SIGNATURE_HEAD = 4096


def signature_head(path: str, size: int = SIGNATURE_HEAD) -> bytes:
    """The first ``size`` bytes of ``path``, or ``b""`` if it can't be read.

    An unreadable file is not an error here — detection simply finds nothing and
    the caller lands on plain bytes, leaving the real failure to be reported by
    the load that follows.
    """
    try:
        with Path(path).open("rb") as handle:
            return handle.read(size)
    except OSError:
        return b""


def _score(info: PluginInfo, path: str, head: bytes) -> int:
    """How strongly ``info`` claims this file: 2 magic, 1 suffix, 0 not at all."""
    if info.magic:
        # Magic is an assertion about the format, so it decides on its own —
        # a matching suffix cannot rescue a container whose bytes disagree.
        return 2 if any(head[at : at + len(m)] == m for at, m in info.magic) else 0
    lowered = path.lower()
    return 1 if any(lowered.endswith(ext) for ext in info.extensions) else 0


def detect_container(registry: Registry, path: str, head: bytes | None = None) -> str:
    """The id of the Read plugin that best claims ``path``.

    ``head`` is the file's leading bytes; it is read from disk when not supplied.
    Magic beats a bare suffix, and registration order breaks a tie — built-ins
    register first, so a user plugin never silently displaces one on an equal
    claim. :data:`~celpix.plugins.base.RAW_READ` when nothing claims the file,
    which is the answer for most files and every plain binary.
    """
    if head is None:
        head = signature_head(path)
    best_id, best_score = RAW_READ, 0
    for plugin in registry.plugins(Stage.READ):
        score = _score(plugin.info, path, head)
        if score > best_score:
            best_id, best_score = plugin.info.id, score
    return best_id


def container_write_id(registry: Registry, read_id: str) -> str:
    """The Write half paired with ``read_id`` by the ``read.X`` ⇄ ``write.X`` rule.

    Falls back to :data:`~celpix.plugins.base.RAW_WRITE` for a read-only
    container (one that ships no writer): its bytes were still acquired through
    the container, but putting them back is then a plain file write rather than
    a re-wrap. A container whose framing cannot be reproduced should ship a
    writer that raises rather than rely on this.
    """
    write_id = read_id.replace("read.", "write.", 1)
    try:
        registry.plugin(Stage.WRITE, write_id)
    except KeyError:
        return RAW_WRITE
    return write_id


def container_label(registry: Registry, container_id: str) -> str:
    """A short tag for ``container_id``, or ``""`` when there is nothing to say.

    Empty for plain bytes — the overwhelming majority of files, which would gain
    only noise from being told they are unwrapped by nothing — and for an id the
    registry no longer has, since naming a container that isn't there would claim
    the file is being read through it.
    """
    if container_id == RAW_READ:
        return ""
    try:
        info = registry.plugin(Stage.READ, container_id).info
    except KeyError:
        return ""
    return info.short_name or info.name


def container_ids(registry: Registry, container_id: str) -> tuple[str, str]:
    """``(read_id, write_id)`` for ``container_id``, degrading to plain bytes.

    A container id outlives the plugin that provided it: a project names the
    container its files were opened with, and the plugin behind it can be
    uninstalled, renamed, or simply left untrusted at the next launch. Opening
    that project should show the file as raw bytes with the container reported
    missing, not fail the load outright — so an unregistered id resolves to
    :data:`~celpix.plugins.base.RAW_READ` here rather than raising downstream.
    """
    try:
        registry.plugin(Stage.READ, container_id)
    except KeyError:
        return RAW_READ, RAW_WRITE
    return container_id, container_write_id(registry, container_id)
