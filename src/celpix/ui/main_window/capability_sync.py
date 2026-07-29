"""Turning the capability table into what is on screen.

:data:`~celpix.core.capabilities.CAPABILITIES` says which controls a kind of
document supports; this applies it. One pass over one declared table, run from
the tail of the refresh cycle, in place of each control carrying its own
"...and not on a tilemap" clause inside whichever ``_sync_*`` method owns it
(``docs/design/tilemap-entry.md`` §4).

**Gating is a veto and never a grant** — for every control that has an owner.
One whose capability is present is left exactly as its own sync left it: paste
still depends on there being something in the clipboard, the transforms still on
a selection. That is what lets this run last without having to know why any
other pass disabled something.

The exception is :data:`_GATE_OWNS`: a handful of controls nothing else ever
enables, because until capabilities existed they applied everywhere. Vetoing one
of those would switch it off for the rest of the session, so for them the gate
owns enablement in both directions.

A control that would be **meaningless** on this kind is hidden; one that is
merely unavailable is disabled. The pixel tools are the clear case of the first:
a panel of brushes over a tilemap is not a disabled feature, it is furniture for
a different room.
"""

from __future__ import annotations

from celpix.core.capabilities import Capability, ContentKind, supports

# Which controls each capability gates, by attribute name. Names rather than
# widgets because they are created by a dozen different mixins in an order this
# module has no business depending on; a name that does not resolve is skipped,
# so a control that has not been built yet costs nothing.
_GATES: dict[Capability, tuple[str, ...]] = {
    Capability.PIXEL_EDIT: (
        "_tools_panel",
        "_edit_mode_action",
        "_toggle_edit_mode_action",
    ),
    Capability.PALETTE_REGIONS: (
        "_pin_palette_action",
        "_unpin_palette_action",
        "_unpin_all_action",
        "_show_palette_regions_action",
        "_show_palette_rows_action",
    ),
    Capability.TILE_REARRANGE: (
        "_rearrange_action",
        "_toggle_rearrange_action",
        "_show_rearranged_action",
    ),
    Capability.NAVIGATION: ("_tile_offset_bar",),
    Capability.IMPORT_IMAGE: ("_import_png_action",),
    Capability.CLIPBOARD: ("_copy_action", "_cut_action", "_paste_action"),
    # The codecs bar swaps its left half by content kind: a tilemap entry's bytes
    # are cells, so the pixel format and the compression preview say nothing
    # about it and the cell format takes their place. Three groups, three
    # actions, because a toolbar hides the action rather than the widget.
    Capability.PIXEL_CODEC: ("_pixel_codec_action",),
    Capability.TILEMAP_CODEC: ("_tilemap_codec_action",),
    Capability.COMPRESSION_SCAN: ("_compression_action",),
}

# The few whose absence means "not a thing here" rather than "not right now".
_HIDDEN = frozenset(
    {
        "_tools_panel",
        "_pixel_codec_action",
        "_tilemap_codec_action",
        "_compression_action",
    }
)

# Controls whose enabled state **nothing else manages**: they are built enabled
# and no ``_sync_*`` ever touches them, because until capabilities existed they
# applied to every document. For those the gate has to own enablement outright
# and switch them back on again — vetoing one would disable it for the rest of
# the session, since there is no owner to undo the veto.
#
# Every other name above has an owner that re-decides it on each refresh, so the
# gate only ever takes away and the owner keeps the last word on when it is
# available at all. Moving a control between the two sets is the whole of what
# it costs to give it an owner later.
_GATE_OWNS = frozenset(
    {
        "_edit_mode_action",
        "_show_palette_regions_action",
        "_show_palette_rows_action",
        "_show_rearranged_action",
        # Hidden groups have to be in here too, or the veto below would leave a
        # group disabled behind its own hiding and it would come back grey.
        "_pixel_codec_action",
        "_tilemap_codec_action",
        "_compression_action",
    }
)

# Capabilities a content kind *declares* but has no behaviour behind yet, so the
# shared control would run the pixel implementation over a document it does not
# fit. Suppressed here rather than removed from CAPABILITIES, because the table
# states the design and this states today.
#
# Empty: every capability either kind declares is implemented for it. The hook
# stays because the next kind added will need it before its behaviours land, and
# because an empty map is a clearer statement of that than no map at all.
_NOT_BUILT_YET: dict[ContentKind, frozenset[Capability]] = {}


class CapabilitySyncMixin:
    """Applies the capability table to the window's controls.

    A slice of :class:`~celpix.ui.main_window.window.MainWindow`, not a
    standalone object.
    """

    def _content_kind(self) -> ContentKind:
        """What the entry on screen holds — pixels when there is nothing open.

        Nothing open leaves the pixel controls as they were, which is what every
        other empty-state path already assumes: they are disabled for want of a
        document, not for want of a capability.
        """
        entry = self._workspace.current
        return entry.content_kind if entry is not None else ContentKind.PIXELS

    def _can(self, capability: Capability) -> bool:
        """Whether the entry on screen supports ``capability`` **today**.

        Both halves of the question: the table says whether the kind supports it
        at all, and :data:`_NOT_BUILT_YET` says whether celPix has the behaviour
        behind it for that kind yet.
        """
        kind = self._content_kind()
        if capability in _NOT_BUILT_YET.get(kind, frozenset()):
            return False
        return supports(kind, capability)

    def _sync_capabilities(self) -> None:
        """Switch off whatever the current entry has no capability for.

        Runs last in the refresh cycle so its veto is the final word — an
        earlier pass may have enabled a control on grounds that are true in
        general and beside the point for this kind of document.
        """
        for capability, names in _GATES.items():
            allowed = self._can(capability)
            for name in names:
                control = getattr(self, name, None)
                if control is None:
                    continue
                if name in _HIDDEN:
                    control.setVisible(allowed)
                if name in _GATE_OWNS:
                    control.setEnabled(allowed)
                elif not allowed:
                    # Never the other branch: see the module docstring on why
                    # this only ever takes away.
                    control.setEnabled(False)
