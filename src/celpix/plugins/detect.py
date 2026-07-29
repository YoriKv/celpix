"""Pick a container for a file from its name and its leading bytes.

Every container declares what it recognises — suffixes and/or magic bytes on its
:class:`~celpix.plugins.base.PluginInfo` — and this module answers which one
claims a file, falling back to plain bytes when none does. Detection only decides
where a file starts out; the user can override it afterwards.

Matching is a byte comparison and a suffix test, with no plugin code executed: it
runs across **every** registered container, including untrusted user ones, before
the file is open. A container claims files by describing itself, not by
inspecting them, which is why a signature is static data rather than a
``sniff(head)`` hook. Qt-free.
"""

from __future__ import annotations

import os
from pathlib import Path

from celpix.core.capabilities import ContentKind
from celpix.core.errors import Stage
from celpix.plugins.base import RAW_CONTAINER, PluginInfo
from celpix.plugins.registry import Registry

# How much of a file detection looks at. Comfortably past every signature we
# carry while staying one cheap read on a file that may be tens of megabytes.
#
# The deepest is an 8bpp tile bank's, at 0x10000: that family writes its
# metadata block *after* the payload, so "the signature is near the front" does
# not hold for every format
# (``docs/graphics-formats-reference/scgcad-formats.md`` §1). A trailing
# signature is only findable this way — seeking from the end would need a
# per-container rule about where from. One 64 KiB read per file *opened* is not
# a cost worth optimising against a format going undetected.
SIGNATURE_HEAD = 0x10100


def head_and_size(path: str, size: int = SIGNATURE_HEAD) -> tuple[bytes, int]:
    """The first ``size`` bytes of ``path`` and its byte length.

    Both come from one ``open``: detection needs the pair, and on a network or
    mounted drive the round trip costs far more than the read. An unreadable file
    is not an error — detection finds nothing, the caller lands on plain bytes,
    and the load that follows reports the real failure. The length is -1 rather
    than 0 when it can't be read, so a missing file fails a ``size_modulo`` test
    instead of satisfying ``size % m == 0`` by accident.
    """
    try:
        with Path(path).open("rb") as handle:
            return handle.read(size), os.fstat(handle.fileno()).st_size
    except OSError:
        return b"", -1


def frames(info: PluginInfo, kind: ContentKind) -> bool:
    """Whether ``info``'s container is one an entry holding ``kind`` can use.

    The one place the declaration is read, so listing containers and detecting
    one cannot disagree about which are eligible — a container the dialog offers
    for a palette must be one detection would also pick for it.
    """
    return kind in info.content_kinds


def containers_for(registry: Registry, kind: ContentKind) -> list[PluginInfo]:
    """Every registered container an entry holding ``kind`` may be read through,
    in registration order — built-ins first, as everywhere else."""
    return [
        plugin.info
        for plugin in registry.plugins(Stage.CONTAINER)
        if frames(plugin.info, kind)
    ]


def _score(info: PluginInfo, path: str, head: bytes, size: int) -> int:
    """How strongly ``info`` claims this file: 2 magic, 1 suffix, 0 not at all."""
    # Narrowing terms, never a claim of their own: they fail the whole match
    # rather than contribute to it, so declaring one only makes a container more
    # selective.
    if info.exact_size and size != info.exact_size:
        return 0
    if info.size_modulo is not None or info.min_size:
        if size < info.min_size:
            return 0
        if info.size_modulo is not None:
            modulus, remainder = info.size_modulo
            if size % modulus != remainder:
                return 0
    if info.magic:
        # Magic is an assertion about the format, so it decides on its own —
        # a matching suffix cannot rescue a container whose bytes disagree.
        return 2 if any(head[at : at + len(m)] == m for at, m in info.magic) else 0
    lowered = path.lower()
    return 1 if any(lowered.endswith(ext) for ext in info.extensions) else 0


def detect_container(
    registry: Registry,
    path: str,
    head: bytes | None = None,
    size: int | None = None,
    kind: ContentKind = ContentKind.PIXELS,
) -> str:
    """The id of the container that best claims ``path`` for a ``kind`` entry.

    ``head`` is the file's leading bytes and ``size`` its length; each is read
    from disk when not supplied. Magic beats a bare suffix, and registration order
    breaks a tie — built-ins register first, so a user plugin never silently
    displaces one on an equal claim. :data:`~celpix.plugins.base.RAW_CONTAINER`
    when nothing claims the file, the answer for every plain binary.

    ``kind`` restricts the field to containers that frame that kind of entry
    (:func:`frames`), so a palette is never claimed by a format that unwraps
    graphics or the reverse. It defaults to pixels because that is what opening a
    file means unless something says otherwise — and because a tilemap container
    frames pixels too, the tilemap-ness of its payload being a separate question
    the container answers with ``default_tilemap_preset``.
    """
    if head is None or size is None:
        read_head, read_size = head_and_size(path)
        head = read_head if head is None else head
        size = read_size if size is None else size
    best_id, best_score = RAW_CONTAINER, 0
    for plugin in registry.plugins(Stage.CONTAINER):
        if not frames(plugin.info, kind):
            continue
        score = _score(plugin.info, path, head, size)
        if score > best_score:
            best_id, best_score = plugin.info.id, score
    return best_id


def tilemap_preset_for(registry: Registry, container_id: str) -> str:
    """The cell codec ``container_id``'s payload should first be read with, or ``""``.

    A container that unwraps to a tilemap says so by carrying a
    ``default_tilemap_preset``, and that declaration does double duty: it is
    what marks an opened file as tilemap content (:func:`content_kind_for`) and
    what the entry's cell codec starts at, since only the container knows which
    of the byte orders its format uses. Empty for every ordinary container,
    which is the answer for a file of pixels.
    """
    try:
        plugin = registry.plugin(Stage.CONTAINER, container_id)
    except KeyError:
        return ""
    return str(getattr(plugin, "default_tilemap_preset", "") or "")


def content_kind_for(registry: Registry, container_id: str) -> ContentKind:
    """What a file read through ``container_id`` holds.

    Derived from the container rather than asked of the user, because the
    container was itself chosen from the file's own signature: a file that
    unwraps through a tilemap container *is* a tilemap, and having it land in
    the wrong section of the list would be a worse first impression than any
    dialog. Everything else is pixels — the answer for a plain binary, and for
    a container the registry no longer has.
    """
    if tilemap_preset_for(registry, container_id):
        return ContentKind.TILEMAP
    return ContentKind.PIXELS


def container_write_enabled(registry: Registry, container_id: str) -> bool:
    """Whether bytes read through ``container_id`` may be written back at all.

    A container with no ``write`` of its own is view-only, and one the registry no
    longer has degrades to the same
    (:meth:`~celpix.plugins.registry.Registry.resolve_stage`). The rule and its
    reasoning live on :class:`~celpix.plugins.base.ContainerPlugin`.
    """
    return registry.resolve_stage(Stage.CONTAINER, container_id)[1]


def container_label(
    registry: Registry, container_id: str, *, short: bool = True
) -> str:
    """A tag for ``container_id``, or ``""`` when there is nothing to say.

    Empty for plain bytes, since most files would gain only noise from being told
    they are unwrapped by nothing, and for an id the registry no longer has, since
    naming an absent container would claim the file is being read through it.
    ``short`` picks the compact form for a list column over the full name a
    tooltip has room for.
    """
    if container_id == RAW_CONTAINER:
        return ""
    try:
        info = registry.plugin(Stage.CONTAINER, container_id).info
    except KeyError:
        return ""
    return (info.short_name or info.name) if short else info.name


def resolved_container_id(registry: Registry, container_id: str) -> str:
    """``container_id`` if the registry still has it, plain bytes if it does not.

    The container half of :meth:`~celpix.plugins.registry.Registry.resolve_stage`,
    named for the question its callers are asking.
    """
    return registry.resolve_stage(Stage.CONTAINER, container_id)[0]
