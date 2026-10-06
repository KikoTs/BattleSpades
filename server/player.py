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
from server import action_clock, anticheat, conduct
from server.flight_profile import apply_mover_tuning, canopy_vz_step, profile_for
from server.lag_compensation import record_player as _record_lag_history
from server.lag_compensation import shooter_rtt_ms as _shooter_rtt_ms
from server.deployable_inventory import (
    commit_deployable_use,
    deployable_ready,
    reset_deployable_inventory,
    restock_deployable_inventory,
)
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
# Compatibility heuristic, not an original-client rule: ClientData carries no
# jetpack-active acknowledgement. The original Player.set_jetpack_active writes
# native state immediately on receipt (player.pyd 0x1000BEE3/0x1000C011), but does
# not reveal the original server's activation schedule. Preserve this existing
# two-frame estimate until the server-side resource timeline is recovered.
JETPACK_ACTIVATION_DEFER_FRAMES = 2
# Live 60 Hz captures (docs/RETAIL_JUMP_RESTORE.md, 2026-09-24): the retail
# owner stops thrust 2-3 frames after the inactive row is sent; three frames
# keeps any residual a forward nudge. Config overrides these fallbacks.
JETPACK_EXHAUSTION_TAIL_FRAMES = 3
# Where the retail owner really applies a flight transition row. Eight live
# 60 Hz captures (16 boundaries, loopback), S = label being simulated when the
# row is queued, N = newest label already received: the owner's thrust changed
# on N + 2 (10 times) or N + 3 (6 times), never earlier. The fixed constants
# above equal that lower bound only while one label is buffered, and ignore
# the round trip a real link adds. ``_jetpack_handoff_frames`` applies the
# bound: ignition starts on the earliest possible label (a late owner is a
# forward nudge), exhaustion ends on the latest (never a rollback).
JETPACK_HANDOFF_EARLIEST_AFTER_NEWEST = 2
JETPACK_HANDOFF_LATEST_AFTER_NEWEST = 3
# ENet's round trip on a link with no delay: the stock client services its
# socket every 10 ms, the loopback captures above already contain it.
JETPACK_HANDOFF_LOCAL_RTT_MS = 8.0
JETPACK_HANDOFF_MAX_FRAMES = 30
# Parachute (equipment 72) policy; docs/PARACHUTE.md has the evidence and the
# measurements. The canopy arithmetic itself (0.05 gravity, per-frame fall
# reset) is the stock native mover and is not touched here. Every value below
# may be overridden by a same-named lower-case ``[debug]``/config attribute.
PARACHUTE_ID = int(C.A370)
# Feet-to-ground clearance required to open. A flat-ground jump apex is ~1.3
# blocks, so no hop can open a canopy; a 6-block drop still can.
PARACHUTE_MIN_DEPLOY_CLEARANCE = 6.0
# A canopy that has been open this long collapses (single deploy per fall, so
# it cannot be reopened before landing). 30 s is ~48 blocks of canopy descent.
PARACHUTE_MAX_OPEN_SECONDS = 30.0
# Native vz is positive downward. Rising faster than the canopy's own terminal
# descent (0.05 native = 1.6 blocks/s) means an explosion or collision is
# lifting the player; 0.05 gravity would turn that into a long float, so the
# canopy spills. Ordinary deployment never sees upward speed (descent only).
PARACHUTE_MAX_RISE_VELOCITY = 0.05
# Retail owners learn canopy state only from WorldUpdate state bit 0x01 and
# apply it when the row is processed. Server canopy physics therefore follows
# the advertised state this many accepted input frames after the owner row
# carrying the change is queued, plus the connection's round trip in frames.
PARACHUTE_OWNER_HANDOFF_FRAMES = 3
PARACHUTE_OWNER_HANDOFF_MAX_FRAMES = 30
# Safety net: an advertised change that no owner row has carried after this
# many frames (self rows disabled/unsafe) is applied to physics anyway.
PARACHUTE_UNSENT_HANDOFF_FRAMES = 30

JUMP_BUFFER_SECONDS = 0.25
POSITION_SAMPLE_FRESHNESS_SECONDS = 0.50

# ClientData carries a client loop label, not a server tick or delivery ACK.
# Keep the compatibility latch; it does not prove arrival by any deadline or
# bit-exact agreement. Retail stores history before native movement and can
# relabel its loop during ClockSync. Simulation consumes accepted input frames
# rather than deriving elapsed time from gaps between those labels.
INPUT_DELAY_TICKS = 1
INPUT_HISTORY_LIMIT = 128
PENDING_VELOCITY_IMPULSE_LIMIT = 64
OWNER_ANCHOR_HISTORY_LIMIT = 128
IDLE_INPUT_FLAGS = (False, False, False, False, False, False, False, False)
# Slack on the server-side fire-rate gate: one 60Hz sim tick (~16.7ms).
FIRE_RATE_GRACE = 1.0 / 60.0
# Per-label eye/aim history for shot-origin validation (combat_runtime).
EYE_HISTORY_LIMIT = 128
# Zoom bit per received ClientData label (see Player.zoom_for_action).
ZOOM_LABEL_HISTORY_LIMIT = 256
ZOOM_ACTION_LOOKBACK = 3
ZOOM_ACTION_SEARCH = 12
# A stock client's label is its ClockSync estimate of the server loop plus
# one-way latency (gameScene process_packet_clock_sync), so it never runs
# seconds ahead of the tick on which the server received it. A label beyond
# both of these bounds cannot come from the retail client; accepting it made
# every later real label "stale" and froze the body for the rest of the life.
INPUT_LABEL_AHEAD_OF_APPLIED = 64
INPUT_LABEL_AHEAD_OF_SERVER = 256
# Samples retained for per-player ClientData queue-delay statistics.
INPUT_QUEUE_DELAY_SAMPLES = 600
# ClientData arrival order. A label this far behind the newest one is a
# ClockSync relabel (MAX_CLOCK_SYNC_DIFFERENCE = 10), not link reordering.
INPUT_REORDER_MAX_FRAMES = 10
# How long one observed reordering keeps the replication guard engaged, and
# the resolution the sliding maximum is kept at.
INPUT_REORDER_WINDOW_TICKS = 1200
INPUT_REORDER_BUCKET_TICKS = 60
# Refilled labels remembered for a late real packet (``_salvage_late_frame``).
SYNTHESIZED_LABEL_LIMIT = 16
# Trigger-like inputs honoured once when they were down only in a frame the
# server never simulated: movement index 4 = jump, action index 7 = hover.
# Held states (directions, crouch, sneak, sprint, fire, zoom) are not latched.
PRESS_LATCH_MOVEMENT_INDICES = (4,)
PRESS_LATCH_ACTION_INDICES = (7,)
# Launcher reload gate. Live retail RPG: 4 rockets take ~6.6 s, i.e. each
# emptied clip waits shoot_interval + reload_time (0.7 + 1.5 s) before the
# next round. The grace absorbs packet jitter between two shots.
LAUNCHER_RELOAD_GRACE_SECONDS = 0.25
LAUNCHER_RELOAD_GRACE_FRACTION = 0.1
# Horizontal drift (blocks) a disguised player may take -- knockback, a
# settling step -- before "Must remain stationary" breaks the Disguise.
DISGUISE_MOVE_TOLERANCE = 0.5
# Stock server-only ONE_HIT_KILL_WEAPONS (A2382, kill types).
_ONE_HIT_KILL_TYPES = frozenset(
    int(value) for value in getattr(
        C, "ONE_HIT_KILL_WEAPONS",
        (0, 1, 2, 3, 4, 5, 6, 21, 22, 23, 24, 18, 13, 14, 15, 16, 17, 19),
    )
)
# Stock server-only fall rules (A2396-A2398): a landing after an own-rocket
# self-push takes ROCKET_JUMP_FALL_DAMAGE_MULTIPLIER of the damage, and a
# landing with almost no air time (teleport/correction artefacts) is scaled
# from 0 at ZERO_FALL_DAMAGE_AIR_TIME to full at MAX_FALL_DAMAGE_AIR_TIME.
ROCKET_JUMP_FALL_DAMAGE_MULTIPLIER = float(
    getattr(C, "ROCKET_JUMP_FALL_DAMAGE_MULTIPLIER", 0.2)
)
ZERO_FALL_DAMAGE_AIR_TIME = float(getattr(C, "ZERO_FALL_DAMAGE_AIR_TIME", 1.0 / 60.0))
MAX_FALL_DAMAGE_AIR_TIME = float(getattr(C, "MAX_FALL_DAMAGE_AIR_TIME", 4.0 / 60.0))
# An own blast counts as the launch of the following airborne phase when it
# lands up to this long before take-off (the push is applied a few accepted
# frames after the blast).
ROCKET_JUMP_TAKEOFF_WINDOW_SECONDS = 0.5
# Kill types of the self-pushing blasts that make a "rocket jump".
_ROCKET_JUMP_KILL_TYPES = frozenset(
    int(getattr(C.KILL, name, default))
    for name, default in (
        ("ROCKET_KILL", 4), ("ROCKET2_KILL", 5), ("UGC_ROCKET2_KILL", 27),
    )
)
# Stock per-life stock of every server-tracked oriented tool, as the stock
# aos.pkg class attributes resolve (scratchpad stock_weapons.txt):
#   tool -> (magazine max, magazine initial, reserve max, reserve initial,
#            crate restock amount)
# Count tools (GrenadeTool & co.) store ``(default_count, initial_count, 0, 0,
# restock_amount)``; Weapon subclasses store their ``ammo`` tuple verbatim.
# ``Tool.restock(AMMO_CRATE)`` = ``min(count + restock, default_count)``;
# ``Weapon.restock(AMMO_CRATE)`` tops the RESERVE up to its max, or the
# magazine when the weapon has no reserve. Any other restock type resets to
# the initial values. RPG2 is (3, 3, 3, 3, 3): 6 rockets per life -- the
# ``RPG2_AMMO_MAX = 6`` constant only exists in the modded constants tail.
ORIENTED_STOCK_AMMO: dict[int, tuple[int, int, int, int, int]] = {
    int(C.GRENADE_TOOL): (4, 2, 0, 0, 4),
    int(getattr(C, "CLASSIC_GRENADE_TOOL", 31)): (4, 2, 0, 0, 4),
    int(getattr(C, "ANTIPERSONNEL_GRENADE_TOOL", 32)): (4, 2, 0, 0, 4),
    int(getattr(C, "MOLOTOV_TOOL", 33)): (3, 3, 0, 0, 3),
    int(getattr(C, "CHEMICALBOMB_TOOL", 54)): (4, 2, 0, 0, 2),
    int(getattr(C, "STICKY_GRENADE_TOOL", 57)): (4, 2, 0, 0, 2),
    int(C.RPG_TOOL): (1, 1, 3, 3, 3),
    int(C.RPG2_TOOL): (3, 3, 3, 3, 3),
    int(C.DRILLGUN_TOOL): (1, 1, 3, 1, 2),
    int(getattr(C, "GRENADE_LAUNCHER_WEAPON_TOOL", 55)): (1, 1, 5, 3, 5),
    int(getattr(C, "MINE_LAUNCHER_TOOL", 58)): (1, 1, 5, 3, 5),
}
# Gun reload vs. a shot that arrives just before the server's own reload
# timer (retail Character.end_reload runs on the client's clock). A shot this
# close to the end of the current cycle completes that cycle first.
# Arrival jitter admitted against an oriented tool's shoot_interval. Retail
# fires launchers continuously while LMB is held (Character.update_weapon), so
# back-to-back packets arrive exactly one interval apart on the client clock.
ORIENTED_CADENCE_GRACE = 0.05
RELOAD_FIRE_GRACE_SECONDS = 0.1
RELOAD_FIRE_GRACE_FRACTION = 0.1
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
    # World topology revision when this frame arrived. Backlog catch-up only
    # replays two frames in one tick when no terrain edit sits between them.
    topology_version: int | None = None


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
        # Per-body maximum: VIP bosses carry RULE_VIP_HEALTH x 100. Every
        # spawn resets it; heals and crates cap at it.
        self.max_health: int = MAX_HEALTH
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
        reset_deployable_inventory(self)
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
        # Resource constants come from the client. Its character/world modules
        # consume replicated state; the server resource recurrence is a local
        # compatibility policy, not a recovered client fuel simulation.
        self.jetpack_id: int = 0            # 0 / NO_JETPACK(65) = none
        self.jetpack_fuel: float = 100.0
        # ``jetpack_active`` is the state advertised to the retail owner in
        # WorldUpdate action bit 0x04. Native activation uses a two-recurrence
        # local estimate; retail provides no packet that proves GameScene has
        # applied the transition.
        self.jetpack_active: bool = False
        self._jetpack_physics_active: bool = False
        self._jetpack_activation_defer_remaining: int = 0
        # Compatibility tail retained from prior capture work; the original
        # client does not prove an extra authoritative exhaustion recurrence.
        self._jetpack_exhaustion_tail_remaining: int = 0
        self._jetpack_requires_release: bool = False
        self._hover_since: float = 0.0
        self._jetpack_idle_seconds: float = 0.0
        self._last_damage_at: float = 0.0
        self._last_combat_damage_at: float = 0.0
        # Fall-rule bookkeeping (scaled_fall_damage).
        self._fall_air_time: float = 0.0
        self._airborne_since: float = 0.0
        self._rocket_jump_blast_at: Optional[float] = None
        self._last_damage_source_id: int = -1
        self._last_damage_source_position = None
        self.parachute_id: int = 0
        # ``parachute_active`` is the advertised canopy (WorldUpdate state bit
        # 0x01, HUD, sounds, observers). ``_parachute_physics_active`` is what
        # the native mover uses; for retail owners it trails the advertised
        # state by the owner handoff (see _advance_parachute_physics).
        self.parachute_active: bool = False
        self._reset_parachute_state()
        self.disguised: bool = False        # specialist disguise toggle
        self.mounted_entity_id = None        # mounted MACHINE_GUN entity, if any
        self.on_fire: bool = False          # authoritative Molotov burn state
        # Chemical Bomb goo contact (WorldUpdate state bit 0x08, stock
        # Character.set_touching_goo). Owned by server.chemical_goo.
        self.touching_goo: bool = False
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
        # The native mover holds its wade flag while airborne and only
        # re-evaluates it on ground contact. A teleport is not a physical
        # move, so the flag it carried from the old position is ignored until
        # the mover next reports the player on the ground.
        self._wade_stale_after_teleport: bool = False

        self.respawn_time: float = 0.0
        self.death_time: float = 0.0
        self.spawned_at: float = 0.0
        # Spawn protection lasts RULE_SPAWN_PROTECTION_TIME or until this
        # life first attacks (end_spawn_protection), whichever comes first.
        self.spawn_protection_cancelled: bool = False
        # Mode-set cap for this life (Zombie patient zero: 0.5 s); None = rule.
        self.spawn_protection_cap: Optional[float] = None
        self._grave_entity_id = None

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
        # ClientData locomotion and movement orientation are held for the next
        # observed frame. Current aim remains immediate for combat/display.
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
        self.input_frames_synthesized: int = 0
        # True while the newest authoritative frame was a refilled (guessed)
        # lost frame. Replication skips the owner self row for that label: the
        # client's history entry for it may include an input change the lost
        # packet carried (crouch geometry alone is a 0.9-block difference).
        self.last_applied_input_synthesized: bool = False
        self._orientation_after_synth: bool = False
        # Arrival order of the unsequenced ClientData stream. A label that
        # arrives after a newer one measures how far this link reorders
        # datagrams; replication paces its unsequenced snapshots by it
        # (ReplicationService reorder guard). Never reset per life: the
        # client keeps one loop clock across respawns.
        self._input_newest_label: Optional[int] = None
        self._input_reorder_events: deque[tuple[int, int]] = deque(
            maxlen=INPUT_REORDER_WINDOW_TICKS // INPUT_REORDER_BUCKET_TICKS + 2
        )
        self.input_frames_reordered: int = 0
        # Labels refilled as lost, kept briefly so a late real packet can
        # still supply what the guess could not know (see
        # ``_salvage_late_frame``). label -> buttons the refill assumed.
        self._synthesized_labels: dict[int, tuple] = {}
        self.input_frames_salvaged: int = 0
        # Buttons and actions that were down only in a frame the server
        # never simulated; honoured once on the next consumed frame.
        self._press_latch_flags: tuple = ()
        self._press_latch_actions: tuple = ()
        self.input_presses_latched: int = 0
        # Action flags of the newest frame the simulation consumed.
        self._applied_action_flags: Optional[tuple] = None
        # False while a ClientData older than the newest seen label is being
        # handled: its immediate state must not overwrite newer input.
        self.last_input_arrival_fresh: bool = True
        # Score-bearing crouch edges are detected on the arrival timeline
        # only; the buffered replay trails it and would repeat each edge.
        self._crouch_edge_held: bool = False
        self._applying_buffered_frame: bool = False
        self.rejected_tool_updates: int = 0
        # Anti-cheat input accounting (docs: server/anticheat.py).
        # label -> (eye xyz, aim xyz) right after that frame was simulated.
        self._eye_history: dict[int, tuple] = {}
        self._starved_streak: int = 0
        self._last_client_data_at: Optional[float] = None
        self._starvation_baseline: float = 0.0
        self._last_simulated_at: Optional[float] = None
        self._starvation_timeout_flagged: bool = False
        self._backlog_over_ticks: int = 0
        self._backlog_catchup: bool = False
        self.input_queue_delays: deque[int] = deque(
            maxlen=INPUT_QUEUE_DELAY_SAMPLES
        )
        self.input_frames_catchup: int = 0
        self.input_frames_backlog_dropped: int = 0
        self.input_frames_starvation_steps: int = 0
        self.input_frames_rejected_ahead: int = 0
        self._tool_before_mg: Optional[int] = None
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
        self.last_retail_jump_loop: Optional[int] = None
        self.last_buffered_jump_active: bool = False
        self.last_collision_count: int = 0
        self.last_collision_preview: list[tuple[float, float, float, float]] = []
        self.last_native_update_dt: float = 0.0
        self.last_native_result: int = 0
        self.last_native_pre_update: dict = {}
        self.last_native_post_update: dict = {}

        self.kills: int = 0
        # Current-life streak (profile/award stats). KillAction.kill_count is
        # the separate MULTIKILLMAXTIMEGAP chain in server/kill_feed.py.
        self.kill_streak: int = 0
        self.deaths: int = 0
        self.captures: int = 0
        # Personal scoreboard number (the client's per-player column). Driven
        # by the mode via scoreboard.send_player_score on each scoring event.
        self.score: int = 0
        self._teabagged_deaths: set[tuple[int, float]] = set()

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
        # InitialInfo speed scale (the lobby speed rules, wire-rounded); the
        # server simulation must use identical effective values or prediction
        # drifts (rubber-band).
        from server.class_data import rule_speed_scale, speed_scale
        scale = speed_scale(self._class_id)
        water_damage_multiplier = self.movement_profile.fall_on_water_damage_multiplier
        server = self.connection.server if self.connection else None
        config = getattr(server, "config", None)
        if config is not None:
            from server.game_rules import get_rules

            rules = get_rules(config)
            # Retail GameClass.__init__ passes zero into the native mover
            # when this InitialInfo rule is disabled, not only a later HP gate.
            if not rules.enabled("RULE_ENABLE_FALL_ON_WATER_DAMAGE"):
                water_damage_multiplier = 0.0
            scale = rule_speed_scale(config, self._class_id)
        world_object.set_class_accel_multiplier(self.movement_profile.accel_multiplier * scale)
        world_object.set_class_sprint_multiplier(self.movement_profile.sprint_multiplier * scale)
        world_object.set_class_jump_multiplier(self.movement_profile.jump_multiplier)
        world_object.set_class_crouch_sneak_multiplier(self.movement_profile.crouch_sneak_multiplier * scale)
        world_object.set_class_can_sprint_uphill(self.movement_profile.can_sprint_uphill)
        world_object.set_class_water_friction(self.movement_profile.water_friction)
        world_object.set_class_fall_on_water_damage_multiplier(
            water_damage_multiplier
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
        # ``hover`` is the UGC Builder pack's toggled Z ability.  The same
        # ClientData bit is also used by our patched Commando deploy key,
        # but feeding it into every native player skips gravity entirely.
        # Retail only applies the mover flag for pack 69; parachutes use their
        # separate replicated state. Do not blanket-reject crouch here:
        # Character.is_crouching excludes active hover (0x10023760), and native
        # hover+crouch is the UGC descent control. ClientData already carries
        # the filtered world hover state rather than the raw key press.
        world_object.hover = bool(
            self.input.hover
            and self.jetpack_id == int(C.JETPACK_UGCBUILDER)
        )
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
            # Product flight policy: the Glide pack combines its stock thrust
            # with the native 0.75-gravity state, approaching level flight at
            # 60 Hz. Rocket/Engineer retain their ordinary upward thrust.
            # Use the same physics phase as active, including release/tail.
            world_object.jetpack_passive = bool(
                self.jetpack_id == int(C.JETPACK2)
                and self._jetpack_physics_active
            )
            world_object.parachute = int(self.parachute_id or 0)
            world_object.parachute_active = bool(
                self._parachute_physics_active
            )
            # Negotiated (BSFP v2) Engineer flight speed and canopy descent;
            # stock owners, v1 natives and bots keep the world.pyd literals.
            apply_mover_tuning(world_object, profile_for(self))
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

    def _jetpack_boundary_frames(self, name: str, default: int) -> int:
        """Configured accepted-input frames for one unacknowledged pack boundary.

        Native BattleSpades clients (BSCF flight capability) predict these
        boundaries locally with the same retail-calibrated constants (defer 2,
        tail 3; the client's ``jetpack_activation_defer_frames`` /
        ``jetpack_exhaustion_tail_frames`` in flight_profile.hpp). A host's
        ``[debug]`` override tunes the retail owner handoff only, so native
        owners keep the defaults and never diverge at a burn's end.
        """
        if bool(getattr(self.connection, "flight_profile_capable", False)):
            return int(default)
        config = getattr(getattr(self.connection, "server", None), "config", None)
        try:
            return max(0, min(30, int(getattr(config, name, default))))
        except (TypeError, ValueError):
            return int(default)

    def _jetpack_handoff_frames(
        self, name: str, default: int, *, latest: bool
    ) -> int:
        """Accepted-input frames until the retail owner applies a transition.

        ``latest`` selects the last label the owner can still be on (used for
        exhaustion, where the server must not stop thrust before the owner
        does); otherwise the first one (ignition). With one buffered label
        and no network delay both equal the configured constants.
        """
        frames = self._jetpack_boundary_frames(name, default)
        if bool(getattr(self.connection, "flight_profile_capable", False)):
            # Native owners predict the boundary from their own input.
            return frames
        config = getattr(getattr(self.connection, "server", None), "config", None)
        if not bool(getattr(config, "jetpack_handoff_latency_aware", True)):
            return frames
        applied = self.last_applied_input_loop
        ahead = 0
        if applied is not None and self.input_history:
            try:
                ahead = max(0, min(10, int(max(self.input_history)) - int(applied)))
            except (TypeError, ValueError):
                ahead = 0
        after_newest = (
            JETPACK_HANDOFF_LATEST_AFTER_NEWEST if latest
            else JETPACK_HANDOFF_EARLIEST_AFTER_NEWEST
        )
        # The boundary label is S + 1 + frames and must reach N + after_newest.
        frames = max(frames, ahead + after_newest - 1)
        network_ms = max(0.0, _shooter_rtt_ms(self) - JETPACK_HANDOFF_LOCAL_RTT_MS)
        network_frames = network_ms * 60.0 / 1000.0
        frames += int(math.ceil(network_frames) if latest else math.floor(network_frames))
        return max(0, min(JETPACK_HANDOFF_MAX_FRAMES, int(frames)))

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
        wade = bool(self._world_object.wade)
        if self._wade_stale_after_teleport:
            if self.airborne:
                wade = False
            else:
                self._wade_stale_after_teleport = False
        self.wade = wade
        self.grounded = not self.airborne

    @property
    def class_id(self) -> int:
        return self._class_id

    @class_id.setter
    def class_id(self, value: int):
        self._class_id = int(value)
        self.movement_profile = get_movement_profile(self._class_id)
        self.blocks = min(self.blocks, self._block_wallet_max())
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
        server = self.connection.server if self.connection else None
        config = getattr(server, "config", None)
        if config is None:
            self.loadout = list(selection.loadout)
            self.prefabs = list(selection.prefabs)
        else:
            from server.game_rules import get_rules

            rules = get_rules(config)
            self.loadout = [
                int(tool) for tool in selection.loadout
                if rules.is_tool_enabled(int(tool))
            ]
            self.prefabs = (
                list(selection.prefabs)
                if rules.enabled("RULE_ENABLE_PREFABS")
                else []
            )
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
    def applied_loop(self) -> Optional[int]:
        """Client loop label of the last input frame authority simulated."""
        return self.last_applied_input_loop

    def eye_at_loop(
        self, loop_count: int
    ) -> Optional[Tuple[float, float, float]]:
        """Server eye right after input frame ``loop_count`` was simulated.

        Covers the last ``EYE_HISTORY_LIMIT`` applied labels of this life,
        including refilled lost frames. ``None`` when the label is unknown
        (never applied, evicted, dropped as backlog, or a previous life).
        """
        try:
            entry = self._eye_history.get(int(loop_count))
        except (AttributeError, TypeError, ValueError, OverflowError):
            return None
        return None if entry is None else entry[0]

    def orientation_at_loop(
        self, loop_count: int
    ) -> Optional[Tuple[float, float, float]]:
        """Aim carried by input frame ``loop_count`` (see ``eye_at_loop``)."""
        try:
            entry = self._eye_history.get(int(loop_count))
        except (AttributeError, TypeError, ValueError, OverflowError):
            return None
        return None if entry is None else entry[1]

    def _record_zoom_label(self, loop_count, action_flags) -> None:
        """Remember the zoom bit of every received ClientData by its label.

        ClientData is unsequenced while ShootPacket is reliable: a shot whose
        datagram was lost is retransmitted after ClientData frames that
        already carry the post-shot state (``Character.reload`` and the last
        round un-zoom a sniper). The shot's pellet spread must use the zoom
        of its own frame, not whatever ClientData arrived last.
        """
        if action_flags is None or len(action_flags) < 3:
            return
        try:
            label = int(loop_count)
        except (TypeError, ValueError, OverflowError):
            return
        history = getattr(self, "_zoom_by_label", None)
        if not isinstance(history, dict):
            history = {}
            self._zoom_by_label = history
        history[label] = bool(action_flags[2])
        while len(history) > ZOOM_LABEL_HISTORY_LIMIT:
            del history[next(iter(history))]
        # When did this label's ClientData arrive? The unsequenced stream is
        # never held behind a lost reliable packet, so it dates the client
        # frame that also produced a (possibly retransmitted) action packet.
        arrivals = getattr(self, "_label_arrival_tick", None)
        if not isinstance(arrivals, dict):
            arrivals = {}
            self._label_arrival_tick = arrivals
        if label not in arrivals:
            server = getattr(getattr(self, "connection", None), "server", None)
            tick = getattr(server, "loop_count", None)
            if tick is not None:
                arrivals[label] = int(tick)
                while len(arrivals) > ZOOM_LABEL_HISTORY_LIMIT:
                    del arrivals[next(iter(arrivals))]

    def label_arrival_tick(self, loop_count) -> Optional[int]:
        """Server tick at which ClientData ``loop_count`` arrived (or None)."""
        arrivals = getattr(self, "_label_arrival_tick", None)
        if not isinstance(arrivals, dict) or loop_count is None:
            return None
        try:
            return arrivals.get(int(loop_count))
        except (TypeError, ValueError, OverflowError):
            return None

    def zoom_for_action(self, loop_count) -> bool:
        """Zoom state of the client frame an action packet describes.

        True when any ClientData labelled within ``ZOOM_ACTION_LOOKBACK``
        frames up to ``loop_count`` had the zoom bit (covers a frame whose
        ClientData was sampled after the shot dropped the zoom, and the
        native client labelling actions one frame late). Frame ``loop_count``
        itself may already show the post-shot state, so it alone never
        proves "not zoomed": the newest frame BEFORE it (searched up to
        ``ZOOM_ACTION_SEARCH`` back, since jittered or lost ClientData may
        not have arrived yet) decides. When neither the shot's frame nor the
        one before it arrived the state is unknowable and the shot counts as
        zoomed; with no history at all, the newest zoom state.
        Zoom only narrows the stock spread and any client may legitimately
        hold it, so leniency here grants nothing a stock client cannot do.
        """
        history = getattr(self, "_zoom_by_label", None)
        current = bool(getattr(getattr(self, "input", None), "zoom", False))
        if loop_count is None or not isinstance(history, dict) or not history:
            return current
        try:
            label = int(loop_count)
        except (TypeError, ValueError, OverflowError):
            return current
        window = [history.get(label - back) for back in range(ZOOM_ACTION_LOOKBACK + 1)]
        if any(window):
            return True
        if window[0] is None and window[1] is None:
            # The shot's frame and the one before it were lost with it (the
            # shot itself was retransmitted): the scope state is unknowable,
            # so favour the shooter -- zoom is client-chosen anyway.
            return True
        for back in range(1, ZOOM_ACTION_SEARCH + 1):
            state = history.get(label - back)
            if state is not None:
                return bool(state)
        own = history.get(label)
        return current if own is None else bool(own)

    def _record_eye_history(self, loop: int, aim) -> None:
        history = getattr(self, "_eye_history", None)
        if not isinstance(history, dict):
            history = {}
            self._eye_history = history
        history[int(loop)] = (
            (float(self.eye_x), float(self.eye_y), float(self.eye_z)),
            tuple(float(value) for value in aim),
        )
        while len(history) > EYE_HISTORY_LIMIT:
            del history[next(iter(history))]

    @property
    def hitbox_crouched(self) -> bool:
        """Crouch state of the simulated body, not the raw input bit.

        The native mover refuses to stand up under a low ceiling and ignores
        crouch while hovering, so ``input.crouch`` can disagree with the
        hitbox the client renders.
        """
        world_object = self._world_object
        if world_object is not None:
            try:
                # Character.is_crouching excludes active hover (0x10023760).
                return bool(world_object.crouch) and not bool(
                    getattr(world_object, "hover", False)
                )
            except Exception:
                pass
        return bool(self.input.crouch)

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
        self._clear_transient_movement_state()

    def _clear_transient_movement_state(self) -> None:
        """Drop per-fall movement state a teleport must not carry over.

        A player teleported out of water kept ``wade`` until the mover next
        touched ground, so ``_update_parachute`` treated the new fall as
        grounded and refused its first canopy. The canopy's per-fall latch
        belongs to the old fall as well; an armed deploy press is the
        player's own intent and survives.
        """
        self.wade = False
        self._wade_stale_after_teleport = True
        self._parachute_used_this_fall = False

    def set_orientation(self, yaw: float, pitch: float):
        self.yaw = yaw
        self.pitch = pitch

    def set_orientation_vector(self, x: float, y: float, z: float):
        try:
            x, y, z = float(x), float(y), float(z)
        except (TypeError, ValueError):
            return
        if not (math.isfinite(x) and math.isfinite(y) and math.isfinite(z)):
            # ClientData carries fixed-point shorts, so the stock wire cannot
            # produce this; keep the previous aim rather than poisoning the
            # native mover and every later ray.
            server = self.connection.server if self.connection else None
            anticheat.report(server, self, "orientation_nonfinite")
            return
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
        if server is not None:
            from server.entities.attachments import forget_player as forget_attachments

            forget_attachments(server, self)
        self.reset_block_color_for_spawn()
        corpse_lifecycle = getattr(server, "corpse_lifecycle", None)
        before_player_spawn = getattr(
            corpse_lifecycle, "before_player_spawn", None
        )
        if callable(before_player_spawn):
            before_player_spawn(self)
        replication = getattr(server, "replication", None)
        forget_player = getattr(replication, "forget_player", None)
        if callable(forget_player):
            forget_player(self.id)
        self.replication_generation += 1
        self.last_kill_action_data = None
        self._teabagged_deaths.clear()
        self.damage_contributions = {}
        self.max_health = MAX_HEALTH
        self.health = MAX_HEALTH
        self.spawned_at = time.monotonic()
        self.spawn_protection_cancelled = False
        self.spawn_protection_cap = None
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
        self._eye_history = {}
        self._starved_streak = 0
        self._starvation_timeout_flagged = False
        self._last_simulated_at = None
        self._backlog_over_ticks = 0
        self._backlog_catchup = False
        # A press or refill of the previous body never reaches the new one.
        self._synthesized_labels = {}
        self._press_latch_flags = ()
        self._press_latch_actions = ()
        self._applied_action_flags = None
        self._crouch_edge_held = False
        self._applying_buffered_frame = False
        self._tool_before_mg = None
        self.blocks = self._block_wallet_start()
        self.grenades = MAX_GRENADES
        self._reset_ammo()
        self.rocket_turret_stock = int(
            getattr(C, "ROCKET_TURRET_INITIAL_STOCK", 2)
        )
        self._reset_equipment_state()
        reset_deployable_inventory(self)
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
        self._jetpack_idle_seconds = 0.0
        self.parachute_id = (
            int(C.A370)
            if int(C.A370) in [int(item) for item in (getattr(self, "loadout", None) or [])]
            else 0
        )
        self.parachute_active = False
        # Retail Character.set_parachute_active(False) on spawn is client-local
        # and immediate, so both sides start closed with no handoff.
        self._reset_parachute_state()
        self.disguised = False
        self.on_fire = False
        self.touching_goo = False
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
        self.last_retail_jump_loop = None
        self.last_buffered_jump_active = False
        self.last_collision_count = 0
        self.last_collision_preview = []
        self.last_native_update_dt = 0.0
        self.last_native_result = 0
        self.last_native_pre_update = {}
        self.last_native_post_update = {}
        self.last_shot_time = 0.0
        self.next_shot_time = 0.0
        action_clock.reset(self)
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
            # 50/7.5, knife 80/1, crowbar 80/5, ...) from the catalog; the
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

    @staticmethod
    def _initial_reserve(profile) -> int:
        """Stock spawn reserve (``Weapon.ammo[3]``), e.g. rifle 30 of 50."""
        initial = int(getattr(profile, "initial_reserve", -1))
        return int(profile.reserve_ammo) if initial < 0 else initial

    def _reset_ammo(self):
        """Grant one fresh per-life wallet for each retail weapon.

        Retail ``Weapon.restock`` (any type but AMMO_CRATE) sets the magazine
        to the initial clip and the reserve to the INITIAL reserve -- not the
        maximum (rifle 30 of 50, classic shotgun 20 of 45).

        Keep the active pair as the compatibility facade used by combat and
        bots. Stowed pairs are saved on selection; selecting is never a grant.
        The bounded catalog also covers Tutorial unlocks and mounted weapons.
        """
        self._weapon_ammo = {
            tool: (profile.clip_size, self._initial_reserve(profile))
            for tool, profile in WEAPON_PROFILES.items()
        }
        profile = WEAPON_PROFILES.get(
            self.weapon,
            WEAPON_PROFILES[next(iter(WEAPON_PROFILES))],
        )
        self.ammo_clip = profile.clip_size
        self.ammo_reserve = self._initial_reserve(profile)

    @staticmethod
    def _crate_reserve(profile, reserve: int) -> int:
        restock = int(getattr(profile, "restock_amount", -1))
        if restock < 0:
            restock = int(profile.reserve_ammo)
        return min(int(reserve) + restock, int(profile.reserve_ammo))

    def _restock_ammo_crate(self) -> None:
        """Retail ``Weapon.restock(AMMO_CRATE)`` on every weapon.

        The crate ADDS the stock restock amount to the reserve, capped at the
        stock maximum; the loaded magazine is untouched (the client then
        auto-reloads an empty held weapon and sends its own WeaponReload).
        """
        for tool, (clip, reserve) in tuple(self._weapon_ammo.items()):
            profile = WEAPON_PROFILES.get(tool)
            if profile is not None:
                self._weapon_ammo[tool] = (clip, self._crate_reserve(profile, reserve))
        profile = WEAPON_PROFILES.get(self.weapon)
        if profile is not None:
            self.ammo_reserve = self._crate_reserve(profile, self.ammo_reserve)
            self._weapon_ammo[self.weapon] = (self.ammo_clip, self.ammo_reserve)

    def _reload_profile(self):
        """The gun whose reload is running (``get_weapon_profile`` facade)."""
        return self.get_weapon_profile()

    def _reload_fire_grace(self, profile) -> float:
        return RELOAD_FIRE_GRACE_SECONDS + RELOAD_FIRE_GRACE_FRACTION * float(
            profile.reload_time
        )

    def _load_reload_cycle(self, profile) -> None:
        """Apply one completed reload cycle (``get_ammo_after_reload``).

        Stock clip_reload weapons (shotguns, snub pistol) load ONE round per
        cycle; every other gun fills the magazine from the reserve.
        """
        needed = max(0, int(profile.clip_size) - int(self.ammo_clip))
        if bool(getattr(profile, "clip_reload", False)):
            needed = min(1, needed)
        loaded = min(needed, int(self.ammo_reserve))
        self.ammo_clip += loaded
        self.ammo_reserve -= loaded

    def _reloadable(self, profile) -> bool:
        """Stock ``Weapon.is_reloadable``: room in the clip and reserve left."""
        return self.ammo_clip < int(profile.clip_size) and self.ammo_reserve > 0

    def _stop_reload(self) -> None:
        self.reloading = False
        self.reload_end_time = 0.0
        # The auto-reload bound belongs to the reload that just ended.
        self._auto_reload_floor = None

    def _advance_reload(self, now: Optional[float] = None) -> bool:
        """Apply every reload cycle due by ``now``; True when reloading ends.

        Retail ``Character.end_reload`` applies the cycle and, while the gun
        is still reloadable and the trigger is not pressed, immediately calls
        ``reload()`` again: a clip_reload gun keeps loading one round per
        ``reload_time`` until full. The next cycle is scheduled from the exact
        end of the previous one, like the client's pullout timer.
        """
        if not self.reloading:
            return False
        current_time = time.monotonic() if now is None else float(now)
        profile = self._reload_profile()
        while self.reloading and current_time >= self.reload_end_time:
            self._load_reload_cycle(profile)
            if (
                bool(getattr(profile, "clip_reload", False))
                and float(profile.reload_time) > 0.0
                and self._reloadable(profile)
            ):
                self.reload_end_time += float(profile.reload_time)
                continue
            self._stop_reload()
            self._reload_done_pending = True
            return True
        return False

    def _advance_reload_and_announce(self) -> None:
        """Tick the reload and relay its completion (WeaponReload is_done=1).

        Remote clients' ``Character.receive_reload`` only plays the weapon's
        reload / reload-done sound for the flag it receives.
        """
        if self.reloading:
            self._advance_reload()
        if getattr(self, "_reload_done_pending", False):
            self._reload_done_pending = False
            self._broadcast_reload_state(True)

    def _reload_done_by_label(self, profile, loop) -> bool:
        """Whether the client's own frame labels prove its reload finished.

        The reload timer starts when WeaponReload(76) ARRIVES. That packet is
        reliable and carries no frame label: when its datagram is lost it
        arrives one retransmission time-out (often 0.3-1 s) late, the server
        reload ends that much after the client's, and the first shots after
        the client's reload were dropped silently ("bullets don't register").

        For the automatic reload that follows the round that emptied the
        magazine, the stock client cannot start reloading before that
        round's weapon_shoot animation ends (``shoot_interval`` after the
        shot, ``Character.update_alive``), so ``shot label + interval`` is a
        hard lower bound of the reload's start frame. A shot labelled at
        least ``reload_time`` after that bound is one the stock client could
        fire; forging labels gains nothing over the stock minimum cycle, and
        ``action_clock`` still caps the sustained rate.
        """
        floor = getattr(self, "_auto_reload_floor", None)
        if floor is None or loop is None:
            return False
        tool, floor_label = floor
        if int(tool) != int(getattr(self, "tool", -1)):
            return False
        if bool(getattr(profile, "clip_reload", False)) and self.ammo_clip > 0:
            # A shell-by-shell gun with rounds loaded already fires mid-chain;
            # with none, the label gap proves the first shell went in.
            return False
        if self.ammo_reserve <= 0:
            return False
        if not action_clock.label_plausible(self, loop):
            return False
        try:
            gap = int(loop) - int(floor_label)
        except (TypeError, ValueError, OverflowError):
            return False
        # One frame of slack for the client's dt-accumulating timers.
        return gap >= action_clock.interval_frames(float(profile.reload_time)) - 1

    def _note_auto_reload_floor(self, loop, interval: float) -> None:
        """Remember the earliest frame the auto reload of this gun can start."""
        if self.ammo_clip > 0 or loop is None or self.is_bot:
            self._auto_reload_floor = None
            return
        if not action_clock.label_plausible(self, loop):
            self._auto_reload_floor = None
            return
        self._auto_reload_floor = (
            int(getattr(self, "tool", -1)),
            int(loop) + action_clock.interval_frames(float(interval)),
        )

    def _reload_blocks_shot(
        self, current_time: float, *, commit: bool, loop=None
    ) -> bool:
        """Whether a running reload rejects a shot at ``current_time``.

        Retail ``Weapon.use_primary`` refuses while ``character.reloading``,
        but pressing fire makes ``end_reload`` stop a clip_reload chain after
        the current round, so a shotgun can fire mid-reload with the rounds
        loaded so far. A shot within the jitter grace of the current cycle's
        end completes that cycle first; any other shot during a clip_reload
        cycle interrupts the chain (the partial round is not loaded). A
        magazine reload still blocks the trigger until it completes.
        """
        if not self.reloading:
            return False
        profile = self._reload_profile()
        remaining = float(self.reload_end_time) - float(current_time)
        if (
            remaining <= self._reload_fire_grace(profile)
            or self._reload_done_by_label(profile, loop)
        ):
            if commit:
                self._load_reload_cycle(profile)
                self._stop_reload()
                self._auto_reload_floor = None
            return False
        if bool(getattr(profile, "clip_reload", False)) and self.ammo_clip > 0:
            if commit:
                self._stop_reload()
            return False
        return True

    def _fire_lane(self) -> str:
        """Share a cooldown across mouse buttons and tool changes."""
        return "fire"

    def _fire_interval(self, fire_interval: Optional[float]) -> float:
        if fire_interval is not None:
            return float(fire_interval)
        return float(self.get_weapon_profile().fire_interval)

    def can_fire(
        self,
        now: Optional[float] = None,
        fire_interval: Optional[float] = None,
        *,
        loop=None,
    ) -> bool:
        """Whether a shot/swing may happen now.

        ``loop`` is the client frame label of the ShootPacket. With it the
        cadence is measured on the client's clock (server/action_clock.py);
        without it (bots, server-side callers) on arrival time as before.
        """
        if not self.alive or not self.spawned:
            return False

        current_time = time.monotonic() if now is None else now
        self._advance_reload(current_time)
        if self._reload_blocks_shot(current_time, commit=False, loop=loop):
            return False
        if not self.is_bot:
            if not action_clock.peek(
                self, self._fire_lane(), label=loop,
                interval=self._fire_interval(fire_interval), now=current_time,
                grace=FIRE_RATE_GRACE,
            ):
                return False
        # Admit up to one tick of arrival jitter against a stable cadence
        # schedule. The grace is never subtracted from every accepted interval,
        # which would permanently raise the weapon's sustained fire rate.
        elif current_time + FIRE_RATE_GRACE < self.next_shot_time:
            return False

        if self.is_spade_tool():
            return True
        if not self.is_weapon_tool():
            return False
        if self.ammo_clip > 0:
            return True
        # An empty clip whose reload cycle ends within the jitter grace, or
        # whose end the client's frame labels prove (_reload_done_by_label).
        return bool(
            self.reloading
            and self.ammo_reserve > 0
            and (
                float(self.reload_end_time) - current_time
                <= self._reload_fire_grace(self._reload_profile())
                or self._reload_done_by_label(self._reload_profile(), loop)
            )
        )

    def consume_shot(
        self,
        now: Optional[float] = None,
        fire_interval: Optional[float] = None,
        *,
        loop=None,
    ) -> bool:
        if not self.can_fire(now, fire_interval=fire_interval, loop=loop):
            return False

        current_time = time.monotonic() if now is None else now
        if self.is_weapon_tool():
            self._reload_blocks_shot(current_time, commit=True, loop=loop)
            if self.ammo_clip <= 0:
                return False
        profile = self.get_weapon_profile()
        interval = profile.fire_interval if fire_interval is None else float(fire_interval)
        if not self.is_bot:
            if not action_clock.admit(
                self, self._fire_lane(), label=loop, interval=interval,
                now=current_time, grace=FIRE_RATE_GRACE,
            ):
                return False
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
            self._note_auto_reload_floor(loop, interval)
        return True

    def start_reload(self, now: Optional[float] = None) -> bool:
        """Accept one retail ``Character.reload`` request (WeaponReload 76).

        The client sends ``is_done=0`` from every ``reload()`` call, i.e.
        once per round of a clip_reload chain. A request that arrives while
        that chain is still running is acknowledged (True: the caller relays
        the round's reload sound) without restarting the cycle.
        """
        if not self.alive or not self.spawned or not self.is_weapon_tool():
            return False

        current_time = time.monotonic() if now is None else now
        if self.tool_is_raw and int(self.tool) not in WEAPON_PROFILES:
            # A launcher (RPGWeapon & co.) is a stock Weapon, so its
            # Character.reload sends WeaponReload like any gun. WEAPON_TOOL_IDS
            # lists it, but its rounds live in oriented_stock and its clip is
            # inferred from time (_launcher_clip_left). get_weapon_profile()
            # would fall back to the last-held GUN and start THAT reload,
            # moving gun reserve into the server's gun clip behind the
            # client's back. Only acknowledge the request for relay.
            return self.launcher_reload_relayable(current_time)
        profile = self.get_weapon_profile()
        if self.reloading:
            self._advance_reload(current_time)
        if self.reloading:
            return bool(getattr(profile, "clip_reload", False))
        if not self._reloadable(profile):
            return False

        self.reloading = True
        self.reload_end_time = current_time + profile.reload_time
        return True

    def finish_reload(self) -> bool:
        """Complete the running reload cycle now and end the reload."""
        if not self.reloading:
            return False

        self._load_reload_cycle(self.get_weapon_profile())
        self._stop_reload()
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

    def spawn_protection_remaining(self) -> float:
        """Seconds of spawn protection left (0 once this life attacked)."""

        if not self.alive or self.spawn_protection_cancelled:
            return 0.0
        server = self.connection.server if self.connection else None
        config = getattr(server, "config", None)
        if config is None:
            return 0.0
        from server.game_rules import get_rules

        duration = float(get_rules(config).get("RULE_SPAWN_PROTECTION_TIME"))
        cap = getattr(self, "spawn_protection_cap", None)
        if cap is not None:
            duration = min(duration, float(cap))
        if duration <= 0.0:
            return 0.0
        return max(0.0, duration - (time.monotonic() - self.spawned_at))

    def end_spawn_protection(self) -> bool:
        """Drop spawn protection because this life attacked.

        The WorldUpdate timer (``world_update_snapshot``) reads the same
        state, so every client's protection effect ends on the next row.
        Returns True when protection was active.
        """

        if self.spawn_protection_remaining() <= 0.0:
            return False
        self.spawn_protection_cancelled = True
        return True

    def damage(
        self,
        amount: int,
        source: Optional["Player"] = None,
        kill_type: int = 0,
        *,
        hp_damage_type: Optional[int] = None,
    ) -> bool:
        """Apply ``amount`` HP of damage and tell the victim with SetHP.

        ``hp_damage_type`` overrides SetHP.damage_type. The stock
        process_packet_set_hp (gameScene.pyd 0x10191E90) reads it as
        0 = plain HP update, 1 = hit (sound + direction indicator from the
        source position), 2 = heal, 3 = burn (burn sound + BURN_INDICATOR),
        4 = sudden death (SUDDEN_DEATH_INDICATOR). The default picks 1 for
        another player's damage and 0 for world/self damage.
        """
        if not self.alive:
            return False
        if self.god_mode:
            return False

        server = self.connection.server if self.connection else None
        # A teammate who set off this player's own deployable (shot their
        # landmine) owns the resulting damage: kill feed, team-kill scoring
        # and grief accounting name the instigator, not a "suicide".
        source = conduct.attribute_damage_source(server, self, source)
        rules = None
        config = getattr(server, "config", None)
        if config is not None:
            from server.game_rules import get_rules

            rules = get_rules(config)
            if (
                source is not None
                and source is not self
                and self.spawn_protection_remaining() > 0.0
            ):
                return False
            if source is not None and source is not self:
                # Dealing damage (melee, blasts, fire, turrets) ends the
                # attacker's own spawn protection.
                end_protection = getattr(source, "end_spawn_protection", None)
                if callable(end_protection):
                    end_protection()
        modify_damage = getattr(
            getattr(server, "mode", None), "modify_incoming_damage", None
        )
        if callable(modify_damage):
            amount = modify_damage(self, amount, source, kill_type)

        if rules is not None and source is not None and source is not self:
            amount = float(amount) * float(rules.get("RULE_WEAPON_DAMAGE"))
            mode_code = str(getattr(config, "default_mode", "")).lower()
            if mode_code in ("zom", "zombie") and int(
                getattr(source, "class_id", -1)
            ) in {
                int(C.CLASS_ZOMBIE),
                int(C.CLASS_FAST_ZOMBIE),
                int(C.CLASS_JUMP_ZOMBIE),
            }:
                amount *= float(rules.get("RULE_ZOMBIE_CLASS_DAMAGE"))
            # Stock server-only ONE_HIT_KILL_WEAPONS (A2382): only these
            # kill types become instakills (not burn ticks, sticky, GL,
            # mines, C4, chemical goo...). Rules audit 2026-09-27 #22.
            if rules.enabled("RULE_ONE_HIT_KILL") and int(kill_type) in _ONE_HIT_KILL_TYPES:
                amount = max(float(amount), float(self.health))

        # Pauses jetpack fuel regen for the type's refill-delay window.
        self._last_damage_at = time.time()

        amount = max(0, int(round(amount)))
        if amount <= 0:
            return False

        health_before = self.health
        self.health = max(0, self.health - amount)
        from server.combat_scores import record_damage, record_damage_taken
        record_damage(server, self, source, health_before - self.health)
        record_damage_taken(
            server, self, source, health_before - self.health, int(kill_type)
        )
        from server import achievements
        achievements.damaged(
            server, self, source, health_before - self.health, int(kill_type)
        )
        conduct.record_team_harm(
            server,
            self,
            source,
            health_before - self.health,
            self.health <= 0,
            int(kill_type),
        )
        source_position = self.position if source is None else source.position
        # World damage (falls, mode DoT) does not erase the last player
        # interaction: a fall right after an enemy hit is still credited to
        # that enemy (rules audit 2026-09-27 #7).
        if source is not None:
            self._last_combat_damage_at = time.monotonic()
            self._last_damage_source_id = int(getattr(source, "id", -1))
            self._last_damage_source_position = tuple(
                float(value) for value in source_position
            )
        damage_type = 0 if source is None or source == self else 1
        if hp_damage_type is not None:
            damage_type = int(hp_damage_type)
        # A gated (still loading) client gets its HP in the join reveal;
        # a mid-handshake SetHP reaches no GameScene.
        if self.connection and getattr(self.connection, "in_game", True):
            from shared.packet import SetHP

            packet = SetHP()
            packet.hp = max(0, min(255, int(self.health)))
            packet.damage_type = damage_type
            packet.source_x, packet.source_y, packet.source_z = source_position
            self.connection.send(bytes(packet.generate()))

        if self.health <= 0:
            killer = source
            if source is None and int(kill_type) == int(C.KILL.FALL_KILL):
                # Knocked or blasted into a lethal fall: the enemy who last
                # damaged this life within PLAYER_INTERACTION_EXPIRY_SECONDS
                # (A100 = 5 s, stock server-only) gets the kill instead of
                # the victim being charged a suicide.
                from server.handlers.team import recent_enemy_attacker

                killer = recent_enemy_attacker(server, self)
            self.die(killer=killer, kill_type=kill_type)
            return True
        return False

    def _simulate_dead_corpse_tick(self, dt: float) -> None:
        """Advance the retail native mover during a jetpack death fuse."""

        if self.alive or dt <= 0.0:
            return
        world_object = self._ensure_world_object()
        if world_object is None:
            return
        try:
            world_object.update(
                float(dt),
                self._build_player_collision_positions(),
            )
            self._sync_cached_vectors()
        except Exception:
            logger.debug("dead corpse physics failed", exc_info=True)

    def _spawn_death_grave(self) -> None:
        """Create the stock falling GraveEntity at the corpse's live state."""

        server = self.connection.server if self.connection else None
        if server is None:
            return
        reg = getattr(server, "entity_registry", None)
        if reg is None or not getattr(server.config, "entities_wire_ready", False):
            return
        existing = reg.get(self._grave_entity_id)
        if existing is not None and bool(getattr(existing, "alive", False)):
            return

        try:
            from server.entities.behaviors import GraveBehavior
            from server.game_rules import get_rules

            grave_x, grave_y, grave_z = self.position
            grave_velocity = tuple(float(value) for value in self.velocity)
            world = getattr(server, "world_manager", None)
            explosion_center = (grave_x, grave_y, grave_z)
            if world is not None:
                explosion_center = world.dry_surface_anchor(
                    grave_x,
                    grave_y,
                    search=0,
                )
            team = server.teams.get(self.team)
            grave_color = tuple(team.color) if team is not None else None
            corpse_explosion = get_rules(server.config).enabled(
                "RULE_ENABLE_CORPSE_EXPLOSION"
            )
            grave = reg.place(
                int(getattr(C, "GRAVE_ENTITY", 11)),
                grave_x,
                grave_y,
                grave_z,
                state=int(self.team),
                color=grave_color,
                kind="grave",
                player_id=self.id,
                vel=grave_velocity,
                behavior=GraveBehavior(
                    thrower_id=self.id,
                    fuse=float(getattr(C, "GRAVE_EXPLOSION_FUSE", 7.0)),
                    damage=(
                        float(getattr(C, "GRAVE_EXPLOSION_DAMAGE", 25.0))
                        if corpse_explosion
                        else 0.0
                    ),
                    block_damage=(
                        float(
                            getattr(C, "GRAVE_EXPLOSION_BLOCK_DAMAGE", 3.0)
                        )
                        if corpse_explosion
                        else 0.0
                    ),
                    blast_radius=(
                        float(getattr(C, "GRAVE_EXPLOSION_RADIUS", 3.0))
                        if corpse_explosion
                        else 0.0
                    ),
                    kill_type=int(getattr(C.KILL, "GRAVE_KILL", 13)),
                    explosion_center=explosion_center,
                ),
            )
            self._grave_entity_id = grave.entity_id
            server.broadcast_create_entity(grave)
        except Exception:
            logger.debug("grave spawn failed", exc_info=True)

    def die(self, killer: Optional["Player"] = None, kill_type: int = 0):
        # Idempotent per death: guard on `alive` alone. The old
        # `not alive and not spawned` let a death slip through for an
        # already-dead-but-still-spawned edge case (double death bookkeeping).
        if not self.alive:
            return

        server = self.connection.server if self.connection else None
        mode = getattr(server, "mode", None)
        # Achievements judge the weapon that killed, not the mode's
        # presentation type below; and the jetpack before death clears it.
        weapon_kill_type = int(kill_type)
        was_jetpacking = bool(getattr(self, "jetpack_active", False))
        death_kill_type = getattr(mode, "death_kill_type_for", None)
        if callable(death_kill_type):
            # Some native modes use a dedicated death transition. VIP_MODE_KILL
            # is not merely a score label: retail uses it for the boss-death
            # presentation and no-respawn state.
            kill_type = int(death_kill_type(self, killer, int(kill_type)))

        self.alive = False
        self.spawned = False
        self.grounded = False
        self.airborne = False
        self.wade = False
        self.disguised = False
        self.touching_goo = False
        self.death_time = time.time()
        transition_death = kill_type in {
            C.FORCED_TEAM_CHANGE_KILL, C.TEAM_CHANGE_KILL, C.CLASS_CHANGE_KILL
        }
        if not transition_death:
            self.deaths += 1
        self.kill_streak = 0
        # "Reloading Kill" (GENERIC_SCORE_RELOAD) needs the state at death.
        self.died_reloading = bool(getattr(self, "reloading", False))
        self.reloading = False
        self.reload_end_time = 0.0
        self.jetpack_active = False
        self.parachute_active = False
        # Death closes the retail canopy client-side too (0x1003393B).
        self._reset_parachute_state()
        self._jetpack_physics_active = False
        self._jetpack_activation_defer_remaining = 0
        self._jetpack_exhaustion_tail_remaining = 0
        self._jetpack_requires_release = False
        self._pending_velocity_impulses.clear()
        self._pending_explosion_impulses.clear()

        world_object = self._ensure_world_object()
        if world_object is not None:
            world_object.set_dead(True)
            # KillAction removes player control but preserves momentum. Keep
            # the server's one-second jetpack corpse prediction on the same
            # uncontrolled native branch as the retail Character.
            world_object.set_walk(False, False, False, False)
            world_object.jump = False
            world_object.sneak = False
            world_object.sprint = False
            world_object.hover = False
            world_object.jetpack_active = False
            world_object.jetpack_passive = False
            world_object.parachute_active = False
            self._sync_cached_vectors()

        from server import kill_feed

        # KillAction.kill_count is the retail multikill chain (kills no more
        # than MULTIKILLMAXTIMEGAP apart), not the life streak; the flags are
        # the domination/revenge pair (server/kill_feed.py).
        kill_count = 0
        is_domination = is_revenge = False
        kill_feed.reset_multikill(self)
        if killer and killer != self and killer.team != self.team and not transition_death:
            killer.kills += 1
            killer.kill_streak = min(255, int(killer.kill_streak) + 1)
            kill_count = kill_feed.register_multikill(killer, time.monotonic())
            is_domination, is_revenge = kill_feed.register_domination(
                killer, self
            )
            # Retail REVENGE (kill whoever dominates you) and PAYBACK (kill the
            # player who last killed you) bonus popups, paid with the kill
            # score by BaseMode.award_generic_kill_score.
            payback = int(getattr(killer, "last_killed_by_id", -1)) == int(self.id)
            bonuses = getattr(killer, "kill_bonuses", None)
            if not isinstance(bonuses, dict):
                bonuses = {}
                killer.kill_bonuses = bonuses
            bonuses[int(self.id)] = (bool(is_revenge), bool(payback))
            if payback:
                killer.last_killed_by_id = -1
            self.last_killed_by_id = int(killer.id)
        elif int(kill_type) in kill_feed.DOMINATION_RESET_KILL_TYPES:
            # The client clears this player's domination flags on the same
            # packet; keep the server relation in step.
            kill_feed.clear_player(server, self)

        from server.profile_stats import death
        death(self, killer, int(kill_type))
        from server.combat_scores import record_death
        record_death(
            server, self, killer, int(kill_type), domination=is_domination
        )
        from server import achievements
        achievements.died(server, self, killer, weapon_kill_type, was_jetpacking)

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
            # Round a fractional mode delay up so the countdown never reaches
            # zero before the server respawns the player.
            packet.respawn_time = max(
                0, min(255, int(math.ceil(float(respawn_time) - 1e-6)))
            )
            packet.kill_count = kill_count
            packet.isDominationKill = int(bool(is_domination))
            packet.isRevengeKill = int(bool(is_revenge))
            death_data = bytes(packet.generate())
            # Roster repair replays this packet to late joiners, and a joiner
            # can inherit the killer's freed id. The replay only has to
            # establish the death, never re-announce banners.
            packet.kill_count = 0
            packet.isDominationKill = 0
            packet.isRevengeKill = 0
            self.last_kill_action_data = bytes(packet.generate())
            server.broadcast(death_data)

            corpse_lifecycle = getattr(server, "corpse_lifecycle", None)
            on_player_death = getattr(corpse_lifecycle, "on_player_death", None)
            uses_classic_corpse = bool(
                callable(on_player_death) and on_player_death(self)
            )

            from server.game_rules import get_rules
            grave_enabled = get_rules(server.config).enabled(
                "RULE_ENABLE_GRAVESTONES"
            )
            if not uses_classic_corpse:
                schedule_normal = getattr(
                    corpse_lifecycle,
                    "schedule_normal_death",
                    None,
                )
                if callable(schedule_normal):
                    schedule_normal(self, grave_enabled=grave_enabled)

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
                and not transition_death
            ):
                server.queue_mode_event("on_player_kill", killer, self, kill_type)

        if server is not None and not transition_death:
            react = getattr(getattr(server, "bots", None), "on_player_killed", None)
            if callable(react):
                react(self, killer if killer is not None else self, kill_type)

        logger.debug("Player %s died (killer: %s)", self.name, killer.name if killer else "none")

    def heal(self, amount: int):
        if self.alive:
            health_before = self.health
            cap = max(1, int(getattr(self, "max_health", MAX_HEALTH)))
            # Never heal below the current HP (a heal is not damage).
            self.health = max(health_before, min(cap, self.health + amount))
            from server.combat_scores import record_healing
            record_healing(self, self.health - health_before)
            if self.connection and getattr(self.connection, "in_game", True):
                from shared.packet import SetHP

                packet = SetHP()
                packet.hp = max(0, min(255, int(self.health)))
                packet.damage_type = 2
                packet.source_x = 0.0
                packet.source_y = 0.0
                packet.source_z = 0.0
                self.connection.send(bytes(packet.generate()))

    def restock_ammo(self, restock_type: int = 0):
        """Restock gun ammo (server-side) and tell the client to restock its
        own counters via Restock(69).

        ``type=0`` is the full spawn restock used by RoundLifecycle (initial
        clip + initial reserve); AMMO_CRATE adds each gun's stock restock
        amount to its reserve, capped at the maximum. A physical
        ammo crate must pass ``AMMO_CRATE`` (3); the retail Character receiver
        treats zero as a general restock and also restores health.
        """
        if not self.alive:
            return
        disguise_before = int(getattr(self, "disguise_stock", 0))
        if int(restock_type) == int(getattr(C, "AMMO_CRATE", 3)):
            # Retail Weapon.restock(AMMO_CRATE): reserve += restock amount
            # (capped at the stock max); the magazine is kept.
            self._restock_ammo_crate()
        else:
            self._reset_ammo()
        self.rocket_turret_stock = min(
            int(getattr(C, "ROCKET_TURRET_STOCK", 4)),
            int(getattr(self, "rocket_turret_stock", 0))
            + int(getattr(C, "ROCKET_TURRET_RESTOCK_AMOUNT", 2)),
        )
        # A stock ammo crate calls restock(AMMO_CRATE) on every equipped
        # weapon/tool in the client: each oriented stock is TOPPED UP
        # (min(count + restock, max)), cadence and the loaded launcher clip
        # are kept. Every other restock type is the per-life reset.
        if int(restock_type) == int(getattr(C, "AMMO_CRATE", 3)):
            self._restock_oriented_crate()
        else:
            self._reset_equipment_state()
        # RoundLifecycle and bot creation send type zero immediately after
        # spawn. New-life deployable stock was already granted by spawn();
        # that notification must not add a second set of consumable items.
        if int(restock_type) != 0:
            restock_deployable_inventory(self)
            # Retail crate restock: +DISGUISE_RESTOCK_AMOUNT (3), capped at 3,
            # rather than resetting to the spawn stock of 2.
            restock = int(getattr(C, "DISGUISE_RESTOCK_AMOUNT", 3))
            self.disguise_stock = min(restock, disguise_before + restock)
        if self.connection:
            from shared.packet import Restock
            pkt = Restock()
            pkt.player_id = self.id
            pkt.type = int(restock_type)
            self.connection.send(bytes(pkt.generate()))

    def add_blocks(self, count: int = 1):
        self.blocks = min(self._block_wallet_max(), self.blocks + count)

    def _block_wallet_multiplier(self) -> float:
        """Return the Match Lobby wallet scale for this authoritative body."""

        server = self.connection.server if self.connection else None
        config = getattr(server, "config", None)
        if config is None:
            return 1.0
        from server.game_rules import get_rules

        return float(get_rules(config).get("RULE_CHARACTER_BLOCK_WALLETS"))

    def _block_wallet_max(self) -> int:
        return max(0, int(round(
            self.movement_profile.max_blocks * self._block_wallet_multiplier()
        )))

    def _block_wallet_start(self) -> int:
        return min(self._block_wallet_max(), max(0, int(round(
            self.movement_profile.starting_blocks * self._block_wallet_multiplier()
        ))))

    def restock_blocks(self):
        """Block-crate pickup: refill the block wallet to the class max and
        tell the client — Restock(69) with type=5 is the block refill (client
        sets its local block_count to max; measured live 2026-07-07)."""
        if not self.alive:
            return
        self.add_blocks(self._block_wallet_max())
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
        server = self.connection.server if self.connection else None
        if server is not None:
            # TeamInfiniteBlocks(82): the client skips its wallet checks.
            from server.hud_packets import team_infinite_blocks

            if team_infinite_blocks(server, getattr(self, "team", -1)):
                return True
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
        self.oriented_stock = {
            tool: int(magazine_initial) + int(reserve_initial)
            for tool, (_mag_max, magazine_initial, _reserve_max,
                       reserve_initial, _restock) in ORIENTED_STOCK_AMMO.items()
        }
        self.grenades = self.oriented_stock[int(C.GRENADE_TOOL)]
        self._oriented_next_use = {}
        # launcher tool -> [rounds left in the clip, monotonic last use]
        self._launcher_rounds = {}
        self.disguise_stock = int(getattr(C, "DISGUISE_INITIAL_STOCK", 2))
        self._disguise_next_use = 0.0

    def _restock_oriented_crate(self, now: Optional[float] = None) -> None:
        """Retail ``restock(AMMO_CRATE)`` on every oriented tool.

        Tops each stock up (never resets it) and keeps cadence and the loaded
        launcher clip, exactly as the stock client (and the native
        ``WeaponReplicationState::restock_from_ammo_crate``) predicts. The
        server keeps one combined magazine+reserve total per launcher, so the
        magazine part is taken from the inferred clip state.
        """
        current_time = time.monotonic() if now is None else float(now)
        stock = getattr(self, "oriented_stock", None)
        if not isinstance(stock, dict):
            self._reset_equipment_state()
            return
        for tool, (mag_max, _mag_init, reserve_max, _reserve_init,
                   restock) in ORIENTED_STOCK_AMMO.items():
            total = max(0, int(stock.get(tool, 0)))
            if reserve_max <= 0:
                stock[tool] = min(total + int(restock), int(mag_max))
                continue
            clip_left = self._launcher_clip_left(tool, current_time)
            magazine = min(total, int(mag_max) if clip_left is None else int(clip_left))
            reserve = total - magazine
            stock[tool] = magazine + min(reserve + int(restock), int(reserve_max))
        self.grenades = stock.get(int(C.GRENADE_TOOL), self.grenades)

    # Retail launchers with a clip: an emptied clip auto-reloads before the
    # next round. UGCDrillgunWeapon reloads like the drill (its
    # get_ammo_after_reload returns (1, 1), so only its reserve is endless);
    # UGCRPG2Weapon never spends a round (use_an_ammo is `pass`) and is
    # limited by its shoot_interval alone.
    _RELOADING_LAUNCHERS = frozenset(
        int(getattr(C, name))
        for name in (
            "RPG_TOOL",
            "RPG2_TOOL",
            "DRILLGUN_TOOL",
            "GRENADE_LAUNCHER_WEAPON_TOOL",
            "MINE_LAUNCHER_TOOL",
            "UGC_DRILLGUN_TOOL",
        )
        if hasattr(C, name)
    )

    # Stock ``clip_reload = True`` launchers (RPG2Weapon only).
    _CLIP_RELOAD_LAUNCHERS = frozenset((int(C.RPG2_TOOL),))

    @staticmethod
    def _launcher_timing(tool: int) -> Optional[tuple[int, float]]:
        """(clip size, minimum seconds from an emptying shot to the next)."""
        from server.game_constants import WEAPON_CATALOG

        profile = WEAPON_CATALOG.get(int(tool))
        if profile is None:
            return None
        clip = int(getattr(profile, "clip_size", 0) or 0)
        reload_time = float(getattr(profile, "reload_time", 0.0) or 0.0)
        if clip <= 0 or reload_time <= 0.0:
            return None
        interval = max(0.0, float(getattr(profile, "fire_interval", 0.0) or 0.0))
        grace = LAUNCHER_RELOAD_GRACE_SECONDS + (
            LAUNCHER_RELOAD_GRACE_FRACTION * reload_time
        )
        return clip, max(interval, interval + reload_time - grace)

    def _launcher_clip_left(self, tool: int, current_time: float) -> Optional[int]:
        """Rounds the retail clip holds at ``current_time`` (None: no clip).

        Launcher WeaponReload packets are only relayed (start_reload), so a
        reload is inferred from time: any gap long enough to reload refills
        the clip.
        This also covers a manual reload of a partly used RPG2 clip.
        """
        if tool not in self._RELOADING_LAUNCHERS or bool(
            getattr(self, "is_bot", False)
        ):
            return None
        timing = self._launcher_timing(tool)
        if timing is None:
            return None
        clip, reload_gap = timing
        rounds = getattr(self, "_launcher_rounds", None)
        state = rounds.get(tool) if isinstance(rounds, dict) else None
        if state is None:
            return clip
        left, last_use = state
        gap = current_time - float(last_use)
        if gap < reload_gap:
            return int(left)
        if tool in self._CLIP_RELOAD_LAUNCHERS:
            # Stock ``clip_reload`` (RPG2): each reload cycle loads ONE round
            # (``reload_time`` apart), so a gap refills only the cycles that
            # fit rather than the whole clip.
            from server.game_constants import WEAPON_CATALOG

            reload_time = float(WEAPON_CATALOG[int(tool)].reload_time)
            cycles = 1 + int((gap - reload_gap) // max(reload_time, 1e-6))
            return min(clip, int(left) + cycles)
        return clip

    def launcher_reload_relayable(self, now: Optional[float] = None) -> bool:
        """Whether a WeaponReload for the held launcher is a stock reload.

        Stock ``Character.reload`` only runs while ``Weapon.is_reloadable``:
        room in the clip and a round left in the reserve. The server's clip
        model is time-inferred, so this only gates the relay of the reload
        sound to other clients; no server state changes.
        """
        tool = int(getattr(self, "tool", -1))
        if tool not in self._RELOADING_LAUNCHERS or not self.tool_is_raw:
            return False
        current_time = time.monotonic() if now is None else float(now)
        timing = self._launcher_timing(tool)
        clip_left = self._launcher_clip_left(tool, current_time)
        if timing is None or clip_left is None:
            return False
        clip = int(timing[0])
        stock = getattr(self, "oriented_stock", None)
        if not isinstance(stock, dict):
            return False
        if tool not in stock:
            # UGC drill: an endless reserve (get_ammo_after_reload (1, 1)).
            return int(clip_left) < clip
        return int(clip_left) < clip and int(stock[tool]) > int(clip_left)

    def can_use_oriented_item(
        self,
        tool: int,
        now: Optional[float] = None,
        *,
        report_violation: bool = True,
    ) -> bool:
        """Validate cadence and authoritative ammo for packet 10.

        This is a read-only preflight. The handler consumes inventory only
        after the projectile has passed framing/float validation and has been
        registered successfully, so malformed packets cannot eat valid ammo.
        """
        tool = int(tool)
        current_time = time.monotonic() if now is None else float(now)
        if tool in (int(C.DYNAMITE_TOOL), int(C.LANDMINE_TOOL)):
            return deployable_ready(self, tool, current_time)
        if current_time + ORIENTED_CADENCE_GRACE < self._oriented_next_use.get(tool, 0.0):
            return False
        if self._launcher_clip_left(tool, current_time) == 0:
            # The clip is empty and no reload fits since the emptying shot:
            # the stock client cannot fire here (reload skip).
            if report_violation:
                state = self._launcher_rounds.get(tool)
                connection = getattr(self, "connection", None)
                anticheat.report(
                    getattr(connection, "server", None),
                    self,
                    "launcher_reload_skip",
                    tool=tool,
                    since=round(current_time - float(state[1]), 3)
                    if state else None,
                )
            return False
        if tool in (
            int(getattr(C, "SNOWBLOWER_TOOL", 29)),
            int(getattr(C, "UGC_SNOWBLOWER_TOOL", 48)),
        ):
            # UGCSnowBlowerWeapon.use_an_ammo is deliberately a no-op; the
            # editor's only limit is the client's global BlockManager capacity.
            if tool == int(getattr(C, "UGC_SNOWBLOWER_TOOL", 48)):
                return True
            # Stock SnowBlowerWeapon.get_has_enough_ammo honours
            # TeamInfiniteBlocks(82) like the block tool.
            return self._team_infinite_blocks() or int(self.blocks) > 0
        return int(self.oriented_stock.get(tool, 1)) > 0

    def _team_infinite_blocks(self) -> bool:
        connection = getattr(self, "connection", None)
        server = getattr(connection, "server", None) if connection else None
        if server is None:
            return False
        from server.hud_packets import team_infinite_blocks

        return bool(team_infinite_blocks(server, getattr(self, "team", -1)))

    def consume_oriented_item(self, tool: int,
                              now: Optional[float] = None) -> bool:
        """Commit one successfully spawned oriented projectile."""
        tool = int(tool)
        current_time = time.monotonic() if now is None else float(now)
        if not self.can_use_oriented_item(
            tool, current_time, report_violation=False
        ):
            return False
        clip_left = self._launcher_clip_left(tool, current_time)
        if clip_left is not None:
            rounds = getattr(self, "_launcher_rounds", None)
            if not isinstance(rounds, dict):
                rounds = {}
                self._launcher_rounds = rounds
            rounds[tool] = [max(0, clip_left - 1), current_time]

        if tool in (int(C.DYNAMITE_TOOL), int(C.LANDMINE_TOOL)):
            commit_deployable_use(self, tool, current_time)
            return True

        if tool in (
            int(getattr(C, "SNOWBLOWER_TOOL", 29)),
            int(getattr(C, "UGC_SNOWBLOWER_TOOL", 48)),
        ):
            if tool != int(getattr(C, "UGC_SNOWBLOWER_TOOL", 48)):
                # Free under TeamInfiniteBlocks(82), like remove_block().
                if not self._team_infinite_blocks():
                    self.blocks = max(0, int(self.blocks) - 1)
        elif tool in self.oriented_stock:
            self.oriented_stock[tool] = max(0, self.oriented_stock[tool] - 1)
            if tool == int(C.GRENADE_TOOL):
                self.grenades = self.oriented_stock[tool]

        from server.game_constants import WEAPON_CATALOG
        profile = WEAPON_CATALOG.get(tool)
        interval = float(profile.fire_interval) if profile is not None else 0.0
        # Schedule from the earlier due time when a held trigger's packet
        # arrives inside the jitter grace, so the grace never raises the
        # sustained rate above one round per shoot_interval.
        previous_due = float(self._oriented_next_use.get(tool, 0.0))
        self._oriented_next_use[tool] = max(current_time, previous_due) + max(0.0, interval)
        return True

    def set_tool(self, tool: int, raw: Optional[bool] = None):
        if raw is None:
            raw = (
                tool in BLOCK_TOOL_IDS
                or tool in SPADE_TOOL_IDS
                or tool in GRENADE_TOOL_IDS
                or tool in WEAPON_TOOL_IDS
            )
        if tool != self.tool or raw != self.tool_is_raw:
            # Retail Tool.on_unset cancels the outgoing reload, including a
            # gun-to-gun switch. Repeated ClientData for one tool does not.
            self.reloading = False
            self.reload_end_time = 0.0
            if int(tool) == int(C.MG_TOOL) and int(self.tool) != int(C.MG_TOOL):
                # Remember the hand tool so an MG unmount can restore it.
                self._tool_before_mg = int(self.tool)
        self.tool = tool
        self.tool_is_raw = raw
        if raw and tool in WEAPON_PROFILES:
            if tool != self.weapon:
                self._weapon_ammo[self.weapon] = (self.ammo_clip, self.ammo_reserve)
                self.weapon = tool
                self.ammo_clip, self.ammo_reserve = self._weapon_ammo[tool]
        if not self.is_weapon_tool():
            self.reloading = False
            self.reload_end_time = 0.0

    def ensure_legal_tool(self) -> Optional[int]:
        """Replace a held tool this life may no longer hold.

        Returns the tool switched to, or ``None`` when the current tool is
        still authorized (or nothing legal exists, in which case it is kept).
        Candidates: the tool held before mounting an MG, the active weapon,
        then the committed loadout in order.
        """
        from server.class_selection import equipped_tool_authorized

        if not self.alive or not self.spawned:
            return None
        if equipped_tool_authorized(self, int(self.tool)):
            return None
        candidates = [self._tool_before_mg, self.weapon]
        candidates.extend(getattr(self, "loadout", None) or ())
        for candidate in candidates:
            if candidate is None:
                continue
            candidate = int(candidate)
            if candidate == int(self.tool) or candidate == int(C.MG_TOOL):
                continue
            if equipped_tool_authorized(self, candidate):
                self.set_tool(candidate, raw=True)
                return candidate
        return None

    def on_machine_gun_unmounted(self) -> Optional[int]:
        """Hook for ``MachineGunBehavior.unmount``: drop the mounted MG tool.

        Leaving the gun (movement, damage, death) must not leave the server
        believing the player still holds MG_TOOL, which ClientData can no
        longer replace once the mount no longer authorizes it.
        """
        if int(self.tool) != int(C.MG_TOOL):
            self._tool_before_mg = None
            return None
        switched = self.ensure_legal_tool()
        self._tool_before_mg = None
        return switched

    def reset_block_color_for_spawn(self) -> int:
        """Reset the held-block palette to the current team's RGB color.

        Players may still select another palette color while alive. Every new
        life starts from the configured team color so the authoritative voxel
        color and the native held-block preview share the same initial state.
        Spectators and detached test players retain their neutral/current
        color because they have no playable team definition.
        """

        server = self.connection.server if self.connection else None
        team = getattr(server, "teams", {}).get(self.team) if server else None
        color = getattr(team, "color", None)
        if color is None or len(color) < 3:
            return int(self.block_color) & 0xFFFFFF
        red, green, blue = (int(channel) & 0xFF for channel in color[:3])
        self.block_color = (red << 16) | (green << 8) | blue
        return self.block_color

    def set_color(self, color: int) -> None:
        self.block_color = int(color) & 0xFFFFFF

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
        # This row carries WorldUpdate state bit 0x01 to the retail owner;
        # a changed canopy bit starts the physics handoff from here.
        self._note_parachute_owner_row()

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

        self._advance_reload_and_announce()

        self.last_update = time.time()
        self.movement_time += dt
        was_airborne = self.airborne
        # The native mover decides whether held jump can launch. Owner
        # snapshots remain outputs, never guessed position rewind inputs.
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
        self._update_parachute(dt)
        if (
            self.jetpack_active
            or self._jetpack_physics_active
            or self._jetpack_activation_defer_remaining
            or self._jetpack_exhaustion_tail_remaining
            or self.parachute_active
            or self._parachute_physics_active
            or self._parachute_deploy_pending
        ):
            # A later flight release must not reopen an ordinary jump's old
            # replay window. Flight owns its separate urgent handoff rows.
            self.last_retail_jump_loop = None
        self._apply_input_state_to_world(
            trigger_jump=trigger_jump, collisions=positions
        )
        chute_pre_vz = float(world_object.velocity.z)
        chute_physics = bool(self._parachute_physics_active)
        # Keep the original mover's displacement for every client. The retail
        # jump fix already removes Character's extra cache reset; reproducing
        # that reset here fights patched clients and creates a new correction.
        # An absent BSCF flight capability cannot identify an unpatched client.
        result = world_object.update(dt, positions)
        if (
            world_object.jump_this_frame
            and not self.is_bot
            and getattr(self.connection, "flight_profile_capable", None) is False
            and not self.jetpack_active
            and not self._jetpack_physics_active
            and not self._jetpack_activation_defer_remaining
            and not self._jetpack_exhaustion_tail_remaining
            and not self.parachute_active
            and not self._parachute_physics_active
            and not self._parachute_deploy_pending
        ):
            # The first post-launch owner row can trigger stock correction
            # replay. Replication spaces the following row past that replay's
            # differently-labelled history, without changing physics/pongs.
            self.last_retail_jump_loop = self.last_applied_input_loop
        self.last_fall_result = int(result or 0)
        self._sync_cached_vectors()
        # The landing row must close the advertised canopy immediately, and
        # the same landing re-arms the one-deploy-per-fall latch. Physics
        # follows through the owner handoff (retail owner still has it open).
        self.last_fall_result = self._parachute_after_move(
            dt,
            was_airborne,
            chute_pre_vz,
            chute_physics,
            self.last_fall_result,
        )
        self._check_disguise_stationary()
        now_airborne = bool(self.airborne)
        if was_airborne:
            self._fall_air_time = float(getattr(self, "_fall_air_time", 0.0)) + float(dt)
        elif now_airborne:
            self._airborne_since = time.monotonic()
        if self.last_fall_result > 0:
            server = self.connection.server if self.connection else None
            config = getattr(server, "config", None)
            if config is not None and bool(getattr(config, "fall_damage", True)):
                from server.game_rules import get_rules

                is_water_landing = self.z > 237.0
                if (
                    not is_water_landing
                    or get_rules(config).enabled(
                        "RULE_ENABLE_FALL_ON_WATER_DAMAGE"
                    )
                ):
                    amount = self.scaled_fall_damage(
                        self.last_fall_result,
                        float(getattr(self, "_fall_air_time", 0.0)),
                    )
                    if amount > 0.0:
                        self.damage(
                            amount,
                            source=None,
                            kill_type=int(C.KILL.FALL_KILL),
                        )
        if not now_airborne:
            self._fall_air_time = 0.0
            if was_airborne:
                self._rocket_jump_blast_at = None
        self._apply_client_authority_pin()
        self.last_landed = bool(was_airborne and self.grounded)
        self.last_step_delta = round(float(self.z - pre_position[2]), 4)
        if capture_debug:
            self.last_native_result = int(result or 0)
            self.last_native_post_update = self._capture_native_debug_state(
                world_object, "post_update", positions
            )

    def break_disguise(self) -> bool:
        """End an active Disguise ("- Must remain stationary")."""
        if not bool(getattr(self, "disguised", False)):
            return False
        self.disguised = False
        return True

    def _check_disguise_stationary(self) -> None:
        """Retail Disguise lasts only while its wearer stays still.

        The stock client only ever SENDS the activation (no deactivate path in
        character/gameScene/player), so the retail server cleared WorldUpdate
        state bit 0x02 itself. Any walk/jump input, or being displaced
        horizontally more than DISGUISE_MOVE_TOLERANCE from where it was put
        on, breaks it (threshold inferred; rules audit 2026-09-27 #10).
        """
        if not bool(getattr(self, "disguised", False)):
            return
        pending = getattr(self, "_disguise_pending_input", None)
        if pending is not None:
            activation_loop, deadline = pending
            # Locomotion in a retail history row is latched from the previous
            # ClientData. Compare that source label, not the row being ACKed.
            source_loop = getattr(self, "_applied_input_source_loop", None)
            if (
                time.monotonic() < deadline
                and action_clock.label_plausible(self, activation_loop)
                and (source_loop is None or source_loop < activation_loop)
            ):
                self._disguise_anchor = (float(self.x), float(self.y), float(self.z))
                return
            self._disguise_pending_input = None
        state = getattr(self, "input", None)
        if state is not None and any(
            bool(getattr(state, name, False))
            for name in ("up", "down", "left", "right", "jump")
        ):
            self.break_disguise()
            return
        anchor = getattr(self, "_disguise_anchor", None)
        if anchor is None:
            self._disguise_anchor = (float(self.x), float(self.y), float(self.z))
            return
        if math.hypot(float(self.x) - anchor[0], float(self.y) - anchor[1]) > (
            DISGUISE_MOVE_TOLERANCE
        ):
            self.break_disguise()

    def note_own_blast_push(self, kill_type: int) -> None:
        """Remember an own-rocket self-push (ROCKET_JUMP_FALL_DAMAGE_MULTIPLIER)."""
        if int(kill_type) in _ROCKET_JUMP_KILL_TYPES:
            self._rocket_jump_blast_at = time.monotonic()

    def scaled_fall_damage(self, raw: float, air_time: float) -> float:
        """Apply the stock server-only fall rules to one landing's damage.

        * ``ZERO/MAX_FALL_DAMAGE_AIR_TIME`` (1/60 s, 4/60 s): a landing whose
          airborne phase was that short (a correction or teleport, never a
          real fall) is scaled linearly from 0 to full.
        * ``ROCKET_JUMP_FALL_DAMAGE_MULTIPLIER`` (0.2): the airborne phase was
          launched by (or included) the player's own rocket blast.

        Both semantics are inferred from the constant names (rules audit
        2026-09-27 #6/#18); a normal fall is unchanged.
        """
        amount = float(raw)
        span = MAX_FALL_DAMAGE_AIR_TIME - ZERO_FALL_DAMAGE_AIR_TIME
        if span > 0.0:
            fraction = (float(air_time) - ZERO_FALL_DAMAGE_AIR_TIME) / span
            amount *= max(0.0, min(1.0, fraction))
        blast_at = getattr(self, "_rocket_jump_blast_at", None)
        if blast_at is not None:
            airborne_since = float(getattr(self, "_airborne_since", 0.0) or 0.0)
            if float(blast_at) >= airborne_since - ROCKET_JUMP_TAKEOFF_WINDOW_SECONDS:
                amount *= ROCKET_JUMP_FALL_DAMAGE_MULTIPLIER
        return amount

    def _update_jetpack(self, dt: float) -> None:
        """Advance the negotiated fuel policy using original native thrust.

        Packs 66/67/68 use jump for thrust; only UGC Builder pack 69 uses the
        toggle-hover input. After the per-pack start delay, activation pays its
        one-time cost and then drains fuel until release or exhaustion. Idle
        fuel regenerates after the post-damage refill delay. Exact activation,
        exhaustion, and damage-clock ordering are not recoverable from the
        inspected client, which consumes server fuel/activity state.
        """
        physics_was_active = bool(self._jetpack_physics_active)
        props = _JETPACK_PROPERTIES.get(self.jetpack_id)
        if props is None or not self.alive:
            self.jetpack_active = False
            self._jetpack_physics_active = False
            self._jetpack_activation_defer_remaining = 0
            self._jetpack_exhaustion_tail_remaining = 0
            self._jetpack_requires_release = False
            self._hover_since = 0.0
            return
        start_delay = float(props.get(0, 0.25))
        max_fuel = float(props.get(1, 100))
        activation_cost = float(props.get(2, 10))
        refill_rate = float(props.get(3, 10))
        drain = float(props.get(4, 75))
        refill_delay = float(props.get(6, 2.0))
        profile = profile_for(self)
        combat_pack = 66 <= self.jetpack_id <= 68
        if combat_pack:
            drain = profile.drain[self.jetpack_id - 66]
            refill_rate = profile.refill[self.jetpack_id - 66]

        # Character.set_hover accepts only UGC pack 69; normal packs retain
        # jump in the native mover. Resource activation from those controls
        # is this server's policy; client code alone does not prove it.
        activation_held = (
            self.input.hover
            if self.jetpack_id == int(C.JETPACK_UGCBUILDER)
            else self.input.jump
        )

        if activation_held or self.jetpack_active or self._jetpack_physics_active:
            self._jetpack_idle_seconds = 0.0
        else:
            self._jetpack_idle_seconds += dt

        # Preserve the existing capture-derived exhaustion tail. This is a
        # compatibility heuristic, not a phase proven by original code.
        # Physical key-up cancels this policy immediately below.
        exhaustion_tail = int(self._jetpack_exhaustion_tail_remaining)
        if activation_held and self._jetpack_requires_release and exhaustion_tail > 0:
            self.jetpack_active = False
            self._jetpack_physics_active = True
            self._jetpack_activation_defer_remaining = 0
            self._jetpack_exhaustion_tail_remaining = exhaustion_tail - 1
            self.jetpack_fuel = 0.0
            return

        # WorldUpdate supplies the owner's jetpack-active state; ClientData
        # does not acknowledge it. Retain the existing deferred server handoff
        # policy without claiming the original client adds this delay: its
        # Player setter writes native activity immediately on packet receipt.
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
                        self._jetpack_handoff_frames(
                            "jetpack_activation_defer_frames",
                            JETPACK_ACTIVATION_DEFER_FRAMES,
                            latest=False,
                        )
                    )
                    self._jetpack_exhaustion_tail_remaining = 0
                    newly_activated = True
        else:
            self._hover_since = 0.0
            self.jetpack_active = False
            # Our compatibility policy stops activity on release. Native
            # thrust also requires jump, but the inspected client does not
            # reveal when the original server stops draining released fuel.
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
            # The compatibility model drains advertised activity during its
            # deferred native handoff. Character.update_jetpack only displays
            # replicated fuel; it does not implement this resource recurrence.
            self.jetpack_fuel -= drain * dt
            if self.jetpack_fuel <= 0.0:
                self.jetpack_fuel = 0.0
                # Advertise exhaustion and preserve the compatibility tail.
                self.jetpack_active = False
                # The existing policy requires release before reactivation;
                # this latch is not proven by original-client resource code.
                self._jetpack_requires_release = True
                self._jetpack_activation_defer_remaining = 0
                self._jetpack_exhaustion_tail_remaining = (
                    self._jetpack_handoff_frames(
                        "jetpack_exhaustion_tail_frames",
                        JETPACK_EXHAUSTION_TAIL_FRAMES,
                        latest=True,
                    )
                    if self._jetpack_physics_active else 0
                )

        if (
            not self.jetpack_active
            and not self._jetpack_physics_active
            and self.jetpack_fuel < max_fuel
            and (
                not combat_pack or not profile.grounded_refill_only
                or (
                    (not self.airborne or self.wade)
                    and self._jetpack_idle_seconds + 1e-9 >= profile.refill_idle_seconds
                )
            )
        ):
            if (time.time() - self._last_damage_at) >= refill_delay:
                self.jetpack_fuel = min(max_fuel, self.jetpack_fuel + refill_rate * dt)

        if not physics_was_active and self._jetpack_physics_active:
            self._note_jetpack_physics_started()

    # ------------------------------------------------------------------
    # Parachute (equipment 72). Evidence, rules and measurements live in
    # docs/PARACHUTE.md. The canopy arithmetic is the stock native mover;
    # everything here decides WHEN the canopy is open and what it protects.
    # ------------------------------------------------------------------
    def _reset_parachute_state(self) -> None:
        """Close the canopy on both sides with no owner handoff.

        Construction, spawn and death: the retail Character clears its own
        canopy on spawn and death (0x1001700B / 0x1003393B), so no WorldUpdate
        row has to carry this transition and physics may close immediately.
        """
        self.parachute_active = False
        self._parachute_physics_active = False
        self._parachute_physics_schedule = deque()
        self._parachute_owner_state = False
        self._parachute_unsent_frames = 0
        self._parachute_deploy_last_held = False
        self._parachute_jump_last_held = False
        self._parachute_deploy_pending = False
        self._parachute_used_this_fall = False
        self._parachute_open_frames = 0
        self._parachute_fall_touched = False
        self._parachute_fallback_clock = 0
        self.last_parachute_event = None

    def _parachute_setting(self, name: str, default):
        """Numeric policy knob from config (lower-case name), else default."""
        config = getattr(
            getattr(self.connection, "server", None), "config", None
        )
        value = getattr(config, name.lower(), None) if config is not None else None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return default
        return type(default)(value)

    def _parachute_native_owner(self) -> bool:
        """Negotiated BattleSpades clients predict their own Z-key canopy."""
        return bool(profile_for(self).descending_parachute_only)

    def _parachute_immediate_physics(self) -> bool:
        """Whether canopy physics may follow the advertised state at once.

        Bots and connection-less players have no predicting owner. The native
        BattleSpades client opens its canopy in the same step as its Z edge.
        Only retail owners need the WorldUpdate handoff.
        """
        if self.connection is None or bool(getattr(self, "is_bot", False)):
            return True
        return self._parachute_native_owner()

    def _parachute_can_hold_canopy(self) -> bool:
        """Alive, carrying equipment 72, and no jetpack (no flight stacking)."""
        return bool(
            self.alive
            and int(self.parachute_id or 0) == PARACHUTE_ID
            and int(self.jetpack_id or 0) not in _JETPACK_PROPERTIES
        )

    def _parachute_ground_clearance(self) -> Optional[float]:
        """Blocks from the feet to the nearest solid voxel below the body.

        Takes the minimum over the centre and the four hull corners, so a
        player skimming a ledge does not count as high. ``None`` without a map.
        """
        server = getattr(self.connection, "server", None)
        world = getattr(getattr(server, "world_manager", None), "world", None)
        game_map = getattr(world, "map", None)
        if game_map is None:
            return None
        feet =float(self.z) + float(self._current_contact_offset())
        start = max(0, int(math.floor(feet)))
        radius = 0.45
        best = None
        for dx, dy in (
            (0.0, 0.0), (-radius, -radius), (radius, -radius),
            (-radius, radius), (radius, radius),
        ):
            try:
                ground = int(game_map.get_z(
                    int(math.floor(self.x + dx)),
                    int(math.floor(self.y + dy)),
                    start,
                ))
            except Exception:
                return None
            clearance = float(ground) - feet
            best = clearance if best is None else min(best, clearance)
        return best

    def _set_parachute_advertised(self, active: bool, reason: str) -> None:
        active = bool(active)
        if active == bool(self.parachute_active):
            return
        self.parachute_active = active
        if active:
            self._parachute_open_frames = 0
            self._parachute_used_this_fall = True
            self._parachute_deploy_pending = False
        self.last_parachute_event = {
            "active": active,
            "reason": reason,
            "loop": self.last_applied_input_loop,
            "z": round(float(self.z), 3),
            "vz": round(float(self.vz), 4),
        }
        logger.debug(
            "parachute %s %s: %s loop=%s z=%.2f vz=%.4f",
            "open" if active else "close",
            self.name,
            reason,
            self.last_applied_input_loop,
            float(self.z),
            float(self.vz),
        )

    def _update_parachute(self, dt: float = 1.0 / 60.0) -> None:
        """Advance the canopy rules for one accepted input frame.

        Retail has no parachute control (the Z binding is UGC-only and
        ClientData has no parachute field); the canopy state is server-owned
        and replicated in WorldUpdate state bit 0x01. Stock world.pyd keeps
        the airborne SPACE request only for jetpack and parachute holders
        (0x10012D32), so an airborne SPACE press opens a retail owner's canopy.
        The ClientData hover bit (patched/native Z) is accepted as well.
        """
        hover_held = bool(self.input.hover)
        jump_held = bool(self.input.jump)
        hover_pressed = hover_held and not self._parachute_deploy_last_held
        jump_pressed = jump_held and not self._parachute_jump_last_held
        self._parachute_deploy_last_held = hover_held
        self._parachute_jump_last_held = jump_held
        # Retail owners and native BattleSpades owners both deploy with an
        # airborne SPACE press; the native client predicts that edge through
        # these same rules (client parity item P1-19), and keeps Z as an extra
        # binding. Bot jump presses are locomotion only; bot AI opens a canopy
        # explicitly through the same hover action it uses for pack 69.
        pressed = hover_pressed or (
            jump_pressed
            and not bool(getattr(self, "is_bot", False))
        )
        grounded = (not self.airborne) or bool(self.wade)
        if grounded:
            self._parachute_used_this_fall = False
        if grounded or not self._parachute_can_hold_canopy():
            self._parachute_deploy_pending = False
            if self.parachute_active:
                self._set_parachute_advertised(
                    False, "grounded" if grounded else "unequipped"
                )
        elif self.parachute_active:
            self._parachute_open_frames += 1
            max_open = float(self._parachute_setting(
                "parachute_max_open_seconds", PARACHUTE_MAX_OPEN_SECONDS
            ))
            max_rise = float(self._parachute_setting(
                "parachute_max_rise_velocity", PARACHUTE_MAX_RISE_VELOCITY
            ))
            if max_open > 0.0 and self._parachute_open_frames * dt >= max_open:
                self._set_parachute_advertised(False, "timeout")
            elif float(self.vz) < -max_rise:
                self._set_parachute_advertised(False, "lifted")
        else:
            if pressed and not self._parachute_used_this_fall:
                self._parachute_deploy_pending = True
            # A press during ascent (or over a ledge) stays armed for this
            # fall; it opens only while descending with enough clearance, so
            # it can neither boost a jump nor float a hop.
            if self._parachute_deploy_pending and float(self.vz) >= 0.0:
                clearance = self._parachute_ground_clearance()
                minimum = float(self._parachute_setting(
                    "parachute_min_deploy_clearance",
                    PARACHUTE_MIN_DEPLOY_CLEARANCE,
                ))
                if clearance is None or clearance >= minimum:
                    self._set_parachute_advertised(True, "deploy")
        self._advance_parachute_physics()

    def _parachute_clock(self) -> int:
        """Label of the input frame being simulated (the owner's clock)."""
        if self.last_applied_input_loop is not None:
            return int(self.last_applied_input_loop)
        return int(self._parachute_fallback_clock)

    def _parachute_handoff_frames(self) -> int:
        """Labels from the current clock to the retail owner's onset.

        Measured on loopback (docs/PARACHUTE.md): the stock owner first moves
        with a new canopy state on label ``S + 3``, where ``S`` is the input
        label whose simulation queued the row, and never before ``N + 2``,
        where ``N`` is the newest label already received (it was sent before
        the row could arrive). Real latency adds the round trip in frames.
        """
        base = int(self._parachute_setting(
            "parachute_owner_handoff_frames", PARACHUTE_OWNER_HANDOFF_FRAMES
        ))
        frames = base
        applied = self.last_applied_input_loop
        history = getattr(self, "input_history", None)
        if applied is not None and history:
            try:
                newest = int(max(history))
            except (TypeError, ValueError):
                newest = int(applied)
            frames = max(base, min(newest - int(applied), 10) + base - 1)
        rtt_frames = 0
        peer = getattr(self.connection, "peer", None)
        rtt = getattr(peer, "roundTripTime", None)
        if isinstance(rtt, (int, float)) and not isinstance(rtt, bool):
            rtt_frames = int(round(max(0.0, float(rtt)) * 60.0 / 1000.0))
        return max(1, min(PARACHUTE_OWNER_HANDOFF_MAX_FRAMES, frames + rtt_frames))

    def _queue_parachute_physics(self, state: bool, frames: int) -> None:
        self._parachute_owner_state = bool(state)
        self._parachute_unsent_frames = 0
        self._parachute_physics_schedule.append(
            [self._parachute_clock() + max(1, int(frames)), bool(state)]
        )

    def _note_parachute_owner_row(self) -> None:
        """An owner self row was queued; start the handoff if its bit changed."""
        state = bool(self.parachute_active)
        if state == self._parachute_owner_state:
            return
        if self._parachute_immediate_physics():
            self._parachute_owner_state = state
            return
        self._queue_parachute_physics(state, self._parachute_handoff_frames())

    def _advance_parachute_physics(self) -> None:
        """Move the native canopy flag along the owner handoff timeline."""
        self._parachute_fallback_clock += 1
        if self._parachute_immediate_physics():
            self._parachute_physics_schedule.clear()
            self._parachute_owner_state = bool(self.parachute_active)
            self._parachute_unsent_frames = 0
            self._parachute_physics_active = bool(self.parachute_active)
            return
        schedule = self._parachute_physics_schedule
        clock = self._parachute_clock()
        while schedule and clock >= schedule[0][0]:
            self._parachute_physics_active = bool(schedule.popleft()[1])
        if bool(self.parachute_active) != self._parachute_owner_state:
            self._parachute_unsent_frames += 1
            if self._parachute_unsent_frames >= PARACHUTE_UNSENT_HANDOFF_FRAMES:
                self._queue_parachute_physics(
                    self.parachute_active, self._parachute_handoff_frames()
                )
        else:
            self._parachute_unsent_frames = 0

    def _parachute_after_move(
        self,
        dt: float,
        was_airborne: bool,
        pre_vz: float,
        physics_active: bool,
        native_result: int,
    ) -> int:
        """Close on landing/water, re-arm, and apply the canopy damage rule.

        The native mover zeroes the fall distance on every canopy frame, which
        would let a canopy opened a frame before impact erase any fall. A
        canopy instead protects only as far as it has actually braked: a
        landing that touched a canopy costs the damage of a free fall that
        reaches the same landing speed (never less than the native result).
        """
        if was_airborne and (physics_active or self.parachute_active):
            self._parachute_fall_touched = True
        if self.airborne and not self.wade:
            return native_result
        if self.parachute_active:
            self._set_parachute_advertised(
                False, "water" if self.wade else "landed"
            )
        self._parachute_deploy_pending = False
        self._parachute_used_this_fall = False
        if self._parachute_immediate_physics():
            self._advance_parachute_physics_now()
        touched = self._parachute_fall_touched
        self._parachute_fall_touched = False
        if not touched or not was_airborne:
            return native_result
        speed_damage = self._parachute_speed_damage(dt, pre_vz, physics_active)
        if speed_damage > max(0, int(native_result)):
            return speed_damage
        return native_result

    def _advance_parachute_physics_now(self) -> None:
        self._parachute_physics_schedule.clear()
        self._parachute_owner_state = bool(self.parachute_active)
        self._parachute_physics_active = bool(self.parachute_active)
        world_object = self._world_object
        if world_object is not None:
            try:
                world_object.parachute_active = bool(self.parachute_active)
            except Exception:
                pass

    def _parachute_gravity(self) -> float:
        server = getattr(self.connection, "server", None)
        world = getattr(getattr(server, "world_manager", None), "world", None)
        getter = getattr(world, "get_gravity", None)
        if callable(getter):
            try:
                return float(getter())
            except Exception:
                pass
        return 1.0

    def _parachute_speed_damage(
        self, dt: float, pre_vz: float, physics_active: bool
    ) -> int:
        """Free-fall-equivalent damage for this frame's landing speed."""
        gravity = self._parachute_gravity()
        dt = float(dt) if dt and dt > 0.0 else 1.0 / 60.0
        if gravity <= 0.0:
            return 0
        if physics_active:
            landing_speed = canopy_vz_step(float(pre_vz), dt, gravity, profile_for(self))
        else:
            landing_speed = (float(pre_vz) + dt * gravity) / (1.0 + dt)
        if landing_speed <= 0.0:
            return 0
        # Same recurrence as the native mover's free fall from rest; the
        # distance travelled before reaching ``landing_speed`` is the fall
        # that would have produced this impact without a canopy.
        velocity = 0.0
        distance = 0.0
        for _ in range(1800):
            velocity = (velocity + dt * gravity) / (1.0 + dt)
            if velocity >= landing_speed:
                break
            distance += velocity * dt * 32.0
        profile = self.movement_profile
        fall = distance * gravity
        minimum = float(profile.falling_damage_min_distance)
        maximum = float(profile.falling_damage_max_distance)
        span = maximum - minimum
        if span > 0.0:
            ratio = (fall - minimum) / span
        else:
            ratio = 1.0 if fall >= maximum else 0.0
        ratio = min(1.0, max(0.0, ratio))
        damage = int(float(profile.falling_damage_max_damage) * ratio)
        if float(self.z) > 237.0:
            damage = int(damage * float(profile.fall_on_water_damage_multiplier))
        return damage

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
        if getattr(self, "_applying_buffered_frame", False):
            # The buffered replay trails the arrival timeline by the queue
            # depth; ``self.input`` alternates between the two, so an edge
            # taken here would repeat once per tick of that depth.
            crouch_pressed = False
        else:
            crouch_pressed = bool(
                crouch and not getattr(self, "_crouch_edge_held", False)
            )
            self._crouch_edge_held = bool(crouch)
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
        if crouch_pressed:
            self._award_teabag_point()
        # No jump edge-detection or queuing here: the held jump flag is
        # consumed directly each tick in update() (client-pipeline mirror).

    def _award_teabag_point(self) -> None:
        """Crouch edge: retail teabag rule (server/combat_scores.py)."""

        server = self.connection.server if self.connection else None
        if server is None or not self.alive:
            return
        from server.combat_scores import record_teabag_crouch

        record_teabag_crouch(server, self)

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
        self._last_client_data_at = time.monotonic()
        # AFK: idle clients keep streaming identical rows, so only a change
        # of keys/aim counts as activity (server.conduct).
        conduct.observe_input(self, flags, orientation, action_flags)
        self._record_zoom_label(loop_count, action_flags)
        self.last_input_arrival_fresh = self._observe_input_arrival_order(
            loop_count, received_server_tick
        )
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
            # second time. A late original of a refilled label still tells
            # what the refill had to guess.
            self._salvage_late_frame(
                loop_count, flags, orientation, action_flags
            )
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
        server = self.connection.server if self.connection else None
        if self._label_far_ahead(server, loop_count, received_server_tick):
            return
        try:
            orientation = tuple(float(value) for value in orientation)
        except (TypeError, ValueError):
            orientation = ()
        if len(orientation) != 3 or not all(
            math.isfinite(value) for value in orientation
        ):
            anticheat.report(server, self, "orientation_nonfinite")
            orientation = tuple(
                self._applied_orientation or self.orientation
            )
        self._input_receive_sequence += 1
        world_manager = getattr(server, "world_manager", None)
        topology = getattr(world_manager, "topology_version", None)
        self.input_history[loop_count] = BufferedInputFrame(
            movement_flags=tuple(flags),
            orientation=orientation,
            topology_version=None if topology is None else int(topology),
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

    def _observe_input_arrival_order(
        self,
        loop_count,
        received_server_tick: Optional[int],
    ) -> bool:
        """Record where ``loop_count`` arrived; True when it is the newest.

        ClientData is ENet unsequenced, so the link may deliver labels out of
        order. The distance a label trails the newest one is a direct
        measurement of how far this link reorders datagrams sent one frame
        apart; ``input_reorder_spread_frames`` reports the recent maximum.
        """
        try:
            label = int(loop_count)
        except (TypeError, ValueError):
            return True
        newest = self._input_newest_label
        if newest is None or label > newest:
            self._input_newest_label = label
            return True
        late = newest - label
        if late > INPUT_REORDER_MAX_FRAMES:
            # The client rewrote its loop clock backwards (ClockSync).
            self._input_newest_label = label
            return True
        if late == 0:
            return False
        self.input_frames_reordered += 1
        tick = 0 if received_server_tick is None else int(received_server_tick)
        # One entry per second of server time, holding that second's largest
        # displacement: a sliding maximum over the window in ~20 entries.
        events = self._input_reorder_events
        if events and 0 <= tick - events[-1][0] < INPUT_REORDER_BUCKET_TICKS:
            if late > events[-1][1]:
                events[-1] = (events[-1][0], late)
        else:
            events.append((tick, late))
        return False

    def input_reorder_spread_frames(self, now_tick: int) -> int:
        """Largest recent arrival displacement of this player's ClientData."""
        events = self._input_reorder_events
        now_tick = int(now_tick)
        while events and now_tick - events[0][0] > INPUT_REORDER_WINDOW_TICKS:
            events.popleft()
        return max((late for _tick, late in events), default=0)

    def _salvage_late_frame(
        self,
        loop_count: int,
        flags: tuple,
        orientation: tuple,
        action_flags: tuple | None,
    ) -> bool:
        """Use the late original of a label that was refilled as lost.

        The refill simulated the label with the held input, which is exact
        for its locomotion and aim (both latched from the previous packet).
        What it could not know is what this packet carried for later frames:

        * the step after this label latches this packet's locomotion buttons
          and aim. If nothing newer has been simulated yet they are installed
          now, so that step matches the client's exactly;
        * a trigger-like button that was down only in this packet (a short
          jump or gadget tap) would otherwise never be seen. It is honoured
          once, on the next consumed frame.

        Each refilled label is salvaged at most once, and a button still held
        in a newer frame is left to that frame, so nothing fires twice.
        """
        assumed = self._synthesized_labels.pop(int(loop_count), None)
        if assumed is None:
            return False
        assumed_flags, assumed_actions = assumed
        try:
            flags = tuple(bool(value) for value in flags)
        except TypeError:
            return False
        if len(flags) != len(IDLE_INPUT_FLAGS):
            return False
        actions = None
        if action_flags is not None:
            try:
                actions = tuple(bool(value) for value in action_flags)
            except TypeError:
                actions = None
        self.input_frames_salvaged += 1
        server = self.connection.server if self.connection else None
        latch_frames = int(getattr(
            getattr(server, "config", None), "movement_input_latch_frames", 1
        ))
        queued = [self.input_history[key] for key in sorted(self.input_history)]
        in_time = bool(
            latch_frames
            and int(loop_count) == self.last_applied_input_loop
        )
        if in_time:
            try:
                aim = tuple(float(value) for value in orientation)
            except (TypeError, ValueError):
                aim = ()
            if len(aim) == 3 and all(math.isfinite(value) for value in aim):
                self._applied_orientation = aim
                self._orientation_after_synth = False
            self._pending_packet_flags = flags
            self._pending_packet_loop = int(loop_count)
        else:
            held = tuple(self._pending_packet_flags)
            latch = list(self._press_latch_flags or IDLE_INPUT_FLAGS)
            for index in PRESS_LATCH_MOVEMENT_INDICES:
                if (
                    flags[index]
                    and not bool(assumed_flags[index])
                    and not bool(held[index])
                    and not any(
                        bool(frame.movement_flags[index]) for frame in queued
                    )
                ):
                    latch[index] = True
                    self.input_presses_latched += 1
            if any(latch):
                self._press_latch_flags = tuple(latch)
        if actions is not None:
            held_actions = self._applied_action_flags or ()
            latch = list(
                self._press_latch_actions or (False,) * len(actions)
            )
            for index in PRESS_LATCH_ACTION_INDICES:
                if index >= len(actions) or index >= len(latch):
                    continue
                if (
                    actions[index]
                    and not (
                        assumed_actions is not None
                        and index < len(assumed_actions)
                        and bool(assumed_actions[index])
                    )
                    and not (
                        index < len(held_actions)
                        and bool(held_actions[index])
                    )
                    and not any(
                        frame.action_flags is not None
                        and index < len(frame.action_flags)
                        and bool(frame.action_flags[index])
                        for frame in queued
                    )
                ):
                    latch[index] = True
                    self.input_presses_latched += 1
            if any(latch):
                self._press_latch_actions = tuple(latch)
        return True

    async def simulate_tick(self, dt: float) -> None:
        """Advance at most one observed client frame per server tick.

        Consume at most one buffered client input in loop-count order. The
        1.x client's reconciliation
        (apply_player_network_correction, RE'd in docs/NETCODE_RECONCILIATION.md)
        looks up its OWN movement_history at the self-row's loop_count and, if
        the server position differs, ADJUSTs (>0.1 block) or SNAPs (>4 blocks,
        wiping history — the "random rollback"). Each authoritative position
        must therefore represent a loop label the client simulated. The client
        advances loop_count by exactly one per update (IDA: GameScene.update),
        one physics step, one history row and one unsequenced ClientData per
        label, and only rewrites the counter on a ClockSync drift of more than
        ten loops. A small missing label is therefore a lost packet for a frame
        the client did simulate; it is refilled with the held input under that
        label (``_synthesize_missing_frame``) so authority never trails by a
        frame. Wider gaps are clock jumps and are never refilled.

        A packet burst remains queued for later server ticks. Empty ticks freeze
        movement and its acknowledgement.  Every consumed or refilled frame is
        exactly one fixed step: neither arrival timing nor server starvation
        encodes a client physics duration.
        """
        # Lag-compensation target history: the state the previous tick's
        # WorldUpdate published, labelled loop_count - 1 (dead bodies too, so
        # a rewind never crosses a death). See server/lag_compensation.py.
        _record_lag_history(self)
        if not self.alive or not self.spawned:
            return

        # Peerless server bots have no ClientData stream or reconciliation
        # history. Their AI writes input/orientation directly each tick, so
        # they must take one ordinary physics step instead of entering the
        # human-client starvation freeze below.
        if self.is_bot:
            await self.update(dt)
            return

        server = self.connection.server if self.connection else None
        if self._check_starvation_timeout(server):
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
            self._starved_streak += 1
            self._backlog_over_ticks = 0
            if await self._starvation_gravity_step(server, dt):
                self._trace_input_sample(None, None, starved=True)
                return
            self._tick_idle()
            self._trace_input_sample(None, None, starved=True)
            return
        self._starved_streak = 0

        catch_up = self._input_backlog_policy(server)
        await self._consume_input_frame(server, dt)
        if (
            catch_up
            and self.alive
            and self.spawned
            and self.last_applied_input_loop is not None
            and (self.last_applied_input_loop + 1) in self.input_history
        ):
            # Backlog catch-up: a second contiguous frame whose pair was
            # proven transition-free by _input_backlog_policy.
            self.input_frames_catchup += 1
            await self._consume_input_frame(server, dt)

    async def _consume_input_frame(self, server, dt: float) -> None:
        """Simulate the oldest buffered frame (or refill one lost label)."""
        loop = min(self.input_history)
        gap_limit = int(getattr(
            getattr(server, "config", None), "input_gap_fill_limit", 8
        ))
        if (
            gap_limit > 0
            and self.last_applied_input_loop is not None
            and 1 < loop - self.last_applied_input_loop <= gap_limit + 1
        ):
            # ClientData is unsequenced on the wire and the client labels
            # every update contiguously, so a small gap is a lost packet.
            # Take that frame with the held input now; the real packet stays
            # queued for the next tick so pacing stays one frame per tick.
            await self._synthesize_missing_frame(
                self.last_applied_input_loop + 1, dt
            )
            return
        frame = self.input_history.pop(loop)
        if frame.received_server_tick is not None and server is not None:
            self.input_queue_delays.append(max(
                0,
                int(getattr(server, "loop_count", 0))
                - int(frame.received_server_tick),
            ))
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
        # Native mouse-input captures pair movement with the preceding
        # packet's orientation, just like locomotion buttons. Using current
        # aim here runs turns one history frame ahead of the retail client.
        applied_orientation = (
            (self._applied_orientation or orientation) if latch_frames else orientation
        )
        if latch_frames and self._orientation_after_synth and self._applied_orientation:
            # The client's frame for this packet used the LOST packet's aim.
            # For a continuous mouse turn the midpoint of the last known and
            # the current aim is the best estimate of that missing sample.
            mid = tuple(
                float(a) + float(b)
                for a, b in zip(self._applied_orientation, orientation)
            )
            norm = math.sqrt(sum(v * v for v in mid))
            if norm > 1e-6:
                applied_orientation = tuple(v / norm for v in mid)
        self._orientation_after_synth = False
        self.last_applied_input_loop = loop
        self.last_applied_input_synthesized = False
        action_flags = frame.action_flags
        if self._press_latch_flags:
            # A tap that lived only in a frame never simulated: once, now.
            flags = tuple(
                bool(value) or bool(latched)
                for value, latched in zip(flags, self._press_latch_flags)
            )
            self._press_latch_flags = ()
        if self._press_latch_actions and action_flags is not None:
            action_flags = tuple(
                bool(value) or (
                    index < len(self._press_latch_actions)
                    and bool(self._press_latch_actions[index])
                )
                for index, value in enumerate(action_flags)
            )
            self._press_latch_actions = ()
        self.set_orientation_vector(*applied_orientation)
        self._applying_buffered_frame = True
        try:
            self.update_input(*flags)
        finally:
            self._applying_buffered_frame = False
        if action_flags is not None:
            self.update_action_input(*action_flags)
        self._applied_action_flags = frame.action_flags
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
        try:
            await self.update(dt)
        finally:
            # The latch belongs only to physics. Shooting and remote facing
            # must continue to use the current packet's responsive aim.
            self.set_orientation_vector(*orientation)
        self._record_eye_history(loop, orientation)
        self._trace_input_sample(loop, flags)

    # -- Anti-cheat input policies ------------------------------------------
    #
    # Every behaviour change below is gated by an ``[anticheat] enforce_*``
    # switch; with the switch off the check only reports (log-only) and the
    # retail-parity input pipeline above is untouched. Bots never reach here.

    def _label_far_ahead(
        self,
        server,
        loop_count: int,
        received_server_tick: Optional[int],
    ) -> bool:
        """Whether to drop a label no stock client can send (see constants)."""
        if received_server_tick is None:
            return False
        if int(loop_count) <= int(received_server_tick) + INPUT_LABEL_AHEAD_OF_SERVER:
            # A ClockSync relabel lands near the server loop: a legit jump.
            return False
        applied = self.last_applied_input_loop
        if applied is not None and int(loop_count) <= applied + INPUT_LABEL_AHEAD_OF_APPLIED:
            return False
        enforced = anticheat.enforcing(server, "enforce_input_starvation")
        anticheat.report(
            server,
            self,
            "input_label_ahead",
            enforced=enforced,
            label=int(loop_count),
            server_tick=int(received_server_tick),
            applied=applied,
        )
        if enforced:
            self.input_frames_rejected_ahead += 1
        return enforced

    def _check_starvation_timeout(self, server) -> bool:
        """Disconnect a live body whose client stopped sending ClientData.

        Measured from the newest ClientData, the spawn, or the first tick of
        a simulation stretch (map rollover/death pauses rebase it). Returns
        True when the player was disconnected.
        """
        now = time.monotonic()
        last_simulated = self._last_simulated_at
        self._last_simulated_at = now
        if last_simulated is None or now - last_simulated > 1.0:
            self._starvation_baseline = now
        timeout = float(anticheat.setting(
            server, "starvation_timeout_seconds", 8.0
        ))
        if timeout <= 0.0 or server is None:
            return False
        last_input = max(
            float(self._last_client_data_at or 0.0),
            float(self._starvation_baseline),
            float(getattr(self, "spawned_at", 0.0) or 0.0),
        )
        silent = now - last_input
        if silent < timeout:
            self._starvation_timeout_flagged = False
            return False
        if self._starvation_timeout_flagged:
            return False
        self._starvation_timeout_flagged = True
        enforced = anticheat.enforcing(server, "enforce_input_starvation")
        anticheat.report(
            server,
            self,
            "input_starvation_timeout",
            enforced=enforced,
            seconds=round(silent, 2),
        )
        if not enforced:
            return False
        self.disconnect(int(C.DISCONNECT.ERROR_TIMEOUT))
        return True

    async def _starvation_gravity_step(self, server, dt: float) -> bool:
        """Let gravity act on an airborne body whose input stopped.

        Withholding ClientData otherwise freezes the body mid-air forever.
        Grounded/wading stalls keep the seamless freeze-and-resume path.
        Returns True when a neutral-input physics step was taken.
        """
        limit = int(anticheat.setting(server, "starvation_airborne_ticks", 24))
        if limit <= 0 or self._starved_streak < limit:
            return False
        if not self.airborne or self.wade:
            return False
        enforced = anticheat.enforcing(server, "enforce_input_starvation")
        if self._starved_streak == limit:
            anticheat.report(
                server,
                self,
                "input_starvation_airborne",
                enforced=enforced,
                ticks=int(self._starved_streak),
            )
        if not enforced:
            return False
        # Neutral locomotion (crouch kept: flipping it moves the eye 0.9),
        # no thrust, no hover. The acknowledged label does not advance, so
        # the owner's next self row corrects it onto the falling body.
        self._applying_buffered_frame = True
        try:
            self.update_input(
                False, False, False, False, False,
                bool(self.input.crouch), False, False,
            )
        finally:
            self._applying_buffered_frame = False
        self.input.hover = False
        self.input_frames_starvation_steps += 1
        await self.update(dt)
        return True

    def _input_backlog_policy(self, server) -> bool:
        """Track queue delay and decide this tick's backlog catch-up.

        Returns True when a second contiguous frame may be simulated this
        tick. When the pair is not provably transition-free the oldest
        frames are dropped down to the cap instead (enforced mode only).
        """
        if not self.input_history or server is None:
            return False
        if self._retail_idle_backlog_pair_safe(server):
            # A render/network stall otherwise leaves a permanent FIFO delay
            # in observation mode. Consume one extra *received* idle frame;
            # walking, actions and native clients retain their normal policy.
            return True
        head = self.input_history[min(self.input_history)]
        if head.received_server_tick is None:
            return False
        cap = max(1, int(anticheat.setting(server, "backlog_max_frames", 6)))
        delay = int(getattr(server, "loop_count", 0)) - int(head.received_server_tick)
        if self._backlog_catchup:
            if delay <= max(1, cap // 2):
                self._backlog_catchup = False
                self._backlog_over_ticks = 0
                return False
        else:
            if delay <= cap:
                self._backlog_over_ticks = 0
                return False
            self._backlog_over_ticks += 1
            window = max(1, int(getattr(server, "tick_rate", 60) or 60))
            if self._backlog_over_ticks <= window:
                return False
            enforced = anticheat.enforcing(server, "enforce_input_backlog")
            anticheat.report(
                server,
                self,
                "input_backlog",
                enforced=enforced,
                delay=delay,
                depth=len(self.input_history),
            )
            if not enforced:
                self._backlog_over_ticks = 0
                return False
            self._backlog_catchup = True
        if self._backlog_pair_safe(server):
            return True
        excess = len(self.input_history) - cap
        if excess > 0:
            self._drop_backlog_frames(excess)
        return False

    def _retail_idle_backlog_pair_safe(self, server) -> bool:
        """Drain only settled retail idle input, with no nearby moving actor."""
        if (
            self.is_bot
            or getattr(self.connection, "flight_profile_capable", None) is not False
            or len(self.input_history) <= 2
            or not self.grounded
            or self.wade
            or max(abs(self.vx), abs(self.vy), abs(self.vz)) > 1e-7
            or self.jetpack_id
            or any(self._pending_packet_flags)
            or any(self._press_latch_flags)
            or any(self._press_latch_actions)
            or any((self.input.up, self.input.down, self.input.left,
                    self.input.right, self.input.jump, self.input.crouch,
                    self.input.sprint, self.input.hover))
            or not self._backlog_pair_safe(server)
        ):
            return False
        first = self.input_history[self.last_applied_input_loop + 1]
        second = self.input_history[self.last_applied_input_loop + 2]
        if any(first.movement_flags) or any(second.movement_flags):
            return False
        actions = first.action_flags
        # Pickup, weapon-display and palette bits are stable capabilities,
        # not held actions; stock idle packets normally keep them enabled.
        if (actions != second.action_flags
                or actions != self._applied_action_flags
                or any(enabled for index, enabled in enumerate(actions or ())
                       if index not in (3, 4, 8))):
            return False
        return not any(
            (x - self.x) ** 2 + (y - self.y) ** 2 + (z - self.z) ** 2 < 16.0
            for x, y, z, _height in self._build_player_collision_positions()
        )

    def _backlog_pair_safe(self, server) -> bool:
        """Two frames may share a tick only across no state transition.

        _simulate_players documents the hazard: batches crossing a terrain
        mutation or a jetpack/hover transition reconcile old client history
        against new server state (ADJUST/SNAP). Require two contiguous
        labels, no topology change since the older one arrived, no queued
        mutation, no pending impulse, and no jetpack/parachute/hover use.
        """
        applied = self.last_applied_input_loop
        if applied is None:
            return False
        first = self.input_history.get(applied + 1)
        second = self.input_history.get(applied + 2)
        if first is None or second is None:
            return False
        world_manager = getattr(server, "world_manager", None)
        topology = getattr(world_manager, "topology_version", None)
        if topology is not None and (
            first.topology_version != topology
            or second.topology_version != topology
        ):
            return False
        mutations = getattr(server, "world_mutations", None)
        if int(getattr(mutations, "pending_count", 0) or 0) > 0:
            return False
        if self._pending_velocity_impulses or self._pending_explosion_impulses:
            return False
        if (
            self.jetpack_active
            or self._jetpack_physics_active
            or self._jetpack_activation_defer_remaining
            or self._jetpack_exhaustion_tail_remaining
            or self.parachute_active
            or self._parachute_deploy_pending
            or self._parachute_physics_active
            or self._parachute_physics_schedule
        ):
            return False
        pack = bool(self.jetpack_id or self.parachute_id)
        for flags in (
            first.movement_flags,
            second.movement_flags,
            self._pending_packet_flags,
        ):
            if pack and len(flags) > 4 and flags[4]:
                return False
        for frame in (first, second):
            actions = frame.action_flags
            if actions is not None and len(actions) > 7 and actions[7]:
                return False
        return True

    def _drop_backlog_frames(self, count: int) -> None:
        """Discard the oldest queued frames (enforced backlog only).

        The dropped labels are never simulated; their input is latched so
        the next real frame composes exactly as after them, and the label
        cursor advances past them so they are not refilled as lost frames.
        """
        for _ in range(max(0, int(count))):
            if not self.input_history:
                return
            loop = min(self.input_history)
            frame = self.input_history.pop(loop)
            self.last_applied_input_loop = loop
            self._pending_packet_flags = frame.movement_flags
            self._pending_packet_loop = loop
            self._pending_packet_received_server_tick = frame.received_server_tick
            self._pending_packet_received_owner_sequence = (
                frame.received_owner_sequence
            )
            self._pending_packet_wire_unknown_byte = frame.wire_unknown_byte
            self._applied_orientation = frame.orientation
            if frame.action_flags is not None:
                self.update_action_input(*frame.action_flags)
            self._apply_velocity_impulses_through(loop)
            self._apply_explosion_impulses_through(frame.received_input_sequence)
            self.input_frames_backlog_dropped += 1
            self.input_frames_dropped += 1

    def input_queue_delay_stats(self) -> dict:
        """p50/max ticks consumed frames waited in the queue (recent window)."""
        samples = sorted(self.input_queue_delays)
        if not samples:
            return {"samples": 0, "p50": None, "max": None}
        return {
            "samples": len(samples),
            "p50": samples[len(samples) // 2],
            "max": samples[-1],
        }

    async def _synthesize_missing_frame(self, loop: int, dt: float) -> None:
        """Simulate one client frame whose ClientData never arrived.

        The client held the previous packet's buttons in that frame with
        overwhelming probability, and under the one-frame latch the step for
        label ``loop`` already uses the previous packet's locomotion buttons
        and orientation.  The pending packet state is left untouched so the
        next real packet composes exactly as it would have after the lost one,
        which keeps the step count equal to the client's frame count.

        The lost packet may still have carried an input change for this very
        label (the client applies crouch and aim from the current packet), so
        no owner self row is stamped with a refilled label
        (``last_applied_input_synthesized``); the next real label carries the
        exact state. Measured: a lost crouch release produced a 0.9-block
        native correction when its refilled row was sent.
        """
        flags = tuple(self._pending_packet_flags)
        orientation = self._applied_orientation or self.orientation
        self.last_applied_input_loop = int(loop)
        self.last_applied_input_synthesized = True
        self._orientation_after_synth = True
        # Remember what was assumed: the original may still arrive late.
        self._synthesized_labels[int(loop)] = (
            flags, self._applied_action_flags
        )
        while len(self._synthesized_labels) > SYNTHESIZED_LABEL_LIMIT:
            del self._synthesized_labels[min(self._synthesized_labels)]
        self.set_orientation_vector(*orientation)
        self._applying_buffered_frame = True
        try:
            self.update_input(*flags)
        finally:
            self._applying_buffered_frame = False
        self._applied_input_flags = flags
        self.input_frames_synthesized += 1
        self._apply_velocity_impulses_through(int(loop))
        await self.update(dt)
        self._record_eye_history(int(loop), orientation)
        self._trace_input_sample(int(loop), flags, synthesized=True)

    def _trace_input_sample(
        self,
        loop: Optional[int],
        flags: tuple | None,
        starved: bool = False,
        synthesized: bool = False,
    ) -> None:
        """Optional per-tick input audit record (``[debug] debug_selfrow``)."""
        server = self.connection.server if self.connection else None
        manager = getattr(server, "debug_parity", None)
        writer = getattr(manager, "write_input_sample", None)
        if callable(writer):
            writer(self, loop, flags, starved=starved, synthesized=synthesized)

    def _tick_idle(self) -> None:
        """Per-tick housekeeping on a held frame (no physics step)."""
        self._advance_reload_and_announce()

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
            world_object.hover = bool(
                hover
                and self.jetpack_id == int(C.JETPACK_UGCBUILDER)
            )
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
        # 0x04 is jetpack active, 0x08 is its separate passive/reduced-gravity
        # state, and 0x10 is can_display_weapon.
        byte = 0
        if self.input.primary_fire:
            byte |= 0x01
        if self.input.secondary_fire:
            byte |= 0x02
        # 0x04 = jetpack active (display + pack-specific flight on the client).
        # SAFE here ONLY because it is gated on jetpack_active: the server's fuel
        # model uses held SPACE for packs 66-68 and hover/Z for UGC pack 69,
        # setting this only while the pack is firing with fuel and clearing it
        # on physical release or exhaustion. That is the OPPOSITE of the old
        # jump-stuck bug, which came from 0x08/0x10 being set UNCONDITIONALLY
        # (echoed from always-on ClientData bits) so the client believed it was
        # perma-jetpacking and skipped gravity forever. A transient, truthful
        # 0x04 makes the jetpack visible on others AND drives server-authoritative
        # flight; when it clears, normal gravity + jumping resume. (If a jump
        # regression shows up, this bit is the first suspect — revert to 0.)
        if getattr(self, "jetpack_active", False):
            byte |= 0x04
            if self.jetpack_id == int(C.JETPACK2):
                byte |= 0x08
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
        # GameScene.process_packet_world_update line 3310 sends this bit to
        # Character.set_hover.  It is the UGC Builder's toggled Z state, not
        # the ClientData hover/deploy extension used by our parachute patch.
        if (
            self.jetpack_id == int(C.JETPACK_UGCBUILDER)
            and self.input.hover
        ):
            byte |= 0x04
        # 0x08 is Character.set_touching_goo (gameScene.pyd
        # process_packet_world_update -> character.pyd 0x10029150): it only
        # starts/stops the Chemical Bomb burn loop (A2920/A2921/A2922). It is
        # NOT a water flag: advertising wade here made every wading player
        # play the chemical-burn loop on every observer.
        if getattr(self, "touching_goo", False):
            byte |= 0x08
        return byte

    def world_update_snapshot(self) -> Tuple[Tuple[float, float, float], ...]:
        # (pos, orient, vel, ping, pong, hp, inp, action, state, tool,
        #  pickup, jetpack_fuel, spawn_protection_timer, weapon_deployment_yaw)
        # `pong` carries wu_ack_loop — the client input loop_count this row's
        # position corresponds to. It is what the client pairs against its own
        # movement_history to decide NO-OP / ADJUST / SNAP.
        spawn_protection = self.spawn_protection_remaining()
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
            spawn_protection,
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
