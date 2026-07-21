"""Shared test setup.

Force Qt's ``offscreen`` platform before PySide6 is imported anywhere, so the
suite runs headless in CI and under WSL without a display server. A developer
can override this by exporting ``QT_QPA_PLATFORM`` themselves.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
