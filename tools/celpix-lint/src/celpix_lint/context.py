"""What every check is handed: the document, the id source, and somewhere to
report to — plus the per-entry reading each check would otherwise redo.

:class:`EntryView` is the important half. It holds both what the file *says* and
what the loader will *make of it* — ``raw_kind`` beside ``kind``, ``raw_content``
beside ``content_kind`` — because almost every finding here is the gap between
the two. A check that only saw the resolved value could not tell a project that
says ``"pixels"`` from one that says ``"pixles"``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from celpix_lint.diagnostics import Diagnostic, Report, Severity
from celpix_lint.document import ProjectDocument
from celpix_lint.known import KnownIds
from celpix_lint.schema import CONTENT_KINDS, KINDS


@dataclass
class EntryView:
    """One entry, as written and as it will be read."""

    index: int
    raw: dict
    #: The ``kind`` string in the file, whatever it is.
    raw_kind: object = None
    #: What the loader resolves that to — an unknown kind reads as ``"file"``.
    kind: str = "file"
    #: The ``content_kind`` string in the file, or None when absent.
    raw_content: object = None
    #: What the loader resolves that to, palette entries forced to ``"palette"``.
    content_kind: str = "pixels"
    name: str = ""
    #: True when the entry is too broken to parse and celPix drops it entirely.
    skipped: bool = False

    @property
    def pointer(self) -> str:
        return f"/entries/{self.index}"

    def at(self, *keys: object) -> str:
        """A JSON Pointer into this entry — ``at("view", "zoom")``."""
        return "/".join([self.pointer, *(str(key) for key in keys)])

    @property
    def is_tilemap(self) -> bool:
        return self.content_kind == "tilemap"


def read_entry(index: int, raw: object) -> EntryView:
    """``raw`` read the way the loader reads it, mistakes preserved."""
    if not isinstance(raw, dict):
        return EntryView(index=index, raw={}, skipped=True)
    raw_kind = raw.get("kind")
    kind = raw_kind if raw_kind in KINDS else "file"
    raw_content = raw.get("content_kind")
    content = raw_content if raw_content in CONTENT_KINDS else "pixels"
    # `Entry.__post_init__`: a palette entry's content kind is derived from its
    # kind, so whatever the file says about it is overwritten rather than read.
    if kind == "palette":
        content = "palette"
    name = raw.get("name")
    path = raw.get("path")
    # An entry with no usable path is skipped by the loader — except a
    # composite, which is alone in having none.
    skipped = kind != "composite" and not (isinstance(path, str) and path)
    label = name if isinstance(name, str) and name else ""
    if not label and isinstance(path, str) and path:
        label = path.rsplit("/", 1)[-1]
    return EntryView(
        index=index,
        raw=raw,
        raw_kind=raw_kind,
        kind=kind,
        raw_content=raw_content,
        content_kind=content,
        name=label,
        skipped=skipped,
    )


def region_size(doc: ProjectDocument, view: EntryView) -> int | None:
    """The joined size of everything ``view``'s bytes come from, or None.

    Offsets under an entry are into the **join**, not into its first file, so a
    region built from several ROM chips has to be measured whole: a slice at
    0x108000 of a two-chip region is in range, and a check against chip one
    alone would call it out of bounds. None when any part of it is missing —
    the reference checks report that, and a partial total would be a lie.
    """
    path = view.raw.get("path")
    if not (isinstance(path, str) and path):
        return None
    total = doc.size_of(path)
    if total is None:
        return None
    for item in view.raw.get("extra_paths") or ():
        if not (isinstance(item, str) and item):
            return None
        size = doc.size_of(item)
        if size is None:
            return None
        total += size
    return total


@dataclass
class Context:
    """Shared state for one file's run."""

    doc: ProjectDocument
    ids: KnownIds
    report: Report
    entries: list = field(default_factory=list)
    #: Set from the CLI — the file checks are the only ones that touch the disk,
    #: and a project whose ROMs are elsewhere should still be lintable.
    check_files: bool = True

    def emit(
        self,
        code: str,
        severity: Severity,
        message: str,
        *,
        pointer: str = "",
        entry: EntryView | None = None,
        detail: str = "",
    ) -> None:
        self.report.add(
            Diagnostic(
                code=code,
                severity=severity,
                message=message,
                pointer=pointer,
                entry=entry.index if entry is not None else None,
                entry_name=entry.name if entry is not None else "",
                detail=detail,
            )
        )

    def error(self, code, message, **kwargs) -> None:
        self.emit(code, Severity.ERROR, message, **kwargs)

    def warn(self, code, message, **kwargs) -> None:
        self.emit(code, Severity.WARNING, message, **kwargs)

    def info(self, code, message, **kwargs) -> None:
        self.emit(code, Severity.INFO, message, **kwargs)
