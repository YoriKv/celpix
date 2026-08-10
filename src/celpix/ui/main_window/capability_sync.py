"""Turning the capability table into what is on screen, and into which code runs.

Both halves of ``docs/design/tilemap-entry.md`` §4, in one module because they
have to agree about one thing — what the entry on screen holds
(:meth:`~CapabilitySyncMixin._content_kind`). Split across two, a control could
be gated as a tilemap's and dispatched as a pixel document's.

**Gating.** :data:`~celpix.core.capabilities.CAPABILITIES` says which controls a
kind of document supports; :meth:`~CapabilitySyncMixin._sync_capabilities`
applies it. One pass over one declared table, run from the tail of the refresh
cycle, in place of each control carrying its own "...and not on a tilemap" clause
inside whichever ``_sync_*`` method owns it.

Gating is a **veto and never a grant** — for every control that has an owner. One
whose capability is present is left exactly as its own sync left it: paste still
depends on there being something in the clipboard, the transforms still on a
selection. That is what lets this run last without having to know why any other
pass disabled something.

The exception is :data:`_GATE_OWNS`: a handful of controls nothing else ever
enables, because until capabilities existed they applied everywhere. Vetoing one
of those would switch it off for the rest of the session, so for them the gate
owns enablement in both directions.

A control that would be **meaningless** on this kind is hidden; one that is
merely unavailable is disabled. The stamp tool is the clear case of the first: a
tool for placing cells is not a feature switched off on a pixel document, it is
furniture for a different room. The pixel tools rail is the counter-example that
shows why the distinction is per *kind* and not per control — a map's pixels are
editable through it (``docs/design/tilemap-entry.md`` §8.4), so it is never
furniture for another room on a tilemap; what varies is whether *this* map has a
bank to paint into, which no per-kind table can say.

Two capabilities gate **in place** rather than through this pass, listed in
:data:`_GATED_IN_PLACE`, and the reason is worth stating because it decides where
any future gate goes. This pass can only show, hide and disable a *named*
control, all or nothing. That is the wrong instrument when the control is
*replaced* rather than switched off — the binding bar and the navigation bar are
two pages of one stack, so what a tilemap needs is the other page, not a greyed
one — or when the kind's answer only sharpens a condition some other ``_sync_*``
was weighing anyway, where arriving as a separate veto would say the same thing
twice. Both ask :meth:`~CapabilitySyncMixin._can` themselves instead, which is
the same table read from the other end.

Gating in place is not the same as a control that weighs more than its
capability does. The Cell spin needs cells this file can edit and a format with
an index field on top of ``STAMP``, and asks ``_can`` for itself — but ``STAMP``
is gated from the table all the same, because the mode it switches needs only
the kind's answer. A capability sits in exactly one of the three sets below; how
many conditions a given control weighs underneath it is a separate question.

**Dispatch.** :data:`_BEHAVIOURS` names which method implements each
:class:`Gesture` on each content kind, and
:meth:`~CapabilitySyncMixin._kind_handler` resolves it. Gating says *whether* a
control applies; this says *what it does*, which is the more interesting half —
several controls exist on both kinds and mean different things on each, and a
flip is the sharpest: pixels here, an attribute bit there.
"""

from __future__ import annotations

from enum import Enum, auto

from celpix.core.capabilities import Capability, ContentKind, supports


class Gesture(Enum):
    """One shared control whose *implementation* differs by content kind.

    The rows of ``docs/design/tilemap-entry.md`` §4's second table: something the
    user does the same way on every document, which means something different
    depending on what that document holds. Copy lifts tile bytes or cells; a flip
    rewrites pixels or toggles a mirror bit.

    Separate from :class:`~celpix.core.capabilities.Capability` because the two
    do not correspond. One capability covers several gestures — all four
    clipboard ones ride on ``CLIPBOARD`` — so the capability cannot key the
    dispatch, and a gesture is not a thing to gate: what a user switches off is
    Paste, not "paste, in its cell sense".
    """

    COPY = auto()
    CUT = auto()
    CLEAR = auto()
    PASTE = auto()
    TRANSFORM_TILES = auto()
    TRANSFORM_BLOCK = auto()
    ASSIGN_PALETTE_ROW = auto()


# Which controls each capability gates, by attribute name. Names rather than
# widgets because they are created by a dozen different mixins in an order this
# module has no business depending on; a name that does not resolve is skipped,
# so a control that has not been built yet costs nothing.
#
# A name may hold a **tuple** of controls rather than one: a menu group whose
# rows are gated as a unit and have no individual reason to be named here. The
# machinery is the same, applied to each of them.
_GATES: dict[Capability, tuple[str, ...]] = {
    # The two *unpin* gestures and the recolour toggle, and not the third gesture
    # beside them: pinning a row is what a pixel document does with a row it has
    # been given, so the gesture that gives one rides on PALETTE_ROW and is gated
    # by its own sync (see _GATED_IN_PLACE). Unpinning has no tilemap reading —
    # every cell has a row, so there is no "back to the view's" to return to.
    Capability.PALETTE_REGIONS: (
        "_unpin_palette_action",
        "_unpin_all_action",
        "_show_palette_regions_action",
    ),
    # The three switches are gated from here, but the tool's *state* is not
    # something this pass can reach: an armed tool has to be put down, not greyed
    # over. ``RearrangeMixin._rearrange_available`` asks :meth:`_can` for that,
    # the way the Cell spin does under STAMP (see _GATED_IN_PLACE's note) — and it
    # is also what re-decides these three, so none of them is _GATE_OWNS'.
    Capability.TILE_REARRANGE: (
        "_rearrange_action",
        "_toggle_rearrange_action",
        "_show_rearranged_action",
    ),
    Capability.CELL_LABELS: ("_show_tile_ids_action",),
    # The Edit Tiles mode. Hidden rather than greyed off a *pixel* document: a
    # tool for placing cells is not a feature switched off there, it is furniture
    # for a different room.
    # The Cell spin rides on this capability too and gates itself, because it
    # needs the format's word as well (see _GATED_IN_PLACE's note).
    Capability.STAMP: ("_stamp_action", "_toggle_stamp_action"),
    # Three surfaces onto the same view window, and all three have to go
    # together. The position bar is the visible one; the Navigate menu's
    # position and row-count rows are the same movements spelled as menu rows;
    # and New Slice from View carves out whatever that window covers, which is
    # the third member of the family COMPRESSION_SCAN belongs to — it reads the
    # window rather than moving it. The menu's *column* rows stay: a map's cell
    # width is its own live setting, which is why the kind's refusal in
    # ``capabilities.py`` names the row count and the position and not that.
    #
    # The **keys** those rows document are not gated here at all: they are routed
    # by an app-wide event filter rather than bound to these actions, so a
    # disabled row does not disarm them. They are refused at the one place every
    # position gesture lands instead
    # (:meth:`~...navigation.NavigationMixin._set_offset`), which is also what
    # covers the scrollbar and the address box.
    Capability.NAVIGATION: (
        "_tile_offset_bar",
        "_nav_window_actions",
        "_new_slice_from_view_action",
    ),
    Capability.CLIPBOARD: ("_copy_action", "_cut_action", "_paste_action"),
    # The codecs bar swaps its left half by content kind: a tilemap entry's bytes
    # are cells, so the pixel format and the compression preview say nothing
    # about it and the cell format takes their place. Three groups, three
    # actions, because a toolbar hides the action rather than the widget.
    Capability.PIXEL_CODEC: ("_pixel_codec_action",),
    Capability.TILEMAP_CODEC: ("_tilemap_codec_action",),
    Capability.COMPRESSION_SCAN: ("_compression_action",),
    # The whole Arrangement row, as one bar rather than five names: every control
    # on it states how a linear run of bytes is cut and grouped, and a tilemap
    # places nothing linearly, so they go or stay together.
    Capability.TILE_ARRANGEMENT: ("_arrange_toolbar",),
}

# Capabilities gated by their own ``_sync_*`` asking :meth:`_can`, because this
# pass cannot express what they need — see the module docstring on why a replaced
# control and a two-level condition both have to gate themselves.
#
# ``TILE_BINDING`` is the replaced control: the binding bar and the navigation bar
# are two pages of one stack, and this pass cannot ask for the other page.
# ``CELL_ROTATE`` is the milder of the two: a quarter turn is already refused on a
# non-square *tile*, so the kind's answer joins a condition the transform bar was
# weighing anyway rather than arriving as a separate veto
# (:meth:`~...transform.TransformMixin._sync_transform_actions`).
# ``STAMP`` is deliberately **not** here, though its Cell spin still asks
# :meth:`_can` itself. The capability's gate is the table's — it hides the Edit
# Tiles mode, which needs only the kind's answer — and the spin's extra
# conditions (cells this file can edit, a format with an index field) are a
# finer question underneath it rather than a second place the capability is
# gated. A capability sits in exactly one bucket; a control may still weigh more
# than that bucket says.
#
# ``IMPORT_IMAGE`` is here for a reason none of the others share, and it is the
# one to check a new gate against: its control has an owner that runs **more
# often than this pass does**. ``_sync_edit_actions`` re-decides Import from PNG
# on every selection change, and a selection changes without anything being
# re-rendered — so a veto applied at the tail of the refresh cycle was undone by
# the next click on a cell, and the row came back live on a tilemap until the
# following render. Veto-last only holds while every owner runs before this
# pass; where one does not, the owner has to ask
# (:meth:`~...selection.SelectionMixin._sync_edit_actions`).
#
# ``PALETTE_ROW`` is a two-level condition of the third sort: both kinds declare
# it, so the table's gate would always be true, while what the two controls
# underneath it actually need is finer and per-kind — a selection and, on a
# tilemap, a format with a palette field to write
# (:meth:`~...palette_regions.PaletteRegionsMixin._sync_pin_actions`).
#
# ``PIXEL_EDIT`` is the same shape and arrived at it the same way. Both kinds
# declare it — a tilemap's pixels are the bound entry's, and painting them is
# what ``tilemap-entry.md`` §8.4 is — so a gate here would always be true, while
# what the rail and the two mode toggles need is per *document*: a map with
# nothing bound has no bank to deposit into, and a sprite object has no cell
# under a canvas pixel at all. ``_pixel_edit_available`` asks ``_can`` and then
# both of those (:meth:`~...selection.SelectionMixin._pixel_edit_available`).
_GATED_IN_PLACE = frozenset(
    {
        Capability.TILE_BINDING,  # tilemap_bar._sync_tilemap_bar — the stack swap
        Capability.CELL_ROTATE,  # transform._sync_transform_actions
        Capability.PALETTE_ROW,  # palette_regions._sync_pin_actions
        Capability.PIXEL_EDIT,  # selection._pixel_edit_available
        Capability.IMPORT_IMAGE,  # selection._sync_edit_actions
    }
)

# Capabilities that gate nothing anywhere, and are **right** not to: both kinds a
# user can activate declare them, so a gate would be a condition that is always
# true. Written down rather than left as an absence, so the question "does this
# one gate anything?" has an answer here instead of in a grep.
#
# They are not dead. Each is a real claim about a kind — that a tilemap has its
# own palette to edit and its own colour format to read it in, that its cells are
# selectable, that it exports as a picture — and the claim is what a *third* kind
# would be measured against. ``PALETTE`` already declines four of them; it has no
# view of its own to gate, being applied rather than activated, which is why they
# stay inert for now.
_UNGATED = frozenset(
    {
        Capability.PALETTE_EDIT,
        Capability.PALETTE_CODEC,
        Capability.TILE_SELECT,
        Capability.CELL_FLIP,
        Capability.EXPORT_IMAGE,
        Capability.GRID,
        Capability.HEX_VIEW,
    }
)

# The few whose absence means "not a thing here" rather than "not right now".
#
# The position bar is one, and has to be: it is an accent-coloured rail with a
# custom handle, so a disabled one still paints in full colour — and its handle
# is still sized and placed from the *bound tile bank*, which is the only thing
# with a tile count here. That reads as a live, partly-scrolled navigator over a
# document that is always shown entire. It goes for the reason the navigation bar
# under the canvas goes rather than greying (``tilemap_bar.py``): the vertical
# half of one control cannot stay behind when the horizontal half is replaced.
_HIDDEN = frozenset(
    {
        "_stamp_action",
        "_toggle_stamp_action",
        "_tile_offset_bar",
        "_pixel_codec_action",
        "_tilemap_codec_action",
        "_compression_action",
        "_arrange_toolbar",
    }
)

# Hidden controls whose *enabled* state this pass must leave alone, because it
# already has an owner that answers a different question. The Arrangement bar is
# the case: it is greyed wholesale for an unavailable entry
# (:meth:`~...session.SessionMixin._set_document_ui_enabled`) and frozen wholesale
# while a scan runs (:meth:`~...compression.CompressionMixin._set_scan_ui`), so a
# veto here would strand it grey on the way back to a pixel entry, and putting it
# in :data:`_GATE_OWNS` instead would do the opposite — hand a bar back over a
# missing file, which is the grant this pass promises never to make. Hidden, it
# needs neither: an invisible bar has nothing left to switch off.
_VISIBILITY_ONLY = frozenset({"_arrange_toolbar"})

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
        "_show_palette_regions_action",
        "_show_tile_ids_action",
        # The Navigate menu's window rows: built enabled beside the keys they
        # document, and no ``_sync_*`` has ever had a reason to touch them.
        "_nav_window_actions",
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

# Which method implements each gesture on each content kind, by name — resolved
# through ``getattr`` for the reason :data:`_GATES` is: the implementations live
# on a dozen mixins this module must not import.
#
# An entry here is an **override, not the whole dispatch**. The shared method's
# own body is the ``PIXELS`` implementation: that is where each of these gestures
# has always been implemented, and lifting seven pixel bodies out into named
# methods purely to fill a second column would buy nothing but a second name for
# each. So a kind absent from this table runs the shared body, and what the table
# reads as is the list of gestures that mean something else somewhere.
_BEHAVIOURS: dict[ContentKind, dict[Gesture, str]] = {
    ContentKind.TILEMAP: {
        Gesture.COPY: "_copy_cells",
        Gesture.CUT: "_cut_cells",
        Gesture.CLEAR: "_clear_cells",
        Gesture.PASTE: "_paste_cells",
        Gesture.TRANSFORM_TILES: "_transform_cells",
        Gesture.TRANSFORM_BLOCK: "_transform_cell_block",
        Gesture.ASSIGN_PALETTE_ROW: "_assign_cell_palette_row",
    },
}

# Which capability each gesture rides on. Not consulted at dispatch time — it
# states the one invariant that ties the two halves of this module together, and
# a test asserts it: a kind with an implementation of a gesture must declare the
# capability that gates it. Break that and the control is switched off over an
# implementation that was there all along, which is exactly the drift the table
# exists to prevent, and is not visible from either side alone.
_GESTURE_CAPABILITY: dict[Gesture, Capability] = {
    Gesture.COPY: Capability.CLIPBOARD,
    Gesture.CUT: Capability.CLIPBOARD,
    Gesture.CLEAR: Capability.CLIPBOARD,
    Gesture.PASTE: Capability.CLIPBOARD,
    # The transform gestures carry the operation, so they are offered wherever
    # *any* of the four is: a tilemap flips and cannot be turned, and which of
    # the two a given button asks for is settled a level up, on the bar
    # (``CELL_ROTATE`` in :data:`_GATED_IN_PLACE`).
    Gesture.TRANSFORM_TILES: Capability.CELL_FLIP,
    Gesture.TRANSFORM_BLOCK: Capability.CELL_FLIP,
    # One gesture, two stores: the pixel body writes a pinned region into the
    # project, the tilemap one writes the row into the cells the file already
    # keeps it in. Both kinds declare the capability, which is what says the
    # question — "give this selection a row of its own" — is the same one.
    Gesture.ASSIGN_PALETTE_ROW: Capability.PALETTE_ROW,
}


class CapabilitySyncMixin:
    """Applies the capability table to the window's controls, and to its dispatch.

    A slice of :class:`~celpix.ui.main_window.window.MainWindow`, not a
    standalone object.
    """

    def _content_kind(self) -> ContentKind:
        """What the entry on screen holds — pixels when there is nothing open.

        Nothing open leaves the pixel controls as they were, which is what every
        other empty-state path already assumes: they are disabled for want of a
        document, not for want of a capability. It is also the honest answer for
        a bar that configures the *next* open — an empty window is waiting for a
        pixel file until told otherwise, and the tilemap controls go.
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

    def _kind_handler(self, gesture: Gesture):  # noqa: ANN201 - a bound method
        """The current kind's own implementation of ``gesture``, or None.

        None means **the caller's own body is the implementation**, which is the
        pixel one for every gesture in :data:`_BEHAVIOURS` today. Call sites read

        .. code-block:: python

            if (paste := self._kind_handler(Gesture.PASTE)) is not None:
                paste()
                return

        so the branch says which decision it is making, rather than testing a
        flag on the document and naming the other kind's method in the same
        breath. What that buys is not the line saved: it is that adding a kind
        means adding a column to one table, instead of finding the branches.

        No document, no gesture. Every one of these acts on what is on screen,
        and the shared body's own empty-state guard is the right answer there —
        a missing-file entry keeps ``current`` on its (possibly tilemap) entry
        with nothing loaded (:meth:`~...session.SessionMixin._show_unavailable`),
        so the kind alone would dispatch a cell edit against no cells.
        """
        if self._doc is None:
            return None
        name = _BEHAVIOURS.get(self._content_kind(), {}).get(gesture)
        return None if name is None else getattr(self, name, None)

    def _sync_capabilities(self) -> None:
        """Switch off whatever the current entry has no capability for.

        Runs last in the refresh cycle so its veto is the final word — an
        earlier pass may have enabled a control on grounds that are true in
        general and beside the point for this kind of document.

        And last in the **empty** state (``SessionMixin._show_empty``), which is
        not the same path: a render needs a document, so a pass that only ran
        from one would leave the bar showing whatever the last entry needed —
        or, before anything had been opened, whatever the toolbar was built
        with. Nothing open answers ``PIXELS`` here, so the empty window is
        gated as the kind it is about to read rather than as a special case.
        """
        for capability, names in _GATES.items():
            allowed = self._can(capability)
            for name in names:
                found = getattr(self, name, None)
                if found is None:
                    continue
                # A name may hold a group rather than a control (see _GATES);
                # every member of one is gated the same way, so the tuple is
                # flattened here rather than spelled out as a dozen names.
                for control in found if isinstance(found, tuple) else (found,):
                    if name in _HIDDEN:
                        control.setVisible(allowed)
                    if name in _VISIBILITY_ONLY:
                        continue  # hiding is the whole gate — see _VISIBILITY_ONLY
                    if name in _GATE_OWNS:
                        control.setEnabled(allowed)
                    elif not allowed:
                        # Never the other branch: see the module docstring on
                        # why this only ever takes away.
                        control.setEnabled(False)
