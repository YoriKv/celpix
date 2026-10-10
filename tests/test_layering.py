"""The model layer imports without Qt — the rule the whole headless split rests on."""

import subprocess
import sys

# Run in a fresh interpreter: this one has Qt loaded by pytest-qt before any
# test starts, so ``sys.modules`` here cannot say who imported it. The check
# matches the binding by prefix, and the name is never spelled out in this
# file, because the conftest marks any module whose source names it as a Qt
# suite and this one belongs in ``-m "not qt"``.
_SCRIPT = """
import importlib, pkgutil, sys
import celpix.core, celpix.pipeline, celpix.plugins, celpix.project
for package in (celpix.core, celpix.pipeline, celpix.plugins, celpix.project):
    for info in pkgutil.walk_packages(package.__path__, package.__name__ + "."):
        importlib.import_module(info.name)
print(sorted(name for name in sys.modules if name.startswith("PySide")))
"""


def test_core_pipeline_plugins_and_project_import_without_qt():
    """Every module of the four model packages imports with Qt left unloaded.

    Nothing else would notice a stray Qt import there: the whole suite, the
    model half included, runs with Qt already loaded, so it passes either way —
    and the model layer quietly stops running headless.
    """
    result = subprocess.run(
        [sys.executable, "-c", _SCRIPT],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "[]"
