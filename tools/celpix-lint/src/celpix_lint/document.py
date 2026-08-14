"""Reading the project file, and resolving the paths inside it.

Path handling mirrors ``docs/design/project-format.md`` §3 exactly, and it has
to: a project written on Windows is opened under WSL from the same checkout, so
a linter that resolved case-sensitively would report every reference in it as
missing. The walk below is celPix's own ``_resolve_path`` / ``_match_case``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from os import listdir, stat
from os.path import (
    abspath,
    dirname,
    exists,
    isabs,
    isdir,
    join,
    normcase,
    normpath,
    split,
)

from celpix_lint.diagnostics import Diagnostic, Severity


@dataclass
class ProjectDocument:
    """One parsed `.celpix`, plus what its paths resolve to on this machine."""

    path: str
    base_dir: str
    data: dict
    #: Resolved absolute path per stored string, and size per resolved path.
    #: Cached because a 455-entry project asks about the same ROM hundreds of
    #: times and each miss is a directory walk.
    _resolved: dict = field(default_factory=dict, repr=False)
    _sizes: dict = field(default_factory=dict, repr=False)

    @property
    def entries(self) -> list:
        raw = self.data.get("entries")
        return raw if isinstance(raw, list) else []

    def resolve(self, stored: str) -> str:
        """A stored path as celPix would resolve it, case differences tolerated.

        A path that resolves nowhere comes back as its literal self, which is
        what the entry would carry — and what the missing-file check reports.
        """
        if stored not in self._resolved:
            self._resolved[stored] = _resolve_path(stored, self.base_dir)
        return self._resolved[stored]

    def size_of(self, stored: str) -> int | None:
        """The referenced file's size in bytes, or None if it is not a file."""
        resolved = self.resolve(stored)
        if resolved not in self._sizes:
            try:
                info = stat(resolved)
            except OSError:
                self._sizes[resolved] = None
            else:
                self._sizes[resolved] = None if isdir(resolved) else info.st_size
        return self._sizes[resolved]

    def exists(self, stored: str) -> bool:
        return exists(self.resolve(stored))

    def is_dir(self, stored: str) -> bool:
        return isdir(self.resolve(stored))

    def identity(self, stored: str) -> str:
        """The key two entries referencing the same file agree on.

        ``normcase`` is the workspace's own identity for a path, so "the same
        file opened twice" means here what it means in the editor.
        """
        return normcase(self.resolve(stored))


def load(path: str) -> tuple[ProjectDocument | None, list[Diagnostic]]:
    """Parse ``path``, or return the one fatal diagnostic that stopped it.

    The four failures here are the only ones celPix itself raises on — every
    other problem in the file degrades an entry rather than the load — so they
    are the only ones that end the run for a file.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except OSError as exc:
        return None, [
            Diagnostic(
                "F001",
                Severity.ERROR,
                f"cannot read the file: {exc.strerror or exc}",
                detail="celPix would refuse to open this project.",
            )
        ]
    except UnicodeDecodeError as exc:
        return None, [
            Diagnostic(
                "F002",
                Severity.ERROR,
                f"not valid UTF-8: {exc}",
                detail="A project file is a UTF-8 JSON document.",
            )
        ]
    except json.JSONDecodeError as exc:
        return None, [
            Diagnostic(
                "F002",
                Severity.ERROR,
                f"not valid JSON: {exc.msg} (line {exc.lineno}, column {exc.colno})",
                detail="celPix would refuse to open this project.",
            )
        ]
    if not isinstance(data, dict):
        return None, [
            Diagnostic(
                "F003",
                Severity.ERROR,
                f"the document is a JSON {type(data).__name__}, not an object",
                detail="A project is a single JSON object with an `entries` array.",
            )
        ]
    if "entries" in data and not isinstance(data["entries"], list):
        return None, [
            Diagnostic(
                "F004",
                Severity.ERROR,
                "`entries` is not an array",
                pointer="/entries",
                detail="celPix would refuse to open this project.",
            )
        ]
    return ProjectDocument(path=path, base_dir=dirname(abspath(path)), data=data), []


# -- celPix's own path resolution (project-format.md §3) -------------------
def _resolve_path(stored: str, base_dir: str) -> str:
    path = stored if isabs(stored) else join(base_dir, stored)
    path = normpath(path)
    return path if exists(path) else _match_case(path)


def _match_case(path: str) -> str:
    # Walk up to the deepest existing ancestor, then re-descend matching each
    # missing segment case-insensitively against the real directory listing.
    head, missing = path, []
    while not exists(head):
        head, tail = split(head)
        if not tail:
            return path
        missing.append(tail)
    for segment in reversed(missing):
        candidate = join(head, segment)
        if not exists(candidate):
            try:
                names = listdir(head)
            except OSError:
                return path
            fold = segment.casefold()
            match = next((n for n in names if n.casefold() == fold), None)
            if match is None:
                return path
            candidate = join(head, match)
        head = candidate
    return head
