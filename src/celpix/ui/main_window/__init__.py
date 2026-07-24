"""The application main window, split by concern.

:class:`~celpix.ui.main_window.window.MainWindow` is one class assembled from
mixins, one per surface it drives - navigation, interpretation, palette (source,
dock and color editing), selection, transforms, entries, transfer, compression.
They are mixins rather than
collaborator objects because they all manipulate the *same* live widgets and the
single ``_doc`` on screen; splitting that state across objects would buy
indirection rather than isolation. What the split does buy is a named home for
each concern, so a change to (say) the palette modes is a change to one file.

Only the window class is public; import it from here.
"""

from celpix.ui.main_window.window import MainWindow

__all__ = ["MainWindow"]
