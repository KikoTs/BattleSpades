"""Accelerated bot decision/navigation soak on the real CityOfChicago VXL.

This is an offline policy diagnostic, not a substitute for retail-client or
authoritative 60 Hz physics validation.  It advances immutable perception
frames with synthetic monotonic timestamps, applies intentions through a
small collision-aware kinematic adapter, and reports priority/action loops.

Examples::

    py -3.12 scripts/bot_city_soak.py --mode tdm --sim-seconds 120
    py -3.12 scripts/bot_city_soak.py --mode zom --sim-seconds 120
"""

from __future__ import annotations

import argparse
from collections import Counter, deque
from dataclasses import dataclass, replace
import json
import math
from pathlib import Path
import random
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aoslib.world import cube_line
import shared.constants as C
from server.bot_ai.messages import (
    BotActionKind,
    EntitySnapshot,
    MapSnapshot,
    ObjectiveSnapshot,
    PerceptionFrame,
    PlayerSnapshot,
    VoxelChange,
    WorldDelta,
)
from server.bot_ai.profiles import ProfileFactory
from server.bot_ai.soak_monitor import BotSoakMonitor
from server.bot_ai.worker import BotBrain, WorkerVoxelWorld
from server.class_selection import normalize_class_selection
from server.config import ServerConfig
from server.game_constants import TEAM1, TEAM2, WEAPON_PROFILES, get_weapon_profile
from server.prefabs import allowed_prefabs_for_class
from server.projectiles import PROJECTILE_SPECS
from server.world_manager import WorldManager


DECISION_DT = 0.2
MOVE_SPEED = 4.2
SPRINT_SPEED = 6.0


@dataclass(slots=True)
class _Actor:
    """Mutable harness state converted to a real immutable worker snapshot."""

    player_id: int
    team: int
    class_id: int
    position: tuple[float, float, float]
    orientation: tuple[float, float, float]
    loadout: tuple[int, ...]
    prefabs: tuple[str, ...]
    weapon_tool: int
    tool: int
    ammo_clip: int
    ammo_reserve: int
    blocks: int = 50
    health: int = 100
    alive: bool = True
    respawn_at: float = 0.0
    velocity: tuple[float, float, float] = (0.0, 0.0, 0.0)
    wade: bool = False
    grounded: bool = True
    airborne_until: float = 0.0
    last_action_kind: str = ""
    last_action_accepted: bool = True
    last_action_position: tuple[float, float, float] | None = None
    last_action_frame: int = -1
    last_action_at: float = 0.0
    last_damage_at: float = 0.0
    last_damage_source_id: int = -1
    last_damage_source_position: tuple[float, float, float] | None = None
    life_id: int = 0

    def snapshot(self) -> PlayerSnapshot:
        """Publish exactly the fields available to the production worker."""

        stock = tuple(
            (int(tool), 3)
            for tool in self.loadout
            if int(tool) in PROJECTILE_SPECS
        )
        return PlayerSnapshot(
            player_id=self.player_id,
            generation=1,
            team=self.team,
            class_id=self.class_id,
            alive=self.alive,
            spawned=self.alive,
            position=self.position,
            eye=self.position,
            orientation=self.orientation,
            velocity=self.velocity,
            health=self.health,
            tool=self.tool,
            blocks=self.blocks,
            ammo_clip=self.ammo_clip,
            ammo_reserve=self.ammo_reserve,
            is_bot=True,
            weapon_tool=self.weapon_tool,
            loadout=self.loadout,
            prefabs=self.prefabs,
            oriented_stock=stock,
            grounded=bool(self.grounded and not self.wade),
            wade=self.wade,
            last_action_kind=self.last_action_kind,
            last_action_accepted=self.last_action_accepted,
            last_action_position=self.last_action_position,
            last_action_frame=self.last_action_frame,
            last_action_at=self.last_action_at,
            last_damage_at=self.last_damage_at,
            last_damage_source_id=self.last_damage_source_id,
            last_damage_source_position=self.last_damage_source_position,
            life_id=self.life_id,
        )


@dataclass(slots=True)
class _Hazard:
    """Short-lived projectile/deployable fact visible to every bot."""

    entity_id: int
    owner_id: int
    team: int
    tool_id: int
    position: tuple[float, float, float]
    radius: float
    expires_at: float

    def snapshot(self) -> EntitySnapshot:
        spec = PROJECTILE_SPECS.get(self.tool_id)
        return EntitySnapshot(
            entity_id=self.entity_id,
            entity_type=int(getattr(spec, "entity_type", -1) or -1),
            team=self.team,
            owner_id=self.owner_id,
            position=self.position,
            kind="projectile",
            tool_id=self.tool_id,
            blast_radius=self.radius,
            detonate_at=self.expires_at,
            hazardous=self.radius > 0.0,
        )


class CitySoak:
    """Run one deterministic accelerated match-shaped policy simulation."""

    def __init__(
        self,
        *,
        map_name: str,
        mode: str,
        bots: int,
        seed: int,
        stranded_water_bots: int = 0,
    ) -> None:
        self.map_name = map_name
        self.mode = "zom" if mode.lower() in {"zom", "zombie"} else "tdm"
        self.bot_count = max(2, min(32, int(bots)))
        self.rng = random.Random(int(seed))
        self.frame_id = 0
        self.topology_version = 0
        self.next_entity_id = 1000
        self.hazards: list[_Hazard] = []
        self.action_counts: Counter[str] = Counter()
        self.role_counts: Counter[str] = Counter()
        self.latest_intents: dict[int, object] = {}
        self.monitor = BotSoakMonitor()
        self.profile_factory = ProfileFactory(seed=seed)

        config = ServerConfig(
            default_map=map_name,
            default_mode=self.mode,
            maps_path=str(ROOT / "maps"),
        )
        self.canonical = WorldManager(config)
        if not self.canonical.load_map(map_name):
            raise RuntimeError(f"could not load map {map_name!r}")
        raw = bytes(self.canonical.map_raw_bytes or b"")
        if not raw:
            raise RuntimeError(f"map {map_name!r} yielded no VXL bytes")
        self.world = WorkerVoxelWorld()
        self.world.load(MapSnapshot(1, 0, raw, self.mode, map_name))
        if not self.world.ready:
            raise RuntimeError("worker navigation map failed to load")
        self.brain = BotBrain(
            self.world,
            seed=seed,
            decision_hz=5.0,
            path_requests_per_second=max(24, self.bot_count * 2),
        )
        self.anchors = {
            TEAM1: self.canonical.team_base_anchor(TEAM1),
            TEAM2: self.canonical.team_base_anchor(TEAM2),
        }
        self.actors = self._make_actors()
        self.stranded_water_bots = self._strand_bots_in_deep_water(
            max(0, min(self.bot_count, int(stranded_water_bots)))
        )
        self.profiles = {
            actor.player_id: self.profile_factory.create("mixed")
            for actor in self.actors
        }
        self.resources = self._make_resources()

    def _strand_bots_in_deep_water(self, count: int) -> int:
        """Place selected actors at the farthest real-map water columns.

        This is an explicit soak fault injection, never a production spawn
        policy. A multi-source shore distance field makes CastleWars choose its
        true 132-cell outer sea instead of a convenient one-cell puddle.
        """

        if count <= 0:
            return 0
        water = {
            (x, y)
            for x in range(512)
            for y in range(512)
            if self.canonical.is_water_column(x, y)
        }
        frontier: deque[tuple[int, int]] = deque()
        distance: dict[tuple[int, int], int] = {}
        offsets = ((1, 0), (-1, 0), (0, 1), (0, -1))
        for cell in water:
            if any(
                0 <= cell[0] + dx < 512
                and 0 <= cell[1] + dy < 512
                and (cell[0] + dx, cell[1] + dy) not in water
                for dx, dy in offsets
            ):
                frontier.append(cell)
                distance[cell] = 1
        while frontier:
            cell = frontier.popleft()
            for dx, dy in offsets:
                neighbor = cell[0] + dx, cell[1] + dy
                if neighbor in water and neighbor not in distance:
                    distance[neighbor] = distance[cell] + 1
                    frontier.append(neighbor)
        candidates = sorted(
            distance,
            key=lambda cell: distance[cell],
            reverse=True,
        )
        selected: list[tuple[int, int]] = []
        for cell in candidates:
            if all(math.dist(cell, previous) >= 32.0 for previous in selected):
                selected.append(cell)
            if len(selected) >= count:
                break
        for actor, (x, y) in zip(self.actors, selected):
            actor.position = (
                float(x) + 0.5,
                float(y) + 0.5,
                float(C.Z_ABOVE_WATERPLANE) - 1.25,
            )
            actor.velocity = (0.0, 0.0, 0.0)
            actor.wade = True
        return len(selected)

    def _make_actors(self) -> list[_Actor]:
        actors: list[_Actor] = []
        ordinary_classes = (
            int(C.CLASS_SOLDIER),
            int(C.CLASS_SCOUT),
            int(C.CLASS_ROCKETEER),
            int(C.CLASS_MINER),
            int(C.CLASS_ENGINEER),
            int(C.CLASS_SPECIALIST),
            int(C.CLASS_MEDIC),
        )
        infected = max(1, self.bot_count // 4)
        offsets = (
            (-7, -7), (-7, 0), (-7, 7), (0, -7),
            (0, 0), (0, 7), (7, -7), (7, 0), (7, 7),
            (-12, 0), (12, 0), (0, -12), (0, 12),
        )
        for index in range(self.bot_count):
            if self.mode == "zom":
                zombie = index >= self.bot_count - infected
                team = TEAM2 if zombie else TEAM1
                class_id = (
                    int(C.CLASS_ZOMBIE)
                    if zombie
                    else ordinary_classes[index % len(ordinary_classes)]
                )
            else:
                team = TEAM1 if index < (self.bot_count + 1) // 2 else TEAM2
                class_id = ordinary_classes[index % len(ordinary_classes)]
            prefabs = sorted(allowed_prefabs_for_class(class_id))[:3]
            selection = normalize_class_selection(class_id, prefabs=prefabs)
            weapon = next(
                (tool for tool in selection.loadout if tool in WEAPON_PROFILES),
                -1,
            )
            melee = next(
                (
                    tool for tool in selection.loadout
                    if tool in {int(value) for value in C.ALL_MELEE_WEAPONS}
                ),
                -1,
            )
            held = weapon if weapon >= 0 else melee
            profile = get_weapon_profile(weapon) if weapon >= 0 else None
            anchor = self.anchors[team]
            ox, oy = offsets[index % len(offsets)]
            spawn = self.canonical.dry_ground_anchor(
                anchor[0] + ox, anchor[1] + oy, search=16
            )
            actors.append(
                _Actor(
                    player_id=index + 1,
                    team=team,
                    class_id=selection.class_id,
                    position=spawn,
                    orientation=(1.0 if team == TEAM1 else -1.0, 0.0, 0.0),
                    loadout=selection.loadout,
                    prefabs=selection.prefabs,
                    weapon_tool=weapon,
                    tool=held,
                    ammo_clip=int(profile.clip_size) if profile is not None else 0,
                    ammo_reserve=int(profile.reserve_ammo) if profile is not None else 0,
                    blocks=int(C.CLASS_BLOCKS.get(class_id, (50, 50))[0]),
                )
            )
        return actors

    def _make_resources(self) -> tuple[EntitySnapshot, ...]:
        result: list[EntitySnapshot] = []
        entity_id = 2000
        for team, anchor in self.anchors.items():
            for offset, entity_type in (
                ((10.0, 4.0), int(C.HEALTH_CRATE)),
                ((-8.0, 7.0), int(C.AMMO_CRATE)),
                ((4.0, -10.0), int(C.BLOCK_CRATE)),
            ):
                position = self.canonical.dry_surface_anchor(
                    anchor[0] + offset[0], anchor[1] + offset[1], search=16
                )
                # Entity z is a surface coordinate; worker resource goals are
                # player positions, so lift it by the standing offset.
                player_position = position[0], position[1], position[2] - 2.25
                result.append(
                    EntitySnapshot(
                        entity_id=entity_id,
                        entity_type=entity_type,
                        team=team,
                        owner_id=-1,
                        position=player_position,
                        kind="resource",
                    )
                )
                entity_id += 1
        return tuple(result)

    def run(self, *, seconds: float, report_every: float) -> dict[str, object]:
        """Advance synthetic time without sleeping and return final metrics."""

        started = time.perf_counter()
        base = time.monotonic() + 1.0
        steps = max(1, int(math.ceil(float(seconds) / DECISION_DT)))
        next_report = max(DECISION_DT, float(report_every))
        for step in range(steps):
            elapsed = step * DECISION_DT
            now = base + elapsed
            self._respawn_due(now)
            self._settle_falling_actors()
            self.hazards = [item for item in self.hazards if item.expires_at > now]
            snapshots = tuple(actor.snapshot() for actor in self.actors)
            entities = self.resources + tuple(item.snapshot() for item in self.hazards)
            objectives = tuple(
                ObjectiveSnapshot("team_anchor", team, position)
                for team, position in self.anchors.items()
            )
            intents = []
            for actor, observer in zip(self.actors, snapshots):
                if not actor.alive:
                    continue
                self.frame_id += 1
                frame = PerceptionFrame(
                    frame_id=self.frame_id,
                    map_epoch=1,
                    mode_epoch=1,
                    topology_version=self.topology_version,
                    observer_id=actor.player_id,
                    observer_generation=1,
                    created_at=now,
                    mode_id=self.mode,
                    players=snapshots,
                    profile=self.profiles[actor.player_id],
                    entities=entities,
                    objectives=objectives,
                    mode_phase="ACTIVE" if self.mode == "zom" else "",
                )
                intent = self.brain.decide(frame)
                if intent is None:
                    continue
                intents.append((actor, observer, intent))
                self.latest_intents[actor.player_id] = intent
                self.monitor.observe(now, observer, intent, snapshots)
                self.action_counts[intent.action.kind.value] += 1
                self.role_counts[intent.debug_role or "idle"] += 1
            for actor, _observer, intent in intents:
                self._apply_intent(actor, intent, now)
            self._collect_resources()
            if elapsed + DECISION_DT >= next_report:
                self._print_status(elapsed + DECISION_DT, now)
                next_report += max(DECISION_DT, float(report_every))

        wall_seconds = time.perf_counter() - started
        summary = self.monitor.summary()
        summary.update(
            {
                "map": self.map_name,
                "mode": self.mode,
                "sim_seconds": round(steps * DECISION_DT, 3),
                "wall_seconds": round(wall_seconds, 3),
                "acceleration": round((steps * DECISION_DT) / max(wall_seconds, 1e-6), 2),
                "native_nav_tiles": self.world.native_tile_count,
                "alive": sum(actor.alive for actor in self.actors),
                "water_remaining": sum(actor.wade for actor in self.actors),
                "positions": {
                    str(actor.player_id): tuple(round(value, 2) for value in actor.position)
                    for actor in self.actors
                },
            }
        )
        return summary

    def _settle_falling_actors(self) -> None:
        """Apply accelerated gravity after terrain beneath an actor is lost.

        The soak has no 60 Hz vertical integrator. Without this adapter, a bot
        left above a mined/collapsed column remains suspended at its old z and
        every legal horizontal candidate appears too far below to traverse.
        Production uses native player gravity; this method only keeps the
        offline diagnostic from reporting that harness artifact as an AI
        navigation stall.
        """

        for actor in self.actors:
            if not actor.alive or actor.wade:
                continue
            x = int(math.floor(actor.position[0]))
            y = int(math.floor(actor.position[1]))
            expected_support = int(round(actor.position[2] + 2.25))
            landing = None
            for support_z in range(max(2, expected_support), 239):
                if not self.world.solid(x, y, support_z):
                    continue
                if self.world.solid(x, y, support_z - 1) or self.world.solid(
                    x, y, support_z - 2
                ):
                    continue
                landing = support_z
                break
            if landing is None or landing <= expected_support + 1:
                continue
            actor.position = (
                actor.position[0],
                actor.position[1],
                float(landing) - 2.25,
            )
            actor.grounded = True
            actor.airborne_until = 0.0

    def _apply_intent(self, actor: _Actor, intent, now: float) -> None:
        if now >= actor.airborne_until:
            actor.grounded = not actor.wade
        if intent.movement.jump and actor.grounded and not actor.wade:
            # The accelerated adapter has no 60 Hz vertical integrator. Keep a
            # short airborne lease so two-phase jump/build recovery observes
            # the same grounded transition the native Player physics emits.
            actor.grounded = False
            actor.airborne_until = now + 0.35
        old = actor.position
        direction = intent.movement.direction
        magnitude = math.hypot(direction[0], direction[1])
        if magnitude > 1e-6:
            speed = SPRINT_SPEED if intent.movement.sprint else MOVE_SPEED
            distance = speed * DECISION_DT
            dx = direction[0] / magnitude * distance
            dy = direction[1] / magnitude * distance
            candidate_x = min(510.5, max(1.5, old[0] + dx))
            candidate_y = min(510.5, max(1.5, old[1] + dy))
            surface = self.world.action_planner.terrain.classify(
                int(math.floor(candidate_x)),
                int(math.floor(candidate_y)),
                old[2],
                # Dry actors must never enter water. A stranded actor must be
                # able to traverse its cached water-only route toward shore.
                allow_water=bool(actor.wade),
                vertical_span=(8 if intent.movement.jump else 3),
            )
            if surface is not None:
                actor.position = candidate_x, candidate_y, surface.support_z - 2.25
            elif intent.debug_role == "stuck_emergency_drop":
                # Native physics permits walking off an unsupported ledge.
                # Gravity settles the actor on the next accelerated frame.
                actor.position = candidate_x, candidate_y, old[2]
        actor.velocity = (
            (actor.position[0] - old[0]) / DECISION_DT,
            (actor.position[1] - old[1]) / DECISION_DT,
            (actor.position[2] - old[2]) / DECISION_DT,
        )
        # The forced waterbed supports a body at z=waterbed-2.25.  A solid at
        # the water-plane coordinate itself is still legal dry terrain.
        actor.wade = actor.position[2] >= float(C.Z_ABOVE_WATERPLANE) - 1.25
        if actor.wade:
            actor.grounded = False
        if intent.look is not None:
            lx = intent.look.target[0] - actor.position[0]
            ly = intent.look.target[1] - actor.position[1]
            length = math.hypot(lx, ly)
            if length > 1e-6:
                actor.orientation = lx / length, ly / length, 0.0
        if intent.tool_id >= 0 and intent.tool_id in actor.loadout:
            actor.tool = int(intent.tool_id)

        action = intent.action
        accepted = True
        if action.kind is BotActionKind.RELOAD:
            profile = get_weapon_profile(actor.weapon_tool)
            needed = max(0, int(profile.clip_size) - actor.ammo_clip)
            loaded = min(needed, actor.ammo_reserve)
            actor.ammo_clip += loaded
            actor.ammo_reserve -= loaded
            actor.tool = actor.weapon_tool
        elif action.kind is BotActionKind.FIRE:
            accepted = actor.weapon_tool >= 0 and actor.ammo_clip > 0
            if accepted:
                actor.tool = actor.weapon_tool
                actor.ammo_clip -= 1
                self._damage_target(actor, intent, now, melee=False)
        elif action.kind is BotActionKind.MELEE:
            accepted = self._damage_target(actor, intent, now, melee=True)
        elif action.kind in {BotActionKind.BUILD, BotActionKind.BUILD_LINE}:
            accepted = self._apply_build(actor, action)
        elif action.kind is BotActionKind.MINE:
            accepted = self._apply_mine(action)
        elif action.kind is BotActionKind.PLACE_PREFAB:
            accepted = actor.blocks >= 10 and action.position is not None
            if accepted:
                actor.blocks -= 10
                accepted = self._apply_single_change(action.position, solid=True)
        elif action.kind in {BotActionKind.ORIENTED, BotActionKind.DEPLOY}:
            accepted = self._spawn_hazard(actor, action, intent, now)

        if action.kind is not BotActionKind.NONE:
            actor.last_action_kind = action.kind.value
            actor.last_action_accepted = bool(accepted)
            actor.last_action_position = action.position
            actor.last_action_frame = int(intent.frame_id)
            actor.last_action_at = now

    def _damage_target(self, actor: _Actor, intent, now: float, *, melee: bool) -> bool:
        target_id = int(intent.look.target_player_id) if intent.look is not None else -1
        target = next((item for item in self.actors if item.player_id == target_id), None)
        if target is None or not target.alive or target.team == actor.team:
            return not melee
        distance = math.dist(actor.position, target.position)
        if melee and distance > 4.25:
            return False
        if not melee and distance > 160.0:
            return True
        if not self.world.has_line_of_sight(actor.position, target.position):
            return not melee
        damage = (
            45
            if melee
            else max(
                8,
                int(get_weapon_profile(actor.weapon_tool).base_damage * 0.45),
            )
        )
        target.health -= damage
        target.last_damage_at = now
        target.last_damage_source_id = actor.player_id
        target.last_damage_source_position = actor.position
        if target.health <= 0:
            target.alive = False
            target.health = 0
            target.life_id += 1
            target.respawn_at = now + 3.0
        return True

    def _apply_build(self, actor: _Actor, action) -> bool:
        if actor.blocks <= 0 or action.position is None:
            return False
        if action.kind is BotActionKind.BUILD_LINE and action.end_position is not None:
            cells = tuple(
                cube_line(
                    *(int(round(value)) for value in action.position),
                    *(int(round(value)) for value in action.end_position),
                )
            )
        else:
            cells = (tuple(int(round(value)) for value in action.position),)
        valid = [cell for cell in cells if not self.world.solid(*cell)]
        if not valid or len(valid) > actor.blocks:
            return False
        changes = tuple(VoxelChange(*cell, True, 0x777777) for cell in valid)
        actor.blocks -= len(valid)
        self._apply_delta(changes)
        return True

    def _apply_mine(self, action) -> bool:
        if action.position is None:
            return False
        cell = tuple(int(round(value)) for value in action.position)
        if not self.world.solid(*cell):
            return False
        self._apply_delta((VoxelChange(*cell, False, 0),))
        return True

    def _apply_single_change(self, position, *, solid: bool) -> bool:
        cell = tuple(int(round(value)) for value in position)
        if solid == self.world.solid(*cell):
            return False
        self._apply_delta((VoxelChange(*cell, solid, 0x777777),))
        return True

    def _apply_delta(self, changes: tuple[VoxelChange, ...]) -> None:
        self.topology_version += 1
        self.world.apply(WorldDelta(1, self.topology_version, changes))

    def _spawn_hazard(self, actor: _Actor, action, intent, now: float) -> bool:
        spec = PROJECTILE_SPECS.get(int(action.tool_id))
        radius = float(getattr(spec, "blast_radius", 0.0) or 0.0)
        if radius <= 0.0:
            return True
        position = action.position
        if position is None and intent.look is not None:
            position = intent.look.target
        if position is None:
            position = actor.position
        # Mirror the authoritative last-moment friendly-body safety gate.
        if any(
            teammate.alive
            and teammate.team == actor.team
            and teammate.player_id != actor.player_id
            and math.dist(teammate.position, position) <= radius + 1.5
            for teammate in self.actors
        ):
            return False
        fuse = max(1.0, float(getattr(spec, "fuse", 0.0) or 2.5))
        self.hazards.append(
            _Hazard(
                entity_id=self.next_entity_id,
                owner_id=actor.player_id,
                team=actor.team,
                tool_id=int(action.tool_id),
                position=position,
                radius=radius,
                expires_at=now + fuse,
            )
        )
        self.next_entity_id += 1
        return True

    def _collect_resources(self) -> None:
        for actor in self.actors:
            if not actor.alive:
                continue
            for resource in self.resources:
                if resource.team not in (-1, actor.team):
                    continue
                if math.dist(actor.position, resource.position) > 2.25:
                    continue
                if resource.entity_type == int(C.HEALTH_CRATE):
                    actor.health = 100
                elif resource.entity_type == int(C.AMMO_CRATE) and actor.weapon_tool >= 0:
                    profile = get_weapon_profile(actor.weapon_tool)
                    actor.ammo_clip = int(profile.clip_size)
                    actor.ammo_reserve = int(profile.reserve_ammo)
                elif resource.entity_type == int(C.BLOCK_CRATE):
                    actor.blocks = 50

    def _respawn_due(self, now: float) -> None:
        for actor in self.actors:
            if actor.alive or actor.respawn_at > now:
                continue
            actor.alive = True
            actor.health = 100
            actor.position = self.canonical.dry_ground_anchor(
                self.anchors[actor.team][0], self.anchors[actor.team][1], search=16
            )
            profile = get_weapon_profile(actor.weapon_tool) if actor.weapon_tool >= 0 else None
            actor.ammo_clip = int(profile.clip_size) if profile is not None else 0
            actor.ammo_reserve = int(profile.reserve_ammo) if profile is not None else 0
            actor.tool = actor.weapon_tool if actor.weapon_tool >= 0 else actor.tool
            actor.blocks = int(C.CLASS_BLOCKS.get(actor.class_id, (50, 50))[0])
            actor.grounded = True
            actor.airborne_until = 0.0

    def _print_status(self, elapsed: float, now: float) -> None:
        rows = []
        for actor in self.actors:
            state = self.brain._states.get((actor.player_id, 1))
            intent = self.latest_intents.get(actor.player_id)
            surface = self.world.action_planner.terrain.classify(
                int(math.floor(actor.position[0])),
                int(math.floor(actor.position[1])),
                actor.position[2],
                vertical_span=5,
            )
            stationary = (
                max(0.0, now - state.last_progress_at)
                if state is not None
                else 0.0
            )
            rows.append(
                {
                    "id": actor.player_id,
                    "team": actor.team,
                    "class": actor.class_id,
                    "pos": [round(value, 1) for value in actor.position],
                    "hp": actor.health,
                    "ammo": [actor.ammo_clip, actor.ammo_reserve],
                    "tool": actor.tool,
                    "stuck_attempts": int(state.stuck_attempts) if state is not None else 0,
                    "route_failures": (
                        int(state.route_escape_failures)
                        if state is not None else 0
                    ),
                    "strategic_stall": (
                        round(max(0.0, now - state.strategic_progress_at), 1)
                        if state is not None
                        and state.strategic_progress_at > 0.0
                        else 0.0
                    ),
                    "stationary": round(stationary, 1),
                    "support": (
                        int(surface.support_z) if surface is not None else None
                    ),
                    "emergency_drop": bool(
                        self.world.emergency_drop(actor.position)
                    ),
                    "role": getattr(intent, "debug_role", "idle") or "idle",
                    "action": (
                        intent.action.kind.value if intent is not None else "none"
                    ),
                    "affordance": (
                        intent.movement.affordance.value
                        if intent is not None else "walk"
                    ),
                    "direction": (
                        [round(value, 2) for value in intent.movement.direction]
                        if intent is not None else [0.0, 0.0, 0.0]
                    ),
                }
            )
        print(
            json.dumps(
                {
                    "sim_time": round(elapsed, 1),
                    "monitor": self.monitor.summary(),
                    "bots": rows,
                },
                sort_keys=True,
            ),
            flush=True,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--map", default="CityOfChicago")
    parser.add_argument("--mode", choices=("tdm", "zom", "zombie"), default="tdm")
    parser.add_argument("--bots", type=int, default=12)
    parser.add_argument("--sim-seconds", type=float, default=120.0)
    parser.add_argument("--report-every", type=float, default=10.0)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument(
        "--strand-water-bots",
        type=int,
        default=0,
        help="fault-inject N bots into the real map's farthest water columns",
    )
    args = parser.parse_args()

    soak = CitySoak(
        map_name=args.map,
        mode=args.mode,
        bots=args.bots,
        seed=args.seed,
        stranded_water_bots=args.strand_water_bots,
    )
    summary = soak.run(seconds=args.sim_seconds, report_every=args.report_every)
    print("FINAL " + json.dumps(summary, sort_keys=True), flush=True)
    # These are correctness invariants, not tuning preferences.
    failed = any(
        int(summary[key]) > 0
        for key in (
            "priority_violations",
            "action_loops",
            "jump_loops",
            "navigation_stalls",
            "invalid_looks",
        )
    ) or (
        int(summary["water_remaining"]) > 0
        if args.strand_water_bots > 0
        else float(summary["max_water_seconds"]) > 3.0
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
