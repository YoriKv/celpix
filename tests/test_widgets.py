"""CommittingLineEdit: commit-on-finish, emit-if-valid, always self-normalise."""

from __future__ import annotations

from celpix.ui.widgets import CommittingLineEdit


def _int_or_none(text: str) -> int | None:
    try:
        return int(text)
    except ValueError:
        return None


def test_valid_commit_emits_then_normalises(qtbot) -> None:
    # The owner doubles the committed value; the box must re-render from the
    # post-commit state, not the raw text the user typed.
    state = {"v": 10}
    edit = CommittingLineEdit(_int_or_none, lambda: f"={state['v']}")
    qtbot.addWidget(edit)
    got: list[int] = []
    edit.committed.connect(got.append)
    edit.committed.connect(lambda v: state.__setitem__("v", v * 2))

    edit.setText("21")
    edit.commit()

    assert got == [21]
    assert edit.text() == "=42"  # re-rendered from current_text after the owner ran


def test_invalid_commit_reverts_without_emitting(qtbot) -> None:
    state = {"v": 7}
    edit = CommittingLineEdit(_int_or_none, lambda: f"={state['v']}")
    qtbot.addWidget(edit)
    got: list[int] = []
    edit.committed.connect(got.append)

    edit.setText("not a number")
    edit.commit()

    assert got == []  # never emitted for invalid input
    assert edit.text() == "=7"  # reverted to current value (refresh path)
