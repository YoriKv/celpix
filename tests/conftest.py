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


@pytest.fixture(autouse=True)
def _destroy_widgets_between_tests():
    """Actually destroy the windows pytest-qt closed, before the next test.

    ``qtbot.addWidget`` cleanup ends in ``deleteLater()``, which only runs the
    destructor once an event loop spins — and these tests never spin one. Left
    alone, every window a run has ever built stays alive to the end of the
    session, and both construction and event delivery get steadily slower as
    they pile up (the tests late in a file paid several times what they cost in
    isolation). Flushing the deferred-delete queue here keeps a test's cost the
    same wherever it sits in the run.
    """
    yield
    qtcore = sys.modules.get("PySide6.QtCore")
    if qtcore is None:
        return
    app = sys.modules["PySide6.QtWidgets"].QApplication.instance()
    if app is not None:
        app.sendPostedEvents(None, qtcore.QEvent.Type.DeferredDelete)


@pytest.fixture(autouse=True)
def _close_discards_edits(monkeypatch):
    """Let pytest-qt close windows without the unsaved-changes prompt.

    ``MainWindow.closeEvent`` asks the user to confirm discarding edits. That
    modal can never be answered under the offscreen platform, and pytest-qt
    closes every widget it was handed during teardown — so *any* test that
    leaves an entry dirty would wedge the whole run there, after its own body
    had already passed. Closing therefore always discards here; no test asserts
    on the quit prompt, and a test that wanted to would re-patch it itself.
    Guarded like :func:`captured_alerts` so headless suites stay Qt-free.
    """
    module = sys.modules.get("celpix.ui.main_window")
    if module is None:
        return
    from PySide6.QtWidgets import QMainWindow

    monkeypatch.setattr(
        module.MainWindow,
        "closeEvent",
        lambda self, event: QMainWindow.closeEvent(self, event),
        raising=False,
    )
