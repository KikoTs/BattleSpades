"""
Player entity for BattleSpades.
Represents a connected player with position, health, inventory, and input state.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import Optional, Tuple, TYPE_CHECKING

import shared.constants as C
from server.game_constants import (
    BLOCK_TOOL_IDS,
    DEFAULT_WEAPON_TOOL,
    GRENADE_TOOL_IDS,
    MAX_BLOCKS,
    MAX_GRENADES,
    MAX_HEALTH,
    PLAYER_CROUCH_HEIGHT,
    PLAYER_HEIGHT,
    SPADE_PROFILE,
    SPADE_TOOL_IDS,
    WEAPON_PROFILES,
    WEAPON_TOOL_IDS,
)
from aoslib.world import Player as WorldPlayer

if TYPE_CHECKING:
    from .connection import Connection

logger = logging.getLogger(__name__)

JUMP_BUFFER_SECONDS = 0.25
POSITION_SAMPLE_FRESHNESS_SECONDS = 0.20
POSITION_DRIFT_DEADZONE = 0.25
POSITION_HARD_SNAP_THRESHOLD = 4.0
POSITION_SOFT_CORRECTION_RATE = 0.15
MAX_HORIZONTAL_SOFT_CORRECTION = 0.12
MAX_VERTICAL_SOFT_CORRECTION = 0.08
MAX_VERTICAL_SOFT_CORRECTION_DISTANCE = 0.75
WORLD_ORIENTATION_HORIZONTAL_EPSILON = 0.001
VELOCITY_ZERO_THRESHOLD = 0.001


@dataclass(frozen=True)
class MovementProfile:
    starting_blocks: int
    max_blocks: int
    accel_multiplier: float
    sprint_multiplier: float
    jump_multiplier: float
    crouch_sneak_multiplier: float
    can_sprint_uphill: bool
    water_friction: float
    damage_multiplier: float
    headshot_damage_multiplier: float
    fall_on_water_damage_multiplier: float
    falling_damage_min_distance: int
    falling_damage_max_distance: int
    falling_damage_max_damage: int


def get_movement_profile(class_id: int) -> MovementProfile:
    starting_blocks, max_blocks = C.CLASS_BLOCKS.get(class_id, (MAX_BLOCKS, MAX_BLOCKS))
    return MovementProfile(
        starting_blocks=starting_blocks,
        max_blocks=max_blocks,
        accel_multiplier=C.CLASS_ACCEL_MULTIPLIER.get(class_id, 0.7),
        sprint_multiplier=C.CLASS_SPRINT_MULTIPLIER.get(class_id, 1.4),
        jump_multiplier=C.CLASS_JUMP_MULTIPLIER.get(class_id, 1.2),
        crouch_sneak_multiplier=C.CLASS_CROUCH_SNEAK_MULTIPLIER.get(class_id, 0.5),
        can_sprint_uphill=C.CLASS_CAN_SPRINT_UPHILL.get(class_id, True),
        water_friction=C.CLASS_WATER_FRICTION.get(class_id, 8.0),
        damage_multiplier=C.CLASS_DAMAGE_MULTIPLIER.get(class_id, 1.0),
        headshot_damage_multiplier=C.CLASS_HEADSHOT_DAMAGE_MULTIPLIER.get(class_id, 1.0),
        fall_on_water_damage_multiplier=C.CLASS_FALL_ON_WATER_DAMAGE_MULTIPLIER.get(class_id, 0.5),
        falling_damage_min_distance=C.CLASS_FALLING_DAMAGE_MIN_DISTANCE.get(class_id, 10),
        falling_damage_max_distance=C.CLASS_FALLING_DAMAGE_MAX_DISTANCE.get(class_id, 40),
        falling_damage_max_damage=C.CLASS_FALLING_DAMAGE_MAX_DAMAGE.get(class_id, MAX_HEALTH),
    )


@dataclass
class InputState:
    """Current input state from the most recent client packet."""

    up: bool = False
    down: bool = False
    left: bool = False
    right: bool = False
    jump: bool = False
    crouch: bool = False
    sneak: bool = False
    sprint: bool = False

    primary_fire: bool = False
    secondary_fire: bool = False
    zoom: bool = False
    can_pickup: bool = False
    can_display_weapon: bool = False
    is_on_fire: bool = False
    is_weapon_deployed: bool = False
    hover: bool = False
    palette_enabled: bool = False


class Player:
    """
    Represents a player in the game.
    Handles position, health, class state, and input processing.
    """

    def __init__(
        self,
        id: int,
        name: str,
        team: int,
        weapon: int,
        connection: Optional["Connection"] = None,
    ):
        self.id = id
        self.name = name
        self.team = team
        self.weapon = weapon if weapon in WEAPON_PROFILES else DEFAULT_WEAPON_TOOL
        self.connection = connection

        self.x: float = 0.0
        self.y: float = 0.0
        self.z: float = 0.0
        self.vx: float = 0.0
        self.vy: float = 0.0
        self.vz: float = 0.0

        self.o_x: float = 1.0
        self.o_y: float = 0.0
        self.o_z: float = 0.0
        self.side_x: float = 0.0
        self.side_y: float = 1.0
        self.side_z: float = 0.0
        self.head_x: float = 0.0
        self.head_y: float = 0.0
        self.head_z: float = 1.0

        self.eye_x: float = 0.0
        self.eye_y: float = 0.0
        self.eye_z: float = 0.0

        self.yaw: float = 0.0
        self.pitch: float = 0.0

        self.health: int = MAX_HEALTH
        self.grenades: int = MAX_GRENADES
        self.tool: int = self.weapon
        self.tool_is_raw: bool = True
        self.block_color: int = 0x00FFFF

        self.ammo_clip: int = 10
        self.ammo_reserve: int = 50
        self.last_shot_time: float = 0.0
        self.reload_end_time: float = 0.0
        self.reloading: bool = False

        self.spawned: bool = False
        self.alive: bool = False
        self.admin: bool = False
        self.muted: bool = False
        self.grounded: bool = True
        self.airborne: bool = False
        self.wade: bool = False

        self.respawn_time: float = 0.0
        self.death_time: float = 0.0

        self.input = InputState()

        self.last_update: float = time.time()
        self.last_position_update: float = 0.0
        self.last_reported_position: Optional[Tuple[float, float, float]] = None
        self.last_position_drift: float = 0.0
        self.last_position_drift_vector: Tuple[float, float, float] = (0.0, 0.0, 0.0)
        self.last_climb_time: float = 0.0
        self.last_fall_result: int = 0
        self.movement_time: float = 0.0
        self.jump_held: bool = False
        self.jump_last_held: bool = False
        self.pending_jump: bool = False
        self.jump_buffer_until: float = 0.0

        self.kills: int = 0
        self.deaths: int = 0
        self.captures: int = 0

        self._class_id: int = int(C.CLASS.SOLDIER)
        self.movement_profile = get_movement_profile(self._class_id)
        self.blocks: int = self.movement_profile.starting_blocks

        self._world_object = None
        self._world_parent = None
        self._reset_ammo()
        self._ensure_world_object()
        self._sync_cached_vectors()

    def _get_world(self):
        if self.connection and self.connection.server and self.connection.server.world_manager:
            return getattr(self.connection.server.world_manager, "world", None)
        return None

    def _current_height(self) -> float:
        if self.input.crouch and not self.wade:
            return PLAYER_CROUCH_HEIGHT
        return PLAYER_HEIGHT

    def _orientation_for_world(self, x: float, y: float, z: float) -> tuple[float, float, float]:
        horizontal_magnitude = math.sqrt(x * x + y * y)
        if horizontal_magnitude > WORLD_ORIENTATION_HORIZONTAL_EPSILON:
            return (x, y, z)

        forward_x = self.side_y
        forward_y = -self.side_x
        forward_magnitude = math.sqrt(forward_x * forward_x + forward_y * forward_y)
        if forward_magnitude <= 0.000001:
            return (x, y, z)

        forward_x /= forward_magnitude
        forward_y /= forward_magnitude
        if abs(z) <= 0.000001:
            return (forward_x, forward_y, 0.0)

        horizontal = WORLD_ORIENTATION_HORIZONTAL_EPSILON
        vertical = math.sqrt(max(0.0, 1.0 - horizontal * horizontal))
        if z < 0.0:
            vertical = -vertical
        return (forward_x * horizontal, forward_y * horizontal, vertical)

    def _build_player_collision_positions(self) -> list[tuple[float, float, float, float]]:
        server = self.connection.server if self.connection else None
        if server is None:
            return []
        players = getattr(server, "players", None)
        if not players:
            return []

        positions = []
        for player in players.values():
            if player is self or not player.alive or not player.spawned:
                continue
            positions.append((player.x, player.y, player.z, player._current_height()))
        return positions

    def _apply_class_profile_to_world(self, world_object) -> None:
        world_object.set_class_accel_multiplier(self.movement_profile.accel_multiplier)
        world_object.set_class_sprint_multiplier(self.movement_profile.sprint_multiplier)
        world_object.set_class_jump_multiplier(self.movement_profile.jump_multiplier)
        world_object.set_class_crouch_sneak_multiplier(self.movement_profile.crouch_sneak_multiplier)
        world_object.set_class_can_sprint_uphill(self.movement_profile.can_sprint_uphill)
        world_object.set_class_water_friction(self.movement_profile.water_friction)
        world_object.set_class_fall_on_water_damage_multiplier(
            self.movement_profile.fall_on_water_damage_multiplier
        )
        world_object.set_class_falling_damage_min_distance(
            self.movement_profile.falling_damage_min_distance
        )
        world_object.set_class_falling_damage_max_distance(
            self.movement_profile.falling_damage_max_distance
        )
        world_object.set_class_falling_damage_max_damage(
            self.movement_profile.falling_damage_max_damage
        )

    def _ensure_world_object(self, reset: bool = False):
        world = self._get_world()
        if world is None:
            self._world_object = None
            self._world_parent = None
            return None

        if reset or self._world_object is None or self._world_parent is not world:
            self._world_parent = world
            self._world_object = WorldPlayer(world)
            self._world_object.set_position(self.x, self.y, self.z)
            self._world_object.set_velocity(self.vx, self.vy, self.vz)
            self._world_object.set_orientation(self._orientation_for_world(self.o_x, self.o_y, self.o_z))
            self._world_object.set_dead(not self.alive)
            self._apply_class_profile_to_world(self._world_object)
            self._apply_input_state_to_world(trigger_jump=False, world_object=self._world_object)
        return self._world_object

    def _apply_input_state_to_world(self, trigger_jump: bool, world_object=None):
        if world_object is None:
            world_object = self._ensure_world_object()
        if world_object is None:
            return

        world_object.set_walk(
            self.input.up,
            self.input.down,
            self.input.left,
            self.input.right,
        )
        # Only the server tick may inject a one-frame jump into the native world.
        world_object.jump = bool(trigger_jump)
        world_object.sneak = self.input.sneak
        world_object.sprint = self.input.sprint
        world_object.hover = self.input.hover
        collisions = self._build_player_collision_positions()
        world_object.set_crouch(self.input.crouch, collisions, len(collisions))

    def _compute_head_vector(self) -> tuple[float, float, float]:
        hx = self.o_y * self.side_z - self.o_z * self.side_y
        hy = self.o_z * self.side_x - self.o_x * self.side_z
        hz = self.o_x * self.side_y - self.o_y * self.side_x
        magnitude = math.sqrt(hx * hx + hy * hy + hz * hz)
        if magnitude <= 0.000001:
            return (0.0, 0.0, 1.0)
        return (hx / magnitude, hy / magnitude, hz / magnitude)

    def _sync_cached_vectors(self):
        if self._world_object is None:
            return

        world_x, world_y, world_z = tuple(self._world_object.position)
        self.x = world_x
        self.y = world_y
        self.z = world_z
        self.vx, self.vy, self.vz = tuple(self._world_object.velocity)
        if abs(self.vx) < VELOCITY_ZERO_THRESHOLD:
            self.vx = 0.0
        if abs(self.vy) < VELOCITY_ZERO_THRESHOLD:
            self.vy = 0.0
        if abs(self.vz) < VELOCITY_ZERO_THRESHOLD:
            self.vz = 0.0
        self.side_x, self.side_y, self.side_z = tuple(self._world_object.s)
        self.head_x, self.head_y, self.head_z = self._compute_head_vector()
        self.eye_x, self.eye_y, self.eye_z = self.x, self.y, self.z
        self.airborne = bool(self._world_object.airborne)
        self.wade = bool(self._world_object.wade)
        self.grounded = not self.airborne
        self.last_climb_time = 0.0

    @property
    def class_id(self) -> int:
        return self._class_id

    @class_id.setter
    def class_id(self, value: int):
        self._class_id = int(value)
        self.movement_profile = get_movement_profile(self._class_id)
        self.blocks = min(self.blocks, self.movement_profile.max_blocks)
        world_object = self._ensure_world_object()
        if world_object is not None:
            self._apply_class_profile_to_world(world_object)
            self._sync_cached_vectors()

    @property
    def position(self) -> Tuple[float, float, float]:
        return (self.x, self.y, self.z)

    @position.setter
    def position(self, value: Tuple[float, float, float]):
        self.set_position(*value)

    @property
    def eye(self) -> Tuple[float, float, float]:
        return (self.eye_x, self.eye_y, self.eye_z)

    @property
    def orientation(self) -> Tuple[float, float, float]:
        return (self.o_x, self.o_y, self.o_z)

    @orientation.setter
    def orientation(self, value: Tuple[float, float, float]):
        self.set_orientation_vector(*value)

    @property
    def velocity(self) -> Tuple[float, float, float]:
        return (self.vx, self.vy, self.vz)

    @velocity.setter
    def velocity(self, value: Tuple[float, float, float]):
        self.vx, self.vy, self.vz = value
        world_object = self._ensure_world_object()
        if world_object is not None:
            world_object.set_velocity(*value)
            self._sync_cached_vectors()

    def is_spade_tool(self) -> bool:
        return self.tool in SPADE_TOOL_IDS if self.tool_is_raw else False

    def is_block_tool(self) -> bool:
        return self.tool in BLOCK_TOOL_IDS if self.tool_is_raw else False

    def is_grenade_tool(self) -> bool:
        return self.tool in GRENADE_TOOL_IDS if self.tool_is_raw else False

    def is_weapon_tool(self) -> bool:
        if self.tool_is_raw:
            return self.tool in WEAPON_TOOL_IDS
        return self.weapon in WEAPON_PROFILES

    def get_combat_weapon_type(self) -> int:
        if self.tool_is_raw and self.tool in WEAPON_PROFILES:
            return self.tool
        if self.weapon in WEAPON_PROFILES:
            return self.weapon
        return DEFAULT_WEAPON_TOOL

    def set_position(self, x: float, y: float, z: float):
        self.x = x
        self.y = y
        self.z = z
        self.eye_x = x
        self.eye_y = y
        self.eye_z = z
        world_object = self._ensure_world_object()
        if world_object is not None:
            world_object.set_position(x, y, z)
            self._sync_cached_vectors()

    def set_orientation(self, yaw: float, pitch: float):
        self.yaw = yaw
        self.pitch = pitch

    def set_orientation_vector(self, x: float, y: float, z: float):
        raw_x, raw_y, raw_z = x, y, z
        magnitude = math.sqrt(x * x + y * y + z * z)
        if magnitude <= 0.000001:
            x, y, z = self.o_x, self.o_y, self.o_z
        else:
            x /= magnitude
            y /= magnitude
            z /= magnitude
            if logger.isEnabledFor(logging.DEBUG) and abs(magnitude - 1.0) > 0.25:
                logger.debug(
                    "Normalizing suspicious orientation for %s: raw=(%.4f, %.4f, %.4f) "
                    "sanitized=(%.4f, %.4f, %.4f) magnitude=%.4f",
                    self.name,
                    raw_x,
                    raw_y,
                    raw_z,
                    x,
                    y,
                    z,
                    magnitude,
                )

        self.o_x = x
        self.o_y = y
        self.o_z = z

        world_object = self._ensure_world_object()
        if world_object is not None:
            world_object.set_orientation(self._orientation_for_world(x, y, z))
            self._sync_cached_vectors()

    def spawn(self, x: float, y: float, z: float):
        self.health = MAX_HEALTH
        self.alive = True
        self.spawned = True
        self.input = InputState()
        self.blocks = self.movement_profile.starting_blocks
        self.grenades = MAX_GRENADES
        self._reset_ammo()
        self.last_reported_position = (x, y, z)
        self.last_position_drift = 0.0
        self.last_position_drift_vector = (0.0, 0.0, 0.0)
        self.movement_time = 0.0
        self.last_fall_result = 0
        self.last_climb_time = 0.0
        self.airborne = False
        self.wade = False
        self.grounded = True
        self.jump_held = False
        self.jump_last_held = False
        self.pending_jump = False
        self.jump_buffer_until = 0.0
        self.last_shot_time = 0.0
        self.reload_end_time = 0.0
        self.reloading = False

        self.x = x
        self.y = y
        self.z = z
        self.vx = self.vy = self.vz = 0.0
        self.eye_x = x
        self.eye_y = y
        self.eye_z = z
        self.tool = self.weapon
        self.tool_is_raw = True

        world_object = self._ensure_world_object(reset=True)
        if world_object is not None:
            world_object.set_dead(False)
            world_object.set_velocity(0.0, 0.0, 0.0)
            world_object.set_position(x, y, z)
            world_object.set_orientation(self._orientation_for_world(self.o_x, self.o_y, self.o_z))
            self._apply_class_profile_to_world(world_object)
            self._apply_input_state_to_world(trigger_jump=False, world_object=world_object)
            self._sync_cached_vectors()

        logger.debug("Player %s spawned at (%.1f, %.1f, %.1f)", self.name, x, y, z)

    def get_weapon_profile(self):
        if self.is_spade_tool():
            return SPADE_PROFILE
        return WEAPON_PROFILES.get(
            self.get_combat_weapon_type(),
            WEAPON_PROFILES[next(iter(WEAPON_PROFILES))],
        )

    def _reset_ammo(self):
        profile = WEAPON_PROFILES.get(
            self.weapon,
            WEAPON_PROFILES[next(iter(WEAPON_PROFILES))],
        )
        self.ammo_clip = profile.clip_size
        self.ammo_reserve = profile.reserve_ammo

    def can_fire(self, now: Optional[float] = None) -> bool:
        if not self.alive or not self.spawned:
            return False

        profile = self.get_weapon_profile()
        current_time = time.monotonic() if now is None else now
        if self.reloading and current_time < self.reload_end_time:
            return False
        if current_time - self.last_shot_time < profile.fire_interval:
            return False

        if self.is_spade_tool():
            return True
        return self.is_weapon_tool() and self.ammo_clip > 0

    def consume_shot(self, now: Optional[float] = None) -> bool:
        if not self.can_fire(now):
            return False

        self.last_shot_time = time.monotonic() if now is None else now
        if self.is_weapon_tool():
            self.ammo_clip = max(0, self.ammo_clip - 1)
        return True

    def start_reload(self, now: Optional[float] = None) -> bool:
        if not self.alive or not self.spawned or not self.is_weapon_tool():
            return False

        profile = self.get_weapon_profile()
        if self.reloading or self.ammo_reserve <= 0 or self.ammo_clip >= profile.clip_size:
            return False

        current_time = time.monotonic() if now is None else now
        self.reloading = True
        self.reload_end_time = current_time + profile.reload_time
        return True

    def finish_reload(self) -> bool:
        if not self.reloading:
            return False

        profile = self.get_weapon_profile()
        needed = max(0, profile.clip_size - self.ammo_clip)
        loaded = min(needed, self.ammo_reserve)
        self.ammo_clip += loaded
        self.ammo_reserve -= loaded
        self.reloading = False
        self.reload_end_time = 0.0
        return True

    def _broadcast_reload_state(self, is_done: bool):
        server = self.connection.server if self.connection else None
        if server is None:
            return

        from shared.packet import WeaponReload

        packet = WeaponReload()
        packet.player_id = self.id
        packet.tool_id = self.tool
        packet.is_done = 1 if is_done else 0
        server.broadcast(bytes(packet.generate()))

    def damage(self, amount: int, source: Optional["Player"] = None, kill_type: int = 0) -> bool:
        if not self.alive:
            return False

        amount = max(0, int(round(amount)))
        if amount <= 0:
            return False

        self.health = max(0, self.health - amount)
        source_position = self.position if source is None else source.position
        damage_type = 0 if source is None or source == self else 1
        if self.connection:
            from shared.packet import SetHP

            packet = SetHP()
            packet.hp = self.health
            packet.damage_type = damage_type
            packet.source_x, packet.source_y, packet.source_z = source_position
            self.connection.send(bytes(packet.generate()))

        if self.health <= 0:
            self.die(killer=source, kill_type=kill_type)
            return True
        return False

    def die(self, killer: Optional["Player"] = None, kill_type: int = 0):
        if not self.alive and not self.spawned:
            return

        self.alive = False
        self.spawned = False
        self.grounded = False
        self.airborne = False
        self.wade = False
        self.death_time = time.time()
        self.deaths += 1
        self.reloading = False
        self.reload_end_time = 0.0

        world_object = self._ensure_world_object()
        if world_object is not None:
            world_object.set_dead(True)
            self._sync_cached_vectors()

        kill_count = 0
        if killer and killer != self:
            killer.kills += 1
            kill_count = killer.kills

        server = self.connection.server if self.connection else None
        if server is not None:
            from shared.packet import KillAction

            packet = KillAction()
            packet.player_id = self.id
            packet.killer_id = killer.id if killer is not None else self.id
            packet.kill_type = kill_type
            packet.respawn_time = int(server.config.respawn_time)
            packet.kill_count = kill_count
            packet.isDominationKill = 0
            packet.isRevengeKill = 0
            server.broadcast(bytes(packet.generate()))

        logger.debug("Player %s died (killer: %s)", self.name, killer.name if killer else "none")

    def heal(self, amount: int):
        if self.alive:
            self.health = min(MAX_HEALTH, self.health + amount)
            if self.connection:
                from shared.packet import SetHP

                packet = SetHP()
                packet.hp = self.health
                packet.damage_type = 2
                packet.source_x = 0.0
                packet.source_y = 0.0
                packet.source_z = 0.0
                self.connection.send(bytes(packet.generate()))

    def add_blocks(self, count: int = 1):
        self.blocks = min(self.movement_profile.max_blocks, self.blocks + count)

    def remove_block(self) -> bool:
        if self.blocks > 0:
            self.blocks -= 1
            return True
        return False

    def set_tool(self, tool: int, raw: Optional[bool] = None):
        if raw is None:
            raw = (
                tool in BLOCK_TOOL_IDS
                or tool in SPADE_TOOL_IDS
                or tool in GRENADE_TOOL_IDS
                or tool in WEAPON_TOOL_IDS
            )
        self.tool = tool
        self.tool_is_raw = raw
        if raw and tool in WEAPON_PROFILES:
            if tool != self.weapon:
                self.weapon = tool
                self._reset_ammo()
        if not self.is_weapon_tool():
            self.reloading = False
            self.reload_end_time = 0.0

    def set_color(self, color: int):
        self.block_color = color

    def _buffered_jump_is_active(self) -> bool:
        if self.jump_buffer_until <= 0.0:
            return False
        if self.movement_time > self.jump_buffer_until:
            self.jump_buffer_until = 0.0
            return False
        return True

    def _launch_buffered_jump(self, world_object, positions) -> bool:
        if world_object is None or not self._buffered_jump_is_active():
            return False

        world_object.set_velocity(self.vx, self.vy, self.movement_profile.jump_multiplier * -0.36)
        world_object.jump = False
        world_object.update(0.0, positions)
        self.pending_jump = False
        self.jump_buffer_until = 0.0
        self._sync_cached_vectors()
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "Consumed buffered jump for %s: held=%s airborne=%s z=%.4f vz=%.4f",
                self.name,
                self.jump_held,
                self.airborne,
                self.z,
                self.vz,
            )
        return True

    async def update(self, dt: float):
        if not self.alive or not self.spawned:
            return

        world_object = self._ensure_world_object()
        if world_object is None:
            return

        if self.reloading and time.monotonic() >= self.reload_end_time:
            if self.finish_reload():
                self._broadcast_reload_state(True)

        self.last_update = time.time()
        self.movement_time += dt
        was_airborne = self.airborne
        buffered_jump_active = self._buffered_jump_is_active()
        trigger_jump = self.pending_jump
        if trigger_jump and logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "Consuming pending jump for %s: held=%s grounded=%s pos=(%.4f, %.4f, %.4f)",
                self.name,
                self.jump_held,
                self.grounded,
                self.x,
                self.y,
                self.z,
            )
        self._apply_input_state_to_world(trigger_jump=trigger_jump)
        positions = self._build_player_collision_positions()
        result = world_object.update(dt, positions)
        if trigger_jump:
            self.pending_jump = False
        self.last_fall_result = int(result or 0)
        self._sync_cached_vectors()
        if trigger_jump and logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "Pending jump result for %s: airborne=%s grounded=%s z=%.4f vz=%.4f",
                self.name,
                self.airborne,
                self.grounded,
                self.z,
                self.vz,
            )
        if buffered_jump_active and was_airborne and self.grounded:
            self._launch_buffered_jump(world_object, positions)
        if self.jump_buffer_until > 0.0 and self.vz < -0.05:
            self.jump_buffer_until = 0.0

    def update_input(
        self,
        up: bool,
        down: bool,
        left: bool,
        right: bool,
        jump: bool,
        crouch: bool,
        sneak: bool,
        sprint: bool,
    ):
        self.input.up = up
        self.input.down = down
        self.input.left = left
        self.input.right = right
        self.jump_last_held = self.jump_held
        self.jump_held = jump
        self.input.jump = jump
        self.input.crouch = crouch
        self.input.sneak = sneak
        self.input.sprint = sprint

        if self.jump_held and not self.jump_last_held:
            if self.grounded:
                self.pending_jump = True
            else:
                self.jump_buffer_until = self.movement_time + JUMP_BUFFER_SECONDS
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "Queued jump input for %s: held=%s grounded=%s pending_jump=%s buffer_until=%.3f",
                    self.name,
                    self.jump_held,
                    self.grounded,
                    self.pending_jump,
                    self.jump_buffer_until,
                )

        world_object = self._ensure_world_object()
        if world_object is not None:
            self._apply_input_state_to_world(trigger_jump=False, world_object=world_object)
            self._sync_cached_vectors()

    def update_action_input(
        self,
        primary: bool,
        secondary: bool,
        zoom: bool = False,
        can_pickup: bool = False,
        can_display_weapon: bool = False,
        is_on_fire: bool = False,
        is_weapon_deployed: bool = False,
        hover: bool = False,
        palette_enabled: bool = False,
    ):
        self.input.primary_fire = primary
        self.input.secondary_fire = secondary
        self.input.zoom = zoom
        self.input.can_pickup = can_pickup
        self.input.can_display_weapon = can_display_weapon
        self.input.is_on_fire = is_on_fire
        self.input.is_weapon_deployed = is_weapon_deployed
        self.input.hover = hover
        self.input.palette_enabled = palette_enabled

        world_object = self._ensure_world_object()
        if world_object is not None:
            world_object.hover = hover
            self._sync_cached_vectors()

    def _record_position_drift(self):
        if self.last_reported_position is None:
            self.last_position_drift_vector = (0.0, 0.0, 0.0)
            self.last_position_drift = 0.0
            return

        dx = self.last_reported_position[0] - self.x
        dy = self.last_reported_position[1] - self.y
        dz = self.last_reported_position[2] - self.z
        self.last_position_drift_vector = (dx, dy, dz)
        self.last_position_drift = math.sqrt(dx * dx + dy * dy + dz * dz)

    def _clamp_correction(self, value: float, limit: float) -> float:
        if value > limit:
            return limit
        if value < -limit:
            return -limit
        return value

    def _apply_soft_drift_correction(self):
        if self._world_object is None or self.last_reported_position is None:
            return
        if self.last_position_update <= 0.0:
            return
        if (time.time() - self.last_position_update) > POSITION_SAMPLE_FRESHNESS_SECONDS:
            return

        self._record_position_drift()
        if self.last_position_drift <= POSITION_DRIFT_DEADZONE:
            return

        dx, dy, dz = self.last_position_drift_vector
        if self.last_position_drift > POSITION_HARD_SNAP_THRESHOLD:
            self._world_object.set_position(
                self.last_reported_position[0],
                self.last_reported_position[1],
                self.last_reported_position[2],
            )
            self._sync_cached_vectors()
            self._record_position_drift()
            return

        correction_x = self._clamp_correction(
            dx * POSITION_SOFT_CORRECTION_RATE,
            MAX_HORIZONTAL_SOFT_CORRECTION,
        )
        correction_y = self._clamp_correction(
            dy * POSITION_SOFT_CORRECTION_RATE,
            MAX_HORIZONTAL_SOFT_CORRECTION,
        )
        correction_z = 0.0
        if self.grounded and abs(dz) <= MAX_VERTICAL_SOFT_CORRECTION_DISTANCE:
            correction_z = self._clamp_correction(
                dz * POSITION_SOFT_CORRECTION_RATE,
                MAX_VERTICAL_SOFT_CORRECTION,
            )

        if correction_x == 0.0 and correction_y == 0.0 and correction_z == 0.0:
            return

        self._world_object.set_position(
            self.x + correction_x,
            self.y + correction_y,
            self.z + correction_z,
        )
        self._sync_cached_vectors()
        self._record_position_drift()

    def pack_input_flags(self) -> int:
        byte = 0
        if self.input.up:
            byte |= 0x01
        if self.input.down:
            byte |= 0x02
        if self.input.left:
            byte |= 0x04
        if self.input.right:
            byte |= 0x08
        if self.input.jump:
            byte |= 0x10
        if self.input.crouch:
            byte |= 0x20
        if self.input.sneak:
            byte |= 0x40
        if self.input.sprint:
            byte |= 0x80
        return byte

    def pack_action_flags(self) -> int:
        byte = 0
        if self.input.primary_fire:
            byte |= 0x01
        if self.input.secondary_fire:
            byte |= 0x02
        if self.input.zoom:
            byte |= 0x04
        if self.input.can_pickup:
            byte |= 0x08
        if self.input.can_display_weapon:
            byte |= 0x10
        if self.input.is_on_fire:
            byte |= 0x20
        if self.input.is_weapon_deployed:
            byte |= 0x40
        if self.input.hover:
            byte |= 0x80
        return byte

    def get_input_byte(self) -> int:
        return self.pack_input_flags()

    def get_action_byte(self) -> int:
        return self.pack_action_flags()

    def world_update_snapshot(self) -> Tuple[Tuple[float, float, float], ...]:
        return (
            self.position,
            self.orientation,
            self.velocity,
            0,
            0,
            self.health,
            self.pack_input_flags(),
            self.pack_action_flags(),
            self.tool,
        )

    def send(self, data: bytes, reliable: bool = True):
        if self.connection:
            self.connection.send(data, reliable)

    def send_packet(self, packet, reliable: bool = True):
        if not self.connection:
            return
        if hasattr(self.connection, "send_packet"):
            self.connection.send_packet(packet, reliable)
            return
        self.connection.send(bytes(packet.generate()), reliable)

    def disconnect(self, reason: int = 0):
        if self.connection:
            self.connection.disconnect(reason)

    def __repr__(self) -> str:
        return f"Player(id={self.id}, name='{self.name}', team={self.team})"
