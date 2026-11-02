"""A scripted human survivor for the Zombie fun gate (``zombie_fun_gate.py``).

The survivor is a peerless, server-owned ``Player`` that is driven exactly the
way a retail client drives its body:

* **movement** only through ``Player.record_input_frame`` (one 8-key frame per
  server tick: up/down/left/right/jump/crouch/sneak/sprint plus the aim
  vector); the native mover does the rest. ``set_position`` is wrapped after
  the spawn and every later call is counted as a teleport (must stay 0).
* **building** only through ``CombatSystem.handle_block_line`` (BlockLine, id
  40: the packet the 1.x client sends for every placement), so reach, line of
  sight, face adjacency, the block wallet, the 0.1 s cadence and the
  post-physics commit (``WorldMutationService``) all apply. ``handle_block_build``
  (id 32) is available as ``Builder.place_single`` but the client never sends it.
* **shooting** through ``CombatSystem.handle_shot`` with a ShootPacket (id 6):
  the armed model of ``bot_zombie_horde_scenario._ArmedSurvivor`` (two-phase
  aim/fire, reload through ``handle_weapon_reload``) with a bounded hit chance:
  a deliberate miss offsets the aim point so the bullet passes the body.
* **digging** likewise (spade ShootPacket), see ``Survivor.dig``.

Tactics (``TACTICS``) are small state machines with timeouts; each adds
human-looking idling (small strafes, look-arounds, an occasional crouch) and a
bounded reaction time (``HumanInput.reaction``). ``FunMetrics`` wraps the horde
harness classifier (``_Metrics``) and adds the gate's own counters.

The module doubles as a server plugin (``ZombieFunGatePlugin``) for
``zombie_fun_gate.py --real-server``: the gate copies this file into a
temporary plugin folder and points ``[plugins] path`` at it, so the same
survivor runs inside a real ``run_server.py`` process.
"""

from __future__ import annotations

from collections import Counter, deque
import json
import logging
import math
import os
from pathlib import Path
import random
import sys
import time

def _repo_root() -> Path:
    """The checkout, also when this file runs as a copied plugin."""

    candidates = []
    override = os.environ.get("ZOMBIE_FUN_GATE_ROOT")
    if override:
        candidates.append(Path(override))
    candidates += [Path(__file__).resolve().parents[1], Path.cwd()]
    for candidate in candidates:
        if (candidate / "server").is_dir() and (candidate / "scripts").is_dir():
            return candidate.resolve()
    return Path(__file__).resolve().parents[1]


ROOT = _repo_root()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import shared.constants as C  # noqa: E402
from modes.zombie import SURVIVOR_TEAM, ZOMBIE_TEAM  # noqa: E402
from server.class_selection import equipped_tool_authorized, normalize_class_selection  # noqa: E402
from server.game_constants import DEFAULT_WEAPON_TOOL, WEAPON_PROFILES  # noqa: E402
from server.player import Player  # noqa: E402
from shared.packet import BlockBuild, BlockLine, ShootPacket  # noqa: E402

logger = logging.getLogger(__name__)

STAND = 2.25                     # eye above the feet (PLAYER_STANDING_POS_ABOVE_GROUND)
TICK = 1.0 / 60.0
WALK_SPEED = 4.1                 # blocks/s, measured on the server mover
SPRINT_SPEED = 8.3
JUMP_PLACE_TICKS = (8, 26)       # ticks after the jump press when the eye is > 1 block up
SAMPLE_SECONDS = 0.5
STUCK_SECONDS = 6.0
STUCK_MOVE = 0.5
TACTICS = ("pillar", "roof", "tree", "runner", "bunker", "neck")
ELEVATED_TACTICS = frozenset({"pillar", "roof", "tree", "neck"})
KEY_UP, KEY_DOWN, KEY_LEFT, KEY_RIGHT, KEY_JUMP, KEY_CROUCH, KEY_SNEAK, KEY_SPRINT = range(8)
BLOCK_TOOL = int(C.BLOCK_TOOL)
RIFLE_TOOL = int(C.RIFLE_TOOL)
SPADE_TOOL = int(C.SPADE_TOOL)
WEAPON_BY_NAME = {
    "rifle": RIFLE_TOOL,
    "smg": int(getattr(C, "SMG_TOOL", 7)),
    "shotgun": int(getattr(C, "SHOTGUN_TOOL", 9)),
    "sniper": int(getattr(C, "SNIPER_TOOL", 18)),
    "pistol": int(getattr(C, "PISTOL_TOOL", 17)),
}


# --------------------------------------------------------------------------- #
# spawning
# --------------------------------------------------------------------------- #

class IdleConnection:
    """A peerless, settled connection: the survivor is server-owned."""

    def __init__(self, server) -> None:
        self.server = server
        self.in_game = True
        self.player = None
        self.peer = None
        self.map_sent = True
        self.known_entity_ids: set = set()

    def send(self, data, reliable: bool = True, prefix: int = 0x30) -> None:
        return None

    def send_packet(self, packet, reliable: bool = True) -> None:
        return None

    def disconnect(self, reason: int = 0) -> None:
        self.in_game = False


def spawn_survivor(server, position, name: str = "Human") -> Player:
    """Create a human-like (non-bot) survivor and spawn it once at ``position``."""

    connection = IdleConnection(server)
    player = Player(server.get_next_player_id(), name, SURVIVOR_TEAM, DEFAULT_WEAPON_TOOL, connection)
    connection.player = player
    player.apply_class_selection(normalize_class_selection(int(C.CLASS_SOLDIER)))
    server.players[player.id] = player
    server.teams[SURVIVOR_TEAM].add_player(player)
    server.connections[object()] = connection
    player.spawn(*position)
    server._broadcast_create_player(player, position)
    mode = getattr(server, "mode", None)
    if mode is not None and hasattr(mode, "_assign_survivor"):
        mode._assign_survivor(player)
    return player


# --------------------------------------------------------------------------- #
# geometry helpers
# --------------------------------------------------------------------------- #

def _norm(v):
    m = math.sqrt(sum(c * c for c in v))
    return tuple(c / m for c in v) if m > 1e-9 else (1.0, 0.0, 0.0)


def feet_cell(world, player):
    """The solid cell the player stands on (first solid at/under the feet)."""

    feet = float(player.z) + STAND
    cx, cy = int(math.floor(player.x)), int(math.floor(player.y))
    start = int(math.floor(feet))
    for z in range(start, min(start + 4, 240)):
        if world.get_solid(cx, cy, z):
            return (cx, cy, z)
    return None


def standing_height(world, x: int, y: int) -> int:
    return int(world.get_height(x, y))


def walkable_segment(world, a, b, *, max_up: int = 1, max_down: int = 4) -> bool:
    """Straight ground walk from ``a`` to ``b`` (xy): small steps, no water."""

    ax, ay = float(a[0]) + 0.5 if isinstance(a[0], int) else float(a[0]), float(a[1]) + 0.5 if isinstance(a[1], int) else float(a[1])
    bx, by = float(b[0]) + 0.5 if isinstance(b[0], int) else float(b[0]), float(b[1]) + 0.5 if isinstance(b[1], int) else float(b[1])
    length = math.hypot(bx - ax, by - ay)
    steps = max(1, int(length / 0.5))
    prev = None
    for i in range(steps + 1):
        f = i / steps
        cx, cy = int(math.floor(ax + (bx - ax) * f)), int(math.floor(ay + (by - ay) * f))
        if not (1 <= cx < 511 and 1 <= cy < 511) or world.is_water_column(cx, cy):
            return False
        h = standing_height(world, cx, cy)
        # a standing body is three blocks tall: the three cells over the floor
        if world.get_solid(cx, cy, h - 1) or world.get_solid(cx, cy, h - 2) or world.get_solid(cx, cy, h - 3):
            return False
        if prev is not None:
            if prev - h > max_up:      # z grows downward: smaller z = higher
                return False
            if h - prev > max_down:
                return False
        prev = h
    return True


def open_ground_near(world, rng: random.Random, start, *, min_d: float = 8.0, max_d: float = 30.0,
                     span: int = 2):
    """Flat, dry, level open ground reachable from ``start`` on a straight walk."""

    sx, sy = float(start[0]), float(start[1])
    start_h = standing_height(world, int(sx), int(sy))
    candidates = []
    for cx in range(int(sx - max_d), int(sx + max_d) + 1, 2):
        for cy in range(int(sy - max_d), int(sy + max_d) + 1, 2):
            d = math.hypot(cx + 0.5 - sx, cy + 0.5 - sy)
            if not (min_d <= d <= max_d) or not (8 < cx < 504 and 8 < cy < 504):
                continue
            heights = []
            ok = True
            for dx in range(-span, span + 1):
                for dy in range(-span, span + 1):
                    if world.is_water_column(cx + dx, cy + dy):
                        ok = False
                        break
                    heights.append(standing_height(world, cx + dx, cy + dy))
                if not ok:
                    break
            if not ok or max(heights) - min(heights) > 1 or abs(heights[0] - start_h) > 2:
                continue
            if not walkable_segment(world, (sx, sy), (cx + 0.5, cy + 0.5)):
                continue
            candidates.append((abs(d - 0.5 * (min_d + max_d)), cx, cy, max(heights)))
    if not candidates:
        return None
    candidates.sort()
    _, cx, cy, floor = rng.choice(candidates[: max(1, len(candidates) // 4)])
    return cx, cy, floor


def heightmap_path(world, start_cell, goal_cells, *, limit: int = 20000, max_up: int = 1,
                   max_down: int = 4):
    """BFS over the standing-height map; returns a cell path or None.

    Only exterior walking is modelled (the surface height per column), so an
    interior staircase is invisible here: that is the "stairs or terrain
    allow" test of the roof tactic.
    """

    goals = set(goal_cells)
    start = (int(start_cell[0]), int(start_cell[1]))
    frontier = deque([start])
    came = {start: None}
    while frontier and len(came) < limit:
        cell = frontier.popleft()
        if cell in goals:
            path = []
            while cell is not None:
                path.append(cell)
                cell = came[cell]
            path.reverse()
            return path
        h = standing_height(world, *cell)
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nxt = (cell[0] + dx, cell[1] + dy)
            if nxt in came or not (1 <= nxt[0] < 511 and 1 <= nxt[1] < 511):
                continue
            if world.is_water_column(*nxt):
                continue
            nh = standing_height(world, *nxt)
            if h - nh > max_up or nh - h > max_down:
                continue
            if (world.get_solid(nxt[0], nxt[1], nh - 1) or world.get_solid(nxt[0], nxt[1], nh - 2)
                    or world.get_solid(nxt[0], nxt[1], nh - 3)):
                continue
            came[nxt] = cell
            frontier.append(nxt)
    return None


def simplify_path(path, world):
    """Keep the corners (and every 6th cell) of a BFS path as waypoints."""

    if not path:
        return []
    points = [path[0]]
    last_dir = None
    for i in range(1, len(path)):
        d = (path[i][0] - path[i - 1][0], path[i][1] - path[i - 1][1])
        if d != last_dir or i % 6 == 0 or i == len(path) - 1:
            points.append(path[i])
        last_dir = d
    return [(cx + 0.5, cy + 0.5, standing_height(world, cx, cy)) for cx, cy in points]


def find_street_room(world, rng: random.Random, near, *, radius: int = 90):
    """A roofed interior cell with one doorway (2 or 3 high) onto the street.

    Returns ``(inside_cell, door_cell, outside_cell, floor_z, door_height)``
    with cells as ``(x, y)`` columns and ``floor_z`` the z of the floor voxel,
    or None. A standing body is three blocks tall; a 2-high doorway is
    entered crouching.
    """

    nx, ny = int(near[0]), int(near[1])
    found = []
    for cx in range(nx - radius, nx + radius + 1):
        for cy in range(ny - radius, ny + radius + 1):
            if not (3 < cx < 508 and 3 < cy < 508) or world.is_water_column(cx, cy):
                continue
            top = standing_height(world, cx, cy)
            # pockets below the surface: an air cell with air above (3 high,
            # standing room), a solid floor and a roof 1-6 blocks over the head
            for z in range(top + 1, min(top + 40, 236)):
                if world.get_solid(cx, cy, z):
                    continue
                if (world.get_solid(cx, cy, z - 1) or world.get_solid(cx, cy, z - 2)
                        or not world.get_solid(cx, cy, z + 1)):
                    continue
                if not any(world.get_solid(cx, cy, z - 3 - k) for k in range(0, 6)):
                    continue
                sides = {}
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    wall = (world.get_solid(cx + dx, cy + dy, z) or world.get_solid(cx + dx, cy + dy, z - 1))
                    sides[(dx, dy)] = wall
                open_sides = [d for d, wall in sides.items() if not wall]
                if len(open_sides) != 1:
                    continue
                dx, dy = open_sides[0]
                door = (cx + dx, cy + dy)
                outside = (cx + 2 * dx, cy + 2 * dy)
                # the doorway: 2 or 3 high, with a solid frame on both sides
                if not (world.get_solid(door[0], door[1], z + 1)
                        and not world.get_solid(door[0], door[1], z)
                        and not world.get_solid(door[0], door[1], z - 1)):
                    continue
                door_height = 3 if not world.get_solid(door[0], door[1], z - 2) else 2
                if not (world.get_solid(door[0] + dy, door[1] + dx, z) or world.get_solid(door[0] + dy, door[1] + dx, z - 1)):
                    continue
                if not (world.get_solid(door[0] - dy, door[1] - dx, z) or world.get_solid(door[0] - dy, door[1] - dx, z - 1)):
                    continue
                # outside must be the open street at the same level (not another room)
                oh = standing_height(world, *outside)
                if abs(oh - (z + 1)) > 1:
                    continue
                far = (cx + 4 * dx, cy + 4 * dy)
                if abs(standing_height(world, *far) - (z + 1)) > 2:
                    continue
                # interior depth: at least one more interior cell behind
                back = (cx - dx, cy - dy)
                if (world.get_solid(back[0], back[1], z) or world.get_solid(back[0], back[1], z - 1)
                        or world.get_solid(back[0], back[1], z - 2)
                        or not world.get_solid(back[0], back[1], z + 1)):
                    continue
                # prefer walk-in doors, then the nearest
                found.append((0 if door_height == 3 else 1, math.hypot(cx - nx, cy - ny),
                              (cx, cy), door, outside, z + 1, door_height))
                break
    if not found:
        return None
    found.sort()
    _, _, inside, door, outside, floor_z, door_height = rng.choice(found[: max(1, min(6, len(found)))])
    return inside, door, outside, floor_z, door_height


# --------------------------------------------------------------------------- #
# the human input layer
# --------------------------------------------------------------------------- #

class HumanInput:
    """Turns intentions into 8-key input frames with human timing.

    ``move`` is a world xy direction (or None), ``look`` the aim vector. Both
    are *requested*; they take effect after ``reaction`` seconds (150-350 ms,
    drawn per change). ``jump()`` holds SPACE for three frames. Idle fidgets
    (a short strafe tap, a glance, a crouch) are mixed in when the tactic
    allows them (``fidget``).
    """

    def __init__(self, rng: random.Random) -> None:
        self.rng = rng
        self.look = (1.0, 0.0, 0.0)
        self.move = None
        self.sprint = False
        self.crouch = False
        self._req_look = None
        self._req_move = None
        self._req_at = 0.0
        self._req_sprint = False
        self._jump_ticks = 0
        self._fidget_until = 0.0
        self._fidget_keys = ()
        self._fidget_back = None      # (keys, until) of the returning half of a shuffle
        self._next_fidget = 2.0
        self.sneak = False
        self.frames = 0
        self.turns = 0

    def reaction(self) -> float:
        return self.rng.uniform(0.15, 0.35)

    def request(self, t: float, *, look=None, move=None, sprint=None, immediate: bool = False):
        delay = 0.0 if immediate else self.reaction()
        if look is not None:
            self._req_look = _norm(look)
        if move is not None:
            self._req_move = _norm((move[0], move[1], 0.0))[:2] if any(move) else ()
        if sprint is not None:
            self._req_sprint = bool(sprint)
        # a request already waiting keeps its (earlier) settle time
        self._req_at = min(self._req_at, t + delay) if self._req_at > t else t + delay

    def stop(self, t: float, immediate: bool = True):
        self._req_move = ()
        self._req_sprint = False
        self._req_at = t if immediate else t + self.reaction()

    def look_at(self, t: float, eye, target, *, immediate: bool = False):
        v = (target[0] - eye[0], target[1] - eye[1], target[2] - eye[2])
        self.request(t, look=v, immediate=immediate)

    def set_look_now(self, look) -> None:
        self.look = _norm(look)
        self._req_look = None

    def jump(self) -> None:
        self._jump_ticks = 3

    def fidget(self, t: float, *, strafe: bool = True) -> None:
        """Human idling: a shuffle (strafe tap and back), a look-around or a
        short crouch. ``strafe=False`` on a perch no human would shuffle on."""

        if self._fidget_back is not None and self._fidget_until <= t:
            # second half of the shuffle: the same tap the other way
            self._fidget_keys, duration = self._fidget_back
            self._fidget_until = t + duration
            self._fidget_back = None
            return
        if t < self._next_fidget or self._fidget_until > t:
            return
        self._next_fidget = t + self.rng.uniform(1.5, 4.0)
        kinds = ("strafe", "strafe", "look", "crouch", "none") if strafe else ("look", "look", "crouch", "none")
        kind = self.rng.choice(kinds)
        if kind == "strafe":
            key = self.rng.choice((KEY_LEFT, KEY_RIGHT))
            duration = self.rng.uniform(0.12, 0.25)
            self._fidget_keys = (key,)
            self._fidget_until = t + duration
            self._fidget_back = ((KEY_RIGHT if key == KEY_LEFT else KEY_LEFT,), duration)
        elif kind == "look":
            yaw = math.atan2(self.look[1], self.look[0]) + self.rng.uniform(-0.6, 0.6)
            pitch = max(-0.6, min(0.6, self.look[2] + self.rng.uniform(-0.2, 0.2)))
            self.request(t, look=(math.cos(yaw), math.sin(yaw), pitch))
        elif kind == "crouch":
            self._fidget_keys = (KEY_CROUCH,)
            self._fidget_until = t + self.rng.uniform(0.4, 0.9)

    def _settle(self, t: float) -> None:
        if self._req_at <= t:
            if self._req_look is not None:
                if self._req_look != self.look:
                    self.turns += 1
                self.look = self._req_look
                self._req_look = None
            if self._req_move is not None:
                self.move = tuple(self._req_move) if self._req_move else None
                self._req_move = None
                self.sprint = self._req_sprint

    def frame(self, t: float) -> tuple:
        """The 8 movement flags for this tick (settles pending requests)."""

        self._settle(t)
        keys = [False] * 8
        if self.move:
            fwd = _norm((self.look[0], self.look[1], 0.0))
            right = (-fwd[1], fwd[0])
            f = self.move[0] * fwd[0] + self.move[1] * fwd[1]
            r = self.move[0] * right[0] + self.move[1] * right[1]
            if f > 0.35:
                keys[KEY_UP] = True
            elif f < -0.35:
                keys[KEY_DOWN] = True
            if r > 0.35:
                keys[KEY_RIGHT] = True
            elif r < -0.35:
                keys[KEY_LEFT] = True
            keys[KEY_SPRINT] = bool(self.sprint and keys[KEY_UP])
            keys[KEY_SNEAK] = bool(self.sneak)
        if self._jump_ticks > 0:
            keys[KEY_JUMP] = True
            self._jump_ticks -= 1
        if self.crouch:
            keys[KEY_CROUCH] = True
        if self._fidget_until > t:
            for key in self._fidget_keys:
                keys[key] = True
        self.frames += 1
        return tuple(keys)


# --------------------------------------------------------------------------- #
# the survivor
# --------------------------------------------------------------------------- #

class Survivor:
    """One scripted human: body, hands and gun, driven through client paths."""

    TORSO_Z = 0.3
    RANGE = 40.0
    TOOL_SWITCH_SECONDS = 0.3

    def __init__(self, server, player: Player, rng: random.Random, *, hit_chance: float = 0.6,
                 weapon: int = RIFLE_TOOL, resupply: bool = False) -> None:
        self.server = server
        self.player = player
        self.world = server.world_manager
        self.rng = rng
        self.input = HumanInput(random.Random(rng.random()))
        self.hit_chance = max(0.0, min(1.0, float(hit_chance)))
        self.weapon = int(weapon)
        self.resupply = bool(resupply)
        self.start_blocks = int(player.blocks)
        # action bookkeeping
        self.shots = self.hits = self.kills = self.misses_intended = 0
        self.shots_blocked: Counter = Counter()
        self.builds_sent = self.builds_accepted = self.builds_rejected = 0
        self.build_cells_committed = 0
        self.build_cells_failed = 0
        self.digs_sent = self.digs_accepted = 0
        self.repairs = 0
        self.teleports_after_spawn = 0
        self.notes: list = []
        self.build_log: list = []
        self._last_blocks = int(player.blocks)
        self._blocks_spent = 0
        self._pending_cells: list = []
        self._pending_shot = None
        self.next_shot_at = 0.5
        self.frames_applied = 0           # our own count: the server's is a 10 s window
        self.frames_synthesized = 0
        self._last_label = None
        self._repair_tries: Counter = Counter()
        self._last_repair_t = -9.0
        self._tool_wanted = int(player.tool)
        self._tool_ready_at = 0.0
        self._last_build_t = -1.0
        self.first_contact_s = None
        self.first_damage_s = None
        self.death_s = None
        self.infected_s = None
        self.killer_id = None
        self.death_health_path = None
        self._start_health = int(player.health)
        self._alive_seen = True
        self._pos_log: deque = deque(maxlen=120)
        real_set_position = player.set_position

        def counting_set_position(x, y, z, _real=real_set_position):
            self.teleports_after_spawn += 1
            self.notes.append(f"set_position after spawn to ({x:.1f},{y:.1f},{z:.1f})")
            return _real(x, y, z)

        player.set_position = counting_set_position
        # Infection kills and respawns the body inside one tick, so the death
        # itself is caught here, not by polling ``alive``.
        real_die = player.die
        self._clock = lambda: 0.0

        def recording_die(killer=None, kill_type=0, _real=real_die):
            if self.death_s is None and int(player.team) == SURVIVOR_TEAM:
                self.death_s = round(self._clock(), 2)
                self.killer_id = int(getattr(killer, "id", -1)) if killer is not None else None
                self.death_kill_type = int(kill_type)
                self.death_killer_is_zombie = bool(killer is not None and int(getattr(killer, "team", -1)) == ZOMBIE_TEAM)
            return _real(killer, kill_type)

        player.die = recording_die
        self.death_kill_type = None
        self.death_killer_is_zombie = None
        self.input.set_look_now(player.orientation)

    # -- state -------------------------------------------------------------------

    @property
    def alive(self) -> bool:
        p = self.player
        return bool(p.alive and p.spawned and int(p.team) == SURVIVOR_TEAM)

    @property
    def position(self):
        return (float(self.player.x), float(self.player.y), float(self.player.z))

    @property
    def eye(self):
        return tuple(float(v) for v in self.player.eye)

    def blocks_spent(self) -> int:
        return int(self._blocks_spent)

    def observe(self, t: float, zombies) -> None:
        p = self.player
        self._clock = lambda: t
        if self._last_label is not None and p.last_applied_input_loop == self._last_label:
            if bool(getattr(p, "last_applied_input_synthesized", False)):
                self.frames_synthesized += 1
            else:
                self.frames_applied += 1
        self._last_label = None
        if self.alive:
            # The wallet is read before the mode can swap it for a zombie's.
            now_blocks = int(p.blocks)
            if now_blocks < self._last_blocks:
                self._blocks_spent += self._last_blocks - now_blocks
            self._last_blocks = now_blocks
        if self.first_damage_s is None and (int(p.health) < self._start_health or not p.alive):
            self.first_damage_s = round(t, 2)
        if self.death_s is None and not p.alive:
            self.death_s = round(t, 2)
            self.killer_id = int(getattr(p, "last_killed_by_id", -1))
        if self.infected_s is None and int(p.team) != SURVIVOR_TEAM:
            self.infected_s = round(t, 2)
        if self.first_contact_s is None:
            for z in zombies:
                if z.alive and z.spawned and math.dist(z.position, self.position) <= 3.0:
                    self.first_contact_s = round(t, 2)
                    break
        self._pos_log.append((t, self.position))

    # -- tools ---------------------------------------------------------------

    def want_tool(self, t: float, tool: int) -> bool:
        """Select a tool like a client (0.3 s hand switch); True when held."""

        tool = int(tool)
        if int(self.player.tool) == tool and self.player.tool_is_raw:
            return True
        if self._tool_wanted != tool:
            self._tool_wanted = tool
            self._tool_ready_at = t + self.TOOL_SWITCH_SECONDS
            return False
        if t < self._tool_ready_at:
            return False
        if tool == self.weapon or equipped_tool_authorized(self.player, tool):
            # The ClientData handler's own gate; the gun of the armed model
            # (rifle, tool 6) is held even though no stock class carries it.
            self.player.set_tool(tool, raw=True)
            return int(self.player.tool) == tool
        self.notes.append(f"tool {tool} not authorized for this class")
        return False

    # -- per tick ------------------------------------------------------------

    def record_frame(self, t: float) -> None:
        """Feed this tick's input frame (the only way the body moves)."""

        p = self.player
        if not self.alive:
            return
        flags = self.input.frame(t)
        p.record_input_frame(self.server.loop_count, flags, self.input.look,
                             received_server_tick=self.server.loop_count)
        self._last_label = int(self.server.loop_count)
        # The frame's aim is applied after physics; the current aim is what
        # action validation compares against, like a client whose packet 4
        # arrived before its action packet.
        p.set_orientation_vector(*self.input.look)
        p.update_input(*flags)

    def settle_builds(self) -> None:
        """Confirm the cells of earlier BlockLines after their commit tick."""

        if not self._pending_cells:
            return
        keep = []
        for loop, cells in self._pending_cells:
            if self.server.loop_count - loop < 2:
                keep.append((loop, cells))
                continue
            for cell in cells:
                if self.world.get_solid(*cell):
                    self.build_cells_committed += 1
                else:
                    self.build_cells_failed += 1
        self._pending_cells = keep

    # -- building ------------------------------------------------------------

    def place_line(self, t: float, a, b) -> bool:
        """One BlockLine (drag start ``a``, hit cube ``b``) like the block tool."""

        if not self.want_tool(t, BLOCK_TOOL):
            return False
        if t - self._last_build_t < 0.12:
            return False  # the stock MIN_BLOCK_INTERVAL between placements
        packet = BlockLine()
        packet.loop_count = int(self.server.loop_count)
        packet.player_id = int(self.player.id)
        packet.x1, packet.y1, packet.z1 = (int(v) for v in a)
        packet.x2, packet.y2, packet.z2 = (int(v) for v in b)
        self.builds_sent += 1
        self._last_build_t = t
        accepted = bool(self.server.combat.handle_block_line(self.player, packet))
        if accepted:
            self.builds_accepted += 1
            cells = [c for c in self.server.combat.block_line_cells(tuple(a), tuple(b))
                     if not self.world.get_solid(*c)]
            self._pending_cells.append((int(self.server.loop_count), cells))
        else:
            self.builds_rejected += 1
        if len(self.build_log) < 400:
            self.build_log.append((round(t, 1), tuple(int(v) for v in a), tuple(int(v) for v in b),
                                   "ok" if accepted else "rejected"))
        return accepted

    def place_single(self, t: float, cell) -> bool:
        """BlockBuild (id 32): kept for completeness; the client sends lines."""

        if not self.want_tool(t, BLOCK_TOOL):
            return False
        packet = BlockBuild()
        packet.loop_count = int(self.server.loop_count)
        packet.player_id = int(self.player.id)
        packet.x, packet.y, packet.z = (int(v) for v in cell)
        packet.block_type = 0
        self.builds_sent += 1
        accepted = bool(self.server.combat.handle_block_build(self.player, packet))
        self.builds_accepted += int(accepted)
        self.builds_rejected += int(not accepted)
        return accepted

    def cell_reachable(self, cell, reach: float = 10.0) -> bool:
        centre = (cell[0] + 0.5, cell[1] + 0.5, cell[2] + 0.5)
        return math.dist(self.eye, centre) <= reach

    # -- digging -------------------------------------------------------------

    def dig(self, t: float, cell) -> bool:
        """One spade swing at ``cell`` (ShootPacket, like the client's dig)."""

        if not self.want_tool(t, SPADE_TOOL):
            return False
        eye = self.eye
        centre = (cell[0] + 0.5, cell[1] + 0.5, cell[2] + 0.5)
        direction = _norm((centre[0] - eye[0], centre[1] - eye[1], centre[2] - eye[2]))
        self.input.set_look_now(direction)
        self.player.set_orientation_vector(*direction)
        packet = self._shoot_packet(eye, direction)
        self.digs_sent += 1
        accepted = bool(self.server.combat.handle_shot(self.player, packet))
        self.digs_accepted += int(accepted)
        return accepted

    # -- shooting ------------------------------------------------------------

    def _shoot_packet(self, eye, direction) -> ShootPacket:
        packet = ShootPacket()
        loop = self.player.applied_loop
        packet.loop_count = int(loop if loop is not None else self.server.loop_count)
        packet.shooter_id = int(self.player.id)
        packet.shot_on_world_update = 0
        packet.x, packet.y, packet.z = eye
        packet.ori_x, packet.ori_y, packet.ori_z = direction
        packet.damage = 0
        packet.penetration = 0
        packet.affect_shooter = 0
        packet.secondary = 0
        packet.seed = self.rng.randrange(256)
        return packet

    def visible_target(self, zombies, *, max_range: float | None = None):
        """Nearest living zombie with a clear line from the eye to its torso."""

        eye = self.eye
        limit = self.RANGE if max_range is None else float(max_range)
        for z in sorted((b for b in zombies if b.alive and b.spawned),
                        key=lambda b: math.dist(b.position, self.position)):
            aim = (float(z.x), float(z.y), float(z.z) + self.TORSO_Z)
            d = math.dist(aim, eye)
            if d > limit:
                return None
            if d < 1e-6:
                continue
            direction = _norm((aim[0] - eye[0], aim[1] - eye[1], aim[2] - eye[2]))
            if self.world.raycast(eye[0], eye[1], eye[2], *direction, max(0.0, d - 0.6)) is not None:
                continue
            return z, aim, d
        return None

    def shoot_tick(self, t: float, zombies, *, allow: bool = True, cadence=(0.6, 0.9)) -> None:
        """The armed model: aim one tick, fire the next, reload when dry."""

        p = self.player
        if not self.alive or not allow:
            self._pending_shot = None
            return
        if self._pending_shot is not None:
            target, direction = self._pending_shot
            self._pending_shot = None
            if self.want_tool(t, self.weapon):
                self._fire(target, direction, zombies)
            return
        if t < self.next_shot_at:
            return
        if not self.want_tool(t, self.weapon):
            return
        clip = int(getattr(p, "ammo_clip", 0))
        if clip <= 0:
            if int(getattr(p, "ammo_reserve", 0)) <= 0:
                if self.resupply:
                    p.restock_ammo(int(getattr(C, "AMMO_CRATE", 3)))
                    self.notes.append(f"resupply at {t:.1f}s")
                else:
                    self.shots_blocked["dry"] += 1
                    self.next_shot_at = t + 1.0
                    return
            if not bool(getattr(p, "reloading", False)):
                self.server.combat.handle_weapon_reload(p)
            self.shots_blocked["reloading"] += 1
            self.next_shot_at = t + 0.3
            return
        found = self.visible_target(zombies)
        if found is None:
            self.shots_blocked["no_target_in_sight"] += 1
            self.next_shot_at = t + 0.25
            return
        target, aim, dist = found
        if self.rng.random() >= self.hit_chance:
            # A deliberate human miss: the aim point slides off the body.
            side = self.rng.choice((-1.0, 1.0))
            dx, dy = aim[0] - self.eye[0], aim[1] - self.eye[1]
            n = math.hypot(dx, dy) or 1.0
            off = self.rng.uniform(0.8, 1.3)
            aim = (aim[0] + side * off * (-dy / n), aim[1] + side * off * (dx / n),
                   aim[2] + self.rng.uniform(-0.3, 0.3))
            self.misses_intended += 1
        eye = self.eye
        direction = _norm((aim[0] - eye[0], aim[1] - eye[1], aim[2] - eye[2]))
        self.input.set_look_now(direction)
        p.set_orientation_vector(*direction)
        self._pending_shot = (target, direction)
        profile = WEAPON_PROFILES.get(self.weapon)
        gap = max(float(getattr(profile, "fire_interval", 0.5)) + 0.05, self.rng.uniform(*cadence))
        self.next_shot_at = t + gap

    def _fire(self, target, direction, zombies) -> None:
        p = self.player
        eye = self.eye
        before, alive_before = int(target.health), bool(target.alive)
        clip_before = int(getattr(p, "ammo_clip", 0))
        packet = self._shoot_packet(eye, direction)
        self.shots += 1
        accepted = bool(self.server.combat.handle_shot(p, packet))
        fired = accepted or int(getattr(p, "ammo_clip", 0)) < clip_before
        if int(target.health) < before or (alive_before and not target.alive):
            self.hits += 1
            if alive_before and not target.alive:
                self.kills += 1
        elif accepted:
            self.shots_blocked["hit_something_else"] += 1
        elif fired:
            self.shots_blocked["missed"] += 1
        else:
            self.shots_blocked["refused_by_server"] += 1

    # -- report --------------------------------------------------------------

    def report(self) -> dict:
        p = self.player
        return {
            "survivor_shots": self.shots,
            "survivor_hits": self.hits,
            "survivor_kills": self.kills,
            "survivor_intended_misses": self.misses_intended,
            "survivor_ammo_left": int(getattr(p, "ammo_clip", 0)) + int(getattr(p, "ammo_reserve", 0)),
            "shots_blocked": dict(self.shots_blocked),
            "builds_sent": self.builds_sent,
            "builds_accepted": self.builds_accepted,
            "builds_rejected": self.builds_rejected,
            "build_cells_committed": self.build_cells_committed,
            "build_cells_failed": self.build_cells_failed,
            "digs_sent": self.digs_sent,
            "digs_accepted": self.digs_accepted,
            "repairs": self.repairs,
            "blocks_spent": self.blocks_spent(),
            "teleports_after_spawn": self.teleports_after_spawn,
            "input_frames": self.input.frames,
            "input_frames_applied": self.frames_applied,
            "input_frames_synthesized": self.frames_synthesized,
            "input_frames_dropped": int(getattr(p, "input_frames_dropped", 0)),
            "input_starved_ticks": int(getattr(p, "input_starved_ticks", 0)),
            "build_log": self.build_log[:60],
            "look_changes": self.input.turns,
            "first_contact_s": self.first_contact_s,
            "first_damage_s": self.first_damage_s,
            "death_s": self.death_s,
            "death_kill_type": self.death_kill_type,
            "death_by_zombie": self.death_killer_is_zombie,
            "infected_s": self.infected_s,
            "killer_id": self.killer_id,
            "survivor_notes": self.notes[:12],
        }


# --------------------------------------------------------------------------- #
# movement primitives shared by the tactics
# --------------------------------------------------------------------------- #

class Walker:
    """Walk/sprint to waypoints by input frames; jumps when progress stalls."""

    def __init__(self, survivor: Survivor) -> None:
        self.s = survivor
        self.target = None
        self.sprint = False
        self._last_progress_t = 0.0
        self._last_d = math.inf
        self.stalls = 0
        self.arrivals = 0
        self.leg_started = 0.0
        self._sidestep_until = 0.0
        self._sidestep = None

    def go(self, t: float, target, *, sprint: bool = False, look_too: bool = True) -> None:
        self.target = (float(target[0]), float(target[1]))
        self.sprint = bool(sprint)
        self._last_progress_t = t
        self._last_d = math.inf
        self.leg_started = t
        self._leg_stalls = 0
        pos = self.s.position
        move = (self.target[0] - pos[0], self.target[1] - pos[1])
        kwargs = {"move": move, "sprint": sprint}
        if look_too:
            kwargs["look"] = (move[0], move[1], 0.08)
        self.s.input.request(t, **kwargs)

    def distance(self) -> float:
        if self.target is None:
            return 0.0
        pos = self.s.position
        return math.hypot(self.target[0] - pos[0], self.target[1] - pos[1])

    def update(self, t: float, *, tolerance: float = 0.7, look_too: bool = True) -> bool:
        """Steer toward the target; True once within ``tolerance`` (and stops)."""

        if self.target is None:
            return True
        d = self.distance()
        pos = self.s.position
        if d <= tolerance:
            self.target = None
            self.arrivals += 1
            self.s.input.stop(t)
            return True
        move = (self.target[0] - pos[0], self.target[1] - pos[1])
        if self._sidestep_until > t and self._sidestep is not None:
            # blocked twice: step aside for a moment, then come back at it
            move = self._sidestep
        kwargs = {"move": move, "sprint": self.sprint and self._sidestep_until <= t}
        if look_too:
            aim = (self.target[0] - pos[0], self.target[1] - pos[1])
            kwargs["look"] = (aim[0], aim[1], 0.08)
        self.s.input.request(t, immediate=True, **kwargs)
        if d < self._last_d - 0.15:
            self._last_d = d
            self._last_progress_t = t
        elif t - self._last_progress_t > 0.6:
            # blocked: hop first (one-block steps), then sidestep around
            self.stalls += 1
            self._leg_stalls = getattr(self, "_leg_stalls", 0) + 1
            self._last_progress_t = t
            if self._leg_stalls % 2 == 1:
                self.s.input.jump()
            else:
                n = math.hypot(move[0], move[1]) or 1.0
                side = 1.0 if (self._leg_stalls // 2) % 2 == 0 else -1.0
                self._sidestep = (-move[1] / n * side, move[0] / n * side)
                self._sidestep_until = t + 0.5
        return False

    def gave_up(self, limit: int = 6) -> bool:
        """Too many stalls on this leg: a human would pick another way."""

        return getattr(self, "_leg_stalls", 0) >= limit


# --------------------------------------------------------------------------- #
# tactics
# --------------------------------------------------------------------------- #

class Tactic:
    """Base: ``setup`` places the survivor and structure, ``update`` runs it."""

    name = "base"
    elevated = False
    horde_floor = None
    shoot_cadence = (0.6, 0.9)

    def __init__(self, gate, rng: random.Random, options: dict) -> None:
        self.gate = gate
        self.rng = rng
        self.options = options
        self.world = gate.world
        self.server = gate.server
        self.state = "init"
        self.state_since = 0.0
        self.built: list = []
        self.site = None
        self.info: dict = {}
        self.timeline: list = []

    # placement happens once; afterwards only inputs move the body
    def start_position(self):
        raise NotImplementedError

    def after_spawn(self, survivor: Survivor) -> None:
        self.s = survivor
        self.walker = Walker(survivor)

    def set_state(self, t: float, state: str) -> None:
        if state != self.state:
            self.timeline.append((round(t, 1), state))
            self.state = state
            self.state_since = t

    def in_state_for(self, t: float) -> float:
        return t - self.state_since

    def update(self, t: float, zombies) -> None:
        raise NotImplementedError

    def report(self) -> dict:
        return {"tactic_state": self.state, "tactic_timeline": self.timeline[:40],
                "structure_cells": len(self.built),
                "structure_cells_gone": sum(1 for c in self.built if not self.world.get_solid(*c)),
                **self.info}

    # -- shared behaviours ---------------------------------------------------

    def hold_and_shoot(self, t: float, zombies, *, fidget: bool = True, strafe: bool = True,
                       anchor=None, radius: float = 1.0) -> None:
        """Stand, shoot and idle; drift back to ``anchor`` (xy) past ``radius``."""

        s = self.s
        if anchor is not None:
            pos = s.position
            drift = math.hypot(pos[0] - anchor[0], pos[1] - anchor[1])
            if drift > radius:
                self._anchoring = True
            if getattr(self, "_anchoring", False):
                if drift <= max(0.25, radius * 0.4):
                    self._anchoring = False
                    s.input.sneak = False
                    s.input.stop(t)
                else:
                    s.input.sneak = True
                    s.input.request(t, move=(anchor[0] - pos[0], anchor[1] - pos[1]), sprint=False,
                                    immediate=True)
                    return
        s.shoot_tick(t, zombies, cadence=self.shoot_cadence)
        if fidget and s._pending_shot is None:
            s.input.fidget(t, strafe=strafe)

    def pillar_step(self, t: float, cells: list, target_height: int) -> bool:
        """Jump-and-place like a player; True when the pillar is finished.

        ``cells`` collects the placed voxels (for repairs). The survivor looks
        down, jumps, and at the apex places one block under its feet.
        """

        s = self.s
        base = feet_cell(self.world, s.player)
        if base is None:
            return False
        if not hasattr(self, "_pillar_jump_tick"):
            self._pillar_jump_tick = None
            self._pillar_base0 = base[2]
        done = self._pillar_base0 - base[2]
        if done >= target_height:
            self._pillar_jump_tick = None
            return True
        if not s.want_tool(t, BLOCK_TOOL):
            return False
        s.input.request(t, look=(0.15, 0.0, 0.98), immediate=True)
        s.input.stop(t)
        if self._pillar_jump_tick is None:
            # let the run's momentum die before the first jump (a slip puts
            # the next block in the neighbouring column)
            if self.in_state_for(t) < 0.4:
                return False
            if s.player.grounded and t - getattr(self, "_pillar_landed_t", -1.0) > 0.15:
                s.input.jump()
                self._pillar_jump_tick = self.server.loop_count
                self._pillar_placed = False
            return False
        since = self.server.loop_count - self._pillar_jump_tick
        rise = (base[2] - STAND) - s.player.z  # eye above its standing height
        if not self._pillar_placed and JUMP_PLACE_TICKS[0] <= since <= JUMP_PLACE_TICKS[1] and rise > 1.05:
            target = (base[0], base[1], base[2] - 1)
            if s.place_line(t, target, target):
                cells.append(target)
            self._pillar_placed = True
        if since > 50 or (since > 6 and s.player.grounded):
            self._pillar_jump_tick = None
            self._pillar_landed_t = t
        return False

    def repair_pillar(self, t: float, cells: list) -> bool:
        """Re-place a lost pillar voxel the player can still see and reach.

        Human pacing: one try every 0.8 s, and a cell the server refused three
        times is given up (it is hidden behind the pillar's own blocks).
        """

        s = self.s
        if t - s._last_repair_t < 0.8:
            return False
        base = feet_cell(self.world, s.player)
        if base is None:
            return False
        for cell in sorted(cells, key=lambda c: c[2]):
            if self.world.get_solid(*cell) or cell[2] <= base[2]:
                continue
            if not s.cell_reachable(cell) or s._repair_tries[cell] >= 3:
                continue
            # the player looks down at it; the server decides visibility
            eye = s.eye
            s.input.look_at(t, eye, (cell[0] + 0.5, cell[1] + 0.5, cell[2] + 0.5), immediate=True)
            s._last_repair_t = t
            s._repair_tries[cell] += 1
            if s.place_line(t, cell, cell):
                s.repairs += 1
                return True
            return False
        return False


class PillarTactic(Tactic):
    name = "pillar"
    elevated = True

    def start_position(self):
        world = self.world
        spawn = self.gate.survivor_spawn
        site = open_ground_near(world, self.rng, spawn)
        if site is None:
            site = (int(spawn[0]), int(spawn[1]), standing_height(world, int(spawn[0]), int(spawn[1])))
            self.info["pillar_site_fallback"] = "no open ground reachable: pillar at the spawn"
        self.site = site
        self.horde_floor = site[2]
        self.height = int(self.options.get("height", 8))
        self.cells: list = []
        self.info["site"] = list(site)
        return spawn

    def update(self, t: float, zombies) -> None:
        s = self.s
        if self.state == "init":
            self.walker.go(t, (self.site[0] + 0.5, self.site[1] + 0.5), sprint=True)
            self.set_state(t, "run_to_open_ground")
        elif self.state == "run_to_open_ground":
            if self.walker.update(t) or self.in_state_for(t) > 25.0:
                self.set_state(t, "pillar_up")
                if hasattr(self, "_pillar_jump_tick"):
                    del self._pillar_jump_tick
        elif self.state == "pillar_up":
            if self.pillar_step(t, self.cells, self.height):
                self.info.setdefault("pillar_done_s", round(t, 1))
                self.set_state(t, "hold")
                s.input.request(t, look=(1.0, 0.0, 0.3), immediate=True)
            elif self.in_state_for(t) > 45.0:
                self.info["pillar_timeout"] = True
                self.set_state(t, "hold")
            elif self.rng.random() < 0.02:
                # a glance at the horde between two jumps
                found = s.visible_target(zombies)
                if found is not None and self.s._pending_shot is None and s.player.grounded:
                    self.hold_and_shoot(t, zombies, fidget=False)
        elif self.state == "hold":
            base = feet_cell(self.world, s.player)
            lost = [c for c in self.cells if not self.world.get_solid(*c)]
            if lost and base is not None and base[2] < self.site[2]:
                if self.repair_pillar(t, self.cells):
                    return
            if base is not None and base[2] >= self.site[2] and self.cells:
                # the pillar fell with us: build again if still alive
                self.info["collapses"] = self.info.get("collapses", 0) + 1
                self.cells = []
                if hasattr(self, "_pillar_jump_tick"):
                    del self._pillar_jump_tick
                self.set_state(t, "pillar_up")
                return
            # a one-block top: no shuffling, and stay centred on it
            top = self.cells[-1] if self.cells else None
            anchor = (top[0] + 0.5, top[1] + 0.5) if top else None
            self.hold_and_shoot(t, zombies, strafe=False, anchor=anchor, radius=0.3)


class RoofTactic(Tactic):
    name = "roof"
    elevated = True

    def start_position(self):
        from bot_zombie_horde_scenario import _rooftop_site

        world = self.world
        rx, ry, rtop = _rooftop_site(world, self.rng)
        self.roof = (rx, ry, rtop)
        self.horde_floor = rtop
        self.height = int(self.options.get("height", 6))
        self.cells: list = []
        # street start 8-14 blocks from the roof, at street level
        start = None
        for _ in range(60):
            angle = self.rng.uniform(0, 2 * math.pi)
            radius = self.rng.uniform(8, 14)
            cx, cy = int(rx + radius * math.cos(angle)), int(ry + radius * math.sin(angle))
            if not (4 < cx < 507 and 4 < cy < 507) or world.is_water_column(cx, cy):
                continue
            h = standing_height(world, cx, cy)
            if h >= rtop + 5 and not world.get_solid(cx, cy, h - 1) and not world.get_solid(cx, cy, h - 2):
                start = (cx, cy, h)
                break
        roof_cells = [(rx + dx, ry + dy) for dx in range(-2, 3) for dy in range(-2, 3)]
        self.route = None
        if start is not None:
            path = heightmap_path(world, start, roof_cells)
            if path is not None:
                self.route = simplify_path(path, world)
        self.site = self.roof
        self.info["roof"] = list(self.roof)
        if self.route:
            self.info["roof_access"] = f"walk {len(self.route)} waypoints"
            return (start[0] + 0.5, start[1] + 0.5, start[2] - STAND)
        self.info["roof_access"] = "placement (no exterior stairs/terrain route)"
        return (rx + 0.5, ry + 0.5, rtop - STAND)

    def update(self, t: float, zombies) -> None:
        s = self.s
        if self.state == "init":
            if self.route:
                self._wp = 0
                self.walker.go(t, self.route[0], sprint=True)
                self.set_state(t, "climb_route")
            else:
                self.set_state(t, "pillar_up")
        elif self.state == "climb_route":
            if self.walker.update(t):
                self._wp += 1
                if self._wp >= len(self.route):
                    self.set_state(t, "pillar_up")
                else:
                    self.walker.go(t, self.route[self._wp], sprint=True)
            on_roof = feet_cell(self.world, s.player)
            if on_roof is not None and on_roof[2] <= self.roof[2] and math.hypot(s.position[0] - self.roof[0] - 0.5, s.position[1] - self.roof[1] - 0.5) < 4:
                self.set_state(t, "pillar_up")
            elif self.in_state_for(t) > 30.0:
                self.info["roof_access"] += "; timed out after 30 s, pillar where we stand"
                self.set_state(t, "pillar_up")
        elif self.state == "pillar_up":
            if self.pillar_step(t, self.cells, self.height):
                self.info.setdefault("pillar_done_s", round(t, 1))
                self.set_state(t, "hold")
            elif self.in_state_for(t) > 40.0:
                self.set_state(t, "hold")
        elif self.state == "hold":
            lost = [c for c in self.cells if not self.world.get_solid(*c)]
            if lost and self.repair_pillar(t, self.cells):
                return
            top = self.cells[-1] if self.cells else None
            anchor = (top[0] + 0.5, top[1] + 0.5) if top else None
            self.hold_and_shoot(t, zombies, strafe=False, anchor=anchor, radius=0.3)


class TreeTactic(Tactic):
    name = "tree"
    elevated = True

    def start_position(self):
        from bot_zombie_horde_scenario import _flat_site, build_structure

        world = self.world
        x, y, floor = _flat_site(world, self.rng, level=True, span=6)
        span = int(self.options.get("bridge_span", 40))
        clearance = floor - 18
        direction = (1, 0)
        for candidate in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            if all(0 < x + candidate[0] * k < 511 and 0 < y + candidate[1] * k < 511
                   and standing_height(world, x + candidate[0] * k, y + candidate[1] * k) > clearance + 1
                   for k in range(3, span + 1)):
                direction = candidate
                break
        tops, self.built = build_structure(world, "treebridge", x, y, floor, bridge_span=span,
                                           direction=direction)
        self.site = (x, y, floor)
        self.horde_floor = floor
        self.direction = direction
        self.span = span
        self.walk_z = floor - 18
        dx, dy = direction
        self.walkway = [(x + dx * k, y + dy * k) for k in range(4, span)]
        self.end_cell = (x + dx * (span - 1), y + dy * (span - 1))
        self.start_cell = (x + dx * 3, y + dy * 3)
        self.info["site"] = list(self.site)
        self.info["direction"] = list(direction)
        return (self.start_cell[0] + 0.5, self.start_cell[1] + 0.5, (floor - 19) - STAND)

    def _zombie_on_walkway(self, zombies):
        dx, dy = self.direction
        for z in zombies:
            if not (z.alive and z.spawned):
                continue
            # perpendicular distance from the walkway line and level with it
            if abs(z.z - (self.walk_z - STAND)) > 3.5:
                continue
            k = (z.x - (self.site[0] + 0.5)) * dx + (z.y - (self.site[1] + 0.5)) * dy
            off = abs((z.x - (self.site[0] + 0.5)) * dy - (z.y - (self.site[1] + 0.5)) * dx)
            if 3.0 <= k <= self.span + 0.5 and off <= 0.9:
                return z, k
        return None

    def _walk_cell(self, k: int):
        k = max(4, min(self.span - 1, int(k)))
        dx, dy = self.direction
        return (self.site[0] + dx * k + 0.5, self.site[1] + dy * k + 0.5)

    def _k_of(self, pos) -> float:
        dx, dy = self.direction
        return (pos[0] - (self.site[0] + 0.5)) * dx + (pos[1] - (self.site[1] + 0.5)) * dy

    def update(self, t: float, zombies) -> None:
        s = self.s
        if self.state == "init":
            self.walker.go(t, self._walk_cell(self.span - 1), sprint=False)
            self.set_state(t, "walk_out")
        elif self.state == "walk_out":
            # stay on the centre line of the one-wide walkway
            if self.walker.update(t, tolerance=0.5) or self.in_state_for(t) > 40.0:
                self.set_state(t, "hold")
                s.input.request(t, look=(-self.direction[0], -self.direction[1], 0.35), immediate=True)
        elif self.state == "hold":
            hit = self._zombie_on_walkway(zombies)
            if hit is not None:
                z, k = hit
                my_k = self._k_of(s.position)
                away = my_k - 10 if k > my_k else my_k + 10
                self.walker.go(t, self._walk_cell(away), sprint=True)
                self.info["retreats"] = self.info.get("retreats", 0) + 1
                self.set_state(t, "retreat")
                return
            # one block wide: no shuffling, stay on the centre line
            k = round(self._k_of(s.position))
            self.hold_and_shoot(t, zombies, strafe=False, anchor=self._walk_cell(k), radius=0.3)
        elif self.state == "retreat":
            if self.walker.update(t, tolerance=0.5) or self.in_state_for(t) > 8.0:
                self.set_state(t, "hold")
            # shoot the walkway zombie while backing off
            self.s.shoot_tick(t, zombies, cadence=(0.5, 0.7))
        if feet_cell(self.world, s.player) is not None and feet_cell(self.world, s.player)[2] >= self.site[2] - 2:
            self.info.setdefault("fell_to_ground_s", round(t, 1))


class RunnerTactic(Tactic):
    name = "runner"
    elevated = False
    shoot_cadence = (0.7, 1.1)

    def start_position(self):
        world = self.world
        spawn = self.gate.survivor_spawn
        sx, sy = float(spawn[0]), float(spawn[1])
        # four street waypoints around the spawn with walkable legs
        best = None
        for _ in range(300):
            pts = []
            for q in range(4):
                a = q * math.pi / 2 + self.rng.uniform(-0.5, 0.5)
                r = self.rng.uniform(14, 28)
                pts.append((sx + r * math.cos(a), sy + r * math.sin(a)))
            ok = True
            for i in range(4):
                a, b = pts[i], pts[(i + 1) % 4]
                if not (6 < a[0] < 505 and 6 < a[1] < 505) or world.is_water_column(int(a[0]), int(a[1])):
                    ok = False
                    break
                if not walkable_segment(world, a, b):
                    ok = False
                    break
            if ok:
                loop_len = sum(math.dist(pts[i], pts[(i + 1) % 4]) for i in range(4))
                if best is None or loop_len > best[0]:
                    best = (loop_len, pts)
        if best is None:
            pts = [(sx + 10, sy), (sx, sy + 10), (sx - 10, sy), (sx, sy - 10)]
            self.info["runner_loop"] = "fallback small loop"
        else:
            pts = best[1]
            self.info["runner_loop_len"] = round(best[0], 1)
        self.waypoints = [(x, y, standing_height(world, int(x), int(y))) for x, y in pts]
        self.info["waypoints"] = [(round(x, 1), round(y, 1)) for x, y, _ in self.waypoints]
        # one water crossing if a far bank exists within 30 blocks
        self.crossing = None
        for wx in range(int(sx) - 80, int(sx) + 81, 2):
            for wy in range(int(sy) - 80, int(sy) + 81, 2):
                if not (4 < wx < 507 and 4 < wy < 507) or not world.is_water_column(wx, wy):
                    continue
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    width = 0
                    cx, cy = wx, wy
                    while 1 < cx < 510 and 1 < cy < 510 and world.is_water_column(cx, cy) and width < 24:
                        cx += dx
                        cy += dy
                        width += 1
                    near = (wx - dx, wy - dy)
                    if (2 <= width < 24 and 1 < cx < 510 and 1 < cy < 510 and not world.is_water_column(cx, cy)
                            and not world.is_water_column(*near)):
                        d = math.hypot(near[0] - sx, near[1] - sy)
                        if d < 45 and (self.crossing is None or d < self.crossing[0]):
                            self.crossing = (d, near, (cx + dx, cy + dy))
        self.info["water_crossing"] = (
            {"from": list(self.crossing[1]), "to": list(self.crossing[2])}
            if self.crossing else "unavailable (no far bank within reach on this map)")
        self.site = (int(sx), int(sy), standing_height(world, int(sx), int(sy)))
        self._wp = 0
        self.laps = 0
        self._crossed = False
        return spawn

    def update(self, t: float, zombies) -> None:
        s = self.s
        if self.state == "init":
            self.walker.go(t, self.waypoints[0], sprint=True)
            self.set_state(t, "run")
        elif self.state == "run":
            arrived = self.walker.update(t, tolerance=1.2)
            if arrived or t - self.walker.leg_started > 20.0 or self.walker.gave_up():
                if not arrived:
                    self.info["legs_abandoned"] = self.info.get("legs_abandoned", 0) + 1
                self._wp = (self._wp + 1) % len(self.waypoints)
                if self._wp == 0:
                    self.laps += 1
                    self.info["laps"] = self.laps
                if self.crossing and not self._crossed and self.laps >= 1:
                    self._crossed = True
                    self.walker.go(t, self.crossing[1], sprint=True)
                    self.set_state(t, "to_water")
                    return
                self.walker.go(t, self.waypoints[self._wp], sprint=True)
                self.set_state(t, "run")
            # shoot on the move: a short walk-and-fire burst, then sprint on
            found = s.visible_target(zombies, max_range=30.0)
            if found is not None and t - getattr(self, "_last_burst", -9.0) > 3.0:
                self._last_burst = t
                self.set_state(t, "burst")
                self.walker.sprint = False
        elif self.state == "burst":
            self.walker.update(t, tolerance=1.2, look_too=False)
            s.shoot_tick(t, zombies, cadence=(0.5, 0.7))
            if self.in_state_for(t) > 1.6:
                self.walker.sprint = True
                self.set_state(t, "run")
        elif self.state == "to_water":
            if self.walker.update(t, tolerance=1.0) or self.in_state_for(t) > 20.0:
                self.walker.go(t, self.crossing[2], sprint=True)
                self.set_state(t, "swim")
        elif self.state == "swim":
            if self.walker.update(t, tolerance=1.0) or self.in_state_for(t) > 25.0:
                self.info["water_crossed_s"] = round(t, 1)
                self.walker.go(t, self.waypoints[self._wp], sprint=True)
                self.set_state(t, "run")


class BunkerTactic(Tactic):
    name = "bunker"
    elevated = False

    def start_position(self):
        from bot_zombie_horde_scenario import build_structure

        world = self.world
        spawn = self.gate.survivor_spawn
        room = find_street_room(world, self.rng, spawn)
        if room is not None:
            inside, door, outside, floor_z, door_height = room
            self.info["room"] = "map interior"
        else:
            # no usable interior: a 5x5 hut on open ground with a doorway cut
            site = open_ground_near(world, self.rng, spawn, min_d=6, max_d=24, span=4)
            if site is None:
                site = (int(spawn[0]) + 6, int(spawn[1]), standing_height(world, int(spawn[0]) + 6, int(spawn[1])))
            x, y, floor = site
            tops, self.built = build_structure(world, "room", x, y, floor)
            for z in (floor - 1, floor - 2, floor - 3):   # a walk-in doorway on the +x wall
                world.set_block(x + 3, y, z, False, 0)
            inside, door, outside, floor_z, door_height = (x, y), (x + 3, y), (x + 5, y), floor, 3
            self.info["room"] = f"synthetic hut at {site}"
        self.inside, self.door, self.outside, self.floor_z = inside, door, outside, floor_z
        self.door_height = int(door_height)
        self.site = (door[0], door[1], floor_z)
        self.door_cells = [(door[0], door[1], floor_z - 1 - k) for k in range(self.door_height)]
        self.hole = self.door_cells[-1]   # the top cell stays open to shoot through
        self.seal_cells = self.door_cells[:-1]
        self.info["door"] = list(self.site)
        self.info["door_height"] = self.door_height
        self.info["inside"] = list(inside)
        # start outside the door, two blocks further out
        dx, dy = outside[0] - door[0], outside[1] - door[1]
        sx, sy = outside[0] + dx // 2 if dx else outside[0], outside[1] + dy // 2 if dy else outside[1]
        return (sx + 0.5, sy + 0.5, standing_height(world, sx, sy) - STAND)

    def _seal(self, t: float) -> bool:
        missing = [c for c in self.seal_cells if not self.world.get_solid(*c)]
        if not missing:
            return False
        # one BlockLine for the whole gap, like a drag over the doorway
        a, b = missing[0], missing[-1]
        self.s.input.look_at(t, self.s.eye, (a[0] + 0.5, a[1] + 0.5, a[2] + 0.5), immediate=True)
        return self.s.place_line(t, a, b)

    def update(self, t: float, zombies) -> None:
        s = self.s
        if self.state == "init":
            self.walker.go(t, (self.door[0] + 0.5, self.door[1] + 0.5), sprint=False)
            s.input.crouch = self.door_height < 3   # duck through a low doorway
            self.set_state(t, "to_door")
        elif self.state == "to_door":
            if self.walker.update(t, tolerance=0.5) or self.in_state_for(t) > 12.0:
                self.walker.go(t, (self.inside[0] + 0.5, self.inside[1] + 0.5))
                self.set_state(t, "enter")
        elif self.state == "enter":
            if self.walker.update(t, tolerance=0.45) or self.in_state_for(t) > 8.0:
                s.input.crouch = False
                # step one more cell back from the door when the room allows
                dx, dy = self.inside[0] - self.door[0], self.inside[1] - self.door[1]
                back = (self.inside[0] + dx, self.inside[1] + dy)
                if (not self.world.get_solid(back[0], back[1], self.floor_z - 1)
                        and not self.world.get_solid(back[0], back[1], self.floor_z - 2)
                        and self.world.get_solid(back[0], back[1], self.floor_z)):
                    self.walker.go(t, (back[0] + 0.5, back[1] + 0.5))
                self.set_state(t, "step_back")
        elif self.state == "step_back":
            if self.walker.update(t, tolerance=0.45) or self.in_state_for(t) > 4.0:
                self.set_state(t, "seal")
        elif self.state == "seal":
            if self._seal(t):
                self.info["seals"] = self.info.get("seals", 0) + 1
            if all(self.world.get_solid(*c) for c in self.seal_cells) or self.in_state_for(t) > 6.0:
                if not all(self.world.get_solid(*c) for c in self.seal_cells):
                    self.info["seal_failed"] = True
                self.set_state(t, "hold")
                hole = self.hole
                s.input.look_at(t, s.eye, (hole[0] + 0.5, hole[1] + 0.5, hole[2] + 0.5), immediate=True)
        elif self.state == "hold":
            if any(not self.world.get_solid(*c) for c in self.seal_cells):
                if t - getattr(self, "_last_reseal", -9.0) > 0.8:
                    self._last_reseal = t
                    if self._seal(t):
                        s.repairs += 1
                        self.info["reseals"] = self.info.get("reseals", 0) + 1
                    return
            self.hold_and_shoot(t, zombies)


class NeckTactic(Tactic):
    name = "neck"
    elevated = True

    def start_position(self):
        from bot_zombie_horde_scenario import _wall_site, build_structure

        world = self.world
        x, y, floor, wall_direction = _wall_site(world, self.rng)
        wx, wy = x + wall_direction[0], y + wall_direction[1]
        away = (-wall_direction[0], -wall_direction[1])
        neck_z = max(floor - 12, standing_height(world, wx, wy) + 2)
        for candidate in sorted(range(neck_z - 3, floor - 3), key=lambda z: abs(z - neck_z)):
            if world.get_solid(wx, wy, candidate) and not world.get_solid(x, y, candidate):
                neck_z = candidate
                break
        tops, self.built = build_structure(world, "wallneck", wx, wy, floor, height=floor - neck_z,
                                           direction=away)
        self.site = (x, y, floor)
        self.horde_floor = floor
        self.away = away
        self.top = neck_z
        self.neck = [(wx + away[0] * k, wy + away[1] * k, neck_z) for k in (1, 2)]
        self.platform_centre = (wx + away[0] * 5, wy + away[1] * 5)
        self.edge = (wx + away[0] * 3 + 0.5, wy + away[1] * 3 + 0.5)
        self.thickened: list = []
        self.info["site"] = list(self.site)
        self.info["neck"] = [list(c) for c in self.neck]
        tx, ty, tz = tops[0]
        return (tx + 0.5, ty + 0.5, tz - STAND)

    def _neck_signature(self) -> tuple:
        damage = getattr(self.world, "block_damage", {})
        return tuple((bool(self.world.get_solid(*cell)), round(float(damage.get(cell, 0.0)), 1))
                     for cell in self.neck)

    def _neck_threatened(self) -> bool:
        """New damage or a lost voxel since the last thickening session."""

        signature = self._neck_signature()
        if signature == getattr(self, "_handled_signature", None):
            return False
        return any(not solid or damage > 0.0 for solid, damage in signature)

    def _thicken_candidates(self):
        """Cells that make the neck thicker: beside/under/over each neck voxel."""

        out = []
        ax, ay = self.away
        tries = self.s._repair_tries
        for cx, cy, cz in self.neck:
            if not self.world.get_solid(cx, cy, cz):
                out.append((cx, cy, cz))        # the lost voxel itself first
            # what a player on the platform can see: on top (z-1 is up in
            # AoS), the two sides, and only then the underside
            for dx, dy, dz in ((0, 0, -1), (ay, ax, 0), (-ay, -ax, 0), (0, 0, 1)):
                cell = (cx + dx, cy + dy, cz + dz)
                if 0 <= cell[2] < 239 and not self.world.get_solid(*cell):
                    out.append(cell)
        # a cell the server refused twice is not placeable from here
        return [cell for cell in out if tries[cell] < 2]

    def update(self, t: float, zombies) -> None:
        s = self.s
        if self.state == "init":
            s.input.request(t, look=(-self.away[0], -self.away[1], 0.3), immediate=True)
            self.set_state(t, "hold")
        elif self.state == "hold":
            if self._neck_threatened() and int(s.player.blocks) > 0:
                self.walker.go(t, self.edge, sprint=False)
                self.set_state(t, "to_edge")
                self.info["neck_alarms"] = self.info.get("neck_alarms", 0) + 1
                return
            centre = (self.platform_centre[0] + 0.5, self.platform_centre[1] + 0.5)
            self.hold_and_shoot(t, zombies, anchor=centre, radius=1.2)
        elif self.state == "to_edge":
            if self.walker.update(t, tolerance=0.5) or self.in_state_for(t) > 4.0:
                self.set_state(t, "thicken")
        elif self.state == "thicken":
            if int(s.player.blocks) <= 0:
                self.info["out_of_blocks_s"] = round(t, 1)
                self._handled_signature = self._neck_signature()
                self.set_state(t, "hold")
                return
            candidates = [c for c in self._thicken_candidates() if s.cell_reachable(c)]
            session_placed = getattr(self, "_session_placed", 0)
            if not candidates or session_placed >= 4 or self.in_state_for(t) > 8.0:
                # done with this alarm: remember the neck as it is now
                self._handled_signature = self._neck_signature()
                self._session_placed = 0
                self.set_state(t, "hold")
                return
            if not s.want_tool(t, BLOCK_TOOL):
                return   # hands still switching to the block tool
            if t - s._last_build_t >= 0.4:   # human pacing between placements
                cell = candidates[0]
                s.input.look_at(t, s.eye, (cell[0] + 0.5, cell[1] + 0.5, cell[2] + 0.5), immediate=True)
                s._repair_tries[cell] += 1
                if s.place_line(t, cell, cell):
                    s.repairs += 1
                    self.thickened.append(cell)
                    self._session_placed = session_placed + 1
            elif s._pending_shot is None and self.rng.random() < 0.3:
                s.shoot_tick(t, zombies, cadence=(0.6, 0.9))   # a shot between placements
            # stay on the edge cell while working (the walker stops on arrival)
            pos = s.position
            if self.walker.target is None and math.hypot(pos[0] - self.edge[0], pos[1] - self.edge[1]) > 0.7:
                self.walker.go(t, self.edge, look_too=False)
            if self.walker.target is not None:
                self.walker.update(t, tolerance=0.3, look_too=False)
        feet = feet_cell(self.world, s.player)
        if feet is not None and feet[2] >= self.site[2] - 2:
            self.info.setdefault("fell_to_ground_s", round(t, 1))


TACTIC_CLASSES = {
    "pillar": PillarTactic, "roof": RoofTactic, "tree": TreeTactic,
    "runner": RunnerTactic, "bunker": BunkerTactic, "neck": NeckTactic,
}


# --------------------------------------------------------------------------- #
# metrics
# --------------------------------------------------------------------------- #

class _AIErrorCounter(logging.Handler):
    """Counts ERROR records from the bot runtime (worker decision failures)."""

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.count = 0
        self.first = None

    def emit(self, record: logging.LogRecord) -> None:
        if not record.name.startswith("server.bot_ai") and "bot" not in record.getMessage().lower():
            return
        self.count += 1
        if self.first is None:
            text = record.getMessage()
            if record.exc_info and record.exc_info[1] is not None:
                text += f" :: {type(record.exc_info[1]).__name__}: {record.exc_info[1]}"
            self.first = text[:300]


class FunMetrics:
    """The horde classifier plus the gate's own counters.

    Wraps ``bot_zombie_horde_scenario._Metrics`` (activity shares,
    ``useful_share``, pile, ``ring_idle_share``, sides, longest still,
    ``first_at_height_s``, min distances) and adds: zombie deaths/respawns,
    never-within-10, stuck zombies (6 s still, not clawing, goal > 3 blocks
    away), prefab stacking events and how many were wasted, tick timings.
    """

    def __init__(self, server, director, zombies, survivors, *, horde_floor=None, elevated: bool) -> None:
        from bot_zombie_horde_scenario import _Metrics

        self.server = server
        self.director = director
        self.zombies = list(zombies)
        self.survivors = list(survivors)
        self.elevated = bool(elevated)
        self.inner = _Metrics(zombies, survivors, 0.0, horde_floor=horde_floor)
        self.alive_seen = {z.id: True for z in zombies}
        self.deaths = 0
        self.respawns = 0
        self.stuck_events: list = []
        self._stuck_flag = {z.id: False for z in zombies}
        self._still_hist = {z.id: deque() for z in zombies}
        self.stacks: list = []           # dicts: bot, t, pos, z_at, climbed, checked
        self._last_feedback_at = {z.id: 0.0 for z in zombies}
        self.build_actions = Counter()
        self.tick_ms: list = []
        self.samples = 0
        # A horde frozen by a worker exception is not "bad AI": count them.
        self.ai_errors = _AIErrorCounter()
        logging.getLogger("server.bot_ai").addHandler(self.ai_errors)
        logging.getLogger("server").addHandler(self.ai_errors)

    def sample(self, t: float, living) -> None:
        world = self.server.world_manager
        self.inner.sample(t, self.director, living, world, {}, -1, self.elevated)
        self.samples += 1
        runtimes = getattr(self.director, "_runtime", {}) or {}
        for bot in self.zombies:
            is_alive = bool(bot.alive and bot.spawned)
            was = self.alive_seen.get(bot.id, is_alive)
            if was != is_alive:
                if is_alive:
                    self.respawns += 1
                else:
                    self.deaths += 1
            self.alive_seen[bot.id] = is_alive
            hist = self._still_hist[bot.id]
            if not is_alive or not living:
                hist.clear()
                self._stuck_flag[bot.id] = False
                continue
            runtime = runtimes.get(bot.id)
            intent = getattr(runtime, "intent", None)
            pos = tuple(bot.position)
            hist.append((t, pos))
            while hist and t - hist[0][0] > STUCK_SECONDS + 1e-6:
                hist.popleft()
            clawing = (runtime is not None and getattr(runtime, "feedback_action_kind", "") == "melee"
                       and time.monotonic() - float(getattr(runtime, "feedback_action_at", 0.0)) < 2.0)
            goal = getattr(intent, "debug_goal", None) if intent is not None else None
            try:
                goal_far = goal is None or len(goal) < 3 or math.dist(tuple(goal)[:3], pos) > 3.0
            except (TypeError, ValueError):
                goal_far = True
            near_survivor = any(math.dist(s.position, pos) <= 3.0 for s in living)
            if hist and t - hist[0][0] >= STUCK_SECONDS - 1e-6:
                moved = math.dist(hist[0][1], pos)
                if moved < STUCK_MOVE and not clawing and goal_far and not near_survivor:
                    if not self._stuck_flag[bot.id]:
                        self._stuck_flag[bot.id] = True
                        self.stuck_events.append({
                            "t": round(t, 1), "bot": int(bot.id),
                            "pos": tuple(round(v, 1) for v in pos),
                            "role": str(getattr(intent, "debug_role", "")) if intent is not None else "",
                            "goal": tuple(round(float(v), 1) for v in goal) if goal is not None else None,
                        })
                elif moved >= STUCK_MOVE:
                    self._stuck_flag[bot.id] = False
            # stacking events: judged 10 s later (did the bot climb?)
            for stack in self.stacks:
                if stack["bot"] != bot.id or stack["checked"]:
                    continue
                if stack["z_at"] - pos[2] >= 0.8 and math.dist(stack["pos"][:2], pos[:2]) < 8.0:
                    stack["climbed"] = True
                if t - stack["t"] >= 10.0:
                    stack["checked"] = True

    def tick(self, t: float) -> None:
        """Per-tick: catch every accepted prefab/build action of a zombie."""

        runtimes = getattr(self.director, "_runtime", {}) or {}
        for bot in self.zombies:
            runtime = runtimes.get(bot.id)
            if runtime is None:
                continue
            at = float(getattr(runtime, "feedback_action_at", 0.0))
            if at <= self._last_feedback_at.get(bot.id, 0.0):
                continue
            self._last_feedback_at[bot.id] = at
            kind = str(getattr(runtime, "feedback_action_kind", ""))
            accepted = bool(getattr(runtime, "feedback_action_accepted", False))
            if kind in ("place_prefab", "build", "build_line"):
                self.build_actions[f"{kind}:{'ok' if accepted else 'rejected'}"] += 1
                if kind == "place_prefab" and accepted:
                    position = getattr(runtime, "feedback_action_position", None) or tuple(bot.position)
                    self.stacks.append({"bot": int(bot.id), "t": round(t, 1),
                                        "pos": tuple(float(v) for v in position),
                                        "z_at": float(bot.z), "climbed": False, "checked": False})

    def report(self, zombie_count: int) -> dict:
        out = self.inner.report()
        never_within_10 = [z.id for z in self.zombies if self.inner.min_distance.get(z.id, math.inf) > 10.0]
        wasted = [s for s in self.stacks if s["checked"] and not s["climbed"]]
        shares = out.get("activity_share", {})
        self.tick_ms.sort()
        n = len(self.tick_ms)
        out.update({
            "zombie_deaths": self.deaths,
            "zombie_respawns": self.respawns,
            "zombies_never_within_10": len(never_within_10),
            "zombies_never_within_10_ids": never_within_10,
            "stuck_zombies": len({e["bot"] for e in self.stuck_events}),
            "stuck_events": self.stuck_events[:16],
            "stacking_events": len(self.stacks),
            "stacking_wasted": len(wasted),
            "stacking_unjudged": sum(1 for s in self.stacks if not s["checked"]),
            "zombie_build_actions": dict(self.build_actions),
            "idle_share": round(shares.get("standing", 0.0) + shares.get("wandering", 0.0), 3),
            "tick_p50_ms": round(self.tick_ms[n // 2], 2) if n else 0.0,
            "tick_p99_ms": round(self.tick_ms[int(0.99 * (n - 1))], 2) if n else 0.0,
            "tick_max_ms": round(self.tick_ms[-1], 2) if n else 0.0,
            "samples": self.samples,
            "bot_ai_errors": self.ai_errors.count,
            "bot_ai_first_error": self.ai_errors.first,
        })
        for name in ("server.bot_ai", "server"):
            logging.getLogger(name).removeHandler(self.ai_errors)
        for key in ("reach_s", "idle_roles", "idle_orders", "walking_notes", "escalations"):
            out.pop(key, None)
        return out


# --------------------------------------------------------------------------- #
# thresholds
# --------------------------------------------------------------------------- #

DEFAULT_THRESHOLDS = {
    "max_infection_s": 60.0,          # pillar/roof/tree/neck/bunker
    "max_infection_runner_s": 120.0,
    "max_stuck_zombies": 0,
    "max_pile_mean": 1.5,
    "max_idle_share": 0.2,
}


def judge(result: dict, thresholds: dict) -> dict:
    """PASS/FAIL per threshold; ``infected_s`` None counts as a miss."""

    tactic = result.get("tactic")
    limit = thresholds["max_infection_runner_s"] if tactic == "runner" else thresholds["max_infection_s"]
    infected = result.get("infected_s")
    checks = {
        "infection": infected is not None and infected <= limit,
        "stuck": int(result.get("stuck_zombies", 0)) <= int(thresholds["max_stuck_zombies"]),
        "pile": float(result.get("pile_mean", 0.0) or 0.0) < float(thresholds["max_pile_mean"]),
        "idle": float(result.get("idle_share", 0.0) or 0.0) < float(thresholds["max_idle_share"]),
    }
    checks["all"] = all(checks.values())
    return checks


def summary_lines(result: dict) -> list:
    """One-screen text summary of one run."""

    s = result
    lines = [
        f"== zombie fun gate: {s.get('map')} tactic={s.get('tactic')} seed={s.get('seed')} bots={s.get('zombies')} "
        f"duration={s.get('duration_s')}s wall={s.get('wall_s')}s hit_chance={s.get('hit_chance')}"
        f"{' (real server)' if s.get('real_server') else ''} zombie start distance={s.get('zombie_start_distance_mean')}",
        f"site={s.get('site')} start={s.get('survivor_start')} {s.get('roof_access') or s.get('room') or ''}",
        f"first zombie within 3: {s.get('first_contact_s')} s   first damage: {s.get('first_damage_s')} s   "
        f"death: {s.get('death_s')} s   infected: {s.get('infected_s')} s   killer id: {s.get('killer_id')}",
        f"zombie deaths/respawns: {s.get('zombie_deaths')}/{s.get('zombie_respawns')}   never within 10: "
        f"{s.get('zombies_never_within_10')}   stuck zombies: {s.get('stuck_zombies')}   "
        f"longest still: {s.get('longest_still_max_s')} s",
        f"pile mean/max/seconds: {s.get('pile_mean')}/{s.get('pile_max')}/{s.get('pile_seconds')}   ring idle: "
        f"{s.get('ring_idle_share')}   first at height: {s.get('first_at_height_s')} s   sides used: {s.get('sides_used')}",
        f"activity: {s.get('activity_share')}  useful={s.get('useful_share')} idle={s.get('idle_share')}",
        f"stacking events: {s.get('stacking_events')} wasted: {s.get('stacking_wasted')} unjudged: "
        f"{s.get('stacking_unjudged')}  zombie builds: {s.get('zombie_build_actions')}",
        f"survivor: shots {s.get('survivor_shots')} hits {s.get('survivor_hits')} kills {s.get('survivor_kills')} "
        f"intended misses {s.get('survivor_intended_misses')} ammo left {s.get('survivor_ammo_left')} blocked {s.get('shots_blocked')}",
        f"builds: sent {s.get('builds_sent')} accepted {s.get('builds_accepted')} rejected {s.get('builds_rejected')} "
        f"cells committed {s.get('build_cells_committed')} failed {s.get('build_cells_failed')}  repairs {s.get('repairs')}  "
        f"blocks spent {s.get('blocks_spent')}  teleports after spawn {s.get('teleports_after_spawn')}",
        f"input frames fed/applied/dropped: {s.get('input_frames')}/{s.get('input_frames_applied')}/{s.get('input_frames_dropped')}  "
        f"tactic states: {s.get('tactic_timeline')}",
        f"tick p50/p99/max ms: {s.get('tick_p50_ms')}/{s.get('tick_p99_ms')}/{s.get('tick_max_ms')}",
    ]
    verdict = s.get("verdict")
    if verdict:
        lines.append("verdict: " + "  ".join(f"{k}={'PASS' if v else 'FAIL'}" for k, v in verdict.items()))
    return lines


# --------------------------------------------------------------------------- #
# the live-server plugin (``--real-server``)
# --------------------------------------------------------------------------- #

def _env(name: str, default, cast=str):
    raw = os.environ.get(f"ZOMBIE_FUN_GATE_{name}")
    if raw is None or raw == "":
        return default
    try:
        return cast(raw)
    except (TypeError, ValueError):
        return default


class _GateContext:
    """What a tactic needs from the gate: server, world, survivor spawn."""

    def __init__(self, server, survivor_spawn) -> None:
        self.server = server
        self.world = server.world_manager
        self.survivor_spawn = survivor_spawn


try:
    from plugins.base_plugin import BasePlugin as _BasePlugin
except Exception:  # noqa: BLE001 - the helper is importable without the plugin package
    _BasePlugin = object


class ZombieFunGatePlugin(_BasePlugin):
    """Runs one gate tactic on a real server once the Zombie round is active.

    Inert unless named in ``[plugins] allowlist`` (``zombie_fun_survivor``);
    ``zombie_fun_gate.py --real-server`` enables it through a temporary config.
    Options come from ``ZOMBIE_FUN_GATE_*`` environment variables (TACTIC,
    SEED, SECONDS, HIT_CHANCE, WEAPON, RESUPPLY, OUT).
    """

    name = "ZombieFunGate"
    version = "1.0.0"
    author = "BattleSpades"
    description = "TEST ONLY: scripted human survivor against the horde on a live server."

    def __init__(self, server) -> None:
        if _BasePlugin is not object:
            super().__init__(server)
        else:
            self.server = server
            self.enabled = True
        self.state = "inert"
        self.tactic_name = _env("TACTIC", "pillar")
        self.seed = _env("SEED", 7, int)
        self.seconds = _env("SECONDS", 240.0, float)
        self.hit_chance = _env("HIT_CHANCE", 0.6, float)
        self.weapon = WEAPON_BY_NAME.get(_env("WEAPON", "rifle"), RIFLE_TOOL)
        self.resupply = bool(_env("RESUPPLY", 0, int))
        self.out = Path(_env("OUT", "logs/zombie_fun_gate_live.json"))
        self._t0 = 0.0
        self._next_sample = 0.0

    async def on_load(self):
        allow = {str(v).strip().casefold() for v in getattr(self.server.config, "plugin_allowlist", ()) or ()}
        if not ({"zombie_fun_survivor", self.name.casefold()} & allow):
            self.enabled = False
            logger.info("ZombieFunGate present but not allowlisted: inert")
            return
        self.state = "waiting"
        logger.warning("ZombieFunGate ENABLED (test-only): tactic=%s seed=%s seconds=%.0f",
                       self.tactic_name, self.seed, self.seconds)

    async def on_tick(self, tick):
        if self.state in ("inert", "done"):
            return
        from modes.zombie import ZombieMode, ZombiePhase

        mode = getattr(self.server, "mode", None)
        if not isinstance(mode, ZombieMode) or getattr(self.server.world_manager, "map", None) is None:
            return
        if self.state == "waiting":
            if mode.phase is ZombiePhase.ACTIVE:
                await self._begin(mode)
            return
        if self.state == "active":
            self._tick_active(mode)

    async def _begin(self, mode) -> None:
        server = self.server
        rng = random.Random(self.seed)
        random.seed(self.seed)
        spawn = tuple(float(v) for v in server.world_manager.get_spawn_point(SURVIVOR_TEAM))
        ctx = _GateContext(server, spawn)
        self.tactic = TACTIC_CLASSES[self.tactic_name](ctx, rng, {})
        position = self.tactic.start_position()
        player = spawn_survivor(server, position, name="GateHuman")
        self.survivor = Survivor(server, player, random.Random(self.seed * 7919 + 1),
                                 hit_chance=self.hit_chance, weapon=self.weapon, resupply=self.resupply)
        self.tactic.after_spawn(self.survivor)
        infected = 0
        for other in list(server.players.values()):
            if getattr(other, "is_bot", False) and int(other.team) == SURVIVOR_TEAM and other is not player:
                await mode._infect(other, patient_zero=False)
                infected += 1
        self.zombies = [p for p in server.players.values() if getattr(p, "is_bot", False) and int(p.team) == ZOMBIE_TEAM]
        self.zombie_starts = [tuple(round(float(v), 1) for v in z.position) for z in self.zombies]
        self.metrics = FunMetrics(server, server.bots, self.zombies, [player],
                                  horde_floor=self.tactic.horde_floor, elevated=self.tactic.elevated)
        metrics = getattr(server, "metrics", None)
        if metrics is not None and hasattr(metrics, "reset_timing_samples"):
            metrics.reset_timing_samples()   # tick timings of the observation only
        self._t0 = time.monotonic()
        self._next_sample = self._t0
        self.start_position = position
        self.state = "active"
        logger.warning("gate BEGIN tactic=%s survivor=%d at (%.1f,%.1f,%.1f) bots infected=%d zombies=%d",
                       self.tactic_name, player.id, *position, infected, len(self.zombies))

    def _tick_active(self, mode) -> None:
        now = time.monotonic()
        t = now - self._t0
        s = self.survivor
        s.settle_builds()
        s.observe(t, self.zombies)
        if s.alive:
            self.tactic.update(t, self.zombies)
            s.record_frame(t)
        self.metrics.tick(t)
        if now >= self._next_sample:
            self._next_sample = now + SAMPLE_SECONDS
            self.metrics.sample(t, [s.player] if s.alive else [])
        from modes.zombie import ZombiePhase

        if t >= self.seconds or not s.alive or mode.phase is not ZombiePhase.ACTIVE:
            self._finish(t)

    def _finish(self, t: float) -> None:
        start = self.start_position
        result = {
            "map": str(getattr(self.server.world_manager, "map_name", "")),
            "tactic": self.tactic_name, "seed": self.seed, "zombies": len(self.zombies),
            "duration_s": round(t, 1), "wall_s": round(t, 1), "real_server": True,
            "hit_chance": self.hit_chance,
            "survivor_start": tuple(round(v, 1) for v in start),
            "zombie_starts": self.zombie_starts[:4],
            # live bots are infected where they stood as survivors, not at the
            # map's zombie spawn: the approach is short by construction
            "zombie_start_distance_mean": round(sum(
                math.hypot(z[0] - start[0], z[1] - start[1]) for z in self.zombie_starts
            ) / max(1, len(self.zombie_starts)), 1),
            "site": self.tactic.site,
        }
        result.update(self.metrics.report(len(self.zombies)))
        metrics = getattr(self.server, "metrics", None)
        if metrics is not None and hasattr(metrics, "snapshot"):
            try:
                snap = metrics.snapshot()
                for key in ("tick_p50_ms", "tick_p99_ms", "tick_max_ms"):
                    result[key] = round(float(snap.get(key, 0.0)), 2)
            except Exception:  # noqa: BLE001 - diagnostics only
                pass
        result.update(self.survivor.report())
        result.update(self.tactic.report())
        result["verdict"] = judge(result, DEFAULT_THRESHOLDS)
        try:
            self.out.parent.mkdir(parents=True, exist_ok=True)
            self.out.write_text(json.dumps(result, indent=1, default=str), encoding="utf-8")
            logger.warning("gate DONE after %.1f s: %s", t, self.out)
        except OSError:
            logger.exception("ZombieFunGate could not write %s", self.out)
        self.state = "done"
