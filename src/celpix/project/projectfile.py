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
from dataclasses import dataclass, field, replace
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
from celpix.core.aspect import PixelAspect
from celpix.core.aspect import parse as parse_aspect
from celpix.core.capabilities import ContentKind
from celpix.core.document import ViewOptions
from celpix.core.errors import Stage
from celpix.core.font import HOLE, TEMPLATES, Glyph, glyphs_from_spec
from celpix.core.paletteregions import PaletteRegion, PaletteRegions
from celpix.core.tilerearrangement import TileRearrangement
from celpix.pipeline.pathway import DEFAULT_SLOT_FILL, SlotFill
from celpix.plugins.aliases import current_id
from celpix.plugins.base import (
    NO_COMPRESSION,
    NO_RESHAPE,
    RAW_CONTAINER,
    STAGE_DEFAULT_PRESET,
)
from celpix.project.workspace import (
    CompositePiece,
    Entry,
    EntryKind,
    EntrySession,
    PaletteMode,
    PaletteSource,
    TileMode,
    TileSource,
    Workspace,
    can_compose,
    palette_source_for,
)

# While celPix is in alpha the schema is expected to change often, and carries
# no upgrade shims: a bump means "this reader may not understand that file",
# nothing more. Projects are cheap to recreate (they hold references and
# settings, never bytes), so the version buys the warning on a newer file and
# the ordinary key-level tolerance below covers the rest. Once the format
# settles, bumps start earning migrations — and a history of what each one
# changed.
#
# Renamed plugin and preset **ids** are the exception, and are translated at
# every version (:func:`_plugin_id`). They are not a schema detail: an id names
# what an entry was opened *with*, so a rename with no forwarding address resets
# that entry to pass-through, which reads as data loss. That mapping lives in
# `plugins/aliases.py` and is independent of this number.
PROJECT_VERSION = 1
PROJECT_EXTENSION = ".celpix"

# The two alphabet presets celPix used to ship, by the id an older project names
# them with, as ``(chars, base)`` — the runs are :data:`~celpix.core.font.
# TEMPLATES` and have not changed. This is the whole of the alphabet's legacy
# read (:func:`_font_from`): the tables that were shipped survive as data, and a
# project naming one of them opens with its text still readable.
_LEGACY_PRESET_RUNS: dict[str, tuple[str, int]] = {
    "alphabet.ascii-upper": (TEMPLATES[0][2], TEMPLATES[0][1]),
    "alphabet.ascii": (TEMPLATES[1][2], TEMPLATES[1][1]),
}

# Fallbacks for a hand-authored project that omits preset ids entirely — the
# same built-ins a fresh window starts on, so a minimal project still renders,
# and the same ones an entry naming a *missing* format falls back to
# (:func:`~celpix.project.workspace.repair_presets`).
_DEFAULT_PIXEL_PRESET = STAGE_DEFAULT_PRESET[Stage.INTERPRET_PIXEL]
_DEFAULT_PALETTE_PRESET = STAGE_DEFAULT_PRESET[Stage.INTERPRET_PALETTE]


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
    #: The stored pixel shape, or ``None`` where the file names none — see
    #: :attr:`~celpix.project.workspace.Workspace.pixel_aspect` for why the two
    #: are different answers.
    pixel_aspect: PixelAspect | None = None


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
        "entries": [_entry_dict(entry, base_dir, ws.entries) for entry in ws.entries],
    }
    # A view-only project setting: which pixel codecs the dropdown lists. Sorted
    # so the serialized form is stable (the UI diffs documents to spot unsaved
    # changes); omitted entirely when nothing is hidden, keeping a default
    # project minimal.
    if ws.hidden_pixel_presets:
        document["hidden_pixel_presets"] = sorted(ws.hidden_pixel_presets)
    # The other project-wide view setting: the shape one pixel is drawn at. A
    # list of two, so the file says ``[1, 2]`` rather than a float nobody can read
    # back as a ratio. Omitted while nothing has answered, which is what keeps the
    # question open for a container's hint on the next load — and keeps every
    # project written before this existed byte-identical.
    if ws.pixel_aspect is not None:
        document["pixel_aspect"] = list(ws.pixel_aspect)
    return document


def save_project(ws: Workspace, path: str) -> None:
    """Serialize ``ws`` to ``path`` as a version-stamped ``.celpix`` document."""
    document = project_dict(ws, path)
    # LF + trailing newline: projects are meant to live in version control.
    #
    # ``ensure_ascii=False`` because a font alphabet's run is whatever characters
    # the sheet draws, and a Japanese or Cyrillic font escaped to ``\uXXXX`` is a
    # wall nobody can read or review a diff of. The file is UTF-8 either way.
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(document, handle, indent=2, ensure_ascii=False)
        handle.write("\n")


_KIND_NAMES = {
    EntryKind.FILE: "file",
    EntryKind.SLICE: "slice",
    EntryKind.BOOKMARK: "bookmark",
    EntryKind.PALETTE: "palette",
    EntryKind.COMPOSITE: "composite",
}
# Derived, so the two directions of the mapping cannot disagree.
_KINDS_BY_NAME = {name: kind for kind, name in _KIND_NAMES.items()}


def _entry_dict(
    entry: Entry, base_dir: str | None, entries: list[Entry]
) -> dict[str, object]:
    data: dict[str, object] = {
        "kind": _KIND_NAMES[entry.kind],
        "name": entry.name,
    }
    # Every kind but a composite is named by a file. A composite is assembled out
    # of other entries and has none, and its ``path`` is ``""`` — which
    # :func:`_store_path` would relativise into a path to the project's own
    # folder, a plausible-looking string naming something that was never there.
    if entry.kind is not EntryKind.COMPOSITE:
        data["path"] = _store_path(entry.path, base_dir)
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
        # Only when it isn't what an unstated one means, so a slice left on the
        # default — and every slice in every project written before the choice
        # existed — is written exactly as it was before.
        if entry.slot_fill is not DEFAULT_SLOT_FILL:
            data["slot_fill"] = entry.slot_fill.value
    elif entry.kind is EntryKind.BOOKMARK:
        data["offset"] = entry.slice_offset
    elif entry.kind is EntryKind.PALETTE:
        # The codec the palette file was last read with — applying the entry
        # later must decode the same way, whatever the dropdown says then.
        data["palette_preset_id"] = entry.palette_preset_id
    elif entry.kind is EntryKind.COMPOSITE:
        # The whole of what a composite is: an ordered list of other entries,
        # written as positions (``docs/design/composite-entry.md``). Its ``path``
        # above is ``""`` — it comes from no file — which is what the reader uses
        # to tell it apart from anything that does.
        data["pieces"] = _pieces_list(entry, entries)
    # What the bytes are, as opposed to how the entry is bounded above. Omitted
    # at the default so a pixel entry — every entry any older project holds — is
    # written exactly as it was before tilemaps existed, and omitted for a
    # palette entry, whose ``kind`` already implies it (Entry.__post_init__).
    if entry.content_kind not in (ContentKind.PIXELS, ContentKind.PALETTE):
        data["content_kind"] = entry.content_kind.value
    # Not gated on the content kind, unlike the binding below it: a tile bank's
    # pinned rows count from a base exactly as a map's cells do, so a pixel entry
    # has one to keep. Written only once the user has overruled what the file
    # said, so an entry reading in the rows its format states carries nothing —
    # and written even at 0, which is a deliberate answer against a format that
    # says 8 and not an absent one.
    if entry.palette_row_base is not None:
        data["palette_row_base"] = entry.palette_row_base
    # Not gated either, and for the mirror of that reason: this is a *font's*
    # answer, so it belongs to the pixel entry holding the tiles rather than to
    # any fontmap that reads them (`docs/design/fontmap-entry.md` §3).
    font = _font_dict(entry)
    if font:
        data["font"] = font
    if entry.content_kind is ContentKind.TILEMAP:
        if entry.tilemap_preset_id:
            data["tilemap_preset_id"] = entry.tilemap_preset_id
        source = entry.tile_source
        if source is not None and source.is_bound:
            data["tile_source"] = _tile_source_dict(source, entries)
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
        # No ``zoom``: it is an app-wide preference rather than the entry's, and
        # lives in QSettings (§7 of ``docs/design/project-format.md``). A project
        # written before that carries one, and it is read back as nothing - the
        # key simply stops being answered for.
        data["view"] = {
            "columns": view.columns,
            "rows": view.rows,
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
        # Same rule, and the same reason it is safe to write unconditionally on
        # nothing: only a sprite map can turn it on, and it defaults off — so an
        # entry that never saw the box leaves the key out entirely.
        if view.show_all_frames:
            data["view"]["show_all_frames"] = True
        # Same rule again: off by default and only a tilemap offers the box, so a
        # project that never asked for it is byte-identical.
        if view.transparent_zero:
            data["view"]["transparent_zero"] = True
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
        # where a byte is no run of pixels at all. The toggle that *shows* them
        # does not ride along, unlike show_rearranged: it is a local preference in
        # QSettings (``ui/main_window/palette_regions.py``).
        if not view.palette_regions.is_empty():
            data["view"]["palette_regions"] = [
                [r.start, r.length, r.row] for r in view.palette_regions.regions
            ]
    palette = palette_source_for(entry)
    if palette is not None:
        data["palette"] = _palette_dict(palette, base_dir)
    return data


def _font_dict(entry: Entry) -> dict[str, object]:
    """``entry``'s font alphabet, or ``{}`` where it has nothing to say.

    Every key omitted at its default, the house rule, so a pixel entry nobody has
    used as a font — which is every entry any older project holds — writes no
    ``font`` key at all.

    The run is trimmed of trailing holes because they say nothing: a hole is what
    keeps the letters *after* it on the right tiles, and there are none after the
    last one. Interior holes stay, and have to.
    """
    data: dict[str, object] = {}
    if entry.use_as_font:
        data["use"] = True
    if entry.font_base:
        data["base"] = entry.font_base
    if entry.font_prepend:
        data["prepend"] = entry.font_prepend
    if entry.font_append:
        data["append"] = entry.font_append
    chars = entry.font_chars.rstrip(HOLE)
    if chars:
        data["chars"] = chars
    if entry.font_codes:
        data["codes"] = [_glyph_dict(glyph) for glyph in entry.font_codes]
    return data


def _glyph_dict(glyph: Glyph) -> dict[str, object]:
    """One named code, written the way it is meant: a character or a command.

    A character spells itself and says nothing else, so it is ``text`` alone —
    including a **dictionary** code, which spells several of them and is still
    only its spelling: ``dict`` is what having more than one character *is*
    (:class:`~celpix.core.font.GlyphRole`), so writing the role beside it would
    be a second copy of a fact the ``text`` already carries, free to disagree
    with it. A command carries a ``name`` — what the string holds inside its
    brackets — its ``role``, a ``description`` where the author wrote one, and
    ``params`` where it swallows cells after itself. Split by role rather than
    written uniformly because the two fields are different promises: ``text`` is
    what a tile *draws*, ``name`` is what a reader *types*.
    """
    if glyph.spells:
        return {"code": glyph.code, "text": glyph.text}
    data: dict[str, object] = {
        "code": glyph.code,
        "name": glyph.text,
        "role": glyph.role.value,
    }
    if glyph.description:
        data["description"] = glyph.description
    if glyph.params:
        data["params"] = glyph.params
    return data


def _palette_dict(palette: PaletteSource, base_dir: str | None) -> dict[str, object]:
    if palette.colors is not None:
        return {"colors": [f"#{color & 0xFFFFFFFF:08X}" for color in palette.colors]}
    if palette.path is not None:
        return {"path": _store_path(palette.path, base_dir), "offset": palette.offset}
    return {"offset": palette.offset}


# -- the clipboard form (docs/design/project-format.md §6) -----------------
#: Bumped only on an incompatible change to the payload below. A copy taken by
#: another build reads as "nothing to paste" rather than as garbage entries.
CLIPBOARD_VERSION = 1


@dataclass(frozen=True)
class CopiedEntry:
    """One entry off the clipboard, plus the two positions it was written with.

    ``source_index`` is where it sat in the list it was copied from, and
    ``tile_source`` / ``tile_source_index`` its tile binding and the position
    that binding named there (``None`` / ``-1`` for a map that is unbound, and
    for everything that is not a map).

    **Neither number is a reference to a live list**, and reading one as though
    it were is the mistake the whole clipboard path is arranged to avoid: the
    rows are the user's to rearrange, so a position recorded when a copy was
    taken names something else entirely by the time it is pasted. They are a
    **join between the records of one payload** — both written in a single
    :func:`entries_payload` call against one snapshot — so all they can answer is
    "was the bank copied along with the map?". A bank that was *not* copied is
    matched by identity instead, outside this file, where the object still exists
    (:data:`~celpix.ui.clipboard._COPIED_BINDINGS`).

    The binding is handed back **beside** the entry rather than on it for the
    reason :func:`_bind_tile_sources` leaves an unresolvable one at ``None``: a
    :class:`~celpix.project.workspace.TileSource` that says it is bound and names
    no entry is a state nothing downstream expects.
    """

    entry: Entry
    source_index: int
    tile_source: TileSource | None
    tile_source_index: int
    #: One position per piece of a copied **composite**, in order — the same join
    #: as ``tile_source_index`` and read the same way, ``-1`` for a pad and for a
    #: source that was not part of the copy. A composite's pieces are entries and
    #: an entry is not a value, so without this a pasted composite arrives with
    #: its list emptied (``docs/design/composite-entry.md``).
    piece_sources: tuple[int, ...] = ()


def entries_payload(
    entries: list[Entry], all_entries: list[Entry], session: str
) -> dict[str, object]:
    """``entries`` as a clipboard payload — the project form, absolute-pathed.

    Deliberately the *same* per-entry shape a project file holds: a copied entry
    is a copied reference plus its settings, which is exactly what
    :func:`_entry_dict` already states, and one writer means a paste can never
    carry less than a save does. What differs is only what a position can be
    resolved against, which is what ``session`` and the two indices below are
    for.

    ``session`` is a token identifying the running editor, and what it buys is
    named on :class:`CopiedEntry`: it says the entry objects this process
    remembered alongside the payload are the ones this payload means. A paste
    into another process has only the payload, and resolves bindings no further
    than the copy itself carries.
    """
    positions = {id(entry): i for i, entry in enumerate(all_entries)}
    written = []
    for entry in entries:
        data = _entry_dict(entry, None, all_entries)
        data["source_index"] = positions.get(id(entry), -1)
        written.append(data)
    return {
        "version": CLIPBOARD_VERSION,
        "session": session,
        "entries": written,
    }


def entries_from_payload(raw: object) -> list[CopiedEntry]:
    """A clipboard payload back into entries — ``[]`` for anything unusable.

    Tolerant per entry exactly as :func:`load_project` is: one unreadable record
    is dropped and the rest of the paste still lands. The whole payload is
    refused only where it is not ours to read at all — the wrong shape, or a
    version this build has no meaning for.
    """
    if not isinstance(raw, dict) or raw.get("version") != CLIPBOARD_VERSION:
        return []
    records = raw.get("entries")
    if not isinstance(records, list):
        return []
    out = []
    for record in records:
        if not isinstance(record, dict):
            continue
        try:
            entry = _entry_from_dict(record, "")
        except Exception:  # noqa: BLE001 — a garbage entry degrades, never aborts
            continue
        binding = _tile_source(record)
        pieces = _pieces_from(record)
        # The pieces themselves ride on the entry; only the entry each one names
        # has to come back beside it, for the reason the binding does.
        entry.pieces = tuple(piece for piece, _at in pieces)
        out.append(
            CopiedEntry(
                entry=entry,
                source_index=_int(record.get("source_index"), -1),
                tile_source=binding[0] if binding is not None else None,
                tile_source_index=binding[1] if binding is not None else -1,
                piece_sources=tuple(at for _piece, at in pieces),
            )
        )
    return out


def payload_session(raw: object) -> str:
    """The session token a payload was written by — ``""`` when it has none."""
    return _str(raw.get("session"), "") if isinstance(raw, dict) else ""


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
    _bind_tile_sources(data.get("entries", []), parsed)
    # After the bindings and for the same reason: both turn a stored position
    # back into an object, and both can only do it once every entry exists.
    _bind_composite_pieces(data.get("entries", []), parsed)
    index = _int(data.get("current"), -1)
    current = parsed[index] if 0 <= index < len(parsed) else None
    if current is not None and not current.kind.has_document:
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
        # None for a missing or malformed ratio, which is the same state a
        # project that has never been asked is in: the hint gets to answer.
        pixel_aspect=parse_aspect(data.get("pixel_aspect")),
    )


def _entry_from_dict(raw: dict[str, object], base_dir: str) -> Entry:
    name = raw.get("name")
    # Anything unrecognised (hand-edited, or a kind a newer build wrote) reads
    # as a plain file rather than failing the entry.
    kind = _KINDS_BY_NAME.get(raw.get("kind"), EntryKind.FILE)
    if kind is EntryKind.COMPOSITE:
        # Alone among the kinds in having no path — it comes from no file — which
        # is why it is answered before the path check below rather than inside
        # it. Its pieces arrive empty and are filled in by
        # :func:`_bind_composite_pieces` once every entry they name is parsed.
        return Entry(
            name=name if isinstance(name, str) and name else "composite",
            kind=kind,
            path="",
            palette_row_base=_int(raw.get("palette_row_base"), None),
            **_font_from(raw),
            session=_session_from(raw.get("session")),
            pending_view=_view_from(raw.get("view")),
            pending_palette=_palette_from(raw.get("palette"), base_dir),
        )
    path = raw["path"]  # type: ignore[index] — non-dict/missing raises: entry skipped
    if not isinstance(path, str) or not path:
        raise ValueError("entry has no usable path")
    path = _resolve_path(path, base_dir)
    if kind is EntryKind.PALETTE:
        # A palette entry is just a reference plus how to read it — the container
        # that says which of its bytes are colour, and the codec that decodes
        # them. No session/view/palette state of its own.
        return Entry(
            name=name if isinstance(name, str) and name else basename(path),
            kind=kind,
            path=path,
            container_id=_plugin_id(raw.get("container_id"), RAW_CONTAINER),
            palette_preset_id=_plugin_id(
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
        compression_id=_plugin_id(raw.get("compression_id"), NO_COMPRESSION),
        reshape_id=_plugin_id(raw.get("reshape_id"), NO_RESHAPE),
        # Absent on every slice that never overrode it, and on every project
        # written before the choice existed — SlotFill.parse gives those the
        # default, which is what they get in the dialog too.
        slot_fill=SlotFill.parse(raw.get("slot_fill")),
        # Absent for every file nothing claimed — plain bytes, which is also what
        # an entry naming a container the registry no longer has falls back to.
        container_id=_plugin_id(raw.get("container_id"), RAW_CONTAINER),
        # Absent on every project written before tilemaps existed, and on every
        # ordinary pixel entry since — ContentKind.parse falls back to PIXELS,
        # which is what those entries are.
        content_kind=ContentKind.parse(raw.get("content_kind")),
        tilemap_preset_id=(_plugin_id(raw.get("tilemap_preset_id"), "") or None),
        # Absent means "whatever the format says", which is every entry that never
        # overrode it — so the default has to stay None and not 0.
        palette_row_base=_int(raw.get("palette_row_base"), None),
        **_font_from(raw),
        sprite_size_pair=_size_pair(raw.get("sprite_size_pair")),
        session=_session_from(raw.get("session")),
        pending_view=_view_from(raw.get("view")),
        pending_palette=_palette_from(raw.get("palette"), base_dir),
    )


def _font_from(raw: dict) -> dict[str, object]:
    """The font-alphabet fields of one entry, as ``Entry`` keyword arguments.

    Tolerant throughout, the rule every reader here follows: a ``chars`` that is
    not a string reads as no run, and a malformed record in ``codes`` is skipped
    by :func:`~celpix.core.font.glyphs_from_spec` rather than costing the user
    the rest of their table.

    **The legacy read.** Before the alphabet was the entry's own data it was a
    preset the entry named, and the two shipped presets held runs this build
    still has (:data:`~celpix.core.font.TEMPLATES`). A project naming one of them
    opens with its text still readable; a project naming any other is left with
    an empty run, which is the ordinary "no alphabet yet" state and reads as hex.
    Deletable once alpha projects have been re-saved.
    """
    font = raw.get("font")
    if not isinstance(font, dict):
        legacy = _plugin_id(raw.get("alphabet_preset_id"), "")
        base = _int(raw.get("alphabet_base"), 0) or 0
        chars = _LEGACY_PRESET_RUNS.get(legacy, ("", 0))
        return {
            "use_as_font": bool(legacy),
            "font_base": base or chars[1],
            "font_chars": chars[0],
        }
    spec = font.get("codes")
    return {
        "use_as_font": bool(font.get("use")),
        "font_base": _int(font.get("base"), 0) or 0,
        # Row counts, so negatives are meaningless rather than a direction — a
        # file carrying one is read as none, the same tolerance every field here
        # is read with.
        "font_prepend": max(0, _int(font.get("prepend"), 0) or 0),
        "font_append": max(0, _int(font.get("append"), 0) or 0),
        "font_chars": _str(font.get("chars"), ""),
        "font_codes": tuple(glyphs_from_spec(spec) if isinstance(spec, list) else ()),
    }


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
        pixel_preset_id=_plugin_id(data.get("pixel_preset_id"), _DEFAULT_PIXEL_PRESET),
        palette_preset_id=_plugin_id(
            data.get("palette_preset_id"), _DEFAULT_PALETTE_PRESET
        ),
        palette_mode=PaletteMode.parse(data.get("palette_mode")),
        preview_compression_id=_plugin_id(data.get("compression_id"), NO_COMPRESSION),
    )


def _view_from(raw: object) -> ViewOptions | None:
    if not isinstance(raw, dict):
        return None
    defaults = ViewOptions()
    return ViewOptions(
        columns=_int(raw.get("columns"), defaults.columns),
        rows=_int(raw.get("rows"), defaults.rows),
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
        show_all_frames=bool(raw.get("show_all_frames", defaults.show_all_frames)),
        transparent_zero=bool(raw.get("transparent_zero", defaults.transparent_zero)),
        tile_rearrangement=_tile_rearrangement(raw),
        show_rearranged=bool(raw.get("show_rearranged", defaults.show_rearranged)),
        palette_regions=_palette_regions(raw),
    )


def _tile_source_dict(source: TileSource, entries: list[Entry]) -> dict[str, object]:
    """A bound tile source as JSON. ``base_index`` rides along when it is set.

    A binding holds the bound :class:`Entry` itself, and a file cannot name an
    object — so this is where it becomes a **position**, and
    :func:`_bind_tile_sources` is where a position becomes an object again. The
    two are the only places the positional form exists, which is what keeps it
    from being something the running editor can get wrong: in a file the list is
    fixed, so "the third entry" names something, and in a session it does not
    (:class:`~celpix.project.workspace.TileSource`).

    ``-1`` for an entry that is not in the list, which is what a binding onto a
    closed entry writes: it round-trips to unbound rather than to whatever now
    sits at the index a stale number would have held.

    No path: the tiles are always another entry in this same project, and that
    entry stores its own path once, where relocating it fixes both.
    """
    at = next((i for i, entry in enumerate(entries) if entry is source.entry), -1)
    data: dict[str, object] = {"mode": source.mode.value, "entry_index": at}
    if source.base_index:
        data["base_index"] = source.base_index
    return data


def _tile_source(raw: dict) -> tuple[TileSource, int] | None:
    """A stored tile source and the entry position it named, or ``None``.

    The position is handed back separately because the entry it names may not be
    parsed yet — :func:`_bind_tile_sources` resolves it once they all are.

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
    source = TileSource(mode=mode, base_index=_int(data.get("base_index"), 0) or 0)
    return source, _int(data.get("entry_index"), -1)


def _bind_tile_sources(raw_entries: list, parsed: list[Entry | None]) -> None:
    """Point every parsed binding at the entry its stored position named.

    Resolved against ``parsed``, which is **positional including the entries that
    failed to parse** — a stored index counts those, since it was written against
    a list that had them. Resolving against the surviving entries instead would
    shift every binding past a dropped one onto its neighbour.

    A position naming nothing leaves the map unbound, on the same rule the rest
    of this module follows: the map opens on placeholders and can be re-pointed.
    """
    for raw, entry in zip(raw_entries, parsed, strict=True):
        if entry is None or not isinstance(raw, dict):
            continue
        found = _tile_source(raw)
        if found is None:
            continue
        source, at = found
        target = parsed[at] if 0 <= at < len(parsed) else None
        entry.tile_source = replace(source, entry=target) if target else None


def _pieces_list(entry: Entry, entries: list[Entry]) -> list[dict[str, object]]:
    """A composite's pieces as JSON, in order — positions, like a tile source.

    A piece holds the source :class:`Entry` itself and a file cannot name an
    object, so this is where it becomes a **position** and
    :func:`_bind_composite_pieces` is where it becomes an object again. The same
    two-places rule :func:`_tile_source_dict` states, for the same reason.

    Every length here is **bytes**, into the source's resolved data — its
    decompressed stream where it is compressed, its own bytes where it is not
    (:class:`~celpix.project.workspace.CompositePiece`). Which keys appear says
    which shape the piece is:

    - A **pad** writes ``length`` and no ``entry_index``: it names nothing, and
      its length is the whole of it.
    - A **whole-entry** source writes ``entry_index`` and ``measured`` — how many
      bytes the run last resolved to, which is what lets a composite keep its
      shape when that piece cannot be read on the next open.
    - A **ranged** source writes ``length`` (and ``offset`` where it is not 0) as
      well, because those are the user's request rather than an observation, and
      a request has to survive a load that cannot read the source
      (``docs/design/composite-entry.md``).

    ``-1`` for a source that is no longer in the list, which round-trips to a pad
    of the right length rather than to whatever now sits at a stale index.
    """
    out: list[dict[str, object]] = []
    for piece in entry.pieces:
        data: dict[str, object] = {}
        if piece.is_pad:
            data["length"] = piece.extent
            out.append(data)
            continue
        data["entry_index"] = next(
            (i for i, other in enumerate(entries) if other is piece.entry), -1
        )
        if piece.offset:
            data["offset"] = piece.offset
        if piece.length:
            data["length"] = piece.length
        # Omitted when it says nothing a reload would not measure again anyway,
        # so an unopened composite's pieces stay as small as their request.
        if piece.measured:
            data["measured"] = piece.measured
        out.append(data)
    return out


def _pieces_from(raw: dict) -> list[tuple[CompositePiece, int]]:
    """Stored pieces and the entry position each named, in order.

    The positions come back separately because the entries they name may not be
    parsed yet — :func:`_bind_composite_pieces` resolves them once they all are.
    A piece with no ``entry_index`` is a pad and gets ``-1``, which resolves to
    the same thing a missing source does: blank tiles, at the length recorded.

    Tolerant like the rest of this path — a malformed piece becomes a pad rather
    than failing the entry, so a composite opens with a hole in it and can be
    repaired in the dialog instead of being lost.
    """
    items = raw.get("pieces")
    if not isinstance(items, list):
        return []
    out: list[tuple[CompositePiece, int]] = []
    for item in items:
        if not isinstance(item, dict):
            out.append((CompositePiece(), 0))
            continue
        # ``length`` on a source *is* a range — the writer omits it for a whole
        # entry — so a file that carries one pins that run to those bytes.
        length = max(0, _int(item.get("length"), 0) or 0)
        offset = max(0, _int(item.get("offset"), 0) or 0)
        measured = max(0, _int(item.get("measured"), 0) or 0)
        out.append(
            (
                CompositePiece(offset=offset, length=length, measured=measured),
                _int(item.get("entry_index"), -1),
            )
        )
    return out


def _bind_composite_pieces(raw_entries: list, parsed: list[Entry | None]) -> None:
    """Point every parsed composite piece at the entry its position named.

    Resolved against ``parsed`` **including the entries that failed to parse**,
    for the reason :func:`_bind_tile_sources` gives: a stored index counts those,
    so resolving against the survivors would shift every piece past a dropped one
    onto its neighbour — and in a composite that is not a wrong binding but a
    wrong *picture*, since the runs are laid end to end.

    A position naming nothing, or naming something a composite may not read
    (:func:`~celpix.project.workspace.can_compose` — another composite, a map, a
    palette), leaves a pad of the recorded length. So a composite whose source
    has gone keeps its shape and every map indexing it still lands on the right
    tiles; the run is simply blank, and the load says so.
    """
    for raw, entry in zip(raw_entries, parsed, strict=True):
        if entry is None or entry.kind is not EntryKind.COMPOSITE:
            continue
        if not isinstance(raw, dict):
            continue
        pieces = []
        for piece, at in _pieces_from(raw):
            target = parsed[at] if 0 <= at < len(parsed) else None
            usable = target is not None and can_compose(entry, target)
            pieces.append(replace(piece, entry=target if usable else None))
        entry.pieces = tuple(pieces)


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


def _plugin_id(value: object, default: str) -> str:
    """A stored plugin or preset id, forwarded through any rename since.

    Applied on **load**, so the workspace only ever holds current ids and the
    next save writes them: a project touched after an upgrade stops depending on
    the alias table, while one that is never re-saved keeps opening through it
    indefinitely (:mod:`celpix.plugins.aliases`).

    An id this build has never heard of is left exactly as it was, renamed or
    not. It may belong to a plugin the user has yet to install, and rewriting it
    to a default would turn "your plugin is missing" into "your setting is gone".
    """
    return current_id(_str(value, default))


# -- path handling (docs/design/project-format.md §3) ----------------------
def _store_path(target: str, base_dir: str | None) -> str:
    """``target`` as stored in the project: relative to the project file with
    POSIX separators when on the same drive/tree, absolute otherwise.

    ``base_dir`` is ``None`` for the **clipboard** form of an entry
    (:func:`entries_payload`), which has no file to be relative to: a copy has to
    survive being pasted into a project saved somewhere else entirely, and only
    an absolute path means the same thing in both.
    """
    target = abspath(target)
    if base_dir is None:
        return target.replace(sep, "/")
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
