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
    PLAYER_CROUCHING_POS_ABOVE_GROUND,
    PLAYER_STANDING_POS_ABOVE_GROUND,
    SPADE_PROFILE,
    SPADE_TOOL_IDS,
    WEAPON_PROFILES,
    WEAPON_TOOL_IDS,
)
from aoslib import world as native_world
from aoslib.world import Player as WorldPlayer

if TYPE_CHECKING:
    from .connection import Connection

logger = logging.getLogger(__name__)

# Per-jetpack-type fuel/behavior table from the client (keys 66-69; value
# indices: 0 start_delay, 1 max_fuel, 2 activation_cost, 3 refill_rate,
# 4 flying_consumption, 5 burdened_slowdown, 6 refill_delay_due_damage,
# 7 fall_damage_multiplier, 8 death_acceleration).
_JETPACK_PROPERTIES: dict = dict(getattr(C, "JETPACK_PROPERTIES", {}) or {})

JUMP_BUFFER_SECONDS = 0.25
POSITION_SAMPLE_FRESHNESS_SECONDS = 0.50

# The client stamps every ClientData with its loop_count. Measured live
# (2026-06-12 evening): the packet stamped N arrives while the server runs
# tick N — i.e. it RACES the tick boundary, and ~per-packet it is applied
# at tick N or N+1 nondeterministically. That jitter is what made self-row
# reconciliation yank the walking player by exactly one step (the client
# pairs our row against its history at the stamp; a coin-flip input lag
# cannot be compensated by any fixed stamp offset).
#
# INPUT_DELAY_TICKS=1 makes the pairing exact BY CONSTRUCTION: tick N
# simulates with input N-1 (always arrived), and the WorldUpdate stamp
# (loop_count - INPUT_DELAY_TICKS) labels the snapshot with the input tick
# actually used — our post-tick-N state equals the client's post-frame-
# (N-1) state bit-for-bit (same input sequence, same engine), so self-row
# corrections are zero-diff. Costs 16.7ms of server-side input latency,
# invisible under client prediction.
INPUT_DELAY_TICKS = 1
INPUT_HISTORY_LIMIT = 128
# Input consumption (see Player.simulate_tick): exactly one physics step per
# tick, applying the freshest buffered input — paced at real time so the server
# can never outrun the client (which is what caused the run-off-the-map / snap).
POSITION_DRIFT_DEADZONE = 0.6
POSITION_HARD_SNAP_THRESHOLD = 6.0
POSITION_SOFT_CORRECTION_RATE = 0.12
MAX_HORIZONTAL_SOFT_CORRECTION = 0.50
MAX_VERTICAL_SOFT_CORRECTION = 0.2
MAX_VERTICAL_SOFT_CORRECTION_DISTANCE = 0.75
WORLD_ORIENTATION_HORIZONTAL_EPSILON = 0.001
VELOCITY_ZERO_THRESHOLD = 0.0001
PLAYER_RADIUS = float(getattr(C, "PLAYER_RADIUS", 0.45))

# Movement authority for all players ("server" or "client"); set at startup
# from ServerConfig.movement_authority. In "client" mode the server pins each
# player's position to their latest fresh PositionData report so WorldUpdate
# echoes the client's own movement instead of fighting it with the (not yet
# parity-accurate) server simulation.
_MOVEMENT_AUTHORITY = "server"
CLIENT_AUTHORITY_FRESHNESS_SECONDS = 1.0


def set_movement_authority(value: str) -> None:
    global _MOVEMENT_AUTHORITY
    value = str(value).lower()
    if value not in ("server", "client"):
        raise ValueError(f"movement_authority must be 'server' or 'client', got {value!r}")
    _MOVEMENT_AUTHORITY = value


def get_movement_authority() -> str:
    return _MOVEMENT_AUTHORITY


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
    """Per-class movement+damage profile.

    Movement fields are sourced from server.class_data.MOVEMENT so the
    InitialInfo we send the client (built from the same module) and the
    server-side simulation use the same multipliers. Damage and starting
    blocks come from shared.constants directly.
    """
    from server.class_data import get_movement, get_damage

    starting_blocks, max_blocks = C.CLASS_BLOCKS.get(class_id, (MAX_BLOCKS, MAX_BLOCKS))
    m = get_movement(class_id)
    d = get_damage(class_id)
    return MovementProfile(
        starting_blocks=starting_blocks,
        max_blocks=max_blocks,
        accel_multiplier=m.accel_multiplier,
        sprint_multiplier=m.sprint_multiplier,
        jump_multiplier=m.jump_multiplier,
        crouch_sneak_multiplier=m.crouch_sneak_multiplier,
        can_sprint_uphill=m.can_sprint_uphill,
        water_friction=m.water_friction,
        damage_multiplier=d.damage_multiplier,
        headshot_damage_multiplier=d.headshot_multiplier,
        fall_on_water_damage_multiplier=m.fall_on_water_damage_multiplier,
        falling_damage_min_distance=m.falling_damage_min_distance,
        falling_damage_max_distance=m.falling_damage_max_distance,
        falling_damage_max_damage=m.falling_damage_max_damage,
    )


def _native_movement_overrides() -> dict[str, float]:
    try:
        overrides = native_world.get_debug_movement_overrides()
    except Exception:
        return {}
    if isinstance(overrides, dict):
        return overrides
    return {}


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
        self.god_mode: bool = False
        # Jetpack (per-class equipment; JETPACK_PROPERTIES keys 66-69).
        # Fuel model mirrors the client's local sim so hover reconciliation
        # stays close (constants extracted from the client 2026-07-07).
        self.jetpack_id: int = 0            # 0 / NO_JETPACK(65) = none
        self.jetpack_fuel: float = 100.0
        self.jetpack_active: bool = False
        self._hover_since: float = 0.0
        self._last_damage_at: float = 0.0
        self.disguised: bool = False        # specialist disguise toggle
        # Client-chosen loadout + prefab selection (SetClassLoadout / join).
        self.loadout: list = []
        self.prefabs: list = []
        # Mid-game class/loadout change, applied at the next respawn.
        self.pending_class_id = None
        self.pending_loadout = None
        self.grounded: bool = True
        self.airborne: bool = False
        self.wade: bool = False

        self.respawn_time: float = 0.0
        self.death_time: float = 0.0

        self.input = InputState()

        self.last_update: float = time.time()
        self.last_position_update: float = 0.0
        # loop_count -> (input flags tuple, orientation tuple); see
        # record_input_frame / apply_buffered_input.
        self.input_history: dict[int, tuple] = {}
        # Next client loop_count to consume (strict in-order cursor).
        self._input_cursor: Optional[int] = None
        self.last_reported_position: Optional[Tuple[float, float, float]] = None
        # The client loop_count of the input frame the simulation last
        # consumed — the ONLY correct stamp for this player's WorldUpdate
        # self-row (a fixed loop-derived stamp mislabels packets whenever
        # transit latency isn't exactly the local-machine value).
        self.last_applied_input_loop: Optional[int] = None
        self.last_position_drift: float = 0.0
        self.last_position_drift_vector: Tuple[float, float, float] = (0.0, 0.0, 0.0)
        self.last_fall_result: int = 0
        self.movement_time: float = 0.0
        self.jump_held: bool = False
        self.jump_last_held: bool = False
        self.pending_jump: bool = False
        self.jump_buffer_until: float = 0.0
        self.last_landed: bool = False
        self.last_step_delta: float = 0.0
        self.last_trigger_jump: bool = False
        self.last_buffered_jump_active: bool = False
        self.last_collision_count: int = 0
        self.last_collision_preview: list[tuple[float, float, float, float]] = []
        self.last_native_update_dt: float = 0.0
        self.last_native_result: int = 0
        self.last_native_pre_update: dict = {}
        self.last_native_post_update: dict = {}

        self.kills: int = 0
        self.deaths: int = 0
        self.captures: int = 0
        # Personal scoreboard number (the client's per-player column). Driven
        # by the mode via scoreboard.send_player_score on each scoring event.
        self.score: int = 0

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
        return self._current_contact_offset() + PLAYER_RADIUS

    def _current_contact_offset(self) -> float:
        overrides = _native_movement_overrides()
        if self.input.crouch and not self.wade:
            return float(
                overrides.get(
                    "crouching_pos_above_ground",
                    PLAYER_CROUCHING_POS_ABOVE_GROUND,
                )
            )
        return float(
            overrides.get(
                "standing_pos_above_ground",
                PLAYER_STANDING_POS_ABOVE_GROUND,
            )
        )

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
        # The client scales its accel/sprint/crouch multipliers by the
        # InitialInfo speed scale (wire-rounded); the server simulation must
        # use identical effective values or prediction drifts (rubber-band).
        from server.class_data import speed_scale
        scale = speed_scale(self._class_id)
        world_object.set_class_accel_multiplier(self.movement_profile.accel_multiplier * scale)
        world_object.set_class_sprint_multiplier(self.movement_profile.sprint_multiplier * scale)
        world_object.set_class_jump_multiplier(self.movement_profile.jump_multiplier)
        world_object.set_class_crouch_sneak_multiplier(self.movement_profile.crouch_sneak_multiplier * scale)
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
        # Jetpack: equipped flag + whether thrust is firing this tick (drives
        # the mover's 0.05x gravity + water-friction branch, same as the
        # client's local sim).
        try:
            world_object.jetpack = bool(self.jetpack_id in _JETPACK_PROPERTIES)
            world_object.jetpack_active = bool(self.jetpack_active)
        except Exception:
            pass
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

    def _capture_native_debug_state(
        self,
        world_object,
        phase: str,
        positions: list[tuple[float, float, float, float]] | None = None,
    ) -> dict:
        if world_object is None:
            return {}
        if positions is None:
            positions = []
        try:
            position = tuple(world_object.position)
        except Exception:
            position = (self.x, self.y, self.z)
        try:
            velocity = tuple(world_object.velocity)
        except Exception:
            velocity = (self.vx, self.vy, self.vz)
        try:
            orientation = tuple(world_object.orientation)
        except Exception:
            orientation = (self.o_x, self.o_y, self.o_z)
        try:
            side = tuple(world_object.s)
        except Exception:
            side = (self.side_x, self.side_y, self.side_z)
        preview = []
        for item in positions[:4]:
            try:
                preview.append(tuple(round(float(value), 4) for value in item[:4]))
            except Exception:
                continue
        return {
            "phase": phase,
            "position": tuple(round(float(value), 4) for value in position[:3]),
            "velocity": tuple(round(float(value), 4) for value in velocity[:3]),
            "orientation": tuple(round(float(value), 4) for value in orientation[:3]),
            "side": tuple(round(float(value), 4) for value in side[:3]),
            "airborne": bool(world_object.airborne),
            "grounded": not bool(world_object.airborne),
            "wade": bool(world_object.wade),
            "crouch": bool(self.input.crouch),
            "sprint": bool(self.input.sprint),
            "sneak": bool(self.input.sneak),
            "hover": bool(self.input.hover),
            "jump_held": bool(self.jump_held),
            "pending_jump": bool(self.pending_jump),
            "contact_offset": round(self._current_contact_offset(), 4),
            "height": round(self._current_height(), 4),
            "collision_count": len(positions),
            "collision_preview": preview,
        }

    def get_debug_movement_state(self) -> dict:
        return {
            "pre_update": dict(self.last_native_pre_update or {}),
            "post_update": dict(self.last_native_post_update or {}),
            "collision_count": int(self.last_collision_count),
            "collision_preview": list(self.last_collision_preview or []),
            "trigger_jump": bool(self.last_trigger_jump),
            "landed": bool(self.last_landed),
            "step_delta": round(float(self.last_step_delta), 4),
            "fall_result": int(self.last_fall_result),
            "native_result": int(self.last_native_result),
            "dt": round(float(self.last_native_update_dt), 6),
        }

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
        # Re-anchor the input cursor to the next inputs that arrive after
        # this spawn (stale pre-spawn inputs must not drive the new body).
        self.input_history = {}
        self._input_cursor = None
        self.last_applied_input_loop = None
        self.blocks = self.movement_profile.starting_blocks
        self.grenades = MAX_GRENADES
        self._reset_ammo()
        # Jetpack: the client's chosen loadout may carry a jetpack id (66-69)
        # in the equipment slot; otherwise fall back to the class default.
        jetpack = 0
        for item in (getattr(self, "loadout", None) or []):
            if int(item) in _JETPACK_PROPERTIES:
                jetpack = int(item)
                break
        if not jetpack:
            try:
                from server.class_data import get_loadout
                jetpack = int(get_loadout(self.class_id).jetpack)
            except Exception:
                jetpack = 0
        self.jetpack_id = jetpack if jetpack in _JETPACK_PROPERTIES else 0
        self.jetpack_fuel = 100.0
        self.jetpack_active = False
        self._hover_since = 0.0
        self.last_reported_position = (x, y, z)
        self.last_position_drift = 0.0
        self.last_position_drift_vector = (0.0, 0.0, 0.0)
        self.movement_time = 0.0
        self.last_fall_result = 0
        self.airborne = False
        self.wade = False
        self.grounded = True
        self.jump_held = False
        self.jump_last_held = False
        self.pending_jump = False
        self.jump_buffer_until = 0.0
        self.last_landed = False
        self.last_step_delta = 0.0
        self.last_trigger_jump = False
        self.last_buffered_jump_active = False
        self.last_collision_count = 0
        self.last_collision_preview = []
        self.last_native_update_dt = 0.0
        self.last_native_result = 0
        self.last_native_pre_update = {}
        self.last_native_post_update = {}
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
            # Per-tool melee stats (pickaxe 50 player/7 block, superspade
            # 50/7.5, knife 20/1, crowbar 80/5, ...) from the catalog; the
            # generic spade profile only as a fallback for unknown tools.
            from server.game_constants import WEAPON_CATALOG
            tool = self.tool if self.tool_is_raw else self.weapon
            profile = WEAPON_CATALOG.get(int(tool))
            if profile is not None and profile.is_melee:
                return profile
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
        if self.god_mode:
            return False

        # Pauses jetpack fuel regen for the type's refill-delay window.
        self._last_damage_at = time.time()

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
        # Idempotent per death: guard on `alive` alone. The old
        # `not alive and not spawned` let a death slip through for an
        # already-dead-but-still-spawned edge case (double death bookkeeping).
        if not self.alive:
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

            # Spawn a GRAVE entity where the player died (rendered until the
            # player respawns). Gated on the same entities_wire_ready flag the
            # crates use, since a bad Entity field crashes the compiled client.
            reg = getattr(server, "entity_registry", None)
            if reg is not None and getattr(server.config, "entities_wire_ready", False):
                try:
                    from server.game_constants import TEAM_NEUTRAL
                    from server.entities.behaviors import GraveBehavior
                    grave = reg.place(
                        int(getattr(C, "GRAVE_ENTITY", 11)),
                        self.x, self.y, self.z,
                        state=TEAM_NEUTRAL, kind="grave", player_id=self.id,
                        behavior=GraveBehavior(),
                    )
                    self._grave_entity_id = grave.entity_id
                    server.broadcast_create_entity(grave)
                except Exception:
                    logger.debug("grave spawn failed", exc_info=True)

        # Notify the active mode (drained next tick, never inline-async). Every
        # death fires on_player_death; a CROSS-TEAM kill by another player also
        # fires on_player_kill (the scoring hook). The team guard keeps
        # friendly-fire / environmental self-credit from awarding score even if
        # a future path passes a same-team killer; team-change deaths come
        # through with killer=None and so never score.
        if server is not None and getattr(server, "mode", None) is not None:
            server.queue_mode_event("on_player_death", self, killer, kill_type)
            if (
                killer is not None
                and killer is not self
                and killer.team != self.team
            ):
                server.queue_mode_event("on_player_kill", killer, self, kill_type)

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

    def restock_ammo(self, restock_type: int = 0):
        """Refill ammo reserve+clip (server-side) and tell the client to
        refill its own ammo counters/play the pickup sound via Restock(69).
        Ammo is client-authoritative for display, so the packet is required."""
        if not self.alive:
            return
        self._reset_ammo()
        if self.connection:
            from shared.packet import Restock
            pkt = Restock()
            pkt.player_id = self.id
            pkt.type = int(restock_type)
            self.connection.send(bytes(pkt.generate()))

    def add_blocks(self, count: int = 1):
        self.blocks = min(self.movement_profile.max_blocks, self.blocks + count)

    def restock_blocks(self):
        """Block-crate pickup: refill the block wallet to the class max and
        tell the client — Restock(69) with type=5 is the block refill (client
        sets its local block_count to max; measured live 2026-07-07)."""
        if not self.alive:
            return
        self.add_blocks(self.movement_profile.max_blocks)
        if self.connection:
            from shared.packet import Restock
            pkt = Restock()
            pkt.player_id = self.id
            pkt.type = 5
            self.connection.send(bytes(pkt.generate()))

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
        # Mirror the live client's input pipeline exactly (measured via the
        # in-game tracer): while the jump key is HELD, the Character sets
        # the world object's jump flag every frame the player is grounded —
        # no edge detection, no queue, no buffer. The buffered-jump-on-
        # landing behavior emerges naturally (key still held when landing).
        trigger_jump = bool(self.input.jump) and not bool(world_object.airborne)
        self.last_trigger_jump = bool(trigger_jump)
        if bool(self.input.jump):
            logger.info("JUMPDBG %s jump_in=1 airborne=%s trigger=%s z=%.3f vz=%.3f",
                        self.name, bool(world_object.airborne), trigger_jump,
                        float(self.z), float(getattr(world_object, 'velocity', type('x',(),{'z':0})).z))
        self.last_native_update_dt = float(dt)
        positions = self._build_player_collision_positions()
        self.last_collision_count = len(positions)
        self.last_collision_preview = [
            tuple(round(float(value), 4) for value in item[:4])
            for item in positions[:4]
        ]
        self.last_native_pre_update = self._capture_native_debug_state(
            world_object,
            "pre_update",
            positions,
        )
        pre_position = self.last_native_pre_update.get("position", (self.x, self.y, self.z))
        self._update_jetpack(dt)
        self._apply_input_state_to_world(trigger_jump=trigger_jump)
        result = world_object.update(dt, positions)
        self.last_native_result = int(result or 0)
        self.last_fall_result = int(result or 0)
        self._sync_cached_vectors()
        self._apply_client_authority_pin()
        self.last_landed = bool(was_airborne and self.grounded)
        self.last_step_delta = round(float(self.z - pre_position[2]), 4)
        self.last_native_post_update = self._capture_native_debug_state(
            world_object,
            "post_update",
            positions,
        )

    def _update_jetpack(self, dt: float) -> None:
        """Advance the jetpack fuel model one tick (constants from the client's
        JETPACK_PROPERTIES table, extracted 2026-07-07):
          - Z held (hover input): after START_DELAY, activate — pay the
            one-time ACTIVATION_COST, then drain FLYING_CONSUMPTION/s.
          - Fuel empty or Z released: thrust off.
          - Idle: regen REFILL_RATE/s, paused REFILL_DELAY_DUE_DAMAGE after
            taking damage.
        The active flag feeds the mover's 0.05x-gravity branch."""
        props = _JETPACK_PROPERTIES.get(self.jetpack_id)
        if props is None:
            self.jetpack_active = False
            return
        start_delay = float(props.get(0, 0.25))
        max_fuel = float(props.get(1, 100))
        activation_cost = float(props.get(2, 10))
        refill_rate = float(props.get(3, 10))
        drain = float(props.get(4, 75))
        refill_delay = float(props.get(6, 2.0))

        if self.input.hover:
            # dt-accumulated hold time (deterministic at the sim rate).
            self._hover_since += dt
            if not self.jetpack_active:
                if self._hover_since >= start_delay and self.jetpack_fuel >= max(activation_cost, 1.0):
                    self.jetpack_active = True
                    self.jetpack_fuel -= activation_cost
            if self.jetpack_active:
                self.jetpack_fuel -= drain * dt
                if self.jetpack_fuel <= 0.0:
                    self.jetpack_fuel = 0.0
                    self.jetpack_active = False
        else:
            self._hover_since = 0.0
            self.jetpack_active = False

        if not self.jetpack_active and self.jetpack_fuel < max_fuel:
            if (time.time() - self._last_damage_at) >= refill_delay:
                self.jetpack_fuel = min(max_fuel, self.jetpack_fuel + refill_rate * dt)

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
        # No jump edge-detection or queuing here: the held jump flag is
        # consumed directly each tick in update() (client-pipeline mirror).

    def record_input_frame(
        self,
        loop_count: int,
        flags: tuple,
        orientation: tuple,
    ) -> None:
        """Store the movement inputs the client used for its frame
        `loop_count` so the simulation can apply them at the matching
        (delayed) server tick."""
        self.input_history[int(loop_count)] = (flags, orientation)
        if len(self.input_history) > INPUT_HISTORY_LIMIT:
            for key in sorted(self.input_history)[:-INPUT_HISTORY_LIMIT]:
                del self.input_history[key]

    async def simulate_tick(self, dt: float) -> None:
        """Advance this player's simulation by exactly ONE physics step.

        Authoritative-server movement, simplest correct form: apply the
        FRESHEST buffered client input (one physics step), drop the rest,
        and always step once. One step per tick means the server advances at
        real time and CANNOT outrun the client; applying the newest input
        keeps it current with the client's latest direction (intermediate
        inputs during a continuous walk carry the same direction, so dropping
        them costs nothing). The self-row is stamped with last_applied_input_loop
        so the client still pairs it to the matching history frame.

        (Earlier experiments that consumed multiple inputs per tick made the
        server sprint multiple-times real-time and run off the map, which is
        what reconciled the client back toward spawn on every jump.)
        """
        if not self.alive or not self.spawned:
            return

        if self.input_history:
            best = max(self.input_history)
            flags, orientation = self.input_history[best]
            # Movement direction + orientation come from the FRESHEST frame,
            # but JUMP is an EDGE (the key is held only ~1-2 client frames).
            # Picking only the freshest frame drops a jump whose press landed
            # in an earlier buffered frame — the reason human jumps never
            # registered server-side while held walk always did. Latch jump
            # (index 4) if ANY buffered frame this tick had it.
            if not flags[4] and any(f[0][4] for f in self.input_history.values()):
                flags = flags[:4] + (True,) + flags[5:]
            self.last_applied_input_loop = best
            self.set_orientation_vector(*orientation)
            self.update_input(*flags)
            self.input_history.clear()

        await self.update(dt)

    def _tick_idle(self) -> None:
        """Per-tick housekeeping on a held frame (no physics step)."""
        if self.reloading and time.monotonic() >= self.reload_end_time:
            if self.finish_reload():
                self._broadcast_reload_state(True)

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

    def _apply_client_authority_pin(self):
        if _MOVEMENT_AUTHORITY != "client":
            return
        if self._world_object is None or self.last_reported_position is None:
            return
        if self.last_position_update <= 0.0:
            return
        if (time.time() - self.last_position_update) > CLIENT_AUTHORITY_FRESHNESS_SECONDS:
            # Stale report (client paused/lagging): let the simulation free-run
            # rather than freezing the player at an old position.
            return
        self._world_object.set_position(*self.last_reported_position)
        self._sync_cached_vectors()
        self._record_position_drift()

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
        # WorldUpdate action byte. Only VERIFIED-safe display bits are emitted.
        # MEASURED client-side meaning of this byte: 0x20=is_on_fire, 0x40=zoom,
        # 0x80=is_weapon_deployed (0x01/0x02 = fire/muzzle).
        #
        # DO NOT set 0x04 / 0x08 / 0x10. PROVEN (decompiled world.pyd +
        # gameScene, 2026-07-07): the client reads one of these as
        # jetpack_passive, which makes its physics SKIP GRAVITY entirely — the
        # player floats at rest, `airborne` latches True forever, and the local
        # jump impulse (gated on !airborne) can never fire. With self-rows on
        # (worldupdate_include_self=true) the client's always-on ClientData bits
        # can_pickup(0x08)/can_display_weapon(0x10) were echoed straight back
        # into this byte -> the client permanently believed it was jetpacking
        # -> "jump stuck, thinks we're in air". Re-map 0x04/0x08/0x10 with the
        # marker-byte replay method before ever emitting them again.
        byte = 0
        if self.input.primary_fire:
            byte |= 0x01
        if self.input.secondary_fire:
            byte |= 0x02
        if self.input.is_on_fire:
            byte |= 0x20
        if self.input.zoom:
            byte |= 0x40
        if self.input.is_weapon_deployed:
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
