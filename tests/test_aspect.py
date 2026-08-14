"""Non-square pixels: the ratio, and what it does to a zoom.

The Qt-free half. What the surfaces do with these numbers is in
``test_navigation.py``, where the canvas lives.
"""

from __future__ import annotations

from celpix.core.aspect import PRESETS, SQUARE, name, parse, scale


def test_scale_stretches_the_axis_that_would_shrink() -> None:
    """Both factors at least 1, and one of them exactly 1.

    The rule the whole feature rests on (``docs/design/pixel-aspect.md`` §2): a
    1:2 pixel drawn half as wide would drop every other column at zoom 1, which
    is a worse lie than the square reading it replaces.
    """
    assert scale(SQUARE) == (1.0, 1.0)
    assert scale((1, 2)) == (1.0, 2.0)  # tall pixel -> taller picture
    assert scale((2, 1)) == (2.0, 1.0)  # wide pixel -> wider picture
    for aspect, _label, _detail in PRESETS:
        sx, sy = scale(aspect)
        assert min(sx, sy) == 1.0 and max(sx, sy) >= 1.0

    # Only the ratio counts, so an unreduced pair is the same aspect.
    assert scale((8, 4)) == scale((2, 1))


def test_scale_never_divides_by_a_side_that_is_not_one() -> None:
    # parse() rejects these, but scale() is also reached from a project field a
    # future reader could set directly, and a display setting must not raise.
    assert scale((0, 1)) == (1.0, 1.0)
    assert scale((1, 0)) == (1.0, 1.0)
    assert scale((-2, 1)) == (1.0, 1.0)


def test_parse_takes_a_pair_of_positive_ints_and_nothing_else() -> None:
    """None for a miss, because "not a ratio" and "square" are different answers
    where this is read — one leaves the container's hint free to seed."""
    assert parse([1, 2]) == (1, 2)
    assert parse((2, 1)) == (2, 1)
    for junk in (None, [1], [1, 2, 3], "2:1", [0, 1], [1, 0], [-1, 2], {"w": 1}, 2):
        assert parse(junk) is None, junk


def test_name_falls_back_to_the_bare_ratio() -> None:
    # A project can carry a ratio this build has no preset for; it still draws,
    # so it still has to be nameable.
    assert name(SQUARE) == "Square"
    assert name((3, 5)) == "3:5"
