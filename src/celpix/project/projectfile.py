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
from dataclasses import dataclass, field
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
from celpix.core.capabilities import ContentKind
from celpix.core.document import ViewOptions
from celpix.core.paletteregions import PaletteRegion, PaletteRegions
from celpix.core.tilerearrangement import TileRearrangement
from celpix.plugins.base import NO_COMPRESSION, NO_RESHAPE, RAW_CONTAINER
from celpix.project.workspace import (
    Entry,
    EntryKind,
    EntrySession,
    PaletteMode,
    PaletteSource,
    TileMode,
    TileSource,
    Workspace,
    palette_source_for,
)

# While celPix is in alpha the schema is expected to change often, and carries
# no upgrade shims: a bump means "this reader may not understand that file",
# nothing more. Projects are cheap to recreate (they hold references and
# settings, never bytes), so the version buys the warning on a newer file and
# the ordinary key-level tolerance below covers the rest. Once the format
# settles, bumps start earning migrations — and a history of what each one
# changed.
PROJECT_VERSION = 1
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
    hidden_pixel_presets: set[str] = field(default_factory=set)


# -- saving ----------------------------------------------------------------
def project_dict(ws: Workspace, path: str) -> dict[str, object]:
    """The version-stamped JSON body ``ws`` would be saved as at ``path``.

    Split out of :func:`save_project` so the UI can also ask *what would be
    written* without writing it: comparing that against the document last
    written or loaded is what tells the user their project has unsaved changes.
    Stored paths are relative to ``path``'s directory, so the same workspace
    saved to two places is legitimately two different documents.
    """
    base_dir = dirname(abspath(path))
    document: dict[str, object] = {
        "version": PROJECT_VERSION,
        "current": ws.entries.index(ws.current) if ws.current is not None else None,
        "entries": [_entry_dict(entry, base_dir) for entry in ws.entries],
    }
    # A view-only project setting: which pixel codecs the dropdown lists. Sorted
    # so the serialized form is stable (the UI diffs documents to spot unsaved
    # changes); omitted entirely when nothing is hidden, keeping a default
    # project minimal.
    if ws.hidden_pixel_presets:
        document["hidden_pixel_presets"] = sorted(ws.hidden_pixel_presets)
    return document


def save_project(ws: Workspace, path: str) -> None:
    """Serialize ``ws`` to ``path`` as a version-stamped ``.celpix`` document."""
    document = project_dict(ws, path)
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
# Derived, so the two directions of the mapping cannot disagree.
_KINDS_BY_NAME = {name: kind for kind, name in _KIND_NAMES.items()}


def _entry_dict(entry: Entry, base_dir: str) -> dict[str, object]:
    data: dict[str, object] = {
        "kind": _KIND_NAMES[entry.kind],
        "name": entry.name,
        "path": _store_path(entry.path, base_dir),
    }
    # The rest of a region's files, when it has any — omitted for the ordinary
    # one-file entry, so nothing that predates multi-file regions changes shape.
    # Stored on a slice too, and not re-derived from its parent on load: a slice
    # is loaded before the workspace exists to look a parent up in, and its
    # offsets are meaningless against a different join.
    if entry.extra_paths:
        data["extra_paths"] = [_store_path(p, base_dir) for p in entry.extra_paths]
    # Only when it isn't the pass-through default, so the ordinary un-reshaped
    # entry adds nothing to the file — and so a project written before the
    # Reshape stage existed round-trips unchanged. Files and slices alike: a
    # reshape is a property of the region, whichever kind holds it.
    if entry.reshape_id != NO_RESHAPE:
        data["reshape_id"] = entry.reshape_id
    # Only when it isn't the plain-bytes default, so detection's usual answer
    # adds nothing to the file — and so a project written before containers
    # existed round-trips unchanged. A palette carries one for the same reason a
    # file does: its colours may stop before its bytes do, and which format says
    # where is the user's to correct.
    if entry.kind in (EntryKind.FILE, EntryKind.PALETTE):
        if entry.container_id != RAW_CONTAINER:
            data["container_id"] = entry.container_id
    if entry.kind is EntryKind.SLICE:
        data["slice_offset"] = entry.slice_offset
        data["slice_length"] = entry.slice_length
        data["compression_id"] = entry.compression_id
    elif entry.kind is EntryKind.BOOKMARK:
        data["offset"] = entry.slice_offset
    elif entry.kind is EntryKind.PALETTE:
        # The codec the palette file was last read with — applying the entry
        # later must decode the same way, whatever the dropdown says then.
        data["palette_preset_id"] = entry.palette_preset_id
    # What the bytes are, as opposed to how the entry is bounded above. Omitted
    # at the default so a pixel entry — every entry any older project holds — is
    # written exactly as it was before tilemaps existed, and omitted for a
    # palette entry, whose ``kind`` already implies it (Entry.__post_init__).
    if entry.content_kind not in (ContentKind.PIXELS, ContentKind.PALETTE):
        data["content_kind"] = entry.content_kind.value
    if entry.content_kind is ContentKind.TILEMAP:
        if entry.tilemap_preset_id:
            data["tilemap_preset_id"] = entry.tilemap_preset_id
        source = entry.tile_source
        if source is not None and source.is_bound:
            data["tile_source"] = _tile_source_dict(source)
        # Only once the user has overruled the format, so a map reading in the
        # rows its preset states carries nothing — and a project written before
        # this control existed round-trips unchanged. Written even at 0, which is
        # a deliberate answer against a format that says 8 and not an absent one.
        if entry.palette_row_base is not None:
            data["palette_row_base"] = entry.palette_row_base
        if entry.sprite_size_pair is not None:
            data["sprite_size_pair"] = list(entry.sprite_size_pair)
    session = entry.session
    if session is not None:
        # The tile selection is deliberately absent: it is a transient pointer
        # at the work, not part of how the entry is set up, and persisting it
        # would make merely clicking around count as an unsaved project change.
        data["session"] = {
            "pixel_preset_id": session.pixel_preset_id,
            "palette_preset_id": session.palette_preset_id,
            "palette_mode": session.palette_mode.value,
            "compression_id": session.preview_compression_id,
        }
    # A loaded document carries the live state; a never-activated entry may
    # still hold state a previous load restored into its pending fields.
    view = entry.doc.view if entry.doc is not None else entry.pending_view
    if view is not None:
        data["view"] = {
            "columns": view.columns,
            "rows": view.rows,
            "zoom": view.zoom,
            "subpalette_row": view.subpalette_row,
            "offset": view.tile_offset,
            "byte_nudge": view.byte_nudge,
            "block_columns": view.block_columns,
            "block_rows": view.block_rows,
            "block_order": view.block_order,
            "two_dimensional": view.two_dimensional,
            "bitmap_width": view.bitmap_width,
        }
        # Only a paged tilemap has an assembly, and only once something has chosen
        # one - so every other entry, and every project written before assemblies
        # existed, is byte-identical either way.
        if view.pages_across:
            data["view"]["pages_across"] = view.pages_across
        # Only a document that was actually rearranged carries the map, so an
        # ordinary project's file is unchanged by the feature existing. Each half
        # is written only if it holds something: a rearrangement that just turns
        # tiles has no positions to store, and one that just moves them no
        # orientations. The toggle rides along — on its own it says nothing.
        if not view.tile_rearrangement.is_identity():
            if view.tile_rearrangement.pairs:
                data["view"]["tile_rearrangement"] = [
                    list(p) for p in view.tile_rearrangement.pairs
                ]
            if view.tile_rearrangement.orientations:
                data["view"]["tile_orientations"] = [
                    list(o) for o in view.tile_rearrangement.orientations
                ]
            data["view"]["show_rearranged"] = view.show_rearranged
        # Same rule for pinned palette regions: written only when something is
        # pinned, so a project that never used the feature is byte-identical.
        # Spans are pixel runs in the document's own picture space (pixel 0 is its
        # first pixel), which is what makes them meaningful under a planar codec
        # where a byte is no run of pixels at all.
        if not view.palette_regions.is_empty():
            data["view"]["palette_regions"] = [
                [r.start, r.length, r.row] for r in view.palette_regions.regions
            ]
            data["view"]["show_palette_regions"] = view.show_palette_regions
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
        raise ProjectError(f"Not a celPix project: {path} ({exc})") from exc
    if not isinstance(data, dict) or not isinstance(data.get("entries", []), list):
        raise ProjectError(f"Not a celPix project: {path}")

    base_dir = dirname(abspath(path))
    # Parse positionally (None for a skipped entry) so the stored `current`
    # index still names the right entry when earlier ones were dropped.
    parsed: list[Entry | None] = []
    for raw in data.get("entries", []):
        try:
            parsed.append(_entry_from_dict(raw, base_dir))
        except Exception:  # noqa: BLE001 — a garbage entry degrades, never aborts
            parsed.append(None)
    index = _int(data.get("current"), -1)
    current = parsed[index] if 0 <= index < len(parsed) else None
    if current is not None and current.kind not in (EntryKind.FILE, EntryKind.SLICE):
        # A bookmark or palette can't be shown; a hand-edited index degrades.
        current = None
    # Tolerate a missing/garbage filter: unknown ids are harmless (they just name
    # presets this build may not have) and a non-list degrades to no filter.
    raw_hidden = data.get("hidden_pixel_presets")
    hidden = (
        {item for item in raw_hidden if isinstance(item, str)}
        if isinstance(raw_hidden, list)
        else set()
    )
    return LoadedProject(
        version=_int(data.get("version"), 1),
        entries=[entry for entry in parsed if entry is not None],
        current=current,
        hidden_pixel_presets=hidden,
    )


def _entry_from_dict(raw: dict[str, object], base_dir: str) -> Entry:
    path = raw["path"]  # type: ignore[index] — non-dict/missing raises: entry skipped
    if not isinstance(path, str) or not path:
        raise ValueError("entry has no usable path")
    path = _resolve_path(path, base_dir)
    name = raw.get("name")
    # Anything unrecognised (hand-edited, or a kind a newer build wrote) reads
    # as a plain file rather than failing the entry.
    kind = _KINDS_BY_NAME.get(raw.get("kind"), EntryKind.FILE)
    if kind is EntryKind.PALETTE:
        # A palette entry is just a reference plus how to read it — the container
        # that says which of its bytes are colour, and the codec that decodes
        # them. No session/view/palette state of its own.
        return Entry(
            name=name if isinstance(name, str) and name else basename(path),
            kind=kind,
            path=path,
            container_id=_str(raw.get("container_id"), RAW_CONTAINER),
            palette_preset_id=_str(
                raw.get("palette_preset_id"), _DEFAULT_PALETTE_PRESET
            ),
        )
    offset_key = "offset" if kind is EntryKind.BOOKMARK else "slice_offset"
    return Entry(
        name=name if isinstance(name, str) and name else basename(path),
        kind=kind,
        path=path,
        extra_paths=tuple(
            _resolve_path(p, base_dir)
            for p in raw.get("extra_paths", ())
            if isinstance(p, str) and p
        ),
        slice_offset=_int(raw.get(offset_key), 0),
        slice_length=_int(raw.get("slice_length"), None),
        compression_id=_str(raw.get("compression_id"), NO_COMPRESSION),
        reshape_id=_str(raw.get("reshape_id"), NO_RESHAPE),
        # Absent for every file nothing claimed — plain bytes, which is also what
        # an entry naming a container the registry no longer has falls back to.
        container_id=_str(raw.get("container_id"), RAW_CONTAINER),
        # Absent on every project written before tilemaps existed, and on every
        # ordinary pixel entry since — ContentKind.parse falls back to PIXELS,
        # which is what those entries are.
        content_kind=ContentKind.parse(raw.get("content_kind")),
        tile_source=_tile_source(raw),
        tilemap_preset_id=(_str(raw.get("tilemap_preset_id"), "") or None),
        # Absent means "whatever the format says", which is every entry that never
        # overrode it — so the default has to stay None and not 0.
        palette_row_base=_int(raw.get("palette_row_base"), None),
        sprite_size_pair=_size_pair(raw.get("sprite_size_pair")),
        session=_session_from(raw.get("session")),
        pending_view=_view_from(raw.get("view")),
        pending_palette=_palette_from(raw.get("palette"), base_dir),
    )


def _size_pair(raw: object) -> tuple[int, int] | None:
    """A stored ``[small, large]`` of tile multiples, or None for the format's own.

    Anything malformed reads as None rather than failing the entry, on the same
    tolerance the rest of the schema follows: the format's answer is a working
    fallback, and a positive pair is the only thing that means anything.
    """
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        return None
    try:
        small, large = int(raw[0]), int(raw[1])
    except (TypeError, ValueError):
        return None
    return (small, large) if small > 0 and large > 0 else None


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
        preview_compression_id=_str(data.get("compression_id"), NO_COMPRESSION),
    )


def _view_from(raw: object) -> ViewOptions | None:
    if not isinstance(raw, dict):
        return None
    defaults = ViewOptions()
    return ViewOptions(
        columns=_int(raw.get("columns"), defaults.columns),
        rows=_int(raw.get("rows"), defaults.rows),
        zoom=_int(raw.get("zoom"), defaults.zoom),
        subpalette_row=_int(raw.get("subpalette_row"), defaults.subpalette_row),
        tile_offset=_int(raw.get("offset"), defaults.tile_offset),
        byte_nudge=_int(raw.get("byte_nudge"), defaults.byte_nudge),
        block_columns=_int(raw.get("block_columns"), defaults.block_columns),
        block_rows=_int(raw.get("block_rows"), defaults.block_rows),
        block_order=_block_order(raw),
        two_dimensional=bool(raw.get("two_dimensional", defaults.two_dimensional)),
        bitmap_width=_int(raw.get("bitmap_width"), defaults.bitmap_width),
        # Absent on every entry that is not a paged tilemap. A value the file no
        # longer has pages for is not checked here — the document does that when it
        # resolves the assembly, which is where the page count is known
        # (:attr:`~celpix.core.document.Document.pages_across`).
        pages_across=_int(raw.get("pages_across"), defaults.pages_across),
        tile_rearrangement=_tile_rearrangement(raw),
        show_rearranged=bool(raw.get("show_rearranged", defaults.show_rearranged)),
        palette_regions=_palette_regions(raw),
        show_palette_regions=bool(
            raw.get("show_palette_regions", defaults.show_palette_regions)
        ),
    )


def _tile_source_dict(source: TileSource) -> dict[str, object]:
    """A bound tile source as JSON. ``base_index`` rides along when it is set.

    No path: the tiles are always another entry in this same project, and that
    entry stores its own path once, where relocating it fixes both.
    """
    data: dict[str, object] = {
        "mode": source.mode.value,
        "entry_index": source.entry_index,
    }
    if source.base_index:
        data["base_index"] = source.base_index
    return data


def _tile_source(raw: dict) -> TileSource | None:
    """A stored tile source, or ``None`` for anything unusable.

    Tolerant like the rest of this path: an unreadable binding leaves the tilemap
    unbound — it opens showing placeholder cells and can be re-pointed — rather
    than failing the entry and losing the map as well as its tiles.
    """
    data = raw.get("tile_source")
    if not isinstance(data, dict):
        return None
    try:
        mode = TileMode(data.get("mode"))
    except ValueError:
        return None
    if mode is TileMode.NONE:
        return None
    return TileSource(
        mode=mode,
        entry_index=_int(data.get("entry_index"), None),
        base_index=_int(data.get("base_index"), 0) or 0,
    )


def _palette_regions(raw: dict) -> PaletteRegions:
    """Stored pinned regions, skipping any triple that isn't three ints.

    Tolerant like everything else on this path: a malformed span is dropped and
    the rest of the entry opens. ``from_regions`` normalizes whatever survives, so
    a hand-edited file with overlapping or unsorted spans still loads to a
    well-formed set rather than one the lookup can't trust.
    """
    items = raw.get("palette_regions")
    if not isinstance(items, list):
        return PaletteRegions()
    parsed = []
    for item in items:
        if not isinstance(item, list | tuple) or len(item) != 3:
            continue
        try:
            start, length, row = (int(v) for v in item)
        except (TypeError, ValueError):
            continue
        if length > 0 and start >= 0 and row >= 0:
            parsed.append(PaletteRegion(start, length, row))
    return PaletteRegions.from_regions(parsed)


def _tile_rearrangement(raw: dict) -> TileRearrangement:
    """A stored rearrangement, or the identity map for anything unusable.

    Hand-edited or truncated pairs can describe something that isn't a
    permutation, which :class:`TileRearrangement` refuses to build. A project
    that won't open is worse than one that opens unrearranged, so a bad map is
    dropped rather than raised — the tiles are all still there, just in file
    order.

    ``tile_map`` is the key this was written under before the type was renamed.
    It is still read, because a project file outlives the name we happened to
    give the class, and a silently unrearranged reopen is exactly the kind of
    data loss the tolerance above exists to avoid.
    """
    pairs = raw.get("tile_rearrangement", raw.get("tile_map"))
    try:
        return TileRearrangement.from_pairs(
            _int_pairs(pairs), _int_pairs(raw.get("tile_orientations"))
        )
    except (ValueError, TypeError):
        return TileRearrangement()


def _int_pairs(raw: object) -> list[tuple[int, int]]:
    """The well-formed two-integer entries of a stored pair list."""
    if not isinstance(raw, list):
        return []
    return [
        (int(pair[0]), int(pair[1]))
        for pair in raw
        if isinstance(pair, (list, tuple)) and len(pair) == 2
    ]


def _block_order(raw: dict) -> str:
    order = raw.get("block_order")
    # An absent or unrecognised order reads as the plain one: an arrangement that
    # can't be named is better opened in file order than not opened.
    return order if order in BLOCK_ORDERS else "row"


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
    # bool is an int subclass; a stray `true` must not become a count of 1.
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
