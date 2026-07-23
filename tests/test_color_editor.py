"""The shared color editor: hex parsing, input↔color sync, and signal hygiene.

The regression risk here is the two-way binding, not the layout: channel inputs
and the hex field both write and are written, so a missing guard turns one edit
into a feedback loop, and a programmatic move (an eyedropper sample, an undo
landing underneath the dialog) must never echo back as a fresh user edit.
"""

from __future__ import annotations

import pytest

from celpix.ui.color_editor import ColorEditor, parse_hex_color


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("#AABBCCDD", 0xAABBCCDD),
        ("AABBCCDD", 0xAABBCCDD),  # the hash is optional
        ("#112233", 0xFF112233),  # 6 digits are taken as opaque
        ("  #112233  ", 0xFF112233),  # surrounding space is tolerated
        ("#00FFFFFF", 0x00FFFFFF),  # a zero alpha is a value, not "missing"
    ],
)
def test_parse_hex_color_accepts(text: str, expected: int) -> None:
    assert parse_hex_color(text) == expected


@pytest.mark.parametrize("text", ["", "#12345", "#123456789", "#GGHHII", "nonsense"])
def test_parse_hex_color_rejects(text: str) -> None:
    # None is the contract CommittingLineEdit reads as "invalid, revert".
    assert parse_hex_color(text) is None


def test_channel_edit_emits_once_and_syncs_the_hex_field(qtbot) -> None:
    editor = ColorEditor()
    qtbot.addWidget(editor)
    editor.set_color(0xFF000000, mark_original=True)
    seen: list[int] = []
    editor.color_changed.connect(seen.append)

    editor._spins["R"].setValue(0x80)

    # One user gesture, one signal — the slider/hex resync must not re-emit.
    assert seen == [0xFF800000]
    assert editor.color() == 0xFF800000
    assert editor._sliders["R"].value() == 0x80
    # Alpha is off by default, so the hex field is the 6-digit form.
    assert editor._hex.text() == "#800000"


def test_hex_commit_drives_the_channel_inputs(qtbot) -> None:
    editor = ColorEditor()
    qtbot.addWidget(editor)
    editor.set_alpha_enabled(True)
    seen: list[int] = []
    editor.color_changed.connect(seen.append)

    editor._hex.setText("#20304050")
    editor._hex.commit()

    assert seen == [0x20304050]
    assert [editor._spins[c].value() for c in ("A", "R", "G", "B")] == [
        0x20,
        0x30,
        0x40,
        0x50,
    ]
    assert editor._hex.text() == "#20304050"


def test_set_color_is_silent(qtbot) -> None:
    # The host uses set_color for every programmatic move (eyedropper sample,
    # undo, retarget); an echo here would push a spurious edit command.
    editor = ColorEditor()
    qtbot.addWidget(editor)
    seen: list[int] = []
    editor.color_changed.connect(seen.append)

    editor.set_color(0xFF445566)

    assert seen == []
    assert editor.color() == 0xFF445566
    assert editor._hex.text() == "#445566"


def test_revert_returns_to_the_marked_original(qtbot) -> None:
    editor = ColorEditor()
    qtbot.addWidget(editor)
    editor.set_color(0xFF102030, mark_original=True)
    editor.set_color(0xFF999999)  # a programmatic move doesn't re-arm Revert
    seen: list[int] = []
    editor.color_changed.connect(seen.append)

    editor._reset.click()

    # Revert is a real edit — it emits, so the host records it on the stack.
    assert seen == [0xFF102030]
    assert editor.color() == 0xFF102030


def test_quantizer_drives_the_stored_preview(qtbot) -> None:
    editor = ColorEditor()
    qtbot.addWidget(editor)
    editor.set_color(0xFF010203, mark_original=True)

    # No quantizer: nothing is written through a codec, so no preview at all.
    editor.set_quantizer(None)
    assert editor._stored.isHidden()

    # A lossy quantizer both shows the stored color and flags the loss.
    editor.set_quantizer(lambda argb: argb & 0xFFF8F8F8)
    assert not editor._stored.isHidden()
    assert editor._stored_note.text() == "#FF000000"
    assert not editor._stored_approx.isHidden()

    # An exact one drops the qualifier — otherwise the warning is just noise.
    editor.set_quantizer(lambda argb: argb)
    assert editor._stored_note.text() == "#FF010203"
    assert editor._stored_approx.isHidden()


def test_pick_button_announces_and_reflects_host_state(qtbot) -> None:
    editor = ColorEditor()
    qtbot.addWidget(editor)
    toggles: list[bool] = []
    editor.pick_toggled.connect(toggles.append)

    editor._pick.click()
    assert toggles == [True]

    # The host disarming the eyedropper must not re-announce the toggle.
    editor.set_pick_active(False)
    assert toggles == [True]
    assert not editor._pick.isChecked()


def test_alpha_input_appears_only_when_the_format_stores_it(qtbot) -> None:
    editor = ColorEditor()
    qtbot.addWidget(editor)

    # Off by default: most retro palette formats have no alpha field, so the
    # channel is hidden and the color is pinned opaque. (isHidden, not
    # isVisible: an unshown parent makes every child "not visible" regardless.)
    assert editor._sliders["A"].isHidden()
    editor._spins["R"].setValue(0x10)
    assert editor.color() >> 24 == 0xFF

    editor.set_alpha_enabled(True)
    assert not editor._sliders["A"].isHidden()
    editor._spins["A"].setValue(0x40)
    assert editor.color() >> 24 == 0x40


def test_disabling_alpha_forces_the_color_opaque(qtbot) -> None:
    editor = ColorEditor()
    qtbot.addWidget(editor)
    editor.set_alpha_enabled(True)
    editor.set_color(0x40FF0000, mark_original=True)
    seen: list[int] = []
    editor.color_changed.connect(seen.append)

    # Retargeting to a format with no alpha must surface the loss now, as a
    # real edit, rather than letting it vanish silently at encode time.
    editor.set_alpha_enabled(False)

    assert editor.color() == 0xFFFF0000
    assert seen == [0xFFFF0000]


def test_typed_alpha_is_ignored_while_alpha_is_off(qtbot) -> None:
    editor = ColorEditor()
    qtbot.addWidget(editor)

    editor._hex.setText("#20304050")  # 8 digits, but alpha isn't offered
    editor._hex.commit()

    assert editor.color() == 0xFF304050


def test_revert_is_disabled_until_the_color_moves(qtbot) -> None:
    editor = ColorEditor()
    qtbot.addWidget(editor)
    editor.set_color(0xFF102030, mark_original=True)
    assert not editor._reset.isEnabled()

    editor._spins["R"].setValue(0x99)
    assert editor._reset.isEnabled()

    editor._reset.click()
    # Back on the original, so there is nothing left to revert to.
    assert editor.color() == 0xFF102030
    assert not editor._reset.isEnabled()

    # Retargeting re-arms the baseline, so Revert goes quiet again.
    editor.set_color(0xFF445566, mark_original=True)
    assert not editor._reset.isEnabled()
