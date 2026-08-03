"""Animation sequences: the order a sprite object's frames are meant to play in.

A sprite object stores its frames in fixed slots and draws them in file order
(:mod:`celpix.core.sprite`); which of them play, in what order and for how long
is a separate table the authoring tool wrote beside them
(``docs/graphics-formats-reference/scgcad-formats.md`` §8.3). This is that table
read into a model — what a player steps through, and nothing else: the frames
themselves are still drawn from the records.

**The table is not a trustworthy account of the file.** Hundreds of corpus files
hold live steps naming a frame past the last drawn one, and hundreds of steps
name a frame the file does not have at all — the tool wrote its terminator and
left whatever was in the buffer behind it. So two rules hold everywhere here:
reading **stops at the terminator**, and a step naming a frame that does not
exist is **kept rather than dropped** (:func:`unknown_frames` counts them for the
reader to say so). Dropping them would quietly renumber a sequence's steps, and
what the user needs to know is that the file says something impossible, not a
tidied version of it.

An **empty sequence is still a sequence**: a file's groups are numbered slots, so
one that terminates immediately comes back as a :class:`Sequence` with no steps
rather than being skipped. Skipping would slide every later group onto a number
that names a different one in the file.

Qt-free, like the rest of ``core``.
"""

from __future__ import annotations

from dataclasses import dataclass

# What ends a sequence: a step of no duration naming frame 0. Both bytes zero, so
# it is also what an untouched group reads as — which is why an empty sequence and
# a terminated one need no distinguishing.
TERMINATOR = (0, 0)


@dataclass(frozen=True, slots=True)
class Step:
    """One step of a sequence: show ``frame`` for ``duration`` ticks.

    A **tick** is the unit the authoring tool counted in, not a time. Nothing in
    the corpus or in the tool's own writer says what it was worth; the reasonable
    reading is one console frame, which a player turns into milliseconds at a
    rate the user can change rather than one this claims to know.
    """

    duration: int = 0
    frame: int = 0


@dataclass(frozen=True, slots=True)
class Sequence:
    """One group of the table — a run of steps, in the order they play.

    Empty where the file's group is empty, which most of them are: a file has
    room for sixteen or thirty-two and typically fills a handful.
    """

    steps: tuple[Step, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.steps)

    @property
    def ticks(self) -> int:
        """How long one pass through takes, in :class:`Step` ticks."""
        return sum(step.duration for step in self.steps)


def read_sequences(
    data: bytes, at: int, count: int, steps_per_sequence: int
) -> tuple[Sequence, ...]:
    """The table at ``at`` read as ``count`` groups of ``steps_per_sequence``.

    The structure both S-CG-CAD sprite forms share, differing only in those two
    numbers — 16 groups of 32 for an object, 32 of 32 for the extended one — so
    the shape is stated by the caller that knows which form it is holding rather
    than guessed from the length.

    Each step is a **byte pair, duration first**. That is the same thing as the
    little-endian word the format reference names it (a word of ``0x0103`` is
    frame 1 for three ticks), read as the two bytes it is built from so that no
    byte order has to be asserted to get at either half.

    A group short of its full length — a truncated file — yields the steps that
    are there rather than raising: the frames are the art and they have already
    been read by the time this runs, so a table cut short should cost the
    sequences and not the file.
    """
    out: list[Sequence] = []
    for group in range(max(0, count)):
        start = at + group * steps_per_sequence * 2
        steps: list[Step] = []
        for step in range(max(0, steps_per_sequence)):
            first = start + step * 2
            if first + 2 > len(data):
                break
            duration, frame = data[first], data[first + 1]
            if (duration, frame) == TERMINATOR:
                break
            steps.append(Step(duration, frame))
        out.append(Sequence(tuple(steps)))
    return tuple(out)


def read_parallel_sequences(data: bytes, at: int, steps: int) -> tuple[Sequence, ...]:
    """One sequence from **two blocks** — ``steps`` frame numbers, then as many
    durations.

    The other shape a sprite trailer comes in
    (``docs/graphics-formats-reference/ys-sprite-patterns.md`` §4): where the
    S-CG-CAD object interleaves a step's two bytes, this format writes all the
    frames and then all the durations, so step *n* is ``data[at + n]`` paired with
    ``data[at + steps + n]``. One sequence rather than a table of them — the
    format has room for exactly one.

    **The block split is inferred, not proven.** The tool's writer emits both
    blocks as opaque byte arrays, so "A is frames, B is durations" is read off the
    corpus rather than off the code, and callers are expected to say so
    (:data:`~celpix.core.context.KEY_TILEMAP_ANIMATIONS_INFERRED`). The
    terminator is inferred one step further: nothing states one, so this stops
    where the sibling format's table does — a step of no duration naming frame 0
    — which is also what an untouched block reads as.
    """
    out: list[Step] = []
    for step in range(max(0, steps)):
        frame_at, duration_at = at + step, at + steps + step
        if duration_at >= len(data):
            break
        duration, frame = data[duration_at], data[frame_at]
        if (duration, frame) == TERMINATOR:
            break
        out.append(Step(duration, frame))
    return (Sequence(tuple(out)),)


def unknown_frames(sequences: tuple[Sequence, ...], frames: int) -> int:
    """How many steps name a frame the file does not hold — 0 when all resolve.

    What a reader shows rather than what it acts on: the steps are kept either
    way (see the module docstring), and this is the count that lets a window say
    the table names frames that are not there instead of silently drawing
    nothing where one of them comes up.
    """
    return sum(
        1
        for sequence in sequences
        for step in sequence.steps
        if not 0 <= step.frame < frames
    )
