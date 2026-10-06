"""
Authoritative combat and block-damage helpers.
"""

from __future__ import annotations

import logging
import math
import random
import time
from typing import Optional

from aoslib.world import cube_line
import shared.constants as C
from server.audio import SND_BUILD, play_sound
from server.dig_profiles import (
    DEFAULT_MELEE_PROFILE,
    DIG_COLUMN,
    DIG_CUBE,
    DIG_MACHETE,
    DIG_SINGLE,
    MELEE_DIG_PROFILES,
    melee_dig_positions as _melee_dig_positions,
)
from server.game_constants import (
    BLOCK_ACTION_BUILD,
    BLOCK_ACTION_DESTROY,
    DEFAULT_BLOCK_HEALTH,
    KILL_HEADSHOT,
    MAX_BUILD_Z,
    MELEE_RANGE,
    build_z_is_safe,
)
from shared.packet import (
    BlockBuild,
    BlockBuildColored,
    BlockLine,
    HitEntity,
    PaintBlockPacket,
    ShootFeedbackPacket,
    ShootPacket,
    ShootResponse,
)
from server.world_mutations import PendingWorldMutation
from server.colors import pack_rgb, unpack_rgb
from server import block_damage_model as _damage_model

logger = logging.getLogger(__name__)
# One INFO line per dropped (never resolved) shot and per near miss of a
# slow single-pellet gun, so a player's "my shot did not register" clip can
# be matched to the server's reason. Rate-limited per player.
shot_logger = logging.getLogger("combat.shots")
SHOT_LOG_BURST = 20
SHOT_LOG_WINDOW_SECONDS = 10.0
# Near misses are logged for guns firing at most this often (sniper, rifle,
# pistol, snub pistol...) and when the ray passed this close to an enemy.
NEAR_MISS_MIN_INTERVAL = 0.4
NEAR_MISS_DISTANCE = 1.0
# The log window keeps its own clock: tests and the lab drive
# ``time.monotonic`` as the simulation clock.
_shot_log_clock = time.perf_counter

SHOT_ORIGIN_TOLERANCE = 8.0
# Initial health of every player-built voxel.  Live-measured on the stock
# client (2026-09-26): BlockBuild(32) type 0, BlockLine(40) and prefab
# BuildPrefabAction(30) cells all enter BlockManager.user_blocks at 9.0
# (DEFAULT_PREFAB_HEALTH); BlockBuildColored(33) enters at 3.0 and an
# untouched map voxel uses DEFAULT_BLOCK_HEALTH (5.0).  The mode rules of
# retail add_user_block (Classic -> 5, UGC -> untracked) are applied by
# WorldManager.user_block_health for every caller.
USER_BLOCK_HEALTH = float(getattr(C, "DEFAULT_PREFAB_HEALTH", 9))
# Retail BlockToolCommon limits placement to MAX_BLOCK_DISTANCE (10) from the
# client eye to the target cube. The server eye lags the client by the
# reconciliation delay, so reuse the ShootPacket origin-drift allowance as
# slack (+1 for the cube centre/diagonal). This only rejects map-wide edits;
# every placement a stock client can make stays well inside it.
BUILD_REACH = float(getattr(C, "MAX_BLOCK_DISTANCE", 10)) + 1.0 + SHOT_ORIGIN_TOLERANCE
# Classic (``manager.classic``): BlockToolCommon/PaintbrushTool use
# CLASSIC_MAX_BLOCK_DISTANCE (A1012 = 5) instead of A1017 = 10.
CLASSIC_BUILD_REACH = (
    float(getattr(C, "CLASSIC_MAX_BLOCK_DISTANCE", 5)) + 1.0 + SHOT_ORIGIN_TOLERANCE
)
# Stock server-only MIN_BLOCK_INTERVAL (A1016 = 0.1 s; no client pyc/pyd reads
# it): the least time between two accepted block builds/lines of one player.
# The stock BlockTool itself fires at most every 0.5 s. A small grace absorbs
# two packets bunched by one late network frame.
MIN_BLOCK_INTERVAL = float(getattr(C, "MIN_BLOCK_INTERVAL", 0.1))
MIN_BLOCK_INTERVAL_GRACE = 0.02
# Melee digging via legacy BlockLiberate(35): the swing reach plus the same
# drift slack.
DIG_REACH = float(MELEE_RANGE) + 1.0 + SHOT_ORIGIN_TOLERANCE
# Minimum spacing between accepted block-tool BlockLiberate(35) removals.
BLOCK_TOOL_LIBERATE_INTERVAL = 0.2
# PaintBlock(7) token bucket: the UGC editor host replicates one packet per
# brushed cell (a surface brush is capped at 128 cells every 30 ms), ordinary
# block-tool paint is a single-cell action.
PAINT_EDITOR_BURST = 256.0
PAINT_EDITOR_RATE = 4400.0
PAINT_BURST = 16.0
PAINT_RATE = 30.0
SHOT_ORIENTATION_DOT_TOLERANCE = 0.25
# --- anti-cheat geometry ----------------------------------------------------
# Voxel line-of-sight segments are shortened this much at each end so an eye
# brushing a wall face (or a launch point nudged into it) is never occluded
# by the wall it touches. A full 1-block wall is still always detected.
LOS_SHRINK = 0.3
# Extra reach a melee target/dug cell may have beyond MELEE_RANGE measured
# from the SERVER eye (the client eye differs by the reconciliation drift).
MELEE_EYE_SLACK = 1.5
# Half-diagonal of a voxel: distance from a cell centre to its farthest corner.
_CELL_HALF_DIAGONAL = math.sqrt(3.0) * 0.5
# When the shot's own loop is not (yet) in the eye history the current eye can
# be ahead of/behind the client by this much movement time.
FALLBACK_LAG_SECONDS = 0.3
MAX_LAG_SLACK = 4.0
# Older applied loops also accepted as a line-of-sight source, so a player
# rounding a corner under lag never has a real action rejected.
_HISTORY_LOOKBACK = (6, 12, 24)
# Histogram bucket upper bounds for per-player detection aggregates.
ORIGIN_ERROR_BUCKETS = (0.1, 0.25, 0.5, 1.0, 1.5, 2.0, 4.0, 8.0)
AIM_ANGLE_BUCKETS = (0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 45.0, 90.0)
# Pellet-seed skew: flag when one 8-bit seed dominates a player's shotgun fire.
SEED_SKEW_MIN_SHOTS = 32
SEED_SKEW_MIN_REPEATS = 6
SEED_SKEW_SHARE = 0.2
# Stock ``variable_accuracy`` hit-scan guns: (accuracy_spread_min,
# accuracy_spread_max, accuracy_spread_increase_per_shot,
# accuracy_spread_reduction_speed) from the stock Weapon subclasses
# (aos.pkg bytecode; identical to the native client's generated
# weapon_catalog RetailAimTuning). accuracy_min/max live on WeaponProfile.
# Guns absent here fire at their constant class ``accuracy`` (profile.spread).
RETAIL_ACCURACY_SPREAD = {
    7: (1.0, 6.0, 0.2, 1.0),     # SMG
    8: (2.0, 7.0, 0.3, 2.0),     # MINIGUN
    9: (4.0, 7.0, 0.5, 1.0),     # SHOTGUN
    10: (4.0, 7.0, 0.5, 1.0),    # SHOTGUN2
    15: (1.0, 6.0, 0.2, 0.6),    # MG
    35: (1.0, 5.0, 0.1, 0.5),    # TOMMYGUN
    37: (4.0, 7.0, 0.5, 1.0),    # CLASSIC_SHOTGUN
    38: (1.0, 6.0, 0.2, 0.6),    # CLASSIC_SMG
    53: (1.5, 6.5, 0.3, 2.0),    # AUTOMATIC_PISTOL
    60: (0.5, 1.0, 0.5, 0.7),    # ASSAULT_RIFLE
    61: (2.0, 6.0, 0.5, 1.4),    # LIGHT_MACHINE_GUN
    62: (4.0, 7.0, 0.5, 1.0),    # AUTO_SHOTGUN
}
# Stock ``accuracy_zoom`` where the class sets one (None elsewhere, so a
# zoomed shot keeps the hip accuracy with the tighter 2/1 random mapping).
RETAIL_ACCURACY_ZOOM = {
    18: 0.0,  # SNIPER
    19: 0.0,  # SNIPER2
}
HITBOX_SCALE = 0.05
ASSAULT_BURST_SIZE = 3
ASSAULT_BURST_WINDOW = 0.30
ASSAULT_BURST_LOOP_INTERVAL = 6
# Stock MinigunWeapon: shoot_interval_initial 0.3 (A1242), cap 0.3-0.2 (A1245),
# -0.15/s while a trigger is held (A1243), +0.075/s when released (A1244).
MINIGUN_INTERVAL_INITIAL = 0.30
MINIGUN_INTERVAL_MIN = 0.10
MINIGUN_INTERVAL_RAMP_PER_SECOND = 0.15
MINIGUN_INTERVAL_RECOVER_PER_SECOND = 0.075
# can_shoot_primary needs spin_speed > 0.5 (A1248) of 5 (A1247): the interval
# is already below 0.3 - 0.2 * 0.1 = 0.28 when a cold gun fires its first round.
MINIGUN_FIRST_SHOT_INTERVAL = 0.28
# Packet-arrival jitter still counted as one continuous burst.
MINIGUN_CONTINUITY_SLACK = 0.1
MINIGUN_HELD_GAP_LIMIT = 1.0

# Stock KV6 collision-model bounding boxes. Values are
# ((size_x, size_y, size_z), (effective_pivot_x, pivot_y, pivot_z)); effective
# pivots include CLASS_BODY_PARTS_OFFSETS. The client tests these oriented
# boxes, not a generic player cylinder/AABB and not occupied KV6 voxels.
_COMMON_ARMS = ((24, 20, 12), (12, 2, 1))
_ZOMBIE_ARMS = ((24, 20, 12), (12, 10, 6))
_CROUCH_TORSO = ((16, 16, 14), (8, 14, 2))
_CROUCH_LEG = ((6, 14, 16), (3, 7, 3))
# ``kv6/ClassicCorpse.kv6`` header: size 48x50x14, pivot 24.5/25/7.
# KillAction owns the model; this box is only for authoritative shot ordering.
_CLASSIC_CORPSE_BOUNDS = ((48, 50, 14), (24.5, 25.0, 7.0))


def _part(size, pivot):
    return (size, pivot)


_CLASS_HITBOXES = {
    C.CLASS_SOLDIER: (_part((14, 16, 15), (7, 8, 13)), _part((16, 9, 19), (7, 6, .5)), _COMMON_ARMS, _part((6, 11, 24), (3, 5.5, 0)), _part((6, 11, 24), (3, 5.5, 0))),
    C.CLASS_SCOUT: (_part((16, 16, 12), (8, 8, 11.5)), _part((16, 8, 18), (8, 5.5, 0)), _COMMON_ARMS, _part((6, 11, 24), (3, 5.5, 0)), _part((6, 11, 24), (3, 5.5, 0))),
    C.CLASS_ROCKETEER: (_part((16, 14, 12), (8, 7, 11.5)), _part((16, 10, 18), (8, 5, 0)), _COMMON_ARMS, _part((6, 10, 24), (3, 5, 0)), _part((6, 10, 24), (3, 5, 0))),
    C.CLASS_MINER: (_part((16, 17, 12), (8, 8.5, 11.5)), _part((16, 11, 18), (8, 5.5, 0)), _COMMON_ARMS, _part((6, 10, 24), (3, 5, 0)), _part((6, 10, 24), (3, 5, 0))),
    C.CLASS_ZOMBIE: (_part((11, 12, 12), (5.5, 6, 11.5)), _part((16, 8, 18), (8, 4, 0)), _ZOMBIE_ARMS, _part((6, 10, 24), (3, 5, 0)), _part((6, 10, 24), (3, 5, 0))),
    C.CLASS_CLASSIC_SOLDIER: (_part((14, 14, 12), (7, 8, 11.5)), _part((18, 12, 20), (8, 7.5, 1)), _COMMON_ARMS, _part((6, 10, 24), (3, 5, 0)), _part((6, 10, 24), (3, 5, 0))),
    C.CLASS_GANGSTER_1: (_part((16, 19, 16), (8, 8.5, 13)), _part((16, 8, 19), (7.5, 4.5, 1)), _COMMON_ARMS, _part((6, 10, 24), (3, 5, 0)), _part((6, 10, 24), (3, 5, 0))),
    C.CLASS_GANGSTER_2: (_part((12, 16, 12), (6, 6.5, 11.5)), _part((16, 8, 19), (7.5, 4.5, 1)), _COMMON_ARMS, _part((6, 10, 24), (3, 5, 0)), _part((6, 10, 24), (3, 5, 0))),
    C.CLASS_GANGSTER_3: (_part((14, 16, 11), (7, 7.5, 10.5)), _part((16, 8, 19), (7.5, 4.5, 1)), _COMMON_ARMS, _part((6, 9, 24), (3, 4, 0)), _part((6, 9, 24), (3, 4, 0))),
    C.CLASS_GANGSTER_4: (_part((20, 21, 13), (10, 10.5, 12)), _part((16, 8, 19), (7.5, 4.5, 1)), _COMMON_ARMS, _part((6, 10, 24), (3, 5, 0)), _part((6, 10, 24), (3, 5, 0))),
    C.CLASS_GANGSTER_VIP_1: (_part((12, 15, 12), (6, 6.5, 11.5)), _part((18, 11, 26), (8.5, 6.5, 1)), _COMMON_ARMS, _part((6, 9, 24), (3, 4, 0)), _part((6, 9, 24), (3, 4, 0))),
    C.CLASS_GANGSTER_VIP_2: (_part((14, 15, 11), (7, 6.5, 10.5)), _part((16, 8, 19), (7.5, 4.5, 1)), _COMMON_ARMS, _part((6, 10, 24), (3, 5, 0)), _part((6, 10, 24), (3, 5, 0))),
    C.CLASS_ENGINEER: (_part((16, 13, 12), (8, 6.5, 11.5)), _part((16, 11, 18), (8, 5.5, 0)), _COMMON_ARMS, _part((6, 11, 24), (3, 5.5, 0)), _part((6, 11, 24), (3, 5.5, 0))),
    C.CLASS_UGCBUILDER: (_part((16, 17, 12), (8, 8.5, 11.5)), _part((16, 11, 18), (8, 5.5, 0)), _COMMON_ARMS, _part((6, 10, 24), (3, 5, 0)), _part((6, 10, 24), (3, 5, 0))),
    C.CLASS_SPECIALIST: (_part((14, 15, 13), (7, 8, 12.5)), _part((16, 10, 18), (8, 6.5, -1)), _COMMON_ARMS, _part((8, 11, 24), (4, 5.5, 0)), _part((8, 11, 24), (4, 5.5, 0))),
    C.CLASS_MEDIC: (_part((14, 13, 13), (7, 7, 11.5)), _part((16, 11, 17), (7, 7, -.5)), _COMMON_ARMS, _part((8, 12, 24), (4, 6, 0)), _part((8, 12, 24), (2, 6, 0))),
}
_CLASS_HITBOXES[C.CLASS_FAST_ZOMBIE] = _CLASS_HITBOXES[C.CLASS_ZOMBIE]
_CLASS_HITBOXES[C.CLASS_JUMP_ZOMBIE] = _CLASS_HITBOXES[C.CLASS_ZOMBIE]


# PlaySound(23) is needed for every observer, including the actor.  The retail
# tool source plays its swing/miss cue immediately, but neither that prediction
# nor Damage(37) reliably produces the successful block-impact sample.  Values
# are the live constants_audio SOUND_MAP ids.
_BLOCK_HIT_SOUND_BY_DAMAGE = {
    int(getattr(C, "PICKAXE_DAMAGE", 0)): 36,
    int(getattr(C, "KNIFE_DAMAGE", 1)): 35,
    int(getattr(C, "SPADE_DAMAGE", 2)): 33,
    int(getattr(C, "SUPERSPADE_DAMAGE", 3)): 37,
    int(getattr(C, "CLASSIC_SPADE_DAMAGE", 4)): 33,
    int(getattr(C, "ZOMBIE_DAMAGE", 17)): 38,
    int(getattr(C, "CROWBAR_DAMAGE", 26)): 34,
    int(getattr(C, "UGC_PICKAXE_DAMAGE", 28)): 36,
    int(getattr(C, "UGC_SUPERSPADE_DAMAGE", 29)): 37,
    int(getattr(C, "UGC_SUPERSPADE_SECONDARY_DAMAGE", 31)): 37,
    # Machete has no dedicated entry in this client's 61-id SOUND_MAP. Knife
    # is the closest safe stock cue; arbitrary sound filenames cannot travel
    # in PlaySound(23).
    int(getattr(C, "MACHETE_DAMAGE", 35)): 35,
}


def _finite_point(value):
    """Return ``value`` as a finite 3-tuple of floats, or ``None``."""

    try:
        point = tuple(float(component) for component in value)
    except (TypeError, ValueError, OverflowError):
        return None
    if len(point) != 3 or not all(math.isfinite(c) for c in point):
        return None
    return point


def segment_clear(
    world,
    start,
    end,
    *,
    shrink_start: float = LOS_SHRINK,
    shrink_end: float = LOS_SHRINK,
    ignore=(),
) -> bool:
    """Whether the segment ``start``→``end`` crosses only air voxels.

    Exact voxel traversal (Amanatides–Woo) over ``world.get_solid``; the
    segment is shortened by ``shrink_start``/``shrink_end`` so geometry the
    endpoints merely touch never occludes. Cells in ``ignore`` are treated as
    air (the target cell of a dig/placement). Without a terrain oracle the
    segment is considered clear.
    """

    get_solid = getattr(world, "get_solid", None)
    if not callable(get_solid):
        return True
    start = _finite_point(start)
    end = _finite_point(end)
    if start is None or end is None:
        return False
    delta = tuple(end[i] - start[i] for i in range(3))
    length = math.sqrt(sum(c * c for c in delta))
    usable = length - float(shrink_start) - float(shrink_end)
    if usable <= 1e-6:
        return True
    unit = tuple(c / length for c in delta)
    origin = tuple(start[i] + unit[i] * float(shrink_start) for i in range(3))
    cell = [int(math.floor(origin[i])) for i in range(3)]
    last = tuple(
        int(math.floor(origin[i] + unit[i] * usable)) for i in range(3)
    )
    step = [0, 0, 0]
    t_max = [math.inf, math.inf, math.inf]
    t_delta = [math.inf, math.inf, math.inf]
    for axis in range(3):
        if unit[axis] > 1e-12:
            step[axis] = 1
            t_max[axis] = (cell[axis] + 1 - origin[axis]) / unit[axis]
            t_delta[axis] = 1.0 / unit[axis]
        elif unit[axis] < -1e-12:
            step[axis] = -1
            t_max[axis] = (origin[axis] - cell[axis]) / -unit[axis]
            t_delta[axis] = -1.0 / unit[axis]
    ignore = set(ignore) if ignore else ()
    # A segment of length L visits at most ~3L + 3 cells; the guard only
    # protects against pathological float input.
    for _ in range(int(usable * 3.0) + 8):
        current = (cell[0], cell[1], cell[2])
        if current not in ignore and get_solid(*current):
            return False
        if current == last:
            return True
        axis = min(range(3), key=lambda index: t_max[index])
        if t_max[axis] > usable:
            return True
        cell[axis] += step[axis]
        t_max[axis] += t_delta[axis]
    return True


def cell_sample_points(cell, inset: float = 0.1):
    """Centre, six inset face centres and eight inset corners of ``cell``."""

    x, y, z = (float(value) for value in cell)
    low, high = inset, 1.0 - inset
    points = [(x + 0.5, y + 0.5, z + 0.5)]
    for axis in range(3):
        for offset in (low, high):
            face = [x + 0.5, y + 0.5, z + 0.5]
            face[axis] = (x, y, z)[axis] + offset
            points.append(tuple(face))
    for ox in (low, high):
        for oy in (low, high):
            for oz in (low, high):
                points.append((x + ox, y + oy, z + oz))
    return points


def cell_visible(world, eyes, cell, *, ignore=()) -> bool:
    """Whether any sample point of ``cell`` is in line of sight of an eye.

    Used for reach checks on dig/build/paint/deployable targets: the stock
    client picks its target by raycasting from its eye, so the real ray
    crosses the cell's interior. Sampling several points (with the target
    cell itself treated as air) keeps grazing placements against a wall the
    player looks at valid while a cell behind a wall is rejected.
    """

    cell = tuple(int(value) for value in cell)
    skip = {cell}
    skip.update(tuple(int(v) for v in extra) for extra in ignore)
    for point in cell_sample_points(cell):
        for eye in eyes:
            if segment_clear(world, eye, point, shrink_end=0.0, ignore=skip):
                return True
    return False


def reference_eyes(player, loop=None):
    """Return ``(eye_at_loop | None, candidate eyes)`` for ``player``.

    Candidates are the eye after the action's own input frame (when the
    history has it), the current authoritative eye, and a few recently
    applied frames. Every candidate is a position the server itself
    simulated, so accepting any of them never grants a forged viewpoint.
    """

    history = getattr(player, "eye_at_loop", None)
    at_loop = None
    if callable(history) and loop is not None:
        try:
            at_loop = _finite_point(history(int(loop)) or ())
        except Exception:  # noqa: BLE001 - tolerate partial Player doubles
            at_loop = None
    eyes = []
    if at_loop is not None:
        eyes.append(at_loop)
    current = _finite_point(getattr(player, "eye", ()) or ())
    if current is not None and current not in eyes:
        eyes.append(current)
    applied = getattr(player, "applied_loop", None)
    if callable(history) and isinstance(applied, int):
        for back in _HISTORY_LOOKBACK:
            try:
                past = _finite_point(history(applied - back) or ())
            except Exception:  # noqa: BLE001
                past = None
            if past is not None and past not in eyes:
                eyes.append(past)
    return at_loop, eyes


def lag_slack(player) -> float:
    """Movement allowance for comparing against the current (not per-loop) eye."""

    velocity = _finite_point(getattr(player, "velocity", ()) or ())
    if velocity is None:
        return 0.0
    speed = math.sqrt(sum(c * c for c in velocity)) * 32.0  # blocks/second
    return min(MAX_LAG_SLACK, speed * FALLBACK_LAG_SECONDS)


def anticheat_stats(player) -> dict:
    """Per-player detection aggregates (created on first use).

    Cheap counters only; ``server.anticheat`` reporting builds summaries from
    them. Keys: shots, hits, headshots, pellet_hits, weapons{tool: {shots,
    hits, headshots}}, origin_error{bucket}, origin_error_fallback{bucket},
    aim_angle{bucket}, aim_angle_fallback{bucket}, pellet_seeds{seed},
    rejected{kind}.
    """

    stats = getattr(player, "anticheat_stats", None)
    if isinstance(stats, dict):
        return stats
    from collections import Counter

    stats = {
        "shots": 0,
        "hits": 0,
        "headshots": 0,
        "pellet_hits": 0,
        "weapons": {},
        "origin_error": Counter(),
        "origin_error_fallback": Counter(),
        "aim_angle": Counter(),
        "aim_angle_fallback": Counter(),
        "pellet_seeds": Counter(),
        "rejected": Counter(),
    }
    try:
        player.anticheat_stats = stats
    except AttributeError:
        pass
    return stats


def bucket_label(value: float, bounds) -> str:
    """Histogram label ``"<=bound"`` (or ``">last"``) for ``value``."""

    for bound in bounds:
        if value <= bound:
            return f"<={bound:g}"
    return f">{bounds[-1]:g}"


def seed_chi_square(seeds) -> float:
    """Pearson chi-square of an 8-bit seed histogram against uniform."""

    total = sum(seeds.values())
    if total <= 0:
        return 0.0
    expected = total / 256.0
    observed_sq = sum(count * count for count in seeds.values())
    # sum((o-e)^2/e) over 256 bins, zero bins included.
    return observed_sq / expected - total


def get_combat_system(server):
    combat = getattr(server, "combat", None)
    if combat is None:
        combat = CombatSystem(server)
        server.combat = combat
    return combat


class CombatSystem:
    def __init__(self, server):
        self.server = server
        self._pellet_spread = {}
        self._assault_bursts = {}
        self._minigun_runs = {}
        self._paintbrush_next_use = {}
        self._liberate_next_use = {}
        self._paint_budget = {}
        self._shot_tally = None
        self._shot_drop_reason = None

    def forget_player(self, player_id: int) -> None:
        """Discard cadence/group state before a wire player id is reused."""

        player_id = int(player_id)
        self._pellet_spread.pop(player_id, None)
        self._assault_bursts.pop(player_id, None)
        self._minigun_runs.pop(player_id, None)
        self._paintbrush_next_use.pop(player_id, None)
        self._liberate_next_use.pop(player_id, None)
        self._paint_budget.pop(player_id, None)

    # ------------------------------------------------------------------
    # Input-validation helpers shared by every client-driven terrain action
    # ------------------------------------------------------------------

    @staticmethod
    def _finite(*values) -> bool:
        """Return whether every value is a finite real number."""

        try:
            return all(math.isfinite(float(value)) for value in values)
        except (TypeError, ValueError, OverflowError):
            return False

    @staticmethod
    def _cell_in_map(cell) -> bool:
        x, y, z = cell
        return (
            0 <= int(x) < int(getattr(C, "MAP_X", 512))
            and 0 <= int(y) < int(getattr(C, "MAP_Y", 512))
            and 0 <= int(z) < int(getattr(C, "MAP_Z", 240))
        )

    def _cell_within_reach(self, player, cell, reach: float) -> bool:
        """Is the centre of ``cell`` within ``reach`` blocks of the eye?"""

        return self._eye_distance(player, cell) <= float(reach)

    def _cell_in_sight(self, player, cell, *, loop=None, kind: str) -> bool:
        """Hard line-of-sight gate for a client-selected terrain target.

        The stock client picks every dig/build/paint cell by raycasting from
        its eye, so some point of the cell is visible from it. Several
        server-simulated eyes and fifteen sample points keep grazing and
        moving placements valid; a cell behind a wall is rejected. Bots are
        server-driven (their targets are planned on the authoritative map)
        and are exempt.
        """

        if getattr(player, "is_bot", False):
            return True
        _, eyes = reference_eyes(player, loop)
        if eyes and cell_visible(self.server.world_manager, eyes, cell):
            return True
        self._reject(player, kind, cell=tuple(int(v) for v in cell))
        return False

    def _consume_paint_budget(self, player, *, editor: bool) -> bool:
        """Token-bucket cadence for PaintBlock(7) requests."""

        burst = PAINT_EDITOR_BURST if editor else PAINT_BURST
        rate = PAINT_EDITOR_RATE if editor else PAINT_RATE
        now = time.monotonic()
        player_id = int(player.id)
        tokens, last = self._paint_budget.get(player_id, (burst, now))
        tokens = min(burst, tokens + max(0.0, now - last) * rate)
        if tokens < 1.0:
            self._paint_budget[player_id] = (tokens, now)
            return False
        self._paint_budget[player_id] = (tokens - 1.0, now)
        return True

    def _queue_blocks_destroyed(self, player, cells, mined: bool) -> None:
        """Tell the mode which cells an action removed (exactly once each)."""

        queue = getattr(self.server, "queue_mode_event", None)
        cells = tuple(tuple(int(value) for value in cell) for cell in cells)
        if player is not None and cells and callable(queue):
            queue("on_blocks_destroyed", player, cells, bool(mined))

    def _queue_canonical_terrain_repair(self, positions) -> None:
        """Schedule bounded repair for a client-predicted edit footprint.

        Successful mutations already have one reliable gameplay packet and
        must never be enrolled here. Recording only rejected/cancelled
        footprints corrects local prediction without replaying native visual
        callbacks for accepted block placement.
        """

        repair = getattr(self.server, "terrain_repair", None)
        if repair is not None:
            repair.record_cells(positions)

    def _cancel_reserved_block_build(self, player, positions) -> None:
        """Refund a deferred build and repair only after it is cancelled.

        A prediction repair must not race a still-pending world mutation. The
        old pre-queue could reassert air at tick 120 and then let the valid
        build commit at tick 180, producing an avoidable air/solid topology
        flip while the owner was moving.
        """

        positions = tuple(positions)
        player.add_blocks(len(positions))
        self._queue_canonical_terrain_repair(positions)

    def handle_shot(self, player, packet) -> bool:
        if not player.alive or not player.spawned:
            self._log_shot_drop(player, packet, "not_spawned")
            return False
        from server.game_rules import get_rules
        if not get_rules(self.server.config).is_tool_enabled(
            int(getattr(player, "tool", -1))
        ):
            self._log_shot_drop(player, packet, "tool_disabled_by_rules")
            return False
        if bool(getattr(player, "pickup_burdensome", False)) and not bool(
            getattr(getattr(self.server, "mode", None), "shoot_with_intel", False)
        ):
            self._log_shot_drop(player, packet, "carrying_objective")
            return False
        if not (player.is_weapon_tool() or player.is_spade_tool()):
            self._log_shot_drop(player, packet, "held_tool_not_a_weapon")
            return False
        self._shot_drop_reason = None
        validated = self._validate_shot_packet(player, packet)
        if validated is None:
            self._log_shot_drop(
                player, packet, self._shot_drop_reason or "invalid_shot_packet"
            )
            return False
        reach_eyes, reach_slack = validated

        profile = player.get_weapon_profile()
        now = time.monotonic()
        if int(player.tool) == int(getattr(C, "ASSAULT_RIFLE_TOOL", 60)):
            if not self._accept_assault_burst_packet(player, packet, now):
                self._log_cadence_drop(player, packet, now)
                return False
        elif int(player.tool) == int(getattr(C, "MINIGUN_TOOL", 8)):
            if not self._accept_minigun_packet(
                player, now, loop=getattr(packet, "loop_count", None)
            ):
                self._log_cadence_drop(player, packet, now)
                return False
        elif int(player.tool) == int(getattr(C, "MG_TOOL", 15)):
            # MG_TOOL is only ever fired from a server-owned mounted gun
            # (the stock client selects it after ChangeEntity attaches the
            # Character). Deployment is the SERVER mount state: the client's
            # ``is_weapon_deployed`` bit used to unlock the 0.1 s cadence.
            from server.class_selection import _mounted_machine_gun_authorized

            if not _mounted_machine_gun_authorized(player, self.server):
                self._reject(player, "mg_fire_unmounted")
                self._log_shot_drop(player, packet, "mg_fire_unmounted")
                return False
            if not player.consume_shot(
                now,
                fire_interval=float(getattr(C, "MG_DEPLOYED_SHOOT_INTERVAL", 0.1)),
                loop=getattr(packet, "loop_count", None),
            ):
                self._log_cadence_drop(player, packet, now)
                return False
        elif not player.consume_shot(now, loop=getattr(packet, "loop_count", None)):
            self._log_cadence_drop(player, packet, now)
            return False

        # Only an admitted attack ends disguise. A stale, dry, or invalid
        # request must not clear a later activation. The return value below
        # describes a hit, so an accepted miss must clear it here as well.
        player.disguised = False
        from server.profile_stats import shot
        shot(player)
        from server.combat_scores import record_shot
        record_shot(self.server, player)
        self._begin_shot_tally(player)
        # Statistical aim analysis (flag-only; server/anticheat_report.py).
        from server.anticheat_report import observe_shot

        observe_shot(self.server, player, packet, now=now)
        if not player.is_spade_tool():
            # Firing a gun ends this life's spawn protection (digging does not:
            # a fresh spawn may still dig in or build cover).
            end_protection = getattr(player, "end_spawn_protection", None)
            if callable(end_protection):
                end_protection()

        if not player.is_spade_tool():
            feedback = self._build_shoot_feedback_packet(player, packet)
            # Packet 6 is the client -> server request. Retail clients
            # reproduce another firearm's shot (sound, muzzle flash and
            # tracer) from packet 8. The firing retail client already
            # predicted it locally, so feedback excludes that owner.
            #
            # Never send packet 8 for digging/melee tools: its native handler
            # calls Character.shoot(), while SpadeTool/MacheteTool implement
            # use_primary() and have no shoot() method. Their remote swing and
            # sound are driven by WorldUpdate primary-action bit 0x01 instead.
            #
            # Packet 8 is purely presentational (the shot is resolved here),
            # so it rides sequenced-unreliable: automatic fire must never
            # head-of-line stall reliable gameplay state behind it.
            self.server.broadcast(
                bytes(feedback.generate()), exclude=player, reliable=False
            )

        stimuli = getattr(self.server, "bot_stimuli", None)
        if stimuli is not None:
            from server.bot_ai.messages import StimulusKind

            stimuli.publish(
                StimulusKind.SHOT,
                (float(packet.x), float(packet.y), float(packet.z)),
                source_id=int(player.id),
                team=int(player.team),
                radius=72.0,
                lifetime=1.25,
            )

        # Use the CLIENT's reported shot origin + direction for hit resolution
        # (already sanity-validated). The server's own eye/orientation lags the
        # client by the reconciliation delay, so raycasting from it destroys a
        # DIFFERENT cell than the player's crosshair (blocks vanishing offset /
        # underground) and draws tracers in the wrong place. The client knows
        # exactly where it fired from.
        origin = (float(packet.x), float(packet.y), float(packet.z))
        direction = self._normalize((packet.ori_x, packet.ori_y, packet.ori_z))
        if direction is None:
            direction = player.orientation
            origin = player.eye

        # Lag compensation: player hits are tested against where targets
        # were on the shooter's screen (server/lag_compensation.py).
        from server import lag_compensation

        with lag_compensation.compensated(self, self.server, player, packet):
            try:
                if player.is_spade_tool():
                    # MEASURED: the 1.x client digs terrain with the spade/pick by
                    # sending a ShootPacket (id 6), NOT BlockLiberate. The spade
                    # shot must destroy the block it points at, not only
                    # melee-hit players.
                    dug_terrain = self._resolve_spade_dig(
                        player, origin, direction, packet,
                        reach_eyes=reach_eyes, reach_slack=reach_slack,
                    )
                    hit_player = self._resolve_melee_hit(
                        player, origin, direction,
                        reach_eyes=reach_eyes, reach_slack=reach_slack,
                    )
                    return dug_terrain or hit_player

                # Character.shoot sends ONE central (unspread) ShootPacket for
                # the trigger. It seeds Python's RNG from packet.seed and
                # expands ``weapon.pellets`` directions locally -- INCLUDING
                # pellets == 1 (rifle, SMG, pistol, ...: one seeded direction,
                # three draws); observers repeat that expansion from the
                # relayed packet. Resolve the same directions here so
                # authoritative damage lands where every client drew/predicted
                # the shot, not exactly on the crosshair (P2-18). The seed must
                # therefore stay client-chosen; skew is detected.
                self._observe_pellet_seed(player, packet)
                hit_any = False
                for pellet_direction in self._seeded_pellet_directions(
                    player, direction, profile, packet, now
                ):
                    if self._resolve_hitscan(player, pellet_direction, origin):
                        hit_any = True
                tally = self._shot_tally
                if tally is not None and not tally.get("hit"):
                    self._log_near_miss(player, packet, origin, direction, profile)
                return hit_any
            finally:
                self._end_shot_tally(player)

    # ------------------------------------------------------------------
    # Anti-cheat accounting
    # ------------------------------------------------------------------

    def _reject(self, player, kind: str, **detail) -> None:
        """Report an enforced rejection and count it in the aggregates."""

        from server import anticheat

        anticheat.report(self.server, player, kind, enforced=True, **detail)
        if not getattr(player, "is_bot", False):
            anticheat_stats(player)["rejected"][kind] += 1

    # ------------------------------------------------------------------
    # Shot diagnostics (INFO, rate-limited)
    # ------------------------------------------------------------------

    def _shot_log_allowed(self, player) -> tuple[bool, int]:
        """Per-player log window; returns (allowed, lines suppressed before)."""

        if getattr(player, "is_bot", False):
            return False, 0
        now = _shot_log_clock()
        state = getattr(player, "_shot_log_window", None)
        suppressed = 0
        if not isinstance(state, list) or now - state[0] > SHOT_LOG_WINDOW_SECONDS:
            suppressed = int(state[2]) if isinstance(state, list) else 0
            state = [now, 0, 0]
            try:
                player._shot_log_window = state
            except AttributeError:
                return False, 0
        if state[1] >= SHOT_LOG_BURST:
            state[2] += 1
            return False, 0
        state[1] += 1
        return True, suppressed

    def _intended_target(self, player, origin, direction):
        """(enemy, distance along the ray, miss distance) nearest the ray.

        Uses the lag-compensated body when a rewind is installed, else the
        live body; the reference point is the torso centre (0.75 below the
        eye). Purely diagnostic.
        """

        try:
            origin = tuple(float(v) for v in origin)
            direction = tuple(float(v) for v in direction)
        except (TypeError, ValueError):
            return None
        from server import lag_compensation

        best = None
        for target in tuple(self.server.players.values()):
            if target is player or not target.alive or not target.spawned:
                continue
            if (
                not getattr(self.server.config, "friendly_fire", False)
                and target.team == player.team
            ):
                continue
            body = lag_compensation.body_for(self, target)
            centre = (float(body.x), float(body.y), float(body.z) + 0.75)
            rel = tuple(centre[i] - origin[i] for i in range(3))
            along = sum(rel[i] * direction[i] for i in range(3))
            if along <= 0.0:
                continue
            miss = math.sqrt(max(0.0, sum(c * c for c in rel) - along * along))
            if best is None or miss < best[2]:
                best = (target, along, miss)
        return best

    def _shot_context(self, player, packet) -> dict:
        context = {
            "tool": int(getattr(player, "tool", -1)),
            "loop": int(getattr(packet, "loop_count", 0) or 0),
        }
        try:
            origin = (float(packet.x), float(packet.y), float(packet.z))
            direction = self._normalize((packet.ori_x, packet.ori_y, packet.ori_z))
        except (AttributeError, TypeError, ValueError):
            return context
        if direction is None or not self._finite(*origin):
            return context
        aimed = self._intended_target(player, origin, direction)
        if aimed is not None:
            target, along, miss = aimed
            context["target"] = repr(getattr(target, "name", "?"))
            context["distance"] = round(along, 1)
            context["miss_by"] = round(miss, 2)
        peer = getattr(getattr(player, "connection", None), "peer", None)
        rtt = getattr(peer, "roundTripTime", None)
        if rtt is not None:
            context["rtt_ms"] = rtt
        return context

    def _log_shot_drop(self, player, packet, reason: str, **detail) -> None:
        """INFO line for a shot that was never resolved, with its reason."""

        try:
            allowed, suppressed = self._shot_log_allowed(player)
            if not allowed:
                return
            context = self._shot_context(player, packet)
            context.update(detail)
            if suppressed:
                context["suppressed_before"] = suppressed
            shot_logger.info(
                "shot dropped player=%s name=%r reason=%s %s",
                getattr(player, "id", "?"), getattr(player, "name", ""), reason,
                " ".join(f"{key}={value}" for key, value in context.items()),
            )
        except Exception:  # noqa: BLE001 - diagnostics must not break combat
            logger.debug("shot drop logging failed", exc_info=True)

    def _log_cadence_drop(self, player, packet, now: float) -> None:
        """Classify a consume_shot refusal: reload, empty magazine, or rate."""

        detail = {
            "clip": int(getattr(player, "ammo_clip", 0)),
            "reserve": int(getattr(player, "ammo_reserve", 0)),
        }
        if bool(getattr(player, "reloading", False)):
            reason = "reloading"
            detail["reload_left"] = round(
                float(getattr(player, "reload_end_time", 0.0)) - float(now), 3
            )
        elif int(getattr(player, "ammo_clip", 0)) <= 0:
            reason = "empty_clip"
        else:
            reason = "fire_rate"
            lanes = getattr(player, "_action_lanes", None)
            lane = lanes.get("fire") if isinstance(lanes, dict) else None
            if lane is not None:
                if lane.label is not None:
                    detail["label_gap"] = (
                        int(getattr(packet, "loop_count", 0) or 0) - int(lane.label)
                    )
                detail["due_in"] = round(float(lane.due) - float(now), 3)
                detail["tokens"] = round(float(lane.tokens), 2)
        self._log_shot_drop(player, packet, reason, **detail)

    def _log_near_miss(self, player, packet, origin, direction, profile) -> None:
        """INFO line when a slow single-pellet gun narrowly missed an enemy."""

        try:
            if getattr(player, "is_bot", False) or player.is_spade_tool():
                return
            if float(profile.fire_interval) < NEAR_MISS_MIN_INTERVAL:
                return
            if int(getattr(profile, "pellet_count", 1)) != 1:
                return
            aimed = self._intended_target(player, origin, direction)
            if aimed is None or aimed[2] > NEAR_MISS_DISTANCE:
                return
            allowed, suppressed = self._shot_log_allowed(player)
            if not allowed:
                return
            target, along, miss = aimed
            rewind = getattr(self, "_lag_rewind", None)
            zoom_for = getattr(player, "zoom_for_action", None)
            zoom = (
                bool(zoom_for(getattr(packet, "loop_count", None)))
                if callable(zoom_for) else None
            )
            peer = getattr(getattr(player, "connection", None), "peer", None)
            shot_logger.info(
                "shot missed player=%s name=%r tool=%s target=%r distance=%.1f "
                "miss_by=%.2f rewind_ms=%s zoom=%s rtt_ms=%s%s",
                getattr(player, "id", "?"), getattr(player, "name", ""),
                int(getattr(player, "tool", -1)), getattr(target, "name", "?"),
                along, miss,
                None if rewind is None else round(float(rewind.rewind_ms), 1),
                zoom, getattr(peer, "roundTripTime", None),
                f" suppressed_before={suppressed}" if suppressed else "",
            )
        except Exception:  # noqa: BLE001 - diagnostics must not break combat
            logger.debug("near-miss logging failed", exc_info=True)

    def _begin_shot_tally(self, player) -> None:
        self._shot_tally = {
            "player": player,
            "hit": False,
            "headshot": False,
            "pellet_hits": 0,
        }

    def _note_player_hit(self, attacker, headshot: bool) -> None:
        tally = getattr(self, "_shot_tally", None)
        if tally is None or tally.get("player") is not attacker:
            return
        tally["hit"] = True
        tally["headshot"] = tally["headshot"] or bool(headshot)
        tally["pellet_hits"] += 1

    def _end_shot_tally(self, player) -> None:
        """Fold one accepted trigger pull into the player's aggregates."""

        tally = getattr(self, "_shot_tally", None)
        self._shot_tally = None
        if tally is None or tally.get("player") is not player:
            return
        if getattr(player, "is_bot", False):
            return
        stats = anticheat_stats(player)
        tool = int(getattr(player, "tool", -1))
        weapon = stats["weapons"].setdefault(
            tool, {"shots": 0, "hits": 0, "headshots": 0}
        )
        stats["shots"] += 1
        weapon["shots"] += 1
        if tally["hit"]:
            stats["hits"] += 1
            weapon["hits"] += 1
        if tally["headshot"]:
            stats["headshots"] += 1
            weapon["headshots"] += 1
        stats["pellet_hits"] += int(tally["pellet_hits"])

    def _observe_pellet_seed(self, player, packet) -> None:
        """Track the client-chosen 8-bit pellet seed for skew detection.

        Retail expands the pellet cloud from ``ShootPacket.seed`` locally for
        the shooter's own tracers/decals and on every observer through
        ShootFeedback(8), so the server must resolve the same seed or damage
        would disagree with what every client drew. A modified client can
        pick the tightest pattern; that shows as a non-uniform histogram.
        """

        if getattr(player, "is_bot", False):
            return
        seeds = anticheat_stats(player)["pellet_seeds"]
        seed = int(getattr(packet, "seed", 0)) & 0xFF
        seeds[seed] += 1
        total = sum(seeds.values())
        if total < SEED_SKEW_MIN_SHOTS:
            return
        top = seeds.most_common(1)[0][1]
        if top >= max(SEED_SKEW_MIN_REPEATS, SEED_SKEW_SHARE * total):
            from server import anticheat

            anticheat.report(
                self.server, player, "pellet_seed_skew", enforced=False,
                shots=total, top_seed_count=top,
                chi_square=round(seed_chi_square(seeds), 1),
            )

    def _accept_assault_burst_packet(self, player, packet, now: float) -> bool:
        """Accept the stock three-round burst at 0.1s internal spacing."""
        from server import action_clock

        loop_count = int(getattr(packet, "loop_count", 0))
        burst = self._assault_bursts.get(player.id)
        labelled_continuation = bool(
            burst is not None
            and action_clock.label_plausible(player, loop_count)
            and burst.get("first_loop") is not None
            and 0 < loop_count - burst["first_loop"]
            <= ASSAULT_BURST_LOOP_INTERVAL * (ASSAULT_BURST_SIZE - 1)
        )
        if (
            burst is not None
            and burst["count"] < ASSAULT_BURST_SIZE
            and loop_count - burst["last_loop"] >= ASSAULT_BURST_LOOP_INTERVAL
            and (now - burst["started_at"] <= ASSAULT_BURST_WINDOW
                 or labelled_continuation)
        ):
            if player.ammo_clip <= 0 or player.reloading:
                return False
            player.ammo_clip -= 1
            burst["count"] += 1
            burst["last_loop"] = loop_count
            note_floor = getattr(player, "_note_auto_reload_floor", None)
            if callable(note_floor):
                note_floor(
                    loop_count, float(player.get_weapon_profile().fire_interval)
                )
            return True

        if not player.consume_shot(now, loop=loop_count):
            return False
        self._assault_bursts[player.id] = {
            "count": 1,
            "last_loop": loop_count,
            "first_loop": loop_count,
            "started_at": now,
        }
        return True

    def _accept_minigun_packet(self, player, now: float, *, loop=None) -> bool:
        """Mirror the stock MinigunWeapon.update spin model.

        Stock: while primary OR secondary is held (and not reloading) the
        shoot interval moves by -0.15/s toward the 0.10 cap; otherwise by
        +0.075/s back toward 0.30 -- a gradual spin-down, never a reset.
        Secondary alone spins the barrels without firing, so a pre-spun gun
        legitimately fires its first rounds at 0.10 s.

        Between packets the server only knows the trigger state at arrival:
        during a continuous burst (gap within the current interval plus slack)
        the gun was held; across a longer gap it spun down unless a trigger is
        held now (pre-spin), in which case the stock cap is granted.
        """
        run = self._minigun_runs.get(player.id)
        user_input = getattr(player, "input", None)
        pre_spun = bool(getattr(user_input, "secondary_fire", False))
        still_firing = bool(getattr(user_input, "primary_fire", False))
        if run is None:
            interval = MINIGUN_INTERVAL_MIN if pre_spun else MINIGUN_FIRST_SHOT_INTERVAL
        else:
            gap = max(0.0, now - run["last_packet_at"])
            from server import action_clock

            if action_clock.label_plausible(player, loop) and run.get("loop") is not None:
                label_gap = (int(loop) - int(run["loop"])) / action_clock.TICK_RATE
                if 0.0 <= label_gap <= action_clock.link_stall_seconds(player) + gap:
                    gap = label_gap
            interval = float(run["interval"])
            # Packet stalls bunch shot arrivals; while the trigger is still
            # down a gap is treated as held so no legitimate round is refused.
            if gap <= interval + MINIGUN_CONTINUITY_SLACK or (
                still_firing and gap <= MINIGUN_HELD_GAP_LIMIT
            ):
                interval -= MINIGUN_INTERVAL_RAMP_PER_SECOND * gap
            elif pre_spun:
                interval = MINIGUN_INTERVAL_MIN
            else:
                interval = min(
                    MINIGUN_FIRST_SHOT_INTERVAL,
                    interval + MINIGUN_INTERVAL_RECOVER_PER_SECOND * gap,
                )
        interval = min(MINIGUN_INTERVAL_INITIAL,
                       max(MINIGUN_INTERVAL_MIN, interval))
        # The stock gun keeps spinning up while held, so the NEXT round is
        # due once the elapsed hold equals the interval at that moment:
        # g = interval - 0.15 * g.
        next_gap = max(
            MINIGUN_INTERVAL_MIN,
            interval / (1.0 + MINIGUN_INTERVAL_RAMP_PER_SECOND),
        )
        if not player.consume_shot(now, fire_interval=next_gap, loop=loop):
            return False
        self._minigun_runs[player.id] = {
            "last_packet_at": now,
            "interval": interval,
            "loop": loop,
        }
        return True

    def _seeded_pellet_directions(
        self, player, direction, profile, packet, now: float
    ):
        """Reproduce retail ``Character.shoot`` pellet expansion.

        IDA recovery of ``character.pyd:sub_10049DB0`` shows three RNG draws
        per pellet, for every hit-scan gun (``Weapon.pellets`` defaults to 1,
        so a rifle/SMG/pistol shot is ONE seeded direction, never the raw
        crosshair).  Hip fire adds ``(random()*4-2)*accuracy`` to each axis;
        zoom adds ``(random()*2-1)*accuracy`` with ``accuracy_zoom`` when the
        class defines one (both snipers: 0.0).  ``accuracy`` is the class
        value, or for ``variable_accuracy`` guns ``Weapon.prep_shoot``'s
        lerp(accuracy_min, accuracy_max) over the ``accuracy_spread`` bloom
        reached BEFORE this round's ``shot_weapon`` increase; the bloom
        recovers at ``accuracy_spread_reduction_speed`` per second and resets
        on a weapon switch (``on_unset``).  Mirrors the native client's
        ``observe_hitscan_bloom`` / ``replicated_hitscan_pellets``.
        """
        state = self._pellet_spread.get(player.id)
        tool = int(player.tool)
        accuracy = float(profile.spread)
        curve = RETAIL_ACCURACY_SPREAD.get(tool)
        spread = 0.0
        if curve is not None:
            spread_min, spread_max, increase, reduction = curve
            if state is None or state.get("tool") != tool:
                spread = spread_min
            else:
                elapsed = max(0.0, now - float(state["last_at"]))
                # The client's bloom decays on its own frame clock: measure
                # the gap by labels when both shots carry plausible ones, so
                # jitter or a retransmission burst does not bloom the
                # server's cone wider than the one the client drew.
                label_gap = self._shot_label_gap(player, state.get("label"), packet)
                if label_gap is not None:
                    elapsed = label_gap
                spread = max(spread_min, float(state["spread"]) - elapsed * reduction)
            ratio = 0.0
            if spread_max > spread_min:
                ratio = min(1.0, max(0.0, (spread - spread_min) / (spread_max - spread_min)))
            accuracy = float(profile.accuracy_min) + ratio * (
                float(profile.accuracy_max) - float(profile.accuracy_min)
            )
            spread = min(spread_max, spread + increase)
        self._pellet_spread[player.id] = {
            "tool": tool,
            "spread": spread,
            "last_at": now,
            "label": getattr(packet, "loop_count", None),
        }

        # The zoom of the shot's OWN client frame (Character.shoot reads
        # ``self.zoom`` before the shot can drop it). The newest ClientData
        # may already carry the post-shot state: Character.reload cancels a
        # sniper's zoom one frame later, and an unsequenced ClientData
        # overtakes a retransmitted reliable ShootPacket. Using it turned
        # zoomed sniper shots (accuracy_zoom 0) into hip shots with up to
        # +/-0.05 rad of spread per axis: clean misses at range.
        zoom_for_action = getattr(player, "zoom_for_action", None)
        if callable(zoom_for_action):
            zoomed = bool(zoom_for_action(getattr(packet, "loop_count", None)))
        else:
            zoomed = bool(getattr(getattr(player, "input", None), "zoom", False))
        if zoomed and tool in RETAIL_ACCURACY_ZOOM:
            accuracy = RETAIL_ACCURACY_ZOOM[tool]
        scale = 2.0 if zoomed else 4.0
        center = 1.0 if zoomed else 2.0
        rng = random.Random(int(getattr(packet, "seed", 0)) & 0xFF)
        pellets = []
        for _ in range(int(profile.pellet_count)):
            pellet = self._normalize((
                direction[0] + (rng.random() * scale - center) * accuracy,
                direction[1] + (rng.random() * scale - center) * accuracy,
                direction[2] + (rng.random() * scale - center) * accuracy,
            ))
            if pellet is not None:
                pellets.append(pellet)
        return pellets

    @staticmethod
    def _shot_label_gap(player, previous_label, packet):
        """Seconds between two shots by client frame labels, or None."""

        if previous_label is None or getattr(player, "is_bot", False):
            return None
        from server import action_clock

        label = getattr(packet, "loop_count", None)
        if not action_clock.label_plausible(player, label):
            return None
        try:
            gap = int(label) - int(previous_label)
        except (TypeError, ValueError, OverflowError):
            return None
        if gap < 0 or gap > 10 * int(action_clock.TICK_RATE):
            return None
        return gap / float(action_clock.TICK_RATE)

    def handle_weapon_reload(self, player) -> bool:
        if not player.start_reload():
            return False
        player._broadcast_reload_state(False)
        return True

    # The CLIENT refuses to place a block that touches nothing: its gate is
    # `map.has_neighbors(x, y, z, 1)` — FACE adjacency (the 6 axis neighbours),
    # not diagonal — plus `get_max_modifiable_z() == 238`. Live-measured on the
    # real client 2026-07-10: directly above the surface -> True, any gap -> False.
    #
    # Our world_manager.can_build only checks bounds/solidity, so the server used
    # to accept FLOATING placements the client silently drops. The server then
    # held blocks no client had: builds "didn't appear", the builder lost
    # inventory for nothing, and the server carried collision where every client
    # saw air (a server-side "invisible wall"). Layer 0 is additionally reserved
    # as sky: standing on a z=0 block makes the stock movement contact solver
    # rise and alternate airborne/grounded forever.
    _NEIGHBOR_OFFSETS = ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1))
    # Retain the old public class attribute for plugins/embedders.
    MAX_MODIFIABLE_Z = MAX_BUILD_Z

    def _block_supported(self, x: int, y: int, z: int, pending=()) -> bool:
        """Client-parity placement gate: the cell must FACE-touch an existing
        solid, or a cell committed earlier in this same line (the client's
        add_block loop walks the generated points in order, so a later cell may
        rest on an earlier one)."""
        if not build_z_is_safe(z):
            return False
        wm = self.server.world_manager
        for dx, dy, dz in self._NEIGHBOR_OFFSETS:
            neighbor = (x + dx, y + dy, z + dz)
            if neighbor in pending or wm.get_solid(*neighbor):
                return True
        return False

    @staticmethod
    def _ordinary_block_tool_selected(player) -> bool:
        """Return whether the retail normal block tool (id 5) is held.

        Normal and flare blocks share palette/UI behavior, so
        ``Player.is_block_tool()`` intentionally recognizes both ids.  Their
        action packets are not interchangeable, however: tool 5 sends
        BlockBuild/BlockLine while flare tool 22 sends PlaceFlareBlock(104).
        Keep the action gate exact so a delayed or forged packet cannot spend
        the wrong inventory amount or create the wrong world representation.
        """

        return (
            bool(getattr(player, "tool_is_raw", False))
            and int(getattr(player, "tool", -1)) == int(C.BLOCK_TOOL)
        )

    def handle_block_build(self, player, packet) -> bool:
        from server.game_rules import get_rules
        if (
            not player.alive
            or not player.spawned
            or not self._ordinary_block_tool_selected(player)
            or not get_rules(self.server.config).enabled("RULE_ENABLE_BLOCKS")
        ):
            return False

        try:
            x, y, z = int(packet.x), int(packet.y), int(packet.z)
        except (AttributeError, TypeError, ValueError, OverflowError):
            return False
        position = (x, y, z)
        # Retail reach (MAX_BLOCK_DISTANCE from the eye) plus drift slack.
        # A forged far placement is dropped without a repair: no stock client
        # predicted a ghost there.
        if not self._cell_in_map(position) or not self._cell_within_reach(
            player, position, self._build_reach()
        ):
            return False
        if not self._cell_in_sight(
            player, position, loop=getattr(packet, "loop_count", None),
            kind="build_occluded",
        ):
            self._queue_canonical_terrain_repair((position,))
            return False
        if not self.server.world_manager.can_build(x, y, z):
            self._queue_canonical_terrain_repair((position,))
            return False
        if not self._block_supported(x, y, z):
            self._queue_canonical_terrain_repair((position,))
            return False
        # Retail BlockTool requires a positive wallet even when the team
        # has infinite blocks; that flag waives cost, not this eligibility.
        if int(player.blocks) <= 0:
            self._queue_canonical_terrain_repair((position,))
            return False
        if not self._block_interval_ok(
            player, (position,), loop=getattr(packet, "loop_count", None)
        ):
            return False
        if not player.remove_block():
            self._queue_canonical_terrain_repair((position,))
            return False

        action_loop = max(0, int(packet.loop_count))
        color = int(player.block_color) & 0xFFFFFF
        service = getattr(self.server, "world_mutations", None)
        if service is not None:
            mutation = PendingWorldMutation(
                owner_id=int(player.id),
                action_loop=action_loop,
                enqueued_tick=int(self.server.loop_count),
                kind="block_build",
                cell_count=1,
                apply=lambda: self._commit_block_build(
                    player, action_loop, position, color
                ),
                cancel=lambda: self._cancel_reserved_block_build(
                    player, (position,)
                ),
            )
            return bool(service.enqueue(mutation))

        self._commit_block_build(player, action_loop, position, color)
        return True

    def _build_reach(self) -> float:
        """Server build reach: the retail block distance plus drift slack."""
        mode = getattr(self.server, "mode", None)
        if str(getattr(mode, "mode_code", "")).lower() == "cctf":
            return CLASSIC_BUILD_REACH
        return BUILD_REACH

    def _block_interval_ok(self, player, cells, *, loop=None) -> bool:
        """Stock MIN_BLOCK_INTERVAL between one player's accepted builds.

        ``[anticheat] enforce_block_interval`` (default off) rejects a too-early
        build with a canonical repair; log-only otherwise. Bots always pace
        themselves (BotActionGateway) so they never trip it.
        """
        from server import action_clock, anticheat

        now = time.monotonic()
        last = getattr(player, "_last_block_build_at", None)
        if not getattr(player, "is_bot", False):
            permitted = action_clock.admit(
                player, "build", label=loop, interval=MIN_BLOCK_INTERVAL,
                now=now, grace=MIN_BLOCK_INTERVAL_GRACE,
            )
        else:
            permitted = last is None or now - float(last) >= (
                MIN_BLOCK_INTERVAL - MIN_BLOCK_INTERVAL_GRACE
            )
        if not permitted:
            enforce = anticheat.enforcing(self.server, "enforce_block_interval")
            anticheat.report(
                self.server, player, "block_interval", enforced=enforce,
                gap=round(now - float(last), 3) if last is not None else 0.0,
            )
            if enforce:
                self._queue_canonical_terrain_repair(list(cells))
                return False
        player._last_block_build_at = now
        return True

    def _commit_block_build(self, player, action_loop, position, color) -> None:
        """Commit a reserved single-block build after its movement frame."""

        if self._build_overlaps_player(player, (position,)):
            self._cancel_reserved_block_build(player, (position,))
            return
        x, y, z = position
        wm = self.server.world_manager
        # Another ready mutation can win the cell before this one. Never charge
        # inventory for a build the authoritative map did not accept.
        if not wm.can_build(x, y, z) or not self._block_supported(x, y, z):
            player.add_blocks(1)
            self._queue_canonical_terrain_repair((position,))
            return
        color = self._commit_color(player, color)
        if not wm.set_block(x, y, z, True, color, health=USER_BLOCK_HEALTH):
            player.add_blocks(1)
            self._queue_canonical_terrain_repair((position,))
            return
        self._announce_block_build(player, position, color, action_loop)
        from server.profile_stats import add
        add(player, C.MAP_SINGLEBLOCKS_ADDED_TOTAL)
        from server.combat_scores import record_blocks_placed
        record_blocks_placed(self.server, player, (position,))
        self._queue_blocks_built(player, (position,))
        # BlockTool sends the line but does not play BUILD_SOUND on success.
        # Include the actor so a solo builder receives the authoritative cue.
        play_sound(
            self.server,
            SND_BUILD,
            position=position,
            reliable=False,
            source=player,
        )

    # Longest line the server will accept. The client regenerates the cells
    # from the echoed ENDPOINTS with its own generator, so the server must
    # never truncate/sparsify a line (server cells != client cells); overlong
    # lines are rejected whole instead.
    BLOCK_LINE_MAX_CELLS = 64

    def handle_block_line(self, player, packet) -> bool:
        """BlockLine (id 40) — how the 1.x client actually PLACES blocks (it
        never emits BlockBuild/id 32). ATOMIC: the whole line builds or none
        of it does. Accepted cells are announced with explicit RGB so remote
        rendering cannot depend on mutable character palette state.

        IDA ground truth (docs/REPLICATION_IDA_FINDINGS.md): the client's
        native remote-placement path is process_packet_block_line
        (sub_1018D690) — it regenerates the cells from the packet's endpoints
        and add_block()s each one. Plain BlockBuild(32) did not render remotely
        in live testing, while BlockBuildColored(33) is the proven prefab path.
        Explicit colored cells also avoid server SetColor packets changing the
        local player's held-block selection.
        """
        from server.game_rules import get_rules
        if (
            not player.alive
            or not player.spawned
            or not self._ordinary_block_tool_selected(player)
            or not get_rules(self.server.config).enabled("RULE_ENABLE_BLOCKS")
        ):
            return False

        try:
            x1, y1, z1 = int(packet.x1), int(packet.y1), int(packet.z1)
            x2, y2, z2 = int(packet.x2), int(packet.y2), int(packet.z2)
        except (AttributeError, TypeError, ValueError, OverflowError):
            return False
        start, end = (x1, y1, z1), (x2, y2, z2)
        # Reject before expansion: a face-connected cube_line has exactly
        # |dx|+|dy|+|dz|+1 cells, and signed-short endpoints would otherwise
        # expand ~196k cells (tens of ms) per packet before the cap applied.
        if not self._cell_in_map(start) or not self._cell_in_map(end):
            return False
        span = abs(x2 - x1) + abs(y2 - y1) + abs(z2 - z1) + 1
        if span > self.BLOCK_LINE_MAX_CELLS:
            return False
        # BlockTool sends (drag start, current hit cube). The current hit is
        # limited to the retail reach; the drag start may trail behind a
        # moving builder but is still bounded by the line length.
        near = min(
            self._eye_distance(player, start), self._eye_distance(player, end)
        )
        far = max(
            self._eye_distance(player, start), self._eye_distance(player, end)
        )
        reach = self._build_reach()
        if near > reach or far > reach + self.BLOCK_LINE_MAX_CELLS:
            return False
        # The current hit cube is visible to the builder; the drag start may
        # have scrolled out of view while moving, so either endpoint counts.
        loop = getattr(packet, "loop_count", None)
        if not getattr(player, "is_bot", False):
            _, eyes = reference_eyes(player, loop)
            world = self.server.world_manager
            if not eyes or not (
                cell_visible(world, eyes, start) or cell_visible(world, eyes, end)
            ):
                self._reject(player, "build_occluded", cell=end)
                self._queue_canonical_terrain_repair(
                    [c for c in self.block_line_cells(start, end)
                     if not world.get_solid(*c)][: self.BLOCK_LINE_MAX_CELLS]
                )
                return False

        # The remote client regenerates this packet through world.cube_line,
        # whose face-connected path is different from VXL.block_line's rounded
        # max-axis interpolation. A single tap has equal endpoints -> 1 cell.
        cells = self.block_line_cells((x1, y1, z1), (x2, y2, z2))
        if not cells or len(cells) > self.BLOCK_LINE_MAX_CELLS:
            return False
        # The stock client removes already-solid cells from its preview/cost,
        # but sends only the original endpoints. Filter them identically;
        # rejecting the whole line loses valid player placements whenever a
        # drag crosses terrain or another just-built voxel.
        build_cells = [cell for cell in cells if not self.server.world_manager.get_solid(*cell)]
        # TeamInfiniteBlocks(82) waives cost, but retail BlockTool still
        # requires at least one block in the wallet.
        from server.hud_packets import team_infinite_blocks

        infinite = team_infinite_blocks(self.server, getattr(player, "team", -1))
        if (not build_cells or int(player.blocks) <= 0
                or (not infinite and player.blocks < len(build_cells))):
            if build_cells:
                self._queue_canonical_terrain_repair(build_cells)
            return False
        pending: set[tuple[int, int, int]] = set()
        for cell in build_cells:
            if not self.server.world_manager.can_build(*cell):
                self._queue_canonical_terrain_repair(build_cells)
                return False
            if not self._block_supported(*cell, pending=pending):
                self._queue_canonical_terrain_repair(build_cells)
                return False
            pending.add(cell)
        if not self._block_interval_ok(
            player, build_cells, loop=getattr(packet, "loop_count", None)
        ):
            return False

        # Reserve inventory now, but do not mutate collision geometry during
        # packet draining.  The retail client recorded movement through
        # packet.loop_count before its echoed BlockLine can commit the ghost
        # voxels.  Production therefore commits after authoritative physics
        # consumes that same loop; otherwise build -> run/jump replays the old
        # movement frame against a newer map and visibly rolls the player back.
        cost = 0 if infinite else len(build_cells)
        player.blocks -= cost
        action_loop = max(0, int(packet.loop_count))
        cells_snapshot = tuple(build_cells)
        endpoints = (x1, y1, z1, x2, y2, z2)
        color = int(player.block_color) & 0xFFFFFF
        service = getattr(self.server, "world_mutations", None)
        if service is not None:
            mutation = PendingWorldMutation(
                owner_id=int(player.id),
                action_loop=action_loop,
                enqueued_tick=int(self.server.loop_count),
                kind="block_line",
                cell_count=cost,
                apply=lambda: self._commit_block_line(
                    player,
                    action_loop,
                    endpoints,
                    cells_snapshot,
                    color,
                ),
                cancel=lambda: self._cancel_reserved_block_build(
                    player, cells_snapshot
                ),
            )
            return bool(service.enqueue(mutation))

        # Compatibility path for focused domain tests and embedders that do
        # not construct the production service composition root.
        self._commit_block_line(
            player,
            action_loop,
            endpoints,
            cells_snapshot,
            color,
        )
        return True

    def _commit_block_line(
        self,
        player,
        action_loop: int,
        endpoints: tuple[int, int, int, int, int, int],
        build_cells: tuple[tuple[int, int, int], ...],
        color: int,
    ) -> None:
        """Commit one validated BlockLine on the post-physics tick boundary."""

        if self._build_overlaps_player(player, build_cells):
            self._cancel_reserved_block_build(player, build_cells)
            return
        color = self._commit_color(player, color)
        failed_cells = []
        for x, y, z in build_cells:
            if not self.server.world_manager.set_block(
                x, y, z, True, color, health=USER_BLOCK_HEALTH
            ):
                failed_cells.append((x, y, z))
        if failed_cells:
            player.add_blocks(len(failed_cells))
            self._queue_canonical_terrain_repair(failed_cells)

        # The builder does NOT commit its ghost blocks locally; it needs the
        # native BlockLine echo to finalize the drag and wallet update. Other
        # clients get explicit-color cells so their rendering is independent
        # of mutable palette state.  Preserve the originating action loop: it
        # is the only timeline label known to exist in the retail history.
        x1, y1, z1, x2, y2, z2 = endpoints
        own_echo = BlockLine()
        own_echo.loop_count = action_loop
        own_echo.player_id = player.id
        own_echo.x1, own_echo.y1, own_echo.z1 = x1, y1, z1
        own_echo.x2, own_echo.y2, own_echo.z2 = x2, y2, z2
        player.send(bytes(own_echo.generate()), reliable=True)

        successful_cells = [
            cell for cell in build_cells if cell not in failed_cells
        ]
        # Only committed cells are announced (and journaled for MapSync
        # joiners); echoing a rejected cell would leave ghost blocks on every
        # observer and every late joiner.
        for x, y, z in successful_cells:
            echo = BlockBuildColored()
            echo.loop_count = action_loop
            echo.player_id = player.id
            echo.x, echo.y, echo.z = x, y, z
            echo.color = color
            self.server.broadcast(bytes(echo.generate()), exclude=player)
        self._send_observer_block_health(player, successful_cells)
        self._send_owner_color_correction(
            player, successful_cells, color, action_loop
        )
        if successful_cells:
            from server.profile_stats import add
            add(player, C.MAP_SINGLEBLOCKS_ADDED_TOTAL, len(successful_cells))
            from server.combat_scores import record_blocks_placed
            record_blocks_placed(self.server, player, successful_cells)
            play_sound(
                self.server,
                SND_BUILD,
                position=successful_cells[0],
                reliable=False,
                source=player,
            )
            self._queue_blocks_built(player, successful_cells)

    @staticmethod
    def _commit_color(player, fallback) -> int:
        """Return the builder's newest known palette colour as ``0xRRGGBB``.

        The stock builder does not colour its own cells when it sends
        BlockLine; it colours them from ``Character.block_color`` when the
        server's echo ARRIVES. A SetColor sent after the BlockLine (same
        frame, or while the build waited for its movement frame) is therefore
        already part of the owner's colour, so the commit reads the latest
        committed palette instead of the one seen when the request drained.
        """

        try:
            return pack_rgb(player.block_color)
        except (AttributeError, TypeError, ValueError):
            return pack_rgb(fallback)

    def _send_owner_color_correction(
        self, player, cells, color: int, action_loop: int
    ) -> None:
        """Pin the builder's echoed cells to the authoritative colour.

        Live-measured on the stock client: the builder's echo (BlockLine 40,
        BlockBuild 32) paints with the palette held when the echo arrives, so
        a colour change during the round trip left the owner with a different
        colour than the VXL, observers and late joiners. BlockBuildColored(33)
        cannot fix an already-solid cell (the client ignores it), but
        PaintBlock(7) recolours exactly and is a no-op on air. Sent after the
        echo on the same reliable stream, it always lands afterwards.
        """

        send = getattr(player, "send", None)
        if not cells or not callable(send):
            return
        rgb = unpack_rgb(color)
        for x, y, z in cells:
            paint = PaintBlockPacket()
            paint.loop_count = max(0, int(action_loop))
            paint.x, paint.y, paint.z = int(x), int(y), int(z)
            paint.color = rgb
            send(bytes(paint.generate()), reliable=True)

    def _announce_block_build(
        self, player, position, color: int, action_loop: int
    ) -> None:
        """Replicate one committed single-block build with explicit RGB.

        BlockBuild(32) carries no colour: every receiver paints it with its
        own idea of the builder's palette, which lags behind the throttled
        SetColor relay (and never matches for a joiner that missed it). The
        builder keeps its native id-32 echo plus the colour pin; everyone
        else gets BlockBuildColored(33), exactly like BlockLine observers.
        """

        packet = BlockBuild()
        packet.loop_count = int(action_loop)
        packet.player_id = player.id
        packet.x, packet.y, packet.z = position
        packet.block_type = 0  # material selector: 0 = normal build
        send = getattr(player, "send", None)
        if callable(send):
            send(bytes(packet.generate()), reliable=True)
        observer = BlockBuildColored()
        observer.loop_count = int(action_loop)
        observer.player_id = player.id
        observer.x, observer.y, observer.z = position
        observer.color = pack_rgb(color)
        self.server.broadcast(bytes(observer.generate()), exclude=player)
        self._send_observer_block_health(player, (position,))
        self._send_owner_color_correction(player, (position,), color, action_loop)

    def _send_observer_block_health(self, player, cells) -> None:
        """Give observers the builder's user-block health for new cells.

        Observers receive BlockBuildColored(33) for exact colour, but the
        stock client stores a packet-33 voxel at 3.0 health while the builder
        (BlockBuild 32 / BlockLine 40 echo) and the server hold 9.0.  One
        BlockManagerState(38) user-row table, sent right after the 33s on the
        same reliable stream, sets ``user_blocks`` to the server's health
        (live 2026-09-26: 33 -> 3.0, then 38 row -> 9.0, colour unchanged).
        Not journalled: MapSync joiners get the same rows from the reveal.
        """

        if not cells:
            return
        # Retail add_user_block pops the cell from user_blocks in the UGC
        # Map Creator (untracked, map-default health): a user row would give
        # observers an entry no stock client ever holds there.
        if bool(getattr(self.server.config, "ugc_runtime", False)):
            return
        rows_of = getattr(self.server.world_manager, "block_manager_rows", None)
        if not callable(rows_of):
            return
        user_rows, _damaged = rows_of(cells)
        if not user_rows:
            return
        from server.prefab_actions import block_state_packets
        for data in block_state_packets(user_rows):
            # Packet 38 is never journalled (only 7/32/33/37/40 are).
            self.server.broadcast(data, exclude=player)

    def _queue_blocks_built(self, player, cells) -> None:
        """Tell the mode which cells a player committed (Demolition repairs)."""
        queue = getattr(self.server, "queue_mode_event", None)
        if player is not None and cells and callable(queue):
            queue(
                "on_blocks_built",
                player,
                tuple(tuple(int(v) for v in cell) for cell in cells),
            )

    def _build_overlaps_player(self, player, cells) -> bool:
        """Recheck construction against living bodies before a commit.

        Applies to humans and bots alike: the retail client refuses to place
        a block inside any character (can_place_block_on_player), so only a
        forged or stale request can reach this with an overlapping cell. It
        runs at the post-physics commit boundary, where the authoritative
        body matches the client's action frame.
        """
        construction = getattr(self.server, "construction", None)
        return bool(construction is not None
                    and construction._overlaps_living_player(frozenset(cells)))

    # Retained name for embedders/tests written before humans were checked.
    _bot_build_overlaps_player = _build_overlaps_player

    def _eye_distance(self, player, cell) -> float:
        """Distance from the eye to a cell centre (inf on malformed state)."""

        try:
            eye = tuple(float(value) for value in player.eye)
        except (AttributeError, TypeError, ValueError):
            return math.inf
        if len(eye) != 3 or not self._finite(*eye):
            return math.inf
        return self._distance(eye, tuple(float(v) + 0.5 for v in cell))

    def block_line_cells(self, a, b):
        """Return the stock face-connected cells for public action validation."""

        return list(cube_line(*a, *b))

    def _block_line_cells(self, a, b):
        """Compatibility alias retained for reverse-engineering regressions."""

        return self.block_line_cells(a, b)

    def _paintbrush_authorized(self, player) -> bool:
        """Return whether this life owns the dedicated UGC paintbrush."""

        from server.game_rules import get_rules

        tool = int(getattr(player, "tool", -1))
        return (
            bool(getattr(self.server.config, "ugc_runtime", False))
            and bool(getattr(player, "alive", False))
            and bool(getattr(player, "spawned", False))
            and int(getattr(player, "class_id", -1))
            == int(getattr(C, "CLASS_UGCBUILDER", 13))
            and bool(getattr(player, "tool_is_raw", False))
            and tool == int(getattr(C, "PAINTBRUSH_TOOL", 43))
            and tool in {
                int(value) for value in (getattr(player, "loadout", ()) or ())
            }
            and get_rules(self.server.config).enabled("RULE_ENABLE_COLOUR_PICKER")
            and get_rules(self.server.config).is_tool_enabled(tool)
        )

    @staticmethod
    def _unpack_rgb(color) -> tuple[int, int, int]:
        """Normalize packed VXL or tuple colour values to wire RGB."""

        try:
            return unpack_rgb(color)
        except ValueError:
            return (0, 0, 0)

    def _paintbrush_surface_cells(
        self,
        center: tuple[int, int, int],
        *,
        radius: int,
    ) -> list[tuple[int, int, int]]:
        """Return a bounded exposed-surface brush around one raycast hit."""

        world = self.server.world_manager
        cx, cy, cz = center
        radius = max(0, min(4, int(radius)))
        radius_sq = radius * radius
        rows = []
        for dx in range(-radius, radius + 1):
            for dy in range(-radius, radius + 1):
                for dz in range(-radius, radius + 1):
                    distance_sq = dx * dx + dy * dy + dz * dz
                    if distance_sq > radius_sq:
                        continue
                    coordinate = (cx + dx, cy + dy, cz + dz)
                    x, y, z = coordinate
                    if not (0 <= x < 512 and 0 <= y < 512 and 0 <= z <= 238):
                        continue
                    if not world.get_solid(x, y, z):
                        continue
                    # Painting buried voxels has no visible result and turns a
                    # held RMB into a needless packet/mutation flood.  Retail's
                    # brush is a surface spray, so retain only exposed cells.
                    if radius and all(
                        world.get_solid(x + ox, y + oy, z + oz)
                        for ox, oy, oz in self._NEIGHBOR_OFFSETS
                        if (
                            0 <= x + ox < 512
                            and 0 <= y + oy < 512
                            and 0 <= z + oz <= 238
                        )
                    ):
                        continue
                    rows.append((distance_sq, coordinate))
        rows.sort(key=lambda row: (row[0], row[1]))
        return [coordinate for _distance, coordinate in rows[:128]]

    def _commit_paint(
        self,
        player,
        positions,
        color,
        *,
        loop_count: int,
    ) -> int:
        """Recolour canonical VXL cells and relay native packet 7 per cell."""

        world = self.server.world_manager
        rgb = self._unpack_rgb(color)
        changed = 0
        for x, y, z in positions:
            x, y, z = int(x), int(y), int(z)
            if not (0 <= x < 512 and 0 <= y < 512 and 0 <= z <= 238):
                continue
            if not world.get_solid(x, y, z):
                continue
            try:
                current = self._unpack_rgb(world.get_color(x, y, z))
            except (AttributeError, TypeError, ValueError):
                current = None
            # A damaged cell is displayed at its darkened shade, not its
            # stored colour: painting it back to the original is a real change.
            shade = getattr(world, "block_shade", {}).get((x, y, z))
            if shade is not None:
                current = self._unpack_rgb(int(shade) & 0xFFFFFF)
            if current == rgb:
                continue
            if not world.set_block(x, y, z, True, rgb):
                continue
            packet = PaintBlockPacket()
            packet.loop_count = max(0, int(loop_count))
            packet.x, packet.y, packet.z = x, y, z
            packet.color = rgb
            # The direct editor can be a MAP_IS_UGC_CLIENT for safe source-map
            # transfer, so it does not always retain the original host's local
            # paint mutation. Include the owner in this idempotent echo.
            self.server.broadcast(bytes(packet.generate()))
            changed += 1
        return changed

    def _repair_rejected_paint(self, position) -> None:
        """Undo a UGC brush's local recolour the server refused.

        The retail brush host recolours its own VXL before sending packet 7,
        so a dropped stroke would leave that client alone with the new
        colour. Canonical repair re-sends the authoritative colour (packet 33
        plus PaintBlock for solid cells). Only proven predictors call this;
        a forged packet cannot enroll arbitrary cells.
        """

        if self._cell_in_map(position):
            self._queue_canonical_terrain_repair((position,))

    def handle_paint_packet(self, player, packet) -> bool:
        """Validate a native packet-7 request and commit its one target cell."""

        is_editor_brush = self._paintbrush_authorized(player)
        if not is_editor_brush:
            from server.game_rules import get_rules

            if not (
                bool(getattr(player, "alive", False))
                and bool(getattr(player, "spawned", False))
                and bool(getattr(player, "is_block_tool", lambda: False)())
                and get_rules(self.server.config).is_tool_enabled(
                    int(getattr(player, "tool", -1))
                )
            ):
                return False
        try:
            position = (
                int(packet.x),
                int(packet.y),
                int(packet.z),
            )
            color = tuple(int(value) & 0xFF for value in packet.color[:3])
        except (AttributeError, IndexError, TypeError, ValueError):
            return False
        if len(color) != 3:
            return False
        if is_editor_brush:
            dx = float(getattr(player, "x", 0.0)) - position[0]
            dy = float(getattr(player, "y", 0.0)) - position[1]
            dz = float(getattr(player, "z", 0.0)) - position[2]
            reach = float(getattr(C, "PAINTBRUSH_RANGE", 15.0)) + 1.0
            if not dx * dx + dy * dy + dz * dz <= reach * reach:
                self._repair_rejected_paint(position)
                return False
        elif not self._cell_within_reach(player, position, self._build_reach()):
            # Non-editor paint used to recolour any voxel on the map.
            return False
        # UGC brush strokes legitimately cover surface cells around corners
        # (radius up to 4 from the hit), so only single-cell paint needs LOS.
        if not is_editor_brush and not self._cell_in_sight(
            player, position, loop=getattr(packet, "loop_count", None),
            kind="paint_occluded",
        ):
            return False
        if not self._consume_paint_budget(player, editor=is_editor_brush):
            if is_editor_brush:
                self._repair_rejected_paint(position)
            return False
        return bool(
            self._commit_paint(
                player,
                (position,),
                color,
                loop_count=int(getattr(packet, "loop_count", self.server.loop_count)),
            )
        )

    def handle_paintbrush_input(self, player, packet) -> bool:
        """Drive the original host-local brush for a dedicated editor client.

        The retail UGC host mutates its own VXL before packet 7 is replicated.
        A standalone dedicated host advertises direct joiners as UGC clients
        so they can safely accept the server's source VXL; those clients do not
        consistently originate packet 7. ClientData still carries held
        primary/secondary state, orientation, loop stamp, and palette state,
        so the server reconstructs the same single-block/surface-brush action.
        This runs in packet-drain context and is capped by the stock 30 ms
        brush interval and a 128-cell surface footprint.
        """

        if not self._paintbrush_authorized(player):
            return False
        # PaintbrushTool.on_set deliberately leaves the palette active, which
        # sets ClientData's packed ``palette_enabled`` bit for normal strokes.
        # Actual palette clicks are consumed by the HUD and arrive without
        # these action bits, so gating on the palette flag disables painting.
        primary = bool(getattr(packet, "primary", False))
        secondary = bool(getattr(packet, "secondary", False))
        if not primary and not secondary:
            return False

        now = time.monotonic()
        player_id = int(player.id)
        if now < float(self._paintbrush_next_use.get(player_id, 0.0)):
            return False
        interval = float(getattr(C, "PAINTBRUSH_SHOOT_INTERVAL", 0.03))
        self._paintbrush_next_use[player_id] = now + max(0.01, interval)

        direction = self._normalize(tuple(float(value) for value in player.orientation))
        if direction is None:
            return False
        origin = tuple(float(value) for value in player.eye)
        center = self.server.world_manager.raycast(
            origin[0],
            origin[1],
            origin[2],
            direction[0],
            direction[1],
            direction[2],
            float(getattr(C, "PAINTBRUSH_RANGE", 15.0)),
        )
        if center is None:
            return False
        center = tuple(int(value) for value in center)
        radius = (
            int(getattr(C, "PAINTBRUSH_SECONDARY_RADIUS", 3))
            if secondary
            else 0
        )
        positions = self._paintbrush_surface_cells(center, radius=radius)
        packed_color = int(getattr(player, "block_color", 0)) & 0xFFFFFF
        return bool(
            self._commit_paint(
                player,
                positions,
                packed_color,
                loop_count=int(getattr(packet, "loop_count", self.server.loop_count)),
            )
        )

    @staticmethod
    def _spade_profile_for_packet(player, packet):
        """Resolve a melee terrain profile including dual-use mouse actions.

        ``DiggingTool.use_spade`` stores its ``secondary_damage`` boolean in
        bit 1 of ShootPacket's flags byte.  The UGC Super Spade uses that bit
        to select between its type-29 single-block LMB and type-31 3x3x3 RMB.
        Treating the base tool profile as a cube made every ordinary click
        erase client-predicted neighbors and was the editor's spade glitch.
        """

        profile = MELEE_DIG_PROFILES.get(
            int(getattr(player, "tool", -1)),
            DEFAULT_MELEE_PROFILE,
        )
        if (
            int(getattr(player, "tool", -1))
            == int(getattr(C, "UGC_SUPERSPADE_TOOL", 45))
            and bool(int(getattr(packet, "secondary", 0)))
        ):
            return (
                int(getattr(C, "UGC_SUPERSPADE_SECONDARY_DAMAGE", 31)),
                float(getattr(C, "UGC_SUPERSPADE_SECONDARY_DAMAGE_AMOUNT", 7.5)),
                DIG_CUBE,
            )
        return profile

    def _resolve_spade_dig(
        self,
        player,
        origin,
        direction,
        packet,
        *,
        reach_eyes=None,
        reach_slack: float = 0.0,
    ) -> bool:
        """Raycast terrain from the CLIENT's reported origin/direction and dig
        per the player's CURRENT tool (MELEE_DIG_PROFILES).

        - Ordinary spades damage the classic (z-1, z, z+1) column by the
          retail amount (5 per cell: map voxels break, 9-health built blocks
          need two swings). The Miner Super Spade, Zombie hands and the UGC
          Super Spade's RMB damage the centered 3x3x3 cube with the retail
          seeded random extra; UGC LMB damages one block. One matching
          area-damage packet (amount + seed) makes each client apply exactly
          the per-cell damage the server applied (block_damage_model).
        - Pickaxe / knife / crowbar (single cell) and Machete (z,z+1):
          accumulate the tool's per-hit block damage (knife 1 -> 5
          hits/block, Machete 2 -> 3 hits, pickaxe 9 -> 1 hit); each block
          breaks when it reaches
          DEFAULT_BLOCK_HEALTH on both sides. Each tool broadcasts its OWN
          damage type so the client shows the right particles + credits the
          wallet (all melee types are block-granting, measured single-cell).
        """
        if direction is None:
            return False
        block_pos = self.server.world_manager.raycast(
            origin[0], origin[1], origin[2],
            direction[0], direction[1], direction[2],
            MELEE_RANGE,
        )
        if block_pos is None:
            return False
        # The swing origin is validated against the server eye, but a cell
        # MELEE_RANGE beyond an 8-block-offset origin was still diggable.
        if not self._within_eye_reach(
            reach_eyes,
            self._block_center(block_pos),
            float(MELEE_RANGE) + MELEE_EYE_SLACK + _CELL_HALF_DIAGONAL + reach_slack,
        ):
            self._reject(player, "dig_out_of_reach")
            self._queue_canonical_terrain_repair((tuple(block_pos),))
            return False

        dmg_type, block_dmg, pattern = self._spade_profile_for_packet(
            player,
            packet,
        )
        x, y, z = block_pos
        wm = self.server.world_manager
        positions = _melee_dig_positions(block_pos, pattern)
        if block_dmg <= 0.0:
            # Non-digging melee (Riot tools) can still produce a
            # client-side predicted crack.  Reassert the canonical voxel, but
            # never mutate or credit inventory for that visual prediction.
            self._queue_canonical_terrain_repair(positions)
            return False
        if pattern == DIG_MACHETE:
            return self._apply_accumulating_melee_footprint(
                player,
                block_pos,
                positions,
                damage_type=dmg_type,
                block_damage=block_dmg,
            )
        if pattern != DIG_SINGLE:
            # One native area packet (retail amount + seed) makes every
            # client apply the exact per-cell damage the server applies here:
            # a spade column deals 5 to each cell (a 9-health built block
            # needs two swings), Super Spade/Zombie cubes add the seeded
            # random extra.  Sending per-cell packets would expand each cell.
            return self._apply_native_dig(
                player, block_pos, positions, dmg_type, block_dmg
            )

        # Single-cell tool: accumulate damage until the block breaks.
        block_dmg = _damage_model.wire_damage(block_dmg)
        total, destroyed = wm.apply_block_damage(
            x, y, z, block_dmg, threshold=DEFAULT_BLOCK_HEALTH)
        if destroyed:
            player.add_blocks(1)
            self._broadcast_block_damage(
                player, block_pos, self._BLOCK_KILL_DAMAGE, damage_type=dmg_type)
            self._collapse_unsupported(player, [block_pos])
            self._queue_blocks_destroyed(player, (block_pos,), True)
            return True
        if total > 0.0:
            # Partial crack — the client accumulates the same per-hit amount.
            self._broadcast_block_damage(
                player, block_pos, block_dmg, damage_type=dmg_type)
            return True
        return False

    def apply_native_terrain_damage(
        self,
        player,
        position,
        damage_type: int,
        amount: float,
        *,
        seed: int | None = None,
    ):
        """Apply one retail Damage(37) footprint to the canonical map.

        Mirrors ``BlockManager.handle_damage`` exactly (see
        :mod:`server.block_damage_model`): the per-cell damage every client
        derives from ``(type, position, wire amount, seed)`` is applied to
        each solid cell through :meth:`WorldManager.apply_block_damage`, so
        per-cell health (9 for built blocks, 5 for map voxels, scaled by
        RULE_BLOCK_HEALTH) decides breakage identically on both sides.
        Returns ``(seed, wire_amount, destroyed_cells, damaged_cells)`` where
        ``damaged_cells`` lists ``(cell, damage)`` for surviving cells; the
        caller broadcasts the one packet with that seed and amount.  Runs on
        the gameplay thread.
        """

        wm = self.server.world_manager
        amount = _damage_model.wire_damage(amount)
        kind = _damage_model.footprint_kind(damage_type)
        if seed is None:
            seed = (
                random.randrange(256)
                if kind is not None
                and kind[0] in (_damage_model.CUBE, _damage_model.RADIUS)
                else 0
            )
        seed = int(seed) & 0xFF
        destroyed = []
        damaged = []
        if amount > 0.0:
            cells = _damage_model.footprint(damage_type, position, amount, seed)
            for cell, damage in _damage_model.iter_damageable(cells):
                if not wm.get_solid(*cell):
                    continue
                total, gone = wm.apply_block_damage(
                    *cell, damage, threshold=DEFAULT_BLOCK_HEALTH
                )
                if gone:
                    destroyed.append(tuple(cell))
                elif total > 0.0:
                    damaged.append((tuple(cell), damage))
        return seed, amount, destroyed, damaged

    def _apply_native_dig(
        self, player, block_pos, positions, damage_type: int, amount: float
    ) -> bool:
        """Resolve one area melee swing (spade column / 3x3x3 cube)."""

        if not _damage_model.is_native(damage_type):
            self._queue_canonical_terrain_repair(positions)
            return False
        seed, wire_amount, destroyed, damaged = (
            self.apply_native_terrain_damage(
                player, block_pos, damage_type, amount
            )
        )
        if not destroyed and not damaged:
            self._queue_canonical_terrain_repair(positions)
            return False
        if destroyed:
            player.add_blocks(len(destroyed))
        self._broadcast_block_damage(
            player, block_pos, wire_amount, damage_type=damage_type, seed=seed
        )
        if destroyed:
            self._collapse_unsupported(player, destroyed)
            # Melee digging never routes through _broadcast_block_destroy, so
            # the mode (Diamond Mine discovery, Demolition destroy awards)
            # learns about the removal only here.
            self._queue_blocks_destroyed(player, destroyed, True)
        return True

    def _apply_accumulating_melee_footprint(
        self,
        player,
        block_pos,
        positions,
        *,
        damage_type: int,
        block_damage: float,
    ) -> bool:
        """Apply one native self-expanding melee packet to canonical cells.

        Retail's Machete handler expands one type-35 Damage packet to ``z``
        and ``z+1`` and applies 2 damage to each. Sending a packet per cell
        would expand twice and touch neighboring voxels the server never hit.
        This method runs on the gameplay thread after shot validation.
        """
        wm = self.server.world_manager
        affected = False
        destroyed_positions = []
        for position in positions:
            total, destroyed = wm.apply_block_damage(
                *position,
                block_damage,
                threshold=DEFAULT_BLOCK_HEALTH,
            )
            if total > 0.0 or destroyed:
                affected = True
            if destroyed:
                destroyed_positions.append(position)

        if not affected:
            self._queue_canonical_terrain_repair(positions)
            return False

        player.add_blocks(len(destroyed_positions))
        # Broadcast the real per-hit amount even on the strike that crosses
        # the threshold. Every client maintains the same 2+2+2 ledger.
        self._broadcast_block_damage(
            player,
            block_pos,
            block_damage,
            damage_type=damage_type,
        )
        if destroyed_positions:
            self._collapse_unsupported(player, destroyed_positions)
            self._queue_blocks_destroyed(player, destroyed_positions, True)
        return True

    def handle_block_destroy(self, player, packet) -> bool:
        if not player.alive or not player.spawned:
            return False
        from server.game_rules import get_rules
        if not get_rules(self.server.config).is_tool_enabled(
            int(getattr(player, "tool", -1))
        ):
            return False

        try:
            position = (int(packet.x), int(packet.y), int(packet.z))
        except (AttributeError, TypeError, ValueError, OverflowError):
            return False
        if not self._cell_in_map(position):
            return False

        if player.is_block_tool():
            # A forged liberation must not delete (and refund) arbitrary
            # terrain across the map: require the retail build reach and a
            # per-player cadence before touching the world.
            if not self._cell_within_reach(player, position, BUILD_REACH):
                return False
            if not self._cell_in_sight(
                player, position, loop=getattr(packet, "loop_count", None),
                kind="liberate_occluded",
            ):
                return False
            now = time.monotonic()
            player_id = int(player.id)
            if now < float(self._liberate_next_use.get(player_id, 0.0)):
                return False
            if not self.server.world_manager.get_solid(*position):
                return False
            self._liberate_next_use[player_id] = now + BLOCK_TOOL_LIBERATE_INTERVAL
            service = getattr(self.server, "world_mutations", None)
            if service is not None:
                mutation = PendingWorldMutation(
                    owner_id=int(player.id),
                    action_loop=max(0, int(packet.loop_count)),
                    enqueued_tick=int(self.server.loop_count),
                    kind="block_destroy",
                    cell_count=1,
                    apply=lambda: self._commit_block_tool_destroy(
                        player, position
                    ),
                    cancel=lambda: self._queue_canonical_terrain_repair(
                        (position,)
                    ),
                )
                return bool(service.enqueue(mutation))
            self._commit_block_tool_destroy(player, position)
            return True

        if player.is_spade_tool():
            damage_type, block_damage, pattern = MELEE_DIG_PROFILES.get(
                getattr(player, "tool", None), DEFAULT_MELEE_PROFILE
            )
            if not self._cell_within_reach(player, position, DIG_REACH):
                return False
            if not self._cell_in_sight(
                player, position, loop=getattr(packet, "loop_count", None),
                kind="liberate_occluded",
            ):
                return False
            positions = _melee_dig_positions(position, pattern)
            if pattern == DIG_MACHETE:
                # Retail MacheteTool uses ShootPacket. Accepting legacy
                # BlockLiberate as well would apply one swing twice; repair a
                # forged/old predicted liberation instead of instant-killing.
                self._queue_canonical_terrain_repair(positions)
                return False
            if block_damage <= 0.0:
                self._queue_canonical_terrain_repair(positions)
                return False
            now = time.monotonic()
            if not player.consume_shot(now):
                self._queue_canonical_terrain_repair(positions)
                return False
            if pattern != DIG_SINGLE:
                return self._apply_native_dig(
                    player, position, positions, damage_type, block_damage
                )
            total, gone = self.server.world_manager.apply_block_damage(
                *position, block_damage, threshold=DEFAULT_BLOCK_HEALTH
            )
            if gone:
                player.add_blocks(1)
                self._broadcast_block_damage(
                    player,
                    position,
                    self._BLOCK_KILL_DAMAGE,
                    damage_type=damage_type,
                )
                self._collapse_unsupported(player, [position])
                self._queue_blocks_destroyed(player, (position,), True)
                return True
            if total > 0.0:
                self._broadcast_block_damage(
                    player, position, block_damage, damage_type=damage_type
                )
                return True
            self._queue_canonical_terrain_repair(positions)
            return False

        return False

    def _resolve_melee_hit(
        self,
        attacker,
        origin=None,
        direction=None,
        *,
        reach_eyes=None,
        reach_slack: float = 0.0,
    ) -> bool:
        if origin is None:
            origin = attacker.eye
        if direction is None:
            direction = attacker.orientation
        # Terrain between the swing origin and a body blocks the swing just
        # like a shot; a knife used to connect through walls.
        max_distance = float(MELEE_RANGE)
        block_pos = self.server.world_manager.raycast(
            origin[0], origin[1], origin[2],
            direction[0], direction[1], direction[2],
            MELEE_RANGE,
        )
        if block_pos is not None:
            max_distance = min(
                max_distance,
                self._distance(origin, self._block_center(block_pos)),
            )
        hit = self._find_first_player_hit(attacker, origin, direction, max_distance)
        if hit is None:
            return False

        target, headshot, _, position, _part = hit
        if not self._within_eye_reach(
            reach_eyes, position, float(MELEE_RANGE) + MELEE_EYE_SLACK + reach_slack
        ):
            self._reject(attacker, "melee_out_of_reach")
            return False
        # A melee tool has ONE stock player-hit figure
        # (<TOOL>_HITPLAYER_DAMAGE_AMOUNT) -- no per-part tuple, so no
        # headshot multiplier; the victim's class multiplier still applies.
        damage = self._calculate_damage(
            attacker,
            attacker.get_weapon_profile(),
            headshot=False,
            target=target,
        )
        damage = self._apply_riot_shield_mitigation(
            target, attacker, damage, impact=position, melee=True
        )
        if int(getattr(attacker, "tool", -1)) == int(C.RIOTSHIELD_TOOL):
            self._apply_riot_shield_knockback(attacker, target)
        health_before = target.health
        target.damage(damage, source=attacker, kill_type=attacker.get_weapon_profile().kill_type)
        if target.health < health_before:
            from server.profile_stats import hit
            hit(attacker, target)
            self._note_player_hit(attacker, headshot)
            self._broadcast_player_hit_feedback(attacker, position)
        return True

    def _within_eye_reach(self, eyes, point, reach: float) -> bool:
        """Is ``point`` within ``reach`` of any server-simulated eye?

        ``eyes`` of ``None`` skips the check (internal callers that already
        start at the authoritative eye).
        """

        if eyes is None:
            return True
        point = _finite_point(point)
        if point is None or not eyes:
            return False
        return any(self._distance(eye, point) <= reach for eye in eyes)

    def _resolve_hitscan(self, attacker, direction, origin=None) -> bool:
        if origin is None:
            origin = attacker.eye
        hit = self._trace_authoritative_hit(
            attacker, origin, direction, attacker.get_weapon_profile().max_range
        )
        if hit is None:
            return False

        kind, target, headshot, position, part = hit
        if kind == "player":
            damage = self._calculate_damage(
                attacker, attacker.get_weapon_profile(), headshot=headshot,
                target=target, part=part,
            )
            damage = self._apply_riot_shield_mitigation(
                target, attacker, damage, impact=position
            )
            kill_type = KILL_HEADSHOT if headshot else attacker.get_weapon_profile().kill_type
            health_before = target.health
            target.damage(damage, source=attacker, kill_type=kill_type)
            if target.health < health_before:
                from server.profile_stats import hit
                hit(attacker, target)
                self._note_player_hit(attacker, headshot)
                self._broadcast_player_hit_feedback(attacker, position)
            return True

        if kind == "entity":
            self._broadcast_entity_hit(target, position)
            damage = self._entity_damage(attacker.get_weapon_profile())
            self.server.entity_registry.damage_entity(
                target.entity_id, damage, attacker, self.server._build_entity_ctx()
            )
            return True

        if kind == "corpse":
            corpse_lifecycle = getattr(self.server, "corpse_lifecycle", None)
            explode = getattr(corpse_lifecycle, "explode", None)
            if not callable(explode):
                return False
            return bool(
                explode(
                    target,
                    attacker,
                    show_explosion_effect=True,
                )
            )

        if kind == "block":
            return self._apply_block_damage(
                attacker, target, attacker.get_weapon_profile().block_damage
            )
        return False

    def _commit_block_tool_destroy(self, player, position) -> None:
        """Commit one block-tool removal at the post-physics boundary."""

        destroyed = self.server.world_manager.destroy_blocks([position])
        if not destroyed:
            self._queue_canonical_terrain_repair((position,))
            return
        player.add_blocks(len(destroyed))
        self._broadcast_block_destroy(player, destroyed)
        from server.audio import SND_DIG_HIT_BLOCK, play_sound
        play_sound(
            self.server,
            SND_DIG_HIT_BLOCK,
            position=position,
            reliable=False,
            source=player,
        )

    def _broadcast_entity_hit(self, entity, position) -> None:
        """Drive the compiled client's per-entity bullet impact path.

        Native ``process_packet_hit_entity`` resolves ``scene.entities[id]``
        and invokes the entity hit callback with this position/type.  It is a
        server-to-client effect packet; health remains server-only.
        """
        packet = HitEntity()
        packet.entity_id = int(entity.entity_id)
        packet.x, packet.y, packet.z = (float(value) for value in position)
        packet.type = int(getattr(C, "PART_ENTITY1", 7))
        self.server.broadcast(bytes(packet.generate()))

    def _broadcast_player_hit_feedback(self, attacker, position) -> None:
        """Publish the retail client's blood and shooter hit-confirm event.

        Native ``process_packet_shoot_response`` draws blood for every
        recipient, but plays the hit sound and changes the crosshair only when
        ``damage_by`` equals the local player id.  Broadcast this only after
        authoritative health decreases so protected or mode-rejected hits do
        not produce false confirmations.
        """

        packet = ShootResponse()
        packet.damage_by = int(attacker.id)
        packet.damaged = 1
        packet.blood = 1
        packet.position_x, packet.position_y, packet.position_z = (
            float(value) for value in position
        )
        # Blood + hit-confirm are cosmetic; health travels in SetHP/KillAction.
        self.server.broadcast(bytes(packet.generate()), reliable=False)

    def _apply_block_damage(self, attacker, block_pos, damage: float,
                            damage_type: int = None,
                            causer_id: int = None) -> bool:
        if not getattr(self.server.config, "build_damage", True):
            return False

        # Model the amount the client decodes from the quarter-unit byte
        # (e.g. block fire 0.7 arrives as 0.75) so both ledgers agree.
        damage = _damage_model.wire_damage(damage)
        total, destroyed = self.server.world_manager.apply_block_damage(
            block_pos[0],
            block_pos[1],
            block_pos[2],
            damage,
            threshold=DEFAULT_BLOCK_HEALTH,
        )
        if destroyed:
            # Kill-damage guarantees client removal even if the client's own
            # ledger drifted from ours (its per-hit crack progression came
            # from the earlier hit broadcasts).
            self._broadcast_block_destroy(
                attacker, [block_pos], damage_type=damage_type,
                causer_id=causer_id,
            )
        elif total > 0.0:
            self._broadcast_block_damage(
                attacker, block_pos, damage, damage_type=damage_type,
                causer_id=causer_id,
            )
        return total > 0.0

    # Block damage/removal wire contract (DECOMPILED from gameScene.pyd,
    # 2026-07-07, adversarially verified): this client has NO destroy packet.
    # BlockBuild(32) is ADD-ONLY — its block_type is a MATERIAL selector
    # (0=prefab/normal, 1=snow; 2/3 KeyError-crash). Our old "destroy" as
    # type 1 made clients BUILD a snow block and fire the green snow-ring
    # particle (the reported wrong-colored effect) while the real block
    # stayed solid. ServerBlockAction(39) is a no-op stub client-side;
    # BlockOccupy(34)/BlockLiberate(35) are ownership bookkeeping. World
    # geometry changes ONLY via Damage(37): BlockManager.handle_damage ->
    # damage_handlers[type] -> crack tint -> remove_block + BLOCK-COLORED
    # debris at 0 health (DEFAULT_BLOCK_HEALTH=5, 0.25 quantization).
    # Damage.damage is a 1-byte fixed-point float — keep <= 31.75 (byte 127)
    # for signedness safety; still >6x block health.
    _BLOCK_KILL_DAMAGE = 31.75

    def _damage_type_for(self, player) -> int:
        # ALWAYS WEAPON_DAMAGE(6). DECOMPILED (BlockManager.handle_weapon_damage
        # sub_10083C80): type 6 removes EXACTLY int(position) — no expansion.
        # SPADE_DAMAGE(2) makes the CLIENT self-expand to a 3-tall column
        # (z-1, z, z+1 = the "underground" cell), and since our server already
        # picks the exact cells to destroy, type 2 double-expanded them (dig
        # one block -> client removed a whole vertical set). GRENADE_DAMAGE(7)
        # self-expands to a radius too. The server owns the exact cell list, so
        # type 6 keeps the client VXL byte-identical to ours.
        import shared.constants as C
        return int(C.WEAPON_DAMAGE)

    def _build_block_damage_packet(self, player, block_pos, damage: float,
                                   damage_type: int = None, seed: int = 0,
                                   causer_id: int = None):
        """Build one exact or native-expanding terrain-damage packet."""

        from shared.packet import Damage
        packet = Damage()
        packet.player_id = player.id if player is not None else -1
        packet.type = self._damage_type_for(player) if damage_type is None else int(damage_type)
        packet.damage = min(float(damage), self._BLOCK_KILL_DAMAGE)
        packet.face = 0
        # Checked removal makes the stock client run its native 18-neighbor
        # collapse and falling-block animation. The server mirrors the exact
        # same topology/work-budget rules in find_unsupported_chunks, so both
        # authorities remove the same component without per-fallen-cell floods.
        packet.chunk_check = 1
        packet.seed = int(seed) & 0xFF
        # causer_id is an ENTITY id the client reads UNSIGNED (measured: -1
        # decodes to 65535 -> entities[65535] lookup aborts the whole damage
        # handler). The reference server sends the shooter's id here; use a
        # small in-range id (0 for no player) so the client's entity lookup
        # resolves safely instead of exploding.
        packet.causer_id = (
            int(causer_id) if causer_id is not None
            else (player.id if player is not None else 0)
        )
        # Send the INTEGER cell coords, NOT the block center. MEASURED live
        # (real shot path, 2026-07-07): the client resolves the damaged cell as
        # int(position + 0.5) (round-half-up), so a block-center (cell+0.5)
        # rounded to cell+1 — every dug/shot block broke one cell too far on
        # +x/+y/+z (the "offset / underground" the user saw). Sending the exact
        # cell coordinate makes int(cell + 0.5) == cell.
        packet.position = (
            float(block_pos[0]),
            float(block_pos[1]),
            float(block_pos[2]),
        )
        return packet

    def _broadcast_block_damage(self, player, block_pos, damage: float,
                                damage_type: int = None, seed: int = 0,
                                causer_id: int = None):
        packet = self._build_block_damage_packet(
            player,
            block_pos,
            damage,
            damage_type=damage_type,
            seed=seed,
            causer_id=causer_id,
        )
        self.server.broadcast(bytes(packet.generate()))
        sound_id = _BLOCK_HIT_SOUND_BY_DAMAGE.get(int(packet.type))
        if sound_id is not None:
            from server.audio import play_sound
            # The Damage(37) above is the reliable state change; its impact
            # cue is cosmetic and must not stall the reliable channel.
            play_sound(
                self.server,
                sound_id,
                position=block_pos,
                reliable=False,
                source=player,
            )

    def record_exact_block_destroy_catchup(self, player, positions,
                                           causer_id: int = None) -> None:
        """Journal crash-safe exact removals without flooding live clients.

        Native Drill damage is compact because one packet expands to an
        81-cell footprint, but it requires a live projectile entity.  A join
        catch-up can run after that entity has expired, so its journal must
        contain stable type-6 cells instead.  This method runs on the gameplay
        thread immediately after the canonical VXL mutation.
        """

        for position in positions:
            packet = self._build_block_damage_packet(
                player,
                position,
                self._BLOCK_KILL_DAMAGE,
                damage_type=int(C.WEAPON_DAMAGE),
                causer_id=causer_id,
            )
            self.server._record_map_mutation(bytes(packet.generate()))

    def _broadcast_block_hit(self, player, block_pos, damage: float = None):
        """Per-hit crack progression on all clients (their ledger mirrors
        ours and removes the block itself when it reaches 0)."""
        if damage is None:
            damage = player.get_weapon_profile().block_damage if player is not None else 1.0
        self._broadcast_block_damage(player, block_pos, damage)

    def _broadcast_block_destroy(self, player, positions, damage_type: int = None,
                                 causer_id: int = None):
        """Guaranteed removal on all clients: kill-damage Damage(37) per cell
        (a no-op for cells the client already removed on its own)."""
        positions = tuple(tuple(int(value) for value in pos) for pos in positions)
        for pos in positions:
            self._broadcast_block_damage(
                player, pos, self._BLOCK_KILL_DAMAGE,
                damage_type=damage_type, causer_id=causer_id,
            )
        self._collapse_unsupported(player, positions)
        if player is not None and positions:
            is_spade = getattr(player, "is_spade_tool", None)
            mined = bool(is_spade()) if callable(is_spade) else False
            queue = getattr(self.server, "queue_mode_event", None)
            if callable(queue):
                queue("on_blocks_destroyed", player, positions, mined)

    def broadcast_native_radius_destroy(
        self,
        player,
        center,
        positions,
        *,
        damage: float,
        damage_type: int,
        causer_entity_id: int,
        seed: int = 0,
        damaged=(),
    ) -> None:
        """Publish one native expanding blast and journal exact catch-up cells.

        Dynamite/C4/landmine handlers expand ``handle_radius_damage`` from
        ``(type, centre, amount, seed)`` exactly as
        :mod:`server.block_damage_model` does, so the ONE packet makes every
        client apply the same per-cell damage the server already applied
        (``positions`` destroyed, ``damaged`` = surviving ``(cell, damage)``).
        That avoids hundreds of reliable packets and keeps the native effects.
        A peer that never saw the charge entity cannot take the native packet
        (the handler dereferences ``causer_id``); it gets the exact outcome
        instead: type-6 kills plus type-6 partial damage per surviving cell.
        Late joiners cannot resolve the expired charge id either, so their
        journal is exact type-6 kills and their damage arrives via the
        BlockManagerState reveal.
        """

        packet = self._build_block_damage_packet(
            player,
            center,
            damage,
            damage_type=int(damage_type),
            seed=int(seed),
            causer_id=int(causer_entity_id),
        )
        data = bytes(packet.generate())
        positions = tuple(tuple(int(value) for value in pos) for pos in positions)
        connections = getattr(self.server, "connections", None)
        if hasattr(self.server, "broadcast_known_entity_packet") and connections is not None:
            exact = None
            for connection in tuple(connections.values()):
                if not bool(getattr(connection, "in_game", False)):
                    continue
                if int(causer_entity_id) in getattr(connection, "known_entity_ids", ()):
                    connection.send(data, reliable=True)
                    continue
                if exact is None:
                    exact = self._exact_outcome_packets(player, positions, damaged)
                for item in exact:
                    connection.send(item, reliable=True)
        else:
            self.server.broadcast(data, record_mutation=False)

        self.record_exact_block_destroy_catchup(
            player,
            positions,
            causer_id=(int(player.id) if player is not None else 0),
        )
        self._collapse_unsupported(player, positions)
        queue = getattr(self.server, "queue_mode_event", None)
        if player is not None and positions and callable(queue):
            queue("on_blocks_destroyed", player, positions, False)

    def broadcast_native_terrain_damage(
        self,
        player,
        center,
        positions,
        *,
        damage: float,
        damage_type: int,
        seed: int,
        mined: bool = False,
    ) -> None:
        """Publish one native (non-entity) footprint already applied here.

        Used for projectile explosions whose visual entity is already gone:
        the Damage causer is the thrower's player id (0 when unknown), which
        the stock handler accepts for every terrain type (live 2026-09-26).
        Joiners get exact type-6 kills through the canonical journal and the
        surviving damage through the BlockManagerState reveal.
        """

        packet = self._build_block_damage_packet(
            player,
            center,
            damage,
            damage_type=int(damage_type),
            seed=int(seed),
            causer_id=(int(player.id) if player is not None else 0),
        )
        self.server.broadcast(bytes(packet.generate()), record_mutation=False)
        positions = tuple(tuple(int(value) for value in pos) for pos in positions)
        if positions:
            self.record_exact_block_destroy_catchup(
                player,
                positions,
                causer_id=(int(player.id) if player is not None else 0),
            )
            self._collapse_unsupported(player, positions)
            queue = getattr(self.server, "queue_mode_event", None)
            if player is not None and callable(queue):
                queue("on_blocks_destroyed", player, positions, bool(mined))

    def _exact_outcome_packets(self, player, destroyed, damaged) -> list[bytes]:
        """Exact per-cell packets reproducing an already-applied footprint."""

        packets = []
        for cell in destroyed:
            packets.append(bytes(self._build_block_damage_packet(
                player, cell, self._BLOCK_KILL_DAMAGE,
                damage_type=int(C.WEAPON_DAMAGE),
            ).generate()))
        for cell, amount in damaged:
            packet = self._build_block_damage_packet(
                player, cell, amount, damage_type=int(C.WEAPON_DAMAGE),
            )
            packet.chunk_check = 0
            packets.append(bytes(packet.generate()))
        return packets

    def _collapse_unsupported(self, player, removed_positions):
        """Floating-structure collapse: any solid chunk left disconnected from
        the base plane by these removals falls too (cascading until stable).
        The classic AoS behavior — without it a streetlamp whose base is dug
        out levitates forever."""
        wm = self.server.world_manager
        from server.profile_stats import add
        removed_positions = tuple(removed_positions)
        add(player, C.MAP_BLOCKS_DESTROYED_TOTAL, len(removed_positions))
        chunks = wm.find_unsupported_chunks(list(removed_positions))
        collapsed = []
        for chunk in chunks:
            # The triggering Damage(chunk_check=1) makes every client remove
            # and animate this whole component natively. Mirror it server-side
            # immediately; do not flood the original action with one Damage per
            # voxel because that duplicates collapse work and effects.
            collapsed.extend(wm.destroy_blocks(chunk))

        add(player, C.MAP_BLOCKS_DESTROYED_TOTAL, len(collapsed))
        from server.combat_scores import record_blocks_destroyed
        record_blocks_destroyed(
            self.server, player, len(removed_positions) + len(collapsed), chunks,
        )

        # A client whose topology differs by even one voxel can derive a
        # different falling component and retain visible blocks that no longer
        # collide on the server. Confirm the committed air cells later through
        # a bounded exact-cell lane. On clients that collapsed correctly these
        # type-6, chunk_check=0 packets are no-ops.
        repair = getattr(self.server, "terrain_repair", None)
        record = getattr(repair, "record_collapse_cells", None)
        if collapsed and callable(record):
            record(collapsed)

    def _broadcast_block_mutation(
        self, player, position, block_type: int, loop_count: int = None
    ):
        """BUILD announcements only — BlockBuild(32) is add-only on the wire;
        destroys route through _broadcast_block_destroy (Damage 37)."""
        if block_type != BLOCK_ACTION_BUILD:
            self._broadcast_block_destroy(player, [position])
            return
        packet = BlockBuild()
        packet.loop_count = (
            self.server.loop_count if loop_count is None else int(loop_count)
        )
        packet.player_id = player.id
        packet.x = position[0]
        packet.y = position[1]
        packet.z = position[2]
        # Material selector on this client: 0=prefab (normal build).
        packet.block_type = 0
        self.server.broadcast(bytes(packet.generate()))

    def _build_shoot_feedback_packet(self, player, packet) -> ShootFeedbackPacket:
        """Build the native server-to-client remote weapon-action event.

        ``GameScene.process_packet_shoot_feedback`` looks up ``shooter_id``,
        verifies that the visible character still has ``tool_id`` equipped,
        then calls ``character.shoot(seed)``. That client call owns firearm
        audio/muzzle effects. It is crash-unsafe for digging tools, which have
        ``use_primary`` but no ``shoot`` method and replicate through the
        WorldUpdate primary-action bit instead. Packet 6 must never be used
        for the server-to-client direction.
        """

        feedback = ShootFeedbackPacket()
        feedback.loop_count = int(getattr(self.server, "loop_count", 0))
        feedback.shooter_id = int(player.id)
        feedback.tool_id = int(player.tool)
        feedback.shot_on_world_update = int(
            getattr(packet, "shot_on_world_update", 0)
        )
        feedback.seed = int(getattr(packet, "seed", 0)) & 0xFF
        return feedback

    def _validate_shot_packet(self, player, packet):
        """Validate one ShootPacket; return ``(reach_eyes, reach_slack)`` or None.

        Hard checks (can never misfire on a stock client, whose origin IS
        its eye): finite values, non-zero direction, origin within
        SHOT_ORIGIN_TOLERANCE of a server-simulated eye, and a clear voxel
        line of sight from that eye to the claimed origin (a forged origin
        on the far side of a wall used to shoot, knife and dig through it).

        Soft checks (``[anticheat] enforce_shot_origin`` /
        ``enforce_aim_direction``): origin distance from the eye of the
        shot's own input frame, and aim angle against that frame's
        orientation. Off by default: they only report the measurement.
        """

        # NaN compares false against every tolerance below, so a non-finite
        # origin/orientation used to pass validation and then raise inside
        # the native raycast on every shot. Reject it first.
        try:
            raw = (
                packet.x, packet.y, packet.z,
                packet.ori_x, packet.ori_y, packet.ori_z,
            )
        except AttributeError:
            self._shot_drop_reason = "malformed_packet"
            return None
        if not self._finite(*raw):
            logger.debug("Rejecting shoot packet from %s with non-finite values", player.name)
            self._shot_drop_reason = "non_finite_values"
            return None
        packet_origin = (float(packet.x), float(packet.y), float(packet.z))
        packet_direction = self._normalize((packet.ori_x, packet.ori_y, packet.ori_z))
        if packet_direction is None:
            logger.debug("Rejecting shoot packet from %s with zero orientation", player.name)
            self._shot_drop_reason = "zero_orientation"
            return None

        loop = getattr(packet, "loop_count", None)
        at_loop, eyes = reference_eyes(player, loop)
        if not eyes:
            self._shot_drop_reason = "no_server_eye"
            return None
        current_eye = _finite_point(player.eye)
        is_bot = bool(getattr(player, "is_bot", False))

        nearest = min(self._distance(packet_origin, eye) for eye in eyes)
        if not nearest <= SHOT_ORIGIN_TOLERANCE:
            self._reject(player, "shot_origin_far", distance=round(nearest, 2))
            self._shot_drop_reason = f"shot_origin_far({nearest:.2f})"
            return None
        if not any(
            segment_clear(self.server.world_manager, eye, packet_origin)
            for eye in eyes
        ):
            self._reject(
                player, "shot_origin_occluded", distance=round(nearest, 2)
            )
            self._shot_drop_reason = "shot_origin_occluded"
            return None

        from server import anticheat

        # Soft origin check. Claiming a position the server itself simulated
        # (the per-loop or the current eye) grants nothing, so the error is
        # the smaller of the two; without the per-loop eye the current one
        # may lead/lag the client by the reconciliation delay.
        slack = 0.0 if at_loop is not None else lag_slack(player)
        references = [eye for eye in (at_loop, current_eye) if eye is not None]
        origin_error = min(
            self._distance(packet_origin, eye) for eye in references
        ) if references else nearest
        tolerance = float(anticheat.setting(
            self.server, "shot_origin_tolerance", 1.5
        )) + slack
        if not is_bot:
            stats = anticheat_stats(player)
            key = "origin_error" if at_loop is not None else "origin_error_fallback"
            stats[key][bucket_label(origin_error, ORIGIN_ERROR_BUCKETS)] += 1
            if origin_error > tolerance:
                enforce = anticheat.enforcing(self.server, "enforce_shot_origin")
                anticheat.report(
                    self.server, player, "shot_origin_drift", enforced=enforce,
                    error=round(origin_error, 3), allowed=round(tolerance, 3),
                    ref="loop" if at_loop is not None else "current",
                )
                if enforce:
                    stats["rejected"]["shot_origin_drift"] += 1
                    self._shot_drop_reason = f"shot_origin_drift({origin_error:.2f})"
                    return None

        # Coarse direction gate (kept from before): the shot must point
        # within ~75 degrees of a reported orientation.
        at_loop_aim = None
        history = getattr(player, "orientation_at_loop", None)
        if callable(history) and loop is not None:
            try:
                at_loop_aim = self._normalize(
                    _finite_point(history(int(loop)) or ()) or (0.0, 0.0, 0.0)
                )
            except Exception:  # noqa: BLE001 - tolerate partial doubles
                at_loop_aim = None
        server_direction = self._normalize(player.orientation)
        aims = [aim for aim in (at_loop_aim, server_direction) if aim is not None]
        if not aims:
            self._shot_drop_reason = "no_server_orientation"
            return None
        dots = [
            sum(packet_direction[i] * aim[i] for i in range(3)) for aim in aims
        ]
        if not max(dots) >= SHOT_ORIENTATION_DOT_TOLERANCE:
            self._reject(
                player, "shot_direction_mismatch", dot=round(max(dots), 3)
            )
            self._shot_drop_reason = f"shot_direction_mismatch({max(dots):.2f})"
            return None

        # Soft aim check against the shot's own frame (fallback: latest).
        if not is_bot:
            reference_dot = dots[0]
            angle = math.degrees(math.acos(max(-1.0, min(1.0, reference_dot))))
            stats = anticheat_stats(player)
            key = "aim_angle" if at_loop_aim is not None else "aim_angle_fallback"
            stats[key][bucket_label(angle, AIM_ANGLE_BUCKETS)] += 1
            aim_tolerance = float(anticheat.setting(
                self.server, "aim_direction_tolerance_deg", 10.0
            ))
            if angle > aim_tolerance:
                enforce = anticheat.enforcing(self.server, "enforce_aim_direction")
                anticheat.report(
                    self.server, player, "aim_direction_mismatch",
                    enforced=enforce, angle_deg=round(angle, 2),
                    allowed_deg=aim_tolerance,
                    ref="loop" if at_loop_aim is not None else "current",
                    tool=int(getattr(player, "tool", -1)),
                )
                if enforce:
                    stats["rejected"]["aim_direction_mismatch"] += 1
                    self._shot_drop_reason = f"aim_direction_mismatch({angle:.1f}deg)"
                    return None
        return eyes, slack

    def _trace_player_hit(self, attacker, origin, direction, max_range: float):
        block_pos = self.server.world_manager.raycast(
            origin[0],
            origin[1],
            origin[2],
            direction[0],
            direction[1],
            direction[2],
            max_range,
        )
        max_distance = max_range
        if block_pos is not None:
            max_distance = min(max_distance, self._distance(origin, self._block_center(block_pos)))

        hit = self._find_first_player_hit(attacker, origin, direction, max_distance)
        if hit is not None:
            target, headshot, distance, position, _part = hit
            return target, headshot, position, block_pos
        return None, False, None, block_pos

    def _trace_authoritative_hit(self, attacker, origin, direction, max_range: float):
        """Return the nearest player, Classic corpse, entity, or terrain hit.

        ``(kind, target, headshot, position, part)``; ``part`` is the stock
        PART_* id for a player hit and ``None`` otherwise. Terrain caps the ray
        before dynamic-target tests. Players, corpses, and entities are
        compared by actual entry distance so no farther target can absorb a
        shot through nearer geometry.
        """
        block_pos = self.server.world_manager.raycast(
            origin[0], origin[1], origin[2],
            direction[0], direction[1], direction[2], max_range,
        )
        max_distance = float(max_range)
        if block_pos is not None:
            max_distance = min(
                max_distance,
                self._distance(origin, self._block_center(block_pos)),
            )

        player_hit = self._find_first_player_hit(
            attacker, origin, direction, max_distance
        )
        entity_hit = self._find_first_entity_hit(
            origin, direction, max_distance
        )
        corpse_hit = self._find_first_classic_corpse_hit(
            origin, direction, max_distance
        )

        if player_hit is not None and (
            entity_hit is None or player_hit[2] <= entity_hit[1]
        ) and (
            corpse_hit is None or player_hit[2] <= corpse_hit[1]
        ):
            target, headshot, _, position, part = player_hit
            return "player", target, headshot, position, part
        if corpse_hit is not None and (
            entity_hit is None or corpse_hit[1] <= entity_hit[1]
        ):
            corpse, _, position = corpse_hit
            return "corpse", corpse, False, position, None
        if entity_hit is not None:
            entity, _, position = entity_hit
            return "entity", entity, False, position, None
        if block_pos is not None:
            return "block", block_pos, False, self._block_center(block_pos), None
        return None

    def _find_first_entity_hit(self, origin, direction, max_distance: float):
        registry = getattr(self.server, "entity_registry", None)
        if registry is None:
            return None

        closest = None
        for entity in registry.all():
            behavior = getattr(entity, "behavior", None)
            radius = float(getattr(behavior, "hit_radius", 0.0) or 0.0)
            if (
                not entity.alive
                or behavior is None
                or not getattr(behavior, "takes_damage", False)
                or radius <= 0.0
            ):
                continue
            center = behavior.get_hit_center(entity)
            hit = self._ray_sphere_entry(
                origin, direction, max_distance, center, radius
            )
            if hit is None:
                continue
            distance, position = hit
            if closest is None or distance < closest[1]:
                closest = (entity, distance, position)
        return closest

    def _find_first_classic_corpse_hit(
        self,
        origin,
        direction,
        max_distance: float,
    ):
        """Return the nearest static ClassicCorpse KV6 intersected by a ray.

        Corpse state is server-owned but has no entity id.  Reuse the retail
        oriented-KV6 slab transform so a corpse competes with players,
        deployables, and terrain by entry distance instead of absorbing shots
        through nearer geometry.
        """

        corpse_lifecycle = getattr(self.server, "corpse_lifecycle", None)
        iter_hittable = getattr(corpse_lifecycle, "iter_hittable", None)
        if not callable(iter_hittable):
            return None

        closest = None
        for corpse in iter_hittable():
            hit = self._ray_hits_model_bounds(
                origin,
                direction,
                max_distance,
                corpse,
                (0.0, 0.0, 0.0),
                _CLASSIC_CORPSE_BOUNDS,
            )
            if hit is None:
                continue
            distance, position = hit
            if closest is None or distance < closest[1]:
                closest = (corpse, distance, position)
        return closest

    @staticmethod
    def _ray_sphere_entry(origin, direction, max_distance, center, radius):
        """Nearest entry point for a normalized ray and finite sphere."""
        relative = tuple(center[index] - origin[index] for index in range(3))
        projection = sum(relative[index] * direction[index] for index in range(3))
        radius_sq = float(radius) * float(radius)
        closest_sq = sum(component * component for component in relative) - projection ** 2
        if closest_sq > radius_sq:
            return None
        half_chord = math.sqrt(max(0.0, radius_sq - closest_sq))
        entry = projection - half_chord
        exit_distance = projection + half_chord
        if exit_distance < 0.0:
            return None
        entry = max(0.0, entry)
        if entry > float(max_distance):
            return None
        position = tuple(
            origin[index] + direction[index] * entry for index in range(3)
        )
        return entry, position

    def _find_first_player_hit(self, attacker, origin, direction, max_distance: float):
        """(target, headshot, distance, position, part) of the nearest body."""
        closest_target = None
        closest_headshot = False
        closest_distance = max_distance + 1.0
        closest_position = None
        closest_part = None

        for target in self.server.players.values():
            if target is attacker or not target.alive or not target.spawned:
                continue
            if (
                not getattr(self.server.config, "friendly_fire", False)
                and target.team == attacker.team
            ):
                continue

            from server import lag_compensation

            hit = self._ray_hits_target(
                origin, direction, max_distance,
                lag_compensation.body_for(self, target),
            )
            if hit is None:
                continue

            distance, position, headshot, part = hit
            if distance < closest_distance:
                closest_target = target
                closest_headshot = headshot
                closest_distance = distance
                closest_position = position
                closest_part = part

        if closest_target is None:
            return None
        return (closest_target, closest_headshot, closest_distance,
                closest_position, closest_part)

    def _ray_hits_target(self, origin, direction, max_distance: float, target):
        """Match stock hitscan_player against oriented KV6 model bounds."""
        profile = _CLASS_HITBOXES.get(target.class_id, _CLASS_HITBOXES[C.CLASS_SOLDIER])
        if getattr(target, "hitbox_crouched", target.input.crouch):
            parts = (
                (C.PART_TORSO, _CROUCH_TORSO, (0.0, 0.0, 0.3)),
                (C.PART_HEAD, profile[C.PART_HEAD], (0.0, 0.0, 0.3)),
                (C.PART_ARMS, profile[C.PART_ARMS], (0.0, 0.0, C.BODY_PART_ARMS_CROUCH_Z)),
                (C.PART_LEFT_LEG, _CROUCH_LEG, (0.25, C.BODY_PART_LEG_CROUCH_Y, 0.7)),
                (C.PART_RIGHT_LEG, _CROUCH_LEG, (-0.25, C.BODY_PART_LEG_CROUCH_Y, 0.7)),
            )
        else:
            parts = (
                (C.PART_TORSO, profile[C.PART_TORSO], (0.0, 0.0, 0.3)),
                (C.PART_HEAD, profile[C.PART_HEAD], (0.0, 0.0, 0.3)),
                (C.PART_ARMS, profile[C.PART_ARMS], (0.0, 0.0, 0.5)),
                (C.PART_LEFT_LEG, profile[C.PART_LEFT_LEG], (0.25, 0.0, 1.1)),
                (C.PART_RIGHT_LEG, profile[C.PART_RIGHT_LEG], (-0.25, 0.0, 1.1)),
            )

        # Stock aoslib.weapons.hitscan_player tests torso, head, arms, left
        # leg, right leg IN THAT ORDER and returns the first box hit (not the
        # nearest), so a ray grazing both torso and head counts as torso.
        for part_id, model_bounds, model_offset in parts:
            hit = self._ray_hits_model_bounds(
                origin, direction, max_distance, target, model_offset, model_bounds)
            if hit is not None:
                distance, position = hit
                return distance, position, part_id == C.PART_HEAD, int(part_id)
        return None

    def _ray_hits_model_bounds(
        self, origin, direction, max_distance, target, model_offset, model_bounds
    ):
        """Ray/slab intersection using the stock hitscan_model transform."""
        yaw = math.atan2(target.orientation[0], target.orientation[1])
        cosine = math.cos(yaw)
        sine = math.sin(yaw)
        model_x, model_y, model_z = model_offset
        model_position = (
            target.x - model_x * cosine + model_y * sine,
            target.y + model_x * sine - model_y * cosine,
            target.z + model_z,
        )
        axes = (
            (-cosine * HITBOX_SCALE, sine * HITBOX_SCALE, 0.0),
            (sine * HITBOX_SCALE, cosine * HITBOX_SCALE, 0.0),
            (0.0, 0.0, HITBOX_SCALE),
        )
        sizes, pivots = model_bounds
        enter = 0.0
        leave = max_distance
        relative = (
            origin[0] - model_position[0],
            origin[1] - model_position[1],
            origin[2] - model_position[2],
        )

        for axis, size, pivot in zip(axes, sizes, pivots):
            axis_length_sq = sum(component * component for component in axis)
            coordinate = sum(relative[i] * axis[i] for i in range(3)) / axis_length_sq + pivot
            rate = sum(direction[i] * axis[i] for i in range(3)) / axis_length_sq
            if abs(rate) < 1e-9:
                if coordinate < 0.0 or coordinate > size:
                    return None
                continue
            first = -coordinate / rate
            second = (size - coordinate) / rate
            if first > second:
                first, second = second, first
            enter = max(enter, first)
            leave = min(leave, second)
            if enter > leave:
                return None

        position = tuple(origin[i] + direction[i] * enter for i in range(3))
        return enter, position

    def _calculate_damage(self, attacker, profile, headshot: bool,
                          target=None, part=None) -> float:
        """Stock hit damage: the weapon's per-part figure scaled by the
        VICTIM's class (and flying-jetpack) multipliers.

        The stock damage tuple is (torso, head, arms, left leg, right leg);
        ``part`` is the PART_* id from the stock hitbox order. Without a part
        the head/torso figure is chosen from ``headshot``. The class tables
        (CLASS_DAMAGE_MULTIPLIER / CLASS_HEADSHOT_DAMAGE_MULTIPLIER) belong to
        the class being hit: Scout 1.43 ("your health ... limited"), Miner
        head 0.5 (helmet), UGC builder 0. ``target=None`` (legacy callers)
        applies no victim multiplier.

        The result stays a float: ``Player.damage`` rounds ONCE, after the
        RULE_WEAPON_DAMAGE scale (rules audit 2026-09-27 #23 -- it used to be
        rounded here with a floor of 1 and again there).
        """
        from server.weapons_retail import victim_damage_multiplier

        if part is None:
            part = C.PART_HEAD if headshot else C.PART_TORSO
        damage_for_part = getattr(profile, "damage_for_part", None)
        if callable(damage_for_part):
            damage = float(damage_for_part(int(part)))
        else:
            damage = float(profile.base_damage)
            if headshot:
                damage *= float(getattr(profile, "headshot_multiplier", 1.0))
        if target is not None:
            damage *= victim_damage_multiplier(
                target, headshot=bool(headshot) and not profile.is_melee
            )
        if damage <= 0.0:
            return 0.0
        return float(damage)

    @staticmethod
    def _entity_damage(profile) -> float:
        """Stock <WEAPON>_DAMAGE_ENTITY: a bullet's damage to a deployable."""
        entity_damage = float(getattr(profile, "entity_damage", 0.0) or 0.0)
        if entity_damage > 0.0:
            return entity_damage
        return float(profile.base_damage)

    def _apply_riot_shield_mitigation(
        self, target, attacker, damage: float, *, impact=None, melee: bool = False
    ) -> float:
        """Apply the retail shield's 50% absorption to frontal direct hits.

        The shield has no activation packet: it is held whenever tool 52 is
        equipped and the ordinary WorldUpdate display bit is set.  A positive
        facing dot means the source lies in the shield bearer's front
        hemisphere. Explosions and status damage do not route through this
        helper because their impact origin is not the attacking character.

        ``impact`` is where the hit landed. When the shield takes it, the
        retail RiotShieldEntity plays its bullet or melee impact there.
        """
        if (
            int(getattr(target, "tool", -1)) != int(C.RIOTSHIELD_TOOL)
            or not bool(getattr(getattr(target, "input", None),
                                "can_display_weapon", False))
        ):
            return damage

        to_source = (
            float(attacker.x) - float(target.x),
            float(attacker.y) - float(target.y),
            float(attacker.z) - float(target.z),
        )
        source_direction = self._normalize(to_source)
        facing = self._normalize(target.orientation)
        if source_direction is None or facing is None:
            return damage
        dot = sum(facing[index] * source_direction[index] for index in range(3))
        if dot <= 0.0:
            return damage

        absorption = max(
            0.0,
            min(
                1.0,
                float(getattr(C, "RIOTSHIELD_DAMAGE_ABSORPTION_PERCENT", 50.0))
                / 100.0,
            ),
        )
        if impact is not None and absorption > 0.0 and float(damage) > 0.0:
            from server.entities.attachments import riot_shield_hit

            riot_shield_hit(self.server, target, impact, melee=melee)
        return max(0.0, float(damage) * (1.0 - absorption))

    @staticmethod
    def _apply_riot_shield_knockback(attacker, target) -> None:
        """Push a shield-bashed enemy horizontally by the recovered 0.5."""
        dx = float(target.x) - float(attacker.x)
        dy = float(target.y) - float(attacker.y)
        length = math.hypot(dx, dy)
        if length <= 1e-6:
            dx = float(attacker.orientation[0])
            dy = float(attacker.orientation[1])
            length = math.hypot(dx, dy)
        if length <= 1e-6:
            return
        strength = float(getattr(C, "RIOTSHIELD_KNOCKBACK", 0.5))
        vx, vy, vz = target.velocity
        target.velocity = (
            float(vx) + dx / length * strength,
            float(vy) + dy / length * strength,
            float(vz),
        )

    def _block_center(self, block_pos):
        return (block_pos[0] + 0.5, block_pos[1] + 0.5, block_pos[2] + 0.5)

    def _distance(self, a, b) -> float:
        return math.sqrt(
            (a[0] - b[0]) * (a[0] - b[0])
            + (a[1] - b[1]) * (a[1] - b[1])
            + (a[2] - b[2]) * (a[2] - b[2])
        )

    def _normalize(self, vector) -> Optional[tuple[float, float, float]]:
        magnitude = math.sqrt(
            vector[0] * vector[0] + vector[1] * vector[1] + vector[2] * vector[2]
        )
        if magnitude <= 0.000001:
            return None
        return (
            vector[0] / magnitude,
            vector[1] / magnitude,
            vector[2] / magnitude,
        )
