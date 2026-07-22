"""The ``.celpix`` project file: save/load the workspace as JSON.

A project stores **references and settings, never the edited bytes** — the open
entries (files and slices), each one's session settings and view state, and
where its palette comes from (``docs/design/project-format.md``). Writers emit
the current schema ``version``; readers are tolerant — unknown keys are
ignored, missing optional keys get defaults, and a broken *entry* degrades that
entry, never the whole load. Plain ``json`` + dataclass mapping, no pickle: a
shared project file is untrusted input and must never execute code.

Loading yields ready-to-adopt :class:`~celpix.project.workspace.Entry` objects
with their documents unloaded (lazy, as in a live session); view/palette state
rides on the entries' pending fields until first activation. The UI applies
the result with :meth:`~celpix.project.workspace.Workspace.replace`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from os import listdir
from os.path import (
    abspath,
    basename,
    dirname,
    exists,
    isabs,
    join,
    normpath,
    relpath,
    sep,
    split,
)

from celpix.core.document import ViewOptions
from celpix.project.workspace import (
    Entry,
    EntryKind,
    EntrySession,
    PaletteSource,
    Workspace,
)

PROJECT_VERSION = 1
PROJECT_EXTENSION = ".celpix"

# Fallbacks for a hand-authored project that omits preset ids entirely — the
# same built-ins a fresh window starts on, so a minimal project still renders.
_DEFAULT_PIXEL_PRESET = "preset.pixel.snes-4bpp"
_DEFAULT_PALETTE_PRESET = "preset.palette.bgr555"

_PALETTE_MODES = ("default", "file", "offset")


class ProjectError(Exception):
    """The project file itself is unreadable (I/O, syntax, wrong shape).

    Per-entry problems never raise this — a broken entry is skipped or
    degraded so the rest of the project still loads.
    """


@dataclass
class LoadedProject:
    """A parsed project: adoptable entries plus what the reader saw.

    ``version`` is the file's own claim — the UI compares it against
    :data:`PROJECT_VERSION` to warn that saving a newer file will rewrite it
    at this version.
    """

    version: int
    entries: list[Entry]
    current: Entry | None


# -- saving ----------------------------------------------------------------
def save_project(ws: Workspace, path: str) -> None:
    """Serialize ``ws`` to ``path`` as a version-stamped ``.celpix`` document."""
    base_dir = dirname(abspath(path))
    document = {
        "version": PROJECT_VERSION,
        "current": ws.entries.index(ws.current) if ws.current is not None else None,
        "entries": [_entry_dict(entry, base_dir) for entry in ws.entries],
    }
    # LF + trailing newline: projects are meant to live in version control.
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(document, handle, indent=2)
        handle.write("\n")


def _entry_dict(entry: Entry, base_dir: str) -> dict[str, object]:
    data: dict[str, object] = {
        "kind": "slice" if entry.kind is EntryKind.SLICE else "file",
        "name": entry.name,
        "path": _store_path(entry.path, base_dir),
    }
    if entry.kind is EntryKind.SLICE:
        data["slice_offset"] = entry.slice_offset
        data["slice_length"] = entry.slice_length
        data["decompress_id"] = entry.decompress_id
    session = entry.session
    if session is not None:
        data["session"] = {
            "pixel_preset_id": session.pixel_preset_id,
            "palette_preset_id": session.palette_preset_id,
            "palette_mode": session.palette_mode,
            "compression_id": session.compression_id,
            "headered": session.headered,
            "header_length": session.header_length,
            "selected_tile": session.selected_tile,
            "selected_last": session.selected_last,
        }
    # A loaded document carries the live state; a never-activated entry may
    # still hold state a previous load restored into its pending fields.
    view = entry.doc.view if entry.doc is not None else entry.pending_view
    if view is not None:
        data["view"] = {
            "columns": view.columns,
            "rows": view.rows,
            "zoom": view.zoom,
            "show_grid": view.show_grid,
            "subpalette_row": view.subpalette_row,
            "offset": view.offset,
            "byte_nudge": view.byte_nudge,
        }
    palette = _palette_state(entry)
    if palette is not None:
        data["palette"] = _palette_dict(palette, base_dir)
    return data


def _palette_state(entry: Entry) -> PaletteSource | None:
    """The entry's palette source, in project form — ``None`` for the default.

    Derived from the live document when there is one (its palette config is
    the truth for file/offset modes); otherwise whatever a previous project
    load left pending.
    """
    if entry.doc is None or entry.session is None:
        return entry.pending_palette
    mode = entry.session.palette_mode
    source = entry.doc.palette_config.source
    if mode == "file":
        return PaletteSource(path=source.path, offset=source.offset)
    if mode == "offset":
        return PaletteSource(offset=source.offset)
    return None


def _palette_dict(palette: PaletteSource, base_dir: str) -> dict[str, object]:
    if palette.colors is not None:
        return {"colors": [f"#{color & 0xFFFFFFFF:08X}" for color in palette.colors]}
    if palette.path is not None:
        return {"path": _store_path(palette.path, base_dir), "offset": palette.offset}
    return {"offset": palette.offset}


# -- loading ---------------------------------------------------------------
def load_project(path: str) -> LoadedProject:
    """Parse ``path`` into adoptable entries; :class:`ProjectError` if the
    file itself can't be read as a project."""
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except OSError as exc:
        raise ProjectError(f"Cannot read {path}: {exc}") from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ProjectError(f"Not a Celpix project: {path} ({exc})") from exc
    if not isinstance(data, dict) or not isinstance(data.get("entries", []), list):
        raise ProjectError(f"Not a Celpix project: {path}")

    base_dir = dirname(abspath(path))
    # Parse positionally (None for a skipped entry) so the stored `current`
    # index still names the right entry when earlier ones were dropped.
    parsed: list[Entry | None] = []
    for raw in data.get("entries", []):
        try:
            parsed.append(_entry_from_dict(raw, base_dir))
        except Exception:  # noqa: BLE001 — a garbage entry degrades, never aborts
            parsed.append(None)
    index = data.get("current")
    current = (
        parsed[index]
        if isinstance(index, int)
        and not isinstance(index, bool)
        and 0 <= index < len(parsed)
        else None
    )
    return LoadedProject(
        version=_int(data.get("version"), 1),
        entries=[entry for entry in parsed if entry is not None],
        current=current,
    )


def _entry_from_dict(raw: dict[str, object], base_dir: str) -> Entry:
    path = raw["path"]  # type: ignore[index] — non-dict/missing raises: entry skipped
    if not isinstance(path, str) or not path:
        raise ValueError("entry has no usable path")
    path = _resolve_path(path, base_dir)
    name = raw.get("name")
    return Entry(
        name=name if isinstance(name, str) and name else basename(path),
        kind=EntryKind.SLICE if raw.get("kind") == "slice" else EntryKind.FILE,
        path=path,
        slice_offset=_int(raw.get("slice_offset"), 0),
        slice_length=_int(raw.get("slice_length"), None),
        decompress_id=_str(raw.get("decompress_id"), "decompress.none"),
        session=_session_from(raw.get("session")),
        pending_view=_view_from(raw.get("view")),
        pending_palette=_palette_from(raw.get("palette"), base_dir),
    )


def _session_from(raw: object) -> EntrySession:
    data = raw if isinstance(raw, dict) else {}
    mode = data.get("palette_mode")
    return EntrySession(
        pixel_preset_id=_str(data.get("pixel_preset_id"), _DEFAULT_PIXEL_PRESET),
        palette_preset_id=_str(data.get("palette_preset_id"), _DEFAULT_PALETTE_PRESET),
        palette_mode=mode if mode in _PALETTE_MODES else "default",
        compression_id=_str(data.get("compression_id"), "decompress.none"),
        headered=bool(data.get("headered", False)),
        header_length=_int(data.get("header_length"), 512),
        selected_tile=_int(data.get("selected_tile"), None),
        selected_last=_int(data.get("selected_last"), None),
    )


def _view_from(raw: object) -> ViewOptions | None:
    if not isinstance(raw, dict):
        return None
    defaults = ViewOptions()
    return ViewOptions(
        columns=_int(raw.get("columns"), defaults.columns),
        rows=_int(raw.get("rows"), defaults.rows),
        zoom=_int(raw.get("zoom"), defaults.zoom),
        show_grid=bool(raw.get("show_grid", defaults.show_grid)),
        subpalette_row=_int(raw.get("subpalette_row"), defaults.subpalette_row),
        offset=_int(raw.get("offset"), defaults.offset),
        byte_nudge=_int(raw.get("byte_nudge"), defaults.byte_nudge),
    )


def _palette_from(raw: object, base_dir: str) -> PaletteSource | None:
    if not isinstance(raw, dict):
        return None
    colors = raw.get("colors")
    if isinstance(colors, list):
        try:
            parsed = [int(str(color).lstrip("#"), 16) & 0xFFFFFFFF for color in colors]
        except ValueError:
            return None  # unparseable colours: fall back to the default palette
        return PaletteSource(colors=parsed)
    path = raw.get("path")
    if isinstance(path, str) and path:
        return PaletteSource(
            path=_resolve_path(path, base_dir), offset=_int(raw.get("offset"), 0)
        )
    if "offset" in raw:
        return PaletteSource(offset=_int(raw.get("offset"), 0))
    return None


def _int(value: object, default: int | None) -> int | None:
    # bool is an int subclass; a stray `true` must not become header_length=1.
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _str(value: object, default: str) -> str:
    return value if isinstance(value, str) and value else default


# -- path handling (docs/design/project-format.md §3) ----------------------
def _store_path(target: str, base_dir: str) -> str:
    """``target`` as stored in the project: relative to the project file with
    POSIX separators when on the same drive/tree, absolute otherwise."""
    target = abspath(target)
    try:
        stored = relpath(target, base_dir)
    except ValueError:  # e.g. another drive letter on Windows — keep absolute
        stored = target
    return stored.replace(sep, "/")


def _resolve_path(stored: str, base_dir: str) -> str:
    """A stored path back to a usable one, tolerating case differences.

    The same checkout is used from Windows and WSL, so a path written on a
    case-insensitive filesystem must still find its file on a case-sensitive
    one. A path that resolves nowhere is returned as-is — the entry stays
    listed and fails (with that path in the message) at activation.
    """
    path = stored if isabs(stored) else join(base_dir, stored)
    path = normpath(path)
    return path if exists(path) else _match_case(path)


def _match_case(path: str) -> str:
    # Walk up to the deepest existing ancestor, then re-descend matching each
    # missing segment case-insensitively against the real directory listing.
    head, missing = path, []
    while not exists(head):
        head, tail = split(head)
        if not tail:  # hit the root without finding an existing ancestor
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
                return path  # genuinely missing — keep the literal path
            candidate = join(head, match)
        head = candidate
    return head
