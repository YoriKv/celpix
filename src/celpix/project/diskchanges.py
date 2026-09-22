"""A file changed on disk under an open entry: noticing it, and reloading
without losing what was edited here.

Another program — an emulator writing its save RAM, an assembler rebuilding
the ROM, a second editor — can rewrite a file while celPix holds a document
read from it. The window then shows bytes the file no longer has, and a Write
would put them back over whatever the other program did. Two Qt-free pieces
answer that, and the window drives them (``ui/main_window/disk_watch.py``):

- :class:`DiskState` remembers what each watched file held when it was last
  read or written *here*, so an outside change is a file whose bytes no longer
  match. A stat at every look and a hash only when the modification time moved:
  a ROM is megabytes, and the question is almost always "did anything happen".
- :func:`merge_bytes` carries the unsaved edits across a reload as a three-way
  merge over bytes. An edit is "every byte that differs from what was read", so
  it needs the buffer *as read* beside the buffer as it stands — which is what
  :attr:`~celpix.core.document.Document.pixel_base_bytes` keeps.

Both stay out of ``ui/`` for the reason everything under ``project/`` does: the
merge is pure arithmetic a script may want, and the fingerprints are a fact
about files, not about widgets (``docs/design/disk-changes.md``).
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from os.path import abspath, normcase

#: The compare granularity of :func:`merge_bytes`. Edits are local — a stroke
#: touches a tile, not a bank — so the buffers are compared a chunk at a time in
#: C and only a chunk that differs is walked byte by byte in Python.
_CHUNK = 256


@dataclass(frozen=True)
class Merge:
    """What :func:`merge_bytes` produced."""

    data: bytes
    #: Bytes the local side had changed and that were carried onto the new
    #: contents — how much unsaved work the reload kept.
    kept: int
    #: Bytes the other program changed *too*: local won there, so these are the
    #: places the user may want to look at.
    conflicts: int
    #: Locally changed bytes past the end of the new contents, which had nowhere
    #: to land — the file shrank under an edit near its end.
    dropped: int


def changed_offsets(base: bytes, local: bytes) -> list[int]:
    """The offsets at which ``local`` differs from ``base``.

    Compared in :data:`_CHUNK` slices so an untouched megabyte costs a few
    thousand C-level comparisons rather than a Python loop over every byte. A
    length difference counts as a change over the tail of the longer one, since
    a byte that exists on one side only has certainly changed.
    """
    out: list[int] = []
    common = min(len(base), len(local))
    for start in range(0, common, _CHUNK):
        end = min(start + _CHUNK, common)
        if base[start:end] != local[start:end]:
            out.extend(i for i in range(start, end) if base[i] != local[i])
    out.extend(range(common, max(len(base), len(local))))
    return out


def merge_bytes(base: bytes, local: bytes, disk: bytes) -> Merge:
    """``disk`` with every byte the local side changed since ``base`` put over it.

    The three-way merge a reload runs: ``base`` is the buffer as it was read (or
    last written) here, ``local`` the same buffer with unsaved edits in it, and
    ``disk`` what the file holds now. Wherever ``local`` differs from ``base``
    the edit wins — that is the whole point of carrying it — and everywhere else
    the new contents stand. A byte both sides changed is a conflict; local still
    wins, because a silent revert of the user's own edit is the one outcome a
    reload exists to prevent, and the count comes back so it can be said.

    Offsets are absolute in the buffer, which is the only coordinate an edit
    has: a splice at byte 4096 stays at byte 4096 whatever else moved. An edit
    past the end of a ``disk`` that shrank is dropped and counted.
    """
    out = bytearray(disk)
    kept = conflicts = dropped = 0
    for at in changed_offsets(base, local):
        if at >= len(local):
            # The local buffer is shorter than the base: nothing to carry there.
            continue
        if at >= len(out):
            dropped += 1
            continue
        value = local[at]
        was = base[at] if at < len(base) else None
        if out[at] != was and out[at] != value:
            conflicts += 1
        out[at] = value
        kept += 1
    return Merge(bytes(out), kept, conflicts, dropped)


def carry_color_edits(
    colors: list[int], edited: list[int], edits: set[int]
) -> list[int]:
    """``colors`` with the entries in ``edits`` taken from ``edited`` instead.

    The palette's version of :func:`merge_bytes`, and simpler because a palette
    already records *which* entries were edited
    (:attr:`~celpix.core.document.Document.palette_edits`) — a save splices
    exactly those into the file's own bytes, so a reload carries exactly those
    over the file's new colours. An edited index past the end of the new palette
    is dropped, as an edit past the end of a shrunk file is.
    """
    out = list(colors)
    for index in edits:
        if 0 <= index < len(out) and index < len(edited):
            out[index] = edited[index]
    return out


Fingerprint = int


def fingerprint(path: str) -> Fingerprint | None:
    """``path``'s modification time in nanoseconds, or None while it cannot be
    stat'd.

    The cheap look. What it says is only that the file was *touched* — whether
    the bytes changed is :func:`digest`'s answer, asked once per touch, so the
    stat need carry nothing more. None during a rename-based save, when the
    path is momentarily gone; the next look finds the replacement.
    """
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


def digest(path: str) -> bytes | None:
    """A hash of ``path``'s bytes, or None while it cannot be read."""
    try:
        with open(path, "rb") as handle:
            return hashlib.sha1(handle.read()).digest()
    except OSError:
        return None


@dataclass
class _Watched:
    """One file: what this program last read or wrote, and what the disk has
    been seen to hold since."""

    path: str
    #: The bytes as this program last read or wrote them.
    loaded: bytes | None
    #: The bytes on disk whose reload was declined, so the same state is not
    #: asked about twice; None when nothing was declined since the last load.
    declined: bytes | None = None
    #: The modification time ``seen`` was hashed at: the disk is re-hashed only
    #: when it moves, so a look that finds it where it was costs a stat.
    stamp: Fingerprint | None = None
    seen: bytes | None = None


@dataclass
class DiskState:
    """What each watched file held the last time this program read or wrote
    it — so a look at the disk can say which ones someone else changed.

    **Bytes, not timestamps, decide.** The modification time says the file was
    touched; only the hash says it changed, and the two differ exactly where a
    prompt would be wrong: a tool that rewrote the same bytes, a timestamp
    restored by a checkout. The hash is taken once per touch, so a look at a
    file that sat still costs a stat.

    Paths are keyed the way the workspace keys them, so a file opened under two
    spellings is one file here as well.
    """

    _files: dict[str, _Watched] = field(default_factory=dict)

    @staticmethod
    def _key(path: str) -> str:
        return normcase(abspath(path))

    def _read(self, path: str) -> _Watched:
        stamp = fingerprint(path)
        held = digest(path) if stamp is not None else None
        return _Watched(path, held, stamp=stamp, seen=held)

    def track(self, paths: list[str] | tuple[str, ...]) -> None:
        """Start watching ``paths`` that are not watched yet, as they are now.

        A path already tracked is left alone: this is called for every entry as
        it appears, and a second entry on a file the first is already watching
        must not reset what "unchanged" means for it.
        """
        for path in paths:
            if not path:
                continue
            key = self._key(path)
            if key not in self._files:
                self._files[key] = self._read(path)

    def refresh(self, paths: list[str] | tuple[str, ...]) -> None:
        """Re-take ``paths`` as what this program holds — after it wrote or
        re-read them, so its own write is not later reported as someone else's,
        and a decline that stood against the old bytes is over. Only paths
        already tracked; an export to a file nobody has open is not one to
        start watching."""
        for path in paths:
            key = self._key(path)
            if key in self._files:
                self._files[key] = self._read(path)

    def decline(self, paths: list[str] | tuple[str, ...]) -> None:
        """Remember that a reload of ``paths`` as they are now was declined:
        they are not offered again until the bytes move on, and what was
        *loaded* is still what it was, so an outright reload still has
        something to do."""
        for path in paths:
            watched = self._files.get(self._key(path))
            if watched is not None:
                watched.declined = self._current(watched)

    def retain(self, paths: list[str] | tuple[str, ...]) -> None:
        """Stop watching everything but ``paths`` — the workspace's current set."""
        keep = {self._key(p) for p in paths if p}
        for key in list(self._files):
            if key not in keep:
                del self._files[key]

    def paths(self) -> list[str]:
        """Every path being watched, as first given."""
        return [w.path for w in self._files.values()]

    @staticmethod
    def _current(watched: _Watched) -> bytes | None:
        """What the disk holds now — re-hashed only when the stat has moved."""
        stamp = fingerprint(watched.path)
        if stamp is None:
            return None
        if stamp != watched.stamp:
            watched.stamp, watched.seen = stamp, digest(watched.path)
        return watched.seen

    def changed(self) -> list[str]:
        """The watched files that hold other bytes than this program last read
        or wrote — and than any reload it declined.

        Only files that can be read right now: one that is missing is either
        mid-rename or deleted, and neither is something to reload. A file that
        was missing when first tracked and has since appeared is a change — a
        build that produced the ROM the project was waiting for.
        """
        out: list[str] = []
        for watched in self._files.values():
            now = self._current(watched)
            if now is None or now == watched.loaded or now == watched.declined:
                continue
            out.append(watched.path)
        return out

    def is_current(self, paths: list[str] | tuple[str, ...]) -> bool:
        """Do ``paths`` hold exactly what this program last read or wrote? A
        path it does not watch counts as current: there is nothing to compare."""
        for path in paths:
            watched = self._files.get(self._key(path))
            if watched is not None and self._current(watched) != watched.loaded:
                return False
        return True
