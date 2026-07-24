"""Standalone widget behaviour: the committing line edit's commit-on-finish /
emit-if-valid / self-normalise contract, the checklist popup's clamp spring-back,
and the geometry of the tool-rail glyphs."""

from __future__ import annotations

from celpix.ui.widgets import ChecklistPopupButton, CommittingLineEdit


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


def test_checklist_popup_springs_back_when_owner_clamps(qtbot) -> None:
    # The button is view-only: a toggle hands the desired set to the owner and
    # re-syncs to whatever the owner returns. Here the owner refuses to drop the
    # last item ("a"), so unchecking it must visibly snap back to checked.
    def apply(desired: set) -> set:
        return desired or {"a"}

    button = ChecklistPopupButton(
        "Filter", lambda: [("a", "A", True), ("b", "B", True)], apply
    )
    qtbot.addWidget(button)
    button._open()  # build the popup + checkboxes without a real click

    button._boxes["b"].setChecked(False)  # allowed -> stays unchecked
    assert not button._boxes["b"].isChecked()
    button._boxes["a"].setChecked(False)  # would empty the set -> clamped back
    assert button._boxes["a"].isChecked()


def test_marquee_glyph_is_centred_like_the_other_tool_shapes() -> None:
    """The marquee's dashed outline must sit on the same footprint as the shapes
    it shares the rail with, and be a mirror of itself both ways.

    Two distinct ways it has drifted off-centre, hence two assertions. Its pen is
    thinner than the other outlines' and lands on an *odd* width (1 px at 1x, 3 px
    at 2x), where an integer inset spills a pixel past the filled bounds — that
    moves the footprint. And a dashed rectangle stroked in one pass runs a single
    phase around the whole perimeter, so it ends mid-pattern and leaves whichever
    corners it lands on bare — that leaves the bounds correct but the ink
    lopsided, which only the symmetry check catches. Both scales are tested
    because the pen parity only trips at one of them.
    """
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QPainter, QPixmap

    from celpix.ui.tools_panel import ToolsPanel

    def alpha(shape: str, box: int) -> list[list[int]]:
        pixmap = QPixmap(box, box)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        ToolsPanel._paint_shape(painter, shape, box)
        painter.end()
        image = pixmap.toImage()
        return [
            [image.pixelColor(x, y).alpha() for x in range(box)] for y in range(box)
        ]

    def bounds(rows: list[list[int]]) -> tuple[int, int]:
        """The first and last columns holding any ink."""
        box = len(rows)
        lit = [x for y in range(box) for x in range(box) if rows[y][x]]
        return min(lit), max(lit)

    for box in (20, 40):  # the 1x and 2x device-pixel-ratio sizes
        rows = alpha("marquee", box)
        first, last = bounds(rows)
        assert first == box - 1 - last  # equal margins either side: centred
        assert (first, last) == bounds(alpha("rect_filled", box))  # the set's box
        # Mirrored horizontally and vertically — an unclosed corner shows up here
        # and nowhere else, since it leaves the bounding box untouched.
        assert rows == [list(reversed(row)) for row in rows]
        assert rows == rows[::-1]
