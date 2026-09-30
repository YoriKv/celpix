"""The clipboard form of copied entries (``docs/design/project-format.md`` §6a).

A copy is the project file's own per-entry records — written and read by
:mod:`celpix.project.projectfile`'s entry writer and reader, so a paste can
never disagree with a save — plus the positions that turn back into objects
on paste: where each entry sat, how deep its slice chain went, and what its
tile binding, composite pieces and ENTRY palette named. Everything here is
Qt-free; the window puts the payload on the system clipboard.
"""

from __future__ import annotations

from dataclasses import dataclass

from celpix.project.inputs import Bindings
from celpix.project.workspace import Entry, EntryKind, TileSource, slice_links

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
    reason :func:`~celpix.project.projectfile._bind_tile_sources` leaves an
    unresolvable one at ``None``: a :class:`~celpix.project.workspace.TileSource`
    that says it is bound and names no entry is a state nothing downstream
    expects.
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
    #: The position an **ENTRY-mode palette** named, or ``None`` on a row whose
    #: palette comes from anywhere else — the same join again, for the fourth
    #: kind of reference a row can hold (``docs/design/palette-editing.md``).
    #: ``-1`` for a source that was not part of the copy. ``None`` rather than
    #: ``-1`` for "no such reference", so a row with an ordinary palette is left
    #: alone instead of having one resolved onto it.
    palette_source_index: int | None = None
    #: One ``(plugin id, key, position)`` per **input binding** that names an
    #: entry, in the order :func:`~celpix.project.inputs.iter_bindings` walks
    #: them — the same join again, for the third kind of reference a row can
    #: hold (``docs/design/plugin-inputs.md`` §3). ``-1`` for a source that
    #: was not part of the copy.
    input_sources: tuple[tuple[str, str, int], ...] = ()
    #: The position a **nested slice**'s parent slice sat at, or ``None`` on
    #: every row that is not one — the same join again, for the reference that
    #: says which slice this one was cut from. ``-1`` for a parent that was not
    #: part of the copy.
    parent_source_index: int | None = None
    #: How deep the row's **parent** sits: ``0`` for a child of a file or a
    #: palette, ``1`` for one nested in a slice of a file, and so on. Not a
    #: position, and not a join — the one thing a paste places a lone child by,
    #: since a kept offset counts from the same kind of buffer only at the same
    #: depth (``docs/design/slices-and-parents.md`` §6).
    parent_depth: int = 0


def _parent_depth(entry: Entry) -> int:
    """How many slice links sit between ``entry`` and its file — the links it
    can see, on a broken chain, and each of them once on a circular one."""
    links, intact = slice_links(entry)
    # A broken chain still has the one link that points nowhere.
    return len(links) + (0 if intact else 1)


def entries_payload(
    entries: list[Entry], all_entries: list[Entry], session: str
) -> dict[str, object]:
    """``entries`` as a clipboard payload — the project form, absolute-pathed.

    Deliberately the *same* per-entry shape a project file holds: a copied entry
    is a copied reference plus its settings, which is exactly what
    :func:`~celpix.project.projectfile._entry_dict` already states, and one
    writer means a paste can never carry less than a save does. What differs is
    only what a position can be resolved against, which is what ``session`` and
    the two indices below are for.

    ``session`` is a token identifying the running editor, and what it buys is
    named on :class:`CopiedEntry`: it says the entry objects this process
    remembered alongside the payload are the ones this payload means. A paste
    into another process has only the payload, and resolves bindings no further
    than the copy itself carries.
    """
    from celpix.project.projectfile import (
        _entry_dict,  # noqa: PLC0415 — circular by nature
    )

    positions = {id(entry): i for i, entry in enumerate(all_entries)}
    written = []
    for entry in entries:
        data = _entry_dict(entry, None, positions)
        data["source_index"] = positions.get(id(entry), -1)
        # Here and not in the project's own entry writer: a project states the
        # chain whole in ``parent_index``, where a lone copied child has only
        # this left.
        if entry.kind in (EntryKind.SLICE, EntryKind.BOOKMARK):
            data["parent_depth"] = _parent_depth(entry)
        written.append(data)
    return {
        "version": CLIPBOARD_VERSION,
        "session": session,
        "entries": written,
    }


def entries_from_payload(raw: object) -> list[CopiedEntry]:
    """A clipboard payload back into entries — ``[]`` for anything unusable.

    Tolerant per entry exactly as :func:`~celpix.project.projectfile.load_project`
    is: one unreadable record is dropped and the rest of the paste still lands.
    The whole payload is refused only where it is not ours to read at all — the
    wrong shape, or a version this build has no meaning for.
    """
    from celpix.project.projectfile import (  # noqa: PLC0415 — circular by nature
        _entry_from_dict,
        _int,
        _pieces_from,
        _tile_source,
    )

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
        # The bindings ride on the entry too, and the ones naming an entry are
        # handed back beside it the same way; the paste resolves or drops them.
        input_sources = entry._pending_input_sources
        entry._pending_input_sources = ()
        # A nested record is at least one level down whatever it says, so one
        # written without the key reads as exactly that.
        floor = 1 if entry.parent_kind is EntryKind.SLICE else 0
        depth = max(_int(record.get("parent_depth"), 0) or 0, floor)
        out.append(
            CopiedEntry(
                entry=entry,
                source_index=_int(record.get("source_index"), -1),
                tile_source=binding[0] if binding is not None else None,
                tile_source_index=binding[1] if binding is not None else -1,
                piece_sources=tuple(at for _piece, at in pieces),
                palette_source_index=_palette_entry_index(record),
                input_sources=input_sources,
                parent_source_index=(
                    _int(record.get("parent_index"), -1)
                    if entry.parent_kind is EntryKind.SLICE
                    else None
                ),
                parent_depth=depth,
            )
        )
    return out


def _palette_entry_index(raw: dict) -> int | None:
    """The entry position an entry-shaped ``palette`` block named, else ``None``.

    Handed back beside the record for the reason every other cross-entry
    reference is: the entry it names may not exist here at all, and a
    :class:`~celpix.project.workspace.PaletteSource` naming nothing is what the
    restore already degrades on.
    """
    from celpix.project.projectfile import _int  # noqa: PLC0415 — circular by nature

    palette = raw.get("palette")
    if not isinstance(palette, dict) or "entry" not in palette:
        return None
    return _int(palette.get("entry"), -1)


def inputs_payload(entry: Entry, all_entries: list[Entry], session: str) -> dict:
    """One entry's input bindings as a clipboard payload (Copy Inputs).

    The same per-plugin form the project file writes, so a paste lands exactly
    what a save would, and the same ``session`` token :func:`entries_payload`
    carries: a binding naming an entry is resolved through the objects the
    copying process remembered, when the paste is in that process.
    """
    from celpix.project.projectfile import (
        _inputs_dict,  # noqa: PLC0415 — circular by nature
    )

    positions = {id(e): i for i, e in enumerate(all_entries)}
    return {
        "version": CLIPBOARD_VERSION,
        "session": session,
        "inputs": _inputs_dict(entry, positions, None),
    }


def inputs_from_payload(
    raw: object,
) -> tuple[dict[str, Bindings], list[tuple[str, str, int]]]:
    """A Copy Inputs payload back into bindings, plus the named positions in
    the order the copying side numbered them; empty for anything unusable."""
    from celpix.project.projectfile import (
        _inputs_from,  # noqa: PLC0415 — circular by nature
    )

    if not isinstance(raw, dict) or raw.get("version") != CLIPBOARD_VERSION:
        return {}, []
    return _inputs_from(raw)


def payload_session(raw: object) -> str:
    """The session token a payload was written by — ``""`` when it has none."""
    from celpix.project.projectfile import _str  # noqa: PLC0415 — circular by nature

    return _str(raw.get("session"), "") if isinstance(raw, dict) else ""
