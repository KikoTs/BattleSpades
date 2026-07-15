"""Pickle-safe messages shared by the server and the bot worker.

Only immutable primitive data crosses the process boundary.  Keeping native
``Player``, packet, VXL, and mode instances out of these records prevents the
worker from accidentally becoming another gameplay authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TypeAlias


Vector3: TypeAlias = tuple[float, float, float]
VoxelCoordinate: TypeAlias = tuple[int, int, int]


class BotActionKind(str, Enum):
    """Actions a worker may request from the authoritative gateway."""

    NONE = "none"
    FIRE = "fire"
    RELOAD = "reload"
    MELEE = "melee"
    BUILD = "build"
    BUILD_LINE = "build_line"
    MINE = "mine"
    PLACE_PREFAB = "place_prefab"
    DEPLOY = "deploy"
    ORIENTED = "oriented"


class MovementAffordance(str, Enum):
    """Topology edge selected by worker navigation."""

    WALK = "walk"
    CROUCH = "crouch"
    JUMP = "jump"
    DROP = "drop"
    JETPACK = "jetpack"
    BREACH = "breach"
    BUILD_STEP = "build_step"
    BUILD_BRIDGE = "build_bridge"
    PLACE_PREFAB = "place_prefab"


class StimulusKind(str, Enum):
    """Information a bot can hear or receive without direct vision."""

    SHOT = "shot"
    EXPLOSION = "explosion"
    BLOCK_DESTROYED = "block_destroyed"
    FOOTSTEP = "footstep"
    OBJECTIVE = "objective"
    DEPLOYABLE = "deployable"
    TEAM_SIGHTING = "team_sighting"


@dataclass(frozen=True, slots=True)
class VoxelChange:
    """One committed canonical terrain change."""

    x: int
    y: int
    z: int
    solid: bool
    color: int = 0

    @property
    def coordinate(self) -> VoxelCoordinate:
        """Return the cell key used by terrain-delta coalescing."""

        return self.x, self.y, self.z


@dataclass(frozen=True, slots=True)
class MapSnapshot:
    """Complete navigation base for one map epoch.

    ``raw_vxl`` is serialized only by the bridge thread.  The gameplay thread
    merely publishes the already-owned immutable bytes object.
    """

    map_epoch: int
    topology_version: int
    raw_vxl: bytes
    mode_id: str
    map_name: str = ""
    changed_cells: tuple[VoxelChange, ...] = ()


@dataclass(frozen=True, slots=True)
class WorldDelta:
    """Coalesced committed changes following a :class:`MapSnapshot`."""

    map_epoch: int
    topology_version: int
    changed_cells: tuple[VoxelChange, ...]


@dataclass(frozen=True, slots=True)
class PlayerSnapshot:
    """Read-only player state available to worker perception."""

    player_id: int
    generation: int
    team: int
    class_id: int
    alive: bool
    spawned: bool
    position: Vector3
    eye: Vector3
    orientation: Vector3
    velocity: Vector3
    health: int
    tool: int
    blocks: int
    ammo_clip: int
    ammo_reserve: int
    is_bot: bool
    weapon_tool: int = -1
    loadout: tuple[int, ...] = ()
    prefabs: tuple[str, ...] = ()
    oriented_stock: tuple[tuple[int, int], ...] = ()
    carried_entity_id: int = -1
    jetpack_id: int = 0
    jetpack_fuel: float = 0.0
    grounded: bool = True
    wade: bool = False
    reloading: bool = False
    # Latest authoritative action result for worker-side retry/replan policy.
    last_action_kind: str = ""
    last_action_accepted: bool = True
    last_action_position: Vector3 | None = None
    last_action_frame: int = -1
    last_action_at: float = 0.0


@dataclass(frozen=True, slots=True)
class EntitySnapshot:
    """Minimal replicated entity state used by objective/cover policies."""

    entity_id: int
    entity_type: int
    team: int
    owner_id: int
    position: Vector3
    alive: bool = True
    kind: str = ""
    tool_id: int = -1
    velocity: Vector3 = (0.0, 0.0, 0.0)
    blast_radius: float = 0.0
    detonate_at: float = 0.0
    hazardous: bool = False


@dataclass(frozen=True, slots=True)
class ObjectiveSnapshot:
    """Mode-sanctioned objective information visible to a bot."""

    kind: str
    team: int
    position: Vector3
    carrier_id: int = -1
    state: int = 0


@dataclass(frozen=True, slots=True)
class Stimulus:
    """Approximate, time-bounded non-visual information."""

    kind: StimulusKind
    position: Vector3
    created_at: float
    expires_at: float
    source_id: int = -1
    team: int = -1
    uncertainty: float = 0.0


@dataclass(frozen=True, slots=True)
class PerceptionFrame:
    """One observer's bounded strategic input at a point in server time."""

    frame_id: int
    map_epoch: int
    mode_epoch: int
    topology_version: int
    observer_id: int
    observer_generation: int
    created_at: float
    mode_id: str
    players: tuple[PlayerSnapshot, ...]
    profile: BotProfile | None = None
    entities: tuple[EntitySnapshot, ...] = ()
    objectives: tuple[ObjectiveSnapshot, ...] = ()
    stimuli: tuple[Stimulus, ...] = ()
    # Enum name published by the authoritative mode (for example ACTIVE or
    # COUNTDOWN).  Workers receive no mode object and may only branch on this
    # immutable phase label.
    mode_phase: str = ""


@dataclass(frozen=True, slots=True)
class MovementIntent:
    """World-space desired locomotion interpreted by the 60 Hz motor."""

    direction: Vector3 = (0.0, 0.0, 0.0)
    jump: bool = False
    crouch: bool = False
    sneak: bool = False
    sprint: bool = False
    affordance: MovementAffordance = MovementAffordance.WALK


@dataclass(frozen=True, slots=True)
class LookIntent:
    """A world-space point the bounded aim motor should approach."""

    target: Vector3
    visible: bool = False
    # Live-track authorization.  While the worker keeps confirming visibility
    # of this player, the 60 Hz director motor may refine the aim point from
    # the gameplay-thread registry, bounded by the director's short lock
    # lease.  The worker remains the only authority for WHO and WHEN.
    target_player_id: int = -1
    target_generation: int = 0
    # Worker-chosen aim offset added to the live eye z (AoS z increases
    # downward: +1.15 torso, 0.0 head, +1.0 zombie center-mass).
    aim_offset_z: float = 0.0


@dataclass(frozen=True, slots=True)
class BotAction:
    """A single authoritative action request.

    ``position`` and ``argument`` are interpreted only by the specific public
    gateway method for ``kind``.  Unknown or incomplete combinations fail
    closed.
    """

    kind: BotActionKind = BotActionKind.NONE
    tool_id: int = -1
    position: Vector3 | None = None
    end_position: Vector3 | None = None
    argument: str = ""
    face: int = -1
    yaw: float = 0.0
    # FIRE only: shots per burst and the pause after each burst.  Zero means
    # continuous fire at the weapon cadence (legacy behavior).
    burst: int = 0
    burst_pause: float = 0.0


@dataclass(frozen=True, slots=True)
class BotIntent:
    """Short-lived worker suggestion validated by :class:`BotDirector`."""

    bot_id: int
    bot_generation: int
    frame_id: int
    map_epoch: int
    mode_epoch: int
    topology_version: int
    created_at: float
    expires_at: float
    movement: MovementIntent
    look: LookIntent | None = None
    tool_id: int = -1
    action: BotAction = BotAction()
    debug_goal: Vector3 | None = None
    debug_path: tuple[Vector3, ...] = ()
    debug_role: str = ""


@dataclass(frozen=True, slots=True)
class BotProfile:
    """Persistent humanization parameters for one bot identity."""

    name: str
    difficulty: str
    skill: float
    aggression: float
    caution: float
    teamwork: float
    creativity: float
    reaction_time: float
    tracking_delay: float
    turn_speed: float
    turn_acceleration: float
    recoil_control: float
    burst_discipline: float
    preferred_range: float
    aim_noise: float
    class_preferences: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class WorkerShutdown:
    """Sentinel requesting an orderly worker exit."""

    reason: str = "server shutdown"


WorkerInput: TypeAlias = MapSnapshot | WorldDelta | PerceptionFrame | WorkerShutdown
WorkerOutput: TypeAlias = BotIntent
