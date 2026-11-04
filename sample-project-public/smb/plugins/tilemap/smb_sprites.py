"""Super Mario Bros.'s sprites, drawn the way the game's draw routines draw them.

The cartridge keeps very little of a sprite as data. A player frame is eight
tile numbers and an enemy frame six; where each 8x8 piece goes, which way it
faces, which half is the other half mirrored, which row turns upside down and
which palette it takes are all decided by code reading RAM. So these formats
are **the draw routines, replayed**: each reads the ROM bytes it is handed
(the tile tables, and the per-enemy offset and attribute tables beside them),
runs the routine's logic over a scripted RAM state, and returns the OAM
entries the routine would have written. The model was checked against the
running game: every call of the enemy and player draw routines in a recorded
session (10,712 of them, with the RAM that drove each) was replayed through
:func:`draw_enemy` / :func:`draw_player`, and all of them drew the same tiles,
attributes and offsets the game did.

**Frames** are the distinct poses the scenarios produce; **sequences** are the
scenarios ticked frame by frame (a tick is one NMI, 60 Hz) and run-length
encoded, so a step's duration is what the game's own timers give. Each format
choice has its own scenario list (``player_sequences``, ``ENEMY_KINDS``,
``OBJECTS``, ``CODE_SPRITES``), and an entry's name lists its sequences in order.

**Editing.** A piece's tile is a byte in the slice, and these formats are
``fixed`` sprite layouts: every byte the model
reads as a tile is wrapped as a :class:`Tile` that remembers its offset, and
each piece names that offset as its ``record``. Retiling a piece writes that one
byte, and every other piece drawn from it follows, as in the game. Positions,
flips and palettes are decided by code, so they cannot be edited. A tile
the code computes from a stored one (the fireball's second tile, the flame's
second and third, the bumped block's ``$86``) has no record: it follows the
piece it was computed from.

Four formats:

- ``format.tilemap.smb-player`` — ``PlayerGfxTblOffsets``, ``PlayerGraphicsTable``
  and ``SwimKickTileNum`` (CPU ``$EE07``-``$EEE8``), drawn by ``$EFBE`` and
  fixed up by ``$F0E9``. Choice ``set``: big, small, or the size changes.
- ``format.tilemap.smb-enemy`` — ``EnemyGraphicsTable`` and the four tables
  after it (``$E73E``-``$E87C``), drawn by ``EnemyGfxHandler`` ``$E87D``.
  Choice ``enemy``: one kind of enemy per entry.
- ``format.tilemap.smb-object`` — the small table-driven objects: power-ups,
  the jumping coin, floating scores, flagpole scores, the hammer, the castle's
  star flag, explosions, the bumped block. Choice ``object``.
- ``format.tilemap.smb-code-sprite`` — the objects with **no table at all**:
  their tile numbers and attributes are immediate operands. The entry is the
  run of code holding them, and each operand is read where the instruction
  holds it, after checking the opcode, so the frame is the game's own and an
  edit to those bytes (in a hex view) shows here. Choice ``object``.

A cell is a byte, so the encode writes each cell's index back; only the bytes
a piece names can change through Edit Subsprites.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from celpix.core.animation import Sequence, Step
from celpix.core.context import KEY_INPUTS, KEY_TILEMAP_ANIMATIONS, PipelineContext
from celpix.core.sprite import Frame, Subsprite, SubspriteLimits
from celpix.core.tilemap import Cell
from celpix.plugins.base import InputKind, InputSpec
from celpix.plugins.formats import FormatInfo

PLAYER_ID = "format.tilemap.smb-player"
ENEMY_ID = "format.tilemap.smb-enemy"
OBJECT_ID = "format.tilemap.smb-object"
CODE_ID = "format.tilemap.smb-code-sprite"

# Sprite palettes are rows 4-7 of the NES palette; an OAM attribute's low two
# bits count from there.
_DECLARES = {"layout": "sprite", "palette_row_base": 4, "subsprite_size": "stated"}

Piece = tuple[int, int, int, int]  # x, y, tile, OAM attribute byte


class Tile(int):
    """A byte read out of the slice, carrying its offset: the cell an edit to a
    piece drawing it writes. Arithmetic on it gives a plain int, so a value
    the code computes from a stored byte carries no offset."""

    at: int

    def __new__(cls, value: int, at: int) -> Tile:
        tile = super().__new__(cls, value)
        tile.at = at
        return tile


class Tracked:
    """The slice, read the way ``bytes`` is, every byte a :class:`Tile`. A
    sub-slice keeps counting from the slice's start."""

    def __init__(self, data: bytes, base: int = 0) -> None:
        self._data = bytes(data)
        self._base = base

    def __len__(self) -> int:
        return len(self._data)

    def __getitem__(self, key):  # noqa: ANN001, ANN204
        if isinstance(key, slice):
            start, stop, step = key.indices(len(self._data))
            assert step == 1
            return Tracked(self._data[start:stop], self._base + start)
        value = self._data[key]
        return Tile(value, self._base + (key % len(self._data)))


# ---------------------------------------------------------------------------
# The player ($EE07-$EEE8 as one slice)

PLAYER_OFFSETS = 0  # PlayerGfxTblOffsets: 16 bytes, indexed by action (+8 small)
PLAYER_FRAMES = 16  # PlayerGraphicsTable: 26 frames x 8 tile numbers
PLAYER_KICK = 16 + 26 * 8  # SwimKickTileNum: big, small
PLAYER_BYTES = PLAYER_KICK + 2
# SwimTileRepOffset ($EEB5): the small third swim frame's own kick tile, $48
SWIM_TILE_REP = 0xEEB5 - 0xEE07
# PlayerGfxTblOffsets index: what ProcessPlayerAction ($EFEC) asks for
JUMP, SWIM, STAND, SKID, WALK, CLIMB, CROUCH, THROW = range(8)
SMALL = 8
KILLED, GROW = 14, 15  # $EF40 (ldy #$0e) and $F0CE (ldy #$0f)
# PlayerAnimTmrData ($B58C): ticks a walk frame stays up, by speed
# (Player_XSpeedAbsolute >= $1C, >= $0E, below) -- GetPlayerAnimSpeed $B58F
ANIM_TIMER = (2, 4, 7)
CLIMB_TIMER = (8, 4)  # $B46B: 8 climbing up, halved (lsr) climbing down
# PlayerGfxOffsets for the size changes ($F09C): ten steps growing, ten shrinking,
# one every fourth frame ($F0B2: FrameCounter & 3)
CHANGE_STEPS = (0, 1, 0, 1, 0, 1, 2, 0, 1, 2, 2, 0, 2, 0, 2, 0, 2, 0, 2, 0)


def draw_player(
    block: bytes,
    offset: int,
    *,
    facing: int = 1,
    killed: bool = False,
    attrib: int = 0,
    kick: bool = False,
    small: bool = False,
) -> list[Piece]:
    """The 8 OAM entries ``$EFBE`` and ``$F0E9`` write for frame ``offset``.

    ``offset`` is the byte offset into ``PlayerGraphicsTable`` (what
    ``PlayerGfxOffset`` ``$06D5`` holds). Each row is ``DrawOneSpriteRow``:
    facing left (``$03`` bit 1) swaps the pair and sets the horizontal flip.
    """
    pieces: list[Piece] = []
    for row in range(4):
        t0 = block[PLAYER_FRAMES + offset + 2 * row]
        t1 = block[PLAYER_FRAMES + offset + 2 * row + 1]
        if row == 3 and kick:
            # $EF13: the tile in the row's leading slot becomes the kick tile,
            # unless the small player's third swim frame already has $48 there
            if not (small and t0 == block[SWIM_TILE_REP]):
                t0 = block[PLAYER_KICK + (1 if small else 0)]
        if facing & 2:
            pieces += [(0, 8 * row, t1, attrib | 0x40), (8, 8 * row, t0, attrib | 0x40)]
        else:
            pieces += [(0, 8 * row, t0, attrib), (8, 8 * row, t1, attrib)]
    # ChkForPlayerAttrib ($F0E9): rows whose two tiles are one tile, mirrored --
    # the left slot unflipped, the right flipped, whichever way the player faces
    rows = ()
    if killed or offset == 0xC8:
        rows = (2, 3)
    elif offset in (0x50, 0xB8, 0xC0):
        rows = (3,)
    for row in rows:
        x, y, t, a = pieces[2 * row]
        pieces[2 * row] = (x, y, t, a & 0x3F)
        x, y, t, a = pieces[2 * row + 1]
        pieces[2 * row + 1] = (x, y, t, (a & 0x3F) | 0x40)
    return pieces


def _player_offset(block: bytes, action: int) -> int:
    return block[PLAYER_OFFSETS + action]


def player_sequences(block: bytes, which: str) -> list[tuple[str, list]]:
    """``(label, [frame description per tick])`` for one ``set``."""
    out: list[tuple[str, list]] = []

    def still(label, action, **kw):
        out.append((label, [(_player_offset(block, action), kw)]))

    def cycle(label, action, steps, ticks, **kw):
        base = _player_offset(block, action)
        out.append(
            (label, [(base + 8 * k, kw) for k in range(steps) for _ in range(ticks)])
        )

    if which in ("big", "small"):
        s = 0 if which == "big" else SMALL
        small = which == "small"
        still("standing", STAND + s)
        for label, ticks in zip(
            ("walk (fast)", "walk", "walk (slow)"), ANIM_TIMER, strict=True
        ):
            cycle(label, WALK + s, 3, ticks)
        still("skid", SKID + s)
        still("jump", JUMP + s)
        # Swimming in the water's open space: the kick tile replaces the last
        # row's leading tile every other four frames ($EF0C: FrameCounter & 4)
        base = _player_offset(block, SWIM + s)
        out.append(
            (
                "swim (drifting)",
                [(base, {"kick": (t & 4) == 0, "small": small}) for t in range(8)],
            )
        )
        out.append(
            (
                "swim (stroke)",
                [
                    (
                        base + 8 * ((t // ANIM_TIMER[2]) % 3),
                        {"kick": (t & 4) == 0, "small": small},
                    )
                    for t in range(3 * ANIM_TIMER[2] * 4)
                ],
            )
        )
        cycle("climb up", CLIMB + s, 2, CLIMB_TIMER[0])
        cycle("climb down", CLIMB + s, 2, CLIMB_TIMER[1])
        if which == "big":
            still("crouch", CROUCH)
            still("throw a fireball", THROW)
        else:
            out.append(("killed", [(_player_offset(block, KILLED), {"killed": True})]))
    else:
        grow = _player_offset(block, GROW)
        out.append(
            (
                "grow",
                [(grow + 8 * CHANGE_STEPS[k], {}) for k in range(10) for _ in range(4)],
            )
        )
        out.append(
            (
                "shrink",
                [
                    (
                        _player_offset(
                            block, SMALL + SWIM if CHANGE_STEPS[10 + k] else SWIM
                        ),
                        {},
                    )
                    for k in range(10)
                    for _ in range(4)
                ],
            )
        )
    return out


# ---------------------------------------------------------------------------
# Enemies ($E73E-$E87C as one slice)

ENEMY_GFX = 0  # EnemyGraphicsTable: 43 frames x 6 tile numbers
ENEMY_OFFSETS = 258  # EnemyGfxTableOffsets: 27, by gfx id
ENEMY_ATTRS = ENEMY_OFFSETS + 27  # EnemyAttributeData: 27, by gfx id
ENEMY_ANIM_MASK = ENEMY_ATTRS + 27  # EnemyAnimTimingBMask: 2
JUMPSPRING_OFFSETS = ENEMY_ANIM_MASK + 2  # JumpspringFrameOffsets: 5 gfx ids
ENEMY_BYTES = JUMPSPRING_OFFSETS + 5


@dataclass(frozen=True)
class EnemyRam:
    """The RAM ``EnemyGfxHandler`` reads, named as the disassembly names it."""

    id: int  # Enemy_ID ($16,x)
    state: int = 0  # Enemy_State ($1E,x)
    moving_dir: int = 1  # Enemy_MovingDir ($46,x): 1 right, 2 left
    spr_attrib: int = 0  # Enemy_SprAttrib ($03C5,x)
    piranha_speed: int = 0  # PiranhaPlant_Y_Speed ($58,x)
    frame_timer: int = 0  # EnemyFrameTimer ($078A,x)
    y_speed: int = 0  # Enemy_Y_Speed ($A0,x)
    interval_timer: int = 0  # EnemyIntervalTimer ($0796,x): 5 or more holds a pose
    frame_counter: int = 0  # FrameCounter ($09)
    timer_control: int = 0  # TimerControl ($0747)
    bowser_gfx: int = 0  # BowserGfxFlag ($036A): 1 front, 2 rear
    bowser_body: int = 0  # BowserBodyControls ($0363)
    world: int = 0  # WorldNumber ($075F)
    jumpspring: int = 0  # JumpspringAnimCtrl ($070E)
    frenzy_timer: int = 0x20  # FrenzyEnemyTimer ($078F)


def draw_enemy(block: bytes, r: EnemyRam) -> list[Piece] | None:
    """The six OAM entries ``EnemyGfxHandler`` writes, or None where it returns
    without drawing (a piranha plant down its pipe). y is relative to
    ``Enemy_Y_Position``, x to the enemy's screen X."""
    gfx = block[ENEMY_GFX:ENEMY_OFFSETS]
    offsets = block[ENEMY_OFFSETS:ENEMY_ATTRS]
    attrs = block[ENEMY_ATTRS:ENEMY_ANIM_MASK]
    mask = block[ENEMY_ANIM_MASK:JUMPSPRING_OFFSETS]
    jspring = block[JUMPSPRING_OFFSETS:ENEMY_BYTES]
    y = 0  # $02
    vflip = 0  # VerticalFlipFlag ($0109)
    flip = r.moving_dir  # $03
    attr = r.spr_attrib  # $04
    if r.id == 0x0D and not r.piranha_speed & 0x80 and r.frame_timer:
        return None  # $E89B: a piranha plant below the pipe's rim
    ed = r.state  # $ED
    ec = r.state & 0x1F  # $EC (Y)
    a = r.id
    if a == 0x35:  # retainer: drawn as gfx id $15, facing right
        ec, flip, a = 0, 1, 0x15
    if a == 0x33:  # cannon's bullet bill: up a pixel, palette 3, behind the
        y -= 1  # background while it leaves the cannon
        attr = 0x03 | (0x20 if r.frame_timer else 0)
        ec, ed, a = 0, 0, 0x08
    if a == 0x32:  # jumpspring: its gfx id is the animation step's
        ec = 3
        a = jspring[r.jumpspring]
    ef = a  # $EF
    if ef == 0x0C and not r.y_speed & 0x80:
        vflip += 1  # a podoboo falling
    if r.bowser_gfx:
        ef = 0x16 if r.bowser_gfx == 1 else 0x17
    if ef == 0x06:  # goomba: flattened, and its walk is a flip every 8 frames
        if r.state >= 2:
            ec = 4
        if not ((r.state & 0x20) | r.timer_control) and not r.frame_counter & 0x08:
            flip ^= 3
    attr |= attrs[ef]
    x = offsets[ef]
    if r.bowser_gfx:
        if r.bowser_gfx == 1:
            if r.bowser_body & 0x80:
                x = 0xDE  # mouth open
        else:
            if r.bowser_body & 0x01:
                x = 0xE4  # other leg forward
            if ed & 0x20:
                y -= 0x10
        if ed & 0x20:
            vflip = x
        return _enemy_finish(gfx, r, x, y, flip, attr, vflip, ef, ec)
    stage = "hammer"
    if x == 0x24:  # spiny; state 5 is the egg Lakitu throws, drawn flipped
        if ec == 5:
            x, flip, ec = 0x30, 2, 5
    elif x == 0x90:  # lakitu: arm up while the next throw is due
        if not ed & 0x20 and r.frenzy_timer < 0x10:
            x = 0x96
        stage = "defeated"
    else:
        if ef < 4 and ec >= 2:  # upside-down shell
            x = 0x5A
            if ef == 2:
                x = 0x7E
                y += 1
        if ec == 4:  # right-side-up shell, or a stomped goomba
            x = 0x72
            y += 1
            if ef != 2:
                x = 0x66
                y += 1
            if ef == 6:
                x = 0x54
                if not ed & 0x20:
                    x = 0x8A
                    y -= 1
    if stage == "hammer":
        if ef == 0x05:
            if ed == 0:
                stage = "animate"
            elif not ed & 0x08:
                stage = "defeated"
            else:
                x = 0xB4  # throwing
                stage = "animate"
        elif x == 0x48:
            stage = "animate"
        elif r.interval_timer >= 5:
            stage = "defeated"
        elif x == 0x3C:  # blooper
            if r.interval_timer == 1:
                stage = "defeated"
            else:
                y += 3
                stage = "stop"
        else:
            stage = "animate"
    if stage == "animate":
        if ef in (0x06, 0x08, 0x0C) or ef >= 0x18:
            stage = "defeated"
        elif ef == 0x15:
            if r.world < 7:
                x, ec = 0xA2, 3  # toad; world 8's retainer is the princess
            stage = "defeated"
        elif r.frame_counter & mask[0]:
            stage = "defeated"
        else:
            stage = "stop"
    if stage == "stop":  # CheckAnimationStop: the second frame of the pair
        if not ((ed & 0xA0) | r.timer_control):
            x += 6
    if ed & 0x20 and ef >= 4:
        vflip, ec = 1, 0
    return _enemy_finish(gfx, r, x, y, flip, attr, vflip, ef, ec)


def _enemy_finish(gfx, r, x, y, flip, attr, vflip, ef, ec) -> list[Piece]:
    s = []  # [x, y, tile, attr] x 6, in OAM order
    for row in range(3):
        t0, t1 = gfx[x + 2 * row], gfx[x + 2 * row + 1]
        yy = y + 8 * row
        if flip & 2:
            s += [[0, yy, t1, attr | 0x40], [8, yy, t0, attr | 0x40]]
        else:
            s += [[0, yy, t0, attr], [8, yy, t1, attr]]
    if ef == 0x08:
        return [tuple(p) for p in s]
    if vflip:  # $EA64: every piece upside down, and two rows swap tiles
        a = s[0][3] | 0x80
        for p in s:
            p[3] = a
        k = 0 if ef in (0x05, 0x11) or ef >= 0x15 else 2
        s[k][2], s[4][2] = s[4][2], s[k][2]
        s[k + 1][2], s[5][2] = s[5][2], s[k + 1][2]
    if r.bowser_gfx or ef == 0x05:
        return [tuple(p) for p in s]
    mirror = ef in (0x07, 0x0D, 0x0C)
    if not mirror:
        if ef == 0x12 and ec != 5:
            mirror = None  # spiny on foot: straight to the lakitu check
        else:
            if ef == 0x15:
                s[5][3] = 0x42
            mirror = ec >= 2
    if mirror:  # FlipEnemyBody: the right column is the left one, mirrored
        a = s[0][3] & 0xA3
        for k in (0, 2, 4):
            s[k][3] = a
        a |= 0x40
        if ec == 5:
            a |= 0x80
        for k in (1, 3, 5):
            s[k][3] = a
        if ec == 4:
            a = s[2][3] | 0x80
            s[2][3] = s[4][3] = a
            a |= 0x40
            s[3][3] = s[5][3] = a
    if ef == 0x11:  # lakitu: his cloud, and his arm, are mirrored halves
        if not vflip:
            s[4][3] &= 0x81
            s[5][3] |= 0x41
            if r.frenzy_timer < 0x10:
                s[3][3] = s[5][3]
                s[2][3] = s[5][3] & 0x81
        else:
            s[0][3] &= 0x81
            s[1][3] |= 0x41
    elif ef >= 0x18:  # jumpspring: the lower rows mirrored both ways
        s[2][3] = s[4][3] = 0x82
        s[3][3] = s[5][3] = 0xC2
    return [tuple(p) for p in s]


def _bowser(block: bytes, r: EnemyRam) -> list[Piece]:
    """Both halves: the front object, and the rear one $D17E places 16 pixels
    behind it (on the side it is not facing) and 8 lower."""
    front = draw_enemy(block, replace(r, bowser_gfx=1)) or []
    rear = draw_enemy(block, replace(r, bowser_gfx=2)) or []
    dx = 0x10 if not r.moving_dir & 1 else -0x10
    return front + [(x + dx, y + 8, t, a) for x, y, t, a in rear]


# Scenario: (label, base RAM, ticks, per-tick RAM changes). A tick is one frame;
# FrameCounter is the tick number.
def _s(label, ticks=1, **ram):
    return (label, ram, ticks)


ENEMY_KINDS: dict[str, tuple[str, list]] = {
    "goomba": (
        "Goomba",
        [
            _s("walk", 16, id=0x06),
            _s("stomped", id=0x06, state=0x04),
            _s("defeated", id=0x06, state=0x20),
        ],
    ),
    "koopa-green": (
        "Koopa Troopa, green",
        [
            _s("walk", 16, id=0x00),
            _s("in its shell", id=0x00, state=0x04, interval_timer=0x10),
            _s("waking in its shell", 16, id=0x00, state=0x04),
            _s("upside down", id=0x00, state=0x03, interval_timer=0x10),
            _s("waking upside down", 16, id=0x00, state=0x03),
        ],
    ),
    "koopa-red": (
        "Koopa Troopa, red",
        [
            _s("walk", 16, id=0x03),
            _s("in its shell", id=0x03, state=0x04, interval_timer=0x10),
            _s("waking in its shell", 16, id=0x03, state=0x04),
            _s("upside down", id=0x03, state=0x03, interval_timer=0x10),
            _s("waking upside down", 16, id=0x03, state=0x03),
        ],
    ),
    "paratroopa": (
        "Koopa Paratroopa",
        [
            _s("green, hopping", 16, id=0x0E),
            _s("red, flying", 16, id=0x0F),
            _s("green, flying", 16, id=0x10),
            _s("green, standing still", 16, id=0x09),
        ],
    ),
    "buzzy-beetle": (
        "Buzzy Beetle",
        [
            _s("walk", 16, id=0x02),
            _s("in its shell", id=0x02, state=0x04, interval_timer=0x10),
            _s("waking in its shell", 16, id=0x02, state=0x04),
            _s("upside down", id=0x02, state=0x03, interval_timer=0x10),
            _s("waking upside down", 16, id=0x02, state=0x03),
        ],
    ),
    "spiny": (
        "Spiny and its egg",
        [
            _s("walk", 16, id=0x12),
            _s("egg", 16, id=0x12, state=0x05),
            _s("defeated", id=0x12, state=0x20),
        ],
    ),
    "hammer-bro": (
        "Hammer Bro",
        [
            _s("walk", 16, id=0x05),
            _s("throwing", 16, id=0x05, state=0x08),
            _s("defeated", id=0x05, state=0x20),
        ],
    ),
    "blooper": (
        "Blooper",
        [
            _s("drifting", id=0x07, interval_timer=0x10),
            _s("contracted", id=0x07),
            _s("defeated", id=0x07, state=0x20),
        ],
    ),
    "cheep-cheep": (
        "Cheep-cheep",
        [
            _s("grey, swimming", 16, id=0x0A),
            _s("red, swimming", 16, id=0x0B),
            _s("leaping", 16, id=0x14),
            _s("defeated", id=0x0A, state=0x20),
        ],
    ),
    "piranha-plant": ("Piranha Plant", [_s("biting", 16, id=0x0D, piranha_speed=0x80)]),
    "podoboo": (
        "Podoboo",
        [_s("rising", id=0x0C, y_speed=0xFC), _s("falling", id=0x0C, y_speed=0x04)],
    ),
    "lakitu": (
        "Lakitu",
        [
            _s("flying", id=0x11),
            _s("throwing", id=0x11, frenzy_timer=0x08),
            _s("defeated", id=0x11, state=0x20),
        ],
    ),
    "bullet-bill": (
        "Bullet Bill",
        [_s("flying", id=0x08), _s("leaving its cannon", id=0x33, frame_timer=1)],
    ),
    "bowser": (
        "Bowser",
        [
            _s("walk", 64, id=0x2D, moving_dir=2, bowser_body=None),
            _s("breathing fire", id=0x2D, moving_dir=2, bowser_body=0x80),
            _s("defeated", id=0x2D, moving_dir=2, state=0x20),
        ],
    ),
    "retainer": (
        "Toad and the Princess",
        [
            _s("Toad (worlds 1-7)", id=0x35),
            _s("the Princess (world 8)", id=0x35, world=7),
        ],
    ),
    "jumpspring": ("Jumpspring", [_s("bounce", 20, id=0x32, jumpspring=None)]),
}


def enemy_ticks(block: bytes, kind: str) -> list[tuple[str, list[list[Piece]]]]:
    """Each scenario of ``kind`` as ``(label, pieces per tick)``."""
    out = []
    for label, ram, ticks in ENEMY_KINDS[kind][1]:
        per_tick = []
        for t in range(ticks):
            fields = dict(ram, frame_counter=t)
            if fields.get("bowser_body", 0) is None:  # legs swap every $20 frames
                fields["bowser_body"] = (t // 0x20) & 1
            if fields.get("jumpspring", 0) is None:  # one step every 4 frames
                fields["jumpspring"] = min(t // 4, 4)
            r = EnemyRam(**fields)
            pieces = _bowser(block, r) if r.id == 0x2D else draw_enemy(block, r)
            per_tick.append(pieces or [])
        out.append((label, per_tick))
    return out


# ---------------------------------------------------------------------------
# Small table-driven objects: each choice reads its own slice


def _row(t0, t1, attr, y=0):
    return [(0, y, t0, attr), (8, y, t1, attr)]


def _powerup(d: bytes) -> list[tuple[str, list[list[Piece]]]]:
    """PowerUpGfxTable ($E6BE, 4 x 2x2) + PowerUpAttributes ($E6CE), drawn by
    $E6D2: mushroom and 1-up as stored; the flower's top row and the whole
    star cycle palettes every 2 frames, and both mirror their right column."""
    out = []
    for n, label in enumerate(("mushroom", "fire flower", "star", "1-up mushroom")):
        t = d[4 * n : 4 * n + 4]
        base = d[16 + n]
        ticks = []
        for f in range(8 if n in (1, 2) else 1):
            p = _row(t[0], t[1], base) + _row(t[2], t[3], base, 8)
            if n in (1, 2):
                cyc = (f >> 1) & 3
                rows = (0, 1) if n == 2 else (0,)
                p = [
                    (
                        x,
                        y,
                        tt,
                        (cyc if (k // 2) in rows else a) | (0x40 if k % 2 else 0),
                    )
                    for k, (x, y, tt, a) in enumerate(p)
                ]
            ticks.append(p)
        out.append((label, ticks))
    return out


def _coin(d: bytes):
    """JumpingCoinTiles ($E682): one tile, and the same tile flipped below it
    ($E6B1: attributes $02, $82); the frame changes every 2 frames."""
    return [
        (
            "spin",
            [
                [(0, 0, d[(f >> 1) & 3], 0x02), (0, 8, d[(f >> 1) & 3], 0x82)]
                for f in range(8)
            ],
        )
    ]


def _pairs(d: bytes, attr: int, labels):
    """Tile pairs drawn side by side, one frame each."""
    return [
        (label, [_row(d[2 * n], d[2 * n + 1], attr)]) for n, label in enumerate(labels)
    ]


def _hammer(d: bytes):
    """$E4C0: FirstSprXPos, FirstSprYPos, SecondSprXPos, SecondSprYPos,
    FirstSprTilenum, SecondSprTilenum, HammerSprAttrib -- four bytes each,
    one per spin step, the step being FrameCounter >> 2 & 3 ($E4F0)."""
    steps = []
    for k in range(4):
        steps.append(
            [
                (d[k], d[4 + k], d[16 + k], d[24 + k]),
                (d[8 + k], d[12 + k], d[20 + k], d[24 + k]),
            ]
        )
    return [("spinning", [steps[(f >> 2) & 3] for f in range(16)])]


def _star_flag(d: bytes):
    """$D2CD: Y offsets, X offsets, tiles (4 each); attribute $22 ($D37D)."""
    return [("castle flag", [[(d[4 + k], d[k], d[8 + k], 0x22) for k in range(4)]])]


def _explosion(d: bytes):
    """ExplosionTiles ($ED06): one tile drawn four ways around the centre
    ($ED22-$ED5D), a step every 2 frames (Fireball_State >> 1)."""
    return [
        (
            "explosion",
            [
                [
                    (-4, -4, d[f >> 1], 0x02),
                    (-4, 4, d[f >> 1], 0x82),
                    (4, -4, d[f >> 1], 0x42),
                    (4, 4, d[f >> 1], 0xC2),
                ]
                for f in range(6)
            ],
        )
    ]


def _block(d: bytes):
    """DefaultBlockObjTiles ($EBCD), drawn by $EBD1 with attribute 3: as stored
    above ground, its top row replaced by $86 elsewhere ($EBFB)."""
    return [
        ("bumped brick, above ground", [_row(d[0], d[1], 3) + _row(d[2], d[3], 3, 8)]),
        ("bumped brick, other areas", [_row(0x86, 0x86, 3) + _row(d[2], d[3], 3, 8)]),
    ]


FLOATEY = (
    "100",
    "200",
    "400",
    "500",
    "800",
    "1000",
    "2000",
    "4000",
    "5000",
    "8000",
    "1-UP",
    "unused",
)
OBJECTS: dict[str, tuple[str, int, object]] = {
    # key: (label, slice length, builder)
    "powerup": ("Power-ups", 20, _powerup),
    "coin": ("Jumping coin", 4, _coin),
    "floatey": ("Floating scores", 24, lambda d: _pairs(d, 0x02, FLOATEY)),
    "flagpole-score": (
        "Flagpole scores",
        10,
        lambda d: _pairs(d, 0x01, ("5000", "2000", "800", "400", "100")),
    ),
    "hammer": ("Hammer", 28, _hammer),
    "star-flag": ("Castle flag", 12, _star_flag),
    "explosion": ("Explosion", 3, _explosion),
    "block": ("Bumped block", 4, _block),
}


# ---------------------------------------------------------------------------
# Objects with no table: the immediates in their draw routines.
#
# Each lists (byte offset into the slice, opcode) for every operand it reads,
# and builds its frames from those operands. The slice starts at ``start``.


@dataclass(frozen=True)
class CodeSprite:
    label: str
    start: int  # CPU address of the slice's first byte
    operands: tuple[tuple[str, int, int], ...]  # (name, CPU address of opcode, opcode)
    build: object  # (operands dict) -> [(label, [pieces per tick])]

    @property
    def length(self) -> int:
        """Through the last operand: the slice is the code that holds them."""
        return max(at for _n, at, _o in self.operands) + 2 - self.start


def _fireball(o):
    # $ECED: tile = base ^ (FrameCounter >> 2 & 1); flipped both ways when
    # FrameCounter bit 3 is set
    return [
        (
            "spinning",
            [
                [
                    (
                        0,
                        0,
                        o["tile"] ^ 1 if (f >> 2) & 1 else o["tile"],
                        o["attr"] | (o["flip"] if f & 8 else 0),
                    )
                ]
                for f in range(16)
            ],
        )
    ]


def _flame(o):
    # $D227: three consecutive tiles in a row; vertical flip on FrameCounter bit 1
    return [
        (
            "flame",
            [
                [
                    (
                        8 * k,
                        0,
                        o["tile"] + k if k else o["tile"],
                        o["attr2"] if f & 2 else o["attr"],
                    )
                    for k in range(3)
                ]
                for f in range(4)
            ],
        )
    ]


def _vine(o):
    # $E441: six pieces 8 pixels apart down the screen, alternate ones 6 pixels
    # right and mirrored; the first is the leaf tile on the vine's top segment
    def seg(top):
        p = []
        for k in range(6):
            tile = o["top"] if (top and k == 0) else o["tile"]
            a = o["attr"] | (o["flip"] if k % 2 else 0)
            p.append((o["dx"] if k % 2 else 0, 8 * k, tile, a))
        return p

    return [("top segment", [seg(True)]), ("segment", [seg(False)])]


def _flag(o):
    # $E54B: the flag on the pole -- $7E, $7F beside it, $7E again below that
    return [
        (
            "flag",
            [
                [
                    (0, 0, o["t0"], o["attr"]),
                    (8, 0, o["t1"], o["attr"]),
                    (8, 8, o["t0"], o["attr"]),
                ]
            ],
        )
    ]


def _lift(o):
    # $E5C8: six pieces in a row; four in castles and the second quest ($E5DD);
    # the cloud tile where the area overrides it ($E5F6)
    return [
        ("lift", [[(8 * k, 0, o["tile"], o["attr"]) for k in range(6)]]),
        (
            "lift, castle and hard mode",
            [[(8 * k, 0, o["tile"], o["attr"]) for k in range(4)]],
        ),
        ("cloud", [[(8 * k, 0, o["cloud"], o["attr"]) for k in range(6)]]),
    ]


def _small_lift(o):
    # $ED66: three pieces in a row ($ED77-$ED8F)
    return [("lift", [[(8 * k, 0, o["tile"], o["attr"]) for k in range(3)]])]


def _bubble(o):
    return [("bubble", [[(0, 0, o["tile"], o["attr"])]])]


def _chunks(o):
    # $EC53: one tile per chunk, flipped by FrameCounter bits 2 and 3
    out = []
    for label, tile, attr in (
        ("brick chunk", o["tile"], o["attr"]),
        ("brick chunk, end of a castle", o["tile2"], o["attr2"]),
    ):
        out.append(
            (label, [[(0, 0, tile, attr | ((f << 4) & 0xC0))] for f in range(16)])
        )
    return out


CODE_SPRITES: dict[str, CodeSprite] = {
    "fireball": CodeSprite(
        "Fireball",
        0xECF4,
        (("tile", 0xECF4, 0x49), ("attr", 0xECFC, 0xA9), ("flip", 0xED00, 0x09)),
        _fireball,
    ),
    "bowser-flame": CodeSprite(
        "Bowser's flame",
        0xD227,
        (("tile", 0xD227, 0xA9), ("attr", 0xD22B, 0xA0), ("attr2", 0xD233, 0xA0)),
        _flame,
    ),
    "vine": CodeSprite(
        "Vine",
        0xE455,
        (
            ("dx", 0xE456, 0x69),
            ("attr", 0xE461, 0xA9),
            ("flip", 0xE46C, 0x09),
            ("tile", 0xE479, 0xA9),
            ("top", 0xE48B, 0xA9),
        ),
        _vine,
    ),
    "flag": CodeSprite(
        "Flagpole flag",
        0xE571,
        (("attr", 0xE571, 0xA9), ("t0", 0xE580, 0xA9), ("t1", 0xE588, 0xA9)),
        _flag,
    ),
    "lift": CodeSprite(
        "Lift",
        0xE5F4,
        (("tile", 0xE5F4, 0xA9), ("cloud", 0xE5FB, 0xA9), ("attr", 0xE603, 0xA9)),
        _lift,
    ),
    "small-lift": CodeSprite(
        "Small lift",
        0xED69,
        (("tile", 0xED69, 0xA9), ("attr", 0xED70, 0xA9)),
        _small_lift,
    ),
    "bubble": CodeSprite(
        "Air bubble", 0xEDFC, (("tile", 0xEDFC, 0xA9), ("attr", 0xEE01, 0xA9)), _bubble
    ),
    "brick-chunks": CodeSprite(
        "Brick chunks",
        0xEC53,
        (
            ("attr2", 0xEC53, 0xA9),
            ("tile2", 0xEC57, 0xA9),
            ("attr", 0xEC5F, 0xA9),
            ("tile", 0xEC63, 0xA9),
        ),
        _chunks,
    ),
}


def code_operands(sprite: CodeSprite, data: bytes) -> dict[str, int] | None:
    """Each operand, or None if any opcode is not the one this was read from."""
    out = {}
    for name, at, opcode in sprite.operands:
        k = at - sprite.start
        if k + 1 >= len(data) or data[k] != opcode:
            return None
        out[name] = data[k + 1]
    return out


# ---------------------------------------------------------------------------
# Scenario lists -> frames and sequences


def _subsprite(piece: Piece) -> Subsprite:
    x, y, tile, attr = piece
    return Subsprite(
        x=int(x),
        y=int(y),
        index=int(tile),
        record=getattr(tile, "at", None),
        palette_row=attr & 3,
        priority=(attr >> 5) & 1,
        flip_h=bool(attr & 0x40),
        flip_v=bool(attr & 0x80),
    )


def compile_scenarios(
    scenarios: list[tuple[str, list[list[Piece]]]],
) -> tuple[list[Frame], tuple[Sequence, ...], list[str]]:
    """Distinct poses in first-seen order, one run-length-encoded sequence per
    scenario, and the scenarios' labels in sequence order."""
    index: dict[tuple, int] = {}
    frames: list[Frame] = []
    sequences = []
    labels = []
    for label, ticks in scenarios:
        steps: list[Step] = []
        for pieces in ticks:
            # The offset is part of a pose: two drawn from different bytes are
            # two frames, since an edit to one leaves the other alone.
            key = tuple(
                (int(x), int(y), int(t), int(a), getattr(t, "at", None))
                for x, y, t, a in pieces
            )
            if key not in index:
                index[key] = len(frames)
                frames.append(tuple(_subsprite(p) for p in pieces))
            n = index[key]
            if steps and steps[-1].frame == n:
                steps[-1] = Step(duration=steps[-1].duration + 1, frame=n)
            else:
                steps.append(Step(duration=1, frame=n))
        sequences.append(Sequence(tuple(steps)))
        labels.append(label)
    return frames, tuple(sequences), labels


def player_scenarios(block: bytes, which: str):
    out = []
    for label, ticks in player_sequences(block, which):
        out.append(
            (
                label,
                [
                    draw_player(
                        block,
                        off,
                        killed=kw.get("killed", False),
                        kick=kw.get("kick", False),
                        small=kw.get("small", False),
                    )
                    for off, kw in ticks
                ],
            )
        )
    return out


def scenarios_for(fmt: str, choice: str, data: bytes):
    data = Tracked(data)
    if fmt == PLAYER_ID:
        return player_scenarios(data, choice) if len(data) >= PLAYER_BYTES else []
    if fmt == ENEMY_ID:
        return enemy_ticks(data, choice) if len(data) >= ENEMY_BYTES else []
    if fmt == OBJECT_ID:
        _label, length, build = OBJECTS[choice]
        return build(data) if len(data) >= length else []
    sprite = CODE_SPRITES[choice]
    operands = code_operands(sprite, data)
    return sprite.build(operands) if operands else []


# ---------------------------------------------------------------------------
# The formats


class _SmbSprites:
    """One cell per byte; frames from the scenarios. A ``fixed`` layout: the
    draw routines place every piece, so only the tile bytes can change."""

    fmt = ""
    choice_key = ""
    options: tuple[tuple[str, str], ...] = ()

    def __init__(self, name: str, label: str, tooltip: str) -> None:
        self.info = FormatInfo(
            id=self.fmt,
            name=name,
            category="Project",
            declares=_DECLARES,
            inputs=(
                InputSpec(
                    self.choice_key,
                    label,
                    InputKind.CHOICE,
                    required=False,
                    tooltip=tooltip,
                    options=self.options,
                ),
            ),
        )

    def bytes_per_cell(self) -> int:
        return 1

    def cell_tiles(self) -> tuple[int, int]:
        return (1, 1)

    def _choice(self, ctx: PipelineContext) -> str:
        inputs = ctx.get(KEY_INPUTS) or {}
        return inputs.get(self.choice_key, self.options[0][0])

    def decode(self, data: bytes, ctx: PipelineContext) -> list[Cell]:
        _frames, sequences, _labels = compile_scenarios(
            scenarios_for(self.fmt, self._choice(ctx), bytes(data))
        )
        ctx.set(KEY_TILEMAP_ANIMATIONS, sequences)
        return [Cell(index=b) for b in data]

    def encode(self, cells: list[Cell], ctx: PipelineContext) -> bytes:
        return bytes(c.index & 0xFF for c in cells)

    def index_limit(self) -> int:
        return 0xFF

    def cell_fields(self) -> dict[str, int]:
        # Flips and palettes come from code and attribute tables, not from the
        # byte a piece names, so the tile is the only field a cell holds.
        return {"index": 0xFF}

    def subsprite_limits(self, ctx: PipelineContext) -> SubspriteLimits:
        return SubspriteLimits("fixed", range(0), range(0))

    def place_subsprite(  # noqa: PLR0913
        self, cell: Cell, ctx: PipelineContext, *, x=None, y=None, size=None, drawn=None
    ) -> Cell:
        if any(value is not None for value in (x, y, size, drawn)):
            raise ValueError("the draw routine places these pieces")
        return cell

    def frames(self, cells: list[Cell], ctx: PipelineContext) -> list[Frame]:
        frames, _sequences, _labels = compile_scenarios(
            scenarios_for(self.fmt, self._choice(ctx), self.encode(cells, ctx))
        )
        return frames


class SmbPlayer(_SmbSprites):
    fmt, choice_key = PLAYER_ID, "set"
    options = (("big", "Big"), ("small", "Small"), ("change", "Growing and shrinking"))


class SmbEnemy(_SmbSprites):
    fmt, choice_key = ENEMY_ID, "enemy"
    options = tuple((k, v[0]) for k, v in ENEMY_KINDS.items())


class SmbObject(_SmbSprites):
    fmt, choice_key = OBJECT_ID, "object"
    options = tuple((k, v[0]) for k, v in OBJECTS.items())


class SmbCodeSprite(_SmbSprites):
    fmt, choice_key = CODE_ID, "object"
    options = tuple((k, v.label) for k, v in CODE_SPRITES.items())


def register(registry):  # noqa: ANN001, ANN201 - the discovery registrar
    registry.register_format(
        SmbPlayer(
            "SMB player (PlayerGraphicsTable, drawn as $EFBE draws it)",
            "Set",
            "Which of the player's frame sets to draw:\nbig, small, or the size changes.",
        )
    )
    registry.register_format(
        SmbEnemy(
            "SMB enemy (EnemyGraphicsTable, drawn as $E87D draws it)",
            "Enemy",
            "Which enemy's draw to replay. Every one reads\nthe same five tables.",
        )
    )
    registry.register_format(
        SmbObject(
            "SMB object (a small sprite table and its draw)",
            "Object",
            "Which table this slice is: each has its own\ndraw routine and length.",
        )
    )
    registry.register_format(
        SmbCodeSprite(
            "SMB code sprite (tile numbers that are operands)",
            "Object",
            "Which draw routine this run of code belongs to:\nits immediates are the sprite.",
        )
    )
