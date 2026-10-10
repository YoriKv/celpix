"""The clipboard bridge: tiles, colors and files-pane rows ⇄ the system clipboard.

A copy goes onto the OS clipboard in **two representations at once**, and which
one a paste uses decides how faithful it is:

- ``application/x-celpix-tiles`` — the tiles themselves, as indices (or ARGB for
  a direct-color codec) plus the palette they were seen through. Pasting this
  back into celPix is lossless: indices are the data, and a same-format paste
  moves them verbatim rather than round-tripping them through color.
- **An image**, so every other program on the machine sees a normal picture. Qt
  converts it to whatever the receiving app asks for (PNG, DIB, …).

Pasting reverses the priority: the celPix payload if it is there, otherwise any
image on the clipboard, which enters through the Qt-free import pathway
(:mod:`celpix.pipeline.importer`) and is fitted to the target palette. That is
what makes "draw a sprite in an image editor, paste it into the ROM" work.

This module is the **Qt bridge alone** — what goes on the clipboard and what comes
off it. The tile flavour's own byte format, and the validation that makes reading
one safe, are :class:`~celpix.core.tilepayload.TilePayload`'s: a payload arrives
from outside the process, so parsing it belongs with the rest of the model where
it can be tested without a window. The same split holds for the three JSON
flavours at the foot of this file: files-pane rows, whose records are
:func:`~celpix.project.projectfile.entries_payload`'s, one entry's input
bindings (Copy Inputs), and one entry's view settings
(:mod:`~celpix.project.view_settings`).

Those three are the flavours with a half that **cannot** be written down — a tile
binding, an input's source or an ENTRY palette's source is an entry, and an
entry is not a value — so a copy
of one leaves the objects behind in memory beside the payload
(:class:`_JsonFlavour`) rather than writing a position that the next drag would
invalidate.
"""

from __future__ import annotations

import json
import weakref
from uuid import uuid4

from PySide6.QtCore import QByteArray, QMimeData
from PySide6.QtGui import QGuiApplication, QImage

from celpix.core.palette import find_argb, format_argb
from celpix.core.tilepayload import TilePayload

# Our own clipboard flavours. Both names are private MIME types — no other
# program claims them, so their presence proves the copy came from celPix. Each
# flavour's payload carries a version bumped only on an incompatible change; a
# mismatch is ignored on paste, which falls back on the interchange
# representation alongside it (an image for tiles, hex text for colors).
TILES_MIME = "application/x-celpix-tiles"
# Palette colors travel under their own type (lossless ARGB) *and* as
# ``#RRGGBB``/``#AARRGGBB`` text, so a color copies to and pastes from any other
# program that speaks hex.
PALETTE_MIME = "application/x-celpix-palette"
PALETTE_PAYLOAD_VERSION = 1
# Rows of the files pane — a file, a slice, a palette registration — as the same
# references-and-settings records a project file holds
# (:func:`~celpix.project.projectfile.entries_payload`). Nothing outside celPix
# can act on one, so unlike the two flavours above there is no interchange
# representation beside it, only a plain-text listing of the paths for a paste
# into a text editor.
ENTRIES_MIME = "application/x-celpix-entries"
# One entry's input bindings (Copy Inputs / Paste Inputs), keyed by plugin id —
# the entry's own record, narrowed to its inputs. As celPix-only as the rows.
INPUTS_MIME = "application/x-celpix-inputs"
# One entry's view settings (Copy View Settings): formats, arrangement,
# compression preview and palette (:mod:`celpix.project.view_settings`).
VIEW_SETTINGS_MIME = "application/x-celpix-view-settings"

# This process, so a paste can tell a copy taken from the running editor from one
# taken from another window (or another day). It buys exactly one thing: it says
# the entry objects remembered beside the last copy (_COPIED_BINDINGS) are the
# ones *this* payload means. A payload read back off the clipboard carries it
# only when it is that last copy — see _JsonFlavour, which is where a copy's own
# token stands in for it on the way out and is answered with it on the way in.
SESSION_TOKEN = uuid4().hex


def put(payload: TilePayload | None, image: QImage) -> None:
    """Place a copy on the system clipboard in both representations."""
    mime = QMimeData()
    if not image.isNull():
        mime.setImageData(image)
    if payload is not None:
        mime.setData(TILES_MIME, QByteArray(payload.to_bytes()))
    QGuiApplication.clipboard().setMimeData(mime)


def take_payload() -> TilePayload | None:
    """The celPix tile payload on the clipboard, if a celPix copy put one there."""
    mime = QGuiApplication.clipboard().mimeData()
    if mime is None or not mime.hasFormat(TILES_MIME):
        return None
    return TilePayload.from_bytes(bytes(mime.data(TILES_MIME)))


def take_image() -> QImage | None:
    """Any image on the clipboard — the cross-application paste path."""
    mime = QGuiApplication.clipboard().mimeData()
    if mime is None or not mime.hasImage():
        return None
    image = QImage(mime.imageData())
    return None if image.isNull() else image


def has_content() -> bool:
    """Whether a paste could do anything — drives the Paste action's enabled state."""
    mime = QGuiApplication.clipboard().mimeData()
    return mime is not None and (mime.hasFormat(TILES_MIME) or mime.hasImage())


# -- palette colors --------------------------------------------------------
def color_text(argb: int) -> str:
    """One color as ``#RRGGBB`` (opaque) or ``#AARRGGBB`` (carries alpha)."""
    return format_argb(argb, alpha=None)


def put_colors(colors: list[int]) -> None:
    """Place palette colors on the system clipboard, lossless + as hex text."""
    mime = QMimeData()
    payload = json.dumps(
        {
            "version": PALETTE_PAYLOAD_VERSION,
            "colors": [c & 0xFFFFFFFF for c in colors],
        }
    ).encode("utf-8")
    mime.setData(PALETTE_MIME, QByteArray(payload))
    mime.setText(" ".join(color_text(c) for c in colors))
    QGuiApplication.clipboard().setMimeData(mime)


def _parse_palette_payload(raw: bytes) -> list[int] | None:
    """Our own palette payload → ARGB list; None for anything malformed."""
    try:
        head = json.loads(raw.decode("utf-8"))
        if head.get("version") != PALETTE_PAYLOAD_VERSION:
            return None
        return [int(c) & 0xFFFFFFFF for c in head["colors"]]
    except (ValueError, KeyError, TypeError, UnicodeDecodeError):
        return None


def take_colors() -> list[int] | None:
    """Palette colors from the clipboard: our lossless payload, else hex text."""
    mime = QGuiApplication.clipboard().mimeData()
    if mime is None:
        return None
    if mime.hasFormat(PALETTE_MIME):
        colors = _parse_palette_payload(bytes(mime.data(PALETTE_MIME)))
        if colors:
            return colors
    if mime.hasText():
        colors = find_argb(mime.text())
        if colors:
            return colors
    return None


def has_colors() -> bool:
    """Whether a palette paste could do anything — drives the action's enabled state."""
    mime = QGuiApplication.clipboard().mimeData()
    if mime is None:
        return False
    if mime.hasFormat(PALETTE_MIME):
        return True
    return mime.hasText() and bool(find_argb(mime.text()))


# -- files-pane entries ----------------------------------------------------
#: The half of an entry copy that cannot be written down: for each copied map,
#: the entry its tiles are bound to, **held by identity** like every other
#: reference to one (:class:`~celpix.project.workspace.TileSource`). A number
#: would not do, and that is the whole reason this exists — the rows are the
#: user's to rearrange, so any position recorded when the copy was taken names a
#: different entry the moment anything is dragged, closed or opened before the
#: paste. A copy can sit on the clipboard across all of that.
class _JsonFlavour:
    """One of the JSON clipboard flavours, and the entries a copy remembers.

    A payload names entries by a key of its own (a record's ``source_index``, a
    binding's position among the named ones) and the entries themselves are kept
    in :attr:`refs` — the payload's join key, not a live position: the two are
    written together and replaced together, and the session token on the payload
    is what says they still belong to each other.

    **A token per copy, not per process.** :attr:`refs` holds the last copy's
    entries only, and a clipboard history (Win+V, a desktop's clipboard manager)
    can put an *earlier* copy of ours back — same process, so a process token
    would vouch for it, and its keys would be read against the later copy's
    entries. So a payload written with :data:`SESSION_TOKEN` goes out under a
    token minted for that copy, and :meth:`take` answers with
    :data:`SESSION_TOKEN` only for the copy whose entries are the ones held; an
    earlier one keeps its own token and pastes unbound, like a foreign copy.

    **Weak**, so a copy taken and then forgotten about does not pin a closed
    entry's document in memory for the rest of the session. An entry that has
    gone that thoroughly is one nothing can be bound to anyway, and the paste
    treats a dead reference exactly as it treats a copy from another window:
    unbound.

    Only this flavour is read back: there is no foreign representation a
    payload could be reconstructed from, so text on the clipboard is never read
    as a paste (a path alone says nothing about how to read the file, and
    guessing would turn an unrelated copied filename into an entry).
    """

    def __init__(self, mime: str) -> None:
        self.mime = mime
        self.refs: dict[int, weakref.ref] = {}
        #: The token the last copy went out under, or None when it named another
        #: session (:meth:`put`).
        self.token: str | None = None

    def put(
        self, payload: dict, remembered: dict[int, object], text: str | None = None
    ) -> None:
        """Place ``payload`` on the clipboard, remembering ``remembered`` beside it.

        Replaced here rather than beside the call so the two cannot be written
        out of step. ``text`` is an optional plain-text half for other programs.
        """
        self.refs.clear()
        self.refs.update(
            {key: weakref.ref(target) for key, target in remembered.items()}
        )
        self.token = None
        if payload.get("session") == SESSION_TOKEN:
            self.token = uuid4().hex
            payload = {**payload, "session": self.token}
        mime = QMimeData()
        mime.setData(self.mime, QByteArray(json.dumps(payload).encode("utf-8")))
        if text is not None:
            mime.setText(text)
        QGuiApplication.clipboard().setMimeData(mime)

    def take(self) -> dict | None:
        """The payload on the clipboard, if a celPix copy of this flavour is there."""
        mime = QGuiApplication.clipboard().mimeData()
        if mime is None or not mime.hasFormat(self.mime):
            return None
        try:
            payload = json.loads(bytes(mime.data(self.mime)).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        if self.token is not None and payload.get("session") == self.token:
            payload["session"] = SESSION_TOKEN  # the copy :attr:`refs` belongs to
        return payload

    def remembered(self) -> dict[int, object]:
        """The entries the last copy remembered, minus any since freed."""
        live = {key: ref() for key, ref in self.refs.items()}
        return {key: target for key, target in live.items() if target is not None}

    def has(self) -> bool:
        """Whether a paste of this flavour could do anything."""
        mime = QGuiApplication.clipboard().mimeData()
        return mime is not None and mime.hasFormat(self.mime)


_ENTRIES = _JsonFlavour(ENTRIES_MIME)
_INPUTS = _JsonFlavour(INPUTS_MIME)
_VIEW_SETTINGS = _JsonFlavour(VIEW_SETTINGS_MIME)
#: The bound entries the last entry copy remembered (:class:`_JsonFlavour`).
_COPIED_BINDINGS = _ENTRIES.refs


def put_entries(payload: dict, paths: list[str], bindings: dict[int, object]) -> None:
    """Place copied entry records on the clipboard, plus ``paths`` as text.

    The text half is not a paste route back in — an entry is a reference *and*
    its settings, and a bare path is only the first of those. It is there so a
    copy can be dropped into a shell, a bug report or a notes file, which is what
    a user reaches for the moment they want to say *which* files a project holds.
    ``bindings`` is the half that cannot be written down (:class:`_JsonFlavour`).
    """
    _ENTRIES.put(payload, bindings, "\n".join(paths))


def take_bindings() -> dict[int, object]:
    """The bound entries the last entry copy remembered, minus any since freed.

    Only meaningful for a payload this process wrote — the caller checks that
    against the payload's session token before asking.
    """
    return _ENTRIES.remembered()


def take_entries() -> dict | None:
    """The entry payload on the clipboard, if a celPix copy put one there."""
    return _ENTRIES.take()


def has_entries() -> bool:
    """Whether an entry paste could do anything — drives the action's state."""
    return _ENTRIES.has()


def put_inputs(payload: dict, sources: dict[int, object]) -> None:
    """Place one entry's input bindings on the clipboard (Copy Inputs).

    ``sources`` are the entries the bindings name, by the binding's position
    among the named ones.
    """
    _INPUTS.put(payload, sources)


def take_inputs() -> dict | None:
    """The bindings payload on the clipboard, if Copy Inputs put one there."""
    return _INPUTS.take()


def take_input_sources() -> dict[int, object]:
    """The entries the last Copy Inputs remembered, minus any since freed."""
    return _INPUTS.remembered()


def has_inputs() -> bool:
    """Whether Paste Inputs could do anything — drives the row's state."""
    return _INPUTS.has()


def put_view_settings(payload: dict, sources: dict[int, object]) -> None:
    """Place one entry's view settings on the clipboard (Copy View Settings).

    ``sources`` holds the entry an ENTRY palette reads its colours from, under
    the position the payload names it by.
    """
    _VIEW_SETTINGS.put(payload, sources)


def take_view_settings() -> dict | None:
    """The view-settings payload on the clipboard, if Copy View Settings put one
    there."""
    return _VIEW_SETTINGS.take()


def take_view_settings_sources() -> dict[int, object]:
    """The entries the last Copy View Settings remembered, minus any since freed."""
    return _VIEW_SETTINGS.remembered()


def has_view_settings() -> bool:
    """Whether Paste View Settings could do anything — drives the action's state."""
    return _VIEW_SETTINGS.has()
