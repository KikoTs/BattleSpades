"""
Player entity for BattleSpades.
Represents a connected player with position, health, inventory, and input state.
"""

from __future__ import annotations

import logging
import math
import time
from collections import deque
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
# ClientData carries no jetpack-active acknowledgement. Two deferred physics
# recurrences are the best local-scheduling estimate recovered from retail
# captures, not a delivery/application proof. ReplicationService owns the
# bounded no-correction handoff that makes this estimate safe under jitter.
JETPACK_ACTIVATION_DEFER_FRAMES = 2

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
PENDING_VELOCITY_IMPULSE_LIMIT = 64
OWNER_ANCHOR_HISTORY_LIMIT = 128
# The patched retail client keeps its current predicted position when a
# grounded jump would otherwise restore an owner row more than this distance
# away.  Character.update_alive's stock restore is useful for small phase
# corrections, but a stale airborne row can move a buffered re-jump by more
# than a whole voxel.  Keep this value byte-for-byte in sync with
# ``aoslib/character_jump_smoothing.py`` in the maintained client.
JUMP_ANCHOR_TELEPORT_GUARD_DISTANCE = 0.25
JUMP_ANCHOR_TELEPORT_GUARD_DISTANCE_SQ = (
    JUMP_ANCHOR_TELEPORT_GUARD_DISTANCE ** 2
)
IDLE_INPUT_FLAGS = (False, False, False, False, False, False, False, False)
# Slack on the server-side fire-rate gate: one 60Hz sim tick (~16.7ms).
FIRE_RATE_GRACE = 1.0 / 60.0
# Input consumption (see Player.simulate_tick): at most one physics step per
# tick, paced so the server can never outrun the client.
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


@dataclass(frozen=True)
class BufferedInputFrame:
    """Complete movement-relevant state carried by one ClientData loop.

    Network draining may receive several ClientData packets before a single
    physics step.  Keeping action state beside movement prevents a future
    packet's hover/jetpack bit from leaking into an older replayed frame.
    Tool selection remains immediate for combat packet authorization, but all
    state consumed by movement is applied transactionally here.
    """

    movement_flags: tuple
    orientation: tuple
    action_flags: tuple | None = None
    # Server simulation tick on which ENet delivered this ClientData. This is
    # transport phase metadata only; physics continues to use fixed 60 Hz dt.
    received_server_tick: int | None = None
    # Total order shared with owner WorldUpdate sends on the gameplay thread.
    # Unlike a 60 Hz tick label, this distinguishes send-before-receive from
    # receive-before-send when both events happen inside the same tick.
    received_owner_sequence: int | None = None
    # Monotonic count of accepted ClientData frames for this player only.
    # Unlike loop_count it cannot skip, and unlike owner_sequence it excludes
    # interleaved WorldUpdate sends.  Deferred client-predicted effects use it
    # as their application witness.
    received_input_sequence: int = 0
    # Unnamed ClientData byte between orientation and movement flags. Retained
    # losslessly for protocol analysis; no gameplay semantics are assumed here.
    wire_unknown_byte: int | None = None


@dataclass(frozen=True)
class PendingExplosionImpulse:
    """Explosion parameters waiting for a future observed ClientData frame."""

    target_input_sequence: int
    origin: tuple[float, float, float]
    blast_radius: float
    knockback_min: float
    knockback_max: float


@dataclass(frozen=True)
class OwnerAnchor:
    """One exact local WorldUpdate row queued for a retail owner.

    The local-player path calls Character with ``force_update=True``.  Retail
    therefore accepts repeated ``stamp`` values and replaces both cached
    position and velocity each time; every send must remain in this history.
    ``queued_owner_sequence`` gives sends and ClientData receives one causal
    order even when their coarse server tick is equal.
    """

    stamp: int
    position: Tuple[float, float, float]
    velocity: Tuple[float, float, float]
    queued_server_tick: int | None
    queued_owner_sequence: int


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
        # Character.update_alive restores all three coordinates to its cached
        # network_position on a jump_this_frame. This is the newest row queued
        # by the server, not proof the retail event loop consumed that row.
        self.last_advertised_owner_position = (0.0, 0.0, 0.0)
        self._spawn_owner_anchor = (0.0, 0.0, 0.0)
        # Ordered exact rows queued in that owner's self WorldUpdates.
        # A launch cannot use a row carrying its own input stamp: the server
        # can only queue that row after retail has already simulated the frame.
        # Duplicate stamps are retained: retail force-applies local rows.
        self._owner_anchor_history: deque[OwnerAnchor] = deque(
            maxlen=OWNER_ANCHOR_HISTORY_LIMIT
        )
        self._owner_timeline_sequence: int = 0
        self._input_receive_sequence: int = 0
        self._current_input_receive_sequence: int = 0
        self._current_input_owner_sequence: Optional[int] = None
        self.last_jetpack_transition_debug: dict = {}
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
        # Retail Block.reset() starts at neutral (112,112,112). Cyan was an
        # accidental server fallback that overrode the held block before the
        # client palette could publish its actual selection.
        self.block_color: int = 0x707070

        self.ammo_clip: int = 10
        self.ammo_reserve: int = 50
        self.rocket_turret_stock: int = int(
            getattr(C, "ROCKET_TURRET_INITIAL_STOCK", 2)
        )
        # Oriented items are locally animated and locally decrement their HUD
        # ammo, but the server must keep an independent wallet.  Otherwise a
        # duplicated/forged UseOrientedItem can create unlimited rockets or
        # special grenades even though the retail client is empty.
        self.oriented_stock: dict[int, int] = {}
        self._oriented_next_use: dict[int, float] = {}
        self.disguise_stock: int = 0
        self._disguise_next_use: float = 0.0
        self._reset_equipment_state()
        self.last_shot_time: float = 0.0
        self.next_shot_time: float = 0.0
        self.reload_end_time: float = 0.0
        self.reloading: bool = False

        self.spawned: bool = False
        self.alive: bool = False
        # server.roster combines this with Player object identity to identify
        # one concrete native Character life across gated join transitions.
        self.replication_generation: int = 0
        self.last_kill_action_data: bytes | None = None
        self.admin: bool = False
        self.muted: bool = False
        self.god_mode: bool = False
        self.is_bot: bool = False
        # Jetpack (per-class equipment; JETPACK_PROPERTIES keys 66-69).
        # Fuel model mirrors the client's local sim so hover reconciliation
        # stays close (constants extracted from the client 2026-07-07).
        self.jetpack_id: int = 0            # 0 / NO_JETPACK(65) = none
        self.jetpack_fuel: float = 100.0
        # ``jetpack_active`` is the state advertised to the retail owner in
        # WorldUpdate action bit 0x04. Native activation uses a two-recurrence
        # local estimate; retail provides no packet that proves GameScene has
        # applied the transition.
        self.jetpack_active: bool = False
        self._jetpack_physics_active: bool = False
        self._jetpack_activation_defer_remaining: int = 0
        # Exhaustion finishes the current active recurrence and one already
        # predicted recurrence before ordinary held-SPACE movement resumes.
        self._jetpack_exhaustion_tail_remaining: int = 0
        self._jetpack_requires_release: bool = False
        self._hover_since: float = 0.0
        self._last_damage_at: float = 0.0
        self._last_combat_damage_at: float = 0.0
        self._last_damage_source_id: int = -1
        self._last_damage_source_position = None
        self.parachute_id: int = 0
        self.parachute_active: bool = False
        self.disguised: bool = False        # specialist disguise toggle
        self.mounted_entity_id = None        # mounted MACHINE_GUN entity, if any
        self.on_fire: bool = False          # authoritative Molotov burn state
        self.pickup_id = None               # objective entity type 14/15/16
        self.pickup_burdensome = False
        self.pickup_state = None             # owning/team state restored on drop
        # Client-chosen loadout + prefab selection (SetClassLoadout / join).
        self.loadout: list[int] = []
        self.prefabs: list[str] = []
        self.ugc_tools: list[int] = []
        # One complete mid-game selection is the source of truth.  The two
        # legacy fields remain synchronized temporarily for old modes/tests;
        # new code must stage/apply through the methods below.
        self.pending_selection = None
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
        self.position_reports_received: int = 0
        # loop_count -> (input flags tuple, orientation tuple); see
        # record_input_frame / apply_buffered_input.
        self.input_history: dict[int, BufferedInputFrame] = {}
        # Server-origin impulses are labeled with the retail/client loop they
        # are predicted to enter. Damage(37) can reach the client several
        # frames after impact detection; applying knockback to the server's
        # older consumed input state creates a full-impulse reconciliation.
        self._pending_velocity_impulses: deque[
            tuple[int, tuple[float, float, float]]
        ] = deque()
        self._pending_explosion_impulses: deque[
            PendingExplosionImpulse
        ] = deque()
        # Last state actually stepped by authoritative physics. ClientData is
        # also applied immediately for combat/tool responsiveness, so
        # self.input may be newer than the movement cursor and must never be
        # used to backfill an older missing movement frame.
        self._applied_input_flags: Optional[tuple] = None
        self._applied_orientation: Optional[tuple] = None
        # ClientData buttons are held for the next observed frame. Foreground
        # jump A/B testing is slightly quieter with this latch; orientation is
        # deliberately current because native-yaw capture resolves it earlier.
        self._pending_packet_flags: tuple = IDLE_INPUT_FLAGS
        self._pending_packet_loop: Optional[int] = None
        self._applied_input_source_loop: Optional[int] = None
        self._pending_packet_received_server_tick: Optional[int] = None
        self._applied_input_source_server_tick: Optional[int] = None
        self._pending_packet_received_owner_sequence: Optional[int] = None
        self._applied_input_source_owner_sequence: Optional[int] = None
        self._pending_packet_wire_unknown_byte: Optional[int] = None
        self._applied_input_source_wire_unknown_byte: Optional[int] = None
        # Telemetry separates harmless stale/duplicate packets from actual
        # history-capacity loss.  ``input_frames_dropped`` remains the aggregate
        # compatibility counter consumed by existing dashboards.
        self.input_frames_dropped: int = 0
        self.input_frames_stale: int = 0
        self.input_frames_overflow: int = 0
        self.input_frames_applied: int = 0
        self.input_starved_ticks: int = 0
        self.last_reported_position: Optional[Tuple[float, float, float]] = None
        # The client loop_count of the input frame the simulation last
        # consumed — the ONLY correct stamp for this player's WorldUpdate
        # self-row (a fixed loop-derived stamp mislabels packets whenever
        # transit latency isn't exactly the local-machine value).
        self.last_applied_input_loop: Optional[int] = None
        # The value written into this player's WorldUpdate row `pong` field:
        # the client loop_count the server has consumed for them (+ the
        # calibration offset). The 1.x client feeds this to
        # set_network_position_and_velocity(..., last_loop_count, ...) and looks
        # its OWN movement_history up by it. Sending 0 (the old behaviour) means
        # the client's dedupe `network_position_loop_count == last_loop_count`
        # matches on every packet, so reconciliation never runs at all.
        self.wu_ack_loop: int = 0
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
        # Current-life streak sent in KillAction.kill_count. Scoreboard kills
        # remain cumulative for the match, but the native multikill HUD resets
        # this value when the killer dies.
        self.kill_streak: int = 0
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
        """Build the native mover's player-contact snapshot for this tick.

        InitialInfo exposes only a same-team collision switch; enemy collision
        remains part of stock movement.  Filtering allies here when that switch
        is disabled is therefore a protocol invariant, not an optimization.
        This runs on the single gameplay tick before ``WorldPlayer.update``.
        """
        server = self.connection.server if self.connection else None
        if server is None:
            return []
        players = getattr(server, "players", None)
        if not players:
            return []

        positions = []
        same_team_collision = bool(getattr(
            getattr(server, "config", None), "same_team_collision", False
        ))
        for player in players.values():
            if player is self or not player.alive or not player.spawned:
                continue
            if player.team == self.team and not same_team_collision:
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

    def _apply_input_state_to_world(
        self, trigger_jump: bool, world_object=None, collisions=None
    ):
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
        # Retail writes the held SPACE state every frame.  Ordinary airborne
        # requests are consumed as no-ops by world.Player, while an active
        # normal/Rocketeer/Engineer jetpack uses the held request as sustained
        # thrust.  Gating this to the grounded jump edge breaks flight.
        world_object.jump = bool(self.input.jump)
        world_object.sneak = self.input.sneak
        world_object.sprint = self.input.sprint
        world_object.hover = self.input.hover
        world_object.burdened = bool(self.pickup_burdensome)
        # Jetpack: concrete pack id + whether thrust is firing this tick.  The
        # stock mover applies pack-specific SPACE thrust and its high-friction
        # movement branch; active packs still receive ordinary gravity.  The
        # separate passive flag is the 0.75-gravity mode.
        try:
            world_object.jetpack = int(
                self.jetpack_id
                if self.jetpack_id in _JETPACK_PROPERTIES
                else C.NO_JETPACK
            )
            world_object.jetpack_active = bool(
                self._jetpack_physics_active
            )
            # Passive flight is a separate 0.75-gravity mode.  The stock
            # Engineer keeps it false while ordinary SPACE thrust is active.
            world_object.jetpack_passive = False
            world_object.parachute = int(self.parachute_id or 0)
            world_object.parachute_active = bool(self.parachute_active)
        except Exception:
            pass
        if collisions is None:
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
            # Transition-only causal evidence. These counters are bounded
            # scalars; full input/anchor histories remain out of telemetry.
            "input_receive_sequence": int(self._input_receive_sequence),
            "current_input_receive_sequence": int(
                self._current_input_receive_sequence
            ),
            "current_input_owner_sequence": self._current_input_owner_sequence,
            "input_history_depth": len(self.input_history),
            "applied_input_source_loop": self._applied_input_source_loop,
            "jetpack_active": bool(self.jetpack_active),
            "jetpack_physics_active": bool(self._jetpack_physics_active),
            "jetpack_activation_defer_remaining": int(
                self._jetpack_activation_defer_remaining
            ),
            "jetpack_exhaustion_tail_remaining": int(
                self._jetpack_exhaustion_tail_remaining
            ),
            "jetpack_transition": dict(self.last_jetpack_transition_debug),
        }

    def note_jetpack_transition_sent(self, active: bool, stamp: int) -> None:
        """Record the causal boundary of one owner transition row.

        Called by ``ReplicationService`` only after ``Connection.send`` queued
        the reliable owner WorldUpdate. Input frames already accepted at this
        point cannot prove that retail had applied the row. The bounded record
        is exposed only through opt-in parity snapshots; it performs no I/O.
        """
        anchor_sequence = None
        if self._owner_anchor_history:
            anchor_sequence = int(
                self._owner_anchor_history[-1].queued_owner_sequence
            )
        self.last_jetpack_transition_debug = {
            "active": bool(active),
            "stamp": int(stamp),
            "sent_input_receive_sequence": int(self._input_receive_sequence),
            "sent_owner_sequence": anchor_sequence,
            "buffered_input_count": len(self.input_history),
            "server_loop": int(getattr(
                getattr(self.connection, "server", None), "loop_count", 0
            )),
        }

    def _note_jetpack_physics_started(self) -> None:
        """Persist the exact consumed frame that first applied active thrust.

        Parity sampling is intentionally capped at 10 Hz, while the activation
        handoff lasts only a few 60 Hz recurrences.  Store one bounded scalar
        event beside the transition metadata so a validation run can recover
        the exact boundary without per-frame logging or gameplay-thread I/O.
        """
        transition = self.last_jetpack_transition_debug
        if (
            not transition
            or not transition.get("active")
            or "physics_started_input_receive_sequence" in transition
        ):
            return
        transition.update({
            "physics_started_input_receive_sequence": int(
                self._current_input_receive_sequence
            ),
            "physics_started_owner_sequence": self._current_input_owner_sequence,
            "physics_started_source_loop": self._applied_input_source_loop,
            "physics_started_server_loop": int(getattr(
                getattr(self.connection, "server", None), "loop_count", 0
            )),
        })

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

    def stage_class_selection(self, selection) -> None:
        """Stage a validated selection for the next life as one value.

        This method runs on the gameplay thread.  It deliberately does not
        mutate the active class or inventory, because the current body must
        remain internally consistent until death/respawn commits the choice.
        """

        from server.class_selection import ClassSelection

        if not isinstance(selection, ClassSelection):
            raise TypeError("selection must be a ClassSelection")
        self.pending_selection = selection
        # Compatibility mirrors for code being migrated to pending_selection.
        self.pending_class_id = int(selection.class_id)
        self.pending_loadout = list(selection.loadout)

    def apply_class_selection(self, selection) -> None:
        """Commit class, tools, prefab choices, and UGC tools atomically."""

        from server.class_selection import ClassSelection

        if not isinstance(selection, ClassSelection):
            raise TypeError("selection must be a ClassSelection")
        self.class_id = int(selection.class_id)
        self.loadout = list(selection.loadout)
        self.prefabs = list(selection.prefabs)
        self.ugc_tools = list(selection.ugc_tools)

    def apply_pending_selection(self) -> bool:
        """Commit the staged selection at a spawn boundary.

        Returns ``True`` when a selection was applied.  The legacy-field
        fallback keeps existing game modes safe while RoundLifecycle call
        sites migrate; it still normalizes the pair before committing it.
        """

        selection = self.pending_selection
        if selection is None and self.pending_class_id is not None:
            from server.class_selection import normalize_class_selection

            selection = normalize_class_selection(
                self.pending_class_id,
                self.pending_loadout or (),
                self.prefabs,
                self.ugc_tools,
                fallback_class_id=self.class_id,
            )
        if selection is None:
            return False
        self.apply_class_selection(selection)
        self.pending_selection = None
        self.pending_class_id = None
        self.pending_loadout = None
        return True

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
        # A new retail Character must not inherit self-row cadence or jetpack
        # transition state from its previous life. Player remains a temporary
        # compatibility facade for legacy mode spawn paths, so reset the
        # replication service here until every mode delegates to RoundLifecycle.
        server = self.connection.server if self.connection else None
        replication = getattr(server, "replication", None)
        forget_player = getattr(replication, "forget_player", None)
        if callable(forget_player):
            forget_player(self.id)
        self.replication_generation += 1
        self.last_kill_action_data = None
        self.health = MAX_HEALTH
        self.alive = True
        self.spawned = True
        self.input = InputState()
        # Retail owners repopulate this from their first ClientData. Peerless
        # bots have no such packet, so their first post-spawn WorldUpdate must
        # already expose the equipped model to observers.
        if self.is_bot:
            self.input.can_display_weapon = True
        # Re-anchor the input cursor to the next inputs that arrive after
        # this spawn (stale pre-spawn inputs must not drive the new body).
        self.input_history = {}
        self._pending_velocity_impulses.clear()
        self._pending_explosion_impulses.clear()
        self._input_receive_sequence = 0
        self._current_input_receive_sequence = 0
        self._current_input_owner_sequence = None
        self.last_jetpack_transition_debug = {}
        self._applied_input_flags = None
        self._applied_orientation = None
        self._pending_packet_flags = IDLE_INPUT_FLAGS
        self._pending_packet_loop = None
        self._applied_input_source_loop = None
        self._pending_packet_received_server_tick = None
        self._applied_input_source_server_tick = None
        self._pending_packet_received_owner_sequence = None
        self._applied_input_source_owner_sequence = None
        self._pending_packet_wire_unknown_byte = None
        self._applied_input_source_wire_unknown_byte = None
        self.last_applied_input_loop = None
        self.blocks = self.movement_profile.starting_blocks
        self.grenades = MAX_GRENADES
        self._reset_ammo()
        self.rocket_turret_stock = int(
            getattr(C, "ROCKET_TURRET_INITIAL_STOCK", 2)
        )
        self._reset_equipment_state()
        # Jetpacks are concrete equipment-slot choices. Never infer one from
        # class_id here: Engineer can choose Disguise instead, and appending a
        # fallback pack would overlap two mutually exclusive native states.
        jetpack = 0
        for item in (getattr(self, "loadout", None) or []):
            if int(item) in _JETPACK_PROPERTIES:
                jetpack = int(item)
                break
        self.jetpack_id = jetpack if jetpack in _JETPACK_PROPERTIES else 0
        self.jetpack_fuel = 100.0
        self.jetpack_active = False
        self._jetpack_physics_active = False
        self._jetpack_activation_defer_remaining = 0
        self._jetpack_exhaustion_tail_remaining = 0
        self._jetpack_requires_release = False
        self._hover_since = 0.0
        self.parachute_id = (
            int(C.A370)
            if int(C.A370) in [int(item) for item in (getattr(self, "loadout", None) or [])]
            else 0
        )
        self.parachute_active = False
        self.disguised = False
        self.on_fire = False
        self.pickup_id = None
        self.pickup_burdensome = False
        self.pickup_state = None
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
        self.next_shot_time = 0.0
        self.reload_end_time = 0.0
        self.reloading = False

        self.x = x
        self.y = y
        self.z = z
        self.last_advertised_owner_position = (x, y, z)
        self._spawn_owner_anchor = (x, y, z)
        self._owner_anchor_history = deque(
            maxlen=OWNER_ANCHOR_HISTORY_LIMIT
        )
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

    def can_fire(
        self,
        now: Optional[float] = None,
        fire_interval: Optional[float] = None,
    ) -> bool:
        if not self.alive or not self.spawned:
            return False

        profile = self.get_weapon_profile()
        current_time = time.monotonic() if now is None else now
        if self.reloading and current_time < self.reload_end_time:
            return False
        # Admit up to one tick of arrival jitter against a stable cadence
        # schedule. The grace is never subtracted from every accepted interval,
        # which would permanently raise the weapon's sustained fire rate.
        if current_time + FIRE_RATE_GRACE < self.next_shot_time:
            return False

        if self.is_spade_tool():
            return True
        return self.is_weapon_tool() and self.ammo_clip > 0

    def consume_shot(
        self,
        now: Optional[float] = None,
        fire_interval: Optional[float] = None,
    ) -> bool:
        if not self.can_fire(now, fire_interval=fire_interval):
            return False

        current_time = time.monotonic() if now is None else now
        profile = self.get_weapon_profile()
        interval = profile.fire_interval if fire_interval is None else float(fire_interval)
        previous_due = self.next_shot_time
        self.last_shot_time = current_time
        if previous_due <= 0.0:
            self.next_shot_time = current_time + interval
        elif current_time < previous_due:
            self.next_shot_time = previous_due + interval
        else:
            self.next_shot_time = current_time + interval
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

        server = self.connection.server if self.connection else None
        modify_damage = getattr(
            getattr(server, "mode", None), "modify_incoming_damage", None
        )
        if callable(modify_damage):
            amount = modify_damage(self, amount, source, kill_type)

        # Pauses jetpack fuel regen for the type's refill-delay window.
        self._last_damage_at = time.time()

        amount = max(0, int(round(amount)))
        if amount <= 0:
            return False

        self.health = max(0, self.health - amount)
        source_position = self.position if source is None else source.position
        self._last_combat_damage_at = time.monotonic()
        self._last_damage_source_id = int(
            getattr(source, "id", -1) if source is not None else -1
        )
        self._last_damage_source_position = tuple(
            float(value) for value in source_position
        )
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
        self.disguised = False
        self.death_time = time.time()
        self.deaths += 1
        self.kill_streak = 0
        self.reloading = False
        self.reload_end_time = 0.0
        self.jetpack_active = False
        self._jetpack_physics_active = False
        self._jetpack_activation_defer_remaining = 0
        self._jetpack_exhaustion_tail_remaining = 0
        self._jetpack_requires_release = False
        self._pending_velocity_impulses.clear()
        self._pending_explosion_impulses.clear()

        world_object = self._ensure_world_object()
        if world_object is not None:
            world_object.set_dead(True)
            self._sync_cached_vectors()

        kill_count = 0
        if killer and killer != self:
            killer.kills += 1
            killer.kill_streak = min(255, int(killer.kill_streak) + 1)
            kill_count = killer.kill_streak

        server = self.connection.server if self.connection else None
        if server is not None:
            from shared.packet import KillAction

            packet = KillAction()
            packet.player_id = self.id
            packet.killer_id = killer.id if killer is not None else self.id
            packet.kill_type = kill_type
            respawn_time_for = getattr(
                getattr(server, "mode", None), "respawn_time_for", None
            )
            respawn_time = (
                respawn_time_for(self)
                if callable(respawn_time_for)
                else server.config.respawn_time
            )
            packet.respawn_time = max(0, min(255, int(respawn_time)))
            packet.kill_count = kill_count
            packet.isDominationKill = 0
            packet.isRevengeKill = 0
            self.last_kill_action_data = bytes(packet.generate())
            server.broadcast(self.last_kill_action_data)

            # Spawn the stock team-coloured grave on the supporting surface.
            # GraveEntity is a moving client object; feeding it the player's
            # eye/body Z made it fall and tumble while the death camera tracked
            # it.  A stable surface anchor restores the intended mouse-orbit
            # camera.  Its delayed explosion is server-authoritative via
            # GraveBehavior and intentionally outlives the 5s respawn timer.
            reg = getattr(server, "entity_registry", None)
            if reg is not None and getattr(server.config, "entities_wire_ready", False):
                try:
                    from server.entities.behaviors import GraveBehavior
                    world = getattr(server, "world_manager", None)
                    grave_x, grave_y, grave_z = self.x, self.y, self.z
                    if world is not None:
                        grave_x, grave_y, grave_z = world.dry_surface_anchor(
                            self.x, self.y, search=0
                        )
                    team = server.teams.get(self.team)
                    grave_color = tuple(team.color) if team is not None else None
                    grave = reg.place(
                        int(getattr(C, "GRAVE_ENTITY", 11)),
                        grave_x, grave_y, grave_z,
                        state=int(self.team), color=grave_color,
                        kind="grave", player_id=self.id,
                        behavior=GraveBehavior(
                            thrower_id=self.id,
                            fuse=float(getattr(C, "GRAVE_EXPLOSION_FUSE", 7.0)),
                            damage=float(getattr(C, "GRAVE_EXPLOSION_DAMAGE", 25.0)),
                            block_damage=float(getattr(C, "GRAVE_EXPLOSION_BLOCK_DAMAGE", 3.0)),
                            blast_radius=float(getattr(C, "GRAVE_EXPLOSION_RADIUS", 3.0)),
                            kill_type=int(getattr(C.KILL, "GRAVE_KILL", 13)),
                        ),
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
        self.rocket_turret_stock = min(
            int(getattr(C, "ROCKET_TURRET_STOCK", 4)),
            int(getattr(self, "rocket_turret_stock", 0))
            + int(getattr(C, "ROCKET_TURRET_RESTOCK_AMOUNT", 2)),
        )
        # A stock ammo crate calls restock() on every equipped weapon/tool in
        # the client. Mirror that complete reset, including late Battle Builder
        # projectile weapons and Disguise, rather than only the primary gun.
        self._reset_equipment_state()
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

    def restock_jetpack(self):
        """Jetpack-crate refill (entity/type 6 through Restock packet 69)."""
        if not self.alive or self.jetpack_id not in _JETPACK_PROPERTIES:
            return
        self.jetpack_fuel = float(_JETPACK_PROPERTIES[self.jetpack_id].get(1, 100.0))
        if self.connection:
            from shared.packet import Restock
            pkt = Restock()
            pkt.player_id = self.id
            pkt.type = int(C.JETPACK_CRATE)
            self.connection.send(bytes(pkt.generate()))

    def remove_block(self) -> bool:
        if self.blocks > 0:
            self.blocks -= 1
            return True
        return False

    def _reset_equipment_state(self) -> None:
        """Reset per-life oriented-tool ammo and cadence.

        Counts below are the retail spawn values: throwable tools expose an
        ``initial_count``; launcher weapons expose one loaded round plus their
        initial reserve. Snowblower is deliberately absent because it consumes
        the player's shared block wallet instead of weapon ammo.
        """
        def total(clip_name: str, reserve_name: str, clip_default: int,
                  reserve_default: int) -> int:
            return int(getattr(C, clip_name, clip_default)) + int(
                getattr(C, reserve_name, reserve_default)
            )

        self.oriented_stock = {
            int(C.GRENADE_TOOL): int(getattr(C, "GRENADE_INITIAL_STOCK", 2)),
            int(getattr(C, "CLASSIC_GRENADE_TOOL", 31)): int(
                getattr(C, "CLASSIC_GRENADE_INITIAL_STOCK", 2)
            ),
            int(getattr(C, "ANTIPERSONNEL_GRENADE_TOOL", 32)): int(
                getattr(C, "ANTIPERSONNEL_GRENADE_INITIAL_STOCK", 2)
            ),
            int(getattr(C, "MOLOTOV_TOOL", 33)): int(
                getattr(C, "MOLOTOV_INITIAL_STOCK", 3)
            ),
            int(C.RPG_TOOL): total(
                "RPG_AMMO_CLIP_SIZE", "RPG_AMMO_INITIAL_STOCK", 1, 3
            ),
            int(C.RPG2_TOOL): total(
                "RPG2_AMMO_CLIP_SIZE", "RPG2_AMMO_INITIAL_STOCK", 3, 3
            ),
            int(C.DRILLGUN_TOOL): total(
                "DRILLGUN_AMMO_CLIP_SIZE", "DRILLGUN_AMMO_INITIAL_STOCK", 1, 1
            ),
            int(getattr(C, "CHEMICALBOMB_TOOL", 54)): 2,
            int(getattr(C, "GRENADE_LAUNCHER_WEAPON_TOOL", 55)): total(
                "GRENADE_LAUNCHER_AMMO_CLIP_SIZE",
                "GRENADE_LAUNCHER_AMMO_INITIAL_STOCK",
                1,
                3,
            ),
            int(getattr(C, "STICKY_GRENADE_TOOL", 57)): 2,
            int(getattr(C, "MINE_LAUNCHER_TOOL", 58)): total(
                "MINE_LAUNCHER_AMMO_CLIP_SIZE",
                "MINE_LAUNCHER_AMMO_INITIAL_STOCK",
                1,
                3,
            ),
        }
        self.grenades = self.oriented_stock[int(C.GRENADE_TOOL)]
        self._oriented_next_use = {}
        self.disguise_stock = int(getattr(C, "DISGUISE_INITIAL_STOCK", 2))
        self._disguise_next_use = 0.0

    def can_use_oriented_item(self, tool: int, now: Optional[float] = None) -> bool:
        """Validate cadence and authoritative ammo for packet 10.

        This is a read-only preflight. The handler consumes inventory only
        after the projectile has passed framing/float validation and has been
        registered successfully, so malformed packets cannot eat valid ammo.
        """
        tool = int(tool)
        current_time = time.monotonic() if now is None else float(now)
        if current_time + FIRE_RATE_GRACE < self._oriented_next_use.get(tool, 0.0):
            return False
        if tool in (
            int(getattr(C, "SNOWBLOWER_TOOL", 29)),
            int(getattr(C, "UGC_SNOWBLOWER_TOOL", 48)),
        ):
            return int(self.blocks) > 0
        return int(self.oriented_stock.get(tool, 1)) > 0

    def consume_oriented_item(self, tool: int,
                              now: Optional[float] = None) -> bool:
        """Commit one successfully spawned oriented projectile."""
        tool = int(tool)
        current_time = time.monotonic() if now is None else float(now)
        if not self.can_use_oriented_item(tool, current_time):
            return False

        if tool in (
            int(getattr(C, "SNOWBLOWER_TOOL", 29)),
            int(getattr(C, "UGC_SNOWBLOWER_TOOL", 48)),
        ):
            self.blocks = max(0, int(self.blocks) - 1)
        elif tool in self.oriented_stock:
            self.oriented_stock[tool] = max(0, self.oriented_stock[tool] - 1)
            if tool == int(C.GRENADE_TOOL):
                self.grenades = self.oriented_stock[tool]

        from server.game_constants import WEAPON_CATALOG
        profile = WEAPON_CATALOG.get(tool)
        interval = float(profile.fire_interval) if profile is not None else 0.0
        self._oriented_next_use[tool] = current_time + max(0.0, interval)
        return True

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

    def record_owner_anchor(
        self,
        stamp: int,
        position: Tuple[float, float, float],
        velocity: Tuple[float, float, float] = (0.0, 0.0, 0.0),
        *,
        queued_server_tick: Optional[int] = None,
        queued_owner_sequence: Optional[int] = None,
    ) -> None:
        """Record one self WorldUpdate row after it is queued to this owner.

        ``stamp`` is the row's per-player pong, in the retail client clock.
        IDA and a live split-clock probe proved the local path force-applies
        duplicate stamps and does not use the WorldUpdate header loop for this
        cache. ``queued_server_tick`` is diagnostic only; the monotonic owner
        sequence is what orders sends against ClientData receives.
        """
        stamp = int(stamp)
        normalized_position = tuple(float(value) for value in position)
        normalized_velocity = tuple(float(value) for value in velocity)
        sequence = self._claim_owner_timeline_sequence(
            queued_owner_sequence
        )
        anchor = OwnerAnchor(
            stamp=stamp,
            position=normalized_position,
            velocity=normalized_velocity,
            queued_server_tick=(
                None
                if queued_server_tick is None
                else int(queued_server_tick)
            ),
            queued_owner_sequence=sequence,
        )
        self._owner_anchor_history.append(anchor)
        self.last_advertised_owner_position = anchor.position

    def _claim_owner_timeline_sequence(
        self, supplied: Optional[int] = None
    ) -> int:
        """Allocate/order one owner send or input receive event.

        This is gameplay-thread state; it is not a transport acknowledgement.
        An optional supplied value exists for deterministic replay tests.
        """
        if supplied is None:
            self._owner_timeline_sequence += 1
            return self._owner_timeline_sequence
        sequence = int(supplied)
        self._owner_timeline_sequence = max(
            self._owner_timeline_sequence, sequence
        )
        return sequence

    def _owner_anchor_entry_before_input(
        self,
        source_loop: Optional[int],
        *,
        source_received_server_tick: Optional[int] = None,
        source_received_owner_sequence: Optional[int] = None,
    ) -> tuple[int, OwnerAnchor] | None:
        """Return the last server-causally eligible row for one source input.

        A self row stamped ``L`` is constructed only after the server consumes
        ClientData ``L``.  It therefore cannot have reached retail before
        retail simulated the input frame that produced that packet. Grounded
        launch reconciliation therefore requires a strict earlier stamp.

        A row queued after ClientData ``L`` reached the gameplay thread could
        not have been in retail's cache when it simulated ``L``.  Comparing the
        event sequence removes that impossible row even when both events share
        one server tick.  The server tick remains diagnostics only because
        fixed one/two-tick delivery-age guesses failed foreground captures.

        This necessary ordering is not a delivery acknowledgement: a row sent
        before the server received ``L`` may still have reached GameScene only
        after retail simulated ``L``. Raw retail captures, not this sequence,
        remain the release gate for launch behavior.
        """
        if source_loop is None:
            return None
        received_sequence = (
            None
            if source_received_owner_sequence is None
            else int(source_received_owner_sequence)
        )
        eligible = [
            anchor
            for anchor in self._owner_anchor_history
            if anchor.stamp < int(source_loop)
            and (
                received_sequence is None
                or anchor.queued_owner_sequence < received_sequence
            )
        ]
        if not eligible:
            return None
        selected = max(
            eligible, key=lambda anchor: anchor.queued_owner_sequence
        )
        return selected.stamp, selected

    def _owner_anchor_before_input(
        self,
        source_loop: Optional[int],
        *,
        source_received_server_tick: Optional[int] = None,
        source_received_owner_sequence: Optional[int] = None,
    ) -> Tuple[float, float, float]:
        """Return the newest XYZ not ruled out by server event ordering."""
        if source_loop is None:
            return tuple(self.last_advertised_owner_position)
        selected = self._owner_anchor_entry_before_input(
            source_loop,
            source_received_server_tick=source_received_server_tick,
            source_received_owner_sequence=(
                source_received_owner_sequence
            ),
        )
        if selected is None:
            return tuple(self._spawn_owner_anchor)
        return selected[1].position

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
        positions = self._build_player_collision_positions()
        server = self.connection.server if self.connection else None
        capture_debug = bool(getattr(
            getattr(server, "config", None), "movement_debug_capture", False
        ))
        pre_position = (self.x, self.y, self.z)
        if capture_debug:
            if bool(self.input.jump) and logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "movement jump %s airborne=%s trigger=%s z=%.3f vz=%.3f",
                    self.name, bool(world_object.airborne), trigger_jump,
                    float(self.z), float(world_object.velocity.z),
                )
            self.last_native_update_dt = float(dt)
            self.last_collision_count = len(positions)
            self.last_collision_preview = [
                tuple(round(float(value), 4) for value in item[:4])
                for item in positions[:4]
            ]
            self.last_native_pre_update = self._capture_native_debug_state(
                world_object, "pre_update", positions
            )
            pre_position = self.last_native_pre_update.get(
                "position", pre_position
            )
        self._update_jetpack(dt)
        self._update_parachute()
        self._apply_input_state_to_world(
            trigger_jump=trigger_jump, collisions=positions
        )
        result = world_object.update(dt, positions)
        if trigger_jump and not self.is_bot:
            # Reconciliation is keyed to Character.movement_history[L], which
            # retail snapshots *before* its native physics call.  ClientData L
            # arrives afterward. Character.update_alive then unconditionally
            # restores full XYZ from its cached network_position whenever
            # jump_this_frame is true (character.pyd 0x100808E5->0x100815AB),
            # while retaining native launch velocity and airborne state. Using
            # only pre-physics Z leaves the server one sprint step ahead and
            # eventually turns a 0.048-block X phase error into a terrain-step
            # rollback. The event-order gate removes rows definitely sent too
            # late, but cannot prove client delivery; foreground raw captures
            # are required after changing this heuristic. Bots have no owner
            # network anchor and keep full native post-move state.
            anchor = self._owner_anchor_before_input(
                self._applied_input_source_loop,
                source_received_server_tick=(
                    self._applied_input_source_server_tick
                ),
                source_received_owner_sequence=(
                    self._applied_input_source_owner_sequence
                ),
            )
            anchor_error_sq = sum(
                (float(anchor[index]) - float(pre_position[index])) ** 2
                for index in range(3)
            )
            # The maintained client has the same guard around Character.update.
            # Preserve retail's cached-anchor behavior for ordinary sub-voxel
            # phase correction, but never mirror a stale row into a visible
            # launch teleport. Velocity/airborne state still come from the
            # native physics step in either branch.
            launch_position = (
                pre_position
                if anchor_error_sq
                > JUMP_ANCHOR_TELEPORT_GUARD_DISTANCE_SQ
                else anchor
            )
            world_object.set_position(
                float(launch_position[0]),
                float(launch_position[1]),
                float(launch_position[2]),
            )
        self.last_fall_result = int(result or 0)
        self._sync_cached_vectors()
        self._apply_client_authority_pin()
        self.last_landed = bool(was_airborne and self.grounded)
        self.last_step_delta = round(float(self.z - pre_position[2]), 4)
        if capture_debug:
            self.last_native_result = int(result or 0)
            self.last_native_post_update = self._capture_native_debug_state(
                world_object, "post_update", positions
            )

    def _update_jetpack(self, dt: float) -> None:
        """Advance the stock jetpack fuel model one simulation tick.

        Packs 66/67/68 use jump for thrust; only UGC Builder pack 69 uses the
        toggle-hover input. After the per-pack start delay, activation pays its
        one-time cost and then drains fuel until release or exhaustion. Idle
        fuel regenerates after the post-damage refill delay.
        """
        physics_was_active = bool(self._jetpack_physics_active)
        props = _JETPACK_PROPERTIES.get(self.jetpack_id)
        if props is None:
            self.jetpack_active = False
            self._jetpack_physics_active = False
            self._jetpack_activation_defer_remaining = 0
            self._jetpack_exhaustion_tail_remaining = 0
            self._jetpack_requires_release = False
            return
        start_delay = float(props.get(0, 0.25))
        max_fuel = float(props.get(1, 100))
        activation_cost = float(props.get(2, 10))
        refill_rate = float(props.get(3, 10))
        drain = float(props.get(4, 75))
        refill_delay = float(props.get(6, 2.0))

        # Stock input split recovered from GameScene/Character: the normal,
        # Rocketeer, and Engineer packs thrust from jump. Only the UGC Builder
        # pack (69) accepts the toggle-hover/Z bit.
        activation_held = (
            self.input.hover
            if self.jetpack_id == int(C.JETPACK_UGCBUILDER)
            else self.input.jump
        )

        # The inactive/fuel-zero row and the client's ordinary-jump branch do
        # not take effect on the same consumed input. Preserve exactly the one
        # in-flight recurrence measured by the strict retail exhaustion gate.
        # Physical key-up still cancels this immediately below.
        exhaustion_tail = int(self._jetpack_exhaustion_tail_remaining)
        if activation_held and self._jetpack_requires_release and exhaustion_tail > 0:
            self.jetpack_active = False
            self._jetpack_physics_active = True
            self._jetpack_activation_defer_remaining = 0
            self._jetpack_exhaustion_tail_remaining = exhaustion_tail - 1
            self.jetpack_fuel = 0.0
            return

        # WorldUpdate is the owner's only source of action bit 0x04; ClientData
        # does not echo a local jetpack-active state.  Preserve the previously
        # advertised value as this tick's native state.  A transition is sent
        # after this simulation step, and native thrust follows on the next
        # consumed frame.  Applying both in the transition frame makes the
        # server one thrust recurrence ahead of retail movement_history.
        previously_advertised = bool(self.jetpack_active)
        activation_defer = int(self._jetpack_activation_defer_remaining)
        if previously_advertised and activation_defer > 0:
            self._jetpack_physics_active = False
            self._jetpack_activation_defer_remaining = activation_defer - 1
        else:
            self._jetpack_physics_active = previously_advertised
        newly_activated = False

        if activation_held:
            # dt-accumulated hold time (deterministic at the sim rate).
            self._hover_since += dt
            if (
                not previously_advertised
                and not self._jetpack_requires_release
            ):
                if (
                    self._hover_since >= start_delay
                    and self.jetpack_fuel >= max(activation_cost, 1.0)
                ):
                    self.jetpack_active = True
                    self.jetpack_fuel -= activation_cost
                    # Announce now. The two-recurrence physics delay is a local
                    # scheduling estimate; the replication handoff prevents
                    # ordinary owner rows from correcting against it before
                    # the unobservable GameScene boundary settles.
                    self._jetpack_physics_active = False
                    self._jetpack_activation_defer_remaining = (
                        JETPACK_ACTIVATION_DEFER_FRAMES
                    )
                    self._jetpack_exhaustion_tail_remaining = 0
                    newly_activated = True
        else:
            self._hover_since = 0.0
            self.jetpack_active = False
            # Retail stops thrust from physical SPACE key-up immediately even
            # while its last received WorldUpdate still carries action 0x04.
            # Do not preserve one extra authoritative thrust/drain recurrence
            # while the reliable inactive owner row travels to GameScene.
            self._jetpack_physics_active = False
            self._jetpack_activation_defer_remaining = 0
            self._jetpack_exhaustion_tail_remaining = 0
            self._jetpack_requires_release = False

        if (
            not newly_activated
            and (
                self.jetpack_active
                or self._jetpack_physics_active
            )
        ):
            # Fuel is a replicated Character resource. Once 0x04 is visible,
            # the retail owner drains it during the three-frame physics handoff,
            # even though authoritative native thrust is still deferred.
            self.jetpack_fuel -= drain * dt
            if self.jetpack_fuel <= 0.0:
                self.jetpack_fuel = 0.0
                # Advertise exhaustion now. This tick already used active
                # thrust; retain exactly one predicted recurrence before the
                # held key resumes ordinary jump behavior.
                self.jetpack_active = False
                # Holding SPACE through empty fuel must not auto-ignite as
                # regeneration crosses the activation cost. Retail requires
                # a release/new press before another start-delay cycle.
                self._jetpack_requires_release = True
                self._jetpack_activation_defer_remaining = 0
                self._jetpack_exhaustion_tail_remaining = (
                    1 if self._jetpack_physics_active else 0
                )

        if (
            not self.jetpack_active
            and not self._jetpack_physics_active
            and self.jetpack_fuel < max_fuel
        ):
            if (time.time() - self._last_damage_at) >= refill_delay:
                self.jetpack_fuel = min(max_fuel, self.jetpack_fuel + refill_rate * dt)

        if not physics_was_active and self._jetpack_physics_active:
            self._note_jetpack_physics_started()

    def _update_parachute(self) -> None:
        """Open the Commando parachute on a second airborne SPACE press.

        The December 2015 release note is explicit: "Press SPACE again after
        jumping to open the Parachute."  The first rising edge launches the
        jump from the ground; a later rising edge while airborne deploys it.
        Once open it remains open until landing/water instead of activating
        automatically at the top of every fall.
        """
        equipped = self.alive and self.parachute_id == int(C.A370)
        if not equipped or not self.airborne or self.wade:
            self.parachute_active = False
            return
        jump_pressed = bool(self.jump_held and not self.jump_last_held)
        if jump_pressed:
            self.parachute_active = True

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

    def queue_velocity_impulse(
        self,
        apply_loop: int,
        impulse: tuple[float, float, float],
    ) -> None:
        """Apply knockback on the authoritative frame bearing ``apply_loop``.

        Damage(37) is processed by retail before its frame physics, while this
        server can be several ClientData labels behind when it detects the
        impact. Labeling the impulse with the current shared loop clock keeps
        both sides from integrating the same velocity change on different
        history rows. The bounded queue fails open by applying immediately;
        gameplay state is never silently dropped under pathological traffic.
        """
        vector = tuple(float(component) for component in impulse)
        apply_loop = int(apply_loop)
        if (
            self.is_bot
            or (
                self.last_applied_input_loop is not None
                and self.last_applied_input_loop >= apply_loop
            )
        ):
            self._apply_velocity_impulse(vector)
            return
        if len(self._pending_velocity_impulses) >= PENDING_VELOCITY_IMPULSE_LIMIT:
            self._apply_velocity_impulse(vector)
            return
        self._pending_velocity_impulses.append((apply_loop, vector))

    def queue_explosion_impulse(
        self,
        after_input_frames: int,
        origin: tuple[float, float, float],
        blast_radius: float,
        knockback_min: float,
        knockback_max: float,
    ) -> int | None:
        """Apply predicted blast physics after observed client input frames.

        Retail calculates the direction when it processes ``Damage(37)``, not
        when the server detects projectile contact.  Store the origin and
        falloff parameters so the vector is recomputed from authoritative
        geometry at the matching frame.  The per-player receive sequence is a
        dense frame witness; protocol loop labels may legitimately skip.

        Returns the target input sequence, or ``None`` when applied immediately
        for a bot/overflow fallback.
        """

        effect = PendingExplosionImpulse(
            target_input_sequence=(
                self._input_receive_sequence + max(1, int(after_input_frames))
            ),
            origin=tuple(float(component) for component in origin),
            blast_radius=float(blast_radius),
            knockback_min=float(knockback_min),
            knockback_max=float(knockback_max),
        )
        if (
            self.is_bot
            or len(self._pending_explosion_impulses)
            >= PENDING_VELOCITY_IMPULSE_LIMIT
        ):
            self._apply_pending_explosion_impulse(effect)
            return None
        self._pending_explosion_impulses.append(effect)
        return effect.target_input_sequence

    def _apply_velocity_impulse(
        self, impulse: tuple[float, float, float]
    ) -> None:
        vx, vy, vz = self.velocity
        self.velocity = (
            vx + impulse[0],
            vy + impulse[1],
            vz + impulse[2],
        )

    def _apply_velocity_impulses_through(self, loop_count: int) -> None:
        if not self._pending_velocity_impulses:
            return
        remaining = deque()
        for apply_loop, impulse in self._pending_velocity_impulses:
            if apply_loop <= int(loop_count):
                self._apply_velocity_impulse(impulse)
            else:
                remaining.append((apply_loop, impulse))
        self._pending_velocity_impulses = remaining

    def _apply_explosion_impulses_through(self, input_sequence: int) -> None:
        if not self._pending_explosion_impulses:
            return
        remaining = deque()
        for effect in self._pending_explosion_impulses:
            if effect.target_input_sequence <= int(input_sequence):
                self._apply_pending_explosion_impulse(effect)
            else:
                remaining.append(effect)
        self._pending_explosion_impulses = remaining

    def _apply_pending_explosion_impulse(
        self, effect: PendingExplosionImpulse
    ) -> None:
        from server.explosions import explosion_impulse

        impulse = explosion_impulse(
            effect.origin,
            self.position,
            effect.blast_radius,
            effect.knockback_min,
            effect.knockback_max,
            crouched=bool(self.input.crouch),
        )
        if impulse is None:
            return
        self._apply_velocity_impulse(impulse)
        server = self.connection.server if self.connection else None
        if bool(getattr(getattr(server, "config", None), "movement_debug_capture", False)):
            self.last_applied_explosion_impulse_debug = {
                "input_sequence": int(self._input_receive_sequence),
                "target_input_sequence": int(effect.target_input_sequence),
                "position": tuple(self.position),
                "impulse": tuple(impulse),
                "origin": tuple(effect.origin),
            }
            logger.info(
                "BLAST IMPULSE APPLY DEBUG player=%s %r",
                self.name,
                self.last_applied_explosion_impulse_debug,
            )

    def record_input_frame(
        self,
        loop_count: int,
        flags: tuple,
        orientation: tuple,
        received_at: Optional[float] = None,
        action_flags: tuple | None = None,
        received_server_tick: Optional[int] = None,
        received_owner_sequence: Optional[int] = None,
        wire_unknown_byte: Optional[int] = None,
    ) -> None:
        """Store the movement inputs the client used for its frame
        `loop_count` so the simulation can apply them at the matching
        (delayed) server tick.

        ``received_at`` is accepted for diagnostic callers but deliberately
        does not drive physics. Live foreground A/B tests proved ENet dequeue
        intervals reflect transport/event-loop scheduling and made previously
        exact straight movement diverge when used as client frame dt.
        """
        if not self.alive or not self.spawned:
            # The retail client keeps sending ClientData during the class-change
            # death screen. Those frames describe the old body and spawn()
            # intentionally re-anchors the new life, so buffering them only
            # fills the bounded history and reports misleading overflow.
            return
        owner_sequence = self._claim_owner_timeline_sequence(
            received_owner_sequence
        )
        loop_count = int(loop_count)
        if (
            self.last_applied_input_loop is not None
            and loop_count <= self.last_applied_input_loop
        ):
            # Never let a delayed duplicate move the authoritative player a
            # second time.
            self.input_frames_stale += 1
            self.input_frames_dropped += 1
            return
        if loop_count in self.input_history:
            # ENet may surface a duplicate before the original buffered frame
            # is consumed. Keep the first complete frame and its receive tick;
            # replacing only that clock would make newer owner rows appear to
            # have existed when retail produced the input.
            self.input_frames_stale += 1
            self.input_frames_dropped += 1
            return
        self._input_receive_sequence += 1
        self.input_history[loop_count] = BufferedInputFrame(
            movement_flags=tuple(flags),
            orientation=tuple(orientation),
            action_flags=None if action_flags is None else tuple(action_flags),
            received_server_tick=(
                None
                if received_server_tick is None
                else int(received_server_tick)
            ),
            received_owner_sequence=owner_sequence,
            received_input_sequence=self._input_receive_sequence,
            wire_unknown_byte=(
                None
                if wire_unknown_byte is None
                else int(wire_unknown_byte) & 0xFF
            ),
        )
        if len(self.input_history) > INPUT_HISTORY_LIMIT:
            overflow = sorted(self.input_history)[:-INPUT_HISTORY_LIMIT]
            self.input_frames_overflow += len(overflow)
            self.input_frames_dropped += len(overflow)
            for key in overflow:
                del self.input_history[key]

    async def simulate_tick(self, dt: float) -> None:
        """Advance at most one observed client frame per server tick.

        Consume at most one buffered client input in loop-count order. The
        1.x client's reconciliation
        (apply_player_network_correction, RE'd in docs/NETCODE_RECONCILIATION.md)
        looks up its OWN movement_history at the self-row's loop_count and, if
        the server position differs, ADJUSTs (>0.1 block) or SNAPs (>4 blocks,
        wiping history — the "random rollback"). Each authoritative position
        must therefore represent a loop label received in ClientData. Retail
        loop labels can skip integers without producing history entries, so a
        synthetic acknowledgement is an immediate native-client SNAP.

        A packet burst remains queued for later server ticks. Empty ticks freeze
        movement and its acknowledgement.  The next real ClientData always
        advances exactly one fixed frame: retail loop labels can skip by two in
        an ordinary 17 ms update, so neither the label gap nor server starvation
        encodes a client physics duration.
        """
        if not self.alive or not self.spawned:
            return

        # Peerless server bots have no ClientData stream or reconciliation
        # history. Their AI writes input/orientation directly each tick, so
        # they must take one ordinary physics step instead of entering the
        # human-client starvation freeze below.
        if self.is_bot:
            await self.update(dt)
            return

        if self.last_applied_input_loop is not None:
            stale = [
                loop
                for loop in self.input_history
                if loop <= self.last_applied_input_loop
            ]
            for loop in stale:
                del self.input_history[loop]
            self.input_frames_stale += len(stale)
            self.input_frames_dropped += len(stale)

        if not self.input_history:
            # Freeze movement and its acknowledgement together.  Never roll
            # this server-side wait into a later nonlinear physics step.
            self.input_starved_ticks += 1
            self._tick_idle()
            return

        loop = min(self.input_history)
        frame = self.input_history.pop(loop)
        self._current_input_receive_sequence = int(
            frame.received_input_sequence
        )
        self._current_input_owner_sequence = frame.received_owner_sequence
        packet_flags = frame.movement_flags
        orientation = frame.orientation
        server = self.connection.server if self.connection else None
        latch_frames = int(getattr(
            getattr(server, "config", None), "movement_input_latch_frames", 1
        ))
        if latch_frames:
            # Retail applies crouch geometry before it records history row L,
            # while locomotion/jump buttons in that row still reflect L-1.
            # Compose those two input phases so the authoritative ACK anchor
            # includes the current packet's immediate +/-0.9 eye-Z change.
            flags = tuple(
                packet_flags[5] if index == 5 else value
                for index, value in enumerate(self._pending_packet_flags)
            )
            applied_input_source_loop = self._pending_packet_loop
            applied_input_source_server_tick = (
                self._pending_packet_received_server_tick
            )
            applied_input_source_owner_sequence = (
                self._pending_packet_received_owner_sequence
            )
            applied_input_source_wire_unknown_byte = (
                self._pending_packet_wire_unknown_byte
            )
        else:
            flags = packet_flags
            applied_input_source_loop = loop
            applied_input_source_server_tick = frame.received_server_tick
            applied_input_source_owner_sequence = (
                frame.received_owner_sequence
            )
            applied_input_source_wire_unknown_byte = frame.wire_unknown_byte
        # Buttons are latched by the retail input path, but native-yaw capture
        # shows aim is already current in the movement history for this label.
        # Delaying orientation creates a mixed turn state and persistent
        # correction during ordinary mouse motion.
        applied_orientation = orientation
        self.last_applied_input_loop = loop
        self.set_orientation_vector(*applied_orientation)
        self.update_input(*flags)
        if frame.action_flags is not None:
            self.update_action_input(*frame.action_flags)
        self._applied_input_flags = flags
        self._applied_orientation = orientation
        self._applied_input_source_loop = applied_input_source_loop
        self._applied_input_source_server_tick = (
            applied_input_source_server_tick
        )
        self._applied_input_source_owner_sequence = (
            applied_input_source_owner_sequence
        )
        self._applied_input_source_wire_unknown_byte = (
            applied_input_source_wire_unknown_byte
        )
        self._pending_packet_flags = packet_flags
        self._pending_packet_loop = loop
        self._pending_packet_received_server_tick = frame.received_server_tick
        self._pending_packet_received_owner_sequence = (
            frame.received_owner_sequence
        )
        self._pending_packet_wire_unknown_byte = frame.wire_unknown_byte
        self.input_frames_applied += 1
        self._apply_velocity_impulses_through(loop)
        self._apply_explosion_impulses_through(frame.received_input_sequence)
        # One packet represents one movement-history record.  ClientData does
        # not carry dt, and its clock label is deliberately non-contiguous.
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
        # Exact stock mapping distinguishes 0x04 (jetpack active) from 0x10
        # (can_display_weapon). 0x08 is still unassigned here.
        byte = 0
        if self.input.primary_fire:
            byte |= 0x01
        if self.input.secondary_fire:
            byte |= 0x02
        # 0x04 = jetpack active (display + pack-specific flight on the client).
        # SAFE here ONLY because it is gated on jetpack_active: the server's fuel
        # model (driven by the client's hover/Z bit) sets this True only while
        # the player is actually firing the jetpack with fuel, and clears it the
        # instant they release Z or run dry. That is the OPPOSITE of the old
        # jump-stuck bug, which came from 0x08/0x10 being set UNCONDITIONALLY
        # (echoed from always-on ClientData bits) so the client believed it was
        # perma-jetpacking and skipped gravity forever. A transient, truthful
        # 0x04 makes the jetpack visible on others AND drives server-authoritative
        # flight; when it clears, normal gravity + jumping resume. (If a jump
        # regression shows up, this bit is the first suspect — revert to 0.)
        if getattr(self, "jetpack_active", False):
            byte |= 0x04
        # Stock WorldUpdate action bit 0x10 is can_display_weapon.  Remote
        # clients feed it directly to set_can_display_weapon; dropping it
        # makes every equipped weapon model invisible to other players.
        if self.input.can_display_weapon:
            byte |= 0x10
        if self.on_fire:
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

    def pack_state_flags(self) -> int:
        """Pack the stock post-action display-state byte for remote clients."""
        byte = 0
        if getattr(self, "parachute_active", False):
            byte |= 0x01
        if self.disguised:
            byte |= 0x02
        if self.wade:
            byte |= 0x08
        return byte

    def world_update_snapshot(self) -> Tuple[Tuple[float, float, float], ...]:
        # (pos, orient, vel, ping, pong, hp, inp, action, state, tool,
        #  pickup, jetpack_fuel, spawn_protection_timer, weapon_deployment_yaw)
        # `pong` carries wu_ack_loop — the client input loop_count this row's
        # position corresponds to. It is what the client pairs against its own
        # movement_history to decide NO-OP / ADJUST / SNAP.
        return (
            self.position,
            self.orientation,
            self.velocity,
            0,
            self.wu_ack_loop,
            self.health,
            self.pack_input_flags(),
            self.pack_action_flags(),
            self.pack_state_flags(),
            self.tool,
            0xFF if self.pickup_id is None else int(self.pickup_id),
            self.jetpack_fuel,
            0.0,
            0.0,
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
