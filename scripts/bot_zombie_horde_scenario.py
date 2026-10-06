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
fighting, swimming, wandering or standing, sprint share while travelling,
how long a stall lasts before the watchdog escalates, water crossings, pile
samples under elevated survivors and the horde planning cost.

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

SCENARIOS = (
    "islands", "sea", "wade", "offshore", "ground", "open", "round", "yard", "room",
    "pillar", "platform", "tower", "fortress", "roof", "bridge", "dropin", "ledge", "pit",
)
ELEVATED = frozenset({"pillar", "platform", "tower", "fortress", "roof", "bridge", "dropin"})
STRUCTURES = ELEVATED | {"yard", "room", "ledge", "pit"}
STAND = 2.25
SAMPLE_SECONDS = 0.5
WINDOW_SECONDS = 2.0


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


def build_structure(world, structure: str, x: int, y: int, floor: int):
    """Place a synthetic structure; survivor tops, built cells, zombie ring."""

    color = 0x777777
    tops: list[tuple[int, int, int]] = []
    built: list[tuple[int, int, int]] = []

    def put(cx, cy, cz):
        built.append((cx, cy, cz))
        world.set_block(cx, cy, cz, True, color)

    def clear(cx, cy, cz):
        world.set_block(cx, cy, cz, False, 0)

    if structure == "pillar":
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


def scenario_args(**overrides) -> argparse.Namespace:
    """Default options of one run, for callers that are not the CLI."""

    options = dict(map="ArcticBase", scenario="ground", zombies=8, survivors=2,
                   seconds=120.0, seed=7, baseline=False, json=None, trace=False,
                   trace_bot=None, real_budgets=False, beacon=False)
    unknown = set(overrides) - set(options)
    if unknown:
        raise TypeError(f"unknown scenario options: {sorted(unknown)}")
    options.update(overrides)
    return argparse.Namespace(**options)


async def run_scenario(args) -> dict:
    random_state = random.getstate()
    horde_objectives = BotDirector._objectives_zombie_horde
    try:
        with _Clock(real_budgets=args.real_budgets) as clock:
            return await _run_scenario(args, clock)
    finally:
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
    # simulated clock like everything else.
    director._zombie_siege = ZombieSiegeService(clock=lambda: clock.now)
    await director.start(initial_count=0)

    survivors: list[Player] = []
    survivor_bots: list = []
    built: list = []
    site = None
    if structure:
        x, y, floor = site = _flat_site(world, rng, level=True,
                                        span=16 if scenario == "dropin" else 8)
        if scenario == "dropin":
            x -= 12  # the pillar stands at one end of the flat, the stairs at the other
        tops, built = build_structure(world, structure, x, y, floor)
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
            radius = rng.uniform(16, 26)
            if scenario == "yard":
                # Everyone starts on the side away from the gate.
                angle = rng.uniform(0.25 * math.pi, 0.75 * math.pi)
            elif scenario == "dropin":
                # ... and here on the side away from the deck.
                angle = rng.uniform(-0.75 * math.pi, -0.25 * math.pi)
            anchor = world.dry_ground_anchor(x + radius * math.cos(angle),
                                             y + radius * math.sin(angle), search=12)
            bot.set_position(*anchor)
        zombies.append(bot)

    supports = {s.id: support_cells(world.get_solid, s.position) for s in survivors}
    start_health = {s.id: s.health for s in survivors}
    metrics = _Metrics(zombies, survivors, clock.now)
    if args.trace_bot is not None:
        supervisor.trace_bot = int(zombies[args.trace_bot].id)
        supervisor.trace_origin = clock.now
    tick = server.tick_interval
    steps = int(args.seconds / tick)
    tick_ms: list[float] = []
    sample_every = max(1, int(round(SAMPLE_SECONDS / tick)))
    siege_seconds = [0.0, 0.0, 0]
    siege = None
    elevated = scenario in ELEVATED
    beacon = scenario in ("islands", "sea") or args.beacon
    infected: dict[int, float] = {}
    wall_started = _perf()
    for step in range(steps):
        t = step * tick
        clock.now += tick
        server.loop_count += 1
        started = _perf()
        await server.simulation_runtime.step()
        tick_ms.append((_perf() - started) * 1000.0)
        await asyncio.sleep(0)
        if siege is None:
            siege = director._zombie_siege
            _time_siege(siege, siege_seconds)
        living = [s for s in survivors
                  if s.alive and s.spawned and int(s.team) == SURVIVOR_TEAM]
        for s in survivors:
            support = supports.get(s.id)
            if (structure and s not in survivor_bots and support and s.alive
                    and int(s.team) == SURVIVOR_TEAM and step % 6 == 0
                    and not any(world.get_solid(*cell) for cell in support)):
                # Idle bodies are client-authoritative: drop one whose
                # footing fell, as its client would.
                metrics.collapse(t)
                cx, cy = int(s.position[0]), int(s.position[1])
                land = next((z for z in range(support[0][2], 240)
                             if world.get_solid(cx, cy, z)), None)
                supports[s.id] = ()
                if land is not None:
                    s.set_position(s.position[0], s.position[1], land - STAND)
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
        "seed": args.seed, "site": site, "duration_s": round(duration, 1),
        "wall_s": round(_perf() - wall_started, 1),
        "infected_s": {str(k): v for k, v in infected.items()},
        "kills": len(infected),
        "all_infected_s": (max(infected.values()) if infected
                           and len(infected) == len(survivors) else None),
    }
    result.update(metrics.report())
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
    """Per-zombie activity shares and contact times."""

    def __init__(self, zombies, survivors, now: float) -> None:
        self.zombies = list(zombies)
        self.survivors = list(survivors)
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
            self.pile.append(max(
                sum(1 for p in positions
                    if math.hypot(p[0] - s.position[0], p[1] - s.position[1]) <= 2.5
                    and p[2] - s.position[2] >= 3.0)
                for s in living))
        for bot in self.zombies:
            runtime = director._runtime.get(bot.id)
            intent = runtime.intent if runtime is not None else None
            if not (bot.alive and bot.spawned) or not living:
                self.history[bot.id].clear()
                continue
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
            if nearest <= 4.0:
                kind = "fighting"
            elif closed >= 1.0:
                kind = "closing"
            elif clawing:
                kind = "digging"
            elif moved >= 2.0:
                kind = "wandering"
            else:
                kind = "standing"
            self.activity[kind] += 1
            self.by_bot[bot.id][kind] += 1
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
        }
        if self.pile:
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
    parser.add_argument("--trace-bot", type=int,
                        help="print every decision of the Nth zombie (0-based)")
    args = parser.parse_args()
    result = asyncio.run(run_scenario(args))
    text = json.dumps(result, indent=1, default=str)
    print(text)
    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
