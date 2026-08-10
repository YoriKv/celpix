"""The format picker's popup: grouping, search, and the selection contract.

What carries the regression risk here is that the widget is still a QComboBox
while its popup is not Qt's — so the rows the popup shows and the rows the combo
holds are two lists that have to keep agreeing, and every path that could leave a
non-selectable heading current has to not.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent

from celpix.plugins.base import CATEGORIES, category_order
from celpix.ui.searchable_combo import (
    SearchableComboBox,
    fill_grouped,
    plugin_rows,
    preset_rows,
)

# Two categories and an ungrouped leader, which is the shape the compression
# picker has: a pass-through that belongs to no machine, then the groups.
ROWS = [
    ("", "None", "none"),
    ("Nintendo", "SNES 4bpp", "snes-4bpp"),
    ("Nintendo", "GB 2bpp", "gb-2bpp"),
    ("Sega", "Genesis 4bpp", "md-4bpp"),
]


def _combo(qtbot, rows=ROWS, selected=None) -> SearchableComboBox:
    combo = SearchableComboBox(160)
    qtbot.addWidget(combo)
    fill_grouped(combo, rows, selected)
    return combo


def _popup_labels(combo: SearchableComboBox) -> list[str]:
    model = combo._popup_model
    return [model.item(row).text() for row in range(model.rowCount())]


def test_headings_are_rows_that_can_never_be_chosen(qtbot) -> None:
    combo = _combo(qtbot)

    assert [combo.itemText(i) for i in range(combo.count())] == [
        "None",
        "Nintendo",
        "SNES 4bpp",
        "GB 2bpp",
        "Sega",
        "Genesis 4bpp",
    ]
    assert [i for i in range(combo.count()) if combo.is_heading(i)] == [1, 4]
    # Disabled is what makes Qt's own arrow-key and wheel stepping skip them —
    # the closed combo steps 0 -> 2 -> 3 -> 5, never onto a heading.
    assert not combo.model().item(1).flags() & Qt.ItemFlag.ItemIsEnabled
    combo.setCurrentIndex(0)
    for expected in (2, 3, 5):
        combo.keyPressEvent(
            QKeyEvent(
                QEvent.Type.KeyPress, Qt.Key.Key_Down, Qt.KeyboardModifier.NoModifier
            )
        )
        assert combo.currentIndex() == expected
    # A heading carries a private sentinel, not None: the alphabet picker stores
    # None as a real choice and must not find a heading with it.
    assert combo.findData(None) == -1


def test_a_leading_heading_is_not_left_selected(qtbot) -> None:
    # The first item inserted into an empty combo becomes current, so a picker
    # whose first row is a heading would otherwise open reading "Nintendo".
    combo = _combo(qtbot, rows=ROWS[1:])

    assert combo.currentData() == "snes-4bpp"
    assert combo.currentIndex() == combo.first_choice() == 1


def test_a_selection_the_list_no_longer_has_falls_back_to_a_choice(qtbot) -> None:
    combo = _combo(qtbot, selected="gone")

    assert combo.currentData() == "none"  # not the heading at row 1


def test_search_keeps_only_the_headings_that_still_have_items(qtbot) -> None:
    combo = _combo(qtbot)
    combo.showPopup()

    combo._rebuild("4bpp")
    assert _popup_labels(combo) == ["Nintendo", "SNES 4bpp", "Sega", "Genesis 4bpp"]

    combo._rebuild("gb")
    assert _popup_labels(combo) == ["Nintendo", "GB 2bpp"]  # Sega's heading is gone

    combo._rebuild("nothing here")
    assert _popup_labels(combo) == []

    combo._close_popup()


def test_search_matches_words_in_any_order(qtbot) -> None:
    # The names put machine, depth and tile size in an order nobody memorises,
    # so "4bpp snes" has to find "SNES 4bpp".
    combo = _combo(qtbot)
    combo.showPopup()

    combo._rebuild("4bpp SNES")

    assert _popup_labels(combo) == ["Nintendo", "SNES 4bpp"]
    combo._close_popup()


def test_choosing_a_filtered_row_selects_the_entry_it_stands_for(qtbot) -> None:
    # The popup's rows and the combo's rows are different lists once a search is
    # narrowing: row 1 of the popup must resolve to the combo's row 5.
    combo = _combo(qtbot)
    activated: list[int] = []
    combo.activated.connect(activated.append)
    combo.showPopup()
    combo._rebuild("genesis")

    combo._on_row_clicked(combo._popup_model.index(1, 0))

    assert combo.currentData() == "md-4bpp"
    assert activated == [5]
    assert combo._popup is None  # picking closes the popup


def test_re_choosing_the_current_entry_still_activates(qtbot) -> None:
    # The tilemap and alphabet pickers hang their edit off `activated`, which
    # fires on every pick — including one that changes nothing.
    combo = _combo(qtbot, selected="gb-2bpp")
    activated: list[int] = []
    changed: list[int] = []
    combo.activated.connect(activated.append)
    combo.currentIndexChanged.connect(changed.append)
    combo.showPopup()

    combo._activate_current()

    assert activated == [3]
    assert changed == []  # nothing moved, so no change was reported


def test_arrow_keys_step_over_the_headings(qtbot) -> None:
    combo = _combo(qtbot, selected="none")
    combo.showPopup()

    combo._step_popup_row(1)  # off "None", over the "Nintendo" heading
    assert combo._list.currentIndex().row() == 2  # "SNES 4bpp"
    combo._step_popup_row(1)
    assert combo._list.currentIndex().row() == 3  # "GB 2bpp"
    combo._step_popup_row(1)  # over the "Sega" heading
    assert combo._list.currentIndex().row() == 5  # "Genesis 4bpp"
    combo._step_popup_row(1)  # off the end: stays put
    assert combo._list.currentIndex().row() == 5

    combo._close_popup()


def test_a_short_flat_picker_keeps_qts_own_popup(qtbot) -> None:
    # A search field over three items costs a row of space and buys nothing.
    combo = SearchableComboBox(160)
    qtbot.addWidget(combo)
    fill_grouped(combo, [("", f"item {i}", i) for i in range(3)])

    combo.showPopup()

    assert combo._popup is None
    combo.hidePopup()


def test_ungrouped_entries_lead_and_unknown_categories_trail() -> None:
    # The pass-through compression plugin names no category and has to stay at
    # the top where a default belongs; a third-party heading is honoured, and
    # sorts after the ones celPix ships.
    order = sorted(["Sega", "", "Zilog", "Nintendo"], key=category_order)

    assert order == ["", "Nintendo", "Sega", "Zilog"]
    assert CATEGORIES.index("Nintendo") < CATEGORIES.index("Sega")


def test_no_shipped_format_invents_a_heading() -> None:
    """A typo — "Nintendo " with a space, "SNK " for "SNK" — is a whole extra
    heading with one item under it, and nothing else would catch it: an unlisted
    category is honoured on purpose, so a third-party preset can invent one.

    Only what a *picker lists* is checked, and only when it names a category at
    all: the interpret engines are never listed (their presets are), and leaving
    the field empty is a legitimate answer — it is how the pass-throughs stay at
    the top of their pickers.
    """
    from celpix.core.errors import Stage
    from celpix.plugins.registry import default_registry

    registry = default_registry()
    listed = [
        *registry.presets(),
        *(
            plugin.info
            for stage in (Stage.CONTAINER, Stage.RESHAPE, Stage.COMPRESSION)
            for plugin in registry.plugins(stage)
        ),
    ]
    stray = {
        (item.id, item.category)
        for item in listed
        if item.category and item.category not in CATEGORIES
    }

    assert stray == set()
    # And the convention is actually used — a run where everything came back
    # uncategorised would pass the check above while grouping nothing.
    assert len({item.category for item in listed if item.category}) > 5


def test_a_dropped_preset_is_filed_by_where_it_came_from(tmp_path) -> None:
    """The source heading **overrides** whatever the file said.

    A preset stating ``category = "Nintendo"`` would otherwise land among fifteen
    shipped SNES formats, which is the "scroll past everything to find your own"
    the grouping exists to end. The two roots are told apart by path, so a project
    that carries plugins gets its own heading rather than the user's.
    """
    from celpix.plugins.discovery import (
        PROJECT_CATEGORY,
        USER_CATEGORY,
        load_user_plugins,
    )
    from celpix.plugins.registry import default_registry

    def _root(name: str, preset_id: str) -> str:
        folder = tmp_path / name / "pixel"
        folder.mkdir(parents=True)
        (folder / "p.toml").write_text(
            f'id = "{preset_id}"\nname = "Mine"\n'
            'engine_id = "codec.pixel.planar"\ncategory = "Nintendo"\n'
            "\n[params]\nbpp = 1\nplanes = [{ base = 0, stride = 1 }]\n"
        )
        return str(tmp_path / name)

    mine, theirs = _root("mine", "preset.pixel.mine"), _root("proj", "preset.pixel.p")
    registry = default_registry()

    assert load_user_plugins(registry, [mine, theirs], project_dir=theirs) == []

    assert registry.preset("preset.pixel.mine").category == USER_CATEGORY
    assert registry.preset("preset.pixel.p").category == PROJECT_CATEGORY
    # And they lead the picker rather than being filed among the shipped formats.
    from celpix.core.errors import Stage

    rows = preset_rows(registry.presets(Stage.INTERPRET_PIXEL))
    assert [row[0] for row in rows[:2]] == [PROJECT_CATEGORY, USER_CATEGORY]


class _Info:
    def __init__(self, ident: str, name: str, category: str) -> None:
        self.id, self.name, self.category = ident, name, category


class _Plugin:
    def __init__(self, ident: str, name: str, category: str) -> None:
        self.info = _Info(ident, name, category)


def test_preset_rows_sort_by_name_and_plugin_rows_keep_registration_order() -> None:
    presets = [
        _Info("b", "Zebra", "Sega"),
        _Info("a", "Alpha", "Sega"),
        _Info("c", "Middle", "Nintendo"),
    ]
    assert [row[2] for row in preset_rows(presets)] == ["c", "a", "b"]

    # Plugins are registered in a deliberate order (LZ1, LZ1 improved, LZ2, …);
    # alphabetising would file LZ16 between LZ1 and LZ2.
    plugins = [
        _Plugin("lz1", "LZ1", "Nintendo"),
        _Plugin("lz16", "LZ16", "Nintendo"),
        _Plugin("lz2", "LZ2", "Nintendo"),
        _Plugin("kos", "Kosinski", "Sega"),
    ]
    assert [row[2] for row in plugin_rows(plugins)] == ["lz1", "lz16", "lz2", "kos"]
