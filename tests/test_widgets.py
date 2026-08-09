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


def test_zoom_steps_through_its_levels_and_reads_them_back(qtbot) -> None:
    """Zoom is a list, not a count: the gap below 1 is one step like any other,
    and the box must not spell a whole level "4.0"."""
    from celpix.ui.widgets import ZoomSpinBox, zoom_level_after

    assert zoom_level_after(1, -1) == 0.5
    assert zoom_level_after(0.5, 1) == 1
    assert zoom_level_after(0.5, -1) == 0.5  # clamps at the bottom
    assert zoom_level_after(2, 3) == 5
    assert zoom_level_after(1.4, 0) == 1  # off-list value snaps to the nearest
    assert zoom_level_after(0.6, 0) == 0.5

    spin = ZoomSpinBox()
    qtbot.addWidget(spin)
    assert spin.textFromValue(4.0) == "4"
    assert spin.textFromValue(0.5) == "0.5"
    spin.setValue(1)
    spin.stepBy(-1)
    assert spin.value() == 0.5
    assert spin.valueFromText("1.7") == 2  # typed in-between snaps to a level


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

    # Every ratio a real display hands out, not just 1x/2x: the pen widths and
    # paddings are rounded off `box`, so a parity trip can hide at one scale and
    # show at the next.
    for box in (20, 25, 30, 35, 40, 50, 60):
        footprint = bounds(alpha("rect_filled", box))
        # Every painted glyph, not just the marquee: the line drifted a pixel
        # down-right for want of this, because a stroke centred on Qt's integer
        # coordinates - which sit *between* pixels - rasterizes wider on one side.
        for shape in ("marquee", "line", "rect", "ellipse", "ellipse_filled"):
            rows = alpha(shape, box)
            first, last = bounds(rows)
            assert first == box - 1 - last, (shape, box)  # equal margins: centred
            assert (first, last) == footprint, (shape, box)  # the set's box
        # The marquee alone is mirrored both ways — an unclosed corner shows up
        # here and nowhere else, since it leaves the bounding box untouched. (The
        # line is a diagonal, so it is symmetric only under transposition.)
        rows = alpha("marquee", box)
        assert rows == [list(reversed(row)) for row in rows]
        assert rows == rows[::-1]


def test_the_tools_rail_bakes_a_greyed_face_for_when_it_is_dead(qtbot) -> None:
    """The rail is disabled outside pixel mode, and has to look it.

    Its glyphs are flat silhouettes in one tint, which the style's automatic
    disabled pixmap barely dims — the greyed rail read as live. So each icon
    carries its own Disabled face in the palette's disabled ink: same shape,
    visibly duller ink.
    """
    from PySide6.QtGui import QIcon, QPalette

    from celpix.ui.tools import Tool
    from celpix.ui.tools_panel import ToolsPanel

    panel = ToolsPanel()
    qtbot.addWidget(panel)
    icon = panel._buttons[Tool.PENCIL].icon()
    size = icon.availableSizes()[0]
    live = icon.pixmap(size, QIcon.Mode.Normal).toImage()
    dead = icon.pixmap(size, QIcon.Mode.Disabled).toImage()

    expected = panel.palette().color(
        QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText
    )
    inked = [
        (x, y)
        for y in range(live.height())
        for x in range(live.width())
        if live.pixelColor(x, y).alpha() == 255
    ]
    assert inked, "the pencil glyph drew nothing"
    for x, y in inked:
        assert dead.pixelColor(x, y).alpha() == 255  # the shape is untouched...
        assert dead.pixelColor(x, y).rgb() == expected.rgb()  # ...only the ink moved


def test_preferences_land_in_a_file_named_for_the_app(qtbot, tmp_path) -> None:
    # celPix sets no organization on the QApplication (it would nest the data dir
    # celPix/celPix), and a bare QSettings() with none files everything under a
    # literal "Unknown Organization". The store names its own instead.
    from PySide6.QtCore import QSettings

    from celpix.ui.widgets import settings

    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(
        QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path)
    )
    # ".ini" because the test redirected the store to IniFormat; the name is the
    # part under test — no "Unknown Organization" between it and the base path.
    # as_posix() because Qt reports paths with "/" separators on every platform,
    # Windows included, where str(Path) would hand back backslashes.
    assert settings().fileName() == (tmp_path / "celPix.ini").as_posix()


def test_recent_projects_survive_the_ini_backends_quirks(qtbot, tmp_path) -> None:
    # Two shapes the INI backend gets to decide for itself, both of which have to
    # come back as the list that went in: a *single* entry, which it stores (and
    # returns) as a bare string rather than a one-item list, and a path holding
    # the comma it separates list items with.
    from PySide6.QtCore import QSettings

    from celpix.ui.widgets import (
        clear_recent_projects,
        load_recent_projects,
        remember_recent_project,
        settings,
    )

    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(
        QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path)
    )
    clear_recent_projects()

    lone = tmp_path / "solo.celpix"
    remember_recent_project(str(lone))
    settings().sync()  # round-trip through the file, not the in-memory cache
    assert load_recent_projects() == [str(lone)]

    comma = tmp_path / "hack, revised.celpix"
    remember_recent_project(str(comma))
    settings().sync()
    assert load_recent_projects() == [str(comma), str(lone)]


def test_a_stale_enum_preference_falls_back_to_the_default(qtbot, tmp_path) -> None:
    # Every app-global preference (grid style, selection shape, active tool) is
    # stored by its enum's string value, so a settings file written by an older or
    # newer celPix can name a member this build doesn't have. That must not stop
    # the window from being built, so an unreadable value reads as the default.
    from enum import Enum

    from PySide6.QtCore import QSettings

    from celpix.ui.widgets import load_enum_setting, save_enum_setting, settings

    class Style(Enum):
        LINE = "line"
        DOT = "dot"

    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(
        QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path)
    )
    settings().clear()

    assert load_enum_setting("probe/style", Style.LINE) is Style.LINE  # unset
    save_enum_setting("probe/style", Style.DOT)
    assert load_enum_setting("probe/style", Style.LINE) is Style.DOT  # round trip
    settings().setValue("probe/style", "bogus")  # stale / foreign value
    assert load_enum_setting("probe/style", Style.LINE) is Style.LINE
