"""Where the view has been, and stepping back and forth through it.

A session wanders: a slice's parent, the file a tilemap draws its tiles from, an
entry an undo reverted in - several gestures move the view somewhere the user did
not pick from the list, and finding the way back by hand means remembering which
row it was. So the window keeps a **visit trail** of the entries that have been
on screen and walks it like a browser's history: Back returns to the previous
one, Forward retraces, and visiting somewhere new from the middle of the trail
drops whatever lay ahead.

It is **session state, not project state** - a trail of live ``Entry`` objects,
never written to the project file, the same reasoning that keeps the undo stack
out of it (``docs/design/undo-redo.md``). Two consequences: closing an entry
takes its slots out of the trail (they can't be returned to), and a project load
replaces the workspace entry by entry, which prunes the old trail down to
nothing on the way through.

Recorded in
:meth:`~celpix.ui.main_window.session.SessionMixin._on_current_entry_changed`
rather than at the activation call sites: every way the view can move - the list,
a jump action, an undo, a close landing on a neighbour - ends there, and it is the
only place that is true of.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import QApplication, QMenu

from celpix.project.workspace import Entry

# Deep enough that a session's worth of hopping stays retraceable, bounded so a
# long one doesn't pin every entry it ever showed. The oldest visit falls off.
_TRAIL_LIMIT = 64


class HistoryMixin:
    """The visit trail, its two actions, and the mouse buttons that drive them.

    A slice of :class:`~celpix.ui.main_window.window.MainWindow`, not a
    standalone object: it moves the window's current entry. See the module
    docstring for what it owns, and the package docstring for why these are
    mixins.
    """

    def _init_history(self) -> None:
        """Seed the trail and its two actions - before anything can make an entry
        current, since the first visit arms them.

        The actions carry **real shortcuts**, unlike the rest of the Navigate
        menu they end up in: Alt+arrows carry the Alt modifier that
        :meth:`~celpix.ui.main_window.navigation.NavigationMixin._handle_nav_key`
        deliberately declines, so they reach the shortcut system instead of the
        navigation filter and can't be stolen from a focused text input.
        """
        # The entries visited, oldest first, and where in them the view sits.
        # _history[_history_pos] is always the entry on screen; -1 is an empty
        # trail (nothing has been shown yet).
        self._history: list[Entry] = []
        self._history_pos = -1
        # Set while a Back/Forward step is activating its target, so the
        # activation it causes is not recorded as a new visit - the whole point
        # of a trail is that walking it doesn't rewrite it.
        self._history_walking = False

        self._back_action = QAction("&Back", self)
        self._back_action.setShortcut(QKeySequence("Alt+Left"))
        self._back_action.triggered.connect(lambda: self._history_step(-1))
        # Mnemonic "a": every earlier letter of the word is taken elsewhere in
        # the Navigate menu ("f" First page, "o" More rows, "r" Fewer rows,
        # "w" Page down).
        self._forward_action = QAction("Forw&ard", self)
        self._forward_action.setShortcut(QKeySequence("Alt+Right"))
        self._forward_action.triggered.connect(lambda: self._history_step(1))
        self._sync_history_actions()

    # -- the actions ---------------------------------------------------------
    def _add_history_actions(self, menu: QMenu) -> None:
        """Put Back/Forward at the head of ``menu`` (Navigate), then a separator."""
        menu.addAction(self._back_action)
        menu.addAction(self._forward_action)
        menu.addSeparator()

    def _sync_history_actions(self) -> None:
        """Arm each direction iff there is a visit that way, and name where it goes."""
        for action, delta, way in (
            (self._back_action, -1, "Back"),
            (self._forward_action, 1, "Forward"),
        ):
            target = self._history_target(delta)
            action.setEnabled(target is not None)
            where = (
                f"{way} to {target.name}"
                if target is not None
                else f"Nothing to go {way.lower()} to"
            )
            button = "Mouse 4" if delta < 0 else "Mouse 5"
            action.setToolTip(f"{where}\nAlso {button} (the browser {way} button)")

    def _history_target(self, delta: int) -> Entry | None:
        """The entry ``delta`` steps along the trail, or None at that end of it."""
        at = self._history_pos + delta
        if 0 <= at < len(self._history):
            return self._history[at]
        return None

    # -- walking it ----------------------------------------------------------
    def _history_step(self, delta: int) -> None:
        """Move one visit back (-1) or forward (+1); a no-op at either end.

        The position only moves once the view actually did: an entry whose file
        has gone bad fails to activate, and swallowing the step would leave Back
        pointing somewhere the user never got to.
        """
        if self._scanning:
            return  # a running scan owns the view; it freezes the UI anyway
        target = self._history_target(delta)
        if target is None:
            return
        self._history_walking = True
        try:
            self._activate_entry(target)
        finally:
            self._history_walking = False
        if self._workspace.current is target:
            self._history_pos += delta
            self._sync_history_actions()

    def _handle_history_mouse(self, event) -> bool:  # noqa: ANN001 - QMouseEvent
        """Route a back/forward mouse button to the trail; True if consumed.

        Consumes the whole click - press, double-click and release - even when
        the trail can't move that way, because these two buttons mean nothing
        else in this window and a widget receiving half a click is worse than a
        press that did nothing. A double-click steps twice, as a browser does:
        Qt delivers the second press of a fast pair as ``MouseButtonDblClick``.
        """
        delta = {
            Qt.MouseButton.BackButton: -1,
            Qt.MouseButton.ForwardButton: 1,
        }.get(event.button())
        if delta is None:
            return False
        if QApplication.activePopupWidget() is not None:
            return False  # an open menu/popup gets its own clicks
        if event.type() in (
            QEvent.Type.MouseButtonPress,
            QEvent.Type.MouseButtonDblClick,
        ):
            self._history_step(delta)
        return True

    # -- keeping it honest ---------------------------------------------------
    def _record_visit(self, entry: Entry | None) -> None:
        """Note ``entry`` as the newest visit, dropping any forward tail.

        Nothing-open (None) is not a visit - it is the absence of one, and there
        is no view to go back to. Re-showing the entry already on screen isn't
        one either: consecutive duplicates would make Back a no-op that looks
        like a dead key.
        """
        if entry is None or self._history_walking:
            return
        if self._history_target(0) is entry:
            return
        del self._history[self._history_pos + 1 :]
        self._history.append(entry)
        del self._history[: max(0, len(self._history) - _TRAIL_LIMIT)]
        self._history_pos = len(self._history) - 1
        self._sync_history_actions()

    def _forget_visits(self, entry: Entry) -> None:
        """Drop every visit to ``entry`` - it is being closed, so it can't be
        returned to.

        Removing a slot from the middle can leave the same entry either side of
        the gap; those collapse into one, or Back would step onto the entry
        already shown and spend a keypress going nowhere.
        """
        kept: list[Entry] = []
        pos = self._history_pos
        for i, visited in enumerate(self._history):
            if visited is entry or (kept and kept[-1] is visited):
                if i <= pos:
                    pos -= 1  # a slot at or before the view's own vanished
            else:
                kept.append(visited)
        self._history = kept
        self._history_pos = max(-1, min(pos, len(kept) - 1))
        self._sync_history_actions()
