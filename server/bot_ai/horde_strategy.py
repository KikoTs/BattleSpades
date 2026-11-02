"""Team brain for the Zombie horde: who hunts whom, and how.

Retail Zombie mode hands the infected a strategic target set: every survivor
is a horde target (the mode reveals entombed survivors and the last
survivor's marker on the map). Each infected bot used to run at the nearest
survivor on its own, so a survivor on a pillar or a sky platform collected
the whole horde in one pile under his feet while everyone else was safe.

``HordeCoordinator`` runs on the gameplay thread (director) once per
perception snapshot and assigns every infected bot one order:

``hunt``      run at the target (spread across survivors with a load penalty
              and hysteresis, so the horde splits instead of dog-piling one).
              The default: the horde rushes.
``tunnel``    claw straight through what stands between the bot and its
              target (a wall, a door, a barricade, a sealed room) whenever
              that is quicker than the way round, or there is no way round.
``dig_root``  claw one site of the minimum collapse cut under an elevated
              survivor (``structure_collapse``): when the cut is dug the
              server's own floating-block rule drops the base. Chosen when
              the cut is quicker than reaching him, or nothing reaches him.
``surround``  the rest of a besieging group waits just outside the footprint
              of what is about to fall, never stacked under it.
``climb``     nothing walks up and no cheap cut exists (a natural spire, a
              thick fortress): dig a staircase / take the climb skill.
``flank``     the progress watchdog fired on a walker: try the target
              from another side (a second alarm lets it claw or climb).

Every choice is a comparison of estimated seconds (walking, from the
survivor's approach flood; clawing, from the claw's cadence), corrected by
what the bots are actually achieving: time without progress counts against
the way being tried.

Pure Python over plain tuples; the director supplies positions, siege
analyses and the world's ``solid`` probe. No server objects, no randomness
(stable ordering by player id), bounded memory (forgets departed bots).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import math
from typing import Callable, Iterable, Mapping, Sequence

from .structure_collapse import (
    PLAYER_SUPPORT_OFFSET,
    Approach,
    CollapsePlan,
    support_cells,
)

Vector3 = tuple[float, float, float]
Cell = tuple[int, int, int]
SolidFn = Callable[[int, int, int], bool]

ROLE_CODES: dict[str, int] = {
    "hunt": 0,
    "flank": 1,
    "dig_root": 2,
    "climb": 3,
    "surround": 4,
    "tunnel": 5,
    "advance": 6,
}
ROLE_NAMES: dict[int, str] = {code: name for name, code in ROLE_CODES.items()}

# Watchdog: no goal progress of at least PROGRESS_STEP blocks for STALL_SECONDS
# raises the bot's stuck level; sustained progress lowers it again. Measured
# over eight maps, three, five and seven seconds reach the survivors equally
# fast; three seconds raises twice the alarms for it, so the wait is five.
PROGRESS_STEP = 1.5
STALL_SECONDS = 5.0
RECOVER_SECONDS = 8.0
MAX_STUCK_LEVEL = 3
# Landed swings count as progress for this long without getting any closer:
# enough for a thick wall, not for digging into a hillside for ever.
WORK_CREDIT_SECONDS = 8.0
# Piling: hunters within this horizontal radius of an elevated target and at
# least PILE_DEPTH blocks below it are standing in a heap under the survivor.
PILE_RADIUS = 2.5
PILE_DEPTH = 3.0
ANALYSIS_RANGE_HINT = 48.0
MAX_DIGGERS = 6
# With no cut to dig, a third of the group climbs (at least two, at most
# four): one finished staircase carries everybody, but one bot digging it
# alone leaves the rest of the horde watching.
MIN_CLIMBERS = 2
MAX_CLIMBERS = 8
# The waiting ring stands this far out when the footing's size is unknown,
# and never closer than RING_MIN: just clear of the heap, one stride from
# where a falling survivor lands.
SURROUND_RADIUS = 8.0
RING_MIN = 6.0
RING_CLEARANCE = 2.0
TUNNEL_RANGE = 20.0
FLANK_RADIUS = 9.0
# A survivor at least this many blocks above the horde's floor is "elevated":
# bringing his footing down is worth comparing with walking up to him.
ELEVATED_RISE = 3
# A target that has moved this far since its footing was analysed (it fell,
# it jumped off) is hunted on sight; the old analysis describes an empty spot.
STALE_ANALYSIS_MOVE = 2.5

# Planning speeds, measured with scripts/bot_zombie_horde_scenario.py on the
# retail class speeds (Zombie sprint 13.2 blocks/s, walk 4.0; about half of a
# hunter's travel over broken ground is sprinted).
RUN_SPEED = 8.0
# A flood move is one cardinal cell; real bodies cut the diagonals.
MOVE_BLOCKS = 0.8
# One claw cube of a collapse cut: about three swings (ZOMBIEHAND 0.4 s, 2-10
# damage a cell against 5 for map voxels, 9 for built ones) and the shuffle.
SITE_SECONDS = 1.6
# Clawing a body-sized hole through one wall on the way, and through each
# further column of a thicker one.
WALL_SECONDS = 1.2
COLUMN_SECONDS = 0.4
# Seconds a way must save before the horde changes to it, and how hard time
# without progress counts against the way being tried.
CHOICE_MARGIN = 1.0
STALL_WEIGHT = 2.0
# A hunter's breach decision stands this long unless the ground changes.
BREACH_REFRESH = 0.5
# A hunter on a known way in is sent this many moves along it at a time (far
# enough to clear the corner the bot's own local planner cannot see round),
# but only when that way is a real detour or a climb: longer than the
# straight run by VIA_DETOUR blocks, or ending VIA_RISE blocks up.
VIA_AHEAD = 10
VIA_DETOUR = 4.0
VIA_RISE = 2.0
VIA_ENTRY_REACH = 24.0
# Besieging a footing: at most this share of the group waits on the ring,
# everyone else works (digs the cut, climbs to an attack position). A
# waiting zombie moves on to the next ring spot every PACE_SECONDS, each on
# its own beat, so the ring paces round the foot instead of standing still.
PRESSURE_SHARE = 0.34
PACE_SECONDS = 4.0
# A hunter this close under an elevated prey, with no walk up from where it
# stands and no known way in, is parked at the foot: it is sent to the ring.
UNDER_RADIUS = 7.0
UNDER_RISE = float(ELEVATED_RISE)
# Floors this close apart are the same level for a walker (a step, a jump).
LEVEL_TOLERANCE = 2
# A stand this close to the survivor, at his level or up to CLAW_UP blocks
# below his feet, is an attack position: the claw lands from there.
REACH_RADIUS = 2.5
CLAW_UP = 3
REACH_SEARCH = 3
STANDS_TTL = 1.0
# A climb destination that got a climber nowhere is avoided this long.
CLIMB_FAIL_SECONDS = 30.0
# A besieger this far above the prey's feet is not besieging: it hunts down.
ABOVE_PREY = 2.0
# A foundation cut's estimate is scaled by this before it may replace a
# reachable exact cut (its sites ring a whole base; the walks between them
# are not in the site model).
FOUNDATION_HANDICAP = 1.5
# Climbers stage on distinct sides of the footing before they start up, out
# of the survivor's sight where the ground allows: one side means one queue
# for his rifle. A climber within STAGE_REACH of its side has staged.
STAGE_RADIUS = 6.0
STAGE_REACH = 3.0
STAGE_TTL = 2.0
# One on the prey's level within this run is not besieging either: it hunts.
AT_LEVEL_REACH = 8.0

# Approach under fire. A survivor who has hit or killed a zombie lately is
# "dangerous" (``SurvivorTarget.threat_until``): zombies still farther than
# RALLY_NEAR from him walk to a rally spot on their own side first, a
# standable spot RALLY_RADII out that a straight line from his eye cannot
# reach, and wait there; when WAVE_MIN of the advancing zombies stand on
# their spots (or the first of them has waited WAVE_WAIT seconds) the whole
# wave goes, and for WAVE_SECONDS nobody new rallies: late arrivals join
# the rush. Measured on a live London server with a rifle on a roof pillar,
# zombies that trickled in one by one across the open were each shot at
# 35-50 blocks and respawned 110 blocks away; the horde never massed until
# the gun was dry (116 s).
RALLY_NEAR = 18.0
# Rally spots are street-level spots (within RALLY_LEVEL of the horde's
# floor: no rooftops) on these rings that his eye cannot see. Only real
# cover counts: a wave gathered in the open beyond his range (tried at
# 44-50 blocks) just stopped absorbing his fire and then charged 45 open
# blocks into it; the far-spawn harness got worse on every seed, and a
# horde standing about in the open is the sight Kiril complained of. A
# London roof pillar sees every street within 35 blocks, so there the horde
# comes straight in, weaving (``rush``).
RALLY_RADII = (22.0, 28.0, 34.0)
RALLY_BEARINGS = 16
RALLY_LEVEL = 3
RALLY_REACH = 3.0
RALLY_TTL = 2.0
RALLY_PER_SPOT = 3
# A wave needs WAVE_SHARE of the zombies advancing on him (at least
# WAVE_MIN) standing on their spots, or the first of them waiting
# WAVE_WAIT seconds. While a wave runs, zombies within WAVE_JOIN of him
# join it; farther ones (respawned across the map) rally for the next.
WAVE_MIN = 2
WAVE_SHARE = 0.5
WAVE_WAIT = 8.0
WAVE_SECONDS = 14.0
WAVE_JOIN = 60.0


@dataclass(frozen=True, slots=True)
class HordeMember:
    player_id: int
    position: Vector3
    is_bot: bool = True
    can_dig: bool = True
    can_build: bool = False
    # A claw swing of this bot landed within the last moments (on terrain or
    # on a survivor): standing still to dig is work, not a stall.
    working: bool = False
    build_levels: float = 0.0
    build_seconds_per_level: float = 1.25
    # Standing on something (an airborne body's closeness to a prey above
    # it is not progress: a zombie jumping at a man on a stub closes in and
    # falls back forever).
    grounded: bool = True


@dataclass(frozen=True, slots=True)
class SurvivorTarget:
    player_id: int
    position: Vector3
    # Until when he counts as dangerous (he hit or killed a zombie lately).
    threat_until: float = 0.0

    def dangerous(self, now: float) -> bool:
        return float(self.threat_until) > float(now)


@dataclass(frozen=True, slots=True)
class SiegeInfo:
    """Director's latest structural analysis of one survivor's footing."""

    isolated: bool
    floor_z: int
    plan: CollapsePlan | None = None
    analysed_at: float = 0.0
    # Walk-in distances around the survivor (None: not analysed yet) and
    # where he stood when they were taken.
    approach: Approach | None = None
    position: Vector3 | None = None
    # The structure's foundation cut (its contact with the ground: a tree's
    # trunk, a building's wall bases), when the exact cut is missing or out
    # of reach. The coordinator digs whichever of the two is quicker.
    foundation: CollapsePlan | None = None


@dataclass(frozen=True, slots=True)
class HordeOrder:
    zombie_id: int
    target_id: int
    role: str
    goal: Vector3
    cells: tuple[Cell, ...] = ()
    aim: Cell | None = None
    stuck_level: int = 0

    @property
    def role_code(self) -> int:
        return ROLE_CODES[self.role]


@dataclass(frozen=True, slots=True)
class WorkStand:
    """Somewhere a zombie can stand and do siege work.

    ``cut``: beside a claw site of the collapse cut (``site``/``cells``);
    ``reach``: within claw reach of the survivor himself; ``prey``: the
    survivor's own spot, when nothing else is known. ``floor_z`` is the
    floor voxel under the stand: a stand far above a zombie's own floor is
    a climb destination, not a walk.
    """

    goal: Vector3
    floor_z: int
    kind: str
    site: Cell | None = None
    cells: tuple[Cell, ...] = ()


def _stand_key(goal: Vector3) -> tuple[int, int, int]:
    return (int(math.floor(goal[0])), int(math.floor(goal[1])), _floor_of(goal))


def _cheb(a: Cell, b: Cell) -> int:
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]), abs(a[2] - b[2]))


def reach_stands(solid: SolidFn | None, position: Vector3, *,
                 radius: int = REACH_SEARCH) -> list[WorkStand]:
    """Open floors beside a survivor from which a claw reaches him.

    At his level, one above or up to CLAW_UP below his feet, within
    REACH_RADIUS horizontally and never his own support column: the roof
    round a short pillar, the deck beside him. None for a 1x1 pillar top.
    """

    if solid is None:
        return []
    support = support_cells(solid, position)
    if not support:
        return []
    columns = {(c[0], c[1]) for c in support}
    top = support[0][2]
    px, py = float(position[0]), float(position[1])
    found: list[WorkStand] = []
    for dx in range(-radius, radius + 1):
        for dy in range(-radius, radius + 1):
            x, y = int(math.floor(px)) + dx, int(math.floor(py)) + dy
            if (x, y) in columns or not (0 <= x < 512 and 0 <= y < 512):
                continue
            if math.hypot(x + 0.5 - px, y + 0.5 - py) > REACH_RADIUS + 0.5:
                continue
            for floor in range(top - 1, top + CLAW_UP + 1):
                if not 3 <= floor < 239:
                    continue
                if (solid(x, y, floor) and not solid(x, y, floor - 1)
                        and not solid(x, y, floor - 2) and not solid(x, y, floor - 3)):
                    found.append(WorkStand(
                        (x + 0.5, y + 0.5, float(floor) - PLAYER_SUPPORT_OFFSET),
                        floor, "reach"))
                    break
    found.sort(key=lambda s: (abs(s.floor_z - top),
                              math.hypot(s.goal[0] - px, s.goal[1] - py)))
    return found[:8]


@dataclass(slots=True)
class _Progress:
    goal_key: tuple
    best_distance: float
    progress_at: float
    level: int = 0
    level_since: float = 0.0
    anchor: Vector3 = (0.0, 0.0, 0.0)
    # When the body last got closer; digging renews ``progress_at`` only for
    # WORK_CREDIT_SECONDS after this.
    closed_at: float = 0.0


@dataclass(slots=True)
class HordeMetrics:
    stuck_events: int = 0
    tunnel_orders: int = 0
    dig_orders: int = 0
    surround_orders: int = 0
    climb_orders: int = 0
    max_pile: int = 0
    piles: dict[int, int] = field(default_factory=dict)
    # Targets currently besieged because the cut beats the walk although a
    # walk exists, and hunters sent through a wall before any stall.
    collapse_choices: int = 0
    breach_choices: int = 0
    # Climbers whose every known way up has failed them (they retry the
    # least-recently failed one), and climbers relieved for getting nowhere.
    ways_exhausted: int = 0
    relieved_climbers: int = 0
    # Waves released against a dangerous survivor (approach under fire).
    waves: int = 0


def _xy_distance(a: Vector3, b: Vector3) -> float:
    return math.hypot(float(a[0]) - float(b[0]), float(a[1]) - float(b[1]))


def _floor_of(position: Vector3) -> int:
    return int(round(float(position[2]) + PLAYER_SUPPORT_OFFSET))


def _ring_point(center: Vector3, slot: int, slots: int, radius: float,
                floor_z: int, phase: float = 0.0) -> Vector3:
    angle = phase + 2.0 * math.pi * (slot % max(1, slots)) / max(1, slots)
    return (
        float(center[0]) + radius * math.cos(angle),
        float(center[1]) + radius * math.sin(angle),
        float(floor_z) - PLAYER_SUPPORT_OFFSET,
    )


def standable_near(solid: SolidFn | None, point: Vector3, floor_z: int,
                   *, span: int = 3) -> Vector3 | None:
    """``point`` snapped onto an open floor within ``span`` blocks, or None.

    A flank or ring spot inside a wall or over a drop is worse than none:
    the bot stalls on it and the watchdog ends up tunnelling.
    """

    if solid is None:
        return point
    x, y = int(math.floor(point[0])), int(math.floor(point[1]))
    if not (0 <= x < 512 and 0 <= y < 512):
        return None
    for offset in sorted(range(-span, span + 1), key=abs):
        floor = int(floor_z) + offset
        if not 2 <= floor < 239:
            continue
        if solid(x, y, floor) and not solid(x, y, floor - 1) and not solid(x, y, floor - 2):
            return (x + 0.5, y + 0.5, float(floor) - PLAYER_SUPPORT_OFFSET)
    return None


def visible(solid: SolidFn | None, eye: Vector3, point: Vector3, *, step: float = 0.5) -> bool:
    """Does a straight line from ``eye`` to ``point`` cross no solid voxel?"""

    if solid is None:
        return True
    dx, dy, dz = (float(point[i]) - float(eye[i]) for i in range(3))
    span = math.sqrt(dx * dx + dy * dy + dz * dz)
    if span < 1e-6:
        return True
    steps = max(1, int(span / step))
    for i in range(1, steps):
        t = i / steps
        x = int(math.floor(float(eye[0]) + dx * t))
        y = int(math.floor(float(eye[1]) + dy * t))
        z = int(math.floor(float(eye[2]) + dz * t))
        if not (0 <= x < 512 and 0 <= y < 512 and 0 <= z < 240):
            continue
        if solid(x, y, z):
            return False
    return True


# Where a bot zombie comes back: a street-level spot RESPAWN_RADII from the
# nearest living survivor that no survivor's eye can see. A horde that walks
# back 110 blocks from the map's zombie spawn after every death leaves the
# man on his pillar half a minute of nothing between waves (measured live
# on London: 36 s lulls, collapse at 86-126 s); respawning out of sight but
# near keeps the pressure on and is how zombie servers have always played.
RESPAWN_RADII = (40.0, 48.0, 56.0)
RESPAWN_BEARINGS = 16
RESPAWN_LEVEL = 3
RESPAWN_CLEAR = 24.0


def bot_respawn_spot(solid: SolidFn | None, height: Callable[[int, int], int] | None,
                     water: Callable[[int, int], bool] | None,
                     survivors: Sequence[Vector3], pick: float) -> Vector3 | None:
    """A hidden street-level spot near the nearest survivor, or None.

    ``pick`` in [0, 1) chooses among the spots found (the caller's random).
    Street level is the median column top of the ring's candidates: a roof
    or a pit is not a spawn. Every living survivor's eye must be unable to
    see a body standing there, and no survivor may be within RESPAWN_CLEAR.
    """

    if solid is None or height is None or not survivors:
        return None
    anchor = min(survivors, key=lambda s: (float(s[2]), s[0]))  # the highest one leads
    eyes = [(float(s[0]), float(s[1]), float(s[2]) - 0.6) for s in survivors]
    phase = 2.0 * math.pi * (pick % 1.0)
    columns: list[tuple[int, int, int]] = []
    for radius in RESPAWN_RADII:
        for slot in range(RESPAWN_BEARINGS):
            angle = phase + 2.0 * math.pi * slot / RESPAWN_BEARINGS
            x = int(math.floor(float(anchor[0]) + radius * math.cos(angle)))
            y = int(math.floor(float(anchor[1]) + radius * math.sin(angle)))
            if not (2 <= x < 510 and 2 <= y < 510):
                continue
            if water is not None and water(x, y):
                continue
            try:
                top = int(height(x, y))
            except Exception:
                continue
            columns.append((x, y, top))
    if len(columns) < 4:
        return None
    tops = sorted(c[2] for c in columns)
    street = tops[len(tops) // 2]
    spots: list[Vector3] = []
    for x, y, top in columns:
        if abs(top - street) > RESPAWN_LEVEL:
            continue
        spot = standable_near(solid, (x + 0.5, y + 0.5, float(top) - PLAYER_SUPPORT_OFFSET), top, span=1)
        if spot is None:
            continue
        if any(_xy_distance(spot, s) < RESPAWN_CLEAR for s in survivors):
            continue
        body = (spot[0], spot[1], spot[2] - 0.9)
        head = (spot[0], spot[1], spot[2] - 2.0)
        if any(visible(solid, eye, body) or visible(solid, eye, head) for eye in eyes):
            continue
        spots.append(spot)
    if not spots:
        return None
    return spots[int(pick * len(spots)) % len(spots)]


def pile_count(target: Vector3, hunters: Iterable[Vector3]) -> int:
    """Hunters heaped under an elevated target (the anti-pattern we remove)."""

    return sum(
        1 for h in hunters
        if _xy_distance(h, target) <= PILE_RADIUS
        and float(h[2]) - float(target[2]) >= PILE_DEPTH
    )


def dig_stand_spot(solid: SolidFn | None, site: Cell, floor_z: int,
                   away_from: Vector3 | None = None, *,
                   reserved: Sequence[Vector3] = ()) -> Vector3 | None:
    """Where a zombie stands to claw ``site``: an open floor beside it.

    Prefers the side facing away from ``away_from`` (the survivor), so the
    digger works from outside the structure's footprint rather than under
    the part that is about to fall.
    """

    x, y, z = site
    if solid is not None:
        best = None
        # Beside the site first: from two blocks off, a body that stops a
        # stride short of its stand has the voxel out of claw reach.
        for radius in (1, 2):
            for dx in range(-radius, radius + 1):
                for dy in range(-radius, radius + 1):
                    if max(abs(dx), abs(dy)) != radius:
                        continue
                    cx, cy = x + dx, y + dy
                    for floor in range(max(z - 1, 0), min(z + 5, 240)):
                        if not solid(cx, cy, floor):
                            continue
                        if (floor - z <= 3 and not solid(cx, cy, floor - 1)
                                and not solid(cx, cy, floor - 2)
                                and not solid(cx, cy, floor - 3)):
                            stand = (cx+.5,cy+.5,float(floor)-PLAYER_SUPPORT_OFFSET)
                            if radius == 2 and math.dist((stand[0],stand[1],stand[2]-.5),
                                                       (x+.5,y+.5,z+.5)) > 3.15:
                                continue
                            if any(math.dist(stand, previous) < 1.25 for previous in reserved):
                                continue
                            outward = 0.0
                            if away_from is not None:
                                outward = -round(math.hypot(
                                    cx + 0.5 - float(away_from[0]),
                                    cy + 0.5 - float(away_from[1])), 1)
                            score = (radius, abs(floor - floor_z), outward,
                                     abs(dx) + abs(dy))
                            if best is None or score < best[0]:
                                best = (score, (cx + 0.5, cy + 0.5,
                                                float(floor) - PLAYER_SUPPORT_OFFSET))
                        break
            if best is not None:
                return best[1]
        return None
    return (x + 0.5, y + 0.5, float(floor_z) - PLAYER_SUPPORT_OFFSET)


def air_stand(solid: SolidFn | None, site: Cell, away_from: Vector3 | None,
              reserved: Sequence[Vector3] = ()) -> Vector3 | None:
    """Where a climber's stack top should be to claw a site nothing stands near.

    A body whose floor is one level below the site, in an open column one
    or two blocks beside it (away from the survivor when possible), has the
    site within claw reach. The column must be open for the body; what
    holds the body up is the climber's own stack.
    """

    if solid is None:
        return None
    x, y, z = site
    floor = z + 1
    best = None
    for radius in (1, 2):
        for dx in range(-radius, radius + 1):
            for dy in range(-radius, radius + 1):
                if max(abs(dx), abs(dy)) != radius or (dx and dy and radius == 2):
                    continue
                cx, cy = x + dx, y + dy
                if not (0 <= cx < 512 and 0 <= cy < 512 and 3 <= floor < 239):
                    continue
                if any(solid(cx, cy, floor - k) for k in (0, 1, 2)):
                    continue  # the body would be inside something
                stand = (cx + 0.5, cy + 0.5, float(floor) - PLAYER_SUPPORT_OFFSET)
                if any(math.dist(stand, previous) < 1.25 for previous in reserved):
                    continue
                outward = 0.0
                if away_from is not None:
                    outward = -round(math.hypot(cx + 0.5 - float(away_from[0]),
                                                cy + 0.5 - float(away_from[1])), 1)
                score = (radius, outward, abs(dx) + abs(dy))
                if best is None or score < best[0]:
                    best = (score, stand)
        if best is not None:
            return best[1]
    return None


def tunnel_cells(solid: SolidFn | None, start: Vector3, target: Vector3,
                 *, length: float = 4.0) -> tuple[Cell, ...]:
    """Solid voxels on the body-height line from ``start`` toward ``target``."""

    if solid is None:
        return ()
    sx, sy, sz = (float(v) for v in start)
    tx, ty = float(target[0]), float(target[1])
    dx, dy = tx - sx, ty - sy
    span = math.hypot(dx, dy)
    if span < 1e-6:
        return ()
    ux, uy = dx / span, dy / span
    floor = int(round(sz + PLAYER_SUPPORT_OFFSET))
    # Rising target: aim the tunnel one block up per two forward (a ramp).
    rise = 1 if float(target[2]) < sz - 1.5 else 0
    cells: list[Cell] = []
    step = 0.5
    t = 0.75
    while t <= length:
        cx = int(math.floor(sx + ux * t))
        cy = int(math.floor(sy + uy * t))
        lift = int(t // 2.0) * rise
        for level in (floor - 1 - lift, floor - 2 - lift):
            cell = (cx, cy, level)
            if cell not in cells and 0 <= level < 239 and solid(*cell):
                cells.append(cell)
        t += step
    return tuple(cells[:8])


@dataclass(frozen=True, slots=True)
class WallLine:
    """What a straight run at the target would have to claw through."""

    walls: int       # separate obstacles on the line
    columns: int     # solid columns in total (their thickness)
    first: float     # blocks from the start to the first of them


def wall_line(solid: SolidFn | None, start: Vector3, target: Vector3,
              *, limit: float = TUNNEL_RANGE + 4.0) -> WallLine | None:
    """Walk the straight line toward ``target`` and count what blocks a body.

    Follows the ground like a walker (one block up, a few down), so a
    terrace is not a wall; a column whose head-height cell is solid is. Only
    for a target on roughly the hunter's level: None when it is more than
    two blocks higher or lower (digging does not climb through air), out of
    ``limit``, or when the line leaves the ground altogether.
    """

    if solid is None:
        return None
    sx, sy = float(start[0]), float(start[1])
    dx, dy = float(target[0]) - sx, float(target[1]) - sy
    span = math.hypot(dx, dy)
    floor = _floor_of(start)
    if span < 1.0 or span > limit or abs(_floor_of(target) - floor) > 2:
        return None
    ux, uy = dx / span, dy / span
    walls = columns = 0
    first = -1.0
    in_wall = False
    seen = (int(math.floor(sx)), int(math.floor(sy)))
    t = 0.5
    while t < span - 0.5:
        column = (int(math.floor(sx + ux * t)), int(math.floor(sy + uy * t)))
        t += 0.5
        if column == seen:
            continue
        seen = column
        x, y = column
        if not (0 <= x < 512 and 0 <= y < 512):
            return None
        if solid(x, y, floor - 2) or (solid(x, y, floor - 1) and solid(x, y, floor - 3)):
            # Head height is blocked (or a step up under a low ceiling).
            columns += 1
            if not in_wall:
                walls += 1
                in_wall = True
                if first < 0.0:
                    first = t - 0.5
            continue
        in_wall = False
        if solid(x, y, floor - 1):
            floor -= 1            # a step up
        elif not solid(x, y, floor):
            for drop in range(1, 5):
                if solid(x, y, floor + drop):
                    floor += drop
                    break
            else:
                return None       # a pit or a ledge: not a walking line
    return WallLine(walls, columns, first)


class HordeCoordinator:
    """Stateful but bounded assignment of infected bots to survivors."""

    def __init__(self, *, seed: int | None = None) -> None:
        self.seed = seed
        self._targets: dict[int, int] = {}
        self._progress: dict[int, _Progress] = {}
        # Per survivor: besieged (True) or pursued; per zombie: its last role.
        self._besieged: dict[int, bool] = {}
        self._roles: dict[int, tuple[int, str]] = {}
        # Per zombie: (decided at, target, its column, the breach decision).
        self._breaches: dict[int, tuple] = {}
        # Per zombie: (searched at, the approach used, its column, the via
        # point, the flood's nearest floor when it stands outside the flood).
        self._vias: dict[int, tuple] = {}
        # Per survivor: the highest stuck level among the hunters sent at him.
        self._stuck: dict[int, int] = {}
        self._ascent_targets: set[int] = set()
        # Per survivor: the work stands round his footing (cached a moment).
        self._stands: dict[int, tuple[float, tuple, list[WorkStand]]] = {}
        # Per zombie: climb destinations that got it nowhere, and until when.
        self._climb_failed: dict[int, dict[tuple[int, int, int], float]] = {}
        # Per survivor: the staging sides round his footing (cached a moment),
        # and per zombie the side it holds.
        self._stagings: dict[int, tuple[float, int, list[tuple[Vector3, bool]]]] = {}
        self._climb_slot: dict[int, tuple[int, int]] = {}
        self._height: Callable[[int, int], int] | None = None
        self._water: Callable[[int, int], bool] | None = None
        # Approach under fire: per survivor the rally spots round him (when,
        # where he stood, the spots) and until when his wave runs; per
        # zombie its rally slot and since when it stands on it.
        self._rallies: dict[int, tuple[float, Vector3, list[tuple[Vector3, bool]]]] = {}
        self._wave_until: dict[int, float] = {}
        self._rally_slot: dict[int, tuple[int, int]] = {}
        self._rallied_since: dict[int, float] = {}
        self.metrics = HordeMetrics()

    @staticmethod
    def _beat(player_id: int) -> float:
        """Each bot's own fixed phase in [0, 1): paces never fall into step."""

        value = (int(player_id) * 2654435761 + 97) & 0xFFFFFFFF
        value = ((value ^ (value >> 15)) * 0x2C1B3C6D) & 0xFFFFFFFF
        return ((value ^ (value >> 12)) & 0xFFFF) / 65536.0

    def preference(self, player_id: int, target_id: int = 0) -> float:
        """Stable, bounded variation; never a new random choice each tick."""
        if self.seed is None:
            return 0.0
        value = (int(self.seed) * 0x9E3779B1 ^ int(player_id) * 7919
                 ^ int(target_id) * 104729) & 0xFFFFFFFF
        value = ((value ^ (value >> 16)) * 0x7FEB352D) & 0xFFFFFFFF
        value = ((value ^ (value >> 15)) * 0x846CA68B) & 0xFFFFFFFF
        return (value ^ (value >> 16)) / 0xFFFFFFFF

    def reset(self) -> None:
        self._targets.clear()
        self._progress.clear()
        self._besieged.clear()
        self._roles.clear()
        self._breaches.clear()
        self._vias.clear()
        self._stuck.clear()
        self._ascent_targets.clear()
        self._stands.clear()
        self._climb_failed.clear()
        self._stagings.clear()
        self._climb_slot.clear()
        self._rallies.clear()
        self._wave_until.clear()
        self._rally_slot.clear()
        self._rallied_since.clear()
        self.metrics = HordeMetrics()

    def struggling(self, target_id: int) -> bool:
        """Is some hunter assigned to this survivor stuck (last snapshot)?"""

        return self._stuck.get(int(target_id), 0) > 0

    def _stuck_level(self, zombie_id: int) -> int:
        record = self._progress.get(zombie_id)
        return record.level if record is not None else 0

    # -- target assignment -------------------------------------------------

    def assign_targets(
        self,
        zombies: Sequence[HordeMember],
        survivors: Sequence[SurvivorTarget],
    ) -> dict[int, int]:
        """Spread hunters over survivors: distance plus a load penalty.

        Closest hunters claim first; a hunter keeps its previous target
        unless another is clearly cheaper (hysteresis), so assignments do not
        flap every snapshot. Human infected count toward a target's load
        (they chase the nearest survivor) but receive no orders.
        """

        if not survivors:
            self._targets.clear()
            return {}
        alive = {s.player_id: s for s in survivors}
        load: dict[int, int] = {s.player_id: 0 for s in survivors}
        for member in zombies:
            if not member.is_bot:
                nearest = min(survivors, key=lambda s: (
                    math.dist(member.position, s.position), s.player_id))
                load[nearest.player_id] += 1
        bots = [m for m in zombies if m.is_bot]
        cap = max(1, math.ceil(len(zombies) / len(survivors))) + 1

        def nearest_distance(member: HordeMember) -> float:
            return min(math.dist(member.position, s.position) for s in survivors)

        result: dict[int, int] = {}
        for member in sorted(bots, key=lambda m: (nearest_distance(m), m.player_id)):
            def cost(survivor: SurvivorTarget) -> float:
                distance = math.dist(member.position, survivor.position)
                return (distance * (1.0 + 0.35 * load[survivor.player_id])
                        + 4.0 * self.preference(member.player_id, survivor.player_id)) + (
                    1000.0 if load[survivor.player_id] >= cap else 0.0)

            best = min(survivors, key=lambda s: (cost(s), s.player_id))
            previous = self._targets.get(member.player_id)
            if (previous in alive and previous != best.player_id
                    and load[previous] < cap
                    and cost(alive[previous]) <= cost(best) * 1.3 + 8.0):
                best = alive[previous]
            result[member.player_id] = best.player_id
            load[best.player_id] += 1
        self._targets = dict(result)
        return result

    # -- watchdog ----------------------------------------------------------

    def _observe_progress(self, zombie_id: int, position: Vector3, goal: Vector3,
                          goal_key: tuple, now: float, working: bool = False,
                          grounded: bool = True) -> int:
        distance = math.dist(position, goal)
        record = self._progress.get(zombie_id)
        if record is None:
            record = _Progress(goal_key, distance, now, 0, now, tuple(position), now)
            self._progress[zombie_id] = record
            return record.level
        if not grounded:
            # In the air: neither progress nor a stall is read from here.
            return record.level
        if record.goal_key != goal_key:
            # Moving prey, rotating roles and decoys must not erase time spent
            # motionless below a platform. Rebase distance without rewarding a
            # target for moving closer to the hunter.
            record.goal_key = goal_key
            record.best_distance = distance
        if (distance + PROGRESS_STEP <= record.best_distance
                and math.dist(position, record.anchor) >= .75):
            record.best_distance = distance
            record.progress_at = now
            record.closed_at = now
            record.anchor = tuple(position)
            if record.level > 0 and now - record.level_since >= RECOVER_SECONDS:
                record.level -= 1
                record.level_since = now
        elif working and now - record.closed_at <= WORK_CREDIT_SECONDS:
            record.progress_at = now
        elif (distance > 3.0 or goal_key[1] in {"dig_root", "climb", "tunnel"}) and now - record.progress_at >= STALL_SECONDS:
            if math.dist(position, record.anchor) > 4.0:
                # Still covering ground (a fleeing target): a chase, not a
                # stall. Restart the window from here.
                record.progress_at = now
                record.anchor = tuple(position)
                record.best_distance = distance
                return record.level
            if record.level < MAX_STUCK_LEVEL:
                record.level += 1
                self.metrics.stuck_events += 1
            record.level_since = now
            record.progress_at = now
            record.closed_at = now
            record.anchor = tuple(position)
            record.best_distance = distance
        if distance <= 3.0 and goal_key[1] not in {"dig_root", "climb", "tunnel"}:
            record.progress_at = now
            record.closed_at = now
            record.best_distance = min(record.best_distance, distance)
        return record.level

    def _stalled_seconds(self, member: HordeMember, target: SurvivorTarget,
                         now: float) -> float:
        """How long this hunter has gone without closing on this target."""

        record = self._progress.get(member.player_id)
        if record is None or not record.goal_key or record.goal_key[0] != target.player_id:
            return 0.0
        if math.dist(member.position, record.anchor) > 4.0:
            return 0.0  # covering ground: a detour or a chase, not a stall
        idle = now - record.progress_at
        return idle if idle >= 1.0 else 0.0

    # -- estimates ---------------------------------------------------------

    def _walk_seconds(self, member: HordeMember, target: SurvivorTarget,
                      siege: SiegeInfo | None, now: float) -> float:
        return self._walk_estimate(member, target, siege, now)[0]

    def _walk_estimate(self, member: HordeMember, target: SurvivorTarget,
                       siege: SiegeInfo | None, now: float) -> tuple[float, bool]:
        """Seconds for this hunter to reach the target on foot (inf: never).

        Exact inside the survivor's approach flood; outside it only a lower
        bound from the flood's frontier (the second value is then True: the
        real walk is at least this long); a straight-line guess with no
        analysis. Time already spent getting nowhere is added on top, so a
        way that is not working stops looking cheap.
        """

        flat = _xy_distance(member.position, target.position)
        climb = max(0.0, float(member.position[2]) - float(target.position[2]))
        approach = siege.approach if siege is not None else None
        bound = False
        if approach is None:
            if siege is not None and siege.isolated:
                return math.inf, False
            blocks = flat + 2.0 * climb
        else:
            moves = approach.moves_from(member.position)
            if moves is not None:
                blocks = max(moves * MOVE_BLOCKS, flat)
            elif approach.closed:
                return math.inf, False
            else:
                bound = True
                blocks = max(approach.frontier * MOVE_BLOCKS + max(0.0, flat - approach.radius),
                             flat + 2.0 * climb)
        return (blocks / RUN_SPEED
                + STALL_WEIGHT * self._stalled_seconds(member, target, now)), bound

    @staticmethod
    def _collapse_seconds(plan: CollapsePlan, group: Sequence[HordeMember],
                          solid: SolidFn | None) -> float:
        """Seconds for this group to dig what is left of the cut."""

        cut = [c for c in plan.cut if solid is None or solid(*c)]
        if not cut:
            return 0.0
        sites = [s for s in plan.sites if any(
            max(abs(c[0] - s[0]), abs(c[1] - s[1]), abs(c[2] - s[2])) <= 1 for c in cut)] or [cut[0]]
        diggers = [m for m in group if m.can_dig]
        if not diggers:
            return math.inf
        crew = min(len(diggers), len(sites))
        # The crew is complete when its farthest member has walked in.
        walks = sorted(
            min(math.dist(m.position, (s[0] + .5, s[1] + .5, s[2] + .5)) for s in sites)
            for m in diggers)
        hardness = max(1.0, plan.cost / max(1, len(plan.cut)))
        return (walks[crew - 1] / RUN_SPEED
                + math.ceil(len(sites) / crew) * SITE_SECONDS * hardness)

    @staticmethod
    def _ascent_seconds(target: SurvivorTarget, group: Sequence[HordeMember]) -> float:
        builders=[member for member in group if member.can_build and member.build_levels>0]
        if not builders:
            return math.inf
        rise=max(0.,min(member.position[2] for member in builders)-target.position[2])
        if sum(member.build_levels for member in builders)<rise:
            return math.inf
        return (min(_xy_distance(member.position,target.position) for member in builders)/RUN_SPEED
                + rise*min(member.build_seconds_per_level for member in builders)+2.)

    # -- orders ------------------------------------------------------------

    def plan(
        self,
        zombies: Sequence[HordeMember],
        survivors: Sequence[SurvivorTarget],
        sieges: Mapping[int, SiegeInfo],
        now: float,
        *,
        solid: SolidFn | None = None,
        height: Callable[[int, int], int] | None = None,
        water: Callable[[int, int], bool] | None = None,
    ) -> dict[int, HordeOrder]:
        """One order per infected bot. ``height``: the map's column tops."""

        self._height = height
        self._water = water
        known = {m.player_id for m in zombies if m.is_bot}
        for zombie_id in tuple(self._progress):
            if zombie_id not in known:
                self._progress.pop(zombie_id, None)
                self._roles.pop(zombie_id, None)
                self._breaches.pop(zombie_id, None)
                self._vias.pop(zombie_id, None)
                self._climb_failed.pop(zombie_id, None)
                self._climb_slot.pop(zombie_id, None)
                self._rally_slot.pop(zombie_id, None)
                self._rallied_since.pop(zombie_id, None)
        targets = self.assign_targets(zombies, survivors)
        by_id = {s.player_id: s for s in survivors}
        self._ascent_targets.intersection_update(by_id)
        for survivor_id in tuple(self._besieged):
            if survivor_id not in by_id:
                self._besieged.pop(survivor_id, None)
        for survivor_id in tuple(self._stands):
            if survivor_id not in by_id:
                self._stands.pop(survivor_id, None)
                self._stagings.pop(survivor_id, None)
        for survivor_id in tuple(self._rallies):
            if survivor_id not in by_id:
                self._rallies.pop(survivor_id, None)
                self._wave_until.pop(survivor_id, None)
        members = {m.player_id: m for m in zombies}
        groups: dict[int, list[HordeMember]] = {}
        for zombie_id, target_id in targets.items():
            groups.setdefault(target_id, []).append(members[zombie_id])
        orders: dict[int, HordeOrder] = {}
        self.metrics.piles = {}
        self._stuck = {}
        for target_id, group in sorted(groups.items()):
            target = by_id[target_id]
            group.sort(key=lambda m: (math.dist(m.position, target.position)
                                     + 6.0 * self.preference(m.player_id, target_id), m.player_id))
            siege = sieges.get(target_id)
            if (siege is not None and siege.position is not None
                    and math.dist(siege.position, target.position) > STALE_ANALYSIS_MOVE
                    and (siege.approach is None
                         or siege.approach.cell_at(target.position) is None)):
                siege = None  # he fell or jumped: that footing is history
            if (siege is None and len(group) >= 3
                    and any(_xy_distance(member.position, target.position) <= ANALYSIS_RANGE_HINT
                            and member.position[2] - target.position[2] >= 8 for member in group)
                    and not any(self._at_prey_level(member, target) for member in group)):
                # The bounded structural flood may still be running. Spread
                # the waiting horde immediately instead of rushing everybody
                # into the same column until the expensive answer arrives.
                floors=sorted(_floor_of(member.position) for member in group)
                siege=SiegeInfo(True,floors[len(floors)//2],position=target.position)
            approach = siege.approach if siege is not None else None
            inside = ([m for m in group if approach.moves_from(m.position) is not None]
                      if approach is not None and approach.closed else [])
            if inside and len(inside) < len(group):
                # A closed footing with some of the horde already on it (up
                # the stairs it cut, on the roof he fell to): those hunt him
                # on foot; everyone still below besieges. One group-level
                # choice would have the climbers wait because a teammate
                # up there "has a walk".
                outside = [m for m in group if m not in inside]
                self._besieged[target_id] = True
                group_orders = (self._pursuit_orders(target, inside, siege, solid, now)
                                + self._siege_orders(target, outside, siege, solid, now))
            elif self._besiege(target, group, siege, solid, now):
                group_orders = self._siege_orders(target, group, siege, solid, now)
            else:
                group_orders = self._pursuit_orders(target, group, siege, solid, now)
            group_orders = self._advance_orders(target, group, group_orders, siege, solid, now)
            pile = pile_count(target.position, (m.position for m in group))
            self.metrics.piles[target_id] = pile
            self.metrics.max_pile = max(self.metrics.max_pile, pile)
            for order in group_orders:
                member = members[order.zombie_id]
                # The watchdog measures closing on the prey for every way of
                # hunting it, so a switch from running to clawing does not
                # restart its clock; standing orders are measured by spot.
                if order.role in ("hunt", "tunnel"):
                    key = (order.target_id, "hunt")
                    goal = tuple(float(v) for v in target.position)
                else:
                    key = (order.target_id, order.role, order.aim,
                           tuple(int(v) for v in order.goal[:2]))
                    goal = order.goal
                if (order.role == "advance"
                        and _xy_distance(member.position, order.goal) <= RALLY_REACH):
                    # Standing on its rally spot is the order: no stall.
                    level = 0
                else:
                    level = self._observe_progress(
                        member.player_id, member.position, goal, key, now, member.working,
                        member.grounded)
                self._stuck[target_id] = max(self._stuck.get(target_id, 0), level)
                order = self._escalate(order, member, target, level, solid, now)
                if order.role == "tunnel":
                    self.metrics.tunnel_orders += 1
                self._roles[order.zombie_id] = (order.target_id, order.role)
                orders[order.zombie_id] = order
        return orders

    # --- approach under fire ------------------------------------------------

    def wave_running(self, target_id: int, now: float) -> bool:
        """Is a released wave rushing this survivor right now?"""

        return float(now) < self._wave_until.get(int(target_id), 0.0)

    def _advance_orders(self, target: SurvivorTarget, group: Sequence[HordeMember],
                        orders: list[HordeOrder], siege: SiegeInfo | None,
                        solid: SolidFn | None, now: float) -> list[HordeOrder]:
        """Hold the far zombies at hidden rally spots and release them as a wave.

        Only against a dangerous survivor (``threat_until``), only for
        zombies still beyond RALLY_NEAR of him with an order that walks
        them in (everything but a tunnel). Zombies already close keep their
        orders: a rush that has started is not called back. See the
        constants above for the wave rule.
        """

        target_id = target.player_id
        if not target.dangerous(now) or solid is None:
            self._wave_until.pop(target_id, None)
            for order in orders:
                self._rally_slot.pop(order.zombie_id, None)
                self._rallied_since.pop(order.zombie_id, None)
            return orders
        members = {m.player_id: m for m in group}
        wave_running = now < self._wave_until.get(target_id, 0.0)
        floor_z = siege.floor_z if siege is not None else _floor_of(target.position)
        # Beyond this the walk in is a lone trickle: rally instead. Only
        # zombies on the horde floor: one already up on a roof (his, or
        # the next one) is not sent back down to a street spot.
        hold_beyond = WAVE_JOIN if wave_running else RALLY_NEAR
        far = [o for o in orders
               if o.role != "tunnel"
               and _xy_distance(members[o.zombie_id].position, target.position) > hold_beyond
               and abs(_floor_of(members[o.zombie_id].position) - floor_z) <= LEVEL_TOLERANCE
               and not self._at_prey_level(members[o.zombie_id], target)
               and float(target.position[2]) - float(members[o.zombie_id].position[2]) < ABOVE_PREY]
        if not far:
            return orders
        spots = self._rally_spots(target, floor_z, solid, now)
        if not spots:
            return orders
        taken: dict[int, int] = {}
        advancing: list[tuple[HordeOrder, Vector3]] = []
        for order in sorted(far, key=lambda o: _xy_distance(members[o.zombie_id].position,
                                                            target.position)):
            member = members[order.zombie_id]
            spot = self._rally_spot_for(member, target, spots, taken)
            if spot is None:
                continue
            advancing.append((order, spot))
        if not advancing:
            return orders
        rallied = 0
        oldest = None
        for order, spot in advancing:
            member = members[order.zombie_id]
            if _xy_distance(member.position, spot) <= RALLY_REACH:
                rallied += 1
                since = self._rallied_since.setdefault(order.zombie_id, now)
                oldest = since if oldest is None else min(oldest, since)
            else:
                self._rallied_since.pop(order.zombie_id, None)
        needed = min(len(advancing), max(WAVE_MIN, math.ceil(WAVE_SHARE * len(advancing))))
        if wave_running:
            # Late for this wave: hold the far ones for the next.
            replaced = {order.zombie_id: HordeOrder(order.zombie_id, target_id, "advance", spot)
                        for order, spot in advancing}
            return [replaced.get(o.zombie_id, o) for o in orders]
        if rallied >= needed or (oldest is not None and now - oldest >= WAVE_WAIT):
            # The wave goes: everyone keeps the order that walks them in.
            self._wave_until[target_id] = now + WAVE_SECONDS
            self.metrics.waves += 1
            for order, _spot in advancing:
                self._rallied_since.pop(order.zombie_id, None)
            return orders
        replaced = {order.zombie_id: HordeOrder(order.zombie_id, target_id, "advance", spot)
                    for order, spot in advancing}
        return [replaced.get(o.zombie_id, o) for o in orders]

    def _rally_spots(self, target: SurvivorTarget, floor_z: int, solid: SolidFn,
                     now: float) -> list[tuple[Vector3, bool]]:
        """Rally spots round him, (spot, hidden=True), cached a moment.

        Street-level spots (within RALLY_LEVEL of the horde's floor: no
        rooftops) on the rings that his eye cannot see; none when nothing
        round him hides a body.
        """

        cached = self._rallies.get(target.player_id)
        if (cached is not None and now - cached[0] < RALLY_TTL
                and _xy_distance(cached[1], target.position) <= 1.5):
            return cached[2]
        eye = (float(target.position[0]), float(target.position[1]), float(target.position[2]) - 0.6)

        def ring(radii: Sequence[float], hidden_only: bool) -> list[tuple[Vector3, bool]]:
            found: list[tuple[Vector3, bool]] = []
            for radius in radii:
                for slot in range(RALLY_BEARINGS):
                    raw = _ring_point(target.position, slot, RALLY_BEARINGS, radius, floor_z,
                                      phase=0.37 * target.player_id)
                    x, y = int(math.floor(raw[0])), int(math.floor(raw[1]))
                    if not (1 <= x < 511 and 1 <= y < 511):
                        continue
                    if self._water is not None and self._water(x, y):
                        continue
                    column = floor_z
                    if self._height is not None:
                        try:
                            column = int(self._height(x, y))
                        except Exception:
                            column = floor_z
                    if abs(column - floor_z) > RALLY_LEVEL:
                        continue
                    spot = standable_near(solid, (raw[0], raw[1], raw[2]), column, span=2)
                    if spot is None:
                        continue
                    body = (spot[0], spot[1], spot[2] - 0.9)
                    head = (spot[0], spot[1], spot[2] - 2.0)
                    hidden = not (visible(solid, eye, body) or visible(solid, eye, head))
                    if hidden_only and not hidden:
                        continue
                    found.append((spot, hidden))
            return found

        spots = ring(RALLY_RADII, True)
        self._rallies[target.player_id] = (now, tuple(float(v) for v in target.position), spots)
        return spots

    def _rally_spot_for(self, member: HordeMember, target: SurvivorTarget,
                        spots: Sequence[tuple[Vector3, bool]],
                        taken: dict[int, int]) -> Vector3 | None:
        """The spot on this zombie's own side of him, hidden first (kept while it holds)."""

        if not spots:
            return None
        bearing = math.atan2(member.position[1] - target.position[1],
                             member.position[0] - target.position[0])

        def turn(spot: Vector3) -> float:
            angle = math.atan2(spot[1] - target.position[1], spot[0] - target.position[0])
            return abs((angle - bearing + math.pi) % (2 * math.pi) - math.pi)

        held = self._rally_slot.get(member.player_id)
        if held is not None and held[0] == target.player_id and held[1] < len(spots):
            spot = spots[held[1]][0]
            if turn(spot) <= math.pi / 2 and taken.get(held[1], 0) < RALLY_PER_SPOT:
                taken[held[1]] = taken.get(held[1], 0) + 1
                return spot
        order = sorted(range(len(spots)),
                       key=lambda k: (not spots[k][1],
                                      round(turn(spots[k][0]) / (math.pi / 8)),
                                      math.dist(member.position, spots[k][0])))
        for k in order:
            if taken.get(k, 0) >= RALLY_PER_SPOT:
                continue
            if turn(spots[k][0]) > 0.75 * math.pi:
                continue  # the far side of him: the walk round is the open street
            taken[k] = taken.get(k, 0) + 1
            self._rally_slot[member.player_id] = (target.player_id, k)
            return spots[k][0]
        return None

    def _chosen_plan(self, siege: SiegeInfo | None, group: Sequence[HordeMember],
                     solid: SolidFn | None) -> CollapsePlan | None:
        """The exact cut or the foundation cut, whichever the crew digs sooner.

        An exact cut hanging in mid-air (a walkway voxel fifty blocks from
        the tree it was built from) is priced by its unreachable voxels;
        the trunk's nine voxels at the horde's feet beat it by a mile.
        """

        if siege is None:
            return None
        exact = siege.plan if siege.plan is not None and (
            solid is None or any(solid(*c) for c in siege.plan.cut)) else None
        foundation = siege.foundation if siege.foundation is not None and (
            solid is None or any(solid(*c) for c in siege.foundation.cut)) else None
        if foundation is None:
            return exact
        if exact is None:
            return foundation
        # The foundation's estimate is the optimistic one (its sites lie
        # round a whole base, with walks between them the site model does
        # not price), so it has to win clearly before a reachable exact
        # cut is given up.
        exact_seconds = self._collapse_seconds(exact, group, solid)
        found_seconds = self._collapse_seconds(foundation, group, solid)
        if found_seconds * FOUNDATION_HANDICAP + CHOICE_MARGIN * 2 < exact_seconds:
            return foundation
        return exact

    def _besiege(self, target: SurvivorTarget, group: Sequence[HordeMember],
                 siege: SiegeInfo | None, solid: SolidFn | None, now: float) -> bool:
        """Bring the footing down (or climb it) instead of walking to him?

        Always when nothing walks in. Otherwise only for a survivor well
        above the horde whose collapse cut is known and quicker to dig than
        the walk up is to make, with a margin and a memory so the horde does
        not change its mind every snapshot.
        """

        target_id = target.player_id
        self._ascent_targets.discard(target_id)
        if siege is None:
            self._besieged.pop(target_id, None)
            return False
        approach = siege.approach
        if approach is not None:
            isolated = approach.closed and not any(
                approach.moves_from(m.position) is not None for m in group)
            if isolated and siege.floor_z - approach.support_z < ELEVATED_RISE:
                # Walled in on the horde's own level (a sealed room, a pit
                # bunker): that is a wall to claw through, not a tower.
                self._besieged.pop(target_id, None)
                return False
        else:
            isolated = siege.isolated
        chosen = self._chosen_plan(siege, group, solid)
        if chosen is None and (siege.plan is not None or siege.foundation is not None):
            # Every voxel of the cut is gone: the footing is coming down this
            # instant. Nobody climbs or stacks a prefab under a falling
            # structure (he lands on the stack and is perched again): the
            # horde runs in for the landing.
            self._ascent_targets.discard(target_id)
            self._besieged.pop(target_id, None)
            return False
        if isolated:
            collapse = (self._collapse_seconds(chosen, group, solid) if chosen else math.inf)
            if self._ascent_seconds(target, group) + CHOICE_MARGIN < collapse:
                self._ascent_targets.add(target_id)
            self._besieged[target_id] = True
            return True
        # His feet now, or the flood's support if that was higher: a flood
        # taken as he fell read the ground three blocks under his feet, and
        # he may have landed on a stub well above it since.
        support_z = _floor_of(target.position)
        if approach is not None:
            support_z = min(support_z, approach.support_z)
        if siege.floor_z - support_z < ELEVATED_RISE:
            self._besieged.pop(target_id, None)
            return False
        walk = min((self._walk_seconds(m, target, siege, now) for m in group),
                   default=math.inf)
        dig = self._collapse_seconds(chosen, group, solid) if chosen else math.inf
        ascent=self._ascent_seconds(target,group)
        if ascent+CHOICE_MARGIN<dig:
            self._ascent_targets.add(target_id)
            dig=ascent
        if self._besieged.get(target_id):
            chosen = dig <= walk + CHOICE_MARGIN
        else:
            chosen = dig + CHOICE_MARGIN < walk
        if chosen:
            if not self._besieged.get(target_id):
                self.metrics.collapse_choices += 1
            self._besieged[target_id] = True
        else:
            self._besieged.pop(target_id, None)
        return chosen

    def _pursuit_orders(self, target: SurvivorTarget, group: Sequence[HordeMember],
                        siege: SiegeInfo | None, solid: SolidFn | None,
                        now: float) -> list[HordeOrder]:
        """Everyone runs at the prey; whoever is quicker through a wall claws."""

        goal = tuple(float(v) for v in target.position)
        orders = []
        for member in group:
            breach = self._breach(member, target, siege, solid, now)
            if breach is not None:
                orders.append(HordeOrder(member.player_id, target.player_id, "tunnel",
                                         breach[0], cells=breach[1],
                                         aim=breach[1][0] if breach[1] else None))
                continue
            via = self._via(member, target, siege, now)
            parked = None if via is not None else self._parked_under(member, target, siege, solid)
            if parked is not None:
                # Standing under him is not reaching him: wait on the ring,
                # eyes up, until the analysis says how to get up there.
                orders.append(HordeOrder(member.player_id, target.player_id, "flank", parked))
                continue
            orders.append(HordeOrder(member.player_id, target.player_id, "hunt", via or goal))
        return orders

    def _can_walk_to(self, member: HordeMember, stand: WorkStand) -> bool:
        """Is this stand on the member's own level, or on open terrain it can walk?

        Levels within LEVEL_TOLERANCE always. A stand whose floor is the
        map's own ground (its column top, give or take a step) is walked to
        from any ground within a terrace or two (a tree in a hollow forty
        blocks from the survivor's end); a stand on a roof is not.
        """

        gap = abs(_floor_of(member.position) - stand.floor_z)
        if self._roles.get(member.player_id, (None, None))[1] == "climb":
            # Part of the way up its ladder (two below the roof, on the palm)
            # a climber is not a digger yet: a dig order walks, and from a
            # rung there is nothing to walk. It digs once it stands level.
            return gap <= 1
        if gap <= LEVEL_TOLERANCE:
            return True
        height = self._height
        if height is None or gap > 8:
            return False
        sx, sy = int(math.floor(stand.goal[0])), int(math.floor(stand.goal[1]))
        mx, my = int(math.floor(member.position[0])), int(math.floor(member.position[1]))
        if not (0 <= sx < 512 and 0 <= sy < 512 and 0 <= mx < 512 and 0 <= my < 512):
            return False
        return (self._on_ground(sx, sy, stand.floor_z)
                and self._on_ground(mx, my, _floor_of(member.position)))

    def _on_ground(self, x: int, y: int, floor_z: int) -> bool:
        """Is a floor at this column the map's ground, not a roof or a deck?

        Its column's top, and level with the ground round it: the low
        quartile of the column tops six and ten blocks out (a roof is far
        above the street round it; the height field alone cannot tell them
        apart).
        """

        height = self._height
        if height is None or abs(height(x, y) - floor_z) > 1:
            return False
        samples = sorted(
            height(x + int(round(r * math.cos(a))), y + int(round(r * math.sin(a))))
            for r in (6, 10) for a in (k * math.pi / 4.0 for k in range(8))
            if 0 <= x + int(round(r * math.cos(a))) < 512
            and 0 <= y + int(round(r * math.sin(a))) < 512)
        if not samples:
            return False
        return abs(samples[(3 * len(samples)) // 4] - floor_z) <= 2

    @staticmethod
    def _at_prey_level(member: HordeMember, target: SurvivorTarget) -> bool:
        """Standing on the prey's own level, a short run from him."""

        return (abs(float(member.position[2]) - float(target.position[2])) < float(LEVEL_TOLERANCE)
                and _xy_distance(member.position, target.position) <= AT_LEVEL_REACH)

    @staticmethod
    def _parked_under(member: HordeMember, target: SurvivorTarget,
                      siege: SiegeInfo | None, solid: SolidFn | None) -> Vector3 | None:
        """A ring spot for a hunter at the foot of a prey it cannot walk up to."""

        if solid is None:
            return None
        rise = float(member.position[2]) - float(target.position[2])
        if rise < UNDER_RISE or _xy_distance(member.position, target.position) > UNDER_RADIUS:
            return None
        approach = siege.approach if siege is not None else None
        if approach is not None and approach.moves_from(member.position) is not None:
            return None  # the walk up starts right here
        floor_z = _floor_of(member.position)
        for attempt in range(4):
            spot = standable_near(solid, _ring_point(
                target.position, member.player_id + 3 * attempt, 8, RING_MIN + 1.5, floor_z,
                phase=math.pi / 8.0), floor_z)
            if spot is not None and _xy_distance(spot, target.position) >= PILE_RADIUS + 1.0:
                return spot
        return None

    def _via(self, member: HordeMember, target: SurvivorTarget,
             siege: SiegeInfo | None, now: float) -> Vector3 | None:
        """The next stretch of the known way in, when it is not the straight run.

        The approach flood knows the stairs round the back, the ramp, the
        door; a bot's own planner sees a dozen blocks and otherwise stands
        under a roof it could walk up to. None for a straight run, an
        unexplored hunter far from every explored floor, or no analysis.
        """

        approach = siege.approach if siege is not None else None
        if approach is None:
            return None
        here = (int(math.floor(member.position[0])), int(math.floor(member.position[1])))
        cached = self._vias.get(member.player_id)
        fresh = (cached is not None and cached[1] is approach
                 and now - cached[0] < BREACH_REFRESH)
        if fresh and cached[2] == here:
            return cached[3]
        via = None
        entry = None
        cell = approach.cell_at(member.position)
        if cell is None and not approach.closed:
            # Outside the flood: where its nearest floor is changes slowly,
            # so the search is repeated on the refresh, not at every stride.
            entry = cached[4] if fresh else approach.entry_near(
                member.position, VIA_ENTRY_REACH)
            cell = entry
        if cell is not None:
            moves = approach.steps[cell]
            flat = _xy_distance(member.position, target.position)
            rise = float(member.position[2]) - float(target.position[2])
            if moves > VIA_AHEAD and (moves * MOVE_BLOCKS > flat + VIA_DETOUR
                                      or abs(rise) >= VIA_RISE):
                x, y, z = approach.waypoint(cell, VIA_AHEAD)
                via = (x + 0.5, y + 0.5, float(z) - PLAYER_SUPPORT_OFFSET)
        self._vias[member.player_id] = (
            cached[0] if fresh else now, approach, here, via, entry)
        return via

    def _breach(self, member: HordeMember, target: SurvivorTarget,
                siege: SiegeInfo | None, solid: SolidFn | None,
                now: float) -> tuple[Vector3, tuple[Cell, ...]] | None:
        """Where to go and what to claw when straight through beats round.

        None when the hunter cannot dig, is out of range, has nothing in the
        way, or walks there sooner. The line is re-walked when the hunter or
        its prey changes column, a clawed cell goes, or after BREACH_REFRESH.
        """

        if solid is None or not member.can_dig:
            return None
        flat = _xy_distance(member.position, target.position)
        if flat > TUNNEL_RANGE:
            self._breaches.pop(member.player_id, None)
            return None
        here = (int(math.floor(member.position[0])), int(math.floor(member.position[1])),
                int(math.floor(target.position[0])), int(math.floor(target.position[1])))
        cached = self._breaches.get(member.player_id)
        if (cached is not None and cached[1] == target.player_id and cached[2] == here
                and now - cached[0] < BREACH_REFRESH
                and (cached[3] is None or all(solid(*cell) for cell in cached[3][1]))):
            return cached[3]
        decision = self._breach_now(member, target, siege, solid, now, flat)
        self._breaches[member.player_id] = (now, target.player_id, here, decision)
        return decision

    def _breach_now(self, member: HordeMember, target: SurvivorTarget,
                    siege: SiegeInfo | None, solid: SolidFn, now: float,
                    flat: float) -> tuple[Vector3, tuple[Cell, ...]] | None:
        line = wall_line(solid, member.position, target.position)
        if line is None or line.walls == 0:
            return None
        dig = (flat / RUN_SPEED + line.walls * WALL_SECONDS
               + (line.columns - line.walls) * COLUMN_SECONDS)
        walk, at_least = self._walk_estimate(member, target, siege, now)
        clawing = self._roles.get(member.player_id) == (target.player_id, "tunnel")
        # A lower bound needs no safety margin: the walk is no shorter.
        margin = 0.0 if at_least else CHOICE_MARGIN
        if clawing and member.working:
            pass  # a claw that is landing is not given up for a walk that
            # only looks short again because the claw counts as progress
        elif not (dig <= walk + CHOICE_MARGIN if clawing else dig + margin <= walk):
            return None
        if not clawing:
            self.metrics.breach_choices += 1
        cells = tunnel_cells(solid, member.position, target.position)
        position = tuple(float(v) for v in target.position)
        if line.first <= 1.75:
            return position, cells
        # The wall is still some strides off: run to its face first, on the
        # line, or the route round it carries the bot along the wall instead.
        back = line.first - 1.0
        face = (float(member.position[0])
                + (float(target.position[0]) - float(member.position[0])) / flat * back,
                float(member.position[1])
                + (float(target.position[1]) - float(member.position[1])) / flat * back,
                float(member.position[2]))
        spot = standable_near(solid, face, _floor_of(member.position))
        return (spot or position), cells

    def _work_stands(self, target: SurvivorTarget, plan: CollapsePlan | None,
                     floor_z: int, solid: SolidFn | None, now: float) -> list[WorkStand]:
        """Where the group can work on this footing, cached for a moment.

        One stand per claw site of the cut (on whatever floor a claw reaches
        the site from: the ground at a leg, the roof at a pillar's foot) and
        the open floors within claw reach of the survivor himself.
        """

        key = (tuple(round(float(v), 1) for v in target.position), id(plan), int(floor_z))
        cached = self._stands.get(target.player_id)
        if cached is not None and cached[1] == key and now - cached[0] < STANDS_TTL:
            return cached[2]
        stands: list[WorkStand] = []
        if plan is not None:
            cut = [c for c in plan.cut if solid is None or solid(*c)]
            sites = [site for site in plan.sites if any(_cheb(c, site) <= 1 for c in cut)]
            if not sites and cut:
                sites = [cut[0]]
            reserved: list[Vector3] = []
            for site in sites[:MAX_DIGGERS]:
                stand = dig_stand_spot(solid, site, floor_z, target.position, reserved=reserved)
                if stand is None:
                    # Nothing to stand on near the site (a neck in mid-air):
                    # the stand is the air beside it, one level below, on
                    # the side away from the prey; a climber stacks up to
                    # it and claws from the top of its stack.
                    stand = air_stand(solid, site, target.position, reserved)
                    if stand is None:
                        continue
                reserved.append(stand)
                cells = tuple(sorted(c for c in cut if _cheb(c, site) <= 1)) or (site,)
                stands.append(WorkStand(stand, _floor_of(stand), "cut", site, cells))
        stands.extend(reach_stands(solid, target.position))
        self._stands[target.player_id] = (now, key, stands)
        return stands

    def _staging_sides(self, target: SurvivorTarget, siege: SiegeInfo,
                       solid: SolidFn | None, count: int, now: float) -> list[tuple[Vector3, bool]]:
        """``count`` standable spots round the footing, hidden ones first.

        Evenly spaced bearings a stride outside the measured footprint, on
        the horde's floor; each is marked hidden when the straight line from
        the survivor's eye to a body standing there crosses the structure.
        """

        cached = self._stagings.get(target.player_id)
        if cached is not None and cached[1] == count and now - cached[0] < STAGE_TTL:
            return cached[2]
        approach = siege.approach
        radius = STAGE_RADIUS
        if approach is not None and approach.closed:
            radius = max(STAGE_RADIUS, approach.radius + 3.0)
        eye = (float(target.position[0]), float(target.position[1]), float(target.position[2]) - 0.6)
        phase = 0.3 * target.player_id + 2 * math.pi * self.preference(target.player_id, 3)
        sides: list[tuple[Vector3, bool]] = []
        for slot in range(count):
            spot = None
            for extra in (0.0, 2.0, 4.0):
                raw = _ring_point(target.position, slot, count, radius + extra, siege.floor_z, phase=phase)
                spot = standable_near(solid, raw, siege.floor_z, span=4)
                if spot is not None:
                    break
            if spot is None:
                continue
            body = (spot[0], spot[1], spot[2] - 0.9)
            sides.append((spot, not visible(solid, eye, body)))
        sides.sort(key=lambda side: (not side[1], side[0]))
        self._stagings[target.player_id] = (now, count, sides)
        return sides

    def _siege_orders(self, target: SurvivorTarget, group: Sequence[HordeMember],
                      siege: SiegeInfo, solid: SolidFn | None, now: float) -> list[HordeOrder]:
        """Work for everyone: dig the cut, climb to an attack position, pace.

        Diggers take the claw sites they can walk to (a stand on their own
        level). A site nobody stands level with (the foot of a pillar on a
        roof) becomes a climb destination, as does every floor within claw
        reach of the survivor; with nothing known, climbers go for the prey
        itself. A bounded share of the group waits, and it waits on the
        move: each zombie steps on to the next ring spot every few seconds,
        on its own beat.
        """

        orders: list[HordeOrder] = []
        remaining = list(group)
        # A hunter standing well above the prey (on a roof over his pillar,
        # a hillside) has nothing to climb and nothing to wait for: it finds
        # its own way down and at him like any hunt. One already on his
        # level beside him (up the stairs, on the roof he fell to) simply
        # goes for him.
        for member in list(remaining):
            if (float(target.position[2]) - float(member.position[2]) >= ABOVE_PREY
                    or self._at_prey_level(member, target)):
                remaining.remove(member)
                orders.append(HordeOrder(member.player_id, target.player_id, "hunt",
                                         tuple(float(v) for v in target.position)))
        plan = (None if target.player_id in self._ascent_targets
                else self._chosen_plan(siege, group, solid))
        stands = self._work_stands(target, plan, siege.floor_z, solid, now)
        cut_stands: list[WorkStand] = []
        for stand in stands:
            if stand.kind != "cut":
                continue
            cells = tuple(c for c in stand.cells if solid is None or solid(*c))
            if not cells:
                continue
            if stand.site is not None and stand.site not in cells and solid is not None:
                # The site itself is dug and what is left of its cube may be
                # out of claw reach from the old stand (the buried core of a
                # leg): stand beside what remains instead.
                site = min(cells, key=lambda c: (_cheb(c, stand.site), c))
                moved = dig_stand_spot(solid, site, siege.floor_z, target.position,
                                       reserved=[other.goal for other in cut_stands])
                if moved is not None:
                    stand = replace(stand, goal=moved, floor_z=_floor_of(moved), site=site)
            cut_stands.append(replace(stand, cells=cells))
        reach = [stand for stand in stands if stand.kind == "reach"]
        per_site = 2 if len(remaining) >= 2 * len(cut_stands) + 2 else 1
        # Claw sites nobody on the ground can reach (the foot of a pillar on
        # a roof): climbers keep heading up there, whether or not somebody
        # already clawing up there has the site, so the crew grows.
        elevated_work = [stand for stand in cut_stands
                         if stand.floor_z <= siege.floor_z - ELEVATED_RISE]
        diggers = 0
        for stand in cut_stands:
            assigned = 0
            for _ in range(per_site):
                at_level = [m for m in remaining if m.can_dig
                            and self._can_walk_to(m, stand)]
                if not at_level:
                    break
                member = min(at_level, key=lambda m: (
                    math.dist(m.position, stand.goal)
                    + 3.0 * self.preference(m.player_id, target.player_id)
                    - (3.0 if m.working else 0.0),
                    m.player_id))
                remaining.remove(member)
                assigned += 1
                diggers += 1
                orders.append(HordeOrder(member.player_id, target.player_id, "dig_root",
                                         stand.goal, cells=stand.cells, aim=stand.site))
                self.metrics.dig_orders += 1
            if not assigned and stand not in elevated_work:
                elevated_work.append(stand)
        capable = [m for m in remaining if m.can_dig or m.can_build]
        # Nobody climbs onto a footing that is being cut from under him:
        # with a cut in hand only its elevated claw sites are destinations.
        destinations: list[WorkStand] = list(elevated_work) + (reach if plan is None else [])
        if not destinations and plan is None:
            destinations = [WorkStand(tuple(float(v) for v in target.position),
                                      _floor_of(target.position), "prey")]
        if destinations and capable:
            # Diggers are drawn from the waiting ring first, never from the
            # climb crew: a climber half way up is not relieved because a
            # teammate reached a claw site.
            waiting = max(0, int(len(group) * PRESSURE_SHARE) - diggers)
            crew = min(MAX_CLIMBERS, len(capable), max(MIN_CLIMBERS, len(capable) - waiting))
            # Committed crews keep climbing as members move past one another;
            # a climber that is getting nowhere is the first to be relieved.
            climbers = sorted(capable, key=lambda m: (
                self._stuck_level(m.player_id) >= 2,
                self._climb_slot.get(m.player_id, (None, 0))[0] != target.player_id,
                self.preference(m.player_id, target.player_id), m.player_id))[:crew]
            # Each climber holds a side of the footing (hidden from the
            # survivor where the ground allows) and stages there before it
            # starts up: one side would be one queue for his rifle.
            sides = self._staging_sides(target, siege, solid, max(2, len(climbers)), now)
            taken = {self._climb_slot[m.player_id][1] for m in climbers
                     if self._climb_slot.get(m.player_id, (None, 0))[0] == target.player_id
                     and self._climb_slot[m.player_id][1] < len(sides)}
            for member in climbers:
                slot = self._climb_slot.get(member.player_id)
                if slot is not None and slot[0] == target.player_id and slot[1] < len(sides):
                    continue
                free = [k for k in range(len(sides)) if k not in taken]
                if not free:
                    self._climb_slot.pop(member.player_id, None)
                    continue
                k = min(free, key=lambda k: (k if sides[k][1] else k + len(sides),
                                             math.dist(member.position, sides[k][0])))
                taken.add(k)
                self._climb_slot[member.player_id] = (target.player_id, k)
            self.metrics.relieved_climbers += sum(
                1 for m in capable if m not in climbers
                and self._roles.get(m.player_id) == (target.player_id, "climb"))
            load = [0] * len(destinations)
            for member in climbers:
                remaining.remove(member)
                failed = self._climb_failed.get(member.player_id, {})
                options = [i for i, stand in enumerate(destinations)
                           if failed.get(_stand_key(stand.goal), -math.inf) <= now]
                if not options:
                    # Every way up known has failed this bot: record it and
                    # take the one that failed longest ago rather than idle.
                    self.metrics.ways_exhausted += 1
                    options = sorted(range(len(destinations)),
                                     key=lambda i: failed.get(_stand_key(destinations[i].goal), 0.0))[:1]
                best = min(options, key=lambda i: (
                    load[i],
                    math.dist(member.position, destinations[i].goal)
                    + 4.0 * self.preference(member.player_id, target.player_id + 31 * i),
                    i))
                load[best] += 1
                stand = destinations[best]
                slot = self._climb_slot.get(member.player_id)
                side = sides[slot[1]][0] if slot is not None and slot[0] == target.player_id else None
                if (side is not None
                        and _xy_distance(member.position, side) > STAGE_REACH
                        and abs(float(member.position[2]) - float(side[2])) < 3.0
                        and _xy_distance(member.position, stand.goal)
                        > _xy_distance(side, stand.goal) - 1.0):
                    # Not yet on its side and still on the horde's floor:
                    # walk round to it first (a flank: it ignores his fire
                    # on the way, eyes on him).
                    orders.append(HordeOrder(member.player_id, target.player_id, "flank",
                                             side, aim=stand.site))
                    self.metrics.climb_orders += 1
                    continue
                orders.append(HordeOrder(member.player_id, target.player_id, "climb",
                                         stand.goal, cells=stand.cells, aim=stand.site))
                self.metrics.climb_orders += 1
        # Everyone else paces just clear of the footing that is coming down
        # (its measured footprint when the flood closed), one stride from
        # where the survivor lands, eyes on him.
        approach = siege.approach
        radius = SURROUND_RADIUS
        if approach is not None and approach.closed:
            radius = max(RING_MIN, approach.radius + RING_CLEARANCE)
        slots = max(4, len(group))
        roster_slots = {member.player_id: slot for slot, member in enumerate(
            sorted(group, key=lambda m: m.player_id))}
        phase = 0.6 * target.player_id + 2 * math.pi * self.preference(target.player_id)
        for member in remaining:
            slot = roster_slots[member.player_id]
            turn = int((now + PACE_SECONDS * self._beat(member.player_id)) // PACE_SECONDS)
            step = turn if slot % 2 == 0 else -turn
            raw = _ring_point(target.position, slot + step, slots,
                              radius + 1.5 * (slot % 2), siege.floor_z, phase=phase)
            goal = standable_near(solid, raw, siege.floor_z) or raw
            orders.append(HordeOrder(member.player_id, target.player_id, "surround", goal))
            self.metrics.surround_orders += 1
        return orders

    def _escalate(self, order: HordeOrder, member: HordeMember,
                  target: SurvivorTarget, level: int,
                  solid: SolidFn | None, now: float = 0.0) -> HordeOrder:
        """A stalled walker tries another side, then digs, then climbs."""

        if level <= 0:
            return order
        if order.role == "climb":
            if level >= 2:
                # This way up is not working: the next orders send the bot
                # to another destination (or relieve it); the skill layer
                # tries another side meanwhile.
                self._climb_failed.setdefault(order.zombie_id, {})[
                    _stand_key(order.goal)] = now + CLIMB_FAIL_SECONDS
            return HordeOrder(order.zombie_id, order.target_id, order.role,
                              order.goal, order.cells, order.aim, level)
        if order.role in ("surround", "dig_root"):
            if level >= 2 and order.role == "dig_root":
                cells = tunnel_cells(solid, member.position, order.goal, length=3.0)
                if cells:
                    return HordeOrder(order.zombie_id, order.target_id, "tunnel",
                                      order.goal, cells=cells + order.cells,
                                      aim=order.aim, stuck_level=level)
            return HordeOrder(order.zombie_id, order.target_id, order.role,
                              order.goal, order.cells, order.aim, level)
        if order.role == "tunnel" and (level < 2 or member.working):
            # Already clawing by choice; give it up only when the swings
            # have stopped landing and the stall has lasted.
            return HordeOrder(order.zombie_id, order.target_id, order.role,
                              order.goal, order.cells, order.aim, level)
        # Clawing only pays off near the prey; far away the route planner's
        # own breach edges and detours are the better tool.
        near = _xy_distance(member.position, target.position) <= TUNNEL_RANGE
        position = tuple(float(v) for v in target.position)
        # The first alarm sends a walker round another side: where clawing
        # was the quicker way the estimate has already chosen it (_breach),
        # and digging at every rise of rough ground holds a horde up (on
        # MayanJungle it kept half of it fifteen blocks short of the prey).
        cells = (tunnel_cells(solid, member.position, target.position)
                 if near and level >= 2 and order.role != "tunnel" else ())
        if cells and member.can_dig:
            return HordeOrder(order.zombie_id, order.target_id, "tunnel", position,
                              cells=cells, aim=cells[0], stuck_level=level)
        above = float(member.position[2]) - float(target.position[2]) >= 2.0
        if level >= 2 and near and above and (member.can_dig or member.can_build):
            return HordeOrder(order.zombie_id, order.target_id, "climb", position,
                              stuck_level=level)
        floor_z = _floor_of(target.position)
        goal = standable_near(solid, _ring_point(
            target.position, member.player_id + level, 4, FLANK_RADIUS, floor_z,
            phase=math.pi / 4.0), floor_z)
        if goal is None:
            return HordeOrder(order.zombie_id, order.target_id, order.role,
                              order.goal, order.cells, order.aim, level)
        return HordeOrder(order.zombie_id, order.target_id, "flank", goal,
                          stuck_level=level)


__all__ = [
    "HordeCoordinator",
    "HordeMember",
    "HordeMetrics",
    "HordeOrder",
    "ROLE_CODES",
    "ROLE_NAMES",
    "SiegeInfo",
    "SurvivorTarget",
    "WallLine",
    "WorkStand",
    "air_stand",
    "bot_respawn_spot",
    "dig_stand_spot",
    "pile_count",
    "reach_stands",
    "standable_near",
    "tunnel_cells",
    "visible",
    "wall_line",
]
