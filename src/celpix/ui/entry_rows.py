"""How a Files dock row reads: its label, its tooltip, its icons and its wash.

The presentation half of :class:`~celpix.ui.file_list_panel.FileListPanel`: a
row is redrawn whole from its entry whenever anything about the entry moves
(:meth:`EntryRowsMixin._refresh_item`), so what a row says is a function of the
entry, the registry naming its container, and whether it is the one on the
canvas. The tooltip's lines are plain functions of the entry
(:func:`missing_lines` and its neighbours); the icons are baked from the icon
font in the theme's colours and cached against the palette and the screen scale
they were baked for.

Mixed into the panel, whose ``_registry``, ``_current``, ``_items`` and
``_icons`` it reads.
"""

from __future__ import annotations

from PySide6.QtCore import QSize
from PySide6.QtGui import QBrush, QColor, QIcon, QPalette, QPixmap
from PySide6.QtWidgets import QTreeWidgetItem

from celpix.core.address import format_hex
from celpix.core.capabilities import ContentKind
from celpix.core.notices import Notice
from celpix.plugins.base import NO_COMPRESSION
from celpix.plugins.detect import compression_label, container_label
from celpix.project.workspace import (
    Entry,
    EntryKind,
    LoadFailure,
    data_missing,
    entry_notices,
    entry_palette_path,
    load_failed,
    palette_missing,
)
from celpix.ui.icon_font import icon_cache_key, icon_pixmap
from celpix.ui.icons import Icon
from celpix.ui.theme import ERROR_INK, WARNING_INK
from celpix.ui.widgets import wrap_lines

__all__ = [
    "ICON_H",
    "ICON_W",
    "STATUS_COL",
    "STATUS_W",
    "EntryRowsMixin",
    "closed_palette_lines",
    "failure_lines",
    "files_hint",
    "missing_lines",
    "notice_lines",
]

# Translucent amber behind an entry that needs the user's attention — a missing
# referenced file, or a container that had to assume something. Reads as a
# warning over either light or dark row backgrounds without fighting the
# selection highlight. Which of the two it is, is what the status icon says.
_MISSING_HIGHLIGHT = QBrush(QColor(255, 193, 7, 70))

# The same, in the error ink, behind an entry that would not open at all: the
# amber says "opened, but look", and a row that did not open is a different
# answer to "can I work on this" — so it wears a different colour, not only a
# different icon. Alpha matched to the amber so the two sit at one weight.
_FAILED_HIGHLIGHT = QBrush(
    QColor(ERROR_INK.red(), ERROR_INK.green(), ERROR_INK.blue(), 70)
)

# Alpha for the tint behind the entry currently on the canvas (the theme's
# Highlight, see _open_entry_wash). Deliberately fainter than the amber above:
# that one is asking to be dealt with, this one only says "here".
_OPEN_ENTRY_ALPHA = 45

# The slice/bookmark icon box. Narrower than the default 16px decoration so a
# centred icon sits close to the entry name rather than across a wide gap; the
# icons are painted at exactly this size so nothing is scaled.
ICON_W = 13
ICON_H = 16

# The status column, right of the name: the *kind* marker already owns column 0's
# icon slot (a slice's picture, a bookmark's ribbon), and a row can be both a
# slice and in trouble — so the two cannot share one slot.
STATUS_COL = 1
STATUS_W = ICON_W + 8  # the icon box plus breathing room from the name


def files_hint(entry: Entry) -> str:
    """`` (3)`` for a region joined from several files; ``""`` for one file.

    A row is named after its *first* file, which alone would read as the whole
    of what the entry holds — so how many files it really is goes beside the
    name, where a name is being read. The whole count rather than how many
    *extra* there are: "(3)" answers the question the row raises without the
    reader having to add one to it. The paths themselves are long and belong
    in the tooltip. Whole files only: a slice carries its parent's list but is
    one region cut out of the join, so counting the parent's chips on its row
    would answer a question the row isn't asking.
    """
    if entry.kind is not EntryKind.FILE or not entry.extra_paths:
        return ""
    return f" ({len(entry.paths)})"


def missing_lines(entry: Entry) -> str:
    """The tooltip lines for whichever of the entry's files is gone; ``""``
    when both are there.

    Which one is named matters more than that something is: a graphic whose
    *palette* file moved still opens and still draws (on the default
    palette), so a row flagged with the same wording as one whose own bytes
    are gone sends the user looking for a file that never went anywhere. The
    palette line carries its path too — unlike the data file, it is nowhere
    else in the tooltip.
    """
    lines = []
    if data_missing(entry):
        # A slice/bookmark has no file of its own: what's missing is the
        # parent it reads through, which is what the user has to go and find.
        lines.append(
            {
                EntryKind.PALETTE: "Palette file is missing",
                EntryKind.SLICE: "Parent file is missing",
                EntryKind.BOOKMARK: "Parent file is missing",
            }.get(entry.kind, "File is missing")
        )
    # Only a palette with a file behind it: one read from another entry that
    # has been closed is not a missing file, and Locate cannot find it
    # (:func:`closed_palette_lines`).
    path = entry_palette_path(entry)
    if path is not None and palette_missing(entry):
        lines.append(f"Palette file is missing:\n  {path}")
    return "".join(f"\n{line}" for line in lines)


def closed_palette_lines(entry: Entry) -> str:
    """The tooltip lines for a palette read from an entry that has been
    closed; ``""`` otherwise.

    Worded as a notice rather than a missing file, because that is what it
    is: the graphic still opens and draws, on the default palette, exactly as
    a map bound to a closed bank does, and what brings the colours back is
    reopening the entry (an undo of the close) or picking another source —
    no file went anywhere. The name is the one thing still to hand: the
    degraded source keeps the closed entry, which is what an undo restores.
    """
    source = entry.missing_palette
    if source is None or source.entry is None:
        return ""
    return (
        f"\nPalette is read from {source.entry.name}, which is not open"
        "\n  The default palette is shown until it is reopened,"
        "\n  or another source is picked in the Palette dock."
    )


def failure_lines(failure: LoadFailure) -> str:
    """The tooltip lines for an entry that would not open: a heading, then
    the failure indented under it like a notice's detail, then what to do.

    Wrapped here rather than where it was recorded, because the summary is a
    stage's own words - an exception's message can run to any length - and
    the tooltip is the one place it is shown that cannot wrap for itself.
    """
    lines = "\nDid not open:"
    lines += "".join(f"\n  {line}" for line in wrap_lines(failure.summary).split("\n"))
    return lines + "\nChange what it reads to try again"


def notice_lines(notice: Notice) -> str:
    """One notice as tooltip lines: the summary flush, its detail indented.

    The indent is what keeps a list of several readable — without it the
    detail of one runs straight into the summary of the next. Both are
    already hard-wrapped by whoever wrote them, per the tooltip rule (Qt
    never wraps a plain-text tooltip), and two extra columns keep them inside
    it.
    """
    lines = f"\n{notice.summary}"
    if notice.detail:
        lines += "".join(f"\n  {line}" for line in notice.detail.split("\n"))
    return lines


class EntryRowsMixin:
    """The row-drawing half of the Files dock; see the module docs."""

    def _container_hint(self, entry: Entry) -> str:
        """`` (TIFF)`` for a file read through a container; ``""`` otherwise.

        Files and palettes (a slice and a bookmark have no container of their
        own), and only when one is actually in use: tagging the great majority of
        rows "(Raw binary file)" would cost every row width to say nothing. So the
        hint's presence is itself the signal that this file is not being read
        literally — which on a palette is exactly the thing worth seeing, since
        the framing decides how many of its colors are real.
        """
        if self._registry is None or entry.kind not in (
            EntryKind.FILE,
            EntryKind.PALETTE,
        ):
            return ""
        label = container_label(self._registry, entry.container_id)
        return f" ({label})" if label else ""

    def _refresh_item(self, entry: Entry, item: QTreeWidgetItem) -> None:
        # The label is just the name (default slice names already read as
        # "offset (length) compression"), plus how many files join onto it and the
        # container hint on a file; coordinates live in the tooltip so a
        # custom-named slice stays inspectable without cluttering the list.
        unsaved = entry.pixel_dirty or entry.palette_dirty
        name = f"{entry.name}{files_hint(entry)}{self._container_hint(entry)}"
        item.setText(0, f"● {name}" if unsaved else name)
        tip = entry.path
        if entry.kind is EntryKind.FILE and entry.extra_paths:
            # Numbered, because the order is what the join uses and is the one
            # thing about a multi-file region a user cannot check anywhere else.
            tip = f"{len(entry.paths)} files joined end to end:\n" + "\n".join(
                f"{n}. {path}" for n, path in enumerate(entry.paths, 1)
            )
        if (
            entry.kind in (EntryKind.FILE, EntryKind.PALETTE)
            and self._registry is not None
        ):
            # The full container name, since the list column only had room for a tag.
            full = container_label(self._registry, entry.container_id, short=False)
            if full:
                tip += f"\nContainer {full}"
            # A file unpacked whole shows a stream no byte of the file holds —
            # said here, since a slice says it in its default name and a file's
            # name says nothing.
            if entry.compression_id != NO_COMPRESSION:
                scheme = compression_label(self._registry, entry.compression_id)
                tip += f"\nDecompressed with {scheme}"
        marker, what = self._entry_marker(entry)
        # Always set, empty included: a row keeps whatever icon it was last given,
        # so a kind that no longer wears one has to say so rather than leaving the
        # previous icon behind it.
        item.setIcon(0, marker)
        if what:
            tip += f"\n{what}"
        if entry.kind is EntryKind.SLICE:
            # A nested slice's offset counts in its parent slice's decoded bytes,
            # not in the file the path above names — said, or the number reads
            # as a file position.
            within = (
                f" in {entry.parent_entry.name}"
                if entry.parent_entry is not None
                else ""
            )
            tip += f"\nOffset {format_hex(entry.slice_offset)}{within}\nLength " + (
                format_hex(entry.slice_length, None)
                if entry.slice_length is not None
                else "to be discovered"
            )
            if entry.match_parent:
                tip += ", matching its parent"
        elif entry.kind is EntryKind.BOOKMARK:
            tip += (
                f"\nBookmark at {format_hex(entry.slice_offset)}\nDouble-click to jump"
            )
        elif entry.kind is EntryKind.PALETTE:
            if entry.palette_preset_id is not None:
                tip += f"\nFormat {entry.palette_preset_id.rsplit('.', 1)[-1]}"
            tip += (
                "\nDouble-click to use as the current palette;"
                "\nOpen Swatches shows its colors as a sheet"
            )
        if unsaved:
            # Name which pathway is pending: a palette edit writes to a different
            # file than the entry's own data, so "unsaved changes" alone would
            # misreport a color tweak as a change to the graphic.
            what = (
                "changes"
                if entry.pixel_dirty and not entry.palette_dirty
                else "palette changes"
                if entry.palette_dirty and not entry.pixel_dirty
                else "changes (data + palette)"
            )
            tip += f"\nUnsaved {what}"
        # Three conditions a row has to own up to, in the order they win. A file
        # it references (its own, or its palette) has moved; its last load
        # failed; or it opened, but a stage had to drop, assume or substitute
        # something. Each has its own icon, because the fixes differ — go and
        # find the file, change what the entry reads (or fix the plugin), read
        # what the container did — and the first two make the entry inert while
        # the third leaves it working. Missing wins over the others: a file that
        # isn't there cannot have been read, so a failure or a notice on the
        # entry is from an older load. A failed entry has no document and so no
        # notices, which is why the two never meet.
        #
        # The whole explanation goes in the tooltip. It is the one place a user
        # already looks to ask "what is wrong with this row", so a notice belongs
        # there rather than somewhere else they have to be told to look — and
        # for a failure it is the *only* place: the dialog was shown once, when
        # the load was tried, and every click after that lands here.
        notes = entry_notices(entry)
        warnings = [n for n in notes if n.is_warning]
        status: QIcon | None = None
        wash = QBrush()
        gone = missing_lines(entry)
        if gone:
            tip += gone + "\nFile ▸ Locate missing files"
            status = self._missing_icon()
            wash = _MISSING_HIGHLIGHT
        elif (failure := load_failed(entry)) is not None:
            tip += failure_lines(failure)
            status = self._failed_icon()
            wash = _FAILED_HIGHLIGHT
        elif entry.parent_kind is EntryKind.SLICE and entry.parent_entry is None:
            # A nested slice that lost its parent slice has nothing to read, which
            # is a failure it will have the first time it is opened — said now,
            # before a click, since nothing about it can change until then.
            tip += (
                "\nIts parent slice is not in the project,"
                "\nso there are no bytes to read it from"
            )
            status = self._failed_icon()
            wash = _FAILED_HIGHLIGHT
        else:
            # Every notice, not only the warnings that earn the icon: an info one
            # raises no marker of its own but is still worth reading once here.
            # A closed palette source ranks with them — the entry works, on the
            # default palette — so it wears their mark rather than the
            # missing-file one.
            closed = closed_palette_lines(entry)
            tip += closed + "".join(notice_lines(n) for n in notes)
            if warnings or closed:
                status = self._notice_icon()
                wash = _MISSING_HIGHLIGHT
        # Which row the canvas is showing, kept visible after the *selection*
        # has moved off it - clicking a palette to apply it, or a bookmark to
        # read its offset, leaves the list highlighting something that is not
        # what is on screen. A problem wash outranks it: it is rarer, it is
        # actionable, and the shown entry is still the one the tooltip and title
        # name.
        if status is None and entry is self._current:
            wash = self._open_entry_wash()
        item.setBackground(0, wash)
        item.setBackground(STATUS_COL, wash)
        item.setIcon(STATUS_COL, status if status is not None else QIcon())
        item.setToolTip(0, tip)
        # The icon is the thing a user points at to ask what is wrong with this
        # one, so it has to answer rather than showing nothing.
        item.setToolTip(STATUS_COL, tip)

    def _open_entry_wash(self) -> QBrush:
        """The tint behind the entry the canvas is showing.

        The theme's own Highlight, at an alpha low enough to read as a tint
        rather than a second selection - the real selection sits on top of it
        whenever the two are the same row, and must stay the stronger of the
        two. Taken from the palette rather than fixed like the amber warning,
        since this one is a *neutral* marker and should follow the theme's accent
        (:meth:`_bake_icons` repaints every row when that changes).
        """
        color = QColor(
            self.palette().color(
                QPalette.ColorGroup.Active, QPalette.ColorRole.Highlight
            )
        )
        color.setAlpha(_OPEN_ENTRY_ALPHA)
        return QBrush(color)

    def _ribbon_icon(self) -> QIcon:
        """The bookmark marker: a flag icon in the theme's accent color."""
        return self._icon(Icon.FLAG, role=QPalette.ColorRole.Highlight)

    def _entry_marker(self, entry: Entry) -> tuple[QIcon, str]:
        """The icon a row wears, and what the tooltip calls what it holds.

        Keyed on the entry's **content kind** first and its bounding second,
        because that is the order the icons answer in. A **map wears the icon of
        its layout whatever it is a window onto** — a whole file as much as a
        slice of a ROM — since which of the three layouts it holds settles what
        the entry can even do: a sprite map is placed by coordinate rather than
        laid into a grid, and a fontmap's cells read as words. The section header
        says *tilemap*; only the row can say *which*. Gating this on
        ``EntryKind.SLICE`` left every map opened as its own file — which is most
        of them — with no icon at all.

        The maps share one motif and differ in how its cells sit: an even
        lattice, the same cells loose at free offsets and sizes, and the lattice's
        columns fused into lines of text. Three slight variants read as a family;
        the tooltip carries the name, since an icon that subtle is a reminder
        rather than an introduction.

        The two exceptions come first and last. A **bookmark** keeps the ribbon
        even on a map: it marks a position rather than content, and the ribbon is
        what tells it from the slices it sits among. A **pixel slice** is its own
        little graphic, and the framed-picture icon is the universal symbol for
        that — while a pixel *file* wears none, its name and its section being the
        whole of what there is to say.

        The variant is the **format's** declaration, exactly as the window's
        ``_tilemap_is_sprite`` / ``_tilemap_is_fontmap`` read it: a row has to draw
        before its entry is loaded, and a map with nothing bound yet is still an
        object, or still a string.
        """
        if entry.kind is EntryKind.BOOKMARK:
            return self._ribbon_icon(), ""
        if entry.content_kind is ContentKind.TILEMAP:
            glyph, what = {
                "sprite": (Icon.GRID_LARGE, "Sprite map"),
                "text": (Icon.GRID_ROWS, "Fontmap"),
            }.get(self._tilemap_layout(entry), (Icon.GRID, "Tilemap"))
            return self._icon(glyph, role=QPalette.ColorRole.Text), what
        if entry.kind is EntryKind.SLICE:
            return self._icon(Icon.IMAGE, role=QPalette.ColorRole.Text), ""
        return QIcon(), ""

    def _tilemap_layout(self, entry: Entry) -> str:
        """What ``entry``'s cell format calls its layout — ``""`` for a plain grid.

        Empty for a format this build hasn't got, and for the panel built with no
        registry at all: an unrecognised map is still a map, and the grid icon is
        the honest thing to draw for one.
        """
        if self._registry is None or not entry.tilemap_preset_id:
            return ""
        try:
            preset = self._registry.preset(entry.tilemap_preset_id)
        except KeyError:
            return ""
        return str(preset.params.get("layout") or "")

    def _missing_icon(self) -> QIcon:
        """A question mark: this entry's file is unaccounted for, and the fix is
        to go and find it (File ▸ Locate missing files). Deliberately not a cross
        — at this size, next to rows the user can close, a cross reads as a close
        button rather than a state."""
        return self._icon(Icon.QUESTION, tint=WARNING_INK)

    def _failed_icon(self) -> QIcon:
        """A ringed exclamation in the error ink: this entry would not open, and
        its tooltip says which stage refused and why. The stronger relative of
        the notice mark - same exclamation, ringed and red - because the row is
        making the stronger claim: not "look", but "this one does not work"."""
        return self._icon(Icon.ERROR, tint=ERROR_INK)

    def _notice_icon(self) -> QIcon:
        """An exclamation mark: the entry opened, but a stage had to drop, assume
        or substitute something on the way in, and the row's own tooltip spells
        out what."""
        return self._icon(Icon.EXCLAMATION, tint=WARNING_INK)

    def _icon(  # noqa: ANN001 - role is a QPalette.ColorRole
        self, glyph: Icon, *, role=None, tint: QColor | None = None
    ) -> QIcon:
        """A baked icon, kept until what it was rasterized from moves.

        Keyed on the glyph, since each one is baked in exactly one color: either
        a theme ``role`` (which follows the palette) or a fixed ``tint``.
        """
        # A cache baked against another palette or scale is dropped here too:
        # a row can be drawn between the change and the event reporting it, and
        # the key is only moved by the re-bake that event triggers.
        if icon_cache_key(self) != self._icon_key:
            self._icons.clear()
        icon = self._icons.get(glyph)
        if icon is None:
            icon = (
                self._role_icon(glyph, role)
                if role is not None
                else self._tinted_icon(glyph, tint)
            )
            self._icons[glyph] = icon
        return icon

    def _bake_icons(self) -> None:
        """Drop the baked icons and re-draw every row against the new palette.

        What the icons were rasterized *from* has moved — the palette (a theme
        switch) or the screen's device pixel ratio (the window dragged to a
        differently scaled monitor) — and both are baked into the finished
        pixmaps, so a cache kept across either would show yesterday's color at
        the wrong resolution (:class:`~celpix.ui.widgets.IconBaker`). The rows
        are redrawn, not only the cache emptied, because a row keeps the icon it
        was given until it is given another.
        """
        self._icons.clear()
        for entry, item in self._items.items():
            self._refresh_item(entry, item)

    def _role_icon(self, glyph: Icon, role: QPalette.ColorRole) -> QIcon:
        """A bundled icon in a **theme** color — what the kind markers use.

        The color comes from the **Active** group explicitly: an entry's marker
        shouldn't wear the dimmed inactive variant (on Windows the inactive
        Highlight is a flat gray) merely because the window happened to be
        unfocused when the icon was first built and cached.
        """
        return self._tinted_icon(
            glyph, self.palette().color(QPalette.ColorGroup.Active, role)
        )

    def _tinted_icon(self, glyph: Icon, color: QColor) -> QIcon:
        """``glyph`` from the icon font, in ``color``.

        The glyph arrives as ink on transparency, fitted to its own bounds (no
        baked-in margin to widen the gap to the entry name). We recolor to the
        given color — which is a palette role for the kind markers, keeping them
        theme-aware in light and dark, and the fixed warning amber for the status
        icons, whose whole job is to read as a warning in either theme — then
        fit the art, centred, into the icon box.

        Rasterized at the screen's **device** resolution, not the logical 13x16:
        a QIcon built from a single 1x pixmap has nothing better to offer a
        scaled display, so Qt would stretch that bitmap — and these marks are
        thin enough that the smear reads as a washed-out gray rather than the
        tint. The pixmap carries its ratio, so the icon still measures 13x16 in
        layout units.

        Two pixmaps, not one: a **selected** row is painted in the highlight
        color, and any ink chosen to read against the ordinary row background
        turns muddy on top of it — the warning amber worst of all. Qt asks a
        QIcon for its ``Selected`` variant when it draws the decoration of a
        selected item, so the second pixmap is the same icon in the highlighted
        text color. The shape still says *which* condition it is; only the ink
        follows the row.
        """
        icon = QIcon(self._icon_pixmap(glyph, color))
        icon.addPixmap(
            self._icon_pixmap(
                glyph,
                self.palette().color(
                    QPalette.ColorGroup.Active, QPalette.ColorRole.HighlightedText
                ),
            ),
            QIcon.Mode.Selected,
        )
        return icon

    def _icon_pixmap(self, glyph: Icon, color: QColor) -> QPixmap:
        """``glyph`` tinted ``color`` and centred in the icon box, at device scale."""
        return icon_pixmap(
            glyph, color, QSize(ICON_W, ICON_H), self.devicePixelRatioF()
        )
