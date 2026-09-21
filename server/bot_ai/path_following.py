"""Follow a voxel route the way a player walks it.

The planner returns one waypoint per grid cell. Steering at each cell in turn
made bots stop and re-aim every block: they crawled, and because movement keys
are relative to the view, their heads followed every zigzag of the grid.

A player looks at the far end of the stretch in front of them and holds
forward. ``lookahead`` finds that far end: the farthest waypoint of the current
run of plain walking steps that the body can reach in a straight line over
real, body-wide, step-height terrain. Jumps, drops, digs and builds keep their
exact cell-by-cell handling; a run never extends across one.
"""

from __future__ import annotations

import math
from typing import Sequence

from .messages import MovementAffordance, Vector3

_MAX_LOOKAHEAD_STEPS = 16
_MAX_LOOKAHEAD_DISTANCE = 12.0
_BODY_HALF_WIDTH = 0.42
_SAMPLE_SPACING = 0.5
_STEP_HEIGHT = 1.05
# Candidates are tried far to near; the first straight walk wins, so open
# ground costs one line test per decision.
_CANDIDATE_OFFSETS = (16, 11, 7, 4, 2, 1)
_RETARGET_DISTANCE = 6.5
_TAKEOFF_TOLERANCE = 0.3
# Falls start to hurt at ten blocks (six for the Classic soldier). Running off
# a ledge this high is ordinary movement for a player and so for a bot.
SAFE_DROP = 4.0
_RUNNING = frozenset({MovementAffordance.WALK, MovementAffordance.DROP})
_GAZE_DISTANCE = 10.0
_NEAR_DISTANCE = 3.5
_NEAR_HALF_WIDTH = 0.25


def run_end(route: Sequence, index: int) -> int:
    """Last index of the unbroken plain-walk run starting at ``index``.

    Waypoints are compacted, so a staircase can arrive as one stride that
    climbs several blocks: a slope of one block per block is still walking.
    """

    if index >= len(route) or route[index].affordance not in _RUNNING:
        return index
    end = index
    limit = min(len(route), index + _MAX_LOOKAHEAD_STEPS + 1)
    for following in range(index + 1, limit):
        step, before = route[following], route[following - 1]
        stride = math.hypot(step.waypoint[0] - before.waypoint[0],
                            step.waypoint[1] - before.waypoint[1])
        change = step.waypoint[2] - before.waypoint[2]  # z grows downward
        if (step.affordance not in _RUNNING
                or -change > max(1.0, stride) * _STEP_HEIGHT
                or change > max(SAFE_DROP, stride * _STEP_HEIGHT)):
            break
        end = following
    return end


def edge_axis(step) -> Vector3 | None:
    """Unit direction of a cardinal jump/drop edge, or ``None``.

    Waypoints are nudged toward walls for walking. Aiming an exact edge at
    that nudged point makes it slightly diagonal, which is enough to swing a
    shoulder of the motor's safety probe over the neighbouring, often much
    deeper, column and have the whole move refused. The edge has a direction
    of its own; use it.
    """

    edge = getattr(step, "entry_edge", None)
    if edge is None:
        return None
    (source_x, source_y, _), (target_x, target_y, _) = edge
    axis_x = (target_x > source_x) - (target_x < source_x)
    axis_y = (target_y > source_y) - (target_y < source_y)
    if (axis_x == 0) == (axis_y == 0):
        return None
    return (float(axis_x), float(axis_y), 0.0)


def takeoff_alignment(step, position: Vector3) -> Vector3 | None:
    """Where to sidestep so a jump or drop is taken along its own line.

    Planner edges are cardinal: a drop leaves one cell straight into the next.
    A body that has drifted into the neighbouring row attacks that edge on a
    diagonal, the motor's safety probe then looks down a different and often
    deadly column, refuses, and the bot stands at the lip until it gives up.
    Players shuffle sideways onto the line first; this is that shuffle's aim,
    or ``None`` once lined up or already over the lip.
    """

    edge = getattr(step, "entry_edge", None)
    if edge is None:
        return None
    (source_x, source_y, _), (target_x, target_y, _) = edge
    axis_x = (target_x > source_x) - (target_x < source_x)
    axis_y = (target_y > source_y) - (target_y < source_y)
    if (axis_x == 0) == (axis_y == 0):
        return None  # not a cardinal edge: nothing to line up with
    offset_x = position[0] - (source_x + 0.5)
    offset_y = position[1] - (source_y + 0.5)
    along = offset_x * axis_x + offset_y * axis_y
    lateral = -offset_x * axis_y + offset_y * axis_x
    if abs(lateral) <= _TAKEOFF_TOLERANCE or along > 0.45 or abs(lateral) > 2.5:
        return None
    back = min(along, 0.1)
    return (source_x + 0.5 + axis_x * back, source_y + 0.5 + axis_y * back, position[2])


def straight_walkable(world, origin: Vector3, target: Vector3,
                      *, half_width: float = _BODY_HALF_WIDTH) -> bool:
    """Can a body walk the straight line without a wall, pit, ledge or water?"""

    dx, dy = target[0] - origin[0], target[1] - origin[1]
    length = math.hypot(dx, dy)
    if length < 1e-6:
        return True
    ux, uy = dx / length, dy / length
    side_x, side_y = -uy * half_width, ux * half_width
    height = origin[2]
    samples = max(1, int(math.ceil(length / _SAMPLE_SPACING)))
    for number in range(1, samples + 1):
        travelled = min(length, number * _SAMPLE_SPACING)
        x, y = origin[0] + ux * travelled, origin[1] + uy * travelled
        centre = None
        for offset_x, offset_y in ((0.0, 0.0), (side_x, side_y), (-side_x, -side_y)):
            surface = _ground(world, x + offset_x, y + offset_y, height)
            if surface is None:
                return False
            if centre is None:
                centre = surface
        height = centre
    return abs(height - target[2]) <= _STEP_HEIGHT


def _ground(world, x: float, y: float, height: float) -> float | None:
    """Head height after stepping onto this column: up one, or down a safe drop."""

    cell_x, cell_y = int(math.floor(x)), int(math.floor(y))
    solid = getattr(world, "solid", None)
    feet = int(round(height + 2.25))
    level = world.surface(cell_x, cell_y, height, vertical_span=1, allow_water=False)
    if level is not None and abs(level.position[2] - height) <= _STEP_HEIGHT:
        # A surface only promises two clear cells. The body is three tall and
        # enters the column at the height it is walking at: a step down under
        # a lower ceiling is a crouch the planner must author, not a walk.
        top = min(feet, int(level.support_z)) - 3
        if callable(solid) and any(solid(cell_x, cell_y, z)
                                   for z in range(top, int(level.support_z))):
            return None
        return level.position[2]
    if not callable(solid):
        return None
    if any(solid(cell_x, cell_y, z) for z in range(feet - 3, feet)):
        return None  # a wall, not a ledge
    for support in range(feet, feet + int(SAFE_DROP) + 1):
        if solid(cell_x, cell_y, support):
            # First thing underfoot on the way down; dry, or it is no landing.
            landing = world.surface(cell_x, cell_y, support - 2.25,
                                    vertical_span=0, allow_water=False)
            return None if landing is None else landing.position[2]
    return None


def runs_off(world, step, position: Vector3) -> bool:
    """Is this planned drop just a ledge to run off, straight ahead and harmless?"""

    return (step.affordance is MovementAffordance.DROP
            and step.waypoint[2] - position[2] <= SAFE_DROP + 0.05
            and callable(getattr(world, "surface", None))
            and straight_walkable(world, position, step.waypoint,
                                  half_width=_NEAR_HALF_WIDTH))


def lookahead(world, route: Sequence, index: int, position: Vector3,
              *, keep: Vector3 | None = None) -> int:
    """Index of the farthest waypoint of this run reachable in a straight walk.

    ``keep`` is the waypoint chosen last time. While it is still ahead, still
    reachable and not nearly underfoot it is kept, so the heading holds steady
    for a second or so like a player walking at a landmark, instead of being
    re-aimed at a slightly different cell on every decision.
    """

    end = run_end(route, index)
    if end <= index or not callable(getattr(world, "surface", None)):
        return index
    if keep is not None:
        for candidate in range(index + 1, end + 1):
            if route[candidate].waypoint == keep:
                gap = math.hypot(keep[0] - position[0], keep[1] - position[1])
                if (gap >= _RETARGET_DISTANCE or candidate == end) and straight_walkable(
                        world, position, keep):
                    return candidate
                break
    for offset in _CANDIDATE_OFFSETS:
        candidate = min(end, index + offset)
        if candidate <= index:
            continue
        waypoint = route[candidate].waypoint
        gap = math.hypot(waypoint[0] - position[0], waypoint[1] - position[1])
        if gap > _MAX_LOOKAHEAD_DISTANCE:
            continue
        # The planner already proved the next cells body-clear. Only a long
        # shortcut across unplanned ground needs the full shoulder width.
        width = _BODY_HALF_WIDTH if gap > _NEAR_DISTANCE else _NEAR_HALF_WIDTH
        if straight_walkable(world, position, waypoint, half_width=width):
            return candidate
    return index


def held_index(route: Sequence, index: int, keep: Vector3 | None) -> int:
    """Index of the steering point chosen earlier, while it is still on this run."""

    if keep is None:
        return index
    for candidate in range(index + 1, run_end(route, index) + 1):
        if route[candidate].waypoint == keep:
            return candidate
    return index


def passed_index(route: Sequence, index: int, target_index: int, position: Vector3) -> int:
    """Skip waypoints the body has already walked past on its way to the target.

    Cutting a corner never touches the skipped cells' centres, so completion
    is judged by progress along the heading to the target instead.
    """

    target = route[target_index].waypoint
    hx, hy = target[0] - position[0], target[1] - position[1]
    while index < target_index:
        waypoint = route[index].waypoint
        ahead = (waypoint[0] - position[0]) * hx + (waypoint[1] - position[1]) * hy
        if ahead > 0.0 and math.hypot(waypoint[0] - position[0], waypoint[1] - position[1]) > 0.9:
            break
        index += 1
    return index


def gaze_waypoint(route: Sequence, index: int, position: Vector3) -> Vector3 | None:
    """Where the route is heading, for the eyes to rest on during exact steps.

    Terrace stairs, jump take-offs and landings put single sidesteps at right
    angles to the direction of travel. Facing each of them swung the head 90
    degrees and back for one block. The movement keys are relative to the
    view, so a player strafes such a step while looking where they are going:
    this is that "where", some blocks along the route whatever the steps are.
    """

    travelled, previous, chosen = 0.0, position, None
    for step in route[index:index + _MAX_LOOKAHEAD_STEPS * 2]:
        travelled += math.hypot(step.waypoint[0] - previous[0], step.waypoint[1] - previous[1])
        previous = chosen = step.waypoint
        if travelled >= _GAZE_DISTANCE:
            break
    if chosen is None or math.hypot(chosen[0] - position[0], chosen[1] - position[1]) < 3.0:
        return None
    return chosen


def remaining_distance(route: Sequence, index: int, position: Vector3) -> float:
    """Planar length of the route still ahead of the body."""

    total, previous = 0.0, position
    for step in route[index:]:
        total += math.hypot(step.waypoint[0] - previous[0], step.waypoint[1] - previous[1])
        previous = step.waypoint
    return total
