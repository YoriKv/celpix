"""The Qt-free half of reloading a file another program rewrote: the byte
merge that carries unsaved edits across, and the fingerprints that notice the
rewrite (``docs/design/disk-changes.md``)."""

from __future__ import annotations

import os

from celpix.project.diskchanges import (
    DiskState,
    carry_color_edits,
    changed_offsets,
    merge_bytes,
)


def test_local_edits_win_and_everything_else_takes_the_disk() -> None:
    base = bytes(range(16))
    local = bytearray(base)
    local[3] = 0xAA  # an edit made here
    disk = bytearray(base)
    disk[10] = 0xBB  # the other program's change, elsewhere
    merge = merge_bytes(base, bytes(local), bytes(disk))
    assert merge.data[3] == 0xAA and merge.data[10] == 0xBB
    assert (merge.kept, merge.conflicts, merge.dropped) == (1, 0, 0)


def test_a_byte_both_sides_changed_is_a_conflict_that_local_still_wins() -> None:
    base = b"\x00" * 8
    local = b"\x00\x01" + b"\x00" * 6
    disk = b"\x00\x02" + b"\x00" * 5 + b"\x03"
    merge = merge_bytes(base, local, disk)
    assert merge.data == b"\x00\x01" + b"\x00" * 5 + b"\x03"
    assert merge.conflicts == 1 and merge.kept == 1


def test_an_edit_past_the_end_of_a_shrunk_file_is_dropped_and_counted() -> None:
    base = bytes(8)
    local = bytes(7) + b"\x09"
    merge = merge_bytes(base, local, bytes(4))
    assert merge.data == bytes(4)
    assert (merge.kept, merge.dropped) == (0, 1)


def test_changed_offsets_walk_only_the_chunks_that_differ() -> None:
    """A length change counts over the tail, and an edit inside a big untouched
    buffer is found at its exact offsets."""
    base = bytes(4096)
    local = bytearray(base)
    local[1000] = 1
    local[3000] = 1
    assert changed_offsets(base, bytes(local)) == [1000, 3000]
    assert changed_offsets(b"ab", b"abcd") == [2, 3]


def test_colour_edits_are_carried_by_index() -> None:
    fresh = [1, 2, 3, 4]
    edited = [9, 9, 9, 9, 9]  # a longer palette from before; index 4 has no home
    assert carry_color_edits(fresh, edited, {1, 4}) == [1, 9, 3, 4]


def _touch(path) -> None:
    os.utime(path, ns=(0, os.stat(path).st_mtime_ns + 2_000_000_000))


def test_the_bytes_decide_and_a_touch_that_changed_nothing_is_no_change(
    tmp_path,
) -> None:
    rom = tmp_path / "game.sfc"
    rom.write_bytes(bytes(64))
    state = DiskState()
    state.track([str(rom)])
    assert state.changed() == [] and state.is_current([str(rom)])

    rom.write_bytes(bytes(64))  # the same bytes again, at a later time
    _touch(rom)
    assert state.changed() == [] and state.is_current([str(rom)])

    rom.write_bytes(bytes(63) + b"\x01")
    _touch(rom)
    assert state.changed() == [str(rom)] and not state.is_current([str(rom)])
    state.refresh([str(rom)])  # "that was us"
    assert state.changed() == []


def test_a_declined_state_is_not_offered_again_but_is_still_not_current(
    tmp_path,
) -> None:
    """The next change asks afresh, and an outright reload still has the
    declined bytes to read."""
    rom = tmp_path / "game.sfc"
    rom.write_bytes(bytes(64))
    state = DiskState()
    state.track([str(rom)])
    rom.write_bytes(b"\x01" * 64)
    _touch(rom)
    assert state.changed() == [str(rom)]
    state.decline([str(rom)])
    assert state.changed() == [] and not state.is_current([str(rom)])
    rom.write_bytes(b"\x02" * 64)
    _touch(rom)
    assert state.changed() == [str(rom)]


def test_a_missing_file_is_not_reported_until_it_appears(tmp_path) -> None:
    rom = tmp_path / "game.sfc"
    rom.write_bytes(bytes(64))
    state = DiskState()
    state.track([str(rom)])

    # A file that vanished mid-rename is nothing to report; one that has
    # appeared where the project was waiting for it is.
    missing = tmp_path / "later.bin"
    state.track([str(missing)])
    assert state.changed() == []
    missing.write_bytes(b"x")
    assert state.changed() == [str(missing)]

    state.retain([str(rom)])
    assert state.paths() == [str(rom)]
