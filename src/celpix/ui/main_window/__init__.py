"""The application main window, split by concern.

:class:`~celpix.ui.main_window.window.MainWindow` is one class assembled from
mixins, one per surface it drives - navigation, interpretation, palette (source,
dock, color editing and pinned regions), selection, transforms, pixel editing,
rearrange, session, tilemap (the binding bar, cell editing, the tile source dock
and the stamp tool), capability sync, rendering, entries, writing, transfer,
compression. They are mixins rather than
collaborator objects because they all manipulate the *same* live widgets and the
single ``_doc`` on screen; splitting that state across objects would buy
indirection rather than isolation. What the split does buy is a named home for
each concern, so a change to (say) the palette modes is a change to one file.

Several surfaces serve **both** kinds of document a window can show, and where
the two mean different things the control is one and the behaviour resolves per
kind - a flip rewrites pixels on a graphic and toggles an attribute bit on a
tilemap (``docs/design/tilemap-entry.md`` §4). Which controls apply at all is
declared once in :data:`~celpix.core.capabilities.CAPABILITIES` and applied by
``capability_sync`` at the tail of the refresh, rather than each surface carrying
its own "...and not on a tilemap" clause.

``window.py`` itself is what is left when every surface has one: the widgets and
docks, the menu bar, the shared undo stack, the open project's dirty state, and
the error modal. It is the shell the mixins hang off, not one more surface.

Only the window class is public; import it from here.
"""

from celpix.ui.main_window.window import MainWindow

__all__ = ["MainWindow"]
