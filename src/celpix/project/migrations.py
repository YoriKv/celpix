"""The project file's schema migrations: an older ``.celpix`` walked forward.

One function per version bump, run in sequence by :func:`_migrated`, so a file
written at any older version opens as the file it would be if it had been
written today. :func:`~celpix.project.projectfile.load_project` is the one
caller; what each bump means is the docstring of its step, and the format itself
is ``wiki/Project-File-Format.md``.
"""

from __future__ import annotations

from collections.abc import Callable

# -- migrations -----------------------------------------------------------
#
# One entry per version bump, keyed by the version it *reads*: ``_MIGRATIONS[n]``
# takes a document written at version ``n`` and returns it at ``n + 1``. They run
# in sequence, so a version-1 file opened by a version-7 build is walked forward
# one step at a time and no migration ever has to know about more than the bump
# it was written for.
#
# A migration only ever rewrites what a *rename or reshape* moved. It is not the
# place for a defaulted key or a widened range: reading those is already the
# tolerance in :func:`~celpix.project.projectfile._view_from` and its
# neighbours, and duplicating it here would leave two answers to maintain for
# the same question.


#: The v1 spelling of a view's :attr:`~celpix.core.document.ViewOptions.palette_row`.
#: A migration is the one place an *old* name has to survive verbatim, so it is
#: named here rather than typed inline — a project-wide rename of the current
#: spelling would otherwise quietly turn the migration below into a no-op.
_V1_PALETTE_ROW_KEY = "subpalette_row"


def _migrate_1_to_2(data: dict[str, object]) -> dict[str, object]:
    """v1 → v2: a view's ``subpalette_row`` is spelled ``palette_row``.

    The UI settled on one noun for the row a view draws through — the same
    "palette row" a cell, a sprite piece and the base offset already used — and
    the stored key followed it. Same meaning, same range: only the spelling
    moved, so an untouched v1 project opens exactly as it was left.
    """
    for entry in data.get("entries", []):
        if not isinstance(entry, dict):
            continue
        view = entry.get("view")
        if isinstance(view, dict) and _V1_PALETTE_ROW_KEY in view:
            # setdefault, not an unconditional write: a hand-edited file holding
            # both spellings is answered by the new one, which is what the reader
            # would have used anyway.
            view.setdefault("palette_row", view[_V1_PALETTE_ROW_KEY])
            del view[_V1_PALETTE_ROW_KEY]
    return data


def _migrate_2_to_3(data: dict[str, object]) -> dict[str, object]:
    """v2 → v3: entries may carry ``inputs`` — what they bind to the data their
    plugins declare they need from outside their own bytes.

    Purely additive, so there is nothing to rewrite: a v2 file is a v3 file
    with no bindings in it. The bump exists for the other direction — a v2 build
    opening a v3 project drops every binding on its next save, and the number
    is what makes it warn before it does (``docs/design/project-format.md`` §2).
    """
    return data


def _migrate_3_to_4(data: dict[str, object]) -> dict[str, object]:
    """v3 → v4: bindings, presets and palettes may use what a v3 build does not
    know — the RLE codec's ``size_word`` inputs, a sprite record's ``arrays``, an
    Offset palette on a composite (read from its first piece's file), and a
    palette read out of **another entry**'s bytes (``palette_mode: "entry"``
    beside ``palette: {"entry": <position>, "offset": N}``,
    ``docs/design/palette-editing.md``).

    Purely additive, so there is nothing to rewrite: every v3 file means the
    same at v4. A v3 composite could not hold an Offset palette through the UI,
    and one written by hand now reads the offset it names rather than falling
    back to the default colours. The bump exists for the other direction, as
    2 → 3's did. A v3 build fails those slices with a misleading message, draws
    those frames scrambled, parses the unknown palette mode as ``default``, and
    **drops the size-word bindings and the palette's entry reference on its next
    save**. The number is what makes it warn before it does
    (``docs/design/project-format.md`` §2).
    """
    return data


def _migrate_4_to_5(data: dict[str, object]) -> dict[str, object]:
    """v4 → v5: a registered palette file is a document of its own — it opens as
    a sheet of swatches and can be sliced (``docs/design/palette-editing.md``
    §2). A PALETTE entry may carry a ``session`` and a ``view``, a slice or
    bookmark cut from one says so with ``"parent": "palette"``, and ``current``
    may name a PALETTE entry.

    Purely additive, so there is nothing to rewrite: every v4 file means the
    same at v5. The bump exists for the other direction, as 3 → 4's did. A v4
    build reads a palette's slice as a slice of a graphics file over the same
    path, and **drops its ``parent`` on its next save**, after which even this
    build reopens it as a graphics file's slice. The number is what makes it warn
    before it does (``docs/design/project-format.md`` §2).
    """
    return data


def _migrate_5_to_6(data: dict[str, object]) -> dict[str, object]:
    """v5 → v6: an input binding may be a bare string — a **choice** input's
    option key (``docs/design/plugin-inputs.md`` §3) — and a slice may be cut
    from another slice, ``"parent": "slice"`` with a ``parent_index``
    (``docs/design/slices-and-parents.md`` §6).

    Purely additive, so there is nothing to rewrite: every v5 file means the
    same at v6. The bump exists for the other direction, as 4 → 5's did. A v5
    build skips a string binding as malformed, so the entry decodes with the
    choice's default rather than the option bound, and reads a nested slice as
    a slice of the file, its offset landing in the wrong bytes — and **drops
    both the binding and the parent on its next save**. The number is what
    makes it warn before it does (``docs/design/project-format.md`` §2).
    """
    return data


def _migrate_6_to_7(data: dict[str, object]) -> dict[str, object]:
    """v6 → v7: a tile binding may say how its map's indices number what they
    draw — ``tile_source.addressing``, ``"corner"`` or ``"ordinal"``
    (:class:`~celpix.core.tilemap.IndexAddressing`) — its ``base_index``
    counts in that unit, and a **palette** entry's ``inputs``, its compression
    preview's bindings, are read back.

    Nothing to rewrite here, though one number changes meaning: a v6 base
    counted cells or tiles even where the index counted records, and a v7 base
    counts records there. Which maps those are, and how many cells a record
    is, is a question for the registry, which a migration does not have — so
    :func:`~celpix.project.projectfile.load_project` only flags them
    (:attr:`~celpix.project.projectfile.LoadedProject.bases_count_elements`),
    and whoever opens the project re-counts them once its registry is final
    (:func:`~celpix.project.documents.count_bases_in_units`).

    The bump also serves the other direction, as 5 → 6's did. A v6 build
    ignores ``addressing``, so a map whose binding overrides its format's
    reading counts every index the format's way and draws the wrong tiles,
    reads a record base as cells, and never reads a palette entry's
    ``inputs``, so its preview decodes with the codec's defaults — and **drops
    both on its next save**. The number is what makes it warn before it does
    (``docs/design/project-format.md`` §2).
    """
    return data


def _migrate_7_to_8(data: dict[str, object]) -> dict[str, object]:
    """v7 → v8: a **file** entry may carry a ``compression_id`` of its own — the
    whole file is one compressed stream, unpacked on load and re-packed on save
    — and a **palette** entry a ``reshape_id``; a palette entry's ``font`` and
    ``palette_row_base`` are read back as well.

    Purely additive, so there is nothing to rewrite: every v7 file means the
    same at v8. The bump exists for the other direction, as 5 → 6's did. A v7
    build ignores a file's own compression, so it shows the packed bytes as the
    picture and lets a pixel edit write unpacked tiles over the stream; it reads
    every child of such a file — slice, bookmark, Offset palette, whose offsets
    count in the decoded buffer — against the packed bytes instead; it reads a
    reshaped palette's colours un-reshaped and writes an edit back in that
    order; and it **drops all of these on its next save**. The number is what
    makes it warn before it does (``docs/design/project-format.md`` §2).
    """
    return data


_MIGRATIONS: dict[int, Callable[[dict[str, object]], dict[str, object]]] = {
    1: _migrate_1_to_2,
    2: _migrate_2_to_3,
    3: _migrate_3_to_4,
    4: _migrate_4_to_5,
    5: _migrate_5_to_6,
    6: _migrate_6_to_7,
    7: _migrate_7_to_8,
}


def _migrated(data: dict[str, object]) -> tuple[dict[str, object], int | None]:
    """``data`` walked forward to
    :data:`~celpix.project.projectfile.PROJECT_VERSION`, and where it started.

    The second element is the version the file claimed when it needed migrating,
    and ``None`` when it did not — which is also the answer for a file from the
    future, since there is nothing to walk it forward *with*. Its own version
    survives on the document for the caller to report.
    """
    # The reader's own tolerant int, so a stray ``true`` is no version 1 here
    # either; imported late because the reader imports this module.
    from celpix.project.projectfile import _int  # noqa: PLC0415 — circular by nature

    stated = _int(data.get("version"), 1)
    version = stated
    while (migration := _MIGRATIONS.get(version)) is not None:
        data = migration(data)
        version += 1
        data["version"] = version
    return data, stated if version != stated else None
