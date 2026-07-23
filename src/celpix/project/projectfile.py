"""The ``.celpix`` project file: save/load the workspace as JSON.

A project stores **references and settings, never the edited bytes** — the open
entries (files, slices, bookmarks, palette files), each one's session settings
and view state, and where its palette comes from
(``docs/design/project-format.md``). Writers emit
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

from celpix.core.arrangement import BLOCK_ORDERS
from celpix.core.document import ViewOptions
from celpix.plugins.base import NO_DECOMPRESS
from celpix.project.workspace import (
    Entry,
    EntryKind,
    EntrySession,
    PaletteMode,
    PaletteSource,
    Workspace,
    palette_source_for,
)

# 2: added the "bookmark" entry kind (v1 readers would misread one as a file,
# so the bump makes them warn instead of degrading silently).
# 3: added the "palette" entry kind (same reasoning — a v2 reader would open a
# palette file as pixel data).
# 4: added the "custom" palette mode, whose colors live inline in the project.
# A v3 reader falls back to "default" for the unknown mode and would drop the
# edited colors on the next save, so the bump makes it warn instead.
PROJECT_VERSION = 4
PROJECT_EXTENSION = ".celpix"

# Fallbacks for a hand-authored project that omits preset ids entirely — the
# same built-ins a fresh window starts on, so a minimal project still renders.
_DEFAULT_PIXEL_PRESET = "preset.pixel.snes-4bpp"
_DEFAULT_PALETTE_PRESET = "preset.palette.bgr555"


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
def project_document(ws: Workspace, path: str) -> dict[str, object]:
    """The version-stamped document ``ws`` would be saved as at ``path``.

    Split out of :func:`save_project` so the UI can also ask *what would be
    written* without writing it: comparing that against the document last
    written or loaded is what tells the user their project has unsaved changes.
    Stored paths are relative to ``path``'s directory, so the same workspace
    saved to two places is legitimately two different documents.
    """
    base_dir = dirname(abspath(path))
    return {
        "version": PROJECT_VERSION,
        "current": ws.entries.index(ws.current) if ws.current is not None else None,
        "entries": [_entry_dict(entry, base_dir) for entry in ws.entries],
    }


def save_project(ws: Workspace, path: str) -> None:
    """Serialize ``ws`` to ``path`` as a version-stamped ``.celpix`` document."""
    document = project_document(ws, path)
    # LF + trailing newline: projects are meant to live in version control.
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(document, handle, indent=2)
        handle.write("\n")


_KIND_NAMES = {
    EntryKind.FILE: "file",
    EntryKind.SLICE: "slice",
    EntryKind.BOOKMARK: "bookmark",
    EntryKind.PALETTE: "palette",
}


def _entry_dict(entry: Entry, base_dir: str) -> dict[str, object]:
    data: dict[str, object] = {
        "kind": _KIND_NAMES[entry.kind],
        "name": entry.name,
        "path": _store_path(entry.path, base_dir),
    }
    if entry.kind is EntryKind.SLICE:
        data["slice_offset"] = entry.slice_offset
        data["slice_length"] = entry.slice_length
        data["decompress_id"] = entry.decompress_id
    elif entry.kind is EntryKind.BOOKMARK:
        data["offset"] = entry.slice_offset
    elif entry.kind is EntryKind.PALETTE:
        # The codec the palette file was last read with — applying the entry
        # later must decode the same way, whatever the dropdown says then.
        data["palette_preset_id"] = entry.palette_preset_id
    session = entry.session
    if session is not None:
        # The tile selection is deliberately absent: it is a transient pointer
        # at the work, not part of how the entry is set up, and persisting it
        # would make merely clicking around count as an unsaved project change.
        data["session"] = {
            "pixel_preset_id": session.pixel_preset_id,
            "palette_preset_id": session.palette_preset_id,
            "palette_mode": session.palette_mode.value,
            "compression_id": session.compression_id,
            "headered": session.headered,
            "header_length": session.header_length,
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
            "offset": view.tile_offset,
            "byte_nudge": view.byte_nudge,
            "block_columns": view.block_columns,
            "block_rows": view.block_rows,
            "block_order": view.block_order,
            "two_dimensional": view.two_dimensional,
        }
    palette = palette_source_for(entry)
    if palette is not None:
        data["palette"] = _palette_dict(palette, base_dir)
    return data


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
    if current is not None and current.kind not in (EntryKind.FILE, EntryKind.SLICE):
        # A bookmark or palette can't be shown; a hand-edited index degrades.
        current = None
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
    kind_name = raw.get("kind")
    if kind_name == "slice":
        kind = EntryKind.SLICE
    elif kind_name == "bookmark":
        kind = EntryKind.BOOKMARK
    elif kind_name == "palette":
        kind = EntryKind.PALETTE
    else:
        kind = EntryKind.FILE
    if kind is EntryKind.PALETTE:
        # A palette entry is just a reference plus its import codec — no
        # session/view/palette state of its own.
        return Entry(
            name=name if isinstance(name, str) and name else basename(path),
            kind=kind,
            path=path,
            palette_preset_id=_str(
                raw.get("palette_preset_id"), _DEFAULT_PALETTE_PRESET
            ),
        )
    offset_key = "offset" if kind is EntryKind.BOOKMARK else "slice_offset"
    return Entry(
        name=name if isinstance(name, str) and name else basename(path),
        kind=kind,
        path=path,
        slice_offset=_int(raw.get(offset_key), 0),
        slice_length=_int(raw.get("slice_length"), None),
        decompress_id=_str(raw.get("decompress_id"), NO_DECOMPRESS),
        session=_session_from(raw.get("session")),
        pending_view=_view_from(raw.get("view")),
        pending_palette=_palette_from(raw.get("palette"), base_dir),
    )


def _session_from(raw: object) -> EntrySession:
    # The session's selection fields stay at their defaults: a project doesn't
    # store a selection, and one written by an earlier version is read past like
    # any other key this version doesn't use — an entry opens with nothing
    # selected either way.
    data = raw if isinstance(raw, dict) else {}
    return EntrySession(
        pixel_preset_id=_str(data.get("pixel_preset_id"), _DEFAULT_PIXEL_PRESET),
        palette_preset_id=_str(data.get("palette_preset_id"), _DEFAULT_PALETTE_PRESET),
        palette_mode=PaletteMode.parse(data.get("palette_mode")),
        compression_id=_str(data.get("compression_id"), NO_DECOMPRESS),
        headered=bool(data.get("headered", False)),
        header_length=_int(data.get("header_length"), 512),
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
        tile_offset=_int(raw.get("offset"), defaults.tile_offset),
        byte_nudge=_int(raw.get("byte_nudge"), defaults.byte_nudge),
        block_columns=_int(raw.get("block_columns"), defaults.block_columns),
        block_rows=_int(raw.get("block_rows"), defaults.block_rows),
        block_order=_block_order(raw),
        two_dimensional=bool(raw.get("two_dimensional", defaults.two_dimensional)),
    )


def _block_order(raw: dict) -> str:
    order = raw.get("block_order")
    if order in BLOCK_ORDERS:
        return order
    # Tolerate early v0.0.6 projects that stored a row_interleave bool before the
    # order selector existed.
    return "row-interleave" if raw.get("row_interleave") else "row"


def _palette_from(raw: object, base_dir: str) -> PaletteSource | None:
    if not isinstance(raw, dict):
        return None
    colors = raw.get("colors")
    if isinstance(colors, list):
        try:
            parsed = [int(str(color).lstrip("#"), 16) & 0xFFFFFFFF for color in colors]
        except ValueError:
            return None  # unparseable colors: fall back to the default palette
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
