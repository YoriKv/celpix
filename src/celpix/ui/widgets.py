"""Small reusable UI widgets.

Qt lives here (this is the ``ui`` layer); the model stays Qt-free.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QLineEdit, QWidget


class CommittingLineEdit(QLineEdit):
    """A free-text field that commits on edit-finish and self-normalises.

    Free-text fields that parse into a value (a hex offset, a dec/hex number, a
    palette index) all share one subtle correctness requirement, and one Qt
    gotcha that makes it easy to get wrong:

    - **Commit, don't stream.** The value should apply when the user finishes
      editing (Enter / focus-out), not on every keystroke — otherwise a
      half-typed value fires repeatedly.
    - **Always re-render on commit — even while focused.** An invalid entry must
      revert to the current value, and a valid one must show its *canonical* form
      (e.g. a tile-snapped, ``0x``-prefixed offset). The trap: ``editingFinished``
      fires on Enter *and* on focus-out, but Qt won't fire it again on a
      focus-out whose text is unchanged since the Enter. So if you skip the
      re-render while the field has focus (the usual guard against clobbering
      mid-typing), an invalid value committed with Enter lingers — the later
      focus-out never corrects it. Re-rendering unconditionally here closes that.

    Wiring it up: pass ``parse`` (text → value, or ``None`` when invalid) and
    ``current_text`` (a callable returning the canonical display string for the
    *current* committed state). On a valid commit the widget emits
    :attr:`committed` with the parsed value — the owner applies it (which may
    clamp/transform the underlying state) — and then the widget re-renders from
    ``current_text``, so the box always reflects the true post-commit state.
    """

    committed = Signal(object)  # the parsed value, on a valid commit

    def __init__(
        self,
        parse: Callable[[str], object | None],
        current_text: Callable[[], str],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._parse = parse
        self._current_text = current_text
        self.editingFinished.connect(self.commit)

    def refresh(self) -> None:
        """Set the displayed text to the canonical current value."""
        self.setText(self._current_text())

    def commit(self) -> None:
        """Parse the text; emit :attr:`committed` if valid, then always re-render.

        Unconditional re-render is the point — see the class docstring: it reverts
        invalid input and normalises valid input regardless of focus.
        """
        value = self._parse(self.text())
        if value is not None:
            self.committed.emit(value)
        self.refresh()
