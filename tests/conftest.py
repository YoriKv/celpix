"""Shared test setup.

Force Qt's ``offscreen`` platform before PySide6 is imported anywhere, so the
suite runs headless in CI and under WSL without a display server. A developer
can override this by exporting ``QT_QPA_PLATFORM`` themselves.
"""

import os
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(autouse=True)
def captured_alerts(monkeypatch):
    """Record error/warning alerts instead of showing them, for every test.

    ``MainWindow._alert`` is the single modal surface for failures; a real
    ``exec()`` would block the offscreen event loop and hang the suite. Here it
    appends ``(title, message)`` to a list a test can request by name to assert
    what the user was told. Guarded on the UI module already being imported, so
    the headless model-layer suites never pull Qt in through this fixture.
    """
    module = sys.modules.get("celpix.ui.main_window")
    if module is None:
        return []
    alerts: list[tuple[str, str]] = []
    monkeypatch.setattr(
        module.MainWindow,
        "_alert",
        lambda self, message, *, title="Celpix", detail="": alerts.append(
            (title, message)
        ),
        raising=False,
    )
    return alerts
