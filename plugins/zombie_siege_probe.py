"""Zombie siege probe: a live, real-server measurement of the roof-pillar case.

TEST-ONLY. This plugin is inert unless it is named in ``[plugins] allowlist``
(``"zombie_siege_probe"`` or ``"ZombieSiegeProbe"``); an empty allowlist
loads every plugin, so the name check is done here as well and the plugin
disables itself when it is not explicitly enabled. Never ship it enabled.

Once the Zombie round is ACTIVE on the configured map (London by default) it
builds a one-block pillar on a real building roof, stands an idle,
server-owned survivor on top (the player who "piled blocks from a building to
vertical height"), optionally arms him (one validated rifle shot every
0.6-0.9 s, a pillar repair every 4 s), turns every remaining bot survivor
into a zombie through the mode's own infection, and then logs one line per
zombie bot every 2 s:

    probe t=  12.0 id= 3 pos=(321.5,276.5,222.8) g=1 d=  9.1 order=surround/0 role=zombie_siege_surround act=none

plus the pile count under the survivor and his health. After the configured
number of seconds (or once the survivor is infected) it writes a JSON summary
to ``logs/zombie_siege_probe.json`` with the same activity classification the
offline harness uses (closing / digging / fighting / building / climbing /
wandering / standing, ``useful_share``, ``pile_*``, ``first_at_height_s``,
``sides_used``, per-bot ``longest_still_s``, deaths and respawns).

Options (environment variables, read on load):

    ZOMBIE_SIEGE_PROBE_SECONDS   observation window after the pillar stands (150)
    ZOMBIE_SIEGE_PROBE_HEIGHT    pillar height in blocks (8)
    ZOMBIE_SIEGE_PROBE_ARMED     1 = the survivor shoots back and repairs (0)
    ZOMBIE_SIEGE_PROBE_INFECT    1 = infect every bot survivor at the start (1)
    ZOMBIE_SIEGE_PROBE_MAP       map the probe waits for; "" = any (London)
    ZOMBIE_SIEGE_PROBE_SEED      site choice seed (7)
    ZOMBIE_SIEGE_PROBE_OUT       summary path (logs/zombie_siege_probe.json)
    ZOMBIE_SIEGE_PROBE_LOG_EVERY seconds between log lines (2.0)

``scripts/zombie_live_siege_probe.py`` launches a real server with this plugin
enabled and prints the summary.
"""

from __future__ import annotations

from collections import Counter, deque
import json
import logging
import math
import os
from pathlib import Path
import random
import time

import shared.constants as C
from modes.zombie import SURVIVOR_TEAM, ZOMBIE_TEAM, ZombieMode, ZombiePhase
from plugins.base_plugin import BasePlugin
from server.bot_ai.structure_collapse import support_cells
from server.class_selection import normalize_class_selection
from server.game_constants import DEFAULT_WEAPON_TOOL
from server.player import Player
from shared.packet import ShootPacket

logger = logging.getLogger(__name__)

PLUGIN_STEM = "zombie_siege_probe"
STAND = 2.25
SAMPLE_SECONDS = 0.5
WINDOW_SECONDS = 2.0
BUILD_ROLE_PREFIXES = ("prefab_ascent", "climb", "pillar", "staircase")
BUILD_ACTIONS = frozenset({"place_prefab", "build", "build_line"})
RISE_BLOCKS = 0.8
STILL_BLOCKS = 0.4
USEFUL_KINDS = ("closing", "digging", "fighting", "building", "climbing")
PILLAR_COLOR = 0x777777
SITE_BUDGET_SECONDS = 0.0008  # per tick, inside the 2 ms plugin budget


def _env(name: str, default, cast=str):
    raw = os.environ.get(f"ZOMBIE_SIEGE_PROBE_{name}")
    if raw is None or raw == "":
        return default
    try:
        return cast(raw)
    except (TypeError, ValueError):
        return default


class _IdleConnection:
    """A peerless, settled connection: the survivor is server-owned."""

    def __init__(self, server) -> None:
        self.server = server
        self.in_game = True
        self.player = None
        self.peer = None
        self.map_sent = True
        self.known_entity_ids: set[int] = set()

    def send(self, data, reliable: bool = True, prefix: int = 0x30) -> None:
        return None

    def send_packet(self, packet, reliable: bool = True) -> None:
        return None

    def disconnect(self, reason: int = 0) -> None:
        self.in_game = False


# --- site finding (copied from scripts/bot_zombie_horde_scenario.py) ----------

def _typical_ground(world) -> int:
    heights = sorted(world.get_height(x, y) for y in range(8, 504, 8) for x in range(8, 504, 8)
                     if not world.is_water_column(x, y))
    return heights[len(heights) // 2] if heights else 0


def _rooftop_candidates(world, span: int = 2):
    """Generator over map rows; yields after each row so a tick can stop."""

    ground = _typical_ground(world)
    candidates: list = []
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
        yield None
    yield candidates


def _pick_site(candidates, rng: random.Random):
    if not candidates:
        raise RuntimeError("no building roof on this map")
    candidates.sort()
    _, _, x, y, top = rng.choice(candidates[: max(1, len(candidates) // 8)])
    return x, y, top


# --- the armed survivor (compact copy of the harness's _ArmedSurvivor) ---------

class _ArmedSurvivor:
    RANGE = 40.0
    REACH = 6.0
    REPAIR_CAP = 20
    TORSO_Z = 0.3

    def __init__(self, server, survivor, built, support, rng: random.Random, hit_chance: float = 1.0) -> None:
        self.server = server
        self.survivor = survivor
        self.hit_chance = max(0.0, min(1.0, float(hit_chance)))
        self.built = [tuple(int(v) for v in cell) for cell in built]
        self.support = tuple(tuple(int(v) for v in cell) for cell in support)
        self.rng = rng
        self.next_shot_at = 1.0
        self.next_repair_at = 4.0
        self.shots = self.hits = self.kills = self.repairs = 0
        self.repaired: set = set()
        self.blocked: Counter = Counter()
        self._pending = None

    def tick(self, t: float, zombies) -> None:
        s = self.survivor
        if not (s.alive and s.spawned and int(s.team) == SURVIVOR_TEAM):
            self._pending = None
            return
        if self._pending is not None:
            direction, self._pending = self._pending, None
            self._fire(direction, zombies)
        elif t >= self.next_shot_at:
            self.next_shot_at = t + self.rng.uniform(0.6, 0.9)
            self._aim(zombies)
        if t >= self.next_repair_at:
            self.next_repair_at = t + 4.0
            self._repair()

    def _aim(self, zombies) -> None:
        s = self.survivor
        if int(getattr(s, "ammo_clip", 0)) <= 0:
            if not bool(getattr(s, "reloading", False)):
                self.server.combat.handle_weapon_reload(s)
            self.blocked["reloading"] += 1
            return
        world = self.server.world_manager
        eye = tuple(float(v) for v in s.eye)
        for z in sorted((b for b in zombies if b.alive and b.spawned),
                        key=lambda b: math.dist(b.position, s.position)):
            to = (float(z.x) - eye[0], float(z.y) - eye[1], float(z.z) + self.TORSO_Z - eye[2])
            dist = math.sqrt(sum(c * c for c in to))
            if dist > self.RANGE:
                break
            if dist < 1e-6:
                continue
            direction = tuple(c / dist for c in to)
            if world.raycast(eye[0], eye[1], eye[2], *direction, max(0.0, dist - 0.6)) is not None:
                continue
            aim = self._slip(eye, (float(z.x), float(z.y), float(z.z) + self.TORSO_Z), dist)
            to = (aim[0] - eye[0], aim[1] - eye[1], aim[2] - eye[2])
            dist = math.sqrt(sum(c * c for c in to)) or 1.0
            direction = tuple(c / dist for c in to)
            s.set_orientation_vector(*direction)
            self._pending = direction
            return
        self.blocked["no_target_in_sight"] += 1

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

    def _fire(self, direction, zombies) -> None:
        s = self.survivor
        eye = tuple(float(v) for v in s.eye)
        target, best = None, math.inf
        for z in zombies:
            if not (z.alive and z.spawned):
                continue
            to = (float(z.x) - eye[0], float(z.y) - eye[1], float(z.z) + self.TORSO_Z - eye[2])
            dist = math.sqrt(sum(c * c for c in to))
            if dist < 1e-6 or dist > self.RANGE:
                continue
            if (sum(to[i] / dist * direction[i] for i in range(3))
                    > (0.98 if self.hit_chance >= 1.0 else 0.9) and dist < best):
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
        packet.damage = packet.penetration = packet.affect_shooter = packet.secondary = 0
        packet.seed = self.rng.randrange(256)
        before, alive_before = int(target.health), bool(target.alive)
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
            return
        column = (int(math.floor(s.x)), int(math.floor(s.y)))
        top = min((cell[2] for cell in self.support), default=-1)
        bodies = [(float(p.x), float(p.y), float(p.z))
                  for p in self.server.players.values() if p.alive and p.spawned]
        candidates = []
        for cell in self.built:
            if cell in self.repaired or world.get_solid(*cell):
                continue
            if abs(cell[0] - column[0]) > 1 or abs(cell[1] - column[1]) > 1 or cell[2] < top:
                continue
            centre = (cell[0] + 0.5, cell[1] + 0.5, cell[2] + 0.5)
            distance = math.dist((float(s.x), float(s.y), float(s.z)), centre)
            if distance > self.REACH:
                continue
            if any(abs(b[0] - centre[0]) < 0.9 and abs(b[1] - centre[1]) < 0.9
                   and b[2] - 0.5 <= centre[2] <= b[2] + 3.0 for b in bodies):
                continue
            candidates.append((distance, cell))
        if candidates:
            _, cell = min(candidates)
            if world.set_block(cell[0], cell[1], cell[2], True, PILLAR_COLOR):
                self.repaired.add(cell)
                self.repairs += 1

    def report(self) -> dict:
        s = self.survivor
        return {"survivor_shots": self.shots, "survivor_hits": self.hits,
                "survivor_kills": self.kills, "repairs": self.repairs,
                "survivor_ammo_left": (int(getattr(s, "ammo_clip", 0))
                                       + int(getattr(s, "ammo_reserve", 0))),
                "shots_blocked": dict(self.blocked)}


# --- the plugin -------------------------------------------------------------------

class ZombieSiegeProbePlugin(BasePlugin):
    name = "ZombieSiegeProbe"
    version = "1.0.0"
    author = "BattleSpades"
    description = "TEST ONLY: measures the horde against a roof-pillar survivor on a live server."

    def __init__(self, server):
        super().__init__(server)
        self.seconds = _env("SECONDS", 150.0, float)
        self.height = max(1, _env("HEIGHT", 8, int))
        self.armed = bool(_env("ARMED", 0, int))
        self.hit_chance = max(0.0, min(1.0, _env("HIT_CHANCE", 1.0, float)))
        self.infect_bots = bool(_env("INFECT", 1, int))
        self.map_filter = _env("MAP", "London")
        self.seed = _env("SEED", 7, int)
        self.out = Path(_env("OUT", "logs/zombie_siege_probe.json"))
        self.log_every = max(0.5, _env("LOG_EVERY", 2.0, float))
        # Optional observation perch "x,y,z": a 3x3 platform whose top is at
        # z, on a column down to the ground, for a god-mode viewer to /tp to.
        self.perch = _env("PERCH", "")
        self._perch_cells: list = []
        self.state = "inert"
        self._search = None
        self._site = None
        self._survivor = None
        self._armed = None
        self._built: list = []
        self._support: tuple = ()
        self._t0 = 0.0
        self._next_sample = 0.0
        self._next_log = 0.0
        self._history: dict[int, deque] = {}
        self._still_since: dict[int, float | None] = {}
        self._last_seen: dict[int, tuple | None] = {}
        self._longest_still: dict[int, float] = {}
        self._activity: Counter = Counter()
        self._by_bot: dict[int, Counter] = {}
        self._roles: Counter = Counter()
        self._orders: Counter = Counter()
        self._pile: list[int] = []
        self._reach: dict[int, float] = {}
        self._min_distance: dict[int, float] = {}
        self._sides: set[int] = set()
        self._first_at_height = None
        self._first_hit = None
        self._collapse = None
        self._infected_at = None
        self._deaths = 0
        self._respawns = 0
        self._kills_by_players = 0
        self._alive_seen: dict[int, bool] = {}
        self._ring_idle = 0
        self._classified = 0
        self._samples = 0
        self._start_health = 0

    # -- lifecycle -------------------------------------------------------------

    async def on_load(self):
        allow = {str(v).strip().casefold()
                 for v in getattr(self.server.config, "plugin_allowlist", ()) or ()}
        if not ({PLUGIN_STEM, self.name.casefold()} & allow):
            self.enabled = False
            self.state = "inert"
            logger.info("ZombieSiegeProbe present but NOT named in [plugins] allowlist: inert")
            return
        self.state = "waiting"
        logger.warning("ZombieSiegeProbe ENABLED (test-only): seconds=%.0f height=%d armed=%s "
                       "infect_bots=%s map=%r out=%s", self.seconds, self.height, self.armed,
                       self.infect_bots, self.map_filter, self.out)

    async def on_player_kill(self, killer, victim, kill_type):
        # Kills credited to a player; deaths and respawns themselves are
        # counted from alive transitions in ``_sample`` (falls and suicides
        # reach no kill hook, and a zombie respawns 10 s after it died).
        if (self.state == "active" and getattr(victim, "is_bot", False)
                and int(getattr(victim, "team", -1)) == ZOMBIE_TEAM
                and killer is not None and killer is not victim):
            self._kills_by_players += 1

    async def on_tick(self, tick):
        if self.state in ("inert", "done"):
            return
        mode = getattr(self.server, "mode", None)
        if not isinstance(mode, ZombieMode):
            return
        world = self.server.world_manager
        if getattr(world, "map", None) is None:
            return
        if self.map_filter and str(getattr(world, "map_name", "")).casefold() != self.map_filter.casefold():
            return
        if self.state == "waiting":
            self._advance_site_search(world)
            if self._site is not None and mode.phase is ZombiePhase.ACTIVE:
                await self._begin(mode, world)
            return
        if self.state == "active":
            self._tick_active(mode, world)

    # -- site search, spread over ticks -------------------------------------------

    def _advance_site_search(self, world) -> None:
        if self._site is not None:
            return
        if self._search is None:
            self._search = _rooftop_candidates(world)
        deadline = time.perf_counter() + SITE_BUDGET_SECONDS
        while time.perf_counter() < deadline:
            try:
                step = next(self._search)
            except StopIteration:
                return
            except Exception:
                logger.exception("ZombieSiegeProbe site search failed; probe off")
                self.state = "done"
                return
            if step is not None:
                try:
                    self._site = _pick_site(step, random.Random(self.seed))
                    self._build_perch(world)
                except RuntimeError:
                    logger.error("ZombieSiegeProbe: no building roof on %s; probe off",
                                 getattr(world, "map_name", "?"))
                    self.state = "done"
                return

    # -- start of the observation ------------------------------------------------

    def _build_perch(self, world) -> None:
        if not self.perch:
            return
        try:
            px, py, pz = (int(float(v)) for v in self.perch.split(","))
        except ValueError:
            logger.error("ZombieSiegeProbe: bad PERCH %r", self.perch)
            return
        ground = world.get_height(px, py)
        for z in range(pz, ground):
            if world.set_block(px, py, z, True, 0x444444):
                self._perch_cells.append((px, py, z))
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                if world.set_block(px + dx, py + dy, pz, True, 0x444444):
                    self._perch_cells.append((px + dx, py + dy, pz))
        logger.warning("probe PERCH built at (%d,%d) top z=%d (%d cells); /tp %.1f %.1f %.2f",
                       px, py, pz, len(self._perch_cells), px + 0.5, py + 0.5, pz - STAND)

    async def _begin(self, mode, world) -> None:
        x, y, floor = self._site
        self._built = []
        for z in range(floor - self.height, floor):
            if world.set_block(x, y, z, True, PILLAR_COLOR):
                self._built.append((x, y, z))
        top = floor - self.height
        position = (x + 0.5, y + 0.5, top - STAND)
        survivor = self._spawn_idle_survivor(position)
        mode._assign_survivor(survivor)
        self._survivor = survivor
        self._support = support_cells(world.get_solid, survivor.position)
        self._start_health = int(survivor.health)
        infected = 0
        if self.infect_bots:
            for player in list(self.server.players.values()):
                if (getattr(player, "is_bot", False) and int(player.team) == SURVIVOR_TEAM
                        and player is not survivor):
                    await mode._infect(player, patient_zero=False)
                    infected += 1
        if self.armed:
            self._armed = _ArmedSurvivor(self.server, survivor, self._built, self._support,
                                         random.Random(self.seed * 7919 + 1),
                                         hit_chance=self.hit_chance)
        now = time.monotonic()
        self._t0 = now
        self._next_sample = now
        self._next_log = now
        self.state = "active"
        logger.warning("probe BEGIN site=(%d,%d,%d) pillar=%d survivor=%d pos=(%.1f,%.1f,%.1f) "
                       "armed=%s bots_infected=%d zombies=%d", x, y, floor, self.height,
                       survivor.id, *position, self.armed, infected, len(self._zombies()))

    def _spawn_idle_survivor(self, position) -> Player:
        server = self.server
        connection = _IdleConnection(server)
        player = Player(server.get_next_player_id(), "PillarMan", SURVIVOR_TEAM,
                        DEFAULT_WEAPON_TOOL, connection)
        connection.player = player
        player.apply_class_selection(normalize_class_selection(int(C.CLASS_SOLDIER)))
        server.players[player.id] = player
        server.teams[SURVIVOR_TEAM].add_player(player)
        server.connections[object()] = connection
        player.spawn(*position)
        server._broadcast_create_player(player, position)
        return player

    # -- per tick --------------------------------------------------------------------

    def _zombies(self) -> list:
        return [p for p in self.server.players.values()
                if getattr(p, "is_bot", False) and int(p.team) == ZOMBIE_TEAM]

    def _tick_active(self, mode, world) -> None:
        s = self._survivor
        now = time.monotonic()
        t = now - self._t0
        zombies = self._zombies()
        if self._armed is not None:
            # Aim before this tick's input frame is recorded: plugins run after
            # player simulation, so the frame applied next tick must already
            # carry the new aim or the server reverts to the stale one and the
            # shot fails its direction gate.
            self._armed.tick(t, zombies)
        if s.alive and s.spawned and int(s.team) == SURVIVOR_TEAM:
            # A real idle client still sends one input frame per physics step.
            s.record_input_frame(self.server.loop_count, (False,) * 8,
                                 (s.o_x, s.o_y, s.o_z),
                                 received_server_tick=self.server.loop_count)
        if (self._collapse is None and self._support
                and not any(world.get_solid(*cell) for cell in self._support)):
            self._collapse = round(t, 1)
            logger.warning("probe t=%6.1f COLLAPSE: the pillar's support is gone", t)
        if self._first_hit is None and (int(s.health) < self._start_health or not s.alive):
            self._first_hit = round(t, 1)
        if self._infected_at is None and int(s.team) != SURVIVOR_TEAM:
            self._infected_at = round(t, 1)
            logger.warning("probe t=%6.1f SURVIVOR INFECTED", t)
        if now >= self._next_sample:
            self._next_sample = now + SAMPLE_SECONDS
            self._sample(t, zombies)
        if now >= self._next_log:
            self._next_log = now + self.log_every
            self._log(t, zombies)
        finished = (t >= self.seconds or self._infected_at is not None
                    or mode.phase is not ZombiePhase.ACTIVE)
        if finished:
            self._finish(t, zombies)

    def _sample(self, t: float, zombies) -> None:
        s = self._survivor
        director = getattr(self.server, "bots", None)
        orders = getattr(getattr(director, "_zombie_siege", None), "last_orders", {}) or {}
        runtimes = getattr(director, "_runtime", {}) or {}
        alive = int(s.team) == SURVIVOR_TEAM and s.alive and s.spawned
        self._samples += 1
        if alive:
            pile = sum(1 for z in zombies if z.alive and z.spawned
                       and math.hypot(z.x - s.x, z.y - s.y) <= 2.5 and z.z - s.z >= 3.0)
            self._pile.append(pile)
        floor = self._site[2]
        for bot in zombies:
            was_alive = self._alive_seen.get(bot.id)
            is_alive = bool(bot.alive and bot.spawned)
            if was_alive is not None and was_alive != is_alive:
                if is_alive:
                    self._respawns += 1
                else:
                    self._deaths += 1
            self._alive_seen[bot.id] = is_alive
            runtime = runtimes.get(bot.id)
            intent = getattr(runtime, "intent", None)
            buf = self._history.setdefault(bot.id, deque())
            if not (bot.alive and bot.spawned) or not alive:
                buf.clear()
                self._still_since[bot.id] = None
                self._last_seen[bot.id] = None
                continue
            order = orders.get(bot.id)
            if order is not None:
                self._orders[order.role] += 1
            role = str(intent.debug_role).split(":")[0] if intent is not None else "none"
            self._roles[role] += 1
            pos = tuple(bot.position)
            seen = self._last_seen.get(bot.id)
            if seen is not None and math.dist(seen[1], pos) < STILL_BLOCKS:
                if self._still_since.get(bot.id) is None:
                    self._still_since[bot.id] = seen[0]
                self._longest_still[bot.id] = max(
                    self._longest_still.get(bot.id, 0.0), t - self._still_since[bot.id])
            else:
                self._still_since[bot.id] = None
            self._last_seen[bot.id] = (t, pos)
            distance = math.dist(pos, s.position)
            across = math.hypot(pos[0] - s.x, pos[1] - s.y)
            self._min_distance[bot.id] = min(self._min_distance.get(bot.id, math.inf), distance)
            if distance <= 3.0:
                self._reach.setdefault(bot.id, round(t, 1))
            if self._first_at_height is None and across <= 4.0 and abs(pos[2] - s.z) <= 3.0:
                self._first_at_height = round(t, 1)
            if across <= 6.0 and pos[2] <= floor - STAND + 1.0:
                bearing = math.atan2(pos[1] - s.y, pos[0] - s.x)
                self._sides.add(int(((bearing + 2 * math.pi) % (2 * math.pi)) // (math.pi / 4.0)) % 8)
            buf.append((t, pos, distance))
            while buf and t - buf[0][0] > WINDOW_SECONDS + 1e-6:
                buf.popleft()
            if t - buf[0][0] < WINDOW_SECONDS - SAMPLE_SECONDS:
                continue
            clawing = (runtime is not None and getattr(runtime, "feedback_action_kind", "") == "melee"
                       and time.monotonic() - float(getattr(runtime, "feedback_action_at", 0.0)) < 2.0)
            moved = math.hypot(buf[0][1][0] - pos[0], buf[0][1][1] - pos[1])
            closed = buf[0][2] - distance
            rise = buf[0][1][2] - pos[2]
            closed_xy = math.hypot(buf[0][1][0] - s.x, buf[0][1][1] - s.y) - across
            building = intent is not None and (
                str(intent.action.kind.value) in BUILD_ACTIONS
                or str(intent.debug_role).startswith(BUILD_ROLE_PREFIXES))
            if distance <= 4.0:
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
            self._activity[kind] += 1
            self._by_bot.setdefault(bot.id, Counter())[kind] += 1
            self._classified += 1
            if order is not None and order.role == "surround" and kind == "standing":
                self._ring_idle += 1

    def _log(self, t: float, zombies) -> None:
        s = self._survivor
        director = getattr(self.server, "bots", None)
        orders = getattr(getattr(director, "_zombie_siege", None), "last_orders", {}) or {}
        runtimes = getattr(director, "_runtime", {}) or {}
        pile = self._pile[-1] if self._pile else 0
        logger.info("probe t=%6.1f survivor hp=%3d pos=(%.1f,%.1f,%.1f) team=%d pile=%d zombies=%d",
                    t, int(s.health), s.x, s.y, s.z, int(s.team), pile, len(zombies))
        for bot in sorted(zombies, key=lambda b: int(b.id)):
            runtime = runtimes.get(bot.id)
            intent = getattr(runtime, "intent", None)
            order = orders.get(bot.id)
            logger.info("probe t=%6.1f id=%2d pos=(%6.1f,%6.1f,%6.1f) g=%d alive=%d d=%5.1f "
                        "order=%s role=%s act=%s", t, bot.id, bot.x, bot.y, bot.z,
                        int(bool(bot.grounded)), int(bool(bot.alive)),
                        math.dist(bot.position, s.position),
                        f"{order.role}/{order.stuck_level}" if order is not None else "-",
                        intent.debug_role if intent is not None else "-",
                        intent.action.kind.value if intent is not None else "-")

    def _finish(self, t: float, zombies) -> None:
        total = sum(self._activity.values()) or 1
        shares = {k: round(v / total, 3) for k, v in sorted(self._activity.items())}
        worst = {}
        for bot_id, counter in self._by_bot.items():
            n = sum(counter.values())
            if n:
                worst[bot_id] = round((counter["standing"] + counter["wandering"]) / n, 3)
        summary = {
            "map": str(getattr(self.server.world_manager, "map_name", "")),
            "site": list(self._site), "height": self.height, "armed": self.armed,
            "bots_infected_at_start": self.infect_bots,
            "zombies": len(zombies), "duration_s": round(t, 1), "window_s": self.seconds,
            "survivor_infected_s": self._infected_at,
            "first_hit_s": self._first_hit, "collapse_s": self._collapse,
            "first_contact_s": min(self._reach.values(), default=None),
            "zombies_reached": len(self._reach),
            "reach_s": {str(k): v for k, v in sorted(self._reach.items())},
            "min_distance": {str(k): round(v, 1) for k, v in sorted(self._min_distance.items())},
            "pile_mean": round(sum(self._pile) / len(self._pile), 2) if self._pile else 0.0,
            "pile_max": max(self._pile, default=0),
            "pile_seconds": round(sum(self._pile) * SAMPLE_SECONDS, 1),
            "activity_share": shares,
            "useful_share": round(sum(shares.get(k, 0.0) for k in USEFUL_KINDS), 3),
            "idle_share_worst_bot": max(worst.values(), default=0.0),
            "ring_idle_share": round(self._ring_idle / max(1, self._classified), 3),
            "first_at_height_s": self._first_at_height,
            "sides_used": len(self._sides),
            "longest_still_s": {str(k): round(v, 1) for k, v in sorted(self._longest_still.items())},
            "longest_still_max_s": round(max(self._longest_still.values(), default=0.0), 1),
            "roles": dict(self._roles.most_common(14)),
            "orders": dict(self._orders.most_common()),
            "zombie_deaths": self._deaths, "zombie_respawns": self._respawns,
            "zombie_kills_by_players": self._kills_by_players,
            "structure_cells": len(self._built),
            "structure_cells_gone": sum(1 for c in self._built
                                        if not self.server.world_manager.get_solid(*c)),
            "samples": self._samples,
        }
        if self._armed is not None:
            summary.update(self._armed.report())
        try:
            self.out.parent.mkdir(parents=True, exist_ok=True)
            self.out.write_text(json.dumps(summary, indent=1, default=str), encoding="utf-8")
            logger.warning("probe DONE after %.1f s: summary written to %s", t, self.out)
        except OSError:
            logger.exception("ZombieSiegeProbe could not write %s", self.out)
        self.state = "done"
