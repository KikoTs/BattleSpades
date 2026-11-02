"""Accelerated Zombie horde scenarios with the production AI and physics.

Runs the real server tick (director, perception, horde coordinator, brain,
motor, native player physics, claw damage, terrain collapse) on an authored
map against a synthetic clock, so two minutes of play take a few seconds of
wall time and the same seed replays the same round. The brain runs inline
through the production batch entry point with the production planning
budget; only the worker thread is removed.

Scenarios (``--scenario``):

``islands``   one zombie on every offshore islet, idle survivors on the
              mainland: does each infected bot cross the water?
``sea``       zombies at their authored spawn (SpookyMansion: the sea ring),
              idle survivors at the survivor spawn.
``wade``      idle survivors standing in open water off the mainland shore,
              zombies on the mainland: do the infected kill in water?
``offshore``  idle survivors on the offshore islets, zombies at their spawn.
``ground``    idle survivors on dry flat top surfaces far apart (a rooftop
              counts), zombies at their spawn.
``open``      the same, but only on ground level with its surroundings:
              pursuit and rush metrics without a climb.
``round``     survivor bots that fight and build against the horde.
``yard``      a survivor in a walled yard whose only gate is on the far
              side: the walk round against clawing through one wall.
``room``      a survivor in a sealed room on the ground.
``pillar`` / ``platform`` / ``tower`` / ``fortress``
              survivors on a synthetic structure nothing walks up to.
``roof`` / ``bridge``
              survivors on a structure with stairs: walking up beats any cut.
``dropin``    a survivor on a pillar that can also be dropped onto from a
              high walkway whose stairs are far away: the one-voxel cut
              beats the walk.
``ledge``     survivors on a block two high (one jump up): nothing to besiege.
``pit``       survivors in a pit three deep (one drop down): likewise.

Measured per run (see ``_Metrics``): first contact / first hit / infection
times, the share of each zombie's time spent closing on its target, digging,
fighting, building (prefab stacks, climb skills), climbing (rising 0.8+
blocks in 2 s without closing horizontally), swimming, wandering or
standing, ``useful_share`` (closing + digging + fighting + building +
climbing), sprint share while travelling, how long a stall lasts before the
watchdog escalates, water crossings, the pile under elevated survivors
(``pile_mean`` / ``pile_max`` / ``pile_seconds``), ``ring_idle_share``
(samples where a bot ordered to surround just stands), ``first_at_height_s``
(a zombie's feet within 3 blocks of the survivor's height and 4 blocks
across), ``sides_used`` (45-degree sectors round the survivor from which a
zombie came within 6 blocks at the structure's base level or higher), the
longest a bot stood still, and the horde planning cost.

``--armed`` makes the idle survivor fight back like a human: every 0.6-0.9 s
one rifle shot at the nearest zombie in sight (through
``CombatSystem.handle_shot``, the validated ShootPacket path: team, alive,
cadence, clip, origin and aim gates all apply; the rifle's own spread can
miss), reloading when the clip is empty, and every 4 s one missing pillar
voxel within reach under his feet is put back (``repairs``, at most 20).
Killed zombies respawn through the mode's ordinary respawn timer;
``zombie_deaths`` / ``zombie_respawns`` count them.

    py -3.12 scripts/bot_zombie_horde_scenario.py --map SpookyMansion \
        --scenario islands --seconds 120 --seed 7 --json out.json
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter, deque
import json
import math
from pathlib import Path
import random
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
_perf = time.perf_counter  # the real counter, for the harness's own timings
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import shared.constants as C  # noqa: E402
from modes import get_mode_class  # noqa: E402
from modes.zombie import SURVIVOR_TEAM, ZOMBIE_TEAM, ZombiePhase  # noqa: E402
from server.bot_ai import BotDirector  # noqa: E402
from server.bot_ai.messages import MovementAffordance, WorldDelta  # noqa: E402
from server.bot_ai.planning_budget import PlanningBudget  # noqa: E402
from server.bot_ai.simple_navigation import SimpleVoxelWorld  # noqa: E402
from server.bot_ai.simple_worker import (  # noqa: E402
    SimpleBotBrain,
    decide_current_frame,
    refresh_planning_observers,
)
from server.bot_ai.structure_collapse import support_cells  # noqa: E402
from server.bot_ai.supervisor import WorkerStatus  # noqa: E402
from server.bot_ai.zombie_siege import ZombieSiegeService  # noqa: E402
from server.class_selection import normalize_class_selection  # noqa: E402
from server.config import load_config  # noqa: E402
from server.game_constants import DEFAULT_WEAPON_TOOL  # noqa: E402
from server.main import BattleSpadesServer  # noqa: E402
from server.player import Player  # noqa: E402
from shared.packet import ShootPacket  # noqa: E402

SCENARIOS = (
    "islands", "sea", "wade", "offshore", "ground", "open", "round", "yard", "room",
    "pillar", "platform", "tower", "fortress", "roof", "bridge", "dropin", "ledge", "pit",
    "highroof", "skybridge", "stacked", "roofpillar", "wallpillar", "treebridge", "roofbridge",
    "wallneck",
)
ELEVATED = frozenset({"pillar", "platform", "tower", "fortress", "roof", "bridge", "dropin",
                      "highroof", "skybridge", "stacked", "roofpillar", "wallpillar",
                      "treebridge", "roofbridge", "wallneck"})
STRUCTURES = ELEVATED | {"yard", "room", "ledge", "pit"}
STAND = 2.25
SAMPLE_SECONDS = 0.5
WINDOW_SECONDS = 2.0
# Activity classification (``_Metrics.sample``).
BUILD_ROLE_PREFIXES = ("prefab_ascent", "climb", "pillar", "staircase")
BUILD_ACTIONS = frozenset({"place_prefab", "build", "build_line"})
RISE_BLOCKS = 0.8          # a body that rose this much in a window is climbing
STILL_BLOCKS = 0.4         # less than this between samples = standing still
USEFUL_KINDS = ("closing", "digging", "fighting", "building", "climbing")


class InlineSupervisor:
    """The production worker batch, evaluated on the caller's thread.

    Same world, brain, planning budget and newest-frame coalescing as
    ``AIThreadSupervisor._worker_main``; a batch runs when the director
    drains intents, so replies arrive one tick later, as from the thread.
    """

    def __init__(self, decision_hz: float = 8.0, path_requests_per_second: float = 24.0):
        self.decision_hz = float(decision_hz)
        self.rate = float(path_requests_per_second)
        self._new_world()
        self._frames: dict = {}
        self._changes: dict = {}
        self._intents: deque = deque()
        self._map_epoch = -1
        self._version = -1
        self.decision_seconds = 0.0
        self.decisions = 0
        self.trace_bot: int | None = None
        self.trace_origin = 0.0
        self.travel_decisions = 0
        self.sprint_decisions = 0
        self.walk_reasons: Counter = Counter()

    def _new_world(self) -> None:
        self.world = SimpleVoxelWorld(planning_budget=PlanningBudget(
            self.rate, decision_hz=self.decision_hz))
        self.brain = SimpleBotBrain(self.world, decision_hz=self.decision_hz)

    @property
    def snapshot_required(self) -> bool:
        return False

    def start(self, snapshot) -> None:
        self.publish_map(snapshot)

    def close(self, timeout: float = 0.0) -> None:
        self._frames.clear()
        self._intents.clear()

    def publish_map(self, snapshot) -> None:
        self.world.load(snapshot)
        self.brain = SimpleBotBrain(self.world, decision_hz=self.decision_hz)
        self.brain.reset_for_map(snapshot.map_epoch)
        self._map_epoch = int(snapshot.map_epoch)
        self._version = int(snapshot.topology_version)
        self._changes.clear()
        self._frames.clear()

    def publish_world_change(self, change, *, map_epoch, topology_version) -> None:
        if int(map_epoch) != self._map_epoch:
            return
        self._version = max(self._version, int(topology_version))
        self._changes[change.coordinate] = change

    def submit_frame(self, frame) -> bool:
        key = int(frame.observer_id), int(frame.observer_generation)
        previous = self._frames.get(key)
        if previous is not None and int(previous.frame_id) >= int(frame.frame_id):
            return False
        self._frames[key] = frame
        return True

    def drain_intents(self, limit: int = 12):
        if self._changes:
            self.world.apply(WorldDelta(self._map_epoch, self._version,
                                        tuple(self._changes.values())))
            self._changes.clear()
        frames = sorted(self._frames.values(), key=lambda item: int(item.frame_id))
        self._frames.clear()
        if frames:
            refresh_planning_observers(self.world, frames)
            started = _perf()
            for frame in frames:
                intent = decide_current_frame(self.world, self.brain, frame)
                if intent is not None:
                    self._intents.append(intent)
                    self._count_walk(frame, intent)
                    if self.trace_bot == int(frame.observer_id):
                        self._trace_decision(frame, intent)
            self.decision_seconds += _perf() - started
            self.decisions += len(frames)
        return [self._intents.popleft()
                for _ in range(min(max(0, int(limit)), len(self._intents)))]

    def _count_walk(self, frame, intent) -> None:
        """Why a travelling bot was told to walk rather than sprint."""

        move = intent.movement
        if math.hypot(move.direction[0], move.direction[1]) < 0.1:
            return
        self.travel_decisions += 1
        if move.sprint:
            self.sprint_decisions += 1
            return
        note = str(dict(intent.debug_navigation).get("step_note", "")).split(":")[0]
        reason = f"{move.affordance.value}:{note}"
        state = self.brain._states.get((int(frame.observer_id), int(frame.observer_generation)))
        observer = next((p for p in frame.players if p.player_id == frame.observer_id), None)
        if (state is not None and observer is not None and note == "blocked"
                and move.affordance is MovementAffordance.WALK):
            previous = observer.position
            heading = None
            reason = "walk:short_run"
            for step in state.route[state.route_index:state.route_index + 8]:
                if step.affordance is not MovementAffordance.WALK:
                    reason = f"walk:before_{step.affordance.value}"
                    break
                if abs(step.waypoint[2] - previous[2]) > 0.25:
                    reason = "walk:slope"
                    break
                dx, dy = step.waypoint[0] - previous[0], step.waypoint[1] - previous[1]
                length = math.hypot(dx, dy)
                if length > 1e-6:
                    unit = (dx / length, dy / length)
                    if heading is not None and heading[0] * unit[0] + heading[1] * unit[1] < 0.94:
                        reason = "walk:turn"
                        break
                    heading = heading or unit
                previous = step.waypoint
        self.walk_reasons[reason] += 1

    def _trace_decision(self, frame, intent) -> None:
        """One line of navigation internals for ``--trace-bot``."""

        observer = next(p for p in frame.players if p.player_id == frame.observer_id)
        state = self.brain._states.get((int(frame.observer_id), int(frame.observer_generation)))
        style = self.brain._traversal_personality(frame, observer).style.value
        step = (state.route[state.route_index]
                if state is not None and state.route_index < len(state.route) else None)
        extra = ""
        if state is not None:
            extra = (f" wc={int(state.water_committed)} wr={int(state.water_recovery)}"
                     f" dryfail={state.dry_route_failures}"
                     f" detour={_r(state.dry_detour_goal)} esc={_r(state.escape_goal)}"
                     f" route={state.route_index}/{len(state.route)}"
                     f" step={(step.affordance.value, _r(step.waypoint)) if step else None}"
                     f" corr={len(state.corridor)} dead={int(state.dead_end)}"
                     f" blocked={len(state.blocked_edges)}")
        move = intent.movement
        print(f"d t={frame.created_at - self.trace_origin:6.2f} pos={_r(observer.position)}"
              f" goal={_r(state.goal.position) if state is not None and state.goal is not None else None}"
              f" g={int(observer.grounded)} w={int(observer.wade)} style={style}"
              f" role={intent.debug_role} aff={move.affordance.value}"
              f" dir=({move.direction[0]:+.2f},{move.direction[1]:+.2f}) j={int(move.jump)}"
              f" spr={int(move.sprint)} act={intent.action.kind.value}{extra}", flush=True)

    def status(self) -> WorkerStatus:
        return WorkerStatus(
            running=True, process_id=None, restarts=0, stalled_restarts=0,
            intent_silence_seconds=0.0, queued_frames=len(self._frames),
            queued_intents=len(self._intents), pending_terrain_cells=len(self._changes),
            dropped_frames=0, dropped_intents=0, snapshot_required=False,
            awaiting_frame_id=None, last_acknowledged_frame_id=-1,
            last_heartbeat_batch_id=-1, last_heartbeat_frame_id=-1,
            awaiting_snapshot_transfer_id=None,
        )


def _r(vector):
    return None if vector is None else tuple(round(float(v), 1) for v in vector)


class _IdleConnection:
    def __init__(self, server) -> None:
        self.server = server
        self.in_game = True
        self.player = None

    def send(self, data, reliable: bool = True, prefix: int = 0x30) -> None:
        return None

    def send_packet(self, packet, reliable: bool = True) -> None:
        return None

    def disconnect(self, reason: int = 0) -> None:
        return None


def land_components(world) -> tuple[dict, list[int]]:
    """Label dry columns by 8-connected land mass; sizes by label."""

    label: dict[tuple[int, int], int] = {}
    sizes: list[int] = []
    for x in range(512):
        for y in range(512):
            if (x, y) in label or world.is_water_column(x, y):
                continue
            index = len(sizes)
            label[(x, y)] = index
            queue = deque([(x, y)])
            count = 0
            while queue:
                cx, cy = queue.popleft()
                count += 1
                for dx in (-1, 0, 1):
                    for dy in (-1, 0, 1):
                        nxt = (cx + dx, cy + dy)
                        if (0 <= nxt[0] < 512 and 0 <= nxt[1] < 512 and nxt not in label
                                and not world.is_water_column(*nxt)):
                            label[nxt] = index
                            queue.append(nxt)
            sizes.append(count)
    return label, sizes


def _typical_ground(world) -> int:
    """Median height of the map's dry land: a big rooftop is far above it."""

    heights = sorted(world.get_height(x, y) for y in range(8, 504, 8) for x in range(8, 504, 8)
                     if not world.is_water_column(x, y))
    return heights[len(heights) // 2] if heights else 0


def _level_with_surroundings(world, x: int, y: int, floor: int) -> bool:
    """Not a rooftop or a pit: within two blocks of the land 12-24 out."""

    ring = sorted(
        world.get_height(x + int(r * math.cos(a)), y + int(r * math.sin(a)))
        for r in (12, 18, 24) for a in (k * math.pi / 6.0 for k in range(12))
        if 0 < x + int(r * math.cos(a)) < 511 and 0 < y + int(r * math.sin(a)) < 511
        and not world.is_water_column(x + int(r * math.cos(a)), y + int(r * math.sin(a))))
    return len(ring) >= 18 and abs(ring[len(ring) // 2] - floor) <= 2


def _flat_site(world, rng: random.Random, span: int = 8, avoid=(),
               near: tuple[float, float] = (256.0, 256.0),
               level: bool = False) -> tuple[int, int, int]:
    """A dry, flat (height range <= 1) square, preferring ``near``."""

    candidates = []
    ground = _typical_ground(world) if level else 0
    for y in range(64, 448, 6):
        for x in range(64, 448, 6):
            if any(math.hypot(x - ax, y - ay) < 40 for ax, ay in avoid):
                continue
            if level and not (
                    abs(world.get_height(x, y) - ground) <= 8
                    and _level_with_surroundings(world, x, y, world.get_height(x, y))):
                continue
            heights = []
            ok = True
            for dx in range(-span, span + 1, 2):
                for dy in range(-span, span + 1, 2):
                    if world.is_water_column(x + dx, y + dy):
                        ok = False
                        break
                    heights.append(world.get_height(x + dx, y + dy))
                if not ok:
                    break
            if ok and heights and max(heights) - min(heights) <= 1 and max(heights) < 236:
                candidates.append((math.hypot(x - near[0], y - near[1]), x, y, max(heights)))
    if not candidates:
        raise RuntimeError("no flat dry site on this map")
    candidates.sort()
    _, x, y, floor = rng.choice(candidates[: max(1, len(candidates) // 6)])
    return x, y, floor


def _rooftop_site(world, rng: random.Random, span: int = 2) -> tuple[int, int, int]:
    """A real building roof: flat 5x5, at least six blocks above the land around it."""

    ground = _typical_ground(world)
    candidates = []
    for y in range(48, 464, 3):
        for x in range(48, 464, 3):
            if world.is_water_column(x, y):
                continue
            top = world.get_height(x, y)
            if top > ground - 6 or top < 40:
                continue
            heights = [world.get_height(x + dx, y + dy)
                       for dx in range(-span, span + 1) for dy in range(-span, span + 1)]
            if max(heights) - min(heights) > 0:
                continue
            ring = sorted(world.get_height(x + int(r * math.cos(a)), y + int(r * math.sin(a)))
                          for r in (8, 12, 16) for a in (k * math.pi / 6.0 for k in range(12))
                          if not world.is_water_column(x + int(r * math.cos(a)),
                                                      y + int(r * math.sin(a))))
            if len(ring) < 24:
                continue
            around = ring[len(ring) // 2]
            if around - top < 6 or around - top > 30:
                continue
            candidates.append((abs(around - top - 12), math.hypot(x - 256, y - 256), x, y, top))
    if not candidates:
        raise RuntimeError("no building roof on this map")
    candidates.sort()
    _, _, x, y, top = rng.choice(candidates[: max(1, len(candidates) // 8)])
    return x, y, top


def _wall_site(world, rng: random.Random) -> tuple[int, int, int, tuple[int, int]]:
    """Open flat ground beside a wall at least eight high; returns the wall direction."""

    candidates = []
    for y in range(48, 464, 3):
        for x in range(48, 464, 3):
            if world.is_water_column(x, y):
                continue
            floor = world.get_height(x, y)
            if floor > 236:
                continue
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                wall = world.get_height(x + dx, y + dy)
                if floor - wall < 8:
                    continue
                # A real wall: solid from the street up, not an awning or a
                # bridge deck high overhead with air beneath.
                if not all(world.get_solid(x + dx, y + dy, z) for z in range(floor - 8, floor)):
                    continue
                # flat open ground on the three other sides
                flat = all(abs(world.get_height(x - dx * k + sx, y - dy * k + sy) - floor) <= 1
                           for k in range(0, 5) for sx, sy in ((0, 0), (dy, dx), (-dy, -dx))
                           if not world.is_water_column(x - dx * k + sx, y - dy * k + sy))
                if not flat:
                    continue
                candidates.append((math.hypot(x - 256, y - 256), x, y, floor, (dx, dy)))
                break
    if not candidates:
        raise RuntimeError("no wall site on this map")
    candidates.sort()
    _, x, y, floor, direction = rng.choice(candidates[: max(1, len(candidates) // 8)])
    return x, y, floor, direction


def _street_anchor(world, rng: random.Random, x: int, y: int, floor: int, scenario: str,
                   wall_direction, fallback, tries: int = 40,
                   spawn_range: tuple[float, float] = (16.0, 26.0)):
    """A horde start at the right level for a player-built pillar.

    ``dry_ground_anchor`` happily lands on a neighbouring rooftop (London seed
    7 wallpillar: zombies 30 blocks above the survivor). Draw (angle, radius
    16-26) candidates and accept only street level: beside a wall within two
    blocks of the pillar's floor; below a building at least five blocks under
    the roof. The plain anchor is kept when nothing qualifies.
    """

    def qualifies(anchor) -> bool:
        level = world.get_height(int(anchor[0]), int(anchor[1]))
        if scenario == "wallpillar":
            return abs(level - floor) <= 2
        return level >= floor + 5

    if qualifies(fallback):
        return fallback
    for _ in range(tries):
        angle = rng.uniform(0, 2 * math.pi)
        if wall_direction is not None:
            away = math.atan2(-wall_direction[1], -wall_direction[0])
            angle = away + rng.uniform(-0.6 * math.pi, 0.6 * math.pi)
        radius = rng.uniform(*spawn_range)
        anchor = world.dry_ground_anchor(x + radius * math.cos(angle),
                                         y + radius * math.sin(angle), search=12)
        if qualifies(anchor):
            return anchor
    return fallback


def build_structure(world, structure: str, x: int, y: int, floor: int,
                    *, height: int = 64, bridge_span: int = 64, bridge_width: int = 1,
                    direction: tuple[int, int] = (1, 0)):
    """Place a synthetic structure; survivor tops, built cells, zombie ring."""

    color = 0x777777
    tops: list[tuple[int, int, int]] = []
    built: list[tuple[int, int, int]] = []

    def put(cx, cy, cz):
        built.append((cx, cy, cz))
        world.set_block(cx, cy, cz, True, color)

    def clear(cx, cy, cz):
        world.set_block(cx, cy, cz, False, 0)

    if structure in {"highroof", "skybridge", "stacked"}:
        if not 8 <= height <= floor-4:
            raise ValueError(f"height {height} needs ground at z >= {height+4}; found {floor}")
        top = floor-height
        half = bridge_span//2 if structure == "skybridge" else 4
        ys = range(y-bridge_width//2, y+(bridge_width+1)//2) if structure == "skybridge" else range(y-4, y+5)
        anchors = ((x-half, ys.start), (x+half, ys.stop-1)) if structure == "skybridge" else (
            (x-4,y-4), (x+4,y-4), (x-4,y+4), (x+4,y+4))
        for cx in range(x-half, x+half+1):
            for cy in ys:
                put(cx,cy,top)
                if structure == "stacked":
                    for level in range(top+12, floor-3, 12):
                        put(cx,cy,level)
        for ax,ay in anchors:
            for z in range(top+1,floor):
                put(ax,ay,z)
        tops.extend([(x,y,top),(x+1,y,top)])
    elif structure in ("roofpillar", "wallpillar"):
        # What a player does under pressure: stands still and places blocks
        # under his own feet. ``floor`` is the roof (or the ground beside a
        # wall); the pillar is one column, ``height`` blocks tall, attached
        # to a building that is itself grounded. No cheap cut exists.
        for z in range(floor - height, floor):
            put(x, y, z)
        tops.append((x, y, floor - height))
    elif structure == "wallneck":
        # Kiril's tower: a 5x5 platform held off a building's wall, twelve
        # blocks up, by a neck of two voxels in mid-air (nothing stands near
        # them: a climber must stack up to the neck to claw it). ``floor``
        # is the street at the wall's foot; ``direction`` points away from
        # the wall.
        dx, dy = direction
        top = floor - height
        neck = [(x + dx * k, y + dy * k, top) for k in (1, 2)]
        for cx, cy, cz in neck:
            put(cx, cy, cz)
        px, py = x + dx * 5, y + dy * 5
        for cx in range(px - 2, px + 3):
            for cy in range(py - 2, py + 3):
                put(cx, cy, top)
        tops.append((px, py, top))
    elif structure == "treebridge":
        # Kiril's case: a tree (3x3 trunk 12 high, a leafy crown of radius
        # 5) and, built out from the crown, a one-wide walkway ``span``
        # blocks long with a short pillar at its end. The survivor stands
        # 22 blocks up and ``span`` blocks from the only thing that holds
        # the whole structure to the ground: the trunk's nine base voxels.
        dx, dy = direction
        for cx in range(x - 1, x + 2):
            for cy in range(y - 1, y + 2):
                for z in range(floor - 12, floor):
                    put(cx, cy, z)
        centre = (x, y, floor - 15)
        for cx in range(x - 5, x + 6):
            for cy in range(y - 5, y + 6):
                for cz in range(floor - 20, floor - 9):
                    if (cx - centre[0]) ** 2 + (cy - centre[1]) ** 2 + (cz - centre[2]) ** 2 <= 25:
                        put(cx, cy, cz)
        for k in range(4, bridge_span + 1):
            put(x + dx * k, y + dy * k, floor - 18)
        ex, ey = x + dx * bridge_span, y + dy * bridge_span
        for z in range(floor - 22, floor - 18):
            put(ex, ey, z)
        tops.append((ex, ey, floor - 22))
    elif structure == "roofbridge":
        # The same walkway built out from a building's roof: it lies on the
        # roof for its first blocks, then hangs over the street, pillar at
        # the end. ``floor`` is the roof.
        dx, dy = direction
        for k in range(0, bridge_span + 1):
            put(x + dx * k, y + dy * k, floor - 1)
        ex, ey = x + dx * bridge_span, y + dy * bridge_span
        for z in range(floor - 5, floor - 1):
            put(ex, ey, z)
        tops.append((ex, ey, floor - 5))
    elif structure == "pillar":
        for px in (x - 3, x + 3):
            for z in range(floor - 12, floor):
                put(px, y, z)
            tops.append((px, y, floor - 12))
    elif structure == "platform":
        top = floor - 14
        for cx in range(x - 3, x + 4):
            for cy in range(y - 3, y + 4):
                put(cx, cy, top)
        for lx, ly in ((x - 3, y - 3), (x + 2, y - 3), (x - 3, y + 2), (x + 2, y + 2)):
            for cx in (lx, lx + 1):
                for cy in (ly, ly + 1):
                    for z in range(top + 1, floor):
                        put(cx, cy, z)
        tops.extend([(x, y, top), (x + 1, y - 1, top)])
    elif structure == "fortress":
        top = floor - 16
        for cx in range(x - 2, x + 3):
            for cy in range(y - 2, y + 3):
                for z in range(top, floor):
                    put(cx, cy, z)
        tops.extend([(x, y, top), (x + 1, y + 1, top)])
    elif structure == "tower":
        top = floor - 14
        for cx in range(x - 1, x + 2):
            for cy in range(y - 1, y + 2):
                for z in range(top, floor):
                    put(cx, cy, z)
        tops.extend([(x, y, top), (x - 1, y + 1, top)])
    elif structure == "roof":
        # A 9x9 hut, walls 6 high, flat roof, and a two-wide outside
        # staircase up to it: the survivors can be walked to (nothing is
        # "isolated"), and the roof also stands on thin walls.
        top = floor - 7
        for cx in range(x - 4, x + 5):
            for cy in range(y - 4, y + 5):
                put(cx, cy, top)
                if cx in (x - 4, x + 4) or cy in (y - 4, y + 4):
                    for z in range(top + 1, floor):
                        put(cx, cy, z)
        for step in range(7):
            # Along the +x wall, rising toward -y; the last step is roof high.
            for sx in (x + 5, x + 6):
                for z in range(floor - 1 - step, floor):
                    put(sx, y + 4 - step, z)
        tops.extend([(x, y, top), (x - 2, y + 2, top)])
    elif structure == "bridge":
        # A three-wide deck 8 up on two 3x3 piers, with a staircase at its
        # west end: reachable on foot the long way, cheap to drop.
        top = floor - 8
        for cx in range(x - 8, x + 9):
            for cy in (y - 1, y, y + 1):
                put(cx, cy, top)
                if cx <= x - 6 or cx >= x + 6:
                    for z in range(top + 1, floor):
                        put(cx, cy, z)
        for step in range(1, 8):
            for cy in (y - 1, y, y + 1):
                for z in range(top + step, floor):
                    put(x - 8 - step, cy, z)
        tops.extend([(x + 2, y, top), (x + 4, y, top)])
    elif structure == "yard":
        # A 15x15 yard behind a wall 5 high and 1 thick; a 3-wide gate in
        # the middle of its -y side. The horde starts on the +y side.
        for cx in range(x - 8, x + 9):
            for cy in range(y - 8, y + 9):
                if not (cx in (x - 8, x + 8) or cy in (y - 8, y + 8)):
                    continue
                if cy == y - 8 and abs(cx - x) <= 1:
                    continue
                for z in range(floor - 5, floor):
                    put(cx, cy, z)
        tops.extend([(x, y, floor), (x + 2, y - 2, floor)])
    elif structure == "dropin":
        # A 1x1 pillar 10 high. Beside it, three blocks above its top and
        # touching nothing of it, runs a 2-wide deck on posts; its staircase
        # comes down 20 blocks away. A zombie can walk all that way round
        # and drop onto the pillar, or claw one voxel.
        top = floor - 10
        for z in range(top, floor):
            put(x, y, z)
        deck = top - 3
        for cx in range(x - 1, x + 21):
            for cy in (y + 1, y + 2):
                put(cx, cy, deck)
                if cx % 8 == (x + 4) % 8 and cy == y + 2:
                    for z in range(deck + 1, floor + 3):
                        put(cx, cy, z)
        for step in range(1, 13):
            for cy in (y + 1, y + 2):
                for z in range(deck + step, floor + 3):
                    put(x + 20 + step, cy, z)
        tops.append((x, y, top))
    elif structure == "ledge":
        # A 3x3 block two high: a jump for anyone, a "platform" to a flood
        # that only walks.
        for cx in range(x - 1, x + 2):
            for cy in range(y - 1, y + 2):
                for z in (floor - 2, floor - 1):
                    put(cx, cy, z)
        tops.extend([(x, y, floor - 2), (x + 1, y + 1, floor - 2)])
    elif structure == "pit":
        # A 5x5 pit three deep: one step off the rim.
        for cx in range(x - 2, x + 3):
            for cy in range(y - 2, y + 3):
                for z in range(floor - 4, floor + 3):
                    clear(cx, cy, z)
                put(cx, cy, floor + 3)
        tops.extend([(x, y, floor + 3), (x + 1, y - 1, floor + 3)])
    elif structure == "room":
        # Sealed 5x5 room: walls 1 thick, 3 high inside, roofed.
        for cx in range(x - 3, x + 4):
            for cy in range(y - 3, y + 4):
                put(cx, cy, floor - 4)
                if cx in (x - 3, x + 3) or cy in (y - 3, y + 3):
                    for z in range(floor - 3, floor):
                        put(cx, cy, z)
                else:
                    for z in range(floor - 3, floor):
                        clear(cx, cy, z)
        tops.append((x, y, floor))
    return tops, built


def spawn_idle_survivor(server, position) -> Player:
    connection = _IdleConnection(server)
    player = Player(server.get_next_player_id(), f"Survivor{len(server.players)}",
                    SURVIVOR_TEAM, DEFAULT_WEAPON_TOOL, connection)
    connection.player = player
    player.apply_class_selection(normalize_class_selection(int(C.CLASS_SOLDIER)))
    server.players[player.id] = player
    server.teams[SURVIVOR_TEAM].add_player(player)
    server.connections[object()] = connection
    player.spawn(*position)
    server._broadcast_create_player(player, position)
    return player


def _islet_centre(label: dict, islet: int, offset: int = 0) -> tuple[int, int]:
    cells = sorted(c for c, v in label.items() if v == islet)
    cx = sum(c[0] for c in cells) / len(cells)
    cy = sum(c[1] for c in cells) / len(cells)
    ranked = sorted(cells, key=lambda c: (c[0] - cx) ** 2 + (c[1] - cy) ** 2)
    return ranked[min(len(ranked) - 1, 5 * (offset % 3))]


def _wade_site(world, label: dict, mainland: int, rng: random.Random) -> tuple[int, int, int]:
    """An open-water column 8-10 blocks off the mainland shore."""

    shore = [c for c, v in label.items() if v == mainland and any(
        world.is_water_column(c[0] + dx, c[1] + dy)
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)))]
    rng.shuffle(shore)
    for sx, sy in shore:
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            cells = [(sx + dx * k, sy + dy * k) for k in range(1, 15)]
            if not all(3 < x < 508 and 3 < y < 508 and world.is_water_column(x, y)
                       for x, y in cells):
                continue
            if all(world.is_water_column(x + ox, y + oy)
                   for x, y in cells[5:] for ox in (-3, 0, 3) for oy in (-3, 0, 3)):
                return cells[8][0], cells[8][1], 239
    raise RuntimeError("map has no open water off the mainland")


def _mainland_anchor(world, label: dict, mainland: int, site, radius: float, angle: float):
    best = None
    for attempt in range(24):
        a = angle + attempt * math.pi / 12.0
        x = int(site[0] + radius * math.cos(a))
        y = int(site[1] + radius * math.sin(a))
        if label.get((x, y)) == mainland:
            best = (x, y)
            break
    if best is None:
        best = min((c for c, v in label.items() if v == mainland),
                   key=lambda c: abs(math.hypot(c[0] - site[0], c[1] - site[1]) - radius))
    return (best[0] + 0.5, best[1] + 0.5, world.get_height(*best) - STAND)


class _Clock:
    """Replace the wall clocks with one simulated clock; restore on exit.

    The server, mode and director are created inside it, so no timestamp
    of the machine's own uptime leaks into a run: the same seed replays the
    same round on every runner.
    """

    BASE = 1_000_000.0
    WALL = 1_700_000_000.0

    def __init__(self, *, real_budgets: bool = False) -> None:
        self._monotonic = time.monotonic
        self._time = time.time
        self._perf_counter = time.perf_counter
        self._real_budgets = bool(real_budgets)
        self.now = self.BASE

    def __enter__(self):
        time.monotonic = lambda: self.now
        time.time = lambda: self.WALL + (self.now - self.BASE)
        if not self._real_budgets:
            # The director's perception publisher and the siege analysis are
            # rationed in real milliseconds. On a busy machine they ration
            # harder, and the round plays out differently from one run to
            # the next. A counter that stands still within a tick models an
            # unloaded server; ``--real-budgets`` keeps the real one.
            time.perf_counter = lambda: self.now
        return self

    def __exit__(self, *exc):
        time.monotonic = self._monotonic
        time.time = self._time
        time.perf_counter = self._perf_counter
        return False


class _MotorProbe:
    """What the motor pressed for one team and what it cost the bodies.

    The sprint share of the ticks a movement key was held, the ground covered
    in those ticks, and every fall that hurt or killed.
    """

    def __init__(self, team: int) -> None:
        self.team = int(team)
        self.move_ticks = 0
        self.sprint_ticks = 0
        self.blocks = 0.0
        self.seconds = 0.0
        self.fall_hits = 0
        self.fall_damage = 0
        self.fall_deaths = 0
        self.deaths = 0
        self.spawns = 0
        self._spawn_base = 0
        self.ticks = 0
        self.queue_ticks = 0
        self._last: dict[int, tuple[float, float, float]] = {}
        self._originals = None

    def mark_baseline(self) -> None:
        """Spawns from here on are respawns (call once the horde stands)."""

        self._spawn_base = self.spawns

    def install(self) -> None:
        set_state = BotDirector.__dict__["_set_movement_state"].__func__
        damage, die, spawn = Player.damage, Player.die, Player.spawn
        self._originals = (BotDirector.__dict__["_set_movement_state"], damage, die, spawn)
        fall = int(C.KILL.FALL_KILL)
        probe = self

        def pressed(runtime, values):
            player = runtime.player
            if int(getattr(player, "team", -1)) == probe.team:
                here = (float(player.x), float(player.y), time.monotonic())
                probe.ticks += 1
                role = str(getattr(runtime.intent, "debug_role", ""))
                probe.queue_ticks += int(role.endswith((":breach_assist_queue", ":breach_yield")))
                before = probe._last.get(int(player.id))
                probe._last[int(player.id)] = here
                if any(values[:4]):
                    probe.move_ticks += 1
                    probe.sprint_ticks += int(bool(values[7]))
                    # (A gap is a death or a respawn, not travel.)
                    if before is not None and here[2] - before[2] <= 0.25:
                        probe.blocks += math.hypot(here[0] - before[0], here[1] - before[1])
                        probe.seconds += here[2] - before[2]
            return set_state(runtime, values)

        def hurt(player, amount, source=None, kill_type=0, **kwargs):
            before = int(getattr(player, "health", 0))
            result = damage(player, amount, source, kill_type, **kwargs)
            if (int(kill_type) == fall and int(getattr(player, "team", -1)) == probe.team
                    and getattr(player, "is_bot", False)):
                lost = before - int(getattr(player, "health", 0))
                if lost > 0:
                    probe.fall_hits += 1
                    probe.fall_damage += lost
            return result

        def died(player, killer=None, kill_type=0):
            if (player.alive and int(getattr(player, "team", -1)) == probe.team
                    and getattr(player, "is_bot", False)):
                probe.deaths += 1
                if int(kill_type) == fall:
                    probe.fall_deaths += 1
            return die(player, killer, kill_type)

        def spawned(player, x, y, z):
            if (int(getattr(player, "team", -1)) == probe.team
                    and getattr(player, "is_bot", False)):
                probe.spawns += 1
            return spawn(player, x, y, z)

        BotDirector._set_movement_state = staticmethod(pressed)
        Player.damage, Player.die, Player.spawn = hurt, died, spawned

    def remove(self) -> None:
        if self._originals is not None:
            (BotDirector._set_movement_state, Player.damage, Player.die,
             Player.spawn) = self._originals
            self._originals = None

    def report(self) -> dict:
        return {
            "motor_sprint_share": round(self.sprint_ticks / max(1, self.move_ticks), 3),
            "travel_speed": round(self.blocks / max(1e-9, self.seconds), 2),
            "breach_queue_share": round(self.queue_ticks / max(1, self.ticks), 4),
            "fall_hits": self.fall_hits,
            "fall_damage": self.fall_damage,
            "fall_deaths": self.fall_deaths,
            "zombie_deaths": self.deaths,
            "zombie_respawns": max(0, self.spawns - self._spawn_base),
        }


class _ArmedSurvivor:
    """An idle survivor who shoots back and rebuilds like a human.

    Every 0.6-0.9 s the nearest zombie with a clear line of sight within
    ``RANGE`` blocks takes one rifle shot through ``CombatSystem.handle_shot``:
    the validated ShootPacket path a retail client uses (team, alive, tool,
    cadence, clip, origin and aim gates), so a hit lands exactly as a
    player's would and the rifle's spread can miss. An empty clip is
    reloaded the way a client requests it. Every 4 s one missing voxel of the
    structure within arm's reach under the survivor's feet is put back with
    ``world.set_block`` (the player rebuilding), at most ``REPAIR_CAP`` times
    and never into a body or once the base has fallen.
    """

    RANGE = 40.0
    REACH = 6.0
    REPAIR_CAP = 20
    TORSO_Z = 0.3  # stock hitscan torso offset from the body position

    def __init__(self, server, survivor, built, support, rng: random.Random,
                 *, color: int = 0x777777, hit_chance: float = 1.0) -> None:
        self.server = server
        self.survivor = survivor
        self.hit_chance = max(0.0, min(1.0, float(hit_chance)))
        self.built = [tuple(int(v) for v in cell) for cell in built]
        self.support = tuple(tuple(int(v) for v in cell) for cell in support)
        self.rng = rng
        self.color = int(color)
        self.next_shot_at = 1.0
        self.next_repair_at = 4.0
        self.shots = 0
        self.hits = 0
        self.kills = 0
        self.repairs = 0
        self.repaired: set = set()
        self.blocked: Counter = Counter()
        # A client aims in one input frame and fires referencing it: the aim
        # is taken here, the shot goes out on the next tick with that frame's
        # loop label, so the server's aim gate compares like with like.
        self._pending = None

    def tick(self, t: float, zombies) -> None:
        s = self.survivor
        if not (s.alive and s.spawned and int(s.team) == SURVIVOR_TEAM):
            self._pending = None
            return
        if self._pending is not None:
            direction = self._pending
            self._pending = None
            self._fire(direction, zombies)
        elif t >= self.next_shot_at:
            self.next_shot_at = t + self.rng.uniform(0.6, 0.9)
            self._aim(zombies)
        if t >= self.next_repair_at:
            self.next_repair_at = t + 4.0
            self._repair()

    def _target(self, zombies):
        s = self.survivor
        world = self.server.world_manager
        eye = tuple(float(v) for v in s.eye)
        for z in sorted((b for b in zombies if b.alive and b.spawned),
                        key=lambda b: math.dist(b.position, s.position)):
            aim = (float(z.x), float(z.y), float(z.z) + self.TORSO_Z)
            dx, dy, dz = aim[0] - eye[0], aim[1] - eye[1], aim[2] - eye[2]
            dist = math.sqrt(dx * dx + dy * dy + dz * dz)
            if dist > self.RANGE:
                return None
            if dist < 1e-6:
                continue
            direction = (dx / dist, dy / dist, dz / dist)
            if world.raycast(eye[0], eye[1], eye[2], *direction,
                             max(0.0, dist - 0.6)) is not None:
                continue  # terrain in the way
            aim = self._slip(eye, aim, dist)
            dx, dy, dz = aim[0] - eye[0], aim[1] - eye[1], aim[2] - eye[2]
            dist = math.sqrt(dx * dx + dy * dy + dz * dz) or 1.0
            return z, eye, (dx / dist, dy / dist, dz / dist)
        return None

    def _slip(self, eye, aim, dist):
        """A human's miss: with probability 1 - hit_chance the aim point is
        slid 0.8-1.3 blocks off the body, across the line of sight."""

        if self.hit_chance >= 1.0 or self.rng.random() < self.hit_chance:
            return aim
        dx, dy = aim[0] - eye[0], aim[1] - eye[1]
        span = math.hypot(dx, dy) or 1.0
        side = self.rng.choice((-1.0, 1.0)) * self.rng.uniform(0.8, 1.3)
        lift = self.rng.uniform(-0.6, 0.9)
        return (aim[0] - dy / span * side, aim[1] + dx / span * side, aim[2] + lift)

    def _aim(self, zombies) -> None:
        s = self.survivor
        if int(getattr(s, "ammo_clip", 0)) <= 0:
            if not bool(getattr(s, "reloading", False)):
                self.server.combat.handle_weapon_reload(s)
            self.blocked["reloading"] += 1
            return
        found = self._target(zombies)
        if found is None:
            self.blocked["no_target_in_sight"] += 1
            return
        _target, _eye, direction = found
        s.set_orientation_vector(*direction)
        self._pending = direction

    def _fire(self, direction, zombies) -> None:
        s = self.survivor
        eye = tuple(float(v) for v in s.eye)
        # The body the aim frame was taken on; a step later it is still the
        # nearest one on that bearing unless it died or moved out of sight.
        target = None
        best = math.inf
        for z in zombies:
            if not (z.alive and z.spawned):
                continue
            to = (float(z.x) - eye[0], float(z.y) - eye[1],
                  float(z.z) + self.TORSO_Z - eye[2])
            dist = math.sqrt(sum(c * c for c in to))
            if dist < 1e-6 or dist > self.RANGE:
                continue
            dot = sum(to[i] / dist * direction[i] for i in range(3))
            if dot > (0.98 if self.hit_chance >= 1.0 else 0.9) and dist < best:
                target, best = z, dist
        if target is None:
            self.blocked["target_moved"] += 1
            return
        packet = ShootPacket()
        loop = s.applied_loop
        packet.loop_count = int(loop if loop is not None else self.server.loop_count)
        packet.shooter_id = int(s.id)
        packet.shot_on_world_update = 0
        packet.x, packet.y, packet.z = eye
        packet.ori_x, packet.ori_y, packet.ori_z = direction
        packet.damage = 0
        packet.penetration = 0
        packet.affect_shooter = 0
        packet.secondary = 0
        packet.seed = self.rng.randrange(256)
        before = int(target.health)
        alive_before = bool(target.alive)
        clip_before = int(getattr(s, "ammo_clip", 0))
        self.shots += 1
        accepted = bool(self.server.combat.handle_shot(s, packet))
        # A consumed round means the server fired the shot; otherwise it
        # refused it (cadence, origin, aim gate) and no bullet left the gun.
        fired = accepted or int(getattr(s, "ammo_clip", 0)) < clip_before
        if int(target.health) < before or (alive_before and not target.alive):
            self.hits += 1
            if alive_before and not target.alive:
                self.kills += 1
        elif accepted:
            self.blocked["hit_something_else"] += 1
        elif fired:
            self.blocked["missed"] += 1
        else:
            self.blocked["refused_by_server"] += 1

    def _repair(self) -> None:
        if self.repairs >= self.REPAIR_CAP or not self.built:
            return
        s = self.survivor
        world = self.server.world_manager
        if self.support and not any(world.get_solid(*cell) for cell in self.support):
            return  # the base fell with the player: nothing left to stand on
        column = (int(math.floor(s.x)), int(math.floor(s.y)))
        top = min((cell[2] for cell in self.support), default=-1)
        bodies = [(float(p.x), float(p.y), float(p.z))
                  for p in self.server.players.values() if p.alive and p.spawned]
        candidates = []
        for cell in self.built:
            if cell in self.repaired or world.get_solid(*cell):
                continue
            if abs(cell[0] - column[0]) > 1 or abs(cell[1] - column[1]) > 1:
                continue
            if cell[2] < top:
                continue  # above the feet: not the support
            centre = (cell[0] + 0.5, cell[1] + 0.5, cell[2] + 0.5)
            distance = math.dist((float(s.x), float(s.y), float(s.z)), centre)
            if distance > self.REACH:
                continue
            if any(abs(b[0] - centre[0]) < 0.9 and abs(b[1] - centre[1]) < 0.9
                   and b[2] - 0.5 <= centre[2] <= b[2] + 3.0 for b in bodies):
                continue  # a body stands there; a client could not place it
            candidates.append((distance, cell))
        if not candidates:
            return
        _, cell = min(candidates)
        if world.set_block(cell[0], cell[1], cell[2], True, self.color):
            self.repaired.add(cell)
            self.repairs += 1

    def report(self) -> dict:
        s = self.survivor
        return {
            "survivor_shots": self.shots,
            "survivor_hits": self.hits,
            "survivor_kills": self.kills,
            # Clip + reserve left: a rifle carries 10 + 30 rounds and there is
            # no resupply on a pillar, so a long run ends with a dry gun.
            "survivor_ammo_left": int(getattr(s, "ammo_clip", 0)) + int(getattr(s, "ammo_reserve", 0)),
            "repairs": self.repairs,
            "shots_blocked": dict(self.blocked),
        }


class _StepClock:
    """A budget counter that charges a fixed cost for every reading."""

    def __init__(self, step: float) -> None:
        self.now = 0.0
        self.step = float(step)

    def __call__(self) -> float:
        self.now += self.step
        return self.now


def scenario_args(**overrides) -> argparse.Namespace:
    """Default options of one run, for callers that are not the CLI."""

    options = dict(map="ArcticBase", scenario="ground", zombies=8, survivors=2,
                   seconds=120.0, seed=7, baseline=False, json=None, trace=False,
                   trace_bot=None, real_budgets=False, beacon=False,
                   height=64, bridge_span=64, bridge_width=1, armed=False)
    unknown = set(overrides) - set(options)
    if unknown:
        raise TypeError(f"unknown scenario options: {sorted(unknown)}")
    options.update(overrides)
    return argparse.Namespace(**options)


async def run_scenario(args) -> dict:
    if getattr(args, "height", None) is None:
        args.height = 8 if args.scenario in ("roofpillar", "wallpillar") else 64
    if not hasattr(args, "armed"):
        args.armed = False
    random_state = random.getstate()
    horde_objectives = BotDirector._objectives_zombie_horde
    probe = args.probe = _MotorProbe(ZOMBIE_TEAM)
    try:
        probe.install()
        with _Clock(real_budgets=args.real_budgets) as clock:
            return await _run_scenario(args, clock)
    finally:
        probe.remove()
        BotDirector._objectives_zombie_horde = horde_objectives
        random.setstate(random_state)


async def _run_scenario(args, clock: _Clock) -> dict:
    rng = random.Random(args.seed)
    random.seed(args.seed)
    config = load_config(ROOT / "config.toml")
    config.default_mode = "zom"
    config.default_map = args.map
    config.respawn_time = 3.0
    config.bots.population_mode = "admin"
    config.bots.max_bots = int(args.zombies) + int(args.survivors) + 2
    config.bots.seed = int(args.seed)
    # The official Zombie fleet profile (configs/official-zombie.toml). With
    # the stock 10 Hz perception the 8 Hz decision gate skips every other
    # frame whenever a frame lands a tick early.
    config.bots.perception_hz = 8.0
    config.bots.main_thread_budget_ms = 1.5
    config.max_players = max(config.max_players, args.zombies + args.survivors + 8)
    if hasattr(config, "revival"):
        try:
            config.revival.enabled = False
        except AttributeError:
            pass
    if args.baseline:
        BotDirector._objectives_zombie_horde = lambda self, mode: []
    server = BattleSpadesServer(config)
    if not server.world_manager.load_map(config.default_map):
        raise RuntimeError("map did not load")
    world = server.world_manager
    scenario = args.scenario
    structure = scenario if scenario in STRUCTURES else ""
    label: dict = {}
    sizes: list[int] = []
    if scenario in ("islands", "sea", "wade", "offshore"):
        label, sizes = land_components(world)
    mainland = max(range(len(sizes)), key=lambda i: sizes[i]) if sizes else -1

    server.mode = get_mode_class("zom")(server)
    await server.mode.on_mode_start()
    supervisor = InlineSupervisor()
    director = BotDirector(server, supervisor=supervisor)
    server.bots = director
    # The horde's timers (stall watchdog, analysis freshness) must run on the
    # simulated clock like everything else. Its millisecond of analysis per
    # snapshot is charged by the step, at the measured cost of one (0.07 ms
    # for six flooded cells on a busy machine, rounded up), so an analysis
    # arrives as late here as it does on a live server.
    director._zombie_siege = ZombieSiegeService(
        clock=lambda: clock.now,
        budget_clock=None if args.real_budgets else _StepClock(0.0001))
    await director.start(initial_count=0)

    survivors: list[Player] = []
    survivor_bots: list = []
    built: list = []
    site = None
    wall_direction = None
    if scenario == "roofpillar":
        x, y, floor = site = _rooftop_site(world, rng)
    elif scenario == "wallpillar":
        x, y, floor, wall_direction = _wall_site(world, rng)
        site = (x, y, floor)
    bridge_direction = (1, 0)
    if scenario == "wallneck":
        x, y, floor, wall_direction = _wall_site(world, rng)
        site = (x, y, floor)
        # The neck starts in the wall's face: the wall column beside the
        # street cell, two blocks out from it the platform hangs over the
        # street. ``_wall_site`` gives the street cell and the direction of
        # the wall; build from the wall column outward.
        wx, wy = x + wall_direction[0], y + wall_direction[1]
        away = (-wall_direction[0], -wall_direction[1])
        # The neck leaves the wall's face below its top, twelve up when the
        # wall allows, at a height where the wall is really solid (London
        # facades have windows), so the structure hangs from the building.
        neck_z = max(floor - 12, world.get_height(wx, wy) + 2)
        for candidate in sorted(range(neck_z - 3, floor - 3), key=lambda z: abs(z - neck_z)):
            if world.get_solid(wx, wy, candidate) and not world.get_solid(x, y, candidate):
                neck_z = candidate
                break
        tops, built = build_structure(world, scenario, wx, wy, floor, height=floor - neck_z,
                                      direction=away)
        for tx, ty, tz in tops[: args.survivors]:
            survivors.append(spawn_idle_survivor(server, (tx + 0.5, ty + 0.5, tz - STAND)))
    elif scenario in ("treebridge", "roofbridge"):
        if scenario == "treebridge":
            x, y, floor = site = _flat_site(world, rng, level=True, span=6)
            clearance = floor - 18
        else:
            x, y, floor = site = _rooftop_site(world, rng)
            clearance = floor - 1
        span = int(args.bridge_span) if args.bridge_span != 64 else 40
        # A way out over which nothing stands as high as the walkway.
        for candidate in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            if all(0 < x + candidate[0] * k < 511 and 0 < y + candidate[1] * k < 511
                   and world.get_height(x + candidate[0] * k, y + candidate[1] * k) > clearance + 1
                   for k in range(3, span + 1)):
                bridge_direction = candidate
                break
        tops, built = build_structure(world, scenario, x, y, floor, bridge_span=span,
                                      direction=bridge_direction)
        for tx, ty, tz in tops[: args.survivors]:
            survivors.append(spawn_idle_survivor(server, (tx + 0.5, ty + 0.5, tz - STAND)))
    elif structure and scenario in ("roofpillar", "wallpillar"):
        tops, built = build_structure(world, structure, x, y, floor, height=args.height)
        for tx, ty, tz in tops[: args.survivors]:
            survivors.append(spawn_idle_survivor(server, (tx + 0.5, ty + 0.5, tz - STAND)))
    elif structure:
        x, y, floor = site = _flat_site(world, rng, level=True,
                                        span=16 if scenario == "dropin" else 8)
        if scenario == "dropin":
            x -= 12  # the pillar stands at one end of the flat, the stairs at the other
        tops, built = build_structure(world, structure, x, y, floor,
            height=args.height, bridge_span=args.bridge_span, bridge_width=args.bridge_width)
        for tx, ty, tz in tops[: args.survivors]:
            survivors.append(spawn_idle_survivor(server, (tx + 0.5, ty + 0.5, tz - STAND)))
    elif scenario in ("ground", "open"):
        used: list = []
        for _ in range(args.survivors):
            sx, sy, sf = _flat_site(world, rng, span=2, avoid=used, level=scenario == "open")
            used.append((sx, sy))
            survivors.append(spawn_idle_survivor(server, (sx + 0.5, sy + 0.5, sf - STAND)))
    elif scenario in ("islands", "sea"):
        for _ in range(args.survivors):
            position = tuple(float(v) for v in world.get_spawn_point(SURVIVOR_TEAM))
            survivors.append(spawn_idle_survivor(server, position))
    elif scenario == "wade":
        site = _wade_site(world, label, mainland, rng)
        for index in range(args.survivors):
            wx, wy = site[0] + (index % 2) * 2, site[1] + (index // 2) * 2
            survivors.append(spawn_idle_survivor(
                server, (wx + 0.5, wy + 0.5, 240.0 - STAND)))
    elif scenario == "offshore":
        offshore = sorted((i for i in range(len(sizes)) if i != mainland and sizes[i] >= 40),
                          key=lambda i: -sizes[i])
        if not offshore:
            raise RuntimeError("map has no offshore islet")
        for index in range(args.survivors):
            cell = _islet_centre(label, offshore[index % len(offshore)], index // len(offshore))
            survivors.append(spawn_idle_survivor(
                server, (cell[0] + 0.5, cell[1] + 0.5, world.get_height(*cell) - STAND)))
    elif scenario == "round":
        for index in range(args.survivors):
            bot = await director.add_bot(team=SURVIVOR_TEAM, name=f"Sam{index}",
                                         difficulty="hard")
            if bot is None:
                raise RuntimeError("could not add survivor bot")
            survivor_bots.append(bot)
            survivors.append(bot)
    for survivor in survivors:
        if survivor not in survivor_bots:
            server.mode._assign_survivor(survivor)
    server.mode.phase = ZombiePhase.ACTIVE
    server.mode.start_time = time.time()
    server.mode.time_limit = 100_000

    zombies = []
    zombie_start_levels: list[float] = []
    try:
        lo, hi = (float(v) for v in str(getattr(args, "spawn_distance", "16,26")).split(","))
    except ValueError:
        lo, hi = 16.0, 26.0
    spawn_range = (min(lo, hi), max(lo, hi))
    islets = []
    if scenario == "islands":
        islets = sorted((i for i in range(len(sizes)) if i != mainland and sizes[i] >= 12),
                        key=lambda i: -sizes[i])
        if not islets:
            raise RuntimeError("map has no offshore islet")
    for index in range(args.zombies):
        bot = await director.add_bot(team=ZOMBIE_TEAM, name=f"Zed{index}", difficulty="hard")
        if bot is None:
            raise RuntimeError("could not add zombie bot")
        if scenario == "islands":
            cell = _islet_centre(label, islets[index % len(islets)], index // len(islets))
            bot.set_position(cell[0] + 0.5, cell[1] + 0.5,
                             world.get_height(*cell) - STAND)
        elif scenario == "wade":
            # On the mainland, 20-30 blocks inland of the wading survivors.
            angle = rng.uniform(0, 2 * math.pi)
            anchor = _mainland_anchor(world, label, mainland, site,
                                      rng.uniform(20, 30), angle)
            bot.set_position(*anchor)
        elif structure:
            x, y, floor = site
            angle = rng.uniform(0, 2 * math.pi)
            radius = rng.uniform(*spawn_range)
            if scenario == "yard":
                # Everyone starts on the side away from the gate.
                angle = rng.uniform(0.25 * math.pi, 0.75 * math.pi)
            elif scenario == "dropin":
                # ... and here on the side away from the deck.
                angle = rng.uniform(-0.75 * math.pi, -0.25 * math.pi)
            elif wall_direction is not None:
                away = math.atan2(-wall_direction[1], -wall_direction[0])
                angle = away + rng.uniform(-0.6 * math.pi, 0.6 * math.pi)
            anchor = world.dry_ground_anchor(x + radius * math.cos(angle),
                                             y + radius * math.sin(angle), search=12)
            if scenario in ("roofpillar", "wallpillar"):
                anchor = _street_anchor(world, rng, x, y, floor, scenario, wall_direction, anchor,
                                        spawn_range=spawn_range)
            elif scenario == "roofbridge":
                anchor = _street_anchor(world, rng, x, y, floor, "roofpillar", None, anchor,
                                        spawn_range=spawn_range)
            elif scenario == "wallneck":
                anchor = _street_anchor(world, rng, x, y, floor, "wallpillar", wall_direction, anchor,
                                        spawn_range=spawn_range)
            bot.set_position(*anchor)
            zombie_start_levels.append(round(float(anchor[2]), 1))
        zombies.append(bot)

    supports = {s.id: support_cells(world.get_solid, s.position) for s in survivors}
    start_health = {s.id: s.health for s in survivors}
    survivor_start_z = round(float(survivors[0].z), 1) if survivors else None
    elevated = scenario in ELEVATED
    metrics = _Metrics(zombies, survivors, clock.now,
                       horde_floor=(site[2] if elevated and site else None))
    probe = getattr(args, "probe", None)
    if probe is not None:
        probe.mark_baseline()
    armed = None
    if args.armed and survivors and survivors[0] not in survivor_bots:
        armed = _ArmedSurvivor(server, survivors[0], built,
                               supports.get(survivors[0].id, ()),
                               random.Random(int(args.seed) * 7919 + 1),
                               hit_chance=float(getattr(args, "hit_chance", 1.0)))
    if args.trace_bot is not None:
        supervisor.trace_bot = int(zombies[args.trace_bot].id)
        supervisor.trace_origin = clock.now
    tick = server.tick_interval
    steps = int(args.seconds / tick)
    tick_ms: list[float] = []
    sample_every = max(1, int(round(SAMPLE_SECONDS / tick)))
    siege_seconds = [0.0, 0.0, 0]
    siege = None
    beacon = scenario in ("islands", "sea") or args.beacon
    infected: dict[int, float] = {}
    wall_started = _perf()
    for step in range(steps):
        t = step * tick
        clock.now += tick
        server.loop_count += 1
        # A real idle client still sends one input frame per physics step.
        # Its gravity, falling damage and resulting infection must emerge
        # from that same movement path; never move a survivor for a verdict.
        for survivor in survivors:
            if not survivor.is_bot and survivor.alive and survivor.spawned:
                survivor.record_input_frame(server.loop_count, (False,)*8,
                    (survivor.o_x,survivor.o_y,survivor.o_z),
                    received_server_tick=server.loop_count)
        started = _perf()
        await server.simulation_runtime.step()
        tick_ms.append((_perf() - started) * 1000.0)
        await asyncio.sleep(0)
        if siege is None:
            siege = director._zombie_siege
            _time_siege(siege, siege_seconds)
        living = [s for s in survivors
                  if s.alive and s.spawned and int(s.team) == SURVIVOR_TEAM]
        if armed is not None:
            armed.tick(t, zombies)
        for s in survivors:
            support = supports.get(s.id)
            if (structure and s not in survivor_bots and support and s.alive
                    and int(s.team) == SURVIVOR_TEAM and step % 6 == 0
                    and not any(world.get_solid(*cell) for cell in support)):
                metrics.collapse(t)
                supports[s.id] = ()
            if s.health < start_health[s.id] or not s.alive:
                metrics.hit(t)
                if beacon and s.alive:
                    # Crossing scenarios time every zombie: the survivors
                    # only mark the far shore and must outlive the first
                    # arrival.
                    s.health = start_health[s.id]
            if int(s.team) != SURVIVOR_TEAM and s.id not in infected:
                infected[s.id] = round(t, 2)
        if step % sample_every == 0:
            metrics.sample(t, director, living, world, label, mainland, elevated)
            if args.trace and step % (sample_every * 4) == 0:
                _trace(t, director, zombies)
        if not living:
            break
    duration = (step + 1) * tick
    result = {
        "map": args.map, "scenario": scenario, "zombies": args.zombies,
        "survivors": len(survivors), "baseline": bool(args.baseline),
        "armed": bool(args.armed), "height": int(args.height),
        "seed": args.seed, "site": site, "duration_s": round(duration, 1),
        "zombie_start_levels": zombie_start_levels,
        "survivor_start_z": survivor_start_z,
        "wall_s": round(_perf() - wall_started, 1),
        "infected_s": {str(k): v for k, v in infected.items()},
        "kills": len(infected),
        "all_infected_s": (max(infected.values()) if infected
                           and len(infected) == len(survivors) else None),
    }
    result.update(metrics.report())
    if getattr(args, "probe", None) is not None:
        result.update(args.probe.report())
    if armed is not None:
        result.update(armed.report())
    tick_ms.sort()
    result["tick_p50_ms"] = round(tick_ms[len(tick_ms) // 2], 2)
    result["tick_p99_ms"] = round(tick_ms[int(0.99 * (len(tick_ms) - 1))], 2)
    result["tick_max_ms"] = round(tick_ms[-1], 2)
    result["sprint_decision_share"] = round(
        supervisor.sprint_decisions / max(1, supervisor.travel_decisions), 3)
    result["walk_reasons"] = dict(supervisor.walk_reasons.most_common(10))
    result["decision_ms_mean"] = round(
        1000.0 * supervisor.decision_seconds / max(1, supervisor.decisions), 3)
    if siege is not None:
        result["siege_stats"] = dict(siege.stats)
        result["siege_call_ms_mean"] = round(
            1000.0 * siege_seconds[0] / max(1, siege_seconds[2]), 4)
        result["siege_call_ms_max"] = round(1000.0 * siege_seconds[1], 3)
        coordinator = siege.coordinator.metrics
        result["coordinator"] = {
            name: getattr(coordinator, name)
            for name in ("stuck_events", "tunnel_orders", "dig_orders",
                         "surround_orders", "climb_orders", "max_pile")
        }
    result["structure_cells"] = len(built)
    result["prefab_access"] = dict(supervisor.brain.prefab_ascent.metrics)
    result["skill_metrics"] = dict(supervisor.brain.skills.metrics)
    result["skill_failures"] = list(supervisor.brain.skills.events)
    result["structure_cells_gone"] = sum(1 for c in built if not world.get_solid(*c))
    await director.close()
    return result


def _time_siege(siege, totals: list) -> None:
    """Wrap the service's snapshot call to measure its gameplay-thread cost."""

    inner = siege._objectives

    def timed(*args, **kwargs):
        started = _perf()
        try:
            return inner(*args, **kwargs)
        finally:
            spent = _perf() - started
            totals[0] += spent
            totals[1] = max(totals[1], spent)
            totals[2] += 1

    siege._objectives = timed


def _trace(t: float, director, zombies) -> None:
    orders = getattr(getattr(director, "_zombie_siege", None), "last_orders", {})
    for bot in zombies:
        runtime = director._runtime.get(bot.id)
        intent = runtime.intent if runtime is not None else None
        order = orders.get(bot.id)
        goal = (tuple(round(v, 1) for v in intent.debug_goal)
                if intent and intent.debug_goal else None)
        print(f"t={t:6.1f} id={bot.id:2d} pos=({bot.position[0]:6.1f},{bot.position[1]:6.1f},"
              f"{bot.position[2]:6.1f}) g={int(bot.grounded)} w={int(bool(bot.wade))}"
              f" role={intent.debug_role if intent else None}"
              f" aff={intent.movement.affordance.value if intent else None}"
              f" act={intent.action.kind.value if intent else None} goal={goal}"
              f" order={(order.role, order.stuck_level, order.aim, len(order.cells)) if order else None}",
              flush=True)


class _Metrics:
    """Per-zombie activity shares and contact times.

    ``horde_floor`` is the z of the structure's base block (the ground at its
    foot); a body at that level or higher counts for ``sides_used``.
    """

    def __init__(self, zombies, survivors, now: float, *, horde_floor=None) -> None:
        self.zombies = list(zombies)
        self.survivors = list(survivors)
        self.horde_floor = None if horde_floor is None else float(horde_floor)
        self.pile_seconds = 0.0
        self.ring_idle = 0
        self.classified = 0
        self.first_at_height = None
        self.sides: set[int] = set()
        self.longest_still = {z.id: 0.0 for z in zombies}
        self._still_since: dict[int, float | None] = {z.id: None for z in zombies}
        self._last_seen: dict[int, tuple | None] = {z.id: None for z in zombies}
        self.first_contact = None
        self.first_hit = None
        self.collapse_s = None
        self.activity: Counter = Counter()
        self.by_bot: dict[int, Counter] = {z.id: Counter() for z in zombies}
        self.roles: Counter = Counter()
        self.orders: Counter = Counter()
        self.sprint = [0, 0]
        self.history = {z.id: deque() for z in zombies}
        self.stuck_level = {z.id: 0 for z in zombies}
        self.escalations: list[dict] = []
        self.left_land: dict[int, float] = {}
        self.reached_mainland: dict[int, float] = {}
        self.start_land: dict[int, int] = {}
        self.min_distance = {z.id: math.inf for z in zombies}
        self.pile: list[int] = []
        self.reach: dict[int, float] = {}
        self.water_samples = 0
        self.order_changes = {z.id: 0 for z in zombies}
        self.after_reach: dict[int, list[int]] = {z.id: [0, 0] for z in zombies}
        self.sprint_notes: Counter = Counter()
        self.idle_roles: Counter = Counter()
        self.idle_orders: Counter = Counter()
        self.last_order: dict[int, tuple] = {}

    def collapse(self, t: float) -> None:
        if self.collapse_s is None:
            self.collapse_s = round(t, 2)

    def hit(self, t: float) -> None:
        if self.first_hit is None:
            self.first_hit = round(t, 2)

    def sample(self, t, director, living, world, label, mainland, elevated) -> None:
        orders = getattr(getattr(director, "_zombie_siege", None), "last_orders", {})
        positions = [z.position for z in self.zombies if z.alive and z.spawned]
        if elevated and living:
            pile = max(
                sum(1 for p in positions
                    if math.hypot(p[0] - s.position[0], p[1] - s.position[1]) <= 2.5
                    and p[2] - s.position[2] >= 3.0)
                for s in living)
            self.pile.append(pile)
            self.pile_seconds += pile * SAMPLE_SECONDS
        for bot in self.zombies:
            runtime = director._runtime.get(bot.id)
            intent = runtime.intent if runtime is not None else None
            if not (bot.alive and bot.spawned) or not living:
                self.history[bot.id].clear()
                self._still_since[bot.id] = None
                self._last_seen[bot.id] = None
                continue
            seen = self._last_seen[bot.id]
            if seen is not None and math.dist(seen[1], bot.position) < STILL_BLOCKS:
                if self._still_since[bot.id] is None:
                    self._still_since[bot.id] = seen[0]
                self.longest_still[bot.id] = max(
                    self.longest_still[bot.id], t - self._still_since[bot.id])
            else:
                self._still_since[bot.id] = None
            self._last_seen[bot.id] = (t, tuple(bot.position))
            for s in living:
                across = math.hypot(bot.position[0] - s.position[0],
                                    bot.position[1] - s.position[1])
                if (self.first_at_height is None and across <= 4.0
                        and abs(bot.position[2] - s.position[2]) <= 3.0):
                    self.first_at_height = round(t, 2)
                if (self.horde_floor is not None and across <= 6.0
                        and bot.position[2] <= self.horde_floor - STAND + 1.0):
                    bearing = math.atan2(bot.position[1] - s.position[1],
                                         bot.position[0] - s.position[0])
                    self.sides.add(int(((bearing + 2 * math.pi) % (2 * math.pi))
                                       // (math.pi / 4.0)) % 8)
            order = orders.get(bot.id)
            target = None
            if order is not None:
                target = next((s for s in living if s.id == order.target_id), None)
                self.orders[order.role] += 1
                key = (order.role, order.target_id)
                if self.last_order.get(bot.id, key) != key:
                    self.order_changes[bot.id] += 1
                self.last_order[bot.id] = key
                if order.stuck_level > self.stuck_level[bot.id]:
                    self.escalations.append({
                        "t": round(t, 1), "bot": bot.id, "level": order.stuck_level,
                        "role": order.role,
                        "still_s": round(self._still_seconds(bot, t), 1),
                    })
                self.stuck_level[bot.id] = order.stuck_level
            if target is None:
                target = min(living, key=lambda s: math.dist(s.position, bot.position))
            distance = math.dist(bot.position, target.position)
            nearest = min(math.dist(bot.position, s.position) for s in living)
            self.min_distance[bot.id] = min(self.min_distance[bot.id], nearest)
            if nearest <= 3.0:
                self.reach.setdefault(bot.id, round(t, 2))
                if self.first_contact is None:
                    self.first_contact = round(t, 2)
            role = intent.debug_role.split(":")[0] if intent is not None else "none"
            self.roles[role] += 1
            column = (int(math.floor(bot.position[0])), int(math.floor(bot.position[1])))
            if label:
                land = label.get(column)
                if bot.id not in self.start_land and land is not None:
                    self.start_land[bot.id] = land
                if land is None and bot.id not in self.left_land:
                    self.left_land[bot.id] = round(t, 2)
                if land == mainland and bot.grounded and bot.id not in self.reached_mainland:
                    self.reached_mainland[bot.id] = round(t, 2)
            buf = self.history[bot.id]
            buf.append((t, tuple(bot.position), distance))
            while buf and t - buf[0][0] > WINDOW_SECONDS + 1e-6:
                buf.popleft()
            clawing = (runtime is not None and runtime.feedback_action_kind == "melee"
                       and time.monotonic() - float(runtime.feedback_action_at) < 2.0)
            wading = bool(bot.wade)
            if wading:
                self.water_samples += 1
            if t - buf[0][0] < WINDOW_SECONDS - SAMPLE_SECONDS:
                continue
            moved = math.hypot(buf[0][1][0] - bot.position[0], buf[0][1][1] - bot.position[1])
            closed = buf[0][2] - distance
            rise = buf[0][1][2] - bot.position[2]  # z grows downward
            closed_xy = (math.hypot(buf[0][1][0] - target.position[0],
                                    buf[0][1][1] - target.position[1])
                         - math.hypot(bot.position[0] - target.position[0],
                                      bot.position[1] - target.position[1]))
            building = intent is not None and (
                str(intent.action.kind.value) in BUILD_ACTIONS
                or str(intent.debug_role).startswith(BUILD_ROLE_PREFIXES))
            if nearest <= 4.0:
                kind = "fighting"
            elif rise >= RISE_BLOCKS and closed_xy < 1.0:
                kind = "climbing"
            elif closed >= 1.0:
                kind = "closing"
            elif building:
                kind = "building"
            elif clawing:
                kind = "digging"
            elif moved >= 2.0:
                kind = "wandering"
            else:
                kind = "standing"
            self.activity[kind] += 1
            self.by_bot[bot.id][kind] += 1
            self.classified += 1
            if order is not None and order.role == "surround" and kind == "standing":
                self.ring_idle += 1
            if kind in ("standing", "wandering"):
                detail = intent.debug_role if intent is not None else "none"
                self.idle_roles[f"{kind}:{detail}"] += 1
                self.idle_orders[f"{kind}:{order.role if order is not None else 'none'}"] += 1
            if bot.id in self.reach:
                self.after_reach[bot.id][1] += 1
                self.after_reach[bot.id][0] += int(nearest <= 4.0)
            if intent is not None and kind in ("closing", "wandering"):
                self.sprint[1] += 1
                self.sprint[0] += int(bool(intent.movement.sprint))
                if not intent.movement.sprint:
                    note = dict(intent.debug_navigation).get("step_note", "")
                    self.sprint_notes[f"{intent.movement.affordance.value}:"
                                      f"{str(note).split(':')[0]}"] += 1

    def _still_seconds(self, bot, t: float) -> float:
        buf = self.history[bot.id]
        if not buf:
            return 0.0
        return t - buf[0][0] if math.dist(buf[0][1], bot.position) < 1.5 else 0.0

    def report(self) -> dict:
        total = sum(self.activity.values()) or 1
        shares = {k: round(v / total, 3) for k, v in sorted(self.activity.items())}
        worst = {}
        for bot_id, counter in self.by_bot.items():
            n = sum(counter.values())
            if n:
                worst[bot_id] = round((counter["standing"] + counter["wandering"]) / n, 3)
        reached = sorted(self.reach.values())
        held = [a / b for a, b in self.after_reach.values() if b]
        report = {
            "reach_median_s": (reached[len(self.zombies) // 2]
                               if len(reached) > len(self.zombies) // 2 else None),
            "engaged_after_reach": round(sum(held) / len(held), 3) if held else None,
            "walking_notes": dict(self.sprint_notes.most_common(8)),
            "first_contact_s": self.first_contact,
            "first_hit_s": self.first_hit,
            "collapse_s": self.collapse_s,
            "activity_share": shares,
            "idle_share_worst_bot": max(worst.values(), default=0.0),
            "sprint_share": round(self.sprint[0] / max(1, self.sprint[1]), 3),
            "zombies_reached": len(self.reach),
            "reach_s": {str(k): v for k, v in sorted(self.reach.items())},
            "min_distance": {str(k): round(v, 1) for k, v in sorted(self.min_distance.items())},
            "roles": dict(self.roles.most_common(14)),
            "idle_roles": dict(self.idle_roles.most_common(12)),
            "idle_orders": dict(self.idle_orders.most_common(8)),
            "orders": dict(self.orders.most_common()),
            "order_changes_per_bot": round(
                sum(self.order_changes.values()) / max(1, len(self.order_changes)), 2),
            "escalations": self.escalations[:24],
            "water_share": round(self.water_samples / max(1, sum(self.roles.values())), 3),
            "useful_share": round(sum(shares.get(k, 0.0) for k in USEFUL_KINDS), 3),
            "ring_idle_share": round(self.ring_idle / max(1, self.classified), 3),
            "first_at_height_s": self.first_at_height,
            "sides_used": len(self.sides) if self.horde_floor is not None else None,
            "longest_still_s": {str(k): round(v, 1)
                                for k, v in sorted(self.longest_still.items())},
            "longest_still_max_s": round(max(self.longest_still.values(), default=0.0), 1),
        }
        if self.pile:
            report["pile_seconds"] = round(self.pile_seconds, 1)
            report["pile_mean"] = round(sum(self.pile) / len(self.pile), 2)
            report["pile_max"] = max(self.pile)
        if self.start_land or self.left_land:
            report["left_land_s"] = {str(k): v for k, v in sorted(self.left_land.items())}
            report["reached_mainland_s"] = {
                str(k): v for k, v in sorted(self.reached_mainland.items())}
            report["never_reached_mainland"] = sorted(
                z.id for z in self.zombies if z.id not in self.reached_mainland)
        return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--map", default="ArcticBase")
    parser.add_argument("--scenario", choices=SCENARIOS, default="ground")
    parser.add_argument("--zombies", type=int, default=8)
    parser.add_argument("--survivors", type=int, default=2)
    parser.add_argument("--seconds", type=float, default=120.0)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--baseline", action="store_true",
                        help="disable the horde coordinator (nearest-survivor hunt)")
    parser.add_argument("--json", type=Path)
    parser.add_argument("--beacon", action="store_true",
                        help="survivors cannot die: time the whole horde's arrival")
    parser.add_argument("--real-budgets", action="store_true",
                        help="keep the real-time perception/siege budgets (load dependent)")
    parser.add_argument("--trace", action="store_true")
    parser.add_argument("--height", type=int, default=None,
                        help="structure height (default 64; roofpillar/wallpillar 8)")
    parser.add_argument("--bridge-span", type=int, default=64)
    parser.add_argument("--bridge-width", type=int, choices=(1,2,3), default=1)
    parser.add_argument("--trace-bot", type=int,
                        help="print every decision of the Nth zombie (0-based)")
    parser.add_argument("--spawn-distance", default="16,26",
                        help="LO,HI blocks from the structure where the horde starts "
                             "(a live London horde comes from 90-120 blocks away)")
    parser.add_argument("--hit-chance", type=float, default=1.0,
                        help="armed survivor: share of shots aimed true (1.0 = perfect; "
                             "a good player on moving zombies is nearer 0.6)")
    parser.add_argument("--armed", action="store_true",
                        help="the idle survivor shoots back (rifle, 0.6-0.9 s) and "
                             "repairs his pillar every 4 s")
    args = parser.parse_args()
    result = asyncio.run(run_scenario(args))
    text = json.dumps(result, indent=1, default=str)
    print(text)
    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
